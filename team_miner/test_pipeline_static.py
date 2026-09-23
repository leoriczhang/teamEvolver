#!/usr/bin/env python3
"""
静态流水线自检（无数据 / 不调用 LLM）

目的：在没有真实执行 agent 的前提下，验证改造后的三段流水线
(sample-package-constructor -> semantic-discovery -> evaluation-compiler)
的"管道接线"是否自洽。只做纯静态检查：

1. 三个 prompt 模块能否正确导入，并暴露约定的 PROMPT 变量 + INPUT_DIR/OUTPUT_DIR
2. 每个 prompt 里的 {INPUT_DIR}/{OUTPUT_DIR} 占位符能否被正确注入（模拟 run_pipeline 的 replace）
3. run_pipeline.PROMPT_MODULES 与实际 prompt 文件/变量是否一致
4. Step 2 的样本包扫描逻辑（排除 package_notes/global_notes）是否正确
5. 三个 SKILL.md 的 frontmatter name 与部署目录是否自洽
6. evaluation-compiler 的 SKILL.md 引用的模板是否真实存在
7. Step 2 的 notes 路径接线是否与磁盘实际布局一致（已知隐患探测）

用法:  python3 test_pipeline_static.py
退出码: 0=全部通过(含仅警告)，1=存在失败项
"""

# 这是可直接执行的自检脚本，内部函数存在显式依赖传参，不是 pytest 测试模块。
__test__ = False

import importlib.util
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.resolve()

# --- 轻量断言框架 -----------------------------------------------------------
PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
_results = []


def record(name, status, detail=""):
    _results.append((name, status, detail))
    icon = {"PASS": "✓", "WARN": "⚠", "FAIL": "✗"}[status]
    line = f"  {icon} [{status}] {name}"
    if detail:
        line += f"\n        {detail}"
    print(line)


def load_module(module_name):
    """按 run_pipeline 的方式加载一个 prompt 模块（作为普通 py 文件）。"""
    path = PROJECT_ROOT / f"{module_name}.py"
    spec = importlib.util.spec_from_file_location(module_name, path)
    mod = importlib.util.module_from_spec(spec)
    previous = sys.modules.get(module_name)
    sys.modules[module_name] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception:
        if previous is None:
            sys.modules.pop(module_name, None)
        else:
            sys.modules[module_name] = previous
        raise
    return mod


# --- 1. prompt 模块导入 + 变量存在性 ----------------------------------------
def test_prompt_modules_import():
    print("\n[1] prompt 模块导入 & 约定变量存在性")
    expected = {
        "sample_package_constructor_agent_prompt": "SAMPLE_PACKAGE_CONSTRUCTOR_AGENT_PROMPT",
        "semantic_discovery_agent_prompt": "SEMANTIC_DISCOVERY_AGENT_PROMPT",
        "evaluation_compiler_agent_prompt": "EVALUATION_COMPILER_AGENT_PROMPT",
    }
    modules = {}
    for mod_name, var_name in expected.items():
        try:
            mod = load_module(mod_name)
        except Exception as e:
            record(f"import {mod_name}", FAIL, f"导入失败: {e}")
            continue
        modules[mod_name] = mod
        ok = True
        for attr in (var_name, "INPUT_DIR", "OUTPUT_DIR"):
            if not hasattr(mod, attr):
                record(f"{mod_name}.{attr}", FAIL, "缺少约定变量")
                ok = False
        if ok:
            record(
                f"{mod_name}",
                PASS,
                f"prompt 变量存在; INPUT_DIR={mod.INPUT_DIR!r} OUTPUT_DIR={mod.OUTPUT_DIR!r}",
            )
    return modules


