"""Pinned MA Skill Replay. Runtime execution receives no evaluation rules."""

from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import io
import json
import re
import time
import zipfile
from collections import Counter

import httpx

from teamEvolver.managed_agents import MAError
from teamEvolver.managed_agents.events import child_threads, complete, metrics, stable_end, tag_values, text, turns

from ..artifacts import skill_treatment_files, skill_treatment_members
from ..hooks import AgentObservation, ReplayUnsupported
from .ma_materials import SANDBOX_MATERIAL_ROOT, bootstrap_environment

ARTIFACT_REGISTRATION_SECONDS = 60


def declared_outputs(events):
    """Read native Write paths and MA display references, never task requirements."""
    prefix, paths = "/mnt/session/outputs/", set()
    for event in events:
        inputs = event.get("input") or event.get("arguments") or {}
        candidates = []
        if event.get("type") == "agent.tool_use" and str(event.get("name", "")).lower() in {"write", "edit"}:
            if isinstance(inputs, dict):
                candidates.append(inputs.get("file_path") or inputs.get("path") or "")
        if event.get("type") == "agent.message":
            candidates.extend(re.findall(r"<display>(/mnt/session/outputs/[^<]+)</display>", text(event)))
        for path in candidates:
            if isinstance(path, str) and path.startswith(prefix):
                relative = path[len(prefix):]
                if relative and all(p not in {"", ".", ".."} for p in relative.split("/")):
                    paths.add(relative)
    return paths


def _raise_replay_error(events):
    for event in events:
        if event.get("type") != "session.error":
            continue
        error = event.get("error") or {}
        code = error.get("code") or error.get("type") or "unknown"
        try:
            detail = json.loads(error.get("message") or "{}")
            if isinstance(detail, dict) and isinstance(detail.get("error"), dict):
                code = detail["error"].get("code") or code
        except (ValueError, TypeError):
            pass
        raise RuntimeError("MA session error: " + str(code))
    for event in events:
        if event.get("type") == "agent.custom_tool_use":
            name = str(event.get("name") or "unknown")[:200]
            thread = str(event.get("session_thread_id") or "main")[:200]
            ident = str(event.get("id") or "unknown")[:200]
            raise ReplayUnsupported(
                f"MA custom tool executor unavailable: {name} (event={ident}, thread={thread}); "
                "no result is fabricated"
            )
    if any(e.get("stop_reason", {}).get("type") == "requires_action" for e in events
           if e.get("type") in {"session.status_idle", "session.thread_status_idle"}):
        raise ReplayUnsupported("MA requires human tool confirmation; no result is fabricated")


class ManagedAgentsFactory:
    """Dispatch recorded MA targets while retaining an optional non-MA factory."""

    def __init__(self, records, capabilities, fallback=None):
        self.records = copy.deepcopy(records)
        self.capabilities, self.fallback = capabilities, fallback
        self.revision = "managed-agents.skill-only.v10"

    def open(self, context):
        if context.runtime_type != "managed-agents":
            if self.fallback:
                return self.fallback.open(context)
            raise ReplayUnsupported("No Replay adapter configured for this runtime")
        runtime = context.source_session.get("runtime") or {}
        matches = [r for r in self.records.values() if r["connection_id"] == runtime.get("connection_id")
                   and r["agent_graph"] == runtime.get("agent_graph")
                   and (not runtime.get("environment") or r["environment"] == runtime["environment"])]
        if not matches:
            raise ReplayUnsupported("Frozen MA execution target is unavailable")
        return ManagedAgentsSession(context, matches[0], self.capabilities)


def _archive(name, files):
    if len(files) > 500:
        raise ReplayUnsupported("MA Skill file limit exceeded")
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, data in sorted(files.items()):
            if path.startswith("/") or "\\" in path or any(p in {"", ".", ".."} for p in path.split("/")):
                raise ReplayUnsupported("Unsafe Replay Skill path")
            archive.writestr(name + "/" + path, data)
    result = output.getvalue()
    if len(result) > 30 * 1024 * 1024:
        raise ReplayUnsupported("MA Skill compressed limit exceeded")
    return result


def _agent_config(value):
    return {k: copy.deepcopy(v) for k, v in value.items()
            if k not in {"id", "type", "version", "created_at", "updated_at", "overridden_fields", "scene"}}


