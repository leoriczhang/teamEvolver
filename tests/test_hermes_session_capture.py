from __future__ import annotations

import json
import sqlite3
import sys
from io import StringIO
from pathlib import Path

import yaml

from teamEvolver.integrations.hermes_skill import install
from teamEvolver.integrations.hermes_skill import push_session
from teamEvolver.integrations.hermes_skill.push_session import _read_session


def _build_state_db(path: Path) -> str:
    session_id = "session-1"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT,
            model TEXT,
            system_prompt TEXT,
            started_at REAL,
            message_count INTEGER,
            tool_call_count INTEGER,
            input_tokens INTEGER,
            output_tokens INTEGER,
            cache_read_tokens INTEGER,
            cache_write_tokens INTEGER,
            reasoning_tokens INTEGER,
            api_call_count INTEGER
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            role TEXT,
            content TEXT,
            tool_call_id TEXT,
            tool_calls TEXT,
            tool_name TEXT,
            timestamp REAL,
            token_count INTEGER,
            finish_reason TEXT,
            reasoning TEXT,
            reasoning_content TEXT,
            active INTEGER
        );
        """
    )
    system_prompt = """
    system instructions
    <available_skills>
      translation:
        - text-translation: Translate technical text.
        - glossary: Maintain terminology.
    </available_skills>
    """
    conn.execute(
        "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            session_id,
            "cli",
            "test-model",
            system_prompt,
            123.0,
            5,
            2,
            100,
            40,
            300,
            0,
            5,
            2,
        ),
    )
    tool_calls = json.dumps(
        [
            {
                "id": "call-1",
                "type": "function",
                "function": {
                    "name": "skill_view",
                    "arguments": json.dumps({"name": "text-translation"}),
                },
            },
            {
                "id": "call-2",
                "type": "function",
                "function": {
                    "name": "terminal",
                    "arguments": json.dumps({"command": "echo ok"}),
                },
            },
        ]
    )
    rows = [
        (session_id, "user", "translate this", None, None, None, 1.0, 10, None, None, None, 1),
        (session_id, "assistant", "", None, tool_calls, None, 2.0, 8, "tool_calls", "thinking", None, 1),
        (session_id, "tool", '{"success": true}', "call-1", None, "skill_view", 3.0, 5, None, None, None, 1),
        (session_id, "tool", '{"output": "ok"}', "call-2", None, "terminal", 4.0, 5, None, None, None, 1),
        (session_id, "assistant", "done", None, None, None, 5.0, 12, "stop", None, None, 1),
    ]
    conn.executemany(
        "INSERT INTO messages (session_id, role, content, tool_call_id, tool_calls, "
        "tool_name, timestamp, token_count, finish_reason, reasoning, reasoning_content, active) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()
    return session_id


def test_capture_keeps_system_tool_messages_and_skill_attribution(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    session_id = _build_state_db(db_path)

    session = _read_session(db_path, session_id)

    assert session is not None
    assert session["injected_skills"] == ["text-translation", "glossary"]
    assert session["used_skills"] == ["text-translation"]
    assert session["messages"][0]["role"] == "system"
    assert [message["role"] for message in session["messages"][1:]] == [
        "user",
        "assistant",
        "tool",
        "tool",
        "assistant",
    ]
    turn = session["turns"][0]
    assert turn["used_skills"] == ["text-translation"]
    assert turn["read_skills"] == [{"skill_name": "text-translation"}]
    assert len(turn["tool_calls"]) == 2
    assert len(turn["tool_results"]) == 2
    assert turn["messages"][1]["reasoning"] == "thinking"
    assert session["metrics"] == {
        "interaction_turns": 1,
        "message_count": 5,
        "tool_call_count": 2,
        "api_call_count": 2,
        "input_tokens": 100,
        "output_tokens": 40,
        "cache_read_tokens": 300,
        "cache_write_tokens": 0,
        "reasoning_tokens": 5,
        "total_tokens": 140,
    }


def test_feed_installer_replaces_same_script_with_new_python(tmp_path: Path) -> None:
    home = tmp_path / ".hermes"
    base_args = [
        "--user",
        "tester",
        "--url",
        "http://127.0.0.1:52010",
        "--hermes-home",
        str(home),
    ]
    assert install.main([*base_args, "--python", "python3"]) == 0
    assert install.main([*base_args, "--python", "/opt/python"]) == 0

    config = yaml.safe_load((home / "config.yaml").read_text("utf-8"))
    hooks = config["hooks"]["on_session_end"]
    assert hooks == [
        {
            "command": f"/opt/python {home / 'skills/teamEvolver-feed/push_session.py'}",
            "timeout": 20,
        }
    ]
    allowlist = json.loads((home / "shell-hooks-allowlist.json").read_text("utf-8"))
    feed_approvals = [
        item for item in allowlist["approvals"] if item.get("event") == "on_session_end"
    ]
    assert len(feed_approvals) == 1
    assert feed_approvals[0]["command"].startswith("/opt/python ")
    feed_path = home / "skills" / "teamEvolver-feed" / "feed.json"
    feed = json.loads(feed_path.read_text("utf-8"))
    assert feed["protocol_version"] == "1.0"
    assert feed["integration_id"].startswith("hermes:")
    assert feed["profile_id"]
    assert feed["external_subject"] == "tester"
    assert feed_path.stat().st_mode & 0o777 == 0o600


def test_v1_hermes_session_binds_profile_and_subject(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    session_id = _build_state_db(db_path)
    session = _read_session(db_path, session_id)

    payload = push_session._v1_session(
        session,
        {
            "integration_id": "hermes:machine:profile",
            "profile_id": "profile",
            "external_subject": "subject-1",
        },
        "alice",
    )

    assert payload["schema_version"] == "teamevolver.agent-session.v1"
    assert payload["runtime"]["integration_id"] == "hermes:machine:profile"
    assert payload["runtime"]["profile_id"] == "profile"
    assert payload["runtime_context"]["external_subject"] == "subject-1"
    assert payload["turns"][0]["context_usage"]["memory_refs"] == []


def test_feed_installer_enables_context_provider_after_v1_registration(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / ".hermes"

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(
                {
                    "ok": True,
                    "credentials": {
                        "agent_access_token": "tev1_workspace_token"
                    },
                }
            ).encode("utf-8")

    monkeypatch.setattr(
        install.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: _Response(),
    )

    assert install.main(
        [
            "--user",
            "alice",
            "--url",
            "http://team-evolver:52010",
            "--api-key",
            "control-key",
            "--hermes-home",
            str(home),
        ]
    ) == 0

    provider = home / "plugins" / "team_evolver"
    assert (provider / "__init__.py").is_file()
    assert (provider / "plugin.yaml").is_file()
    config = yaml.safe_load((home / "config.yaml").read_text("utf-8"))
    assert config["memory"]["provider"] == "team_evolver"
    feed = json.loads(
        (home / "skills" / "teamEvolver-feed" / "feed.json").read_text(
            "utf-8"
        )
    )
    assert feed["workspace_token"] == "tev1_workspace_token"


def test_failed_hermes_delivery_is_spooled_and_retried(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cfg = {"spool_dir": str(tmp_path / "spool")}
    session = {"session_id": "session-1", "turns": [{}]}
    push_session._spool(session, cfg)
    spool_file = tmp_path / "spool" / "session-1.json"
    assert spool_file.is_file()
    assert spool_file.stat().st_mode & 0o777 == 0o600

    calls = []
    monkeypatch.setattr(
        push_session,
        "_post",
        lambda *_args, **_kwargs: calls.append(True) or True,
    )
    push_session._flush_spool(
        cfg,
        base_url="http://team-evolver",
        user="alice",
        api_key="token",
    )

    assert calls == [True]
    assert not spool_file.exists()


def test_embedded_skillminer_hermes_never_posts_session(monkeypatch) -> None:
    monkeypatch.setenv("TEAMEVOLVER_DISABLE_SESSION_FEED", "1")
    monkeypatch.setattr(sys, "stdin", StringIO('{"session_id":"session-1"}'))
    monkeypatch.setattr(
        push_session,
        "_post",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not post")),
    )

    assert push_session.main() == 0
