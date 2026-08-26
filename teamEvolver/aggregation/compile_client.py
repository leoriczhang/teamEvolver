"""``ov compile`` invocation for a single aggregation batch.

Two transports are possible (see the design doc). This module uses transport
**A**: shell out to the ``ov`` binary, reusing the exact conf/env contract that
``OpenVikingWorkspaceMixin._workspace_cli`` already established (temp
``ovcli.conf`` with a service credential, ``output=json``,
localized/anti-color env). We do not import that mixin to avoid a Request-bound
call path; instead we build the same conf here from plain credentials so the
aggregation service can run in a background task without an HTTP request.

Transport B (direct ``POST /bot/v1/compile`` + poll) is the intended
productionization target and can replace :meth:`CompileClient.run_batch`
without changing the service layer.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_ANSI_ESCAPE_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
_MAX_CLI_OUTPUT_BYTES = 512 * 1024


class CompileBinaryUnavailable(RuntimeError):
    """Raised when no usable ``ov`` binary is found."""


@dataclass
class CompileClient:
    """Run ``ov compile`` for one batch under a chosen OpenViking identity.

    Aggregation runs compile in two identity modes using the request-scoped
    OpenViking Admin Key:

    - **per-user** (``user_id`` + Admin Key): reads that user's memory into a
      per-user staging directory under ``viking://resources/...``.
    - **team** (``user_id`` = the team/service user + Admin Key): merges the
      staged per-user products into the shared-knowledge target. The
      ``resources`` namespace is writable by any role.
    """

    endpoint: str
    account_id: str
    # The OpenViking user this compile runs as.
    user_id: str = ""
    # Request-scoped OpenViking Admin Key.
    api_key: str = ""
    agent_id: str = "team-skill-evolver"
    binary_override: str = ""
    timeout_seconds: float = 3000.0

    def _binary(self) -> str:
        candidates = [
            self.binary_override.strip(),
            os.environ.get("OPENVIKING_CLI_BIN", "").strip(),
            str(Path.home() / "OpenViking" / "target" / "release" / "ov"),
            shutil.which("ov") or "",
        ]
        for candidate in candidates:
            if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
        raise CompileBinaryUnavailable(
            "OpenViking CLI binary is unavailable; set OPENVIKING_CLI_BIN"
        )

    async def install_skill(
        self,
        *,
        skill_name: str,
        skill_body: str,
        parent_uri: str = "viking://agent/skills",
    ) -> dict[str, Any]:
        """Publish a SKILL.md into an OpenViking skills namespace.

        Writes the body to a temp ``<skill_name>/SKILL.md`` and runs
        ``ov skills add <dir> --parent-auto-create <parent_uri> --wait --yes``.
        Idempotent enough for setup: re-adding updates the skill content.
        """
        binary = self._binary()
        conf = {
            "url": self.endpoint,
            "api_key": self.api_key or None,
            "account": self.account_id,
            "agent_id": self.agent_id,
            "timeout": float(self.timeout_seconds),
            "output": "json",
            "echo_command": False,
            "show_progress": False,
        }
        if self.user_id.strip():
            conf["user"] = self.user_id.strip()
        with tempfile.TemporaryDirectory(prefix="teamEvolver-agg-skill-") as src_dir:
            skill_dir = os.path.join(src_dir, skill_name)
            os.makedirs(skill_dir, exist_ok=True)
            with open(os.path.join(skill_dir, "SKILL.md"), "w", encoding="utf-8") as handle:
                handle.write(skill_body)
            argv = [
                "skills", "add", skill_dir,
                "--parent-auto-create", parent_uri,
                "--wait", "--yes",
            ]
            return await self._exec(binary, argv, conf)

    async def run_batch(
        self,
        *,
        source_uris: tuple[str, ...] | list[str],
        target_uri: str,
        skill_uri: str,
        reason: str = "",
        runtime_timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        """Compile ``source_uris`` into ``target_uri`` with ``skill_uri``.

        Returns a structured result dict: ``ok``, ``exit_code``, ``command``,
        ``stdout``, ``stderr``, plus a best-effort parsed ``result`` when the
        CLI emits JSON.
        """
        if not source_uris:
            return {"ok": True, "skipped": True, "reason": "no sources"}
        binary = self._binary()
        argv: list[str] = ["compile"]
        for uri in source_uris:
            argv += ["--from", uri]
        argv += ["--to", target_uri, "--skill", skill_uri, "--wait"]
        if reason.strip():
            argv += ["--reason", reason.strip()]
        if runtime_timeout_seconds:
            argv += ["--runtime-timeout", str(int(runtime_timeout_seconds))]

        conf = {
            "url": self.endpoint,
            "api_key": self.api_key or None,
            "account": self.account_id,
            "agent_id": self.agent_id,
            "timeout": float(self.timeout_seconds),
            "output": "json",
            "echo_command": False,
            "show_progress": False,
        }
        if self.user_id.strip():
            conf["user"] = self.user_id.strip()
        return await self._exec(binary, argv, conf)

    async def _exec(
        self, binary: str, argv: list[str], conf: dict[str, Any]
    ) -> dict[str, Any]:
        with tempfile.TemporaryDirectory(prefix="teamEvolver-agg-ovcli-") as temp_dir:
            config_path = os.path.join(temp_dir, "ovcli.conf")
            settings_path = os.path.join(temp_dir, ".openviking", "ovcli.settings.conf")
            os.makedirs(os.path.dirname(settings_path), exist_ok=True)
            with open(config_path, "w", encoding="utf-8") as handle:
                json.dump(conf, handle, ensure_ascii=True)
            with open(settings_path, "w", encoding="utf-8") as handle:
                json.dump({"language": "zh-CN"}, handle, ensure_ascii=True)
            os.chmod(config_path, 0o600)
            os.chmod(settings_path, 0o600)
            env = os.environ.copy()
            env["HOME"] = temp_dir
            env["OPENVIKING_CLI_CONFIG_FILE"] = config_path
            env["OPENVIKING_LANG"] = "zh-CN"
            env["NO_COLOR"] = "1"
            process = await asyncio.create_subprocess_exec(
                binary,
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=self.timeout_seconds + 30,
                )
            except (TimeoutError, asyncio.TimeoutError):
                process.kill()
                await process.communicate()
                return {
                    "ok": False,
                    "exit_code": -1,
                    "command": ["ov", *argv],
                    "stdout": "",
                    "stderr": f"ov compile timed out after {self.timeout_seconds}s",
                }
        output = _ANSI_ESCAPE_RE.sub(
            "", stdout[:_MAX_CLI_OUTPUT_BYTES].decode("utf-8", errors="replace")
        ).strip()
        error = _ANSI_ESCAPE_RE.sub(
            "", stderr[:_MAX_CLI_OUTPUT_BYTES].decode("utf-8", errors="replace")
        ).strip()
        parsed: Any = None
        if output:
            try:
                parsed = json.loads(output)
            except ValueError:
                parsed = None
        return {
            "ok": process.returncode == 0,
            "exit_code": process.returncode,
            "command": ["ov", *argv],
            "stdout": output,
            "stderr": error,
            "result": parsed,
        }
