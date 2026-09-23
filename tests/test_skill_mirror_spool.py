"""Storage mirroring keeps its durable producer queue after identity migration."""
from team_skills.library.hub import SkillHub
from team_skills.library.mirror import VikingSkillMirror


def test_mirror_can_enqueue_restart_and_deliver(tmp_path):
    hub = SkillHub(backend="local", endpoint="", local_root=str(tmp_path / "remote"))
    spool = tmp_path / "spool"
    mirror = VikingSkillMirror(spool_dir=spool, viking_hub=hub)
    mirror.enqueue_skill("demo", {"SKILL.md": b"# Demo", "reference.txt": b"details"})
    resumed = VikingSkillMirror(spool_dir=spool, viking_hub=hub)
    assert resumed.flush()["acked"] == 1
    assert hub._bucket.get_object(hub._skill_bundle_key("demo", "SKILL.md")).read() == b"# Demo"
    assert resumed.status()["backlog"] == 0
