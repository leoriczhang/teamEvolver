from __future__ import annotations

import hashlib

from teamEvolver.storage import InMemoryObjectStore
from team_skills.library.hub import SkillHub
from team_skills.library.mutations import SkillMutationService


class TransactionalMemoryStore(InMemoryObjectStore):
    native_batch_write = True

    def object_precondition(self, key):
        try:
            current = self.get_object(key).read()
        except FileNotFoundError:
            return {"kind": "create_if_absent"}
        return {
            "kind": "replace_if_hash",
            "base_hash": "sha256:" + hashlib.sha256(current).hexdigest(),
        }

    def batch_write(self, objects, *, preconditions=None, **_kwargs):
        with self._lock:
            for key, condition in (preconditions or {}).items():
                current = self._data.get(key)
                kind = condition.get("kind")
                if kind == "create_if_absent" and current is not None:
                    raise RuntimeError(f"create conflict: {key}")
                if kind == "replace_if_hash":
                    current_hash = (
                        "sha256:" + hashlib.sha256(current).hexdigest()
                        if current is not None
                        else ""
                    )
                    if current_hash != condition.get("base_hash"):
                        raise RuntimeError(f"replace conflict: {key}")
            for key, value in objects.items():
                self.put_object(key, value)
        return {"succeeded": sorted(objects), "failed": [], "mode": "transactional"}


def _bundle(body: str) -> dict[str, bytes]:
    return {
        "SKILL.md": (
            "---\nname: alpha\ndescription: test\n---\n\n" + body + "\n"
        ).encode(),
        "scripts/run.py": b"print('ok')\n",
    }


def test_publish_bundle_is_atomic_and_idempotent() -> None:
    bucket = TransactionalMemoryStore()
    hub = SkillHub.from_bucket(bucket)

    first = hub.publish_bundle("alpha", _bundle("v1"), action="publish")
    repeated = hub.publish_bundle("alpha", _bundle("v1"), action="publish")
    second = hub.publish_bundle("alpha", _bundle("v2"), action="update")

    assert first["record"]["version"] == 1
    assert repeated["uploaded"] == 0
    assert repeated["record"]["version"] == 1
    assert second["record"]["version"] == 2
    assert hub.read_version_bundle("alpha", 1)["SKILL.md"].endswith(b"v1\n")
    assert hub.read_version_bundle("alpha", 2)["SKILL.md"].endswith(b"v2\n")


def test_mutation_publish_bundle_creates_one_commit_and_event() -> None:
    bucket = TransactionalMemoryStore()
    service = SkillMutationService.from_hub(SkillHub.from_bucket(bucket))

    first = service.publish_bundle(
        action="publish",
        name="alpha",
        mutation_id="candidate:tenant-a:job-1:r1",
        bundle=_bundle("v1"),
        tenant_ids=["tenant-a"],
    )
    repeated = service.publish_bundle(
        action="publish",
        name="alpha",
        mutation_id="candidate:tenant-a:job-1:r1",
        bundle=_bundle("v1"),
        tenant_ids=["tenant-a"],
    )

    assert first == repeated
    assert first["expected"]["version"] == 1
    assert first["event_id"]
    events = list(service.iter_outbox_events())
    assert len(events) == 1
    assert events[0][1]["skills"][0]["name"] == "alpha"
