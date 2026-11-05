"""Frozen workflow grouping and metadata renderers using the existing OV file Map."""

from __future__ import annotations

from .compile_strategy import compiler_business_policy, render_single_stage_compiler
from .workflow_metadata import OUTPUT_SCHEMA as METADATA_SCHEMA
from .workflow_metadata import PROTOCOL as METADATA_PROTOCOL
from .workflow_partition import CAPACITY_SCHEMA as PARTITION_CAPACITY_SCHEMA
from .workflow_partition import OUTPUT_SCHEMA as PARTITION_SCHEMA
from .workflow_partition import PROTOCOL as PARTITION_PROTOCOL

PARTITION_STAGE = "workflow_partition"
METADATA_STAGE = "workflow_metadata"

PARTITION_RULES = (
    "当前协议 " + PARTITION_PROTOCOL + "。本阶段只归组方法索引，不生成候选metadata、Skill正文或案例。"
    "读取 inputs[].payload.text 中已给出的packet，不寻找猜测的资源。derived_controls.method_columns按顺序声明"
    "id、task、boundary、scope_id；method_rows每行严格按这些列读取，task/boundary均为完整索引文本。"
    "source_relationships.scope_columns声明id,files,refs列；scope_rows按列保存每个scope_id的完整来源短ID集合。"
    "索引与来源关系不是Evidence；同源、邻近、"
    "相似措辞或同主题不证明方法等价。宿主保留完整原记录、来源URI、范围、SHA与成员谱系。"
    "正常输出严格只有 schema_version, assignment_sha256, rows, reasons；"
    "schema_version='" + PARTITION_SCHEMA + "'，assignment_sha256原样复制derived_controls.assignment_sha256，"
    "禁止计算或猜测。"
    "rows每行严格为[method_id,destination]两个字符串，按derived_controls.disposition_columns读取。第一列必须按"
    "derived_controls.assigned_ids原顺序完整复制，每个方法恰好出现一次，不能遗漏、重复或重排。第二列只能是本包"
    "已提供的完整父方法ID、字符串knowledge或字符串deferred；不创建父ID。独立方法映射自己；选为父方法也必须"
    "显式映射自己，不能靠被子方法引用省略其行；禁止父指向别的父的链或环。知识/暂缓仍各保留该方法自己的行。"
    "例如assigned_ids=[m0001,m0002,m0003,m0004]，前两项是已给m0003完整流程内的步骤而第四项仅组织背景时，"
    "rows=[[m0001,m0003],[m0002,m0003],[m0003,m0003],[m0004,knowledge]]；所有字符串须JSON双引号。"
    "reasons严格为对象：只有多成员父方法ID和knowledge/deferred行的method_id作为键，各一个非空原因；"
    "单独映射自己的独立方法没有reason键，不加knowledge/deferred总类原因或未知键。上述例reasons只有m0003和m0004。"
    "TE从逐方法明确映射恢复成员数组及全部来源，不能指望TE替你补父行或遗漏行。正常结果不含input_ids、groups、"
    "independent、knowledge或deferred顶层数组。所有原因最多derived_controls.limits.reason_max_chars（200字符），遵循"
    "packet.output_language_policy；zh-CN用简体中文。禁止输出模型改写的来源、原文、source_refs或候选业务字段。"
    "比较整个已分配集合，包括后出现的方法，再选完整父方法；不用目标数量，不按文档、标题、行或提取条目拆Skill。"
    "重复变体与包含的子步骤只有在现有完整父方法的目的、终态产出、过程及强制适用边界已经覆盖它们时才能归入父方法。"
    "人物、渠道、示例、数量及内部步骤作为参数或分支；阶段内部的更窄触发条件、填充父流程某字段的中间产出不代表独立目标。"
    "保留子方法为独立项，必须有来源支持的父流程之外调用方式、独立需要的结果及可独立复用的过程。不同完整阶段若有独立"
    "需要的终态结果、调用条件或强制边界，仍须分开；不能因共享旅程、参与者或最终大目标吞并成超级流程。互补的同层步骤"
    "不等价，也不因串联便构成另一个Skill：旅程、课纲或人群模板若只排列已独立支持的阶段，且没有独立的跨阶段协调决策、交接约束"
    "及必要终态结果，归knowledge组织上下文；不以人物或人群参数另建跨阶段候选。来源确实支持上述协调方法时可独立保留。"
    "不能因出现得早选狭窄父方法，不能扩大父方法边界，也不能指望正文生成时修补错误包含关系。组reason说明真实"
    "父方法覆盖关系，不能只写同主题。包含关系不确定时保留已获支持的独立方法；只有该方法完整可执行范围无法支持才暂缓。"
    "静态描述、名称、配置字段、查找事实归knowledge，不得发明维护、校验、决策过程把事实变成方法。运行时未知值仍是输入。"
    "本包索引未包含原文不证明整个来源缺Evidence；不得发明附件缺失、业务事实或冲突解法。所有成员来源通过精确ID保留。"
    "此阶段没有固定24方法或8候选上限。完整输出须同时满足derived_controls.limits.max_output_chars及12000字符。"
    "若完整无损处置无法容纳，只输出容量替代形式：schema_version='" + PARTITION_CAPACITY_SCHEMA
    + "',assignment_sha256,input_ids,reason。复制本包assignment_sha256；input_ids原样包含全部"
    "derived_controls.assigned_ids且各一次；reason非空且最多200字符。不含任何部分rows/reasons或其它处置。容量不足不是"
    "Evidence缺口，不得为容纳输出强行归并或遗漏方法。唯一输出packet.output_path，完成即结束。"
)

