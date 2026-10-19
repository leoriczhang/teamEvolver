"""Deferred review retains exact rows, complete quote coverage and method/context roles."""

import copy

import pytest

from team_miner import deferred_evidence_review as review
from team_miner import evidence_quote_aliases as aliases
from team_miner import evidence_quote_units as quotes
from team_miner.ov_staged_orchestration import _read, _save, _sha
from team_miner.source_unit_review import quote_parent, validate_positive_support
from team_miner.source_units import load_source_units
from team_miner.tests.test_deferred_evidence_review import candidate
from team_miner.tests.test_source_unit_scope import route_fixture
from team_miner.tests.test_source_units import frozen


def prepared(root):
    _, _, state = route_fixture(root, row_count=10, max_units=4)
    state.update(efficiency_protocol="te-mining-efficient.v2", source_scope_protocol="te-source-scope.v3")
    _save(root / "state.json", state)
    files = {}
    for path in (root / "chunks").glob("*.json"):
        chunk = _read(path)
        files.setdefault(chunk["source_uri"], []).append(chunk["ordinal"])
        _save(root / "chunk-metadata" / path.name, {k: v for k, v in chunk.items() if k != "content"})
    for uri, ordinals in files.items():
        _save(root / "source-files" / (_sha(uri) + ".json"), {"uri": uri, "ordinals": sorted(ordinals)})
    for index in range(10):
        record = _read(root / "catalog/method" / f"{index:08d}.json")
        ids = {identity for ref in record["item"]["source_refs"] for identity in record["source_unit_bindings"][ref]}
        rows = load_source_units(root, list(ids))
        record["item"].update(kind="method", source_files=sorted({row["source_uri"] for row in rows}))
        record.update(leaf_items=[record["item"]["item_id"]],
                      ordinals=sorted({ordinal for row in rows for ordinal in row["ordinals"]}))
        _save(root / "proposals" / f"{index:08d}.json", record)
        if index == 0:
            gap = {"reason": "当前索引尚需按原文确认完整步骤", "source_refs": record["item"]["source_files"]}
            _save(root / "method-decisions" / (record["item"]["item_id"] + ".json"),
                  {"target": "deferred", "name": "", "reason": gap["reason"]})
    plan = _read(root / "plan.json")
    plan["deferred"] = [gap]
    _save(root / "plan.json", plan)
    manifest = review._prepare_review(root, 10)
    return manifest, _read(root / "evidence-review/methods/00000000.json")


