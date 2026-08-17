from __future__ import annotations

import base64
import json

from teamEvolver.dreamcycle.config import (
    DreamCycleConfig,
    LLMConfig,
    LogConfig,
    OpenVikingConfig,
    SchedulerConfig,
)
from teamEvolver.dreamcycle.jobs import ALL_JOBS
from teamEvolver.dreamcycle.react.engine import ReActEngine
from teamEvolver.dreamcycle.react.planner import (
    Task,
    TaskPlan,
    TaskStatus,
)
from teamEvolver.dreamcycle.scheduler import Scheduler
from teamEvolver.dreamcycle.tools.base import (
    Tool,
    ToolRegistry,
    ToolResult,
)
from teamEvolver.dreamcycle.tools.viking import (
    VikingReadTool,
    VikingSearchTool,
)


def _key(account: str, user: str) -> str:
    def encode(value: str) -> str:
        return base64.b64encode(value.encode()).decode().rstrip("=")

    return f"{encode(account)}.{encode(user)}.secret"


class _Response:
    def __init__(self, payload: dict) -> None:
        self.status_code = 200
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self) -> dict:
        return self._payload


class _Client:
    def __init__(self, user: str) -> None:
        self.user = user
        self.calls: list[tuple[str, str, dict]] = []

    def post(self, path: str, **kwargs):
        self.calls.append(("post", path, kwargs))
        return _Response(
            {
                "result": {
                    "memories": [
                        {
                            "uri": (
                                f"viking://user/{self.user}"
                                "/memories/pattern/x.md"
                            ),
                            "abstract": self.user,
                        }
                    ],
                    "resources": [],
                    "skills": [],
                }
            }
        )

    def get(self, path: str, **kwargs):
        self.calls.append(("get", path, kwargs))
        return _Response({"result": f"content-from-{self.user}"})


def test_openviking_config_supports_cloud_and_local_personal_sources() -> None:
    team_key = _key("acct", "team-space")
    alice_key = _key("acct", "alice")
    config = OpenVikingConfig(
        endpoint="http://openviking.test",
        api_key=team_key,
        source_api_keys=[alice_key],
        source_users=["bob"],
    )

    assert config.account == "acct"
    assert config.agent_id == "team-space"
    assert config.source_api_keys == [alice_key]
    assert config.source_users == ["bob"]

    local = OpenVikingConfig(
        endpoint="http://localhost:1933",
        api_key="local-root",
        agent_id="team",
        source_users=["alice", "bob"],
    )
    assert local.agent_id == "team"
    assert local.source_users == ["alice", "bob"]


def test_search_and_read_route_each_personal_source() -> None:
    team = _Client("team-space")
    alice = _Client("alice")
    bob = _Client("bob")
    search = VikingSearchTool(
        team,
        "team-space",
        source_clients=[alice, bob],
    )

    result = search.execute(query="workflow", scope="all", limit=10)

    assert result.success is True
    assert len(team.calls) == len(alice.calls) == len(bob.calls) == 1
    assert alice.calls[0][2]["json"]["target_uri"] == (
        "viking://user/memories/"
    )

    read = VikingReadTool(team, source_clients=[alice])
    read_result = read.execute(
        uri="viking://user/alice/memories/pattern/x.md"
    )
    assert read_result.success is True
    assert read_result.output == "content-from-alice"
    assert alice.calls[-1][0] == "get"


def test_complete_scheduler_runs_all_five_jobs_and_persists_history(
    tmp_path,
    monkeypatch,
) -> None:
    class _Memory:
        action_count = 2

    class _Engine:
        def __init__(self, **_kwargs) -> None:
            self.turn_count = 3
            self.working_memory = _Memory()
            self.successful_writes = 2

        def execute_plan(self, plan):
            for task in plan.tasks:
                task.status = TaskStatus.COMPLETED
                task.result = "completed by test engine"
            return plan

    monkeypatch.setattr(
        "teamEvolver.dreamcycle.scheduler.ReActEngine",
        _Engine,
    )
    config = DreamCycleConfig(
        viking=OpenVikingConfig(
            endpoint="http://openviking.test",
            api_key=_key("acct", "team"),
        ),
        llm=LLMConfig(
            base_url="http://llm.test/v1",
            api_key="llm-key",
            model="model-a",
        ),
        scheduler=SchedulerConfig(),
        log=LogConfig(
            log_dir=tmp_path / "logs",
            report_dir=tmp_path / "reports",
            state_file=tmp_path / "state.json",
        ),
    )
    scheduler = Scheduler(
        config,
        ALL_JOBS,
        register_signals=False,
    )

    results = scheduler.run_once()

    assert [result.job_name for result in results] == [
        "team_overview",
        "deduplication",
        "cleanup",
        "onboarding_check",
        "consolidate",
    ]
    assert all(result.status.value == "completed" for result in results)
    assert all(result.turns_used == 3 for result in results)
    assert scheduler.status()["total_cycles"] == 1
    assert len(scheduler.status()["history"]) == 1