def _same_agent_fields(actual, desired, members):
    """Allow native display normalization, while checking expanded execution fields."""
    for key, wanted in desired.items():
        observed = actual.get(key)
        if key == "skills":
            def references(values):
                return [{k: v for k, v in item.items() if k != "display_name"} for item in values or []]
            if references(observed) != references(wanted):
                return False
        elif key == "multiagent" and isinstance(wanted, dict) and isinstance(observed, dict):
            if {k: v for k, v in wanted.items() if k != "agents"} != {
                    k: v for k, v in observed.items() if k != "agents"}:
                return False
            expected_refs, actual_refs = wanted.get("agents") or [], observed.get("agents") or []
            if len(expected_refs) != len(actual_refs):
                return False
            for expected, expanded in zip(expected_refs, actual_refs):
                if not _same_agent_fields(expanded, expected, members):
                    return False
                frozen = members.get((expected.get("id"), str(expected.get("version")))) or {}
                # The Session API expands pinned references. Verify every extra
                # execution field against the exact original or cloned member.
                extra = {k: v for k, v in expanded.items() if k not in expected and k != "name"}
                if any(k not in frozen for k in extra):
                    return False
                if not _same_agent_fields(extra, {k: frozen[k] for k in extra}, members):
                    return False
        elif observed != wanted:
            return False
    return True


def _same_environment(actual, desired):
    # MA omits explicitly cleared TOS in GET readback. No other execution
    # field is normalized, and any inherited output destination still fails.
    def normalized(config):
        return {k: v for k, v in config.items() if k != "tos" or v is not None}
    return isinstance(actual, dict) and normalized(actual) == normalized(desired)


async def _polled_stream(stream):
    """Keep SSE alive while allowing child-completion history checks."""
    pending = asyncio.create_task(anext(stream))
    try:
        while True:
            done, _ = await asyncio.wait({pending}, timeout=2)
            if not done:
                yield {"type": "bridge.poll"}
                continue
            try:
                event = pending.result()
            except StopAsyncIteration:
                return
            pending = asyncio.create_task(anext(stream))
            yield event
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


