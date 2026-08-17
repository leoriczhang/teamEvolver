"""Regression tests for the skill management editor, cloud delete, and the
SkillsAdmin REST routes.

Three layers are covered:

* :mod:`teamEvolver.skills.editor` — pure on-disk CRUD over ``skills_dir``.
* :meth:`teamEvolver.skills.hub.SkillHub.delete_skill` and the ``include_names``
  push filter — the cloud side used for auto-sync.
* :class:`teamEvolver.proxy.skills_admin.SkillsAdminMixin` routes — wired through a
  real :class:`~teamEvolver.proxy.ProxyServer` and exercised with the FastAPI
  ``TestClient`` (no live upstream), including the local-backend cloud auto-sync.
"""

from __future__ import annotations

import base64
import io
import json
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from teamEvolver.config import TeamEvolverConfig
from teamEvolver.proxy import ProxyServer
from teamEvolver.proxy.users_admin import _hub_from_user, resolve_registered_user_id
from teamEvolver.skills import editor
from teamEvolver.skills.editor import SkillEditorError
from teamEvolver.skills.hub import SkillHub
from teamEvolver.skills.manager import SkillManager
from teamEvolver.storage import shared_memory_bucket
from teamEvolver.validation.store import ValidationStore


def _skill_md(name: str, description: str = "Demo skill", body: str = "# Demo\n\nDo it.") -> str:
    return f"---\nname: {name}\ndescription: {description}\ncategory: general\n---\n\n{body}\n"


def _seed_skill(skills_dir: Path, name: str) -> Path:
    skill_dir = skills_dir / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(_skill_md(name), encoding="utf-8")
    return skill_dir


