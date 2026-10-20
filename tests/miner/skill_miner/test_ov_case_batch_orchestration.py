from __future__ import annotations

import asyncio
import copy
import json

import httpx
import pytest
from starlette.exceptions import HTTPException

from team_miner.skill_miner import bounded_compile_plan as planning
from team_miner.skill_miner.ov_case_batch_orchestration import CASE_BATCH_DIRECTORY_PROTOCOL, CASE_BATCH_STAGE
from team_miner.skill_miner.ov_staged_orchestration import OvStagedOrchestrationMixin, _json, _read, _save, _sha
from tests.miner.skill_miner.fixtures import build_document
from tests.miner.skill_miner.support.host import AdmissionMixin
from tests.miner.skill_miner.support.ov_staged_orchestration import FakeCompile, Owner, drive, start


class BatchFake(FakeCompile):
    def __init__(self):
        super().__init__()
        self.batches = []
        self.break_slots = {}
        self.duplicate = False
        self.same_key_503 = False
        self.deferred_slots = set()
        self.crosspacket = False
        self.extra = False
        self.input_extras = {}

    async def request(self, account, method, path, **kwargs):
        request = kwargs.get("json", {})
        if path == "/api/v1/fs/ls":
            params = kwargs["params"]
            assert params["output"] == "original" and params["recursive"] == "false"
            assert params["show_all_hidden"] == "false"
            prefix = params["uri"].rstrip("/") + "/"
            entries = {}
            for uri in [*self.contents, *self.input_extras, *self.directories]:
                if not uri.startswith(prefix):
                    continue
                relative = uri[len(prefix):]
                name = relative.split("/", 1)[0]
                is_dir = "/" in relative or uri in self.directories
                if not is_dir and name.startswith("."):
                    continue
                entries[prefix + name] = {"uri": prefix + name, "name": name, "isDir": is_dir}
            rows = sorted(entries.values(), key=lambda row: (not row["isDir"], row["name"].lower(), row["name"]))
            return rows[params["offset"]:params["offset"] + params["node_limit"]]
        if (method != "POST" or path != "/api/v1/compile"
                or "/stages/case_batch-" not in request["from"][0]):
            return await super().request(account, method, path, **kwargs)
        key = kwargs["extra_headers"]["Idempotency-Key"]
        self.calls.append((key, copy.deepcopy(request)))
        if any(uri not in self.directories for uri in request["from"]):
            raise HTTPException(422, "Compile source must be a directory")
        assert len(request["from"]) == 1
        if self.same_key_503:
            self.same_key_503 = False
            raise HTTPException(503, "Temporary admission failure")
        if key in self.accepted:
            task_id, previous = self.accepted[key]
            assert previous == request
        else:
            task_id = "cmp_batch_" + str(len(self.accepted))
            self.accepted[key] = (task_id, copy.deepcopy(request))
            prefix = request["from"][0] + "/"
            packets = [json.loads(self.contents[uri]) for uri in sorted(self.contents)
                       if uri.startswith(prefix) and not uri.rsplit("/", 1)[-1].startswith(".")]
            self.batches.append(copy.deepcopy(packets))
            self.tasks[task_id] = {
                "task_id": task_id,
                "status": "completed",
                "meta": {"pipeline": {"warnings": ["Partial output"], "errors": []}},
            }
            for packet in packets:
                controls = packet["derived_controls"]
                ordinal = int(packet["output_path"].split("-")[-1].split(".")[0])
                failures = self.break_slots.get(ordinal, [])
                failure = failures.pop(0) if failures else ""
                if failure == "missing":
                    continue
                case = build_document(controls["skill_name"], [{"query": "Fixture"}])["cases"][0]
                case["case_id"] = controls["case_id"]
                case["query"] = "针对原文检查此独立边界场景 " + controls["coverage_dimension"] + controls["case_id"]
                if self.duplicate or failure == "duplicate":
                    case["query"] = "所有批内请求完全相同场景"
                case["provenance"]["session_ids"] = controls["allowed_source_refs"]
                if self.crosspacket or failure == "foreign":
                    case["provenance"]["session_ids"] = ["viking://unassigned/foreign.md"]
                if failure == "invalid":
                    case["checks"] = case["checks"][:1]
                if ordinal in self.deferred_slots:
                    case = {
                        "schema_version": planning.DEFERRED_SCHEMA,
                        "reason": "原文缺完整条件",
                        "source_refs": controls["allowed_source_refs"],
                    }
                self.contents[request["to"] + "/" + packet["output_path"]] = json.dumps(case)
            if self.extra:
                self.contents[request["to"] + "/unexpected.json"] = "{}"
        if self.lose_ack:
            self.lose_ack = False
            raise httpx.ReadTimeout("Lost batch ACK after acceptance")
        return {"task_id": task_id}