class ManagedAgentsSession:
    def __init__(self, context, record, capabilities):
        self.context, self.record, self.cap = context, copy.deepcopy(record), capabilities
        self.owner = context.request_id
        self.client, self.effects, self.session_id = None, None, ""
        self.seen, self.turn_num, self.closed = set(), 0, False
        self.deadline = time.monotonic() + context.timeout_seconds
        self.material_blocks, self.runtime_tree = [], ""
        self.root_thread_id = ""
        self.bootstrap_metrics = {}
        self.execution_snapshot = {}
        self.final_diagnostics = {}

    def diagnostics(self):
        """Failure evidence survives even when send() never returns an observation."""
        return copy.deepcopy(self.final_diagnostics)

    def remaining(self):
        value = self.deadline - time.monotonic()
        if value <= 0:
            raise TimeoutError("MA Replay branch deadline exceeded")
        return value

    def exclude_assessment_time(self, seconds):
        """Local accounting only: judge waiting is outside MA execution."""
        self.deadline += max(0.0, float(seconds))

    def send(self, user_message):
        if self.closed:
            raise RuntimeError("MA Replay Session is closed")
        async def bounded():
            try:
                async with asyncio.timeout(self.remaining()):
                    return await self._send(user_message)
            except TimeoutError:
                raise TimeoutError("MA Replay branch deadline exceeded") from None
        return self.cap["run"](bounded(), timeout=self.remaining() + 5)

    async def _original_material(self, material, raw):
        """Verify existing native Files without acquiring cleanup ownership."""
        from teamEvolver.managed_agents.client import _bounded, validate_url

        lookup = self.cap.get("original_file_id")
        ident = await lookup(self.record, material) if lookup else ""
        if not ident:
            return ""
        if not re.fullmatch(r"[A-Za-z0-9_-]+", ident):
            return ""
        try:
            native = await self.client.request("GET", f"files/{ident}")
        except MAError as exc:
            if exc.status in {404, 410}:
                return ""
            raise
        url = native.get("download_url")
        if native.get("id") != ident or native.get("status") != "active" or not url:
            return ""
        validate_url(url.split("?", 1)[0], allow_loopback=self.client.allow_loopback)
        async with self.client.download_http.stream("GET", url) as response:
            # An expired signed URL is not a credential failure on the MA API.
            if response.status_code in {403, 404, 410}:
                return ""
            if response.status_code != 200:
                raise MAError(response.status_code, "OriginalMaterialDownloadFailed")
            try:
                data = await _bounded(response, len(raw))
            except MAError as exc:
                if exc.code == "DownloadTooLarge":
                    return ""
                raise
        return ident if data == raw else ""

    async def _start(self):
        config = self.record["config"]
        capability = self.cap["replay_capability"](config, self.record["agent_graph"],
                                                  self.record["replay_environment"])
        if not capability["supported"]:
            raise ReplayUnsupported(capability["reason"])
        self.client = await self.cap["client"](self.record)
        self.effects = self.cap["effects"](self.record["connection_id"], self.owner)
        members = skill_treatment_members(self.context.treatment.skill)
        replacement = {}
        for member in members:
            files = skill_treatment_files(member)
            semantic_name = member["name"]
            # Remote name is unique; ZIP root retains the semantic name.
            remote_name = "te-replay-" + hashlib.sha256((self.owner + semantic_name).encode()).hexdigest()[:32]
            async def find_owned_skill(row, title=remote_name, tree=member["bundle"]["tree_sha256"]):
                from team_skills.library.bundle import bundle_tree_sha256
                from team_skills.transfer.packages import discover, read_zip
                matches = [s for s in await self.client.listing("skills") if s.get("display_title") == title]
                if len(matches) != 1:
                    return None
                value = matches[0]
                version = str(value.get("latest_version") or value.get("version") or "1")
                raw = await self.client.download(f"skills/{value['id']}/versions/{version}/content")
                packages = discover(read_zip(raw))
                if len(packages) != 1 or bundle_tree_sha256(packages[0].files) != tree:
                    return None
                return {**value, "version": version}
            receipt = await self.effects.apply(
                self.owner + ":skill:" + semantic_name, "replay-skill",
                {"name": remote_name, "tree_sha256": member["bundle"]["tree_sha256"]},
                lambda n=semantic_name, f=files, title=remote_name:
                    self.client.upload_skill(n, _archive(n, f), display_title=title),
                find_owned_skill,
            )
            replacement[semantic_name] = {"type": "custom", "skill_id": receipt["id"],
                                          "version": str(receipt.get("version")
                                                         or receipt.get("latest_version") or "1")}
        graph, refs, member_configs = self.record["agent_graph"], {}, {}
        bindings = self.record["skill_bindings"]
        target_name = self.record.get("target_skill_name")
        entry = graph["entry_agent_id"]
        root_config = None
        # JSON persistence may reorder keys; traverse dependencies explicitly.
        ordered, visited = [], set()
        def visit(ident):
            if ident in visited:
                return
            visited.add(ident)
            for child in (graph["agents"][ident].get("multiagent") or {}).get("agents") or []:
                if child.get("type") != "self":
                    visit(child["id"])
            ordered.append(ident)
        visit(entry)
        for ident in ordered:
            agent = graph["agents"][ident]
            desired = _agent_config(agent)
            skills = copy.deepcopy(agent.get("skills") or [])
            for name, binding in bindings.items():
                remote = binding.get("id")
                recorded = any(s.get("skill_id") == remote for s in skills)
                if not recorded and ident not in binding.get("agents", []):
                    continue
                if name in replacement:
                    skills = [s for s in skills if s.get("skill_id") != remote]
                    skills.append(replacement[name])
                elif name == target_name:
                    skills = [s for s in skills if s.get("skill_id") != remote]
            desired["skills"] = skills
            if desired.get("multiagent"):
                for child in desired["multiagent"].get("agents") or []:
                    if child.get("type") != "self":
                        child.update(refs[child["id"]])
            if ident == entry:
                # A source Session can override its native Agent's model or
                # tools without changing that Agent reference/version. Freeze
                # these controls on both sides instead of inheriting cloud values.
                root_config = {key: copy.deepcopy(desired[key]) for key in (
                    "system", "model", "tools", "skills", "multiagent", "context_management",
                ) if key in desired}
                continue
            if desired == _agent_config(agent):
                refs[ident] = {"type": "agent", "id": ident, "version": agent["version"]}
                member_configs[(ident, str(agent["version"]))] = desired
                continue
            desired["name"] = "te-replay-" + hashlib.sha256((self.owner + ident).encode()).hexdigest()[:32]
            async def find_owned_agent(row, payload=desired):
                matches = [a for a in await self.client.listing("agents") if a.get("name") == payload["name"]]
                if len(matches) != 1:
                    return None
                actual = await self.client.request("GET", f"agents/{matches[0]['id']}")
                return actual if all(actual.get(k) == v for k, v in payload.items()) else None
            receipt = await self.effects.apply(
                self.owner + ":agent:" + ident, "replay-agent", desired,
                lambda payload=desired: self.client.request("POST", "agents", json=payload),
                find_owned_agent,
            )
            refs[ident] = {"type": "agent", "id": receipt["id"], "version": receipt["version"]}
            member_configs[(receipt["id"], str(receipt["version"]))] = desired
        environment_config = copy.deepcopy(self.record["environment"]["config"])
        # Keep the recorded environment in both native Session sandboxes. Clear
        # shared output storage so MA allocates per-Session output locations.
        replay_environment = self.record["replay_environment"]
        environment_config["tos"] = None
        binding = copy.deepcopy(config.get("runtime_binding") or {})
        runtime_files = {}
        # Runtime is a control variable: never install candidate runtime bytes.
        # Both branches recover the same version from the frozen source graph.
        if binding and binding.get("source") != "openviking":
            from team_skills.transfer.packages import discover, read_zip
            bound = bindings.get(binding["skill_name"]) or {}
            runtime_refs = [s for a in graph["agents"].values() for s in a.get("skills") or []
                    if s.get("skill_id") == bound.get("id")]
            if not runtime_refs or len({str(s["version"]) for s in runtime_refs}) != 1:
                raise ReplayUnsupported("Recorded runtime Skill reference is missing or ambiguous")
            data = await self.client.download(
                f"skills/{runtime_refs[0]['skill_id']}/versions/{runtime_refs[0]['version']}/content")
            packages = discover(read_zip(data))
            if len(packages) != 1:
                raise ReplayUnsupported("Recorded runtime Bundle is unavailable")
            runtime_files = packages[0].files
            # Prompt-only treatments retain the recorded runtime bytes. Their
            # replacement mount no longer contains those files.
            if binding.get("source") == "skill_mount":
                binding.update(source="embedded", archive_compression="xz")
        if binding:
            environment_config, self.runtime_tree = self.cap["runtime_binding"](
                environment_config, binding, runtime_files,
            )
        resources, mounted_paths, material_proofs = [], set(), []
        for index, material in enumerate(self.context.materials):
            raw = base64.b64decode(material["content_b64"], validate=True)
            if hashlib.sha256(raw).hexdigest() != material["sha256"] or len(raw) > 20 * 1024 * 1024:
                raise ReplayUnsupported("Replay material content mismatch or size limit")
            path = material["path"]
            if path.startswith("/") or "\\" in path or any(p in {"", ".", ".."} for p in path.split("/")):
                raise ReplayUnsupported("Unsafe Replay material path")
            mount_path = path
            for prefix in ("mnt/session/uploads/", "uploads/"):
                if path.startswith(prefix):
                    mount_path = path[len(prefix):]
                    break
            if mount_path in mounted_paths:
                raise ReplayUnsupported("Replay materials resolve to the same mount path")
            mounted_paths.add(mount_path)
            filename = "te-replay-" + hashlib.sha256((self.owner + str(index)).encode()).hexdigest()[:24]
            file_id = await self._original_material(material, raw)
            transport = "verified_original" if file_id else "uploaded"
            if not file_id:
                import mimetypes
                media_type = material.get("media_type") or "application/octet-stream"
                filename += mimetypes.guess_extension(media_type) or ""
                receipt = await self.effects.apply(
                    f"{self.owner}:file:{index}", "replay-file",
                    {"path": path, "filename": filename, "sha256": material["sha256"]},
                    lambda p=filename, data=raw, mime=media_type: self.client.request(
                        "POST", "files", data={"purpose": "agent"},
                        files={"file": (p.rsplit("/", 1)[-1], data, mime)},
                    ),
                )
                file_id = receipt["id"]
            resources.append({"type": "file", "file_id": file_id, "mount_path": mount_path})
            material_proofs.append({"path": path, "sha256": material["sha256"], "size": len(raw),
                                    "file_id": file_id, "mount_path": mount_path, "transport": transport})
            input_type = material.get("input_type")
            if input_type in {"video", "image"} or (
                    input_type == "document" and material.get("media_type") == "application/pdf"):
                source = {"type": "file", "file_id": file_id}
                if input_type == "video" and material.get("fps") is not None:
                    source["fps"] = material["fps"]
                self.material_blocks.append({"type": input_type, "source": source})
            else:
                self.material_blocks.append({"type": "text", "text":
                    f"Provided material {path} mounted at: /mnt/session/uploads/{mount_path}"})
        payload = {
            "agent": {"type": "agent_with_overrides", "id": entry, "version": graph["agents"][entry]["version"],
                      **root_config},
            "environment": {"type": "environment_with_overrides", "id": replay_environment["id"],
                            "config": environment_config},
            "vault_ids": config.get("replay_vault_ids") or [],
            "tags": [{"key": "te-replay", "value": self.owner}], "resources": resources,
            "title": "teamEvolver isolated Replay",
        }
        async def find_owned_session(row):
            matches = [s for s in await self.client.listing("sessions", agent_id=entry)
                       if "te-replay:" + self.owner in tag_values(s.get("tags"))]
            return matches[0] if len(matches) == 1 else None
        try:
            receipt = await self.effects.apply(self.owner + ":session", "replay-session", payload,
                                              lambda: self.client.request("POST", "sessions", json=payload),
                                              find_owned_session)
        except MAError as exc:
            if (exc.status != 403 or exc.code != "OperationDenied.FileQuotaExceeded"
                    or not material_proofs
                    or any(m["transport"] != "verified_original" for m in material_proofs)):
                raise
            credentials = []
            for vault_id in config.get("replay_vault_ids") or []:
                credentials.extend(await self.client.listing(f"vaults/{vault_id}/credentials"))
            if not any((c.get("auth") or {}).get("type") == "environment_variable"
                       and (c.get("auth") or {}).get("secret_name") == "ARK_API_KEY" for c in credentials):
                raise
            environment_config = bootstrap_environment(environment_config, self.client.base_url, material_proofs)
            payload["environment"]["config"] = environment_config
            payload["resources"] = []
            for index, material in enumerate(material_proofs):
                material["transport"] = "sandbox_download"
                material["sandbox_path"] = SANDBOX_MATERIAL_ROOT + "/" + material["mount_path"]
                if self.material_blocks[index]["type"] == "text":
                    self.material_blocks[index] = {"type": "text", "text":
                        f"Provided material {material['path']} available at: {material['sandbox_path']}"}
            # The rejected intent stays immutable. This distinct, owned intent
            # copies the same verified inputs without allocating native Files.
            receipt = await self.effects.apply(self.owner + ":session:material-bootstrap", "replay-session", payload,
                                              lambda: self.client.request("POST", "sessions", json=payload),
                                              find_owned_session)
        self.session_id = receipt["id"]
        actual = await self.client.request("GET", f"sessions/{self.session_id}")
        actual_agent, actual_env = actual.get("agent") or {}, actual.get("environment") or {}
        if (actual_agent.get("id") != entry
                or str(actual_agent.get("version")) != str(graph["agents"][entry]["version"])
                or not _same_agent_fields(actual_agent, root_config, member_configs)
                or actual_env.get("id") != replay_environment["id"]
                or not _same_environment(actual_env.get("config"), environment_config)
                or (actual.get("vault_ids") or []) != (config.get("replay_vault_ids") or [])):
            raise ReplayUnsupported("MA Session readback differs from the frozen execution target")
        self.execution_snapshot = {
            "session_id": self.session_id, "agent_id": entry,
            "agent_version": str(actual_agent["version"]), "environment_id": actual_env["id"],
            "vault_ids": list(actual.get("vault_ids") or []),
            "environment_config_sha256": hashlib.sha256(
                json.dumps(actual_env["config"], sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
            "runtime_tree_sha256": self.runtime_tree,
            "runtime_ready_verified": False,
            "agent_override_fields": sorted(root_config),
            "agent_execution_config_sha256": hashlib.sha256(json.dumps({
                key: actual_agent[key] for key in ("system", "model", "tools", "context_management")
                if key in root_config
            }, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
            "skills": copy.deepcopy(actual_agent.get("skills") or []),
            "materials": material_proofs,
        }
        await self.cap["save_artifact"](
            "ma-session-snapshot.json", json.dumps(self.execution_snapshot, sort_keys=True).encode(), self.owner,
        )
        if binding.get("ready_file") and self.runtime_tree:
            await self._ready()

    async def _ready(self):
        """Read a bootstrap proof from real Files bytes before any task message."""
        from teamEvolver.managed_agents.client import _bounded, validate_url
        probe = (self.record["config"].get("runtime_binding") or {}).get("ready_probe_message")
        if probe:
            # Some real MA deployments register setup outputs only after a tool
            # turn. Both branches run the same configured runtime-only probe.
            observed = await self._send(probe, probe=True)
            self.bootstrap_metrics = dict(observed.metrics)
        for _ in range(90):
            self.remaining()
            files = await self.client.request("GET", "files", params={"scope_id": self.session_id, "purpose": "agent"})
            proof = next((f for f in files.get("data", [])
                          if (f.get("filename") or f.get("name")) == "team-evolver-runtime-ready.json"), None)
            if proof:
                url = proof.get("download_url")
                if not url:
                    url = (await self.client.request("GET", f"files/{proof['id']}")).get("download_url")
                if not url:
                    raise ReplayUnsupported("Runtime ready proof has no downloadable bytes")
                validate_url(url.split("?", 1)[0], allow_loopback=self.client.allow_loopback)
                async with self.client.download_http.stream("GET", url) as response:
                    if response.status_code != 200:
                        raise ReplayUnsupported("Runtime ready proof cannot be downloaded")
                    data = await _bounded(response, 1024 * 1024)
                value = json.loads(data)
                if value.get("ready") is not True or value.get("runtime_tree_sha256") != self.runtime_tree:
                    raise ReplayUnsupported("Runtime ready proof/tree hash mismatch")
                saved = await self.cap["save_artifact"]("team-evolver-runtime-ready.json", data, self.owner)
                self.execution_snapshot.update(runtime_ready_verified=True, runtime_ready_proof=saved)
                return
            await asyncio.sleep(min(2, self.remaining()))
        raise ReplayUnsupported("Runtime bootstrap proof unavailable before task execution")

    async def _history(self):
        events = await self.client.history(self.session_id)
        self.root_thread_id = next((e["session_thread_id"] for e in events
                                    if e.get("type") == "session.thread_status_running"
                                    and e.get("session_thread_id")), self.root_thread_id)
        threads = await self.client.listing(f"sessions/{self.session_id}/threads", max_pages=10)
        done = True
        for thread in child_threads(events, threads):
            history = await self.client.history(self.session_id, thread["id"])
            events.extend({**e, "session_thread_id": thread["id"]} for e in history)
            if thread.get("status") not in {"idle", "terminated", "failed", "completed"}:
                done = False
            if history and history[-1].get("stop_reason", {}).get("type") == "requires_action":
                done = False
        result = complete(events)
        result.sort(key=lambda e: str(e.get("processed_at") or "\uffff"))
        return result, done

    async def _send(self, message, *, probe=False):
        if not self.session_id:
            await self._start()
        if not probe:
            self.turn_num += 1
        collected, sent, accepted_ids, last_end, completed = {}, False, set(), None, False
        seeded = False
        for _ in range(5):  # bounded reconnects, never re-submit an accepted event
            stream = self.client.stream(self.session_id)
            polled = _polled_stream(stream)
            try:
                async for event in polled:
                    self.remaining()
                    if event.get("type") == "bridge.ready":
                        history, _ = await self._history()
                        if not sent:
                            if not seeded:
                                self.seen.update(e["id"] for e in history)
                                seeded = True
                            content = [{"type": "text", "text": message}]
                            if self.turn_num == 1 and not probe:
                                content += self.material_blocks
                            async def find_message(row):
                                history = await self.client.history(self.session_id)
                                matches = [e for e in history if e["id"] not in self.seen
                                           and e.get("type") == "user.message" and e.get("content") == content]
                                return {"data": matches} if len(matches) == 1 else None
                            receipt = await self.effects.apply(
                                f"{self.owner}:message:{'runtime-probe' if probe else self.turn_num}", "replay-event",
                                {"session_id": self.session_id, "content": content},
                                lambda: self.client.request("POST", f"sessions/{self.session_id}/events",
                                                            json={"events": [{"type": "user.message",
                                                                              "content": content}]}),
                                find_message,
                            )
                            accepted_ids = {e["id"] for e in receipt.get("data", [])}
                            if not accepted_ids:
                                raise RuntimeError("MA message acceptance lacks event IDs")
                            sent = True
                            collected.update({e["id"]: e for e in history if e["id"] not in self.seen})
                        else:
                            collected.update({e["id"]: e for e in history if e["id"] not in self.seen})
                    elif event.get("type") == "bridge.poll" and sent:
                        history, _ = await self._history()
                        collected.update({e["id"]: e for e in history if e["id"] not in self.seen})
                    elif event.get("id") and event["id"] not in self.seen:
                        collected[event["id"]] = event
                    current = complete(collected.values())
                    if not sent:
                        continue
                    processed = {e["id"] for e in current if e.get("processed_at")}
                    if not accepted_ids <= processed:
                        continue
                    _raise_replay_error(current)
                    last_end = stable_end(current)
                    if last_end:
                        history, children_done = await self._history()
                        collected.update({e["id"]: e for e in history if e["id"] not in self.seen})
                        final_events = complete(collected.values())
                        _raise_replay_error(final_events)
                        actual_end = stable_end(final_events)
                        if children_done and actual_end and actual_end["id"] == last_end["id"]:
                            completed = True
                            break
                if completed:
                    break
            except MAError as exc:
                if exc.status not in {408, 429, 500, 502, 503, 504}:
                    raise
            except httpx.HTTPError:
                pass  # replacement SSE opens before history recovery
            finally:
                await polled.aclose()
                await stream.aclose()
        if not completed:
            raise TimeoutError("MA turn did not reach stable end_turn")
        events = complete(collected.values())
        self.seen.update(e["id"] for e in events)
        _raise_replay_error(events)
        if probe:
            results = [e for e in events if e.get("type") in {
                "agent.tool_result", "agent.mcp_tool_result", "user.tool_result", "user.custom_tool_result",
            }]
            if results and results[-1].get("is_error") is True:
                code = ("task_service_unavailable" if "task service unavailable" in text(results[-1]).lower()
                        else "tool_error")
                raise ReplayUnsupported("MA runtime readiness probe failed: " + code)
        response = "\n".join(text(e) for e in events if e.get("type") == "agent.message"
                             and e.get("session_thread_id") in {None, "", self.root_thread_id})
        if not response:
            raise RuntimeError("MA turn produced no complete response")
        if any(e.get("type") in {"session.thread_status_terminated", "session.status_terminated"} or
               e.get("stop_reason", {}).get("type") == "retries_exhausted" for e in events):
            raise RuntimeError("MA turn or child thread failed")
        observed = metrics(events)
        if not probe and self.bootstrap_metrics:
            for key, value in self.bootstrap_metrics.items():
                if key in observed:
                    observed[key] += value
            self.bootstrap_metrics = {}
        artifacts = await self._artifacts(expected=declared_outputs(events))
        # Persist the exact protocol events as well as the judge projection.
        # The adapter's transport metrics still count only real tool events.
        await self.cap["save_artifact"](
            f"ma-events-turn-{self.turn_num}.json",
            json.dumps(events, ensure_ascii=False, sort_keys=True).encode(), self.owner,
        )
        projected = turns([
            {k: v for k, v in e.items() if k != "session_thread_id"}
            if self.root_thread_id and e.get("session_thread_id") == self.root_thread_id else e
            for e in events
        ])
        return AgentObservation(
            response=response, messages=tuple(m for t in projected for m in t["messages"]),
            artifacts=tuple(artifacts), metrics=observed, trace_id=self.session_id + ":" + str(self.turn_num),
            metrics_incomplete_reason="MA model usage unavailable" if "total_tokens" not in observed else "",
            execution_snapshot=copy.deepcopy(self.execution_snapshot),
        )

    async def _artifacts(self, *, expected=()):
        # MA may register Write outputs after the stable end_turn event. Keep
        # the Session alive until declared output bytes have actually arrived.
        deadline = min(self.deadline, time.monotonic() + ARTIFACT_REGISTRATION_SECONDS)
        while True:
            result = await self._download_artifacts()
            expected = set(expected)
            # Files API may return only a basename for an output in a nested
            # directory. Match it only when both sides identify a unique file;
            # a supplied directory must still match exactly.
            expected_names = Counter(path.rsplit("/", 1)[-1] for path in expected)
            normalized = [item["path"].removeprefix("/mnt/session/outputs/") for item in result]
            actual_names = Counter(path.rsplit("/", 1)[-1] for path in normalized)
            for item, path in zip(result, normalized, strict=True):
                if ("/" not in path and expected_names[path] == 1 and actual_names[path] == 1):
                    path = next(p for p in expected if p.rsplit("/", 1)[-1] == path)
                if path != item["path"]:
                    item["ma_filename"], item["path"] = item["path"], path
            missing = expected - {item["path"] for item in result}
            if not missing:
                return result
            if time.monotonic() >= deadline:
                raise ReplayUnsupported("Declared MA outputs unavailable after tool completion: "
                                        + ", ".join(sorted(missing)))
            await asyncio.sleep(min(1, deadline - time.monotonic()))

    async def _download_artifacts(self):
        result, after, total = [], None, 0
        for _ in range(20):
            page = await self.client.request("GET", "files", params={
                "scope_id": self.session_id, "purpose": "agent", "limit": 100,
                **({"after": after} if after else {}),
            })
            for item in page.get("data", []):
                url = item.get("download_url")
                if not url:
                    item = await self.client.request("GET", f"files/{item['id']}")
                    url = item.get("download_url")
                if not url:
                    continue
                from teamEvolver.managed_agents.client import _bounded, validate_url
                validate_url(url.split("?", 1)[0], allow_loopback=self.client.allow_loopback)
                async with self.client.download_http.stream("GET", url) as response:
                    if response.status_code != 200:
                        continue
                    data = await _bounded(response, 80 * 1024 * 1024)
                total += len(data)
                if total > 200 * 1024 * 1024 or len(result) >= 100:
                    raise ReplayUnsupported("Replay artifacts exceed archival limits")
                name = item.get("filename") or item.get("name") or item["id"]
                saved = await self.cap["save_artifact"](name, data, self.owner)
                from ..evidence import artifact_evidence
                evidence = await asyncio.to_thread(artifact_evidence, name, data)
                result.append({**saved, "evidence": evidence})
            if not page.get("has_more"):
                return result
            after = page.get("last_id")
            if not after:
                raise RuntimeError("MA output pagination missing cursor")
        raise ReplayUnsupported("Replay artifact page limit exceeded")

    def close(self):
        if self.closed:
            return
        self.closed = True
        if self.client:
            async def finish():
                try:
                    self.final_diagnostics = {"session_id": self.session_id,
                                              "execution_snapshot": copy.deepcopy(self.execution_snapshot)}
                    if self.session_id:
                        try:
                            events, done = await asyncio.wait_for(self._history(), timeout=20)
                            final = {"events": events, "children_complete": done, "metrics": metrics(events)}
                            self.final_diagnostics.update(
                                observed_metrics=final["metrics"], children_complete=done,
                            )
                        except Exception:
                            final = {"available": False, "reason": "Final MA history unavailable"}
                        saved = await self.cap["save_artifact"](
                            "ma-final-session.json", json.dumps(final, ensure_ascii=False, sort_keys=True).encode(),
                            self.owner,
                        )
                        self.final_diagnostics["final_history"] = saved
                finally:
                    try:
                        await self.cap["cleanup"](self.record["connection_id"], self.owner, self.client)
                    finally:
                        await self.client.close()
            self.cap["run"](finish(), timeout=60)