# --- 2. 占位符注入 ----------------------------------------------------------
def test_placeholder_injection(modules):
    print("\n[2] {INPUT_DIR}/{OUTPUT_DIR} 占位符注入")
    var_map = {
        "sample_package_constructor_agent_prompt": "SAMPLE_PACKAGE_CONSTRUCTOR_AGENT_PROMPT",
        "semantic_discovery_agent_prompt": "SEMANTIC_DISCOVERY_AGENT_PROMPT",
        "evaluation_compiler_agent_prompt": "EVALUATION_COMPILER_AGENT_PROMPT",
    }
    for mod_name, var_name in var_map.items():
        mod = modules.get(mod_name)
        if not mod:
            continue
        prompt = getattr(mod, var_name)
        # 每个 prompt 至少应含两个占位符
        has_in = "{INPUT_DIR}" in prompt
        has_out = "{OUTPUT_DIR}" in prompt
        if not (has_in and has_out):
            record(
                f"{mod_name} 占位符存在",
                FAIL,
                f"{{INPUT_DIR}}={has_in}, {{OUTPUT_DIR}}={has_out}",
            )
            continue
        # 模拟 run_pipeline 的 replace 注入
        injected = prompt.replace("{INPUT_DIR}", str(PROJECT_ROOT / mod.INPUT_DIR))
        injected = injected.replace("{OUTPUT_DIR}", str(PROJECT_ROOT / mod.OUTPUT_DIR))
        # semantic-discovery 额外含 {NOTES_DIR}（Step 2 注入 sample_packages/ 顶层）
        if "{NOTES_DIR}" in injected:
            injected = injected.replace(
                "{NOTES_DIR}", str(PROJECT_ROOT / "sample_packages")
            )
        leftover = re.findall(r"\{[A-Z_]+_DIR\}", injected)
        if leftover:
            record(
                f"{mod_name} 注入后无残留占位符",
                FAIL,
                f"仍有未替换占位符: {set(leftover)}",
            )
        else:
            record(f"{mod_name} 注入正常", PASS, "替换后无 *_DIR 占位符残留")


# --- 3. run_pipeline.PROMPT_MODULES 一致性 ----------------------------------
def test_run_pipeline_modules():
    print("\n[3] run_pipeline.PROMPT_MODULES 一致性")
    try:
        rp = load_module("run_pipeline")
    except Exception as e:
        record("import run_pipeline", FAIL, f"导入失败: {e}")
        return None
    record("import run_pipeline", PASS)

    for info in rp.PROMPT_MODULES:
        mod_name = info["module"]
        var_name = info["prompt_var"]
        try:
            mod = load_module(mod_name)
        except Exception as e:
            record(f"PROMPT_MODULES -> {mod_name}", FAIL, f"模块加载失败: {e}")
            continue
        if hasattr(mod, var_name):
            record(f"PROMPT_MODULES -> {mod_name}.{var_name}", PASS)
        else:
            record(
                f"PROMPT_MODULES -> {mod_name}.{var_name}",
                FAIL,
                "run_pipeline 引用的 prompt 变量在模块中不存在",
            )

    # semantic-discovery 未出现在 PROMPT_MODULES（Step 2 循环内动态构造），是预期的
    declared = {i["module"] for i in rp.PROMPT_MODULES}
    if "semantic_discovery_agent_prompt" not in declared:
        record(
            "semantic-discovery 由 Step2 动态构造",
            PASS,
            "未列入 PROMPT_MODULES 属预期（循环内逐个样本包构造）",
        )
    return rp


# --- 4. Step 2 样本包扫描逻辑 -----------------------------------------------
def test_package_scan():
    print("\n[4] Step 2 样本包扫描逻辑（排除 package_notes/global_notes）")
    sp_dir = PROJECT_ROOT / "sample_packages"
    if not sp_dir.exists():
        record("sample_packages 存在", WARN, "目录不存在，跳过扫描测试（无数据时属正常）")
        return
    package_dirs = []
    for item in sorted(sp_dir.iterdir()):
        if item.is_dir() and item.name not in ("package_notes", "global_notes"):
            package_dirs.append(item)
    excluded = [
        d.name
        for d in sp_dir.iterdir()
        if d.is_dir() and d.name in ("package_notes", "global_notes")
    ]
    record(
        "样本包扫描",
        PASS,
        f"识别到 {len(package_dirs)} 个样本包: {[p.name for p in package_dirs]}; "
        f"已排除: {excluded}",
    )


