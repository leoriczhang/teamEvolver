from __future__ import annotations
from copy import deepcopy
import unittest
from validator import (ROOT, ContractError, load_json, validate_candidate, validate_shape,
                       visible_for_demo, eval_eq, kleene_all, pack_complete_units)

class ContractTests(unittest.TestCase):
    def setUp(self):
        self.bundle = load_json(ROOT / "examples" / "candidate_bundle.json")
        # Test fixture simulates an independently authenticated registry; not a trust bootstrap.
        self.registry = {e["evidence_id"]: {**deepcopy(e), "tenant": "demo-tenant"} for e in self.bundle["evidence"]}
    def valid(self, bundle=None, registry=None):
        validate_candidate(self.bundle if bundle is None else bundle, expected_tenant="demo-tenant",
                           registry=self.registry if registry is None else registry)
    def invalid(self):
        with self.assertRaises(ContractError): self.valid()
    def test_01_positive_candidate(self): self.valid()
    def test_02_unknown_root_field(self): self.bundle["approved"] = True; self.invalid()
    def test_03_duplicate_entity(self): self.bundle["entities"].append(deepcopy(self.bundle["entities"][0])); self.invalid()
    def test_04_dangling_endpoint(self): self.bundle["assertions"][0]["object"] = "missing"; self.invalid()
    def test_05_wrong_relation_direction(self):
        a = self.bundle["assertions"][0]; a["subject"], a["object"] = a["object"], a["subject"]; self.invalid()
    def test_06_unknown_type(self): self.bundle["entities"][0]["entity_type"] = "Invented"; self.invalid()
    def test_07_unknown_predicate(self): self.bundle["assertions"][0]["predicate"] = "pays_automatically"; self.invalid()
    def test_08_missing_qualifier(self): del self.bundle["assertions"][0]["qualifiers"]["region"]; self.invalid()
    def test_09_empty_proof(self): self.bundle["support_sets"][0]["evidence_ids"] = []; self.invalid()
    def test_10_unresolvable_proof(self): self.bundle["support_sets"][0]["evidence_ids"] = ["invented"]; self.invalid()
    def test_11_unknown_support(self): self.bundle["assertions"][0]["support_set_ids"] = ["missing"]; self.invalid()
    def test_12_duplicate_proof_member(self): self.bundle["support_sets"][0]["evidence_ids"] *= 2; self.invalid()
    def test_13_tenant_mismatch(self): self.bundle["tenant"] = "other"; self.invalid()
    def test_14_schema_not_approved(self): self.bundle["schema_revision"] = "made-up@2"; self.invalid()
    def test_15_source_digest_changed(self): self.bundle["evidence"][0]["source_sha256"] = "c" * 64; self.invalid()
    def test_16_source_version_changed(self): self.bundle["evidence"][0]["source_revision"] = "r2"; self.invalid()
    def test_17_source_revoked_after_extraction(self): self.registry["demo:evidence:1"]["status"] = "revoked"; self.invalid()
    def test_18_fake_system_fact(self): self.bundle["assertions"][0]["epistemic_kind"] = "system_fact"; self.invalid()
    def test_19_forged_authority_label(self):
        self.bundle["assertions"][0]["epistemic_kind"] = "system_fact"
        self.bundle["evidence"][0]["source_kind"] = "system_record"
        self.invalid()
    def test_20_reversed_time(self): self.bundle["assertions"][0]["valid_to"] = "2026-09-17T00:00:00Z"; self.invalid()
    def test_21_missing_timezone(self): self.bundle["assertions"][0]["valid_from"] = "2026-09-18T00:00:00"; self.invalid()
    def test_22_unknown_observation_not_true(self): self.assertIsNone(eval_eq("receipt_method", "recipient", {}))
    def test_23_unverified_observation_not_true(self):
        self.assertIsNone(eval_eq("x", "recipient", {"x": {"value": "recipient", "verified": False, "fresh": True}}))
    def test_24_stale_observation_not_true(self):
        self.assertIsNone(eval_eq("x", "recipient", {"x": {"value": "recipient", "verified": True, "fresh": False}}))
    def test_25_conflict_not_true(self):
        self.assertIsNone(eval_eq("x", "recipient", {"x": {"value": "recipient", "verified": True, "fresh": True, "conflict": True}}))
    def test_26_verified_fresh_comparison(self):
        self.assertTrue(eval_eq("x", "recipient", {"x": {"value": "recipient", "verified": True, "fresh": True}}))
    def test_27_bool_not_integer(self):
        self.assertIsNone(eval_eq("x", 1, {"x": {"value": True, "verified": True, "fresh": True}}))
    def test_28_three_valued_conjunction(self):
        self.assertIsNone(kleene_all([True, None])); self.assertFalse(kleene_all([False, None])); self.assertIsNone(kleene_all([]))
    def test_29_atomic_budget_truncation(self):
        unit = {"assertion_id": "a", "qualifiers": {"exception": "must remain"}, "evidence": ["e"], "token_cost": 20}
        packed, truncated = pack_complete_units([unit], 19)
        self.assertEqual([], packed); self.assertTrue(truncated)
        packed, truncated = pack_complete_units([unit], 20)
        self.assertEqual([unit], packed); self.assertFalse(truncated)
    def test_30_budget_invalid_cost(self):
        with self.assertRaises(ContractError): pack_complete_units([{"token_cost": -1}], 20)
    def can_see(self, **kwargs):
        defaults = {"principal_tenant": "demo-tenant", "entity_grants": {e["entity_id"] for e in self.bundle["entities"]},
                    "evidence_grants": {"demo:evidence:1"}, "current_registry": self.registry}
        defaults.update(kwargs)
        return visible_for_demo(self.bundle, self.bundle["assertions"][0], **defaults)
    def test_31_visibility_positive(self): self.assertTrue(self.can_see())
    def test_32_no_permission_to_proof(self): self.assertFalse(self.can_see(evidence_grants=set()))
    def test_33_hidden_endpoint(self): self.assertFalse(self.can_see(entity_grants={"demo:issue:receipt-dispute"}))
    def test_34_fail_closed_authority_unavailable(self): self.assertFalse(self.can_see(authority_available=False))
    def test_35_revoked_source_not_visible(self):
        self.registry["demo:evidence:1"]["status"] = "revoked"; self.assertFalse(self.can_see())
    def test_36_other_tenant_not_visible(self): self.assertFalse(self.can_see(principal_tenant="other"))
    def test_37_other_example_shapes(self):
        for name in ("context_packet", "publication_grant", "typed_query"):
            validate_shape(name, load_json(ROOT / "examples" / f"{name}.json"))
    def test_38_query_cannot_increase_hops(self):
        q = load_json(ROOT / "examples" / "typed_query.json"); q["limits"]["max_hops"] = 3
        with self.assertRaises(ContractError): validate_shape("typed_query", q)
    def test_39_query_cannot_include_sql(self):
        q = load_json(ROOT / "examples" / "typed_query.json"); q["sql"] = "SELECT * FROM all_data"
        with self.assertRaises(ContractError): validate_shape("typed_query", q)
    def test_40_all_required_proof_members(self):
        e = deepcopy(self.bundle["evidence"][0]); e["evidence_id"] = "demo:evidence:2"
        self.bundle["evidence"].append(e); self.registry[e["evidence_id"]] = {**e, "tenant": "demo-tenant"}
        self.bundle["support_sets"][0]["evidence_ids"].append(e["evidence_id"])
        self.valid(); self.assertFalse(self.can_see())
        self.assertTrue(self.can_see(evidence_grants={"demo:evidence:1", "demo:evidence:2"}))

if __name__ == "__main__": unittest.main()
