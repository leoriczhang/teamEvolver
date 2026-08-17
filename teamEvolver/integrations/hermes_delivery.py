"""Zero-dependency durable delivery spool for one Hermes profile."""

from __future__ import annotations

import hashlib
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from threading import RLock
from typing import Any, Callable, Iterator

_LOCK = RLock()
_TERMINAL = {"acked", "cancelled"}


def _delivery_id(
    integration_id: str,
    kind: str,
    aggregate_id: str,
    sequence: int,
    payload: dict[str, Any],
) -> str:
    digest = hashlib.sha256(
        json.dumps(
            {
                "integration_id": integration_id,
                "kind": kind,
                "aggregate_id": aggregate_id,
                "sequence": sequence,
                "payload": payload,
            },
            sort_keys=True,
            default=str,
        ).encode("utf-8")
    ).hexdigest()[:32]
    return f"hermes_delivery_{digest}"


class HermesDeliverySpool:
    def __init__(self, path: Path, *, integration_id: str) -> None:
        self.path = path.expanduser()
        self.integration_id = str(integration_id or "hermes:local")

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.path.mkdir(parents=True, exist_ok=True)
        os.chmod(self.path, 0o700)
        lock_path = self.path / ".lock"
        with _LOCK, lock_path.open("a+b") as handle:
            os.chmod(lock_path, 0o600)
            try:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            except (ImportError, OSError):
                pass
            try:
                yield
            finally:
                try:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except (ImportError, OSError):
                    pass

    def _file(self, delivery_id: str) -> Path:
        return self.path / f"{delivery_id}.json"

    @staticmethod
    def _read(path: Path) -> dict[str, Any] | None:
        try:
            value = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            return None
        return value if isinstance(value, dict) else None

    @staticmethod
    def _write(path: Path, value: dict[str, Any]) -> None:
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        os.chmod(path, 0o600)

    def enqueue(
        self,
        *,
        kind: str,
        aggregate_id: str,
        sequence: int,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        delivery_id = _delivery_id(
            self.integration_id,
            kind,
            aggregate_id,
            sequence,
            payload,
        )
        target = self._file(delivery_id)
        with self._locked():
            existing = self._read(target)
            if existing:
                return existing
            now = time.time()
            delivery = {
                "schema_version": "teamevolver.hermes-delivery.v1",
                "delivery_id": delivery_id,
                "integration_id": self.integration_id,
                "kind": kind,
                "aggregate_id": aggregate_id,
                "sequence": max(1, int(sequence)),
                "payload": dict(payload),
                "status": "pending",
                "attempt": 0,
                "next_retry_at": now,
                "last_error": "",
                "created_at": now,
                "updated_at": now,
            }
            self._write(target, delivery)
            return delivery

    def _records(self) -> list[dict[str, Any]]:
        records = [
            record
            for path in self.path.glob("hermes_delivery_*.json")
            if (record := self._read(path)) is not None
        ]
        priority = {"registration.ensure": 0, "context.start": 1}
        return sorted(
            records,
            key=lambda item: (
                priority.get(str(item.get("kind") or ""), 2),
                float(item.get("created_at") or 0),
                str(item.get("aggregate_id") or ""),
                int(item.get("sequence") or 0),
            ),
        )

    def _blocked(self, delivery: dict[str, Any]) -> bool:
        for candidate in self._records():
            if candidate.get("delivery_id") == delivery.get("delivery_id"):
                continue
            if (
                candidate.get("integration_id") == delivery.get("integration_id")
                and candidate.get("aggregate_id") == delivery.get("aggregate_id")
                and int(candidate.get("sequence") or 0)
                < int(delivery.get("sequence") or 0)
                and candidate.get("status") not in _TERMINAL
            ):
                return True
        return False

    def deliver(
        self,
        delivery_id: str,
        sender: Callable[[dict[str, Any]], dict[str, Any]],
        *,
        force: bool = False,
    ) -> dict[str, Any]:
        target = self._file(delivery_id)
        with self._locked():
            delivery = self._read(target)
            if not delivery:
                return {"status": "missing", "delivery_id": delivery_id}
            if delivery.get("status") in _TERMINAL:
                return delivery
            if self._blocked(delivery):
                return {**delivery, "blocked": True}
            if not force and float(delivery.get("next_retry_at") or 0) > time.time():
                return delivery
            delivery["status"] = "delivering"
            delivery["updated_at"] = time.time()
            self._write(target, delivery)
            try:
                ack = sender(delivery)
                if not isinstance(ack, dict):
                    raise RuntimeError("delivery was not acknowledged")
            except Exception as exc:
                attempt = int(delivery.get("attempt") or 0) + 1
                delivery.update(
                    {
                        "attempt": attempt,
                        "status": (
                            "dead_letter" if attempt >= 8 else "failed"
                        ),
                        "next_retry_at": time.time()
                        + min(3600, 2 ** attempt),
                        "last_error": (
                            f"{type(exc).__name__}: {exc}"[:2000]
                        ),
                        "updated_at": time.time(),
                    }
                )
                self._write(target, delivery)
                return delivery
            target.unlink(missing_ok=True)
            return {
                **delivery,
                "status": "acked",
                "ack": ack,
                "acked_at": time.time(),
            }

    def flush(
        self,
        sender: Callable[[dict[str, Any]], dict[str, Any]],
        *,
        limit: int = 20,
    ) -> dict[str, int]:
        summary = {"acked": 0, "failed": 0, "blocked": 0, "dead_letter": 0}
        for delivery in self._records()[: max(1, int(limit))]:
            status = str(delivery.get("status") or "pending")
            if status == "dead_letter":
                summary["dead_letter"] += 1
                continue
            result = self.deliver(str(delivery["delivery_id"]), sender)
            if result.get("status") == "acked":
                summary["acked"] += 1
            elif result.get("blocked"):
                summary["blocked"] += 1
            elif result.get("status") in {"failed", "dead_letter"}:
                summary["failed"] += 1
        return summary

    def health(self) -> dict[str, Any]:
        records = self._records()
        now = time.time()
        return {
            "backlog": len(records),
            "oldest_age_seconds": int(
                max(
                    [now - float(item.get("created_at") or now) for item in records]
                    or [0]
                )
            ),
            "dead_letter": sum(
                item.get("status") == "dead_letter" for item in records
            ),
            "last_error": next(
                (
                    str(item.get("last_error") or "")
                    for item in reversed(records)
                    if item.get("last_error")
                ),
                "",
            ),
        }

    def retry(self, delivery_id: str) -> dict[str, Any]:
        target = self._file(delivery_id)
        with self._locked():
            delivery = self._read(target)
            if not delivery:
                raise KeyError(delivery_id)
            delivery.update(
                {
                    "status": "pending",
                    "attempt": 0,
                    "next_retry_at": time.time(),
                    "last_error": "",
                    "updated_at": time.time(),
                }
            )
            self._write(target, delivery)
            return delivery

    def discard(
        self,
        delivery_id: str,
        *,
        reason: str = "",
    ) -> None:
        target = self._file(delivery_id)
        with self._locked():
            delivery = self._read(target)
            if not delivery:
                raise KeyError(delivery_id)
            audit = self.path / "discard-audit.jsonl"
            with audit.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "delivery_id": delivery_id,
                            "reason": reason,
                            "discarded_at": time.time(),
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
            os.chmod(audit, 0o600)
            target.unlink(missing_ok=True)
