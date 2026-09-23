#!/usr/bin/env python3
"""
样本包切分质量校验器（Step1 产物硬约束检查，纯标准库、不调用模型）

针对"广而薄"的切分失败模式（包很多、每包每个文件只切一小段、跨包切同一段），
在 Step1 产出后立即做程序化校验。run_pipeline.py 会在校验不过时把违规明细
作为反馈注入 prompt 重跑 Step1 一次；仍不过则中止本轮，避免低质量样本包污染下游。

校验分两档：
  硬伤（hard）—— 打回重做/中止：
    - 结构缺失：无样本包、缺 package_notes/<包名>.md、缺 global_notes 三件套
    - 空包：包内没有非空文件
    - 臆造来源：包内出现输入簇中不存在的文件
    - common/ 不一致：包内 common/ 与输入 common/ 非逐字节一致
    - 整份复制：大簇切分时单文件来源片段量 >= 原文 90%
      （原文 < 2000 字符的短文件、或总量 <= 200000 字符且仅产出一个包的
      小簇透传豁免）
    - 跨包重复切片：同一来源文件在不同包中的片段逐字节相同，或相似度 >= 85%
    - 证据太薄：单包非 common 实质内容 < 8000 字符（簇本身更小则按簇总量折算）
    - 超出容量：单包内容总量 > 200000 字符
    - 并集吸收率不足：跨包并集片段量 < 簇原文总量的 25%
  软性偏差（soft）—— 仅告警：
    - 目录来源采样密度低于 source-coverage-policy 规则
    - 目录来源并集文件覆盖 < 80%

用法：
  python3 validate_sample_packages.py [--input data/input] [--packages sample_packages]
退出码：0=无硬伤（可有告警），1=存在硬伤
"""

import argparse
import difflib
import hashlib
import math
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.resolve()

# 默认阈值口径。与 sample-package-constructor-agent-skill 的
# references/slice-depth-policy.md 保持一致；如按下游上下文能力调整，两处同步改。
DEFAULTS = {
    "near_full_copy_ratio": 0.90,   # 片段量/原文量 >= 此值视为整份复制
    "short_file_exempt_chars": 2000, # 原文短于此的单文件豁免整份复制约束
    "cross_pkg_dup_ratio": 0.85,    # 同名文件跨包片段相似度 >= 此值视为重复搬运
    "min_pkg_chars": 8000,          # 单包非 common 实质内容下限
    "max_pkg_chars": 200000,        # 单包内容总量上限（下游一次可消化）
    "min_union_absorption": 0.25,   # 跨包并集片段量 / 簇原文总量 下限
    "min_dir_union_coverage": 0.80, # 目录来源并集文件覆盖下限（软性）
}

NOTES_DIRS = ("package_notes", "global_notes")
GLOBAL_NOTE_FILES = ("package_index.md", "coverage_report.md", "unused_or_low_priority_data.md")


def _read_text(path):
    return path.read_text(encoding="utf-8", errors="ignore")


def _norm(text):
    """去空白归一化，字符量与相似度都按归一化口径计算。"""
    return re.sub(r"\s+", "", text)


def _similarity(a, b):
    """两段归一化文本的相似度（0~1），带快速预筛避免无谓的全量比对。"""
    if not a or not b:
        return 0.0
    # 完全相同是最常见的违规输入之一（例如误把整份大文档复制进样本包）。
    # 先走线性字符串比较，避免 difflib 对十万字符级重复文本做昂贵匹配。
    if a == b:
        return 1.0
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    if sm.real_quick_ratio() < 0.5 or sm.quick_ratio() < 0.5:
        return 0.0
    return sm.ratio()


def dir_min_samples(n):
    """目录来源在每个包中的最低采样文件数（source-coverage-policy 规则）。"""
    if n <= 0:
        return 0
    if n <= 4:
        return n - 1
    if n <= 8:
        return math.ceil(0.5 * n)
    return max(4, math.ceil(0.35 * n))


def _discover_sources(input_dir):
    """识别输入簇的三类来源：单文件 / 目录 / common。"""
    singles = [f for f in sorted(input_dir.iterdir())
               if f.is_file() and not f.name.startswith(".")]
    dirs = [d for d in sorted(input_dir.iterdir())
            if d.is_dir() and d.name != "common" and not d.name.startswith(".")]
    common = input_dir / "common"
    return singles, dirs, (common if common.is_dir() else None)