class BatchOwner(AdmissionMixin, OvStagedOrchestrationMixin):
    """Use the actual production mixin defaults; delegate only inert repository/transport fixtures."""

    def __init__(self, root):
        self.fixture = Owner(root)
        self.fixture.ov = BatchFake()
        self.repo, self.ov = self.fixture.repo, self.fixture.ov

    def __getattr__(self, name):
        return getattr(self.fixture, name)


async def to_batch(owner):
    job = await start(owner, count=12)
    while True:
        _, state = await owner._staged_load(job)
        if state.get("current", {}).get("stage") == CASE_BATCH_STAGE:
            return job
        job = await owner._advance_staged_round(job, owner.ov.tasks[job["ov_task_id"]])


async def legacy_file_batch(owner, *, accepted=False):
    """Freeze the actual pre-directory protocol without weakening the public gateway fixture."""
    job = await to_batch(owner)
    root, state = await owner._staged_load(job)
    original = copy.deepcopy(state["current"])
    locators = _read(root / original["packet_file"])
    contexts = [row["context"] for row in locators]
    texts = [(root / row["local_path"]).read_text() for row in locators]
    digest = _sha(_json([state["round"], contexts, [_sha(text) for text in texts], original["compiler_sha256"]]))[:24]
    child = CASE_BATCH_STAGE + "-" + digest
    folder = "children/" + child
    input_uri = original["input_uri"].replace(original["child"], child)
    output_uri = original["output_uri"].replace(original["child"], child)
    for row, text in zip(locators, texts, strict=True):
        row["uri"] = row["uri"].replace(original["child"], child)
        row["local_path"] = row["local_path"].replace(original["child"], child)
        path = root / row["local_path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    request = _read(root / original["request_file"])
    request.update({"from": [row["uri"] for row in locators], "to": output_uri})
    old = {
        **original, "child": child, "input_uri": input_uri, "output_uri": output_uri,
        "key": original["key"].rsplit("-", 1)[0] + "-" + digest,
        "request_file": folder + "/request.json", "packet_file": folder + "/packets.json",
        "compiler_file": folder + "/compiler.md", "task_id": "",
        "request_sha256": _sha(_json(request)), "packet_sha256": _sha(_json(locators) + "\n"),
    }
    old.pop("input_protocol")
    _save(root / old["packet_file"], locators)
    _save(root / old["request_file"], request)
    (root / old["compiler_file"]).write_bytes((root / original["compiler_file"]).read_bytes())
    _save(root / folder / "metadata.json", old)
    state["current"] = old
    _save(root / "state.json", state)
    owner.ov.accepted.pop(original["key"])
    if accepted:
        owner.ov.accepted[old["key"]] = (original["task_id"], copy.deepcopy(request))
        for path, text in list(owner.ov.contents.items()):
            if path.startswith(original["output_uri"] + "/"):
                owner.ov.contents[path.replace(original["child"], child)] = text
    job = owner.repo.update_job(
        job["job_id"], status="preparing", ov_task_id="", compile_request=request,
        submission_idempotency_key=old["key"],
    )
    return job, root, state


def test_two_independent_batches_complete_twelve_cases(tmp_path):
    async def check():
        owner = BatchOwner(tmp_path)
        final = await drive(owner, await start(owner, count=12))
        assert final["status"] == "waiting_human"
        assert [len(batch) for batch in owner.ov.batches] == [8, 4]
        root, _ = await owner._staged_load(final)
        ledger = _read(root / "case-batches/refund-review/ledger.json")
        assert all(slot["status"] == "valid" for slot in ledger["slots"])
        dataset = json.loads((owner.repo.workspace(final) / "compiled_skill/refund-review/benchmark.json").read_text())
        assert len(dataset["cases"]) == 12
        assert len({planning.normalize_case_query(case["query"]) for case in dataset["cases"]}) == 12
        for batch in owner.ov.batches:
            assert len({packet["output_path"] for packet in batch}) == len(batch)
            assert all(len(json.dumps(packet, ensure_ascii=False, separators=(",", ":"))) <= 32000 for packet in batch)
        batch_requests = [request for _, request in owner.ov.calls if "/stages/case_batch-" in request["from"][0]]
        assert len(batch_requests) == 2
        assert all(len(request["from"]) == 1 and request["from"][0] in owner.ov.directories
                   for request in batch_requests)

    asyncio.run(check())


def test_partial_batch_only_missing_invalid_slots_repaired_and_valid_siblings_retained(tmp_path):
    async def check():
        owner = BatchOwner(tmp_path)
        owner.ov.break_slots = {1: ["missing"], 3: ["invalid"]}
        final = await drive(owner, await start(owner, count=12))
        assert final["status"] == "waiting_human"
        assigned = [
            int(packet["output_path"].split("-")[-1].split(".")[0]) for batch in owner.ov.batches for packet in batch
        ]
        assert assigned.count(0) == assigned.count(2) == 1
        assert assigned.count(1) == assigned.count(3) == 2
        root, _ = await owner._staged_load(final)
        ledger = _read(root / "case-batches/refund-review/ledger.json")
        assert ledger["slots"][1]["format_attempts"] == ledger["slots"][3]["format_attempts"] == 1

    asyncio.run(check())


def test_lost_ack_and_503_keep_same_frozen_key_request(tmp_path):
    async def check():
        owner = BatchOwner(tmp_path)
        job = await to_batch(owner)
        root, state = await owner._staged_load(job)
        current = copy.deepcopy(state["current"])
        state["current"]["task_id"] = ""
        _save(root / "state.json", state)
        previous_calls = len(owner.ov.calls)
        recovered = await owner._resume_staged_round(job)
        assert len(owner.ov.calls) == previous_calls
        assert recovered["ov_task_id"] == current["task_id"]
        assert _read(root / current["request_file"]) == owner.ov.accepted[current["key"]][1]

    asyncio.run(check())


def test_foreign_source_and_duplicate_query_rejected_with_bounded_attempts(tmp_path):
    async def check():
        owner = BatchOwner(tmp_path)
        owner.ov.crosspacket = True
        final = await drive(owner, await start(owner))
        assert final["status"] == "failed"
        root, _ = await owner._staged_load(final)
        ledger = _read(root / "case-batches/refund-review/ledger.json")
        assert max(slot["format_attempts"] for slot in ledger["slots"]) == 3
        assert not any(slot["status"] == "valid" for slot in ledger["slots"])

    asyncio.run(check())


def test_default_production_path_is_batched_and_six_frozen_compilers_unchanged(tmp_path):
    async def check():
        owner = BatchOwner(tmp_path)
        assert "_staged_cases_batched" not in BatchOwner.__dict__
        assert owner._staged_cases_batched() is True
        job = await start(owner)
        root, state = await owner._staged_load(job)
        compilers = {name: (root / "compilers" / (name + ".md")).read_bytes() for name in state["compilers"]}
        final = await drive(owner, job)
        assert final["status"] == "waiting_human"
        assert all((root / "compilers" / (name + ".md")).read_bytes() == content for name, content in compilers.items())
        assert (root / "aux-compilers/case_batch_source_bound.md").is_file()

    asyncio.run(check())


def test_503_retries_exact_key_and_request_without_replacing_frozen_packets(tmp_path):
    async def check():
        owner = BatchOwner(tmp_path)
        job = await start(owner)
        while True:
            root, state = await owner._staged_load(job)
            if state["current"]["stage"] == "evaluation":
                break
            job = await owner._advance_staged_round(job, owner.ov.tasks[job["ov_task_id"]])
        owner.ov.same_key_503 = True
        job = await owner._advance_staged_round(job, owner.ov.tasks[job["ov_task_id"]])
        root, state = await owner._staged_load(job)
        current = copy.deepcopy(state["current"])
        first_key, first_request = owner.ov.calls[-1]
        assert current["task_id"] == "" and current["key"] == first_key
        frozen_packets = {
            row["local_path"]: (root / row["local_path"]).read_bytes() for row in _read(root / current["packet_file"])
        }
        job = await owner._resume_staged_round(job)
        assert owner.ov.calls[-1] == (first_key, first_request)
        assert all((root / path).read_bytes() == content for path, content in frozen_packets.items())
        assert job["ov_task_id"]
        assert (await drive(owner, job))["status"] == "waiting_human"

    asyncio.run(check())


def test_stop_with_accepted_lost_ack_recovers_exact_task_without_new_post(tmp_path):
    async def check():
        owner = BatchOwner(tmp_path)
        job = await to_batch(owner)
        root, state = await owner._staged_load(job)
        expected_task = state["current"]["task_id"]
        state["current"]["task_id"] = ""
        _save(root / "state.json", state)
        job = owner.repo.update_job(job["job_id"], status="stopping", ov_cancel_reason="user_stop")
        previous_posts = len(owner.ov.calls)
        job = await owner._resume_staged_round(job)
        assert job["status"] == "stopping" and job["ov_task_id"] == expected_task
        assert len(owner.ov.calls) == previous_posts
        job = await owner._advance_staged_round(job, owner.ov.tasks[expected_task])
        assert job["status"] == "stopped" and len(owner.ov.calls) == previous_posts

    asyncio.run(check())


def test_stop_before_any_acceptance_does_not_provision_or_post_again(tmp_path):
    async def check():
        owner = BatchOwner(tmp_path)
        job = await start(owner)
        while True:
            _, state = await owner._staged_load(job)
            if state["current"]["stage"] == "evaluation":
                break
            job = await owner._advance_staged_round(job, owner.ov.tasks[job["ov_task_id"]])
        owner.ov.same_key_503 = True
        job = await owner._advance_staged_round(job, owner.ov.tasks[job["ov_task_id"]])
        job = owner.repo.update_job(job["job_id"], status="stopping", ov_cancel_reason="user_stop")
        calls = len(owner.ov.calls)
        job = await owner._resume_staged_round(job)
        assert job["status"] == "stopped" and len(owner.ov.calls) == calls

    asyncio.run(check())


def test_duplicate_queries_repair_only_duplicate_siblings_and_never_complete_fake_dataset(tmp_path):
    async def check():
        owner = BatchOwner(tmp_path)
        owner.ov.duplicate = True
        final = await drive(owner, await start(owner))
        assert final["status"] == "failed"
        root, _ = await owner._staged_load(final)
        ledger = _read(root / "case-batches/refund-review/ledger.json")
        assert ledger["slots"][0]["status"] == "valid"
        assert sum(slot["status"] == "valid" for slot in ledger["slots"]) == 1
        assert max(slot["format_attempts"] for slot in ledger["slots"]) == 3
        assert (
            sum(packet["output_path"] == "case-output-00.json" for batch in owner.ov.batches for packet in batch) == 1
        )
        assert not (owner.repo.workspace(final) / "compiled_skill/refund-review/benchmark.json").exists()

    asyncio.run(check())


def test_late_ledger_write_replays_valid_sibling_even_when_remote_file_disappears(tmp_path, monkeypatch):
    from team_miner.skill_miner import ov_staged_orchestration

    async def check():
        owner = BatchOwner(tmp_path)
        job = await to_batch(owner)
        root, state = await owner._staged_load(job)
        current = copy.deepcopy(state["current"])
        save = ov_staged_orchestration._save
        armed = True

        def broken_save(path, value):
            nonlocal armed
            if armed and path == root / "case-batches/refund-review/ledger.json":
                armed = False
                raise OSError("Interrupted after valid Case audit")
            return save(path, value)

        monkeypatch.setattr(ov_staged_orchestration, "_save", broken_save)
        job = await owner._advance_staged_round(job, owner.ov.tasks[job["ov_task_id"]])
        assert "Interrupted" in job["submission_error"]
        owner.ov.contents.pop(current["output_uri"] + "/case-output-00.json")
        monkeypatch.setattr(ov_staged_orchestration, "_save", save)
        final = await drive(owner, job)
        assert final["status"] == "waiting_human"
        ledger = _read(root / "case-batches/refund-review/ledger.json")
        assert ledger["slots"][0]["status"] == "valid" and ledger["slots"][0]["format_attempts"] == 0
        assert (
            sum(packet["output_path"] == "case-output-00.json" for batch in owner.ov.batches for packet in batch) == 1
        )

    asyncio.run(check())


def test_missing_evidence_tries_all_original_method_groups_then_defers_without_padding(tmp_path):
    async def check():
        owner = BatchOwner(tmp_path)
        owner.ov.deferred_slots = {0}
        final = await drive(owner, await start(owner, count=12))
        assert final["status"] == "waiting_human" and final["candidate_count"] == 0
        root, _ = await owner._staged_load(final)
        ledger = _read(root / "case-batches/refund-review/ledger.json")
        assert ledger["slots"][0]["group_attempt"] == 2
        assert ledger["slots"][0]["format_attempts"] == 0
        assignments = [
            packet for batch in owner.ov.batches for packet in batch if packet["output_path"] == "case-output-00.json"
        ]
        assert len(assignments) == 2
        assert set(assignments[0]["input_ids"]).isdisjoint(assignments[1]["input_ids"])
        assert _read(root / "plan.json")["deferred"]

    asyncio.run(check())


def test_unexpected_side_file_never_accepted_but_valid_expected_cases_remain_durable(tmp_path):
    async def check():
        owner = BatchOwner(tmp_path)
        owner.ov.extra = True
        final = await drive(owner, await start(owner))
        assert final["status"] == "failed"
        root, _ = await owner._staged_load(final)
        ledger = _read(root / "case-batches/refund-review/ledger.json")
        assert sum(slot["status"] == "valid" for slot in ledger["slots"]) == 8
        assert not (owner.repo.workspace(final) / "compiled_skill/refund-review/benchmark.json").exists()

    asyncio.run(check())


def test_legacy_case_child_is_accepted_before_remaining_cases_use_new_batches(tmp_path):
    async def check():
        legacy = Owner(tmp_path)
        legacy.ov = BatchFake()
        job = await start(legacy, count=12)
        while True:
            root, state = await legacy._staged_load(job)
            if state["current"]["stage"] == "case":
                break
            job = await legacy._advance_staged_round(job, legacy.ov.tasks[job["ov_task_id"]])
        current = copy.deepcopy(state["current"])
        frozen = {key: (root / current[key]).read_bytes() for key in ("request_file", "packet_file", "compiler_file")}
        state["current"]["task_id"] = ""
        _save(root / "state.json", state)
        modern = BatchOwner(tmp_path)
        modern.fixture, modern.repo, modern.ov = legacy, legacy.repo, legacy.ov
        calls = len(modern.ov.calls)
        job = await modern._resume_staged_round(job)
        assert len(modern.ov.calls) == calls
        final = await drive(modern, job)
        assert final["status"] == "waiting_human"
        assert sum(len(batch) for batch in modern.ov.batches) == 11
        assert all(packet["output_path"] != "case-output-00.json" for batch in modern.ov.batches for packet in batch)
        assert all((root / current[key]).read_bytes() == content for key, content in frozen.items())

    asyncio.run(check())


@pytest.mark.parametrize("tamper", ["manifest_source", "manifest_path", "packet", "ledger"])
def test_accept_rejects_frozen_assignment_tamper_without_post_or_case_acceptance(tmp_path, tamper):
    async def check():
        owner = BatchOwner(tmp_path)
        job = await to_batch(owner)
        root, state = await owner._staged_load(job)
        current = state["current"]
        locators = _read(root / current["packet_file"])
        if tamper == "manifest_source":
            locators[0]["context"]["source_refs"] = ["viking://foreign/doc.md"]
            _save(root / current["packet_file"], locators)
        elif tamper == "manifest_path":
            locators[0]["output_path"] = "unexpected-case.json"
            _save(root / current["packet_file"], locators)
        elif tamper == "packet":
            packet = _read(root / locators[0]["local_path"])
            packet["evidence"][0]["content"] += "fabricated condition"
            _save(root / locators[0]["local_path"], packet)
        else:
            _save(root / "case-batches/refund-review/ledger.json", {"wrong": []})
        calls = len(owner.ov.calls)
        final = await owner._advance_staged_round(job, owner.ov.tasks[job["ov_task_id"]])
        assert final["status"] == "failed" and len(owner.ov.calls) == calls
        assert not list((root / "cases/refund-review").glob("*.json"))

    asyncio.run(check())


def test_case_batch_bound_revision_with_twenty_mb_archived_prior_and_custom_ids(tmp_path):
    from team_miner.skill_miner.ov_compile_mining import SKILL_BODY
    from team_miner.skill_miner.ov_round_orchestration import OvRoundOrchestrationMixin

    async def check():
        owner = BatchOwner(tmp_path)
        job, entries = owner.new(count=12)
        first_manifest = owner.repo._job_dir(job["job_id"]) / "round_inputs/round-1/manifest.json"
        first_manifest.write_text(json.dumps(entries), encoding="utf-8")
        job = owner.repo.update_job(job["job_id"], max_rounds=2, input_manifest=str(first_manifest))
        policy = {"version": 1, "basis": "frozen_original_source", "language": "zh-CN"}
        job = await drive(owner, await owner._prepare_staged_round(job, SKILL_BODY, entries, policy, ""))
        assert job["status"] == "waiting_human"
        workspace = owner.repo.workspace(job)
        dataset_path = workspace / "compiled_skill/refund-review/benchmark.json"
        document = json.loads(dataset_path.read_text())
        identities = [f"reviewed-custom:{index}:" + "x" * 100 for index in range(12)]
        for case, identity in zip(document["cases"], identities, strict=True):
            case["case_id"] = identity
        dataset_path.write_text(json.dumps(document), encoding="utf-8")
        skill_path = workspace / "compiled_skill/refund-review/SKILL.md"
        with skill_path.open("w", encoding="utf-8") as stream:
            stream.write("---\nname: refund-review\ndescription: 复核退款\n---\n\n# 复核方法\n")
            chunk = '原文引用含双引号"和反斜杠\\，保留条件。\n' * 2000
            for _ in range(400):
                stream.write(chunk)
        assert skill_path.stat().st_size > 20 * 1024 * 1024
        job = owner.repo.prepare_next_round(job["job_id"], job["pending_checkpoint"]["id"], {}, [], {})
        second_entries = await OvRoundOrchestrationMixin._freeze_round_inputs(owner, job, staged=True)
        job_dir = owner.repo._job_dir(job["job_id"])
        job = owner.repo.update_job(job["job_id"], input_manifest=str(job_dir / "round_inputs/round-2/manifest.json"))
        archived = job_dir / "rounds/round-1/reviewed_output/compiled_skill/refund-review/SKILL.md"
        assert archived.is_file() and archived.stat().st_size > 20 * 1024 * 1024
        prior = _read(job_dir / "round_inputs/round-2/previous-candidates/refund-review.json")
        assert prior["case_ids"] == identities
        owner.ov.directories.add(job["source_snapshot"].rsplit("/", 1)[0])
        batch_start = len(owner.ov.batches)
        feedback = "需要检查修改后的边界条件。" * 500
        revision = json.dumps(
            {
                "feedback": feedback,
                "review_questions": [{"qid": "region" + "q" * 160, "question": "是否保留地区？" * 40}],
            }
        )
        final = await drive(owner, await owner._prepare_staged_round(job, SKILL_BODY, second_entries, policy, revision))
        assert final["status"] == "waiting_human"
        dataset_path = owner.repo.workspace(final) / "compiled_skill/refund-review/benchmark.json"
        assert [case["case_id"] for case in json.loads(dataset_path.read_text())["cases"]] == identities
        root, state = await owner._staged_load(final)
        seen = []
        for path in root.glob("children/case_batch-*/packets.json"):
            request = _read(path.parent / "request.json")
            body = (path.parent / "compiler.md").read_text()
            for locator in _read(path):
                text = (root / locator["local_path"]).read_text()
                packet = json.loads(text)
                assert len(text) <= 32000
                assert len(text) + len(body) + len(request["instruction"]) <= 48000
                note = json.loads(packet["revision_context"])
                assert note["feedback"] and note["feedback_truncated"]
                assert note["review_questions"][0]["qid"].startswith("region")
                assert len(note["review_questions"][0]["qid"]) == 160
                assert note["review_questions"][0]["qid_truncated"]
                assert note["review_questions"][0]["question_truncated"]
                assert note["full_revision_archived"]
                assert note["revision_context_is_evidence"] is False
                assert note["complete_previous_candidate_archived"] is True
                assert note["previous_candidate"]["case_id"] == packet["derived_controls"]["case_id"]
                assert note["previous_candidate"]["skill_body_excerpt_omitted"]
                assert "skill_body_excerpt" not in note["previous_candidate"]
                seen.append(packet["derived_controls"]["case_id"])
        assert len(owner.ov.batches[batch_start:]) == 2 and set(seen) == set(identities)
        assert state["current"] is None

    asyncio.run(check())


def test_public_gateway_rejects_legacy_files_without_accepting_a_task(tmp_path):
    async def check():
        owner = BatchOwner(tmp_path)
        _, root, state = await legacy_file_batch(owner)
        current = state["current"]
        with pytest.raises(HTTPException, match="directory") as exc:
            await owner.ov.request(
                "fixture", "POST", "/api/v1/compile", json=_read(root / current["request_file"]),
                extra_headers={"Idempotency-Key": current["key"]},
            )
        assert exc.value.status_code == 422 and current["key"] not in owner.ov.accepted

    asyncio.run(check())


def test_unaccepted_legacy_request_superseded_with_immutable_audit_and_same_packets(tmp_path):
    async def check():
        owner = BatchOwner(tmp_path)
        job, root, state = await legacy_file_batch(owner)
        old = copy.deepcopy(state["current"])
        old_files = {path.name: path.read_bytes() for path in (root / "children" / old["child"]).iterdir()}
        old_rows = _read(root / old["packet_file"])
        ledger_path = root / "case-batches/refund-review/ledger.json"
        ledger = _read(ledger_path)
        ledger["slots"][0].update(format_attempts=1, group_attempt=1, error="previous bounded attempt")
        _save(ledger_path, ledger)
        before = ledger_path.read_bytes()
        posts = len(owner.ov.calls)
        job = await owner._resume_staged_round(job)
        _, state = await owner._staged_load(job)
        new = copy.deepcopy(state["current"])
        assert new["child"] != old["child"] and new["key"] != old["key"]
        assert new["input_protocol"] == CASE_BATCH_DIRECTORY_PROTOCOL
        request = _read(root / new["request_file"])
        assert request["from"] == [new["input_uri"]] and new["input_uri"] in owner.ov.directories
        new_rows = _read(root / new["packet_file"])
        assert [row["context"] for row in new_rows] == [row["context"] for row in old_rows]
        assert [(root / row["local_path"]).read_bytes() for row in new_rows] == [
            (root / row["local_path"]).read_bytes() for row in old_rows
        ]
        assert ledger_path.read_bytes() == before and len(owner.ov.calls) == posts + 1
        assert {path.name: path.read_bytes() for path in (root / "children" / old["child"]).iterdir()} == old_files
        audit_path = root / "case-batches/superseded" / (old["child"] + ".json")
        audit = _read(audit_path)
        assert audit["old_current"] == old and audit["old_request"] == _read(root / old["request_file"])
        assert audit["submission_lookup"] == "not_found" and audit["lookup_key"] == old["key"]
        assert audit["replacement_child"] == new["child"] and audit["case_slots_and_attempts_preserved"]
        # Recover a late old-state save using the durable audit and the already accepted replacement key.
        audit_bytes = audit_path.read_bytes()
        state["current"] = old
        _save(root / "state.json", state)
        job = owner.repo.update_job(job["job_id"], status="preparing", ov_task_id="")
        calls = len(owner.ov.calls)
        job = await owner._resume_staged_round(job)
        assert job["ov_task_id"] == new["task_id"] and len(owner.ov.calls) == calls
        assert audit_path.read_bytes() == audit_bytes and ledger_path.read_bytes() == before
        assert (await drive(owner, job))["status"] == "waiting_human"

    asyncio.run(check())


@pytest.mark.parametrize("stop", [False, True])
def test_accepted_legacy_key_recovers_exact_task_without_supersession_or_post(tmp_path, stop):
    async def check():
        owner = BatchOwner(tmp_path)
        job, root, state = await legacy_file_batch(owner, accepted=True)
        current = copy.deepcopy(state["current"])
        task_id = owner.ov.accepted[current["key"]][0]
        frozen = {path.name: path.read_bytes() for path in (root / "children" / current["child"]).iterdir()}
        if stop:
            job = owner.repo.update_job(job["job_id"], status="stopping", ov_cancel_reason="user_stop")
        calls = len(owner.ov.calls)
        job = await owner._resume_staged_round(job)
        assert job["ov_task_id"] == task_id and len(owner.ov.calls) == calls
        assert not (root / "case-batches/superseded").exists()
        assert all((root / "children" / current["child"] / name).read_bytes() == text
                   for name, text in frozen.items() if name != "metadata.json")
        final = await drive(owner, job)
        assert final["status"] == ("stopped" if stop else "waiting_human")

    asyncio.run(check())


def test_stopped_unaccepted_legacy_request_is_not_superseded(tmp_path):
    async def check():
        owner = BatchOwner(tmp_path)
        job, root, state = await legacy_file_batch(owner)
        old = copy.deepcopy(state["current"])
        job = owner.repo.update_job(job["job_id"], status="stopping", ov_cancel_reason="user_stop")
        calls = len(owner.ov.calls)
        job = await owner._resume_staged_round(job)
        assert job["status"] == "stopped" and len(owner.ov.calls) == calls
        assert _read(root / "state.json")["current"] == old
        assert not (root / "case-batches/superseded").exists()

    asyncio.run(check())


@pytest.mark.parametrize("tamper", ["extra", "nested", "hidden_directory", "packet", "missing"])
def test_directory_inventory_and_remote_packet_integrity_reject_before_case_acceptance(tmp_path, tamper):
    async def check():
        owner = BatchOwner(tmp_path)
        job = await to_batch(owner)
        root, state = await owner._staged_load(job)
        current = state["current"]
        locators = _read(root / current["packet_file"])
        if tamper == "extra":
            owner.ov.contents[current["input_uri"] + "/extra.json"] = "{}"
        elif tamper in {"nested", "hidden_directory"}:
            directory = "/.hidden" if tamper == "hidden_directory" else "/nested"
            owner.ov.contents[current["input_uri"] + directory + "/extra.json"] = "{}"
        elif tamper == "packet":
            owner.ov.contents[locators[0]["uri"]] += "\n"
        else:
            owner.ov.contents.pop(locators[0]["uri"])
        calls = len(owner.ov.calls)
        job = await owner._advance_staged_round(job, owner.ov.tasks[job["ov_task_id"]])
        assert job["status"] == "failed" and len(owner.ov.calls) == calls
        ledger = _read(root / "case-batches/refund-review/ledger.json")
        assert all(slot["status"] == "pending" and slot["format_attempts"] == 0 for slot in ledger["slots"])
        assert not list((root / "cases/refund-review").glob("*.json"))

    asyncio.run(check())


def test_unaccepted_directory_with_extra_file_never_posts(tmp_path):
    async def check():
        owner = BatchOwner(tmp_path)
        job = await to_batch(owner)
        root, state = await owner._staged_load(job)
        current = state["current"]
        owner.ov.accepted.pop(current["key"])
        current["task_id"] = ""
        _save(root / "state.json", state)
        owner.ov.contents[current["input_uri"] + "/extra.json"] = "{}"
        job = owner.repo.update_job(job["job_id"], ov_task_id="", status="preparing")
        calls = len(owner.ov.calls)
        job = await owner._resume_staged_round(job)
        assert job["status"] == "failed" and len(owner.ov.calls) == calls
        assert not _read(root / "state.json")["current"]["task_id"]

    asyncio.run(check())


def test_recovered_accepted_task_identity_persisted_before_directory_integrity_failure(tmp_path):
    async def check():
        owner = BatchOwner(tmp_path)
        job = await to_batch(owner)
        root, state = await owner._staged_load(job)
        current = state["current"]
        task_id = current["task_id"]
        current["task_id"] = ""
        _save(root / "state.json", state)
        owner.ov.contents[current["input_uri"] + "/extra.json"] = "{}"
        job = owner.repo.update_job(job["job_id"], ov_task_id="", status="preparing")
        calls = len(owner.ov.calls)
        job = await owner._resume_staged_round(job)
        assert job["status"] == "failed" and job["ov_task_id"] == task_id and len(owner.ov.calls) == calls
        assert _read(root / "state.json")["current"]["task_id"] == task_id
        assert _read(root / "children" / current["child"] / "metadata.json")["task_id"] == task_id

    asyncio.run(check())


@pytest.mark.parametrize("missing", ["directory", "packet"])
def test_frozen_remote_input_404_is_integrity_failure_not_infinite_submission_retry(tmp_path, missing):
    async def check():
        owner = BatchOwner(tmp_path)
        job = await to_batch(owner)
        root, state = await owner._staged_load(job)
        current = state["current"]
        locator = _read(root / current["packet_file"])[0]
        request = owner.ov.request

        async def remote_missing(account, method, path, **kwargs):
            if ((missing == "directory" and path == "/api/v1/fs/ls")
                    or (missing == "packet" and path == "/api/v1/content/read"
                        and kwargs["params"]["uri"] == locator["uri"])):
                raise HTTPException(404, "Deleted frozen input")
            return await request(account, method, path, **kwargs)

        owner.ov.request = remote_missing
        calls = len(owner.ov.calls)
        final = await owner._advance_staged_round(job, owner.ov.tasks[job["ov_task_id"]])
        assert final["status"] == "failed" and "missing" in final["error"]
        assert len(owner.ov.calls) == calls and final["ov_task_id"] == current["task_id"]

    asyncio.run(check())


def test_hidden_native_metadata_files_ignored_and_complete_listing_pagination_checked(tmp_path):
    async def check():
        owner = BatchOwner(tmp_path)
        job = await to_batch(owner)
        root, state = await owner._staged_load(job)
        current = state["current"]
        locators = _read(root / current["packet_file"])
        for name in (".abstract.md", ".overview.md"):
            owner.ov.contents[current["input_uri"] + "/" + name] = "Generated Native metadata"
        await owner._verify_case_batch_input_directory(job, current, locators)
        # Also tolerate legacy ls adapters returning hidden file sidecars, without counting them as Evidence.
        rows = [
            {"uri": current["input_uri"] + f"/.metadata-{index:03d}", "isDir": False} for index in range(128)
        ] + [{"uri": row["uri"], "isDir": False} for row in locators]
        request, offsets = owner.ov.request, []

        async def paged(account, method, path, **kwargs):
            if path == "/api/v1/fs/ls":
                params = kwargs["params"]
                assert params["node_limit"] == 64 and "limit" not in params
                offsets.append(params["offset"])
                return rows[params["offset"]:params["offset"] + params["node_limit"]]
            return await request(account, method, path, **kwargs)

        owner.ov.request = paged
        await owner._verify_case_batch_input_directory(job, current, locators)
        assert offsets == [0, 64, 128]
        owner.ov.request = request
        assert (await drive(owner, job))["status"] == "waiting_human"

    asyncio.run(check())


def test_directory_listing_repeated_full_page_is_rejected(tmp_path):
    async def check():
        owner = BatchOwner(tmp_path)
        job = await to_batch(owner)
        root, state = await owner._staged_load(job)
        current = state["current"]
        rows = [{"uri": current["input_uri"] + f"/.metadata-{index:03d}", "isDir": False} for index in range(64)]
        request, offsets = owner.ov.request, []

        async def repeat_page(account, method, path, **kwargs):
            if path == "/api/v1/fs/ls":
                offsets.append(kwargs["params"]["offset"])
                return rows
            return await request(account, method, path, **kwargs)

        owner.ov.request = repeat_page
        calls = len(owner.ov.calls)
        final = await owner._advance_staged_round(job, owner.ov.tasks[job["ov_task_id"]])
        assert final["status"] == "failed" and "pagination" in final["error"]
        assert offsets == [0, 64] and len(owner.ov.calls) == calls

    asyncio.run(check())


@pytest.mark.parametrize("guard", ["lookup_unavailable", "compiler_changed", "audit_changed"])
def test_legacy_supersession_requires_verified_absence_and_unchanged_authority(tmp_path, guard):
    async def check():
        owner = BatchOwner(tmp_path)
        job, root, state = await legacy_file_batch(owner)
        current = copy.deepcopy(state["current"])
        audit_path = root / "case-batches/superseded" / (current["child"] + ".json")
        if guard == "lookup_unavailable":
            request = owner.ov.request

            async def unavailable(account, method, path, **kwargs):
                if path == "/api/v1/compile/submissions/" + current["key"]:
                    raise HTTPException(503, "Lookup unavailable; acceptance remains unknown")
                return await request(account, method, path, **kwargs)

            owner.ov.request = unavailable
        elif guard == "compiler_changed":
            with (root / "aux-compilers" / (current["compiler_stage"] + ".md")).open("a", encoding="utf-8") as stream:
                stream.write("\nChanged compiler\n")
        else:
            _save(audit_path, {"replacement_child": "tampered"})
        calls = len(owner.ov.calls)
        final = await owner._resume_staged_round(job)
        assert len(owner.ov.calls) == calls
        assert _read(root / "state.json")["current"] == current
        assert final["status"] == ("preparing" if guard == "lookup_unavailable" else "failed")
        if guard == "audit_changed":
            assert _read(audit_path) == {"replacement_child": "tampered"}
        else:
            assert not audit_path.exists()

    asyncio.run(check())
