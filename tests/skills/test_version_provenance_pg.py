"""Origins belong to exact committed versions and candidate bases remain frozen."""
import os

import pytest

from team_skills.candidates.repository import CandidateRepository
from team_skills.config import BundleLimits
from team_skills.evolution.history import candidate_payload
from team_skills.library.repository import SkillLibraryRepository
from tests.support.postgres import _run
from tests.support.skills import _skill_md

pytestmark = [pytest.mark.postgres, pytest.mark.skipif(
    not os.environ.get("TEAMEVOLVER_TEST_PG_DSN"), reason="isolated PostgreSQL required",
)]


def test_each_version_uses_its_own_mutation_not_the_latest_actor(pg_env):
    async def write(conn, tenant):
        lib = SkillLibraryRepository(pg_env.tx)
        for index, source in enumerate(("console", "candidate-review", "workbench", "transfer", "external"), 1):
            await lib.commit_bundle(
                conn, tenant_id=tenant, name="demo", bundle={"SKILL.md": _skill_md("demo", f"{tenant}-{index}")},
                mutation_id=f"mutation-{index}", limits=BundleLimits(), actor="same-admin", source=source,
                metadata={"candidate_id": "te-candidate", "revision": 2} if source == "candidate-review" else {},
            )
        # An unchanged manual write must not relabel an existing version.
        await lib.commit_bundle(
            conn, tenant_id=tenant, name="demo", bundle={"SKILL.md": _skill_md("demo", f"{tenant}-5")},
            mutation_id="unchanged", limits=BundleLimits(), actor="same-admin", source="console",
        )
        history = await lib.list_versions(conn, "demo")
        assert [v["provenance"]["kind"] for v in history] == ["unknown", "manual", "manual", "te", "manual"]
        te = await lib.get_version(conn, "demo", 2)
        assert te["provenance"]["candidate_id"] == "te-candidate"
        assert te["provenance"]["base_version"] == 1
        assert all(v["provenance"]["actor"] == "same-admin" for v in history)
        return history

    _run(pg_env, "alpha", lambda conn: write(conn, "alpha"))
    _run(pg_env, "beta", lambda conn: write(conn, "beta"))
    assert len(_run(pg_env, "alpha", lambda conn: SkillLibraryRepository(pg_env.tx).list_versions(conn, "demo"))) == 5


def test_candidate_list_and_detail_keep_generation_base_after_new_skill_version(pg_env):
    async def run(conn):
        lib, repo = SkillLibraryRepository(pg_env.tx), CandidateRepository(pg_env.tx)
        await lib.commit_bundle(
            conn, tenant_id="alpha", name="demo", bundle={"SKILL.md": _skill_md("demo", "original-base")},
            mutation_id="base", limits=BundleLimits(), source="console",
        )
        item = await repo.create(
            conn, tenant_id="alpha", skill_name="demo", proposed_action="update", base_version=1,
            bundle={"SKILL.md": _skill_md("demo", "generated")}, limits=BundleLimits(),
        )
        await lib.commit_bundle(
            conn, tenant_id="alpha", name="demo", bundle={"SKILL.md": _skill_md("demo", "latest-unrelated")},
            mutation_id="latest", limits=BundleLimits(), source="console",
        )
        item = await repo.revise_content(
            conn, tenant_id="alpha", candidate_id=item["candidate_id"], actor="alice",
            bundle={"SKILL.md": _skill_md("demo", "human-edit")}, limits=BundleLimits(),
        )
        for compact in (True, False):
            payload = await candidate_payload(conn, pg_env.tx, item, compact=compact, include_replay=False)
            assert payload["baseline"] == {"kind": "version", "skill_name": "demo", "version": 1}
            assert payload["base_version"] == 1 and payload["user_modified"]
            if not compact:
                assert "original-base" in payload["current_skill_md"]
                assert "latest-unrelated" not in payload["current_skill_md"]
        new = await repo.create(
            conn, tenant_id="alpha", skill_name="fresh", base_version=0, proposed_action="create",
            bundle={"SKILL.md": _skill_md("fresh")}, limits=BundleLimits(),
        )
        assert (await candidate_payload(conn, pg_env.tx, new, compact=True))["baseline"]["kind"] == "new"
    _run(pg_env, "alpha", run)
