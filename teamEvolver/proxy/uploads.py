"""Skill synchronization helpers for the teamEvolver service.

The service does not upload model-session traffic into evolution. Evolution
input is accepted only through the evolve server's
``/ingest_session`` endpoint (username + session payload). This mixin keeps
skill pull/reload and background-task drain behavior for service runtime use.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
from typing import Optional

logger = logging.getLogger(__name__)


class UploadsMixin:
    """Session upload, evolve trigger, and skill pull/reload state machine."""

    async def _await_background_tasks(self, timeout_seconds: float) -> None:
        pending = [t for t in list(self._background_tasks) if not t.done()]
        if not pending:
            return
        done, still_pending = await asyncio.wait(pending, timeout=timeout_seconds)
        if still_pending:
            logger.warning(
                "[Proxy] background drain timeout: %d task(s) still running",
                len(still_pending),
            )
            for task in still_pending:
                task.cancel()
            await asyncio.gather(*still_pending, return_exceptions=True)
        else:
            logger.info("[Proxy] background drain complete (%d task(s))", len(done))

    def _start_skill_reload_polling(self) -> None:
        if not self.config.sharing_enabled:
            return
        mode = str(getattr(self.config, "sharing_skill_reload_mode", "") or "poll").strip().lower()
        if mode != "poll":
            return
        if self._skill_reload_task is not None and not self._skill_reload_task.done():
            return
        self._skill_reload_task = asyncio.create_task(self._skill_reload_poll_loop())
        self._skill_reload_task.add_done_callback(self._task_done_cb)
        logger.info(
            "[SkillHub] skill reload polling enabled interval=%ds",
            self._skill_reload_interval_seconds,
        )

    async def _skill_reload_poll_loop(self) -> None:
        consecutive_failures = 0
        first_pull = True
        try:
            while True:
                if first_pull:
                    first_pull = False
                else:
                    jitter = random.uniform(0, self._skill_reload_interval_seconds * 0.1)
                    backoff = min(consecutive_failures * 5.0, 60.0)
                    await asyncio.sleep(self._skill_reload_interval_seconds + jitter + backoff)
                try:
                    await self._pull_skills_from_cloud()
                    consecutive_failures = 0
                except Exception as exc:
                    consecutive_failures += 1
                    logger.warning(
                        "[SkillHub] skill reload poll failed (streak=%d): %s",
                        consecutive_failures,
                        exc,
                    )
        except asyncio.CancelledError:
            logger.info("[SkillHub] skill reload polling stopped")
            raise

    # ------------------------------------------------------------------ #
    # Session data upload (cloud)                                          #
    # ------------------------------------------------------------------ #

    async def _upload_session_data(
        self,
        session_id: str,
        turns: list[dict],
    ) -> bool:
        """Deprecated no-op.

        Proxy-originated sessions must not enter evolution. Submit sessions to
        the evolve server's ``/ingest_session`` endpoint instead.
        """
        logger.info("[Proxy] session upload disabled; use /ingest_session (session=%s)", session_id)
        return False

    def _maybe_upload_session_snapshot(self, session_id: str, user_turn_num: int) -> None:
        """Deprecated no-op: proxy sessions no longer participate in evolution."""
        return

    async def _upload_session_snapshot_and_trigger(self, session_id: str, turns: list[dict]) -> None:
        """Deprecated no-op: snapshot uploads from proxy are disabled."""
        logger.info("[Proxy] session snapshot upload disabled; use /ingest_session (session=%s)", session_id)
        return

    def _evolve_trigger_debounce_seconds(self) -> float:
        raw = os.environ.get("TEAMEVOLVER_EVOLVE_TRIGGER_DEBOUNCE_S", "15")
        try:
            return max(0.0, float(raw))
        except ValueError:
            return 15.0

    def _schedule_evolve_trigger(self) -> bool:
        """Schedule one debounced evolve trigger. Returns True when scheduled."""
        task = getattr(self, "_evolve_trigger_task", None)
        if task is not None and not task.done():
            logger.info("[SkillHub] evolve trigger already scheduled; coalescing request")
            return False
        delay = self._evolve_trigger_debounce_seconds()
        self._evolve_trigger_task = self._safe_create_task(self._trigger_evolve_after_delay(delay))
        logger.info("[SkillHub] evolve trigger scheduled in %.1fs", delay)
        return True

    async def _trigger_evolve_after_delay(self, delay: float) -> None:
        if delay > 0:
            await asyncio.sleep(delay)
        await self._trigger_evolve()

    async def _trigger_evolve(self) -> None:
        url = str(getattr(self.config, "evolve_server_url", "") or "").strip().rstrip("/")
        if not url:
            return
        import httpx

        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=300.0) as client:
                    resp = await client.post(f"{url}/trigger")
                    resp.raise_for_status()
                    result = resp.json()
                logger.info("[SkillHub] triggered evolve server: %s", url)
                if isinstance(result, dict) and int(result.get("uploaded_skills") or 0) > 0:
                    await self._pull_skills_from_cloud()
                return
            except Exception as e:
                if attempt < 2:
                    await asyncio.sleep(1.0 * (attempt + 1))
                else:
                    logger.warning("[SkillHub] evolve trigger failed after 3 attempts: %s", e)

    # ------------------------------------------------------------------ #
    # Skill pull (cloud -> local)                                          #
    # ------------------------------------------------------------------ #

    async def _pull_skills_from_cloud(self, skip_names: Optional[set[str]] = None) -> None:
        """Pull latest skills from cloud storage and reload the skill manager.

        This is a *read-only* operation — local skills are never pushed
        automatically.  Use ``teamEvolver skills push`` for explicit uploads.
        """
        try:
            from ..skills.hub import SkillHub

            team_hub = SkillHub.team_from_config(self.config)
            team_result = team_hub.pull_skills(
                self.config.skills_dir,
                mirror=False,
                skip_names=skip_names,
            )
            logger.info(
                "[SkillHub] skill pull: team=%d downloaded, team=%d total remote",
                team_result["downloaded"],
                team_result.get("total_remote", 0),
            )
            if team_result.get("failed_names"):
                logger.warning("[SkillHub] skill pull failed names: %s", ", ".join(team_result["failed_names"]))
            if self.skill_manager and (
                team_result.get("downloaded", 0) > 0
                or team_result.get("deleted", 0) > 0
                or team_result.get("restored_from_backup", False)
            ):
                self.skill_manager.reload()
        except Exception as e:
            logger.warning("[SkillHub] skill pull failed: %s", e)
