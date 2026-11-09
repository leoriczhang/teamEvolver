"""Prompt-specific adapter for the shared Skill Candidate editing loop.

The editor can diagnose and patch frozen business text. It cannot execute the
business Agent, see hidden Replay Checks, or make a publication decision.
"""
from __future__ import annotations

import copy

from team_skills.evolution.agent.loop import LoopPlanResult
from team_skills.evolution.kernel.enums import DecisionAction
from team_skills.execution.editor_tools import PromptEditorTools, canonical, exact_refs, sha
from team_skills.library.repository import SkillLibraryError
from teamEvolver.evidence_transport import TRANSPORT_INSTRUCTIONS, encode_evidence

EDITOR_PROTOCOL = """
本轮使用与 Skill 共用的多轮候选编辑循环，不得绕过精确工具直接提交全文。
允许读取后对已选择组件做whole-component exact replace结构精炼；变量、metadata、全部唯一业务条件、
精确编辑和提交仍须校验；业务效果由后续True Replay验证，不能无依据重做任务。
每轮只返回一个完整 JSON 对象，使用下述 type 字段；不要返回 DSML/XML、原生 function-call 标记或 JSON 以外的说明。
本轮允许修改的只是 execution_plan 中已登记且可编辑的业务组件正文。
先提出有证据的 plan，再用工具查证当前正文、实施精确局部编辑、检查差异与业务约束，最后提交 final。
原始 Evidence 已完整提供；工具索引和搜索预览不替代原文。读取分页时必须查看完整长度、hash和has_more，
不得把一页称为完整证据。Context-only可作对照但不能引用为新增缺陷或触发来源。
按需查证优先使用具体Trace引用和JSON路径，避免重复展开整个大Session；需要时使用明确标记的分页。
existing_guidance可填空字符串，表示本计划未提供逐字引文，不表示原文没有规则。
可用非引用的guidance_summary或rationale描述待核假设，不必在计划阶段复读大段原文。
提供非空existing_guidance时仍必须是当前/基线原文连续逐字文本，不得总结、拼接或用省略号冒充引文。
合法计划的组件引用累计保留；后续计划可补充或调整诊断。历史诊断仅是先前接受的假设，不是真值。
无效新计划只拒绝该次提交，先前合法计划与暂存编辑不失效；每次patch仍须该组件已接受计划支持其全部引用。
先区分不可变的外部业务契约与作为演化对象的实施策略。原始纠正可支持实施策略的有意修订，
plan应说明改哪条策略、原始Evidence如何支持、保留哪些硬要求，以及替代解释和待Replay验证的假设。
语气、生成/自检方法、重复指引及内部默认优先级的适用性不是天然不可改；不能因此自动认定它们有误。
机器评分即使记在user消息里也不是owner授权或黄金答案，不能据它放松外部业务契约。
正确但同义重复的表达、示例和自检也可合并精炼，不要求逐字保留冗余文本；原条件、例外、硬事实与有效能力含义不能丢。
只做有证据或待验证效率假设的局部编辑，短正文不等于效果更好，不以字符上限代替Replay。
业务 Prompt 缺陷与用户偏好、单次任务、Agent运行时、证据不足分别分类；后四者不能硬改为共享规则。
plan 示例：{"type":"plan","action_candidate":"change_execution","rationale":"缺陷与替代解释",
"evidence_classification":{"business_prompt":[{"component_id":"已有组件ID","problem":"具体缺陷或语义保持精炼目标",
"existing_guidance":"原文片段或空字符串","proposed_change":"局部修改及预期/失败条件",
"evidence_refs":["allowed_evidence_refs中的完整引用"]}],"user_memory":[],"task_requirement":[],
"agent_runtime":[],"insufficient_evidence":[]}}。
证据不足时 plan 的 action_candidate 为 skip。不要套用 Skill 的命名、description、文件包或team_skill分类。
工具调用：{"type":"tool_calls","calls":[{"tool":"工具名","args":{...}}]}。
可用工具：
- read_component：{"component_id":"...","version":"current"或"baseline"}，完整读取当前编辑基准或只读发布对照。
- read_evidence：{"ref":"精确目标引用"}，返回完整原字段。Context使用 context:example_id，只读不入引用白名单。
  可选path为JSON路径数组；可选offset/limit为显式字符分页，未返回部分不得当作已读取。
- search_evidence：{"query":"文本","offset":0,"limit":20}，只返回索引预览；用read_evidence查证原文。
- propose_component_edits：{"component_id":"...","reason":"证据、缺陷及保留要求",
  "evidence_refs":["精确目标引用"],"edits":[{"operation":"replace","old_string":"准确原文",
  "new_string":"替换文本"}]}。replace的new_string可空，表示删除；也支持insert_after的anchor/new_string。
  insert_after不会自动补换行；需要换行时在new_string中明确写入换行符。
  先read_component的current全文，再逐字复制唯一锚点。整批编辑成功才暂存，出错根据Observation修正。
  有依据时可replace整段或章节以合并规则、示例和自检；局部指确定锚点范围，不限制只改几个字。
  可删合并重复表达，不能丢独特条件、事实、权限或消息列表含义；仍经机械校验与后续Replay，不设字数门槛。
- replace_component_content：{"component_id":"...","content":"完整新正文","expected_content_hash":"当前正文SHA256",
  "evidence_refs":["精确目标引用"],"reason":"去冗余/策略合并依据及保留要求"}。
  仅在整体精炼理由充分时使用，不要求每轮全文重写。须先read_component current且组件可写；
  expected_content_hash用该读取返回的content_sha256，或自己之后成功patch返回的当前content_sha256，
  不能用editor_state.staged_hash（整个组合的hash）。服务端用当前原文展开为同一exact replace，
  仍走原计划/引用/变量/原子性校验；所有唯一要求仍须覆盖，不能通过final直接提交正文。
- inspect_changes：{}，查看相对working的累计差异，以及published/working/staged的字数、UTF-8字节数和两组delta。
  尺寸是未替换模板变量的原文测量，不是Token；检查继承草稿相对published的净增量，不只看本轮变化。
- validate_changes：{}，机械校验编辑范围、引用、模板变量及暂存组合的一致性。
  报告问题就修正后再次校验；不调用业务规则审查模型。
保持外部业务事实、角色、权限、只读变量、任务边界、首次/再次核心状态、列表基本含义与时间范围。
不得新增业务触发前提或用当前上下文覆盖列表约束；外部指令和授权层级不可改。
可对有原始Evidence支持的实施策略、语气和内部优先级适用性冲突作局部修订，不要求旧策略语义等价。
有意改变策略时须对照原文、原始纠正及反例说明理由；不能只凭自述认定改动正确。
原始Context-only对照不能冒充新target或原始纠正，实际修改仍须原target支持。
固定业务数字不等于需要泛化的实例。
final不能携带完整组件正文或component_changes；正文只能来自成功暂存的局部编辑。
提交：{"type":"final","decision":{"action":"change_execution","rationale":"已修改内容、保留要求和仍待验证假设"}}。
轮次耗尽时，仅已成功暂存并通过同一提交校验的修改可保留为候选；无有效修改则skip。
不修改则提交action=skip及原因。工具机械校验不等于业务效果已改善；后续仍走原True Replay。
"""


