"""Component transport validation is independent of the mined business domain."""

import copy
import hashlib
import json

import pytest

from team_miner.skill_miner import native_components
from team_miner.skill_miner.native_components import PROTOCOL, assemble_components
from tests.miner.skill_miner.fixtures import build_document

SOURCE = "viking://resources/frozen/operations.md"
OTHER_SOURCE = "viking://resources/frozen/recovery.md"


def write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def package(root, name="service-recovery", *, sources=None):
    sources = sources or [SOURCE]
    candidate = root / "compiled_skill" / name
    component = root / "analysis/components" / name
    candidate.mkdir(parents=True)
    method = {"schema_version": "skill-miner.method.v1", "name": name,
              "source_refs": sources, "task": "恢复中断服务", "trigger": "健康探针失败",
              "inputs": ["运行记录"], "outputs": ["恢复记录"], "boundary": "仅当前服务",
              "rationale": "独立触发与终态", "questions": []}
    write_json(candidate / "METHOD.json", method)
    (candidate / "SKILL.md").write_text("# 服务恢复\n\n先确认影响范围，再按来源执行。\n", encoding="utf-8")
    (candidate / "EVALUATION.md").write_text("# 验证说明\n\n这些产物仍需实际业务复核。\n", encoding="utf-8")
    reference = f"compiled_skill/{name}/references/configuration.md"
    (root / reference).parent.mkdir()
    (root / reference).write_text("# 参数配置\n\n保留原始服务条件。\n", encoding="utf-8")
    dataset = build_document(name, [{"query": f"诊断第 {index + 1} 个已记录故障。",
                                     "source_session_ids": sources} for index in range(12)])
    cases = dataset.pop("cases")
    manifest = {"schema_version": PROTOCOL, "name": name, "dataset": dataset,
                "references": [{"path": reference, "source_refs": sources}], "cases": [],
                "coverage": [{"source_ref": source, "reference_paths": [reference],
                              "case_ids": [case["case_id"] for case in cases],
                              "disposition": "retained", "reason": "保留配置和有来源的场景"} for source in sources]}
    for index, case in enumerate(cases):
        path = f"analysis/components/{name}/cases/case-{index + 1:02}.json"
        write_json(root / path, case)
        manifest["cases"].append({"path": path, "case_id": case["case_id"], "source_refs": sources})
    write_json(component / "manifest.json", manifest)
    return manifest, {**dataset, "cases": cases}


def save_manifest(root, manifest):
    write_json(root / "analysis/components" / manifest["name"] / "manifest.json", manifest)