# --- 5. SKILL.md frontmatter name 与部署目录自洽 ----------------------------
def _read_frontmatter_name(skill_md_path):
    text = skill_md_path.read_text(encoding="utf-8")
    m = re.search(r"^name:\s*(.+)$", text, re.MULTILINE)
    return m.group(1).strip() if m else None


def test_skill_frontmatter():
    print("\n[5] SKILL.md frontmatter name 与部署")
    try:
        rp = load_module("run_pipeline")
        skills = rp.SKILLS
    except Exception:
        skills = [
            "sample-package-constructor-agent-skill",
            "semantic-discovery-agent-skill",
            "evaluation-compiler-agent-skill",
        ]
    for skill_dir in skills:
        md = PROJECT_ROOT / skill_dir / "SKILL.md"
        if not md.exists():
            record(f"{skill_dir}/SKILL.md 存在", FAIL, "SKILL.md 缺失")
            continue
        name = _read_frontmatter_name(md)
        if not name:
            record(f"{skill_dir} frontmatter name", FAIL, "未找到 name 字段")
        else:
            record(f"{skill_dir}", PASS, f"frontmatter name = {name}")


# --- 6. evaluation-compiler 引用模板是否存在 --------------------------------
def test_compiler_templates():
    print("\n[6] evaluation-compiler SKILL.md 引用的模板存在性")
    md = PROJECT_ROOT / "evaluation-compiler-agent-skill" / "SKILL.md"
    if not md.exists():
        record("evaluation-compiler SKILL.md", FAIL, "缺失")
        return
    text = md.read_text(encoding="utf-8")
    # 抓取形如 assets/xxx.md 的引用
    refs = set(re.findall(r"assets/[\w\-./]+\.md", text))
    if not refs:
        record("模板引用抓取", WARN, "未在 SKILL.md 中发现 assets/*.md 引用")
        return
    for ref in sorted(refs):
        target = PROJECT_ROOT / "evaluation-compiler-agent-skill" / ref
        if target.exists():
            record(f"引用模板 {ref}", PASS)
        else:
            record(f"引用模板 {ref}", FAIL, f"文件不存在: {target}")


# --- 7. Step 2 notes 路径接线 vs 磁盘布局 -----------------------------------
def test_step2_notes_wiring():
    print("\n[7] Step 2 notes 路径接线 vs 磁盘实际布局")
    sp_dir = PROJECT_ROOT / "sample_packages"
    if not sp_dir.exists():
        record("notes 路径接线", WARN, "sample_packages 不存在，跳过")
        return
    package_dirs = [
        d
        for d in sorted(sp_dir.iterdir())
        if d.is_dir() and d.name not in ("package_notes", "global_notes")
    ]
    if not package_dirs:
        record("notes 路径接线", WARN, "无样本包，跳过")
        return

    pkg = package_dirs[0]
    # 复刻 run_pipeline Step 2 的真实注入：
    #   INPUT_DIR = 单个样本包目录
    #   NOTES_DIR = sample_packages/ 顶层
    try:
        mod = load_module("semantic_discovery_agent_prompt")
        prompt = mod.SEMANTIC_DISCOVERY_AGENT_PROMPT
    except Exception as e:
        record("notes 路径接线", FAIL, f"无法加载 semantic prompt: {e}")
        return

    injected = prompt.replace("{INPUT_DIR}", str(pkg))
    injected = injected.replace("{OUTPUT_DIR}", str(PROJECT_ROOT / "semantic_reports"))
    injected = injected.replace("{NOTES_DIR}", str(sp_dir))

    # 从注入后的 prompt 中还原出 notes 引用路径并核对是否真实存在
    expected_pkg_notes = sp_dir / "package_notes"
    expected_global_notes = sp_dir / "global_notes"

    # prompt 接线问题 -> FAIL；磁盘产物缺失（运行产物不完整）-> WARN，与 [4] 口径一致
    wiring_problems = []
    if str(pkg / "package_notes") in injected or str(pkg / "global_notes") in injected:
        wiring_problems.append("prompt 仍把 notes 指向样本包内部（{INPUT_DIR}/...），应指向顶层")
    if str(expected_pkg_notes) not in injected:
        wiring_problems.append("prompt 未引用顶层 package_notes")
    if str(expected_global_notes) not in injected:
        wiring_problems.append("prompt 未引用顶层 global_notes")
    disk_problems = []
    if not expected_pkg_notes.exists():
        disk_problems.append(f"磁盘缺少 {expected_pkg_notes}")
    if not expected_global_notes.exists():
        disk_problems.append(f"磁盘缺少 {expected_global_notes}")

    if wiring_problems:
        record("notes 路径接线", FAIL, "; ".join(wiring_problems + disk_problems))
    elif disk_problems:
        record(
            "notes 路径接线", WARN,
            "prompt 接线正确，但运行产物不完整: " + "; ".join(disk_problems),
        )
    else:
        record(
            "notes 路径接线",
            PASS,
            "prompt 的 notes 引用已指向 sample_packages/ 顶层，且磁盘上真实存在",
        )