def _zip_bytes(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, data in files.items():
            zf.writestr(path, data)
    return buf.getvalue()


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


# --------------------------------------------------------------------------- #
# editor.py — pure on-disk CRUD                                                #
# --------------------------------------------------------------------------- #


def test_validate_skill_name_rejects_traversal_and_empty() -> None:
    assert editor.validate_skill_name(" good-name_1.2 ") == "good-name_1.2"
    for bad in ["", "../escape", "a/b", "with space", ".hidden"]:
        with pytest.raises(SkillEditorError):
            editor.validate_skill_name(bad)


def test_save_skill_creates_then_updates_and_preserves_extra_frontmatter(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()

    created = editor.save_skill(
        str(skills_dir), "alpha", "First skill", "coding", "Body one."
    )
    assert created["created"] is True
    md_path = skills_dir / "alpha" / "SKILL.md"
    assert md_path.is_file()
    assert "First skill" in md_path.read_text(encoding="utf-8")

    # Add an extra frontmatter key by hand; a structured re-save must keep it.
    md_path.write_text(
        "---\nname: alpha\ndescription: First skill\nkeep_me: yes\n---\n\nBody one.\n",
        encoding="utf-8",
    )
    updated = editor.save_skill(
        str(skills_dir), "alpha", "Updated desc", "general", "Body two."
    )
    assert updated["created"] is False
    text = md_path.read_text(encoding="utf-8")
    assert "Updated desc" in text
    assert "keep_me" in text
    assert "Body two." in text


def test_save_skill_raw_mode_writes_verbatim(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    raw = _skill_md("beta", description="Raw skill", body="# Raw body")
    result = editor.save_skill(
        str(skills_dir), "beta", "", "general", "", skill_md=raw
    )
    assert result["created"] is True
    on_disk = (skills_dir / "beta" / "SKILL.md").read_text(encoding="utf-8")
    assert on_disk == raw


def test_list_and_get_skill_report_bundle_files(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    skill_dir = _seed_skill(skills_dir, "gamma")
    (skill_dir / "scripts").mkdir()
    (skill_dir / "scripts" / "run.py").write_text("print('hi')\n", encoding="utf-8")

    listing = editor.list_skills(str(skills_dir))
    assert [s["name"] for s in listing] == ["gamma"]
    assert listing[0]["file_count"] == 2  # SKILL.md + scripts/run.py

    detail = editor.get_skill(str(skills_dir), "gamma")
    assert detail["name"] == "gamma"
    assert detail["category"] == "general"
    assert "scripts/run.py" in detail["files"]
    assert detail["skill_md"].startswith("---")
    assert detail["body"].strip() != ""


def test_add_and_delete_bundle_files_guard_skill_md(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    _seed_skill(skills_dir, "delta")

    result = editor.add_bundle_files(
        str(skills_dir), "delta", {"references/guide.md": b"hello\n"}
    )
    assert result["written"] == ["references/guide.md"]
    assert (skills_dir / "delta" / "references" / "guide.md").read_bytes() == b"hello\n"

    with pytest.raises(SkillEditorError):
        editor.add_bundle_files(str(skills_dir), "delta", {"SKILL.md": b"nope"})

    removed = editor.delete_bundle_file(str(skills_dir), "delta", "references/guide.md")
    assert removed["removed"] == "references/guide.md"
    assert not (skills_dir / "delta" / "references" / "guide.md").exists()

    with pytest.raises(SkillEditorError):
        editor.delete_bundle_file(str(skills_dir), "delta", "SKILL.md")


def test_delete_skill_removes_directory_and_is_not_found_safe(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    _seed_skill(skills_dir, "epsilon")

    result = editor.delete_skill(str(skills_dir), "epsilon")
    assert result == {"name": "epsilon", "deleted": True}
    assert not (skills_dir / "epsilon").exists()

    with pytest.raises(SkillEditorError):
        editor.delete_skill(str(skills_dir), "epsilon")


def test_import_zip_takes_name_from_frontmatter_and_strips_wrapper(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    zip_data = _zip_bytes(
        {
            "zeta/SKILL.md": _skill_md("zeta").encode("utf-8"),
            "zeta/references/notes.md": b"notes\n",
        }
    )

    result = editor.import_zip(str(skills_dir), zip_data)
    assert result["name"] == "zeta"
    assert result["created"] is True
    assert "references/notes.md" in result["files"]
    assert (skills_dir / "zeta" / "SKILL.md").is_file()
    assert (skills_dir / "zeta" / "references" / "notes.md").read_bytes() == b"notes\n"


def test_import_zip_requires_entrypoint(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    zip_data = _zip_bytes({"readme.txt": b"no skill here\n"})
    with pytest.raises(SkillEditorError):
        editor.import_zip(str(skills_dir), zip_data)


# --------------------------------------------------------------------------- #
# hub.py — cloud delete + single-skill push                                    #
# --------------------------------------------------------------------------- #


def _local_hub(tmp_path: Path) -> SkillHub:
    # Share one in-process bucket per tmp_path so repeated calls within a test
    # observe the same objects (mirroring the old shared-directory backend).
    return SkillHub.from_bucket(
        shared_memory_bucket("memory://" + str(tmp_path / "bucket")),
        customer_id="",
        user_alias="tester",
    )


def test_push_include_names_scopes_to_single_skill(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    _seed_skill(skills_dir, "one")
    _seed_skill(skills_dir, "two")

    hub = _local_hub(tmp_path)
    result = hub.push_skills(str(skills_dir), include_names=["one"])

    assert result["uploaded"] == 1
    assert result["total_local"] == 1
    manifest = hub._load_remote_manifest()
    assert set(manifest) == {"one"}


def test_delete_skill_removes_remote_bundle_and_is_idempotent(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    _seed_skill(skills_dir, "solo")

    hub = _local_hub(tmp_path)
    hub.push_skills(str(skills_dir))
    assert set(hub._load_remote_manifest()) == {"solo"}

    # The skill's SKILL.md object exists in the shared bucket.
    assert any(
        obj.key.endswith("skills/solo/SKILL.md")
        for obj in hub._bucket.iter_objects(prefix="")
    )

    deleted = hub.delete_skill("solo")
    assert deleted == {"deleted": True, "name": "solo"}
    assert hub._load_remote_manifest() == {}
    # The object store is key-value: delete removes every object under the
    # skill's subtree, so no objects remain beneath it.
    remaining = [
        obj.key
        for obj in hub._bucket.iter_objects(prefix="")
        if "skills/solo/" in obj.key
    ]
    assert remaining == []

    # Idempotent: deleting again reports it was already absent, never raises.
    again = hub.delete_skill("solo")
    assert again == {"deleted": False, "name": "solo"}


def test_version_detail_includes_evolution_and_replay_context(tmp_path: Path) -> None:
    server = _make_server(tmp_path, sharing=True)
    client = _authed_client(server)
    skills_dir = Path(server.config.skills_dir)
    skill_dir = _seed_skill(skills_dir, "audit-skill")
    hub = SkillHub.team_from_config(server.config)
    hub.push_skills(str(skills_dir), include_names=["audit-skill"])
    candidate = _skill_md("audit-skill", body="# After")
    (skill_dir / "SKILL.md").write_text(candidate, encoding="utf-8")
    hub.push_skills(str(skills_dir), include_names=["audit-skill"])

    store = ValidationStore.from_config(server.config)
    job_id = "job-audit-skill-v2"
    store.save_job(
        {
            "job_id": job_id,
            "candidate_skill": {
                "name": "audit-skill",
                "description": "Demo skill",
                "category": "general",
                "content": "# After",
                "edit_summary": {
                        "changed_sections": ["Stable replay metrics"],
                    "notes": "Fix post-write validation.",
                },
            },
            "current_skill": {
                "name": "audit-skill",
                "description": "Demo skill",
                "category": "general",
                "content": "# Before",
            },
            "proposed_action": "improve_skill",
            "rationale": "Real replay exposed a missing validation step.",
            "evidence_classification": {
                "team_skill": [{"claim": "Rerun validation after editing."}]
            },
        }
    )
    store.save_evaluation(
        job_id,
        {
            "accepted": True,
            "decision": "accept",
            "replay_summary": {
                "verdict": "accept",
                "efficiency": {
                    "dimensions": {
                        "tool_call_count": {
                            "baseline": 8,
                            "candidate": 6,
                            "delta": 2,
                            "winner": "candidate",
                        }
                    }
                },
                "cases": [
                    {
                        "baseline": {"tool_call_count": 8, "final_response": "before"},
                        "candidate": {"tool_call_count": 6, "final_response": "after"},
                    }
                ],
            },
        },
    )
    store.save_decision(
        job_id,
        {
            "status": "published",
            "skill_name": "audit-skill",
            "version": 2,
        },
    )

    response = client.get("/api/skills/audit-skill/versions/2")

    assert response.status_code == 200
    evolution = response.json()["evolution"]
    assert evolution["job_id"] == job_id
    assert "Stable replay metrics" in evolution["optimization_items"]
    assert (
        evolution["evaluation"]["replay"]["efficiency"]["dimensions"][
            "tool_call_count"
        ]["delta"]
        == 2
    )
    assert "# Before" in evolution["skill_diff"]
    assert "# After" in evolution["skill_diff"]


# --------------------------------------------------------------------------- #
# SkillsAdmin routes via ProxyServer + TestClient                             #
# --------------------------------------------------------------------------- #


def _make_server(tmp_path: Path, *, sharing: bool = False) -> ProxyServer:
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    # SkillManager requires at least the directory to exist; seed one skill so
    # reload() has something to load and generation bumps are observable.
    _seed_skill(skills_dir, "seed-skill")

    config = TeamEvolverConfig(
        skills_dir=str(skills_dir),
        users_registry_path=str(tmp_path / "users.json"),
        sharing_enabled=sharing,
        sharing_backend="viking" if sharing else "",
        sharing_viking_endpoint="memory://" + str(tmp_path / "bucket") if sharing else "",
        sharing_user_alias="tester",
    )
    manager = SkillManager(str(skills_dir))
    return ProxyServer(config, skill_manager=manager)


def _authed_client(server: ProxyServer) -> TestClient:
    client = TestClient(server.app)
    resp = client.post(
        "/api/auth/bootstrap",
        json={
            "username": "admin",
            "display_name": "Admin",
            "password": "password123",
        },
    )
    assert resp.status_code == 200
    return client


def test_public_registration_creates_regular_user_and_logs_in(tmp_path: Path) -> None:
    server = _make_server(tmp_path)
    client = _authed_client(server)

    resp = client.post(
        "/api/auth/register",
        json={
            "username": "alice",
            "display_name": "Alice",
            "email": "alice@example.com",
            "role": "admin",
            "password": "pw",
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["authenticated"] is True
    assert body["user"]["id"] == "alice"
    assert body["user"]["role"] == "user"

    status = client.get("/api/auth/status")
    assert status.status_code == 200
    assert status.json()["user"]["id"] == "alice"

    users = client.get("/api/users")
    assert users.status_code == 200
    assert [user["id"] for user in users.json()["users"]] == ["alice"]

    upsert = client.post("/api/users", json={"id": "bob", "password": "pw"})
    assert upsert.status_code == 403

    write_skill = client.post(
        "/api/skills",
        json={
            "name": "blocked",
            "description": "should not write",
            "body": "blocked",
        },
    )
    assert write_skill.status_code == 403

    duplicate = client.post(
        "/api/auth/register",
        json={"username": "alice", "password": "pw2"},
    )
    assert duplicate.status_code == 409


def test_logout_invalidates_console_session(tmp_path: Path) -> None:
    server = _make_server(tmp_path)
    client = _authed_client(server)

    before = client.get("/api/auth/status")
    logged_out = client.post("/api/auth/logout")
    after = client.get("/api/auth/status")

    assert before.json()["authenticated"] is True
    assert logged_out.status_code == 200
    assert logged_out.json()["authenticated"] is False
    assert after.json()["authenticated"] is False


def test_user_profile_updates_identity_mapping_and_rejects_conflicts(
    tmp_path: Path,
) -> None:
    server = _make_server(tmp_path)
    admin_client = _authed_client(server)
    for user_id in ("alice", "bob"):
        created = admin_client.post(
            "/api/users",
            json={
                "id": user_id,
                "role": "user",
                "password": "password123",
                "team_space": {"viking_user": "shared-team"},
            },
        )
        assert created.status_code == 200

    alice_client = TestClient(server.app)
    login = alice_client.post(
        "/api/auth/login",
        json={"username": "alice", "password": "password123"},
    )
    assert login.status_code == 200

    updated = alice_client.put(
        "/api/users/alice/profile",
        json={
            "display_name": "Alice Updated",
            "agent_identities": {"agentshub": "agents-alice"},
            "personal_space": {"viking_user": "openviking-alice"},
            "team_space": {"viking_user": "must-not-change"},
            "role": "admin",
        },
    )
    assert updated.status_code == 200
    body = updated.json()
    assert body["display_name"] == "Alice Updated"
    assert body["role"] == "user"
    assert body["agent_identities"] == {"agentshub": "agents-alice"}
    assert body["personal_space"]["viking_user"] == "openviking-alice"
    assert body["team_space"]["viking_user"] == "shared-team"

    forbidden = alice_client.put(
        "/api/users/bob/profile",
        json={"display_name": "Not Allowed"},
    )
    assert forbidden.status_code == 403

    conflict = admin_client.post(
        "/api/users",
        json={
            "id": "bob",
            "agent_identities": {"agentshub": "agents-alice"},
        },
    )
    assert conflict.status_code == 409

    assert (
        resolve_registered_user_id(
            server.config,
            runtime_type="agentshub",
            external_username="agents-alice",
        )
        == "alice"
    )
    assert (
        resolve_registered_user_id(
            server.config,
            runtime_type="agentshub",
            external_username="bob",
        )
        == "bob"
    )
    assert (
        resolve_registered_user_id(
            server.config,
            runtime_type="agentshub",
            external_username="unknown",
            preferred_user_id="bob",
        )
        == ""
    )
    assert (
        resolve_registered_user_id(
            server.config,
            runtime_type="agentshub",
            external_username="unknown",
        )
        == ""
    )


def test_user_routes_register_hide_keys_and_share_local_spaces(tmp_path: Path) -> None:
    server = _make_server(tmp_path, sharing=True)
    client = _authed_client(server)

    bucket_endpoint = "memory://" + str(tmp_path / "bucket")
    raw_personal = tmp_path / "raw-personal"
    _seed_skill(raw_personal, "mine")
    # Personal space is scoped under peers/{user_id}/ within the shared bucket.
    personal_hub = SkillHub.from_bucket(
        shared_memory_bucket(bucket_endpoint),
        customer_id="alice",
        user_alias="tester",
    )
    personal_hub.push_skills(str(raw_personal))

    created = client.post(
        "/api/users",
        json={
            "id": "alice",
            "display_name": "Alice",
            "email": "alice@example.com",
            "role": "admin",
            "personal_space": {},
            "team_space": {},
        },
    )
    assert created.status_code == 200
    assert created.json()["id"] == "alice"
    assert created.json()["role"] == "admin"

    listing = client.get("/api/users")
    assert listing.status_code == 200
    assert "registry_path" not in listing.json()
    alice = next(u for u in listing.json()["users"] if u["id"] == "alice")
    assert alice["personal_space"]["backend"] == "viking"
    assert "local_root" not in alice["personal_space"]

    personal_list = client.get("/api/users/alice/skills?space=personal")
    assert personal_list.status_code == 200
    assert [s["name"] for s in personal_list.json()["skills"]] == ["mine"]

    shared = client.post(
        "/api/users/alice/share",
        json={"direction": "personal_to_team", "skill_names": ["mine"]},
    )
    assert shared.status_code == 200
    assert shared.json()["uploaded"] == 1
    # Team space lives at the bucket root (no peer scoping).
    team_hub = SkillHub.from_bucket(
        shared_memory_bucket(bucket_endpoint),
        customer_id="",
        user_alias="tester",
    )
    assert set(team_hub._load_remote_manifest()) == {"mine"}

    raw_team = tmp_path / "raw-team"
    _seed_skill(raw_team, "team-only")
    team_hub.push_skills(str(raw_team))

    # General users may copy team skills to their personal space, but cannot
    # publish personal skills back into team assets.
    updated_role = client.post(
        "/api/users",
        json={
            "id": "alice",
            "role": "user",
            "personal_space": {},
            "team_space": {},
        },
    )
    assert updated_role.status_code == 200
    forbidden = client.post(
        "/api/users/alice/share",
        json={"direction": "personal_to_team", "skill_names": ["mine"]},
    )
    assert forbidden.status_code == 403
    copied_down = client.post(
        "/api/users/alice/share",
        json={"direction": "team_to_personal", "skill_names": ["team-only"]},
    )
    assert copied_down.status_code == 200
    assert copied_down.json()["uploaded"] == 1
    assert "team-only" in personal_hub._load_remote_manifest()

    with_key = client.post(
        "/api/users",
        json={
            "id": "alice",
            "role": "admin",
            "personal_space": {
                "viking_api_key": "personal-secret",
            },
            "team_space": {
                "viking_api_key": "team-secret",
            },
        },
    )
    assert with_key.status_code == 200
    body = with_key.json()
    assert "viking_api_key" not in body["personal_space"]
    assert "viking_endpoint" not in body["personal_space"]
    assert body["personal_space"]["api_key_present"] is True
    assert "viking_api_key" not in body["team_space"]
    assert "viking_endpoint" not in body["team_space"]
    assert body["team_space"]["api_key_present"] is True

    # Empty API key fields preserve the stored secrets.
    preserve = client.post(
        "/api/users",
        json={
            "id": "alice",
            "display_name": "Alice Updated",
            "personal_space": {
                "viking_api_key": "",
            },
            "team_space": {
                "viking_api_key": "",
            },
        },
    )
    assert preserve.status_code == 200
    registry = (tmp_path / "users.json").read_text(encoding="utf-8")
    assert "personal-secret" in registry
    assert "team-secret" in registry

    regular = client.post(
        "/api/users",
        json={
            "id": "bob",
            "role": "user",
            "personal_space": {},
            "team_space": {},
        },
    )
    assert regular.status_code == 200
    assert regular.json()["team_space"]["backend"] == "viking"
    assert regular.json()["team_space"]["api_key_present"] is True
    assert regular.json()["team_space"]["inherited_from_admin"] is True

    team_secret = client.get("/api/users/bob/spaces/team/secret")
    assert team_secret.status_code == 200
    assert team_secret.json()["api_key_present"] is True
    assert team_secret.json()["inherited_from_admin"] is True
    assert team_secret.json()["viking_api_key"] == ""

    data = json.loads((tmp_path / "users.json").read_text(encoding="utf-8"))
    bob = next(user for user in data["users"] if user["id"] == "bob")
    # Bob inherits the admin's team key. Verify the resolution wiring directly
    # (the in-memory test bucket does not carry the api key the way the
    # OpenViking HTTP store does).
    from teamEvolver.proxy.users_admin import _effective_team_key

    assert _effective_team_key(server.config, data) == "team-secret"
    bob_team_hub = _hub_from_user(server.config, bob, space="team")
    assert bob_team_hub is not None


def test_routes_full_crud_cycle_without_sharing(tmp_path: Path) -> None:
    server = _make_server(tmp_path)
    client = _authed_client(server)

    # UI is served.
    ui = client.get("/skills-ui")
    assert ui.status_code == 200
    assert "<html" in ui.text.lower()

    # LIST includes the seed skill and reports sharing disabled.
    listing = client.get("/api/skills")
    assert listing.status_code == 200
    body = listing.json()
    assert body["sharing_enabled"] is False
    assert any(s["name"] == "seed-skill" for s in body["skills"])

    # CREATE
    created = client.post(
        "/api/skills",
        json={"name": "made-here", "description": "Created via API", "category": "coding", "body": "# hi"},
    )
    assert created.status_code == 200
    payload = created.json()
    assert payload["created"] is True
    assert payload["cloud"]["synced"] is False  # sharing disabled
    assert payload["loaded_skills"] >= 2

    # GET
    got = client.get("/api/skills/made-here")
    assert got.status_code == 200
    assert got.json()["description"] == "Created via API"

    # ADD FILE
    add = client.post(
        "/api/skills/made-here/files",
        json={"files": [{"path": "references/x.md", "content_b64": _b64(b"data\n")}]},
    )
    assert add.status_code == 200
    assert add.json()["written"] == ["references/x.md"]

    # DELETE FILE
    delf = client.delete("/api/skills/made-here/files/references/x.md")
    assert delf.status_code == 200
    assert delf.json()["removed"] == "references/x.md"

    # IMPORT ZIP
    zip_data = _zip_bytes({"imported/SKILL.md": _skill_md("imported").encode("utf-8")})
    imp = client.post("/api/skills/import-zip", json={"zip_b64": _b64(zip_data)})
    assert imp.status_code == 200
    assert imp.json()["name"] == "imported"

    # DELETE skill
    dele = client.delete("/api/skills/made-here")
    assert dele.status_code == 200
    assert dele.json()["deleted"] is True

    names = {s["name"] for s in client.get("/api/skills").json()["skills"]}
    assert "made-here" not in names
    assert "imported" in names


def test_sync_skills_endpoint_uses_shared_store_when_configured(tmp_path: Path) -> None:
    server = _make_server(tmp_path, sharing=True)
    client = _authed_client(server)

    cloud_source = tmp_path / "cloud-source"
    _seed_skill(cloud_source, "cloud-team-skill")
    hub = SkillHub.from_bucket(
        shared_memory_bucket("memory://" + str(tmp_path / "bucket")),
        customer_id="",
        user_alias="tester",
    )
    hub.push_skills(str(cloud_source))

    resp = client.get("/sync/skills")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["source"] == "shared"
    assert [skill["name"] for skill in body["skills"]] == ["cloud-team-skill"]
    assert body["skills"][0]["files"][0]["path"] == "SKILL.md"
    assert "seed-skill" not in {skill["name"] for skill in body["skills"]}


def test_sync_skills_endpoint_falls_back_to_local_without_sharing(tmp_path: Path) -> None:
    server = _make_server(tmp_path, sharing=False)
    client = _authed_client(server)

    resp = client.get("/sync/skills")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["source"] == "local"
    assert [skill["name"] for skill in body["skills"]] == ["seed-skill"]


def test_routes_error_handling(tmp_path: Path) -> None:
    server = _make_server(tmp_path)
    client = _authed_client(server)

    assert client.get("/api/skills/does-not-exist").status_code == 404
    assert client.delete("/api/skills/does-not-exist").status_code == 404

    bad_name = client.post("/api/skills", json={"name": "../evil", "description": "x", "body": "y"})
    assert bad_name.status_code == 400

    bad_b64 = client.post(
        "/api/skills/seed-skill/files",
        json={"files": [{"path": "a.txt", "content_b64": "not base64!!!"}]},
    )
    assert bad_b64.status_code == 400

    empty_files = client.post("/api/skills/seed-skill/files", json={"files": []})
    assert empty_files.status_code == 400


def test_routes_auto_sync_to_cloud_on_save_and_delete(tmp_path: Path) -> None:
    server = _make_server(tmp_path, sharing=True)
    client = _authed_client(server)

    created = client.post(
        "/api/skills",
        json={"name": "shared", "description": "Sync me", "category": "general", "body": "# body"},
    )
    assert created.status_code == 200
    cloud = created.json()["cloud"]
    assert cloud["synced"] is True
    assert cloud["action"] == "push"
    assert cloud["uploaded"] == 1

    # The skill really landed in the shared local bucket.
    hub = SkillHub.team_from_config(server.config)
    assert "shared" in hub._load_remote_manifest()

    deleted = client.delete("/api/skills/shared")
    assert deleted.status_code == 200
    dc = deleted.json()["cloud"]
    assert dc["synced"] is True
    assert dc["action"] == "delete"
    assert dc["deleted"] is True
    assert "shared" not in hub._load_remote_manifest()
