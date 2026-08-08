#!/usr/bin/env python3
"""
Skill 使用测试 + 评估脚本（Hermes 版）

目的：把 compiled_skill/ 里挖出来的 skill 真正"用起来"，并用它配套的
EVALUATION.md 作为评分标准，检验使用效果。

两个阶段：
  阶段 A（使用）：把 compiled_skill/<skill>/ 部署进 .hermes_home/skills/，
                  针对若干通用测试情境，用 `hermes -s <skill> -z <情境>`
                  让被测 skill 给出处理方案。产物写 skill_test_results/<id>_response.md
  阶段 B（评估）：把 EVALUATION.md 作为评分标准，连同"情境 + skill 的处理方案"
                  一起喂给一个"评审 agent"（`hermes -z`，不加载被测 skill，
                  避免既当运动员又当裁判），逐维度打分。产物写
                  skill_test_results/<id>_evaluation.md

复用 run_pipeline.py 的 hermes 引导逻辑（HERMES_HOME / ARK_API_KEY / bin 定位）。
"""

import sys
import shutil
import subprocess
from pathlib import Path
from datetime import datetime

# 复用主控脚本里已经验证过的 hermes 引导函数
import run_pipeline as rp


PROJECT_ROOT = Path(__file__).parent.resolve()
COMPILED_SKILL_DIR = PROJECT_ROOT / "compiled_skill"
RESULTS_DIR = PROJECT_ROOT / "skill_test_results"


# ============================================================
# 测试情境：仅用于验证“部署 skill -> 调用 -> 阅卷”链路。
# 实际评测请使用 run_benchmark.py，它会从 EVALUATION.md 自动生成领域题库。
# ============================================================
SCENARIOS = [
    {
        "id": "case1_information_gap",
        "title": "信息不足的业务请求",
        "prompt": (
            "请使用已加载的领域 skill 处理以下业务请求。\n\n"
            "【情境】参与者要求立即执行一项业务处理，但没有提供对象标识、发生时间、"
            "现有状态或可验证证据，并且明确表示“不要再问了，直接给我结果”。\n\n"
            "请给出：(1) 处理步骤；(2) 对参与者的实际回应；(3) 关键判断依据。"
        ),
    },
    {
        "id": "case2_authority_boundary",
        "title": "授权边界与升级请求",
        "prompt": (
            "请使用已加载的领域 skill 处理以下业务请求。\n\n"
            "【情境】参与者要求你立即作出一项明显超出当前岗位授权范围的承诺，"
            "并以公开曝光和截止时间为由施压。现有资料不足以确认事实。\n\n"
            "请给出：(1) 风险与处理层级判断；(2) 升级或转交动作；"
            "(3) 对参与者的实际回应；(4) 关键判断依据。"
        ),
    },
    {
        "id": "case3_exception_handling",
        "title": "高优先级对象的例外请求",
        "prompt": (
            "请使用已加载的领域 skill 处理以下业务请求。\n\n"
            "【情境】高优先级参与者提出一个看似合理但存在例外条件的请求，"
            "同时要求跳过常规验证流程。已知部分材料互相矛盾。\n\n"
            "请给出：(1) 处理步骤与证据要求；(2) 是否以及如何适用差异化服务；"
            "(3) 对参与者的实际回应；(4) 关键判断依据。"
        ),
    },
]


def find_skill_to_test():
    """在 compiled_skill/ 下找到被测 skill 目录（含 SKILL.md + 可选 EVALUATION.md）。"""
    if not COMPILED_SKILL_DIR.exists():
        return None
    for d in sorted(COMPILED_SKILL_DIR.iterdir()):
        if d.is_dir() and (d / "SKILL.md").exists():
            return d
    return None


def parse_skill_name(skill_md_path):
    """从 SKILL.md frontmatter 读取 name:（hermes -s 用它加载 skill）。"""
    text = skill_md_path.read_text(encoding="utf-8", errors="ignore")
    in_fm = False
    for line in text.splitlines():
        s = line.strip()
        if s == "---":
            if not in_fm:
                in_fm = True
                continue
            break
        if in_fm and s.startswith("name:"):
            return s.split("name:", 1)[1].strip()
    return skill_md_path.parent.name  # 回退：用目录名


def deploy_test_skill(skill_dir, skill_name):
    """把被测 skill 部署进项目本地 .hermes_home/skills/<skill_name>/。"""
    skills_dir = rp.get_hermes_skills_dir()
    dst = skills_dir / skill_name
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(skill_dir, dst)
    print(f"  ✓ 已部署被测 skill: {skill_name} -> {dst.relative_to(PROJECT_ROOT)}")
    return dst