class PromptEditorAdapter:
    """A body-only Plan/Act adapter; the shared loop owns model calls and rounds."""

    def __init__(self, payload, *, max_tool_calls_per_round=8, editable_component_ids=None):
        self.tools_state = PromptEditorTools(
            payload, max_tool_calls_per_round=max_tool_calls_per_round,
            editable_component_ids=editable_component_ids,
        )
        self.plan_record = None
        self.last_error = None
        self.plan_round = None

    def _validate_diagnoses(self, plan):
        if not isinstance(plan, dict):
            raise SkillLibraryError("plan must be an object")
        if plan.get("action_candidate") != "change_execution":
            raise SkillLibraryError("plan action_candidate must be change_execution or skip")
        if not isinstance(plan.get("rationale"), str) or not plan["rationale"].strip():
            raise SkillLibraryError("plan requires a diagnosis rationale")
        classification = plan.get("evidence_classification")
        if not isinstance(classification, dict):
            raise SkillLibraryError("plan requires Prompt-specific evidence_classification")
        claims = classification.get("business_prompt")
        if not isinstance(claims, list) or not claims or len(claims) > 32:
            raise SkillLibraryError(
                "a shared business_prompt improvement rationale is required; team_skill is not a substitute",
            )
        references, diagnoses = {}, []
        expanded = []
        for claim in claims:
            if isinstance(claim, dict) and "variable_group_id" in claim:
                group = self.tools_state.variable_groups.get(claim["variable_group_id"])
                members = claim.get("members")
                if group is None or not isinstance(members, list) or not members:
                    raise SkillLibraryError("variable-group diagnosis requires known group and explicit members")
                allowed = {member["component_id"] for member in group["members"]}
                ids = [member.get("component_id") for member in members if isinstance(member, dict)]
                if len(ids) != len(members) or len(set(ids)) != len(ids) or not set(ids) <= allowed:
                    raise SkillLibraryError("diagnosis members must be distinct members of the selected variable group")
                for member in members:
                    if set(member) - {"component_id", "existing_guidance", "guidance_summary", "evidence_refs"}:
                        raise SkillLibraryError("group member diagnosis only selects component text and Evidence")
                    expanded.append({**{k: v for k, v in claim.items() if k != "members"}, **member})
            else:
                expanded.append(claim)
        for claim in expanded:
            if not isinstance(claim, dict):
                raise SkillLibraryError("business_prompt diagnoses must be objects")
            cid = claim.get("component_id")
            if cid not in self.tools_state.editable:
                raise SkillLibraryError("diagnosis component is outside the frozen editable scope")
            for key in ("problem", "proposed_change"):
                if not isinstance(claim.get(key), str) or not claim[key].strip():
                    raise SkillLibraryError("each diagnosis requires problem and proposed_change")
            refs = exact_refs(claim.get("evidence_refs"), self.tools_state.allowed_refs)
            if (self.tools_state.components[cid].get("binding") or {}).get("inline_variable"):
                self.tools_state.component_target_refs(cid, refs)
            guidance = claim.get("existing_guidance")
            if not isinstance(guidance, str):
                raise SkillLibraryError("existing_guidance must quote original text or explicitly be empty")
            if guidance and not any(guidance in self.tools_state._component(cid, version=version)["content"]
                                    for version in ("current", "baseline")):
                raise SkillLibraryError("existing_guidance is not an exact recorded component quote")
            if "guidance_summary" in claim and not isinstance(claim["guidance_summary"], str):
                raise SkillLibraryError("guidance_summary must be non-quoted hypothesis text")
            references.setdefault(cid, set()).update(refs)
            diagnoses.append(copy.deepcopy(claim))
        return references, diagnoses

    def plan(self, plan, *, round_no, max_rounds):
        if isinstance(plan, dict) and plan.get("action_candidate") == DecisionAction.SKIP:
            return LoopPlanResult(done=True, result={"action": DecisionAction.SKIP,
                                                    "rationale": str(plan.get("rationale") or "evidence_insufficient")},
                                  log_kind="plan_skip")
        try:
            refs, diagnoses = self._validate_diagnoses(plan)
        except (SkillLibraryError, TypeError, ValueError) as exc:
            self.last_error = str(exc)
            return LoopPlanResult(message=self.observe(
                [{"tool": "(plan)", "ok": False, "error": str(exc), "accepted_state_preserved": True}],
                round_no=round_no, max_rounds=max_rounds, must_submit=round_no + 1 >= max_rounds,
            ), log_kind="plan_rejected", log_fields={"error": str(exc)})
        self.plan_record, self.plan_round = copy.deepcopy(plan), round_no
        plan_id = self.tools_state.accept_plan(plan, refs, diagnoses, round_no=round_no)
        self.last_error = None
        return LoopPlanResult(message=self.observe(
            [{"tool": "(plan)", "ok": True, "message": "诊断假设已记录；本组件此前已接受的引用和编辑依据仍保留。",
              "accepted_plan_id": plan_id, "planned_component_ids": sorted(self.tools_state.plan_claims),
              "current_component_ids": sorted(refs)}], round_no=round_no, max_rounds=max_rounds,
        ))

    async def tools(self, calls):
        return await self.tools_state.execute(calls)

    def finalize(self, decision):
        if not isinstance(decision, dict):
            return None, ["final decision must be an object"]
        if set(decision) - {"type", "action", "rationale"}:
            return None, ["final cannot supply component bodies, changes or metadata; use staged exact-edit tools"]
        action = decision.get("action")
        if action == DecisionAction.SKIP:
            return {"action": DecisionAction.SKIP, "rationale": str(decision.get("rationale") or "no_valid_edit")}, []
        if action != "change_execution":
            return None, ["final action must be change_execution or skip"]
        if not self.plan_record or not self.tools_state.plan_claims:
            return None, ["an accepted business-Prompt diagnosis plan is required"]
        if not isinstance(decision.get("rationale"), str) or not decision["rationale"].strip():
            return None, ["final requires a rationale grounded in the selected Evidence"]
        try:
            self.tools_state.mechanical_validation()
        except (SkillLibraryError, TypeError, ValueError) as exc:
            return None, [str(exc)]
        return {"action": "change_execution", "rationale": decision["rationale"],
                "component_changes": self.tools_state.changes()}, []

    def observe(self, observations, *, round_no, max_rounds, must_submit=False):
        value = {"editor_state": {
            "round": round_no, "max_rounds": max_rounds, "stage": "evolve_prompt",
            "planned_component_ids": sorted(self.tools_state.plan_claims),
            "components_read": sorted(self.tools_state.read_current),
            "applied_edits": self.tools_state.applied_edits,
            "staged_hash": sha(self.tools_state.working),
        }, "observations": observations}
        if self.tools_state.editable != set(self.tools_state.components):
            value["editor_state"]["editable_component_ids"] = sorted(self.tools_state.editable)
        if must_submit:
            value["round_limit_notice"] = "轮次即将耗尽。仅提交已暂存且通过校验的修改；否则明确skip，不得伪造成功。"
        raw = canonical(value)
        if len(raw.encode()) > 65536:
            body, packed = encode_evidence(value, max_bytes=65536)
            if packed:
                return TRANSPORT_INSTRUCTIONS + body
        return raw

    def fallback(self):
        # Like Skill best-effort, preserve real staged work as a Candidate only.
        if self.tools_state.applied_edits > 0:
            result, errors = self.finalize({
                "action": "change_execution",
                "rationale": "编辑轮次耗尽；提交已暂存且通过机械校验的修改，仍待 True Replay 验证。",
            })
            if result is not None and not errors:
                return result
        return {"action": DecisionAction.SKIP, "rationale": "prompt_editor_round_limit_without_valid_final"}

    def evidence_classification(self):
        value = copy.deepcopy((self.plan_record or {}).get("evidence_classification") or {})
        value["business_prompt"] = self.tools_state.planned_diagnoses()
        return value

    def audit(self):
        return {"schema_version": "te.prompt-editor-attempt.v1", "plan_round": self.plan_round,
                "planned_component_ids": sorted(self.tools_state.plan_claims),
                "applied_edits": self.tools_state.applied_edits,
                "accepted_plan_history": copy.deepcopy(self.tools_state.plan_history),
                "edit_provenance": copy.deepcopy(self.tools_state.edit_provenance),
                "evidence_reads": copy.deepcopy(self.tools_state.evidence_reads),
                "staged_hash": sha(self.tools_state.working),
                "mechanical_only": True, "available_tools": list(self.tools_state.available_tools)}
