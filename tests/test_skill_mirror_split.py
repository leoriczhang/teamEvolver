"""Per-purpose backend split: local ledgers + async OpenViking skill mirror.

teamEvolver's own hot read/write data (session queue / indexes / evidence /
validation / registry / manifest) lives on the built-in local backend; the
team skills Agents read are mirrored asynchronously into
``viking://resources/{root}/skills/<name>/`` so the cross-machine read surface
keeps working. These tests pin the split and the mirror contract:

- config-driven hubs default to the local backend with a viking mirror target
- ``push_skills`` / ``delete_skill`` enqueue mirror deliveries; a flush writes
  only the ``skills/<name>/`` subtree to OpenViking — registry / manifest /
  versions stay local
- OpenViking outages never block publishing (deliveries retry via the spool)
- the evolve server mirrors a just-published skill and keeps its ledgers local
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from teamEvolver.config import TeamEvolverConfig
from teamEvolver.skills.hub import SkillHub
from teamEvolver.skills.mirror import VikingSkillMirror
from teamEvolver.storage import InMemoryObjectStore, LocalObjectStore


@pytest.fixture(autouse=True)
def _probe_ok(monkeypatch):
    """OpenViking is considered reachable so hubs attach a real mirror target."""
    monkeypatch.setattr(
        "teamEvolver.storage.probe_viking_availability",
        lambda store, *, timeout=3.0: (True, ""),
    )


def _config(tmp_path: Path, **overrides) -> TeamEvolverConfig:
    base = dict(
        sharing_backend="viking",
        sharing_viking_endpoint="http://viking.test",
        sharing_enabled=True,
        sharing_local_root=str(tmp_path / "local_store"),
        sharing_skill_mirror_spool_dir=str(tmp_path / "spool"),
    )
    base.update(overrides)
    return TeamEvolverConfig(**base)


def _mirror(hub, tmp_path):
    return VikingSkillMirror(
        spool_dir=str(tmp_path / "spool"),
        viking_hub=hub._mirror_viking_hub,
        sequence_bucket=hub._bucket,
    )


def _capture_target(hub) -> InMemoryObjectStore:
    """Swap the mirror's viking bucket for an in-memory capture store."""
    mem = InMemoryObjectStore("mirror-target")
    hub._mirror_viking_hub._bucket = mem
    return mem


def _write_skill(skills_dir: Path, name: str, body: str = "body") -> None:
    (skills_dir / name).mkdir(parents=True)
    (skills_dir / name / "SKILL.md").write_text(f"# {name}\n\n{body}\n", encoding="utf-8")


# --------------------------------------------------------------------- #
# Split construction                                                     #
# --------------------------------------------------------------------- #


def test_team_hub_defaults_to_local_with_viking_mirror(tmp_path) -> None:
    hub = SkillHub.team_from_config(_config(tmp_path))
    assert isinstance(hub._bucket, LocalObjectStore)
    assert hub.mirror_viking_hub is not None
    # The mirror target never falls back to local: an OpenViking outage must
    # surface as a failed (retryable) delivery, not a silent local write.
    assert not isinstance(hub._mirror_viking_hub._bucket, LocalObjectStore)


def test_object_storage_hub_defaults_to_local(tmp_path) -> None:
    hub = SkillHub.object_storage_from_config(_config(tmp_path))
    assert hub is not None
    assert isinstance(hub._bucket, LocalObjectStore)


def test_explicit_viking_skill_backend_keeps_single_bucket(tmp_path) -> None:
    hub = SkillHub.team_from_config(_config(tmp_path, sharing_skill_backend="viking"))
    assert not isinstance(hub._bucket, LocalObjectStore)
    assert hub.mirror_viking_hub is None


def test_mirror_disabled_builds_no_mirror_hub(tmp_path) -> None:
    hub = SkillHub.team_from_config(_config(tmp_path, sharing_skill_mirror_enabled=False))
    assert isinstance(hub._bucket, LocalObjectStore)
    assert hub.mirror_viking_hub is None


# --------------------------------------------------------------------- #
# Mirror delivery                                                        #
# --------------------------------------------------------------------- #


def test_push_skills_enqueues_and_flush_mirrors_only_skill_subtree(tmp_path) -> None:
    hub = SkillHub.team_from_config(_config(tmp_path))
    target = _capture_target(hub)
    skills_dir = tmp_path / "skills"
    _write_skill(skills_dir, "demo")

    result = hub.push_skills(str(skills_dir))
    assert result["uploaded"] == 1

    mirror = _mirror(hub, tmp_path)
    assert mirror.status()["backlog"] == 1
    assert mirror.flush()["acked"] == 1

    mirrored = sorted(obj.key for obj in target.iter_objects())
    assert mirrored == ["skills/demo/SKILL.md"]
    # Internal ledgers must never leave the local store.
    assert not any(
        token in key for key in mirrored for token in ("registry", "manifest", "versions")
    )
    # And they exist locally.
    local_keys = sorted(obj.key for obj in hub._bucket.iter_objects())
    assert "manifest.json" in local_keys
    assert "evolve_skill_registry.json" in local_keys
    assert any(key.startswith("skills/demo/versions/") for key in local_keys)


