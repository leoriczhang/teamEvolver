"""Skill runtime-cache helpers for the teamEvolver service.

Session ingestion is owned by ``session_ingestion``. This mixin keeps local
Skill cache refresh and background-task drain behavior for service runtime use.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random

logger = logging.getLogger(__name__)

_SKILL_RELOAD_INTERVAL_SECONDS = 30


class UploadsMixin:
    """Evolution trigger and Skill runtime-cache refresh state machine."""

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
        if self._skill_reload_task is not None and not self._skill_reload_task.done():
            return
        self._skill_reload_task = asyncio.create_task(self._skill_reload_poll_loop())
        self._skill_reload_task.add_done_callback(self._task_done_cb)
        logger.info(
            "[SkillHub] skill reload polling enabled interval=%ds",
            _SKILL_RELOAD_INTERVAL_SECONDS,
        )

    async def _skill_reload_poll_loop(self) -> None:
        consecutive_failures = 0
        first_pull = True
        try:
            while True:
                if first_pull:
                    first_pull = False
                else:
                    jitter = random.uniform(0, _SKILL_RELOAD_INTERVAL_SECONDS * 0.1)
                    backoff = min(consecutive_failures * 5.0, 60.0)
                    await asyncio.sleep(_SKILL_RELOAD_INTERVAL_SECONDS + jitter + backoff)
                try:
                    await self._pull_all_tenant_skill_caches()
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
    # Evolution trigger                                                    #
    # ------------------------------------------------------------------ #

    def _evolve_trigger_debounce_seconds(self) -> float:
        raw = os.environ.get("TEAMEVOLVER_EVOLVE_TRIGGER_DEBOUNCE_S", "15")
        try:
            return max(0.0, float(raw))
        except ValueError:
            return 15.0

    def _schedule_evolve_trigger(self) -> bool:
        """Schedule one debounced evolve trigger. Returns True when scheduled."""
        if bool(getattr(self.config, "storage_pg_enabled", False)):
            from ..tenants.registry import current_tenant_id

            # Wake this account's bounded scheduler; never loop back to the
            # unauthenticated default tenant over HTTP.
            tid = current_tenant_id()
            due = asyncio.get_running_loop().time() + self._evolve_trigger_debounce_seconds()
            self._evolve_next_due[tid] = min(self._evolve_next_due.get(tid, due), due)
            return True
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
                    await self._refresh_skill_runtime_cache()
                return
            except Exception as e:
                if attempt < 2:
                    await asyncio.sleep(1.0 * (attempt + 1))
                else:
                    logger.warning("[SkillHub] evolve trigger failed after 3 attempts: %s", e)

    # ------------------------------------------------------------------ #
    # Skill pull (cloud -> local)                                          #
    # ------------------------------------------------------------------ #

    async def _pull_all_tenant_skill_caches(self) -> None:
        if not bool(getattr(self.config, "storage_pg_enabled", False)):
            await self._refresh_skill_runtime_cache()
            return

        from ..tenants.registry import (
            TenantRegistry,
            reset_current_tenant,
            set_current_tenant,
        )

        registry = TenantRegistry(self.config)
        tenants = await asyncio.to_thread(registry.list_tenants)
        for tenant in tenants:
            token = set_current_tenant(tenant)
            try:
                await self._refresh_skill_runtime_cache()
            finally:
                reset_current_tenant(token)

    async def _refresh_skill_runtime_cache(self) -> None:
        """Materialize the latest stored Skills into the tenant runtime cache.

        This is a read-only operation. The cache is rebuildable and is never
        treated as a publication source.
        """
        try:
            from team_skills.library.hub import SkillHub
            from ..tenants.registry import (
                current_tenant_id,
                effective_config,
                get_current_tenant,
            )

            tenant = get_current_tenant()
            tenant_id = current_tenant_id()
            config = effective_config(None, tenant, self.config)
            team_hub = SkillHub.team_from_config(config, tenant_id=tenant_id)
            team_result = team_hub.materialize_runtime_cache(
                self._skill_runtime_cache_dir(),
            )
            logger.info(
                "[SkillHub] skill pull: tenant=%s team=%d downloaded, team=%d total remote",
                tenant_id,
                team_result["downloaded"],
                team_result.get("total_remote", 0),
            )
            if team_result.get("failed_names"):
                logger.warning("[SkillHub] skill pull failed names: %s", ", ".join(team_result["failed_names"]))
        except Exception as e:
            logger.warning("[SkillHub] skill pull failed: %s", e)
