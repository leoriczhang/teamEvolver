"""Tests for personal-skill editing/deletion and the publish-approval queue.

Covers the routes added to :mod:`teamEvolver.proxy.users_admin`:

* ``POST/DELETE /api/users/{uid}/skills`` — users edit their own personal space.
* ``POST /api/users/{uid}/publish-requests`` — non-admins request a team publish.
* ``POST /api/skill-publish-requests/{id}/approve`` — admin approval copies the
  personal skill into the shared team space.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from teamEvolver.config import TeamEvolverConfig
from teamEvolver.proxy import ProxyServer
from teamEvolver.skills.manager import SkillManager


def _make_server(tmp_path: Path) -> ProxyServer:
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    config = TeamEvolverConfig(
        skills_dir=str(skills_dir),
        users_registry_path=str(tmp_path / "users.json"),
        sharing_enabled=True,
        sharing_backend="viking",
        sharing_viking_endpoint="memory://" + str(tmp_path / "bucket"),
        sharing_user_alias="tester",
    )
    return ProxyServer(config, skill_manager=SkillManager(str(skills_dir)))


def _admin_client(server: ProxyServer) -> TestClient:
    client = TestClient(server.app)
    resp = client.post(
        "/api/auth/bootstrap",
        json={"username": "admin", "display_name": "Admin", "password": "password123"},
    )
    assert resp.status_code == 200
    return client


def _register_user(admin: TestClient, user_id: str) -> None:
    created = admin.post(
        "/api/users",
        json={"id": user_id, "role": "user", "password": "password123"},
    )
    assert created.status_code == 200


def test_admin_can_create_edit_and_delete_personal_skill(tmp_path: Path) -> None:
    server = _make_server(tmp_path)
    admin = _admin_client(server)

    created = admin.post(
        "/api/users/admin/skills",
        json={
            "space": "personal",
            "name": "note-taker",
            "description": "Take structured notes",
            "body": "# Note taker\n\nDo it well.",
        },
    )
    assert created.status_code == 200, created.text
    assert created.json()["name"] == "note-taker"

    listed = admin.get("/api/users/admin/skills?space=personal")
    assert listed.status_code == 200
    assert any(s.get("name") == "note-taker" for s in listed.json()["skills"])

    detail = admin.get("/api/users/admin/skills/note-taker?space=personal")
    assert detail.status_code == 200
    assert detail.json()["description"] == "Take structured notes"
    assert detail.json()["skill_md"].startswith("---")

    deleted = admin.delete("/api/users/admin/skills/note-taker?space=personal")
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] is True

    listed_again = admin.get("/api/users/admin/skills?space=personal")
    assert not any(s.get("name") == "note-taker" for s in listed_again.json()["skills"])


def test_regular_user_edits_own_personal_skill_but_not_team(tmp_path: Path) -> None:
    server = _make_server(tmp_path)
    admin = _admin_client(server)
    _register_user(admin, "alice")

    alice = TestClient(server.app)
    assert alice.post(
        "/api/auth/login", json={"username": "alice", "password": "password123"}
    ).status_code == 200

    ok = alice.post(
        "/api/users/alice/skills",
        json={"space": "personal", "name": "my-skill", "description": "mine", "body": "# hi"},
    )
    assert ok.status_code == 200

    # Editing the shared team space is admin-only.
    forbidden = alice.post(
        "/api/users/alice/skills",
        json={"space": "team", "name": "sneaky", "description": "x", "body": "y"},
    )
    assert forbidden.status_code == 403

    # A user cannot touch another user's personal space.
    other = alice.post(
        "/api/users/admin/skills",
        json={"space": "personal", "name": "x", "description": "x", "body": "y"},
    )
    assert other.status_code == 403


def test_publish_request_requires_admin_approval(tmp_path: Path) -> None:
    server = _make_server(tmp_path)
    admin = _admin_client(server)
    _register_user(admin, "alice")

    alice = TestClient(server.app)
    assert alice.post(
        "/api/auth/login", json={"username": "alice", "password": "password123"}
    ).status_code == 200

    # Seed a personal skill, then request publishing it to the team space.
    assert alice.post(
        "/api/users/alice/skills",
        json={"space": "personal", "name": "shareme", "description": "share", "body": "# share"},
    ).status_code == 200

    # Direct share to team is rejected for non-admins.
    direct = alice.post(
        "/api/users/alice/share",
        json={"direction": "personal_to_team", "skill_names": ["shareme"]},
    )
    assert direct.status_code == 403

    submitted = alice.post(
        "/api/users/alice/publish-requests",
        json={"skill_names": ["shareme"]},
    )
    assert submitted.status_code == 200
    request_id = submitted.json()["request_id"]
    assert submitted.json()["status"] == "pending"

    # Alice sees only her own request; cannot approve it.
    own = alice.get("/api/skill-publish-requests")
    assert own.status_code == 200
    assert own.json()["pending_count"] == 1
    denied = alice.post(f"/api/skill-publish-requests/{request_id}/approve", json={})
    assert denied.status_code == 403

    # Admin approves; the skill is copied into the team space.
    approved = admin.post(f"/api/skill-publish-requests/{request_id}/approve", json={})
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "approved"

    team = admin.get("/api/users/alice/skills?space=team")
    assert team.status_code == 200
    assert any(s.get("name") == "shareme" for s in team.json()["skills"])


def test_publish_request_reject_keeps_team_empty(tmp_path: Path) -> None:
    server = _make_server(tmp_path)
    admin = _admin_client(server)
    _register_user(admin, "bob")

    bob = TestClient(server.app)
    assert bob.post(
        "/api/auth/login", json={"username": "bob", "password": "password123"}
    ).status_code == 200

    assert bob.post(
        "/api/users/bob/skills",
        json={"space": "personal", "name": "draft", "description": "d", "body": "# d"},
    ).status_code == 200
    request_id = bob.post(
        "/api/users/bob/publish-requests", json={"skill_names": ["draft"]}
    ).json()["request_id"]

    rejected = admin.post(f"/api/skill-publish-requests/{request_id}/reject", json={})
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"

    # A decided request cannot be approved afterwards.
    again = admin.post(f"/api/skill-publish-requests/{request_id}/approve", json={})
    assert again.status_code == 409

    team = admin.get("/api/users/bob/skills?space=team")
    assert not any(s.get("name") == "draft" for s in team.json()["skills"])