def _package_dirs(packages_dir):
    return [d for d in sorted(packages_dir.iterdir())
            if d.is_dir() and d.name not in NOTES_DIRS]


def _pkg_fragments(pkg):
    """包内非 common 的实质文件列表（相对路径 -> Path）。"""
    frags = {}
    for f in sorted(pkg.rglob("*")):
        if not f.is_file():
            continue
        rel = f.relative_to(pkg)
        if rel.parts and rel.parts[0] == "common":
            continue
        if f.stat().st_size == 0:
            continue
        frags[rel] = f
    return frags


def validate(input_dir, packages_dir, config=None):
    """校验样本包产物，返回 {"hard":[...], "soft":[...], "metrics":{...}}。"""
    cfg = dict(DEFAULTS)
    if config:
        cfg.update(config)
    input_dir = Path(input_dir)
    packages_dir = Path(packages_dir)
    hard, soft = [], []
    metrics = {}

    if not packages_dir.exists():
        return {"hard": [f"输出目录不存在: {packages_dir}"], "soft": [], "metrics": {}}

    singles, dir_sources, common_dir = _discover_sources(input_dir)
    pkgs = _package_dirs(packages_dir)

    if not pkgs:
        return {"hard": ["未产出任何样本包目录"], "soft": [], "metrics": {}}

    # ---- 结构：notes 层 ----
    for note in GLOBAL_NOTE_FILES:
        if not (packages_dir / "global_notes" / note).exists():
            hard.append(f"缺少 global_notes/{note}")
    for pkg in pkgs:
        if not (packages_dir / "package_notes" / f"{pkg.name}.md").exists():
            hard.append(f"缺少 package_notes/{pkg.name}.md")

    # ---- 原文基线 ----
    orig_norm = {}   # 相对路径 -> 归一化原文
    for f in singles:
        orig_norm[Path(f.name)] = _norm(_read_text(f))
    for d in dir_sources:
        for f in sorted(d.rglob("*")):
            if f.is_file():
                orig_norm[f.relative_to(input_dir)] = _norm(_read_text(f))
    cluster_chars = sum(len(v) for v in orig_norm.values())
    metrics["cluster_chars"] = cluster_chars
    # Skill 规范要求：整簇能被下游一次消化时，直接作为一个包透传，避免
    # 为满足形式上的“切片”而拆散本应一起观察的证据。只有同时满足
    # “单包 + 总量不超过下游容量”才算小簇透传；单文件但超大仍须切分。
    small_cluster_passthrough = (
        len(pkgs) == 1 and cluster_chars <= cfg["max_pkg_chars"]
    )
    metrics["small_cluster_passthrough"] = small_cluster_passthrough

    # ---- 逐包检查 ----
    pkg_frag_norm = {}   # 包名 -> {相对路径: 归一化片段}
    pkg_chars = {}
    for pkg in pkgs:
        frags = _pkg_fragments(pkg)
        if not frags:
            hard.append(f"{pkg.name} 为空包（无非空实质文件）")
            pkg_frag_norm[pkg.name] = {}
            pkg_chars[pkg.name] = 0
            continue

        norm_map = {}
        for rel, f in frags.items():
            text = _norm(_read_text(f))
            norm_map[rel] = text
            if rel not in orig_norm:
                hard.append(f"{pkg.name}/{rel} 在输入簇中不存在对应来源文件（疑似臆造）")
                continue
            # 整份复制检查（仅单文件来源级别都适用；短原文豁免）
            orig = orig_norm[rel]
            if (
                not small_cluster_passthrough
                and len(orig) >= cfg["short_file_exempt_chars"]
                and len(orig) > 0
            ):
                ratio = len(text) / len(orig)
                if ratio >= cfg["near_full_copy_ratio"] and \
                        _similarity(text, orig) >= cfg["cross_pkg_dup_ratio"]:
                    hard.append(
                        f"{pkg.name}/{rel} 接近整份复制原文"
                        f"（片段量为原文的 {ratio:.0%}，阈值 {cfg['near_full_copy_ratio']:.0%}）"
                    )
        pkg_frag_norm[pkg.name] = norm_map
        total = sum(len(v) for v in norm_map.values())
        pkg_chars[pkg.name] = total

        floor = min(cfg["min_pkg_chars"], int(cluster_chars * 0.9))
        if total < floor:
            hard.append(
                f"{pkg.name} 证据太薄：非 common 实质内容仅 {total} 字符"
                f"（下限 {floor}）。应加深每个来源文件的切片深度，而不是每个文件只切一小段"
            )
        if total > cfg["max_pkg_chars"]:
            hard.append(
                f"{pkg.name} 超出单包容量：{total} 字符（上限 {cfg['max_pkg_chars']}），"
                f"应拆分或收窄切片"
            )
    metrics["pkg_chars"] = pkg_chars

    # ---- 覆盖检查 ----
    for f in singles:
        rel = Path(f.name)
        for pkg in pkgs:
            if rel not in pkg_frag_norm.get(pkg.name, {}):
                hard.append(f"{pkg.name} 缺少单文件来源 {f.name} 的采样（每包必须覆盖所有单文件来源）")
    for d in dir_sources:
        dir_files = [f for f in sorted(d.rglob("*")) if f.is_file()]
        n = len(dir_files)
        need = dir_min_samples(n)
        union_hit = set()
        for pkg in pkgs:
            hit = [rel for rel in pkg_frag_norm.get(pkg.name, {})
                   if rel.parts and rel.parts[0] == d.name]
            union_hit.update(hit)
            if not hit:
                hard.append(f"{pkg.name} 完全缺席目录来源 {d.name}/")
            elif len(hit) < need:
                soft.append(
                    f"{pkg.name} 对目录来源 {d.name}/ 采样偏薄：{len(hit)}/{n} 个文件"
                    f"（密度规则要求 >= {need}）"
                )
        if n > 0 and len(union_hit) / n < cfg["min_dir_union_coverage"]:
            soft.append(
                f"目录来源 {d.name}/ 跨包并集文件覆盖仅 {len(union_hit)}/{n}"
                f"（目标 >= {cfg['min_dir_union_coverage']:.0%}）"
            )

    # ---- 跨包重复切片 ----
    all_rels = set()
    for m in pkg_frag_norm.values():
        all_rels.update(m.keys())
    for rel in sorted(all_rels):
        holders = [(pkg.name, pkg_frag_norm[pkg.name][rel])
                   for pkg in pkgs if rel in pkg_frag_norm.get(pkg.name, {})]
        for i in range(len(holders)):
            for j in range(i + 1, len(holders)):
                (na, ta), (nb, tb) = holders[i], holders[j]
                if not ta or not tb:
                    continue
                if hashlib.md5(ta.encode()).digest() == hashlib.md5(tb.encode()).digest():
                    hard.append(
                        f"{rel}: {na} 与 {nb} 的片段逐字节相同——同一来源在不同包中"
                        f"必须切原文的不同部分"
                    )
                    continue
                sim = _similarity(ta, tb)
                if sim >= cfg["cross_pkg_dup_ratio"]:
                    hard.append(
                        f"{rel}: {na} 与 {nb} 的片段相似度 {sim:.0%}"
                        f"（阈值 {cfg['cross_pkg_dup_ratio']:.0%}）——近乎重复搬运，"
                        f"不同包必须切不同章节/段落"
                    )

    # ---- 并集吸收率 ----
    seen_hashes = set()
    union_chars = 0
    for m in pkg_frag_norm.values():
        for text in m.values():
            h = hashlib.md5(text.encode()).digest()
            if h not in seen_hashes:
                seen_hashes.add(h)
                union_chars += len(text)
    absorption = (union_chars / cluster_chars) if cluster_chars else 0.0
    metrics["union_chars"] = union_chars
    metrics["absorption"] = absorption
    if cluster_chars and absorption < cfg["min_union_absorption"]:
        hard.append(
            f"跨包并集吸收率仅 {absorption:.1%}（下限 {cfg['min_union_absorption']:.0%}）："
            f"并集片段 {union_chars} 字符 / 簇原文 {cluster_chars} 字符。"
            f"每包对来源文件的切片太浅，应大幅加深切片（切完整章节而非零星段落）"
        )

    # ---- common/ 一致性 ----
    if common_dir:
        common_files = [f for f in sorted(common_dir.rglob("*")) if f.is_file()]
        for pkg in pkgs:
            for f in common_files:
                rel = f.relative_to(common_dir)
                target = pkg / "common" / rel
                if not target.exists():
                    hard.append(f"{pkg.name}/common/{rel} 缺失（common/ 必须原封不动完整复制）")
                elif target.read_bytes() != f.read_bytes():
                    hard.append(f"{pkg.name}/common/{rel} 与输入 common/ 不一致（必须逐字节一致）")

    return {"hard": hard, "soft": soft, "metrics": metrics}


