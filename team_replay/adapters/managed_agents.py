"""Full isolated MA graph Replay. Runtime execution receives no evaluation rules."""

from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import io
import time
import zipfile

import httpx

from teamEvolver.managed_agents import MAError
from teamEvolver.managed_agents.events import child_threads, complete, metrics, stable_end, tag_values, text, turns

from ..artifacts import skill_treatment_files, skill_treatment_members
from ..hooks import AgentObservation, ReplayUnsupported


class ManagedAgentsFactory:
    """Dispatch recorded MA targets while retaining an optional non-MA factory."""

    def __init__(self, records, capabilities, fallback=None):
        self.records = copy.deepcopy(records)
        self.capabilities, self.fallback = capabilities, fallback
        self.revision = "managed-agents.v1"

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
        self.bootstrap_metrics = {}

    def remaining(self):
        value = self.deadline - time.monotonic()
        if value <= 0:
            raise TimeoutError("MA Replay branch deadline exceeded")
        return value

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

    async def _start(self):
        config = self.record["config"]
        capability = self.cap["replay_capability"](config, self.record["agent_graph"],
                                                  self.record["replay_environment"])
        if not capability["supported"]:
            raise ReplayUnsupported(capability["reason"])
        self.client = await self.cap["client"](self.record)
        self.effects = self.cap["effects"](self.record["connection_id"], self.owner)
        members = skill_treatment_members(self.context.treatment.skill)
        replacement, replacement_files = {}, {}
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
            replacement_files[semantic_name] = files
        graph, refs = self.record["agent_graph"], {}
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
                if ident not in binding.get("agents", []):
                    continue
                remote = binding.get("id")
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
                root_config = desired
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
        environment_config = copy.deepcopy(self.record["environment"]["config"])
        # Isolation infrastructure uses explicitly configured Replay Environment;
        # execution packages/setup are fixed from source, keeping comparable behavior.
        replay_environment = self.record["replay_environment"]
        for field in ("tos", "networking"):
            if field in replay_environment["config"]:
                environment_config[field] = copy.deepcopy(replay_environment["config"][field])
        binding = config.get("runtime_binding") or {}
        if binding and binding["skill_name"] not in replacement_files:
            from team_skills.transfer.packages import discover, read_zip
            bound = bindings.get(binding["skill_name"]) or {}
            refs = [s for a in graph["agents"].values() for s in a.get("skills") or []
                    if s.get("skill_id") == bound.get("id")]
            if not refs or len({str(s["version"]) for s in refs}) != 1:
                raise ReplayUnsupported("Recorded runtime Skill reference is missing or ambiguous")
            data = await self.client.download(f"skills/{refs[0]['skill_id']}/versions/{refs[0]['version']}/content")
            packages = discover(read_zip(data))
            if len(packages) != 1:
                raise ReplayUnsupported("Recorded runtime Bundle is unavailable")
            replacement_files[binding["skill_name"]] = packages[0].files
        if binding:
            environment_config, self.runtime_tree = self.cap["runtime_binding"](
                environment_config, binding, replacement_files[binding["skill_name"]],
            )
        resources = []
        for index, material in enumerate(self.context.materials):
            raw = base64.b64decode(material["content_b64"], validate=True)
            if hashlib.sha256(raw).hexdigest() != material["sha256"] or len(raw) > 20 * 1024 * 1024:
                raise ReplayUnsupported("Replay material content mismatch or size limit")
            path = material["path"]
            if path.startswith("/") or "\\" in path or any(p in {"", ".", ".."} for p in path.split("/")):
                raise ReplayUnsupported("Unsafe Replay material path")
            filename = "te-replay-" + hashlib.sha256((self.owner + str(index)).encode()).hexdigest()[:24]
            receipt = await self.effects.apply(
                f"{self.owner}:file:{index}", "replay-file",
                {"path": path, "filename": filename, "sha256": material["sha256"]},
                lambda p=filename, data=raw: self.client.request(
                    "POST", "files", data={"purpose": "agent"},
                    files={"file": (p.rsplit("/", 1)[-1], data, "application/octet-stream")},
                ),
            )
            resources.append({"type": "file", "file_id": receipt["id"], "mount_path": path})
            self.material_blocks.append({"type": "document", "source": {"type": "file", "file_id": receipt["id"]}})
        payload = {
            "agent": {"type": "agent_with_overrides", "id": entry, "version": graph["agents"][entry]["version"],
                      # Inherit the exact pinned model/base Agent. Explicit model
                      # overrides have separate permissions on real MA accounts.
                      **{k: v for k, v in root_config.items()
                         if k not in {"name", "metadata", "model", "base_agent"}}},
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
        receipt = await self.effects.apply(self.owner + ":session", "replay-session", payload,
                                            lambda: self.client.request("POST", "sessions", json=payload),
                                            find_owned_session)
        self.session_id = receipt["id"]
        if binding.get("ready_file") and self.runtime_tree:
            await self._ready()

    async def _ready(self):
        """Read a bootstrap proof from real Files bytes before any task message."""
        import json

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
                await self.cap["save_artifact"]("team-evolver-runtime-ready.json", data, self.owner)
                return
            await asyncio.sleep(min(2, self.remaining()))
        raise ReplayUnsupported("Runtime bootstrap proof unavailable before task execution")

    async def _history(self):
        events = await self.client.history(self.session_id)
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
                    last_end = stable_end(current)
                    if last_end:
                        history, children_done = await self._history()
                        collected.update({e["id"]: e for e in history if e["id"] not in self.seen})
                        actual_end = stable_end(complete(collected.values()))
                        if children_done and actual_end and actual_end["id"] == last_end["id"]:
                            completed = True
                            break
                    if any(e.get("stop_reason", {}).get("type") == "requires_action" for e in current
                           if not e.get("session_thread_id") and e.get("type") == "session.status_idle"):
                        raise ReplayUnsupported("MA requires human tool confirmation; no result is fabricated")
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
        response = "\n".join(text(e) for e in events if e.get("type") == "agent.message"
                             and not e.get("session_thread_id"))
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
        artifacts = await self._artifacts()
        import json
        # Persist the exact protocol events as well as the judge projection.
        # The adapter's transport metrics still count only real tool events.
        await self.cap["save_artifact"](
            f"ma-events-turn-{self.turn_num}.json",
            json.dumps(events, ensure_ascii=False, sort_keys=True).encode(), self.owner,
        )
        projected = turns(events)
        return AgentObservation(
            response=response, messages=tuple(m for t in projected for m in t["messages"]),
            artifacts=tuple(artifacts), metrics=observed, trace_id=self.session_id + ":" + str(self.turn_num),
            metrics_incomplete_reason="MA model usage unavailable" if "total_tokens" not in observed else "",
        )

    async def _artifacts(self):
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
                    if self.session_id:
                        import json
                        try:
                            events, done = await asyncio.wait_for(self._history(), timeout=20)
                            final = {"events": events, "children_complete": done, "metrics": metrics(events)}
                        except Exception:
                            final = {"available": False, "reason": "Final MA history unavailable"}
                        await self.cap["save_artifact"](
                            "ma-final-session.json", json.dumps(final, ensure_ascii=False, sort_keys=True).encode(),
                            self.owner,
                        )
                    await self.cap["cleanup"](self.record["connection_id"], self.owner, self.client)
                finally:
                    await self.client.close()
            self.cap["run"](finish(), timeout=60)