METADATA_RULES = (
    "当前协议 " + METADATA_PROTOCOL + "。只为本packet已经归组的一个完整工作流生成一次canonical候选metadata。"
    "derived_controls.parent_id必须是member_ids中的现有方法；method_columns声明id,task,boundary,scope_id列，"
    "method_rows保留全部成员完整task/boundary；scope_id通过source_relationships.scopes映射files/refs短ID。"
    "阅读所有成员后再描述父流程，不能重新归组、遗漏已支持分支、引用兄弟组，或暗中扩大父流程目的及强制边界。"
    "这些索引和来源关系是控制上下文，不是Evidence。读取已提供的inputs[].payload.text，不搜索猜测的附件或schema资源。"
    "正常输出严格只有schema_version,assignment_sha256,candidate；schema_version='" + METADATA_SCHEMA
    + "'，assignment_sha256原样复制derived_controls.assignment_sha256。candidate严格只有"
    "name,task,trigger,inputs,outputs,boundary,rationale七个字段。禁止derived_from、source_refs、member_ids、parent_id、"
    "title、Case ID、正文或Test Dataset；宿主恢复全部成员的完整谱系。name为最多64字符的小写ASCII kebab slug机器标识，"
    "不是展示标题。task/trigger/boundary/rationale各非空且最多400字符；inputs/outputs各为1至16项非空字符串数组，"
    "每项最多240字符。所有人类可读内容遵循packet.output_language_policy；zh-CN须简体中文，包括task及后续生成的"
    "展示标题和小节标题，不能以英文task代替中文标题。机器键、slug、schema、原文直接引述、数值和单位保持原样。"
    "候选须描述来源支持的完整可复用过程、独立调用触发条件、独立需要的终态结果和强制边界；内部动作及情境变体保持"
    "步骤或参数，不再拆候选。不得吞并有不同独立结果的另一完整阶段成为超级流程，不得用成员更多细节为扩大狭窄父方法"
    "找理由。若旅程、课纲或人群模板仅串联独立阶段，没有独立的跨阶段协调决策、交接约束及必要终态结果，应明确deferred，"
    "不得以人物或人群参数额外生成跨阶段Skill；它是组织上下文而非独立方法。真正有来源支持的协调方法仍可保留。"
    "不得把静态事实改写成操作方法。保留适用条件、例外、单位、顺序要求和未解决的原文冲突。金额、期限、"
    "人物和示例值保留为所提供的参数或有明确限定的例子，不能变通用事实；历史陈述保留时点及适用性，个人经验须真实且可选。"
    "不发明业务事实、前提、附件、规则、冲突解法或边界之外的决策。未知运行值放inputs，不当作来源缺口。索引只能组织工作；"
    "后续Skill正文和案例仍需核对原始Evidence，metadata结构通过不代表业务正确。"
    "若索引不能支持独立完整方法，或现有parent无法不靠发明来覆盖组，只能返回明确替代形式："
    "schema_version,assignment_sha256,deferred；schema和SHA不变，deferred严格只有reason，非空且最多400字符，"
    "遵循输出语言。candidate和deferred互斥，不得为避免暂缓而发明父方法。区分方法支持不足与未知运行值；规划包无原文"
    "不证明来源缺Evidence。格式错误或输出容量问题不是业务暂缓理由。完整输出最多derived_controls.limits.max_output_chars"
    "且不得超过12000字符；唯一输出packet.output_path，完成即结束。"
)


def _inherited_policy(base_body: str, stages: tuple[str, ...]) -> str:
    """Keep applicable explicit business policy without copying old output contracts."""
    policy = compiler_business_policy(base_body)
    return "".join(
        "Inherited custom policy for " + stage + ":\n" + policy["stages"][stage] + "\n\n"
        for stage in stages if stage in policy["stages"]
    )


def render_workflow_partition_compiler(base_body: str) -> str:
    """Render a pure method partition, independent of candidate-metadata output limits."""
    inherited = _inherited_policy(base_body, ("merge_index", "merge_batch", "direct_workflow", "plan"))
    from .mining_intent import GUIDANCE
    return render_single_stage_compiler(base_body, PARTITION_STAGE, inherited + PARTITION_RULES + GUIDANCE)


def render_workflow_metadata_compiler(base_body: str) -> str:
    """Render one final workflow's canonical metadata without repeating its lineage."""
    inherited = _inherited_policy(base_body, ("direct_workflow", "plan"))
    from .mining_intent import GUIDANCE
    return render_single_stage_compiler(base_body, METADATA_STAGE, inherited + METADATA_RULES + GUIDANCE)