def test_mirror_retry_after_viking_failure_then_success(tmp_path) -> None:
    hub = SkillHub.team_from_config(_config(tmp_path))
    target = _capture_target(hub)
    skills_dir = tmp_path / "skills"
    _write_skill(skills_dir, "retry-demo")

    hub.push_skills(str(skills_dir))
    mirror = _mirror(hub, tmp_path)

    # Simulate an OpenViking outage: sender raises, delivery stays pending.
    def failing_bucket(*args, **kwargs):
        raise RuntimeError("ConnectError: simulated outage")

    target.put_object = failing_bucket
    first = mirror.flush()
    assert first["failed"] == 1
    assert mirror.status()["backlog"] == 1

    # OpenViking recovers: redeliver the pending item (force bypasses the
    # spool's retry backoff; the background flusher waits it out instead).
    del target.put_object
    records = mirror._spool._records()
    assert len(records) == 1
    result = mirror._spool.deliver(records[0]["delivery_id"], mirror._sender, force=True)
    assert result["status"] == "acked"
    mirrored = sorted(obj.key for obj in target.iter_objects())
    assert "skills/retry-demo/SKILL.md" in mirrored


def test_delete_skill_mirrors_removal(tmp_path) -> None:
    hub = SkillHub.team_from_config(_config(tmp_path))
    target = _capture_target(hub)
    skills_dir = tmp_path / "skills"
    _write_skill(skills_dir, "gone")

    hub.push_skills(str(skills_dir))
    mirror = _mirror(hub, tmp_path)
    mirror.flush()
    assert "skills/gone/SKILL.md" in [obj.key for obj in target.iter_objects()]

    hub.delete_skill("gone")
    mirror.flush()
    assert "skills/gone/SKILL.md" not in [obj.key for obj in target.iter_objects()]


# --------------------------------------------------------------------- #
# Evolve server publish → mirror                                         #
# --------------------------------------------------------------------- #


def test_evolve_publish_mirrors_skill_and_keeps_ledgers_local(tmp_path, monkeypatch) -> None:
    from teamEvolver.evolve.kernel.settings import EvolveServerConfig
    from teamEvolver.evolve.runtime.orchestrator import EvolveServer
    from teamEvolver.evolve.store.object_store import load_manifest

    target = InMemoryObjectStore("evolve-mirror-target")
    config = EvolveServerConfig(
        storage_backend="local",
        storage_local_root=str(tmp_path / "engine_store"),
        viking_endpoint="http://viking.test",
        skill_mirror_enabled=True,
        skill_mirror_spool_dir=str(tmp_path / "spool"),
        llm_api_key="k",
        publish_mode="direct",
        dataset_synthesis_enabled=False,
        bundle_static_checks_enabled=False,
        use_session_judge=False,
        history_path=str(tmp_path / "h.jsonl"),
        processed_log_path=str(tmp_path / "p.json"),
    )
    server = EvolveServer(config, mock=False)
    assert isinstance(server._skill_bucket, LocalObjectStore)

    # Point the lazily-built mirror hub at the capture store.
    mirror_hub = server._mirror_viking_hub()
    mirror_hub._bucket = target
    server.__dict__["_mirror_hub"] = mirror_hub

    skill = {
        "name": "local-born",
        "description": "born on local storage",
        "content": "# local-born\n\nbody\n",
        "category": "general",
    }
    assert server._upload_skill(skill, "create_skill") == "uploaded"

    mirror = VikingSkillMirror(
        spool_dir=str(tmp_path / "spool"),
        viking_hub=mirror_hub,
        sequence_bucket=server._skill_bucket,
    )
    assert mirror.status()["backlog"] == 1
    assert mirror.flush()["acked"] == 1

    mirrored = sorted(obj.key for obj in target.iter_objects())
    assert mirrored == ["skills/local-born/SKILL.md"]

    # Engine ledgers stay on the local skill bucket; the registry is persisted
    # lazily at cycle end, so here we assert the in-memory registry recorded
    # the skill and no ledger key leaked into the mirror target.
    manifest = load_manifest(server._skill_bucket, server._skill_prefix)
    assert "local-born" in manifest
    assert server._id_registry.get_version("local-born") >= 1
    local_keys = sorted(obj.key for obj in server._skill_bucket.iter_objects())
    assert "manifest.json" in local_keys
    assert any(key.startswith("skills/local-born/versions/") for key in local_keys)
    mirrored_again = sorted(obj.key for obj in target.iter_objects())
    assert not any(
        token in key
        for key in mirrored_again
        for token in ("registry", "manifest", "versions", "mutation")
    )


def test_evolve_publish_skips_mirror_when_disabled(tmp_path) -> None:
    from teamEvolver.evolve.kernel.settings import EvolveServerConfig
    from teamEvolver.evolve.runtime.orchestrator import EvolveServer

    config = EvolveServerConfig(
        storage_backend="local",
        storage_local_root=str(tmp_path / "engine_store"),
        viking_endpoint="http://viking.test",
        skill_mirror_enabled=False,
        skill_mirror_spool_dir=str(tmp_path / "spool"),
        llm_api_key="k",
        publish_mode="direct",
        dataset_synthesis_enabled=False,
        bundle_static_checks_enabled=False,
        use_session_judge=False,
        history_path=str(tmp_path / "h.jsonl"),
        processed_log_path=str(tmp_path / "p.json"),
    )
    server = EvolveServer(config, mock=False)
    skill = {
        "name": "no-mirror",
        "description": "d",
        "content": "# no-mirror\n\nb\n",
        "category": "general",
    }
    assert server._upload_skill(skill, "create_skill") == "uploaded"
    mirror = VikingSkillMirror(
        spool_dir=str(tmp_path / "spool"),
        viking_hub=None,
        sequence_bucket=server._skill_bucket,
    )
    assert mirror.status()["backlog"] == 0