def render_feedback(report):
    """把违规明细渲染成注入 Step1 prompt 的反馈块（{VALIDATION_FEEDBACK}）。"""
    if not report["hard"]:
        return ""
    lines = [
        "",
        "【上一次切分未通过质量校验——本次必须逐项修正（重要）】",
        "上一次产出的样本包被自动校验器判为不合格，硬性问题如下：",
    ]
    for i, msg in enumerate(report["hard"], 1):
        lines.append(f"{i}. {msg}")
    m = report.get("metrics", {})
    if m.get("cluster_chars"):
        lines.append(
            f"（参考：簇原文总量约 {m['cluster_chars']} 字符，"
            f"上次并集吸收率 {m.get('absorption', 0):.1%}）"
        )
    lines += [
        "修正要求：",
        "- 不要通过增加包数解决证据太薄的问题，优先加深每个包对来源文件的切片深度；",
        "- 对每个单文件来源，先把原文按章节/主题划成互不重叠的区段，把不同区段分配给不同包；",
        "- 每包分到的区段要完整、连续、足量（切完整章节，不是每处抄两行）；",
        "- 重新产出全部样本包与 notes（旧产物已清空）。",
        "",
    ]
    return "\n".join(lines)


def render_markdown(report):
    """人读校验报告。"""
    m = report.get("metrics", {})
    lines = ["# 样本包切分质量校验报告", ""]
    lines.append(f"- 结论：{'不合格（存在硬伤）' if report['hard'] else '通过'}")
    if m.get("cluster_chars") is not None:
        lines.append(f"- 簇原文总量（归一化字符）：{m.get('cluster_chars', 0)}")
        lines.append(f"- 跨包并集吸收量：{m.get('union_chars', 0)}"
                     f"（吸收率 {m.get('absorption', 0):.1%}）")
    for name, chars in (m.get("pkg_chars") or {}).items():
        lines.append(f"- {name} 非 common 实质内容：{chars} 字符")
    if report["hard"]:
        lines += ["", "## 硬伤（打回重做口径）", ""]
        lines += [f"{i}. {msg}" for i, msg in enumerate(report["hard"], 1)]
    if report["soft"]:
        lines += ["", "## 软性偏差（仅告警）", ""]
        lines += [f"{i}. {msg}" for i, msg in enumerate(report["soft"], 1)]
    lines.append("")
    return "\n".join(lines)


