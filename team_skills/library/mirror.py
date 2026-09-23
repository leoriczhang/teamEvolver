"""Async mirror of the team skill library to OpenViking.

teamEvolver's own ledgers (registry / manifest / version history / session
queue / validation) live on the built-in local backend; remote Agents read the
team skills from ``viking://resources/{root_prefix}/skills/<name>/``. This
module keeps that cross-machine read surface populated without putting
OpenViking on the evolution hot path: every local publish/delete enqueues a
mirror delivery in a durable local spool, and a background flush delivers
pending items with the spool's retry/backoff semantics. OpenViking outages
therefore never block evolution — deliveries simply retry later.

Only the ``skills/<name>/`` subtree is mirrored. The registry
(``evolve_skill_registry.json``), the manifest (``manifest.json``) and the
version history (``skills/<name>/versions/``) are teamEvolver-internal ledgers
and stay on the local store.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any, Optional

from teamEvolver.integrations.hermes_delivery import HermesDeliverySpool

logger = logging.getLogger(__name__)

_MIRROR_KIND_PREFIX = "skill_mirror"
_PRODUCER_ID = "skill-mirror:viking"


def default_spool_dir() -> Path:
    return Path.home() / ".teamEvolver" / "skill_mirror_spool"


class VikingSkillMirror:
    """Durable outbox mirroring skill bundles from the local store to OpenViking."""

    def __init__(
        self,
        *,
        spool_dir: str | Path | None = None,
        viking_hub: Any = None,
        sequence_bucket: Any = None,
    ) -> None:
        path = Path(spool_dir).expanduser() if spool_dir else default_spool_dir()
        self._spool = HermesDeliverySpool(path, producer_id=_PRODUCER_ID)
        self._viking_hub = viking_hub
        # Bucket used only for the monotonic per-skill delivery sequence. The
        # evolve server passes its skill bucket; hub-based callers pass the
        # local hub's bucket. None falls back to spool-local sequencing.
        self._sequence_bucket = sequence_bucket

    # ------------------------------------------------------------------ #
    # Config-driven construction                                           #
    # ------------------------------------------------------------------ #

    @classmethod
    def from_config(cls, config) -> Optional["VikingSkillMirror"]:
        """Build a mirror when the skill backend is local and mirroring is on.

        Returns ``None`` when mirroring is disabled, the local hub is not
        actually local-backed, or no viking endpoint is configured (nothing to
        mirror to).
        """
        if not bool(getattr(config, "sharing_skill_mirror_enabled", True)):
            return None
        if not bool(getattr(config, "sharing_enabled", False)):
            return None
        from team_skills.library.hub import SkillHub

        hub = SkillHub.team_from_config(config)
        return cls.from_hub(hub, spool_dir=getattr(config, "sharing_skill_mirror_spool_dir", "") or None)

    @classmethod
    def from_hub(cls, hub, *, spool_dir: str | Path | None = None) -> Optional["VikingSkillMirror"]:
        """Build a mirror for an already-constructed team hub."""
        mirror_hub = getattr(hub, "mirror_viking_hub", None)
        if mirror_hub is None:
            return None
        return cls(
            spool_dir=spool_dir,
            viking_hub=mirror_hub,
            sequence_bucket=getattr(hub, "_bucket", None),
        )

    # ------------------------------------------------------------------ #
    # Enqueue                                                              #
    # ------------------------------------------------------------------ #

    def _sequence(self, skill_name: str) -> int:
        # Monotonic per skill. When a sequence bucket is available the counter
        # persists across restarts; otherwise fall back to the spool's own
        # records so redeliveries stay ordered within one spool directory.
        bucket = self._sequence_bucket
        if bucket is not None:
            seq_key = f"_skill_mirror_seq/{skill_name}.txt"
            try:
                raw = bucket.get_object(seq_key).read().decode("utf-8")
                sequence = int(raw or "0") + 1
            except Exception:  # noqa: BLE001 - missing/invalid restarts at 1
                sequence = 1
            try:
                bucket.put_object(seq_key, str(sequence).encode("utf-8"))
            except Exception:  # noqa: BLE001 - ordering degrades, never blocks
                pass
            return sequence
        highest = 0
        for record in self._spool._records():
            if str(record.get("aggregate_id") or "") == str(skill_name):
                highest = max(highest, int(record.get("sequence") or 0))
        return highest + 1

    def enqueue_skill(self, skill_name: str, bundle_files: dict[str, bytes]) -> dict[str, Any]:
        """Enqueue a full bundle mirror for one skill."""
        name = str(skill_name or "").strip()
        if not name:
            raise ValueError("skill_name is required")
        payload = {
            "op": "push",
            "skill_name": name,
            "files": {
                rel_path: content.hex()
                for rel_path, content in sorted(bundle_files.items())
            },
        }
        return self._spool.enqueue(
            kind=f"{_MIRROR_KIND_PREFIX}.push",
            aggregate_id=name,
            sequence=self._sequence(name),
            payload=payload,
        )

    def enqueue_delete(self, skill_name: str) -> dict[str, Any]:
        """Enqueue removal of one skill's mirrored subtree."""
        name = str(skill_name or "").strip()
        if not name:
            raise ValueError("skill_name is required")
        payload = {"op": "delete", "skill_name": name}
        return self._spool.enqueue(
            kind=f"{_MIRROR_KIND_PREFIX}.delete",
            aggregate_id=name,
            sequence=self._sequence(name),
            payload=payload,
        )

    # ------------------------------------------------------------------ #
    # Delivery                                                             #
    # ------------------------------------------------------------------ #

    def _sender(self, delivery: dict[str, Any]) -> dict[str, Any]:
        payload = delivery.get("payload") or {}
        op = str(payload.get("op") or "")
        skill_name = str(payload.get("skill_name") or "")
        hub = self._viking_hub
        if hub is None:
            raise RuntimeError("mirror has no viking target configured")
        if op == "push":
            files = {
                rel_path: bytes.fromhex(hexed)
                for rel_path, hexed in (payload.get("files") or {}).items()
            }
            if not files:
                raise RuntimeError("mirror push payload has no files")
            for rel_path, content in sorted(files.items()):
                hub._bucket.put_object(hub._skill_bundle_key(skill_name, rel_path), content)
            hub._delete_remote_bundle_extras(skill_name, files.keys())
            return {"status": "ok", "skill_name": skill_name, "files": len(files)}
        if op == "delete":
            # Agent-facing subtree only: SKILL.md + files/, never versions/.
            for obj in list(hub._iter_remote_keys(hub._skill_files_prefix(skill_name))):
                hub._bucket.delete_object(obj.key)
            try:
                hub._bucket.delete_object(hub._skill_key(skill_name))
            except Exception:  # noqa: BLE001 - already absent
                pass
            return {"status": "ok", "skill_name": skill_name, "deleted": True}
        raise RuntimeError(f"unknown mirror op: {op!r}")

    def flush(self, *, limit: int = 20) -> dict[str, Any]:
        """Deliver pending mirror items (oldest first), bounded per call."""
        return self._spool.flush(self._sender, limit=limit)

    def status(self) -> dict[str, Any]:
        """Spool health summary (pending/failed counts) for status endpoints."""
        health = self._spool.health()
        return {
            "enabled": True,
            "spool_dir": str(self._spool.path),
            **health,
        }

    # ------------------------------------------------------------------ #
    # Background flusher                                                   #
    # ------------------------------------------------------------------ #


class MirrorFlusher:
    """Periodic background flush of the skill mirror spool.

    Runs in its own daemon thread so an OpenViking stall never blocks the
    evolution event loop; a delivery error just reschedules via the spool's
    backoff.
    """

    def __init__(self, mirror: VikingSkillMirror, *, interval_seconds: float = 30.0) -> None:
        self._mirror = mirror
        self._interval = max(5.0, float(interval_seconds))
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="skill-mirror-flush",
            daemon=True,
        )
        self._thread.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                result = self._mirror.flush()
                acked = int(result.get("acked") or 0)
                failed = int(result.get("failed") or 0)
                if acked or failed:
                    logger.info(
                        "[SkillMirror] flush acked=%d failed=%d blocked=%s",
                        acked,
                        failed,
                        result.get("blocked"),
                    )
            except Exception as exc:  # noqa: BLE001 - flusher must never die loudly
                logger.warning("[SkillMirror] flush failed: %s", exc)