def test_scheduler_applies_runtime_override_to_one_job(
    tmp_path,
    monkeypatch,
) -> None:
    observed: list[tuple[str, float, int, int, int]] = []

    class _Memory:
        action_count = 0

    class _Engine:
        def __init__(self, *, config, system_prompt, **_kwargs) -> None:
            observed.append(
                (
                    config.llm.model,
                    config.llm.temperature,
                    config.llm.max_tokens,
                    config.scheduler.max_turns_per_job,
                    config.scheduler.max_consecutive_errors,
                )
            )
            self.system_prompt = system_prompt
            self.turn_count = 1
            self.working_memory = _Memory()

        def execute_plan(self, plan):
            for task in plan.tasks:
                task.status = TaskStatus.COMPLETED
            return plan

    monkeypatch.setattr(
        "teamEvolver.dreamcycle.scheduler.ReActEngine",
        _Engine,
    )
    config = DreamCycleConfig(
        viking=OpenVikingConfig(
            endpoint="http://openviking.test",
            api_key=_key("acct", "team"),
        ),
        llm=LLMConfig(
            base_url="http://llm.test/v1",
            api_key="llm-key",
            model="global-model",
            max_tokens=4096,
            temperature=0.3,
        ),
        scheduler=SchedulerConfig(
            max_turns_per_job=25,
            max_consecutive_errors=3,
        ),
        log=LogConfig(
            log_dir=tmp_path / "logs",
            report_dir=tmp_path / "reports",
            state_file=tmp_path / "state.json",
        ),
        job_settings={
            "team_overview": {
                "model": "overview-model",
                "temperature": 0.1,
                "max_tokens": 16000,
                "max_turns": 18,
                "max_errors": 2,
            }
        },
    )
    scheduler = Scheduler(
        config,
        ALL_JOBS[:2],
        register_signals=False,
    )

    scheduler.run_once()

    assert observed[0] == (
        "overview-model",
        0.1,
        16000,
        18,
        2,
    )
    assert observed[1] == (
        "global-model",
        0.3,
        4096,
        25,
        3,
    )


def test_full_supervisor_allows_disabling_every_job(tmp_path) -> None:
    from teamEvolver.config import TeamEvolverConfig
    from teamEvolver.integrations.dreamcycle_runtime import (
        FullDreamCycleSupervisor,
    )

    supervisor = FullDreamCycleSupervisor(
        TeamEvolverConfig(
            sharing_viking_endpoint="http://openviking.test",
            sharing_viking_team_api_key=_key("acct", "team"),
            llm_api_key="llm-key",
            llm_model_id="model-a",
            dreamcycle_enabled_jobs=[],
            dreamcycle_state_dir=str(tmp_path),
        )
    )

    assert supervisor._scheduler._job_classes == []
    assert all(
        not job["enabled"]
        for job in supervisor.dry_run()["jobs"]
    )


def test_scheduler_preserves_midnight_end_hour(tmp_path) -> None:
    from teamEvolver.config import TeamEvolverConfig
    from teamEvolver.integrations.dreamcycle_runtime import (
        FullDreamCycleSupervisor,
    )

    supervisor = FullDreamCycleSupervisor(
        TeamEvolverConfig(
            sharing_viking_endpoint="http://openviking.test",
            sharing_viking_team_api_key=_key("acct", "team"),
            llm_api_key="llm-key",
            llm_model_id="model-a",
            dreamcycle_active_start_hour=22,
            dreamcycle_active_end_hour=0,
            dreamcycle_state_dir=str(tmp_path),
        )
    )

    scheduler = supervisor._scheduler._config.scheduler
    assert scheduler.active_start_hour == 22
    assert scheduler.active_end_hour == 0


def test_react_loop_executes_tool_call_and_completes_plan(tmp_path) -> None:
    class EchoTool(Tool):
        @property
        def name(self) -> str:
            return "echo"

        @property
        def description(self) -> str:
            return "Echo one value."

        @property
        def parameters_schema(self) -> dict:
            return {
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
            }

        def execute(self, **kwargs) -> ToolResult:
            return ToolResult(True, f"echo:{kwargs['value']}")

    class Response:
        def __init__(self, payload: dict) -> None:
            self._payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return self._payload

    class Client:
        def __init__(self) -> None:
            self.calls = 0

        def post(self, _path: str, **_kwargs) -> Response:
            self.calls += 1
            if self.calls == 1:
                return Response(
                    {
                        "choices": [
                            {
                                "finish_reason": "tool_calls",
                                "message": {
                                    "role": "assistant",
                                    "content": None,
                                    "tool_calls": [
                                        {
                                            "id": "call-1",
                                            "type": "function",
                                            "function": {
                                                "name": "echo",
                                                "arguments": '{"value":"ok"}',
                                            },
                                        }
                                    ],
                                },
                            }
                        ]
                    }
                )
            return Response(
                {
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {
                                "role": "assistant",
                                "content": "PLAN COMPLETE: echo verified",
                            },
                        }
                    ]
                }
            )

    config = DreamCycleConfig(
        viking=OpenVikingConfig(
            endpoint="http://openviking.test",
            api_key=_key("acct", "team"),
        ),
        llm=LLMConfig(
            base_url="http://llm.test/v1",
            api_key="llm-key",
            model="model-a",
        ),
        scheduler=SchedulerConfig(max_turns_per_job=5),
        log=LogConfig(
            log_dir=tmp_path / "logs",
            report_dir=tmp_path / "reports",
            state_file=tmp_path / "state.json",
        ),
    )
    tools = ToolRegistry()
    tools.register(EchoTool())
    engine = ReActEngine(config, tools, "test prompt")
    client = Client()

    def fake_call_llm():
        response = client.post("")
        return response.json()

    engine._call_llm = fake_call_llm
    plan = TaskPlan("react-test", [Task("t1", "Echo a value")])

    result = engine.execute_plan(plan)

    assert result.is_complete is True
    assert result.tasks[0].status == TaskStatus.COMPLETED
    assert engine.turn_count == 3
    assert engine.working_memory.action_count == 1
