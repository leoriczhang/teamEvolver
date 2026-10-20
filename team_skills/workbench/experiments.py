"""Prepare unsaved multi-file Skill experiments using fixed library bundles."""
from __future__ import annotations

import copy

from team_replay._util import normalize_artifact_rel_path
from team_replay.contracts import ReplaySpec, ReplayTreatment
from team_replay.datasets.schema import DATASET_SCHEMA, normalize_document

from ..config import BundleLimits
from ..evolution.history import skill_payload
from ..library.bundle import bundle_tree_sha256
from ..library.editor import validate_skill_name
from ..library.repository import validate_bundle


def draft_bundle(name, baseline, markdown, files=None):
    bundle = dict(baseline)
    if not isinstance(markdown, str):
        raise ValueError("candidate_skill_md must be text")
    if files is not None:
        if not isinstance(files, dict) or len(files) > 100:
            raise ValueError("candidate_files must be at most 100 text files")
        total = 0
        for path, content in files.items():
            if not isinstance(path, str) or not isinstance(content, str):
                raise ValueError("candidate_files must map paths to text")
            if "\\" in path or ":" in path or any(part.startswith(".") for part in path.split("/")):
                raise ValueError("unsafe Candidate file path")
            path = normalize_artifact_rel_path(path)
            if path == "SKILL.md":
                raise ValueError("SKILL.md must use candidate_skill_md")
            data = content.encode()
            total += len(data)
            if len(data) > 2 * 1024 * 1024 or total > 16 * 1024 * 1024:
                raise ValueError("Candidate file size exceeded")
            bundle[path] = data
    bundle["SKILL.md"] = markdown.encode()
    return validate_bundle(name, bundle, BundleLimits())


def prepare_spec(*, run_id, skill_id, dataset, bundles, versions, candidate_skill_md, candidate_files=None,
                 runtime=None, options=None, dataset_hash=""):
    """All peer Skills are fixed; only the selected Skill receives draft edits."""
    name = validate_skill_name(skill_id)
    document = normalize_document(dataset)
    ids = [ref["skill_id"] for ref in document["skills"]]
    if name not in ids:
        raise ValueError("selected Skill must be declared in Dataset skills")
    candidate_bundle = draft_bundle(name, bundles[name], candidate_skill_md, candidate_files)
    baseline_members = []
    candidate_members = []
    for peer in ids:
        baseline_members.append(skill_payload(peer, bundles[peer]))
        candidate_members.append(skill_payload(peer, candidate_bundle if peer == name else bundles[peer]))
    def treatment(members):
        return members[0] if len(members) == 1 else {"kind": "skill_set", "skills": members}
    before = treatment(baseline_members)
    after = treatment(candidate_members)
    cases = tuple(copy.deepcopy(case) for case in document["cases"] if name in case["skill_ids"])
    if not cases:
        raise ValueError("Dataset has no Case referencing the selected Skill")
    return ReplaySpec(
        run_id=run_id, subject_kind="skill_set" if len(ids) > 1 else "skill", subject_id=name,
        subject_ids=tuple(ids), dataset_format=DATASET_SCHEMA, cases=cases,
        baseline=ReplayTreatment("skill_set" if len(ids) > 1 else "skill_bundle", str(versions[name]),
                                 before, content_hash=bundle_tree_sha256(bundles[name])),
        candidate=ReplayTreatment("skill_set" if len(ids) > 1 else "skill_bundle", "draft",
                                  after, content_hash=bundle_tree_sha256(candidate_bundle)),
        runtime=runtime or {}, limits=options or {},
        metadata={"dataset_hash": dataset_hash, "skill_versions": versions, "changed_skill_ids": [name]},
    )