def test_shared_transport_and_repeated_headers_do_not_mix_other_customer_rows(tmp_path):
    manifest, descriptor = prepared(tmp_path)
    assert manifest["proposal_indexes"] == [0]
    rows = load_source_units(tmp_path, descriptor["source_unit_ids"])
    operative = [row for row in rows if row["id"] in descriptor["operative_source_unit_ids"]]
    assert len(operative) == 1 and "条件 0" in operative[0]["content"]
    assert not any("条件 1" in row["content"] for row in rows)
    assert {row["kind"] for row in rows} == {"heading", "table_header", "table_row"}
    frozen_manifest = review._quote_manifest(tmp_path, descriptor, 0)
    evidence, controls, after = review._quote_assignment(
        tmp_path, descriptor, frozen_manifest, 0, {"language": "zh-CN"},
    )
    packet = {"stage": quotes.QUOTE_STAGE, "input_ids": [row["id"] for row in evidence],
              "evidence": evidence, "derived_controls": controls}
    encoded, alias_manifest = aliases.build_quote_alias_packet(packet)
    assert set(encoded["derived_controls"]["method_input_ids"]) <= set(encoded["input_ids"])
    assert all(identity.startswith("q") for identity in encoded["derived_controls"]["context_input_ids"])
    aliases.validate_quote_alias_manifest(alias_manifest, packet=encoded, canonical_packet=packet)
    selected = controls["method_input_ids"][0]
    response = {"schema_version": quotes.QUOTE_SCHEMA, "input_ids": packet["input_ids"], "reviews": [{
        "item_id": controls["items"][0]["item_id"], "outcome": "candidate", "reason": "当前完整原文行支持方法",
        "source_refs": controls["allowed_source_refs"], "supporting_quote_ids": [selected],
        "partial_steps": [], "candidate": candidate(),
    }]}
    checked, audit = review.validate_quote_id_review(response, items=controls["items"], evidence=evidence)
    validate_positive_support(descriptor, checked["reviews"][0], audit)
    fragment = {"input_ids": packet["input_ids"], "ordinals": descriptor["ordinals"],
                "review": checked["reviews"][0], "quotation_protocol": quotes.QUOTE_PROTOCOL,
                "source_quote_audit": audit}
    _save(tmp_path / "evidence-review/quote-fragments/00000000/00000000.json", fragment)
    assert review._fragment_source_quotes(tmp_path, fragment)[0]["source_unit_id"] == operative[0]["id"]
    result = review._finish_method(tmp_path, descriptor)
    assert result["outcome"] == "candidate" and result["source_unit_ids"] == descriptor["source_unit_ids"]
    assert after == frozen_manifest["unit_count"]
    forged = copy.deepcopy(descriptor)
    forged["source_unit_ids"] = forged["source_unit_ids"][:-1]
    with pytest.raises(ValueError, match="scope changed"):
        review._quote_assignment(tmp_path, forged, frozen_manifest, 0, {"language": "zh-CN"})
    with pytest.raises(ValueError, match="Context alone"):
        validate_positive_support(descriptor, checked["reviews"][0],
                                  [{"source_unit_id": descriptor["context_source_unit_ids"][0]}])
    incomplete = copy.deepcopy(frozen_manifest)
    incomplete["segments"] = incomplete["segments"][:-1]
    with pytest.raises(ValueError, match="manifest changed"):
        review._quote_assignment(tmp_path, descriptor, incomplete, 0, {"language": "zh-CN"})


def test_exact_row_quotes_cross_transport_chunks_without_leaking_neighbour_text(tmp_path):
    text = "| 客户 | 方法 |\n| --- | --- |\n| 甲 | " + "先征得同意再说明；" * 180 + " |\n| 乙 | 另一客户的金额9999 |\n"
    _, manifest = frozen(tmp_path, [text], max_chars=800)
    rows = load_source_units(tmp_path, list(range(manifest["unit_count"])))
    row = next(row for row in rows if "| 甲 |" in row["content"])
    assert len(row["ordinals"]) > 1
    parent = quote_parent(tmp_path, row["id"])
    pieces = [quotes.source_quote_unit(parent, position) for position in range(quotes.quote_unit_count(parent))]
    assert "".join(piece["content"] for piece in pieces) == row["content"]
    for piece in pieces:
        quotes.validate_source_quote_unit(piece, parent)
        assert piece["source_unit_id"] == row["id"] and "9999" not in piece["content"]


def test_readonly_planning_uses_exact_unit_membership_even_when_notes_share_a_chunk(tmp_path):
    from team_miner.direct_workflow_planning import related_context

    prepared(tmp_path)
    state = _read(tmp_path / "state.json")
    state.update(direct_workflow_protocol="te-direct-workflow-planning.v1",
                 catalog_counts={"method": 10, "fact": 2, "gap": 0})
    _save(tmp_path / "state.json", state)
    for index, text in enumerate(("当前客户的原文条件", "另一客户的原文条件")):
        record = _read(tmp_path / "proposals" / f"{index:08d}.json")
        record["item"] = {**record["item"], "kind": "fact", "text": text}
        _save(tmp_path / "catalog/fact" / f"{index:08d}.json", record)
    methods = [_read(tmp_path / "proposals/00000000.json")]
    result = related_context(tmp_path, state, methods)
    assert [row["text"] for row in result["related_facts"]] == ["当前客户的原文条件"]