def snapshot(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def reject_without_writes(root, match, *, sources=None, required=True):
    before = snapshot(root)
    with pytest.raises(ValueError, match=match):
        assemble_components(root, set(sources or [SOURCE]), required=required)
    assert snapshot(root) == before


def test_assembly_preserves_components_exact_data_order_and_repeatability(tmp_path):
    manifest, expected = package(tmp_path)
    # Component delivery order is not a lexical filename order.
    manifest["cases"] = manifest["cases"][::-1]
    expected["cases"] = expected["cases"][::-1]
    save_manifest(tmp_path, manifest)
    originals = snapshot(tmp_path)
    audits = assemble_components(tmp_path, {SOURCE}, required=True)
    target = tmp_path / "compiled_skill/service-recovery/benchmark.json"
    assert json.loads(target.read_text()) == expected
    assert all((tmp_path / path).read_bytes() == raw for path, raw in originals.items())
    assert audits[0]["schema_version"] == PROTOCOL
    assert audits[0]["coverage_is_semantic_proof"] is False
    assert audits[0]["output"]["sha256"] == hashlib.sha256(target.read_bytes()).hexdigest()
    assert {item["path"] for item in audits[0]["inputs"]} == set(originals)
    assert all(item["sha256"] == hashlib.sha256(originals[item["path"]]).hexdigest()
               for item in audits[0]["inputs"])
    assembled = snapshot(tmp_path)
    assert assemble_components(tmp_path, {SOURCE}, required=True) == audits
    assert snapshot(tmp_path) == assembled


def test_existing_identical_dataset_keeps_its_original_bytes(tmp_path):
    _, expected = package(tmp_path)
    target = tmp_path / "compiled_skill/service-recovery/benchmark.json"
    content = json.dumps(expected, ensure_ascii=False, separators=(",", ":")).encode()
    target.write_bytes(content)
    audits = assemble_components(tmp_path, {SOURCE})
    assert target.read_bytes() == content
    assert audits[0]["output"]["sha256"] == hashlib.sha256(content).hexdigest()


def test_canonical_dataset_source_without_session_ids_does_not_invent_them(tmp_path):
    manifest, expected = package(tmp_path)
    manifest["dataset"]["source"] = {"kind": "skill_miner"}
    expected["source"] = {"kind": "skill_miner"}
    save_manifest(tmp_path, manifest)
    assemble_components(tmp_path, {SOURCE}, required=True)
    actual = json.loads((tmp_path / "compiled_skill/service-recovery/benchmark.json").read_text())
    assert actual == expected
    assert actual["source"] == {"kind": "skill_miner"}


def test_conflicting_existing_dataset_is_not_overwritten(tmp_path):
    _, expected = package(tmp_path)
    expected["cases"][0]["query"] = "另一个模型产生的题目不能被覆盖"
    write_json(tmp_path / "compiled_skill/service-recovery/benchmark.json", expected)
    reject_without_writes(tmp_path, "冲突")


def test_validate_all_sibling_packages_before_writing_any_output(tmp_path):
    package(tmp_path, "a-service")
    broken, _ = package(tmp_path, "z-service")
    (tmp_path / broken["cases"][4]["path"]).unlink()
    reject_without_writes(tmp_path, "缺少文件")
    assert not list(tmp_path.rglob("benchmark.json"))
    assert not list(tmp_path.rglob("assembly-audit.json"))


@pytest.mark.parametrize("kind", ["case", "reference", "core"])
def test_missing_component_rejected(tmp_path, kind):
    manifest, _ = package(tmp_path)
    path = {"case": manifest["cases"][0]["path"], "reference": manifest["references"][0]["path"],
            "core": "compiled_skill/service-recovery/EVALUATION.md"}[kind]
    (tmp_path / path).unlink()
    reject_without_writes(tmp_path, "缺少文件")


@pytest.mark.parametrize("path", ["analysis/components/service-recovery/cases/orphan.json",
                                  "analysis/components/service-recovery/extra.md",
                                  "compiled_skill/service-recovery/references/orphan.md"])
def test_undeclared_components_rejected(tmp_path, path):
    package(tmp_path)
    (tmp_path / path).write_text("{}")
    reject_without_writes(tmp_path, "未声明")


@pytest.mark.parametrize("path", ["../outside.json", "/tmp/outside.json",
                                  "analysis/components/service-recovery/cases/../outside.json",
                                  "analysis/components/service-recovery/cases/./case-01.json",
                                  "analysis\\components\\case.json", "file:///tmp/case.json",
                                  "analysis/components/another/cases/case-01.json"])
def test_case_path_escape_or_cross_skill_rejected(tmp_path, path):
    manifest, _ = package(tmp_path)
    manifest["cases"][0]["path"] = path
    save_manifest(tmp_path, manifest)
    reject_without_writes(tmp_path, "路径")


@pytest.mark.parametrize("part", ["file", "parent", "root"])
def test_symlink_component_file_or_directory_rejected(tmp_path, part):
    root = tmp_path / "output"
    root.mkdir()
    manifest, _ = package(root)
    target = root / manifest["cases"][0]["path"]
    outside = tmp_path / "outside.json"
    outside.write_bytes(target.read_bytes())
    if part == "file":
        target.unlink()
        target.symlink_to(outside)
    elif part == "parent":
        directory = target.parent
        moved = tmp_path / "moved-cases"
        directory.rename(moved)
        directory.symlink_to(moved, target_is_directory=True)
    else:
        link = tmp_path / "link"
        link.symlink_to(root, target_is_directory=True)
        root = link
    with pytest.raises(ValueError, match="符号链接"):
        assemble_components(root, {SOURCE}, required=True)
    assert not (tmp_path / "output/compiled_skill/service-recovery/benchmark.json").exists()


@pytest.mark.parametrize("kind", ["case_path", "case_id", "reference", "source"])
def test_duplicate_declarations_rejected(tmp_path, kind):
    manifest, _ = package(tmp_path)
    if kind == "case_path":
        manifest["cases"][1]["path"] = manifest["cases"][0]["path"]
    elif kind == "case_id":
        manifest["cases"][1]["case_id"] = manifest["cases"][0]["case_id"]
    elif kind == "reference":
        manifest["references"].append(copy.deepcopy(manifest["references"][0]))
    else:
        manifest["references"][0]["source_refs"].append(SOURCE)
    save_manifest(tmp_path, manifest)
    reject_without_writes(tmp_path, "重复")


@pytest.mark.parametrize("field,value", [("case_id", "other-case"), ("skill_ids", ["another-method"]),
                                        ("skill_ids", ["service-recovery", "another-method"])])
def test_case_identity_must_match_manifest(tmp_path, field, value):
    manifest, _ = package(tmp_path)
    path = tmp_path / manifest["cases"][0]["path"]
    case = json.loads(path.read_text())
    case[field] = value
    write_json(path, case)
    reject_without_writes(tmp_path, "身份")


@pytest.mark.parametrize("location", ["method", "reference", "case", "provenance", "dataset", "coverage"])
def test_unknown_source_rejected_at_every_declared_surface(tmp_path, location):
    manifest, _ = package(tmp_path)
    if location == "method":
        path = tmp_path / "compiled_skill/service-recovery/METHOD.json"
        record = json.loads(path.read_text())
        record["source_refs"] = [OTHER_SOURCE]
        write_json(path, record)
    elif location in {"reference", "case"}:
        manifest["references" if location == "reference" else "cases"][0]["source_refs"] = [OTHER_SOURCE]
    elif location == "provenance":
        path = tmp_path / manifest["cases"][0]["path"]
        record = json.loads(path.read_text())
        record["provenance"]["session_ids"] = [OTHER_SOURCE]
        write_json(path, record)
    elif location == "dataset":
        manifest["dataset"]["source"]["session_ids"] = [OTHER_SOURCE]
    else:
        manifest["coverage"][0]["source_ref"] = OTHER_SOURCE
    save_manifest(tmp_path, manifest)
    reject_without_writes(tmp_path, "来源")


def test_allowed_source_still_must_belong_to_this_method(tmp_path):
    manifest, _ = package(tmp_path)
    manifest["references"][0]["source_refs"] = [OTHER_SOURCE]
    save_manifest(tmp_path, manifest)
    reject_without_writes(tmp_path, "来源超出 METHOD", sources=[SOURCE, OTHER_SOURCE])


def test_case_provenance_cannot_disagree_with_its_declared_sources(tmp_path):
    manifest, _ = package(tmp_path, sources=[SOURCE, OTHER_SOURCE])
    manifest["cases"][0]["source_refs"] = [SOURCE]
    save_manifest(tmp_path, manifest)
    reject_without_writes(tmp_path, "来源与声明不一致", sources=[SOURCE, OTHER_SOURCE])


@pytest.mark.parametrize("kind", ["missing_source", "unknown_reference", "unknown_case", "empty_reason"])
def test_coverage_checks_accounting_completeness_not_semantics(tmp_path, kind):
    manifest, _ = package(tmp_path, sources=[SOURCE, OTHER_SOURCE])
    if kind == "missing_source":
        manifest["coverage"].pop()
    elif kind == "unknown_reference":
        manifest["coverage"][0]["reference_paths"] = ["compiled_skill/service-recovery/references/absent.md"]
    elif kind == "unknown_case":
        manifest["coverage"][0]["case_ids"] = ["absent-case"]
    else:
        manifest["coverage"][0]["reason"] = ""
    save_manifest(tmp_path, manifest)
    reject_without_writes(tmp_path, "coverage", sources=[SOURCE, OTHER_SOURCE])


def test_coverage_may_explicitly_record_unresolved_content_without_inventing_cases(tmp_path):
    manifest, expected = package(tmp_path)
    manifest["coverage"][0].update(reference_paths=[], case_ids=[], disposition="unresolved",
                                   reason="原文存在冲突，未声称问题已解决")
    save_manifest(tmp_path, manifest)
    audits = assemble_components(tmp_path, {SOURCE})
    assert audits[0]["coverage_is_semantic_proof"] is False
    assert json.loads((tmp_path / "compiled_skill/service-recovery/benchmark.json").read_text()) == expected


@pytest.mark.parametrize("component", ["reference", "case"])
def test_coverage_links_cannot_claim_sources_absent_from_component_declarations(tmp_path, component):
    manifest, _ = package(tmp_path, sources=[SOURCE, OTHER_SOURCE])
    if component == "reference":
        manifest["references"][0]["source_refs"] = [SOURCE]
    else:
        manifest["cases"][0]["source_refs"] = [SOURCE]
        path = tmp_path / manifest["cases"][0]["path"]
        case = json.loads(path.read_text())
        case["provenance"]["session_ids"] = [SOURCE]
        write_json(path, case)
    # The second source still claims this component; neither membership in
    # METHOD nor a legitimate component ID makes that declaration consistent.
    save_manifest(tmp_path, manifest)
    reject_without_writes(tmp_path, "coverage 来源.*不一致", sources=[SOURCE, OTHER_SOURCE])


@pytest.mark.parametrize("kind", ["wrong_schema", "missing_field", "extra_field", "wrong_name", "inline_cases",
                                  "wrong_dataset_skill", "invalid_dataset", "case_count"])
def test_manifest_and_dataset_shape_are_strict(tmp_path, kind):
    manifest, _ = package(tmp_path)
    if kind == "wrong_schema":
        manifest["schema_version"] = "unknown"
    elif kind == "missing_field":
        del manifest["coverage"]
    elif kind == "extra_field":
        manifest["untrusted_extension"] = True
    elif kind == "wrong_name":
        manifest["name"] = "another-method"
    elif kind == "inline_cases":
        manifest["dataset"]["cases"] = []
    elif kind == "wrong_dataset_skill":
        manifest["dataset"]["skills"][0]["skill_id"] = "another-method"
    elif kind == "invalid_dataset":
        manifest["dataset"]["schema_version"] = "invalid"
    else:
        manifest["cases"].pop()
    write_json(tmp_path / "analysis/components/service-recovery/manifest.json", manifest)
    reject_without_writes(tmp_path, "组件")


def test_duplicate_json_keys_rejected_before_assembly(tmp_path):
    package(tmp_path)
    path = tmp_path / "analysis/components/service-recovery/manifest.json"
    raw = path.read_text()
    path.write_text(raw.replace('"name": "service-recovery"', '"name": "wrong", "name": "service-recovery"'))
    reject_without_writes(tmp_path, "字段重复")


def test_size_limits_reject_without_truncation(tmp_path, monkeypatch):
    package(tmp_path)
    monkeypatch.setattr(native_components, "MAX_FILE_BYTES", 32)
    reject_without_writes(tmp_path, "大小上限")


@pytest.mark.parametrize("raw", ['{"value":NaN}', '{"value":Infinity}', '{"a":1,"a":2}', '{"unclosed":'])
def test_reference_json_must_be_valid_without_duplicate_keys(tmp_path, raw):
    manifest, _ = package(tmp_path)
    old = manifest["references"][0]["path"]
    path = old.removesuffix(".md") + ".json"
    (tmp_path / old).unlink()
    (tmp_path / path).write_text(raw)
    manifest["references"][0]["path"] = path
    manifest["coverage"][0]["reference_paths"] = [path]
    save_manifest(tmp_path, manifest)
    reject_without_writes(tmp_path, "JSON")


def test_valid_reference_json_array_is_preserved(tmp_path):
    manifest, _ = package(tmp_path)
    old = manifest["references"][0]["path"]
    path = old.removesuffix(".md") + ".json"
    (tmp_path / old).unlink()
    raw = '[{"id":"config-a","step":"核实状态"}]\n'
    (tmp_path / path).write_text(raw, encoding="utf-8")
    manifest["references"][0]["path"] = path
    manifest["coverage"][0]["reference_paths"] = [path]
    save_manifest(tmp_path, manifest)
    assemble_components(tmp_path, {SOURCE})
    assert (tmp_path / path).read_text(encoding="utf-8") == raw


def test_legacy_without_components_remains_byte_identical(tmp_path):
    candidate = tmp_path / "compiled_skill/legacy"
    candidate.mkdir(parents=True)
    (candidate / "METHOD.json").write_text('{"legacy": true}\n')
    before = snapshot(tmp_path)
    assert assemble_components(tmp_path, {SOURCE}) is None
    assert snapshot(tmp_path) == before
    reject_without_writes(tmp_path, "缺少 manifest")


def test_required_protocol_allows_notes_only(tmp_path):
    notes = tmp_path / "analysis/notes/exclusions.json"
    write_json(notes, {"deferred": [{"source_refs": [SOURCE], "reason": "未发现完整方法"}]})
    before = snapshot(tmp_path)
    assert assemble_components(tmp_path, {SOURCE}, required=True) is None
    assert snapshot(tmp_path) == before


def test_required_protocol_detects_method_without_component_manifest(tmp_path):
    package(tmp_path, "complete")
    legacy = tmp_path / "compiled_skill/omitted"
    legacy.mkdir()
    (legacy / "METHOD.json").write_text("{}")
    reject_without_writes(tmp_path, "每个 METHOD")


def test_existing_audit_cannot_be_silently_replaced(tmp_path):
    package(tmp_path)
    assemble_components(tmp_path, {SOURCE})
    path = tmp_path / "analysis/components/service-recovery/assembly-audit.json"
    payload = json.loads(path.read_text())
    payload["inputs"][0]["sha256"] = "0" * 64
    write_json(path, payload)
    reject_without_writes(tmp_path, "装配审计")