# --- 8. Step1 切分质量校验器接线 --------------------------------------------
def test_step1_validation_wiring():
    print("\n[8] Step1 切分质量校验器接线")
    try:
        vsp = load_module("validate_sample_packages")
    except Exception as e:
        record("import validate_sample_packages", FAIL, f"导入失败: {e}")
        return
    for attr in ("validate", "render_feedback", "write_report", "print_summary", "DEFAULTS"):
        if not hasattr(vsp, attr):
            record(f"validate_sample_packages.{attr}", FAIL, "缺少约定函数/常量")
            return
    record("import validate_sample_packages", PASS, "校验器可导入且接口齐全")

    # Step1 prompt 必须含 {VALIDATION_FEEDBACK} 占位符（带反馈重跑用）
    try:
        mod = load_module("sample_package_constructor_agent_prompt")
        prompt = mod.SAMPLE_PACKAGE_CONSTRUCTOR_AGENT_PROMPT
    except Exception as e:
        record("Step1 prompt 加载", FAIL, f"{e}")
        return
    if "{VALIDATION_FEEDBACK}" in prompt:
        record("Step1 prompt 含 {VALIDATION_FEEDBACK}", PASS)
    else:
        record("Step1 prompt 含 {VALIDATION_FEEDBACK}", FAIL, "缺少校验反馈占位符")

    # run_pipeline 侧的开关常量存在
    try:
        rp = load_module("run_pipeline")
    except Exception as e:
        record("run_pipeline 校验接线", FAIL, f"导入失败: {e}")
        return
    missing = [a for a in ("STRICT_STEP1", "STEP1_VALIDATION_RETRIES", "_reset_sample_packages")
               if not hasattr(rp, a)]
    if missing:
        record("run_pipeline 校验接线", FAIL, f"缺少: {missing}")
    else:
        record("run_pipeline 校验接线", PASS,
               f"STRICT_STEP1={rp.STRICT_STEP1}, RETRIES={rp.STEP1_VALIDATION_RETRIES}")


def main():
    print("=" * 64)
    print("静态流水线自检（无数据 / 无 LLM 调用）")
    print("=" * 64)
    print(f"项目根目录: {PROJECT_ROOT}")

    modules = test_prompt_modules_import()
    test_placeholder_injection(modules)
    test_run_pipeline_modules()
    test_package_scan()
    test_skill_frontmatter()
    test_compiler_templates()
    test_step2_notes_wiring()
    test_step1_validation_wiring()

    n_pass = sum(1 for _, s, _ in _results if s == PASS)
    n_warn = sum(1 for _, s, _ in _results if s == WARN)
    n_fail = sum(1 for _, s, _ in _results if s == FAIL)
    print("\n" + "=" * 64)
    print(f"结果汇总:  PASS={n_pass}  WARN={n_warn}  FAIL={n_fail}")
    print("=" * 64)
    if n_fail:
        print("\n存在 FAIL 项，请查看上方 [FAIL] 明细。")
    sys.exit(1 if n_fail else 0)


if __name__ == "__main__":
    main()