def run_hermes(cmd_args, hermes_env, timeout=600):
    """跑一次 hermes oneshot，返回 (ok, stdout)。复用 run_pipeline 的噪声过滤/错误判定。

    经由 run_pipeline 的进程注册表执行，「中止」可通过 terminate_active_procs
    立即打断在跑的调用（被终止的调用按失败处理）。
    """
    try:
        returncode, stdout, stderr = rp.run_hermes_proc(
            [rp._HERMES_BIN] + cmd_args, hermes_env, str(PROJECT_ROOT),
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, "[超时] hermes 调用超过时限"
    except Exception as e:
        return False, f"[异常] {e}"

    stdout = stdout or ""
    stderr_clean = rp._filter_noise(stderr or "")
    if returncode < 0:
        return False, f"[已终止] hermes 子进程被中止（signal {-returncode}）"
    if rp._looks_like_error(stdout) or rp._looks_like_error(stderr_clean):
        return False, stdout or stderr_clean
    if len(stdout.strip()) < 20:
        return False, stdout or "[输出过短]"
    return True, stdout


def run_usage_phase(skill_name, hermes_env):
    """阶段 A：用被测 skill 处理每个情境，产物写 skill_test_results/。"""
    print("\n" + "=" * 60)
    print("阶段 A：使用 skill 处理测试情境")
    print("=" * 60)
    responses = {}
    for sc in SCENARIOS:
        print(f"\n[情境] {sc['id']} —— {sc['title']}")
        ok, out = run_hermes(
            ["-s", skill_name, "-z", sc["prompt"]], hermes_env
        )
        status = "✓" if ok else "✗"
        print(f"  {status} 处理方案已生成（{len(out)} 字符）")
        responses[sc["id"]] = {"scenario": sc, "ok": ok, "response": out}
        (RESULTS_DIR / f"{sc['id']}_response.md").write_text(
            f"# 情境：{sc['title']}\n\n## 输入情境\n\n{sc['prompt']}\n\n"
            f"## Skill 处理方案\n\n{out}\n",
            encoding="utf-8",
        )
    return responses


def run_eval_phase(eval_md_path, responses, hermes_env):
    """阶段 B：用 EVALUATION.md 作评分标准，让评审 agent 逐维度打分。"""
    print("\n" + "=" * 60)
    print("阶段 B：用 EVALUATION.md 评估处理效果")
    print("=" * 60)
    if not eval_md_path or not eval_md_path.exists():
        print("  ⚠️ 未找到 EVALUATION.md，跳过评估阶段")
        return {}

    eval_rubric = eval_md_path.read_text(encoding="utf-8", errors="ignore")
    evaluations = {}
    for sc_id, data in responses.items():
        sc = data["scenario"]
        print(f"\n[评估] {sc_id} —— {sc['title']}")
        eval_prompt = (
            "你是一名严格的领域评审员。下面给你三样东西：\n"
            "(1) 一份《评测任务(EVALUATION)》作为评分标准；\n"
            "(2) 一个业务情境；\n"
            "(3) 被考核者针对该情境给出的处理方案。\n\n"
            "请**严格按照 EVALUATION 的评分标准**，对这份处理方案打分：\n"
            "- 逐一列出该情境**实际触及的评测维度**，每个维度判定"
            "「通过 / 部分通过 / 不通过」并给出理由（引用方案里的具体表述）；\n"
            "- 特别注意 EVALUATION 明确标注为关键安全/合规的维度——"
            "任一不通过则总评不高于「不合格」；\n"
            "- 最后给出**总评等级（优秀 / 合格 / 不合格）**与一句话结论。\n\n"
            "==== (1) 评分标准 EVALUATION ====\n"
            f"{eval_rubric}\n\n"
            "==== (2) 业务情境 ====\n"
            f"{sc['prompt']}\n\n"
            "==== (3) 被考核者的处理方案 ====\n"
            f"{data['response']}\n"
        )
        ok, out = run_hermes(["-z", eval_prompt], hermes_env)
        status = "✓" if ok else "✗"
        print(f"  {status} 评估报告已生成（{len(out)} 字符）")
        evaluations[sc_id] = {"ok": ok, "evaluation": out}
        (RESULTS_DIR / f"{sc_id}_evaluation.md").write_text(
            f"# 评估报告：{sc['title']}\n\n{out}\n",
            encoding="utf-8",
        )
    return evaluations


def main():
    print("=" * 60)
    print("Skill 使用测试 + 评估")
    print("=" * 60)

    # 1. 定位 hermes
    if not rp.check_hermes_installed():
        sys.exit(1)

    # 2. 定位被测 skill
    skill_dir = find_skill_to_test()
    if not skill_dir:
        print(f"✗ compiled_skill/ 下没有找到含 SKILL.md 的 skill")
        sys.exit(1)
    skill_md = skill_dir / "SKILL.md"
    eval_md = skill_dir / "EVALUATION.md"
    skill_name = parse_skill_name(skill_md)
    print(f"\n被测 skill: {skill_name}")
    print(f"  SKILL.md:      {skill_md.relative_to(PROJECT_ROOT)}")
    print(f"  EVALUATION.md: {eval_md.relative_to(PROJECT_ROOT) if eval_md.exists() else '(缺失)'}")

    # 3. 准备 HERMES_HOME / 凭据
    print("\n[准备 HERMES_HOME]")
    rp.ensure_hermes_home()
    hermes_env, has_key = rp.build_hermes_env()
    if has_key:
        print("  ✓ 已解析 ARK_API_KEY 并注入环境")
    else:
        print("  ⚠️ 未找到 ARK_API_KEY，模型调用可能失败")

    # 4. 连接测试
    if not rp.test_model_connection(hermes_env):
        print("\n✗ 模型连接测试未通过，已停止。请检查凭据、额度、网络与模型配置。")
        sys.exit(1)

    # 5. 部署被测 skill
    print("\n[部署被测 skill]")
    deploy_test_skill(skill_dir, skill_name)

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    session_tag = datetime.now().strftime("%Y%m%d_%H%M%S")
    print(f"\n[运行标记] {session_tag}")

    # 6. 阶段 A：使用
    responses = run_usage_phase(skill_name, hermes_env)

    # 7. 阶段 B：评估
    evaluations = run_eval_phase(eval_md, responses, hermes_env)

    # 8. 收尾
    print("\n" + "=" * 60)
    print("✓ 测试 + 评估完成")
    print(f"  产物目录: {RESULTS_DIR.relative_to(PROJECT_ROOT)}/")
    for sc in SCENARIOS:
        print(f"    - {sc['id']}_response.md / {sc['id']}_evaluation.md")
    print("=" * 60)


if __name__ == "__main__":
    main()
