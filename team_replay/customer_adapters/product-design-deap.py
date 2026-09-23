"""产品设计智能体（DEAP / product-upclaw SIT）真回放传输。"""
from team_replay.factories import DeapReplayFactory

REPLAY_ADAPTER = {"label": "产品设计 DEAP (product-upclaw)", "enabled": True}


def build_replay_adapter(config):
    return DeapReplayFactory(
        endpoint="http://product-upclaw.intsit.sfcloud.local:1080",
        employee_no="01450373",
    )