def write_report(report, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_markdown(report), encoding="utf-8")
    return path


def print_summary(report):
    m = report.get("metrics", {})
    if m.get("cluster_chars") is not None:
        print(f"  [切分质量校验] 并集吸收率 {m.get('absorption', 0):.1%} | "
              f"包内容量 {m.get('pkg_chars', {})}")
    if report["hard"]:
        print(f"  ✗ 硬伤 {len(report['hard'])} 项:")
        for msg in report["hard"]:
            print(f"    - {msg}")
    else:
        print("  ✓ 切分质量校验通过（无硬伤）")
    if report["soft"]:
        print(f"  ⚠️ 软性偏差 {len(report['soft'])} 项:")
        for msg in report["soft"]:
            print(f"    - {msg}")


def main():
    ap = argparse.ArgumentParser(description="样本包切分质量校验器（不调用模型）")
    ap.add_argument("--input", default="data/input", help="输入簇目录（默认 data/input）")
    ap.add_argument("--packages", default="sample_packages",
                    help="Step1 产物目录（默认 sample_packages）")
    ap.add_argument("--write-report", action="store_true",
                    help="把报告写入 <packages>/global_notes/validation_report.md")
    args = ap.parse_args()

    input_dir = (PROJECT_ROOT / args.input).resolve()
    packages_dir = (PROJECT_ROOT / args.packages).resolve()
    print(f"输入簇: {input_dir}")
    print(f"样本包: {packages_dir}")

    report = validate(input_dir, packages_dir)
    print_summary(report)
    if args.write_report:
        out = write_report(report, packages_dir / "global_notes" / "validation_report.md")
        print(f"报告已写入: {out}")
    sys.exit(1 if report["hard"] else 0)


if __name__ == "__main__":
    main()
