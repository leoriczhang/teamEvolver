#!/usr/bin/env python3
r"""
语义覆盖占比报告（Semantic Coverage Report）

回答一个问题：**Step2 语义发现挖出来的东西，有多少真正进了最终 skill？**

知识源（全部是现成 markdown，无需改动流水线）：
  - semantic_reports/样本包00X_语义分析报告.md
      · 候选单元 U-01…U-NN（规范名 + 当前保留等级：高/中/低）
      · 候选判断/流程结构 S-01…（可选）
      · 结构化缺口清单 GAP-01…（严重度 高/中/低）
  - compiled_skill/<skill>/SKILL.md
      · 能力维度一…十二
      · 「引用来源（可追溯）」表：维度 → 001-U-XX / 002-U-XX 等语义单元引用
      · 「待补维度/未解决缺口」表 + 全文 GAP-\d+ 残留

计算两个口径的覆盖：
  A) 单元级覆盖：每个语义单元 U-XX 是否被 SKILL 的引用来源表采纳（进入了某个维度）。
     -> 采纳率、按保留等级(高/中/低)分层的采纳率、未采纳单元清单。
  B) 维度级覆盖：EVALUATION/SKILL 的每个能力维度由多少语义单元、跨几份文件支撑；
     以及 GAP 消解情况（语义报告里发现的 GAP 有多少在最终 skill 里仍标记为未解决）。

产物（写入 coverage_reports/）：
  - SEMANTIC_COVERAGE.md   人读报告
  - semantic_coverage.json 机器可读

只读解析 + 只写 coverage_reports/，不触碰流水线任何中间产物。
"""

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.resolve()
SEMANTIC_DIR = PROJECT_ROOT / "semantic_reports"
COMPILED_DIR = PROJECT_ROOT / "compiled_skill"
OUT_DIR = PROJECT_ROOT / "coverage_reports"

RETENTION_LEVELS = ["高", "中", "低", "未标注"]


# ============================================================
# 解析 semantic_reports/*.md
# ============================================================
def _pkg_number(report_path):
    """从 '样本包001_语义分析报告.md' 提取包号 '001'（用于拼 001-U-XX 引用键）。"""
    m = re.search(r"样本包\s*0*(\d+)", report_path.name)
    if m:
        return f"{int(m.group(1)):03d}"
    return "000"


def parse_semantic_report(report_path):
    """解析单份语义报告，抽出候选单元 U-XX（规范名 + 保留等级）与 GAP 清单。

    返回 {"pkg","units":[{uid,ref,name,retention}],"gaps":[{gid,ref,severity,desc}]}。
    """
    pkg = _pkg_number(report_path)
    text = report_path.read_text(encoding="utf-8", errors="ignore")
    lines = text.splitlines()

    units = []
    # 候选单元标题形如： "### 候选单元 U-01：首问负责制（First-Respondent...）"
    # 也兼容 002 报告的 "### 单元 U-01：投诉严重度分级判定"
    unit_hdr = re.compile(r"^#{2,4}\s*(?:候选)?单元\s*(U-\d+)\s*[:：]\s*(.+?)\s*$")
    # 保留等级形如： "- **当前保留等级**：高（……）"
    reten_re = re.compile(r"当前保留等级\**\s*[：:]\s*\**\s*(高|中|低)")
    # 规范名形如： "- **规范名**：投诉严重度分级判定"
    canon_re = re.compile(r"规范名\**\s*[：:]\s*\**\s*(.+?)\s*$")

    cur = None
    for ln in lines:
        m = unit_hdr.match(ln.strip())
        if m:
            if cur:
                units.append(cur)
            uid = m.group(1)
            # 规范名去掉英文括号部分
            name = re.sub(r"[（(].*?[)）]\s*$", "", m.group(2)).strip()
            cur = {"uid": uid, "ref": f"{pkg}-{uid}", "name": name, "retention": "未标注"}
            continue
        if cur:
            cm = canon_re.search(ln)
            if cm and "结构规范名" not in ln:
                cur["name"] = re.sub(r"[（(].*?[)）]\s*$", "", cm.group(1)).strip()
            rm = reten_re.search(ln)
            if rm and cur["retention"] == "未标注":
                cur["retention"] = rm.group(1)
    if cur:
        units.append(cur)

    # 结构化缺口清单：表格行 "| GAP-01 | 描述… | 影响单元 | **高** | … |"
    gaps = []
    gap_row = re.compile(r"^\|\s*(GAP-\d+)\s*\|(.+)$")
    for ln in lines:
        m = gap_row.match(ln.strip())
        if not m:
            continue
        gid = m.group(1)
        rest = m.group(2)
        sev = "未标注"
        sm = re.search(r"(高|中|低)\s*(?:严重度|—|-|\||）|\))", rest)
        if not sm:
            sm = re.search(r"\*\*(高|中|低)\*\*", rest)
        if sm:
            sev = sm.group(1)
        desc = rest.split("|")[0].strip()
        gaps.append({"gid": gid, "ref": f"{pkg}-{gid}", "severity": sev, "desc": desc})

    return {"pkg": pkg, "path": report_path, "units": units, "gaps": gaps}


def load_semantic_reports():
    reports = []
    if not SEMANTIC_DIR.exists():
        return reports
    for p in sorted(SEMANTIC_DIR.glob("*.md")):
        reports.append(parse_semantic_report(p))
    return reports


# ============================================================
# 解析 compiled_skill/<skill>/SKILL.md
# ============================================================
def find_skill_md(skill_name=None):
    if not COMPILED_DIR.exists():
        return None
    cands = [d for d in sorted(COMPILED_DIR.iterdir())
             if d.is_dir() and (d / "SKILL.md").exists()]
    if not cands:
        return None
    if skill_name:
        for d in cands:
            if d.name == skill_name:
                return d / "SKILL.md"
    return cands[0] / "SKILL.md"


def parse_skill_references(skill_md_path):
    r"""解析 SKILL.md，抽出：
      - dims: 能力维度列表（如 '维度一 首问负责制'）
      - refs_by_dim: {维度key: set(引用的 NNN-U-XX)}
      - referenced_units: set(所有被引用的 NNN-U-XX)
      - unresolved_gaps: set(SKILL 中仍标记未解决的 GAP，形如 NNN-GAP-XX 或裸 GAP-XX)
      - all_gap_tokens: 全文 GAP-\d+ 去重
    """
    text = skill_md_path.read_text(encoding="utf-8", errors="ignore")

    # 能力维度标题： "### 维度一：首问负责制…" / "## 能力维度一 …"
    dims = []
    for m in re.finditer(r"^#{2,4}\s*(?:能力)?维度([一二三四五六七八九十百]+)\s*[:：]?\s*(.+?)\s*$",
                         text, re.M):
        dims.append(f"维度{m.group(1)} {m.group(2).strip()}")

    # 引用来源：形如 001-U-01 / 002-U-03（也兼容 001-GAP-03 / 001-S-03 但只收 U）
    ref_unit_re = re.compile(r"\b(\d{3})-(U-\d+)\b")

    # 引用来源表：定位到「引用来源」小节后逐行找 "| 维度X … | …001-U-.. |"
    refs_by_dim = {}
    referenced_units = set()
    lines = text.splitlines()
    for ln in lines:
        # 收集全表内的引用（不强依赖节边界，全文兜底也收）
        row_units = {f"{a}-{b}" for a, b in ref_unit_re.findall(ln)}
        if row_units:
            referenced_units |= row_units
            dm = re.search(r"维度([一二三四五六七八九十百]+)", ln)
            if dm:
                key = f"维度{dm.group(1)}"
                refs_by_dim.setdefault(key, set())
                refs_by_dim[key] |= row_units

    # 全文所有 U 引用（兜底，防止引用不在表格行）
    for a, b in ref_unit_re.findall(text):
        referenced_units.add(f"{a}-{b}")

    # 未解决缺口：全文 GAP token
    all_gap_tokens = set(re.findall(r"GAP-\d+", text))
    # 带包号的 GAP 引用（如 001-GAP-03），作为"仍被 skill 记录为坑位/线索"的信号
    pkg_gap = set(f"{a}-{b}" for a, b in re.findall(r"\b(\d{3})-(GAP-\d+)\b", text))

    return {
        "dims": dims,
        "refs_by_dim": refs_by_dim,
        "referenced_units": referenced_units,
        "pkg_gap_refs": pkg_gap,
        "all_gap_tokens": all_gap_tokens,
    }


# ============================================================
# 覆盖计算
# ============================================================
def compute_coverage(reports, skill_ref):
    referenced = skill_ref["referenced_units"]

    # ---- A) 单元级覆盖 ----
    all_units = []
    for rep in reports:
        for u in rep["units"]:
            all_units.append(u)
    total_units = len(all_units)
    adopted = [u for u in all_units if u["ref"] in referenced]
    dropped = [u for u in all_units if u["ref"] not in referenced]

    # 按保留等级分层
    by_reten = {lv: {"total": 0, "adopted": 0} for lv in RETENTION_LEVELS}
    for u in all_units:
        lv = u["retention"] if u["retention"] in by_reten else "未标注"
        by_reten[lv]["total"] += 1
        if u["ref"] in referenced:
            by_reten[lv]["adopted"] += 1

    unit_cov = {
        "total_units": total_units,
        "adopted": len(adopted),
        "dropped": len(dropped),
        "adopt_rate": round(len(adopted) / total_units * 100, 1) if total_units else 0.0,
        "by_retention": {
            lv: {
                **by_reten[lv],
                "rate": (round(by_reten[lv]["adopted"] / by_reten[lv]["total"] * 100, 1)
                         if by_reten[lv]["total"] else None),
            } for lv in RETENTION_LEVELS
        },
        "dropped_units": [{"ref": u["ref"], "name": u["name"], "retention": u["retention"]}
                          for u in dropped],
    }

    # ---- B) GAP 消解 ----
    all_gaps = []
    for rep in reports:
        all_gaps.extend(rep["gaps"])
    total_gaps = len(all_gaps)
    # 认为"仍未消解"= SKILL 里仍以 NNN-GAP-XX 形式引用该缺口（作为坑位/线索保留）
    still = skill_ref["pkg_gap_refs"]
    unresolved = [g for g in all_gaps if g["ref"] in still]
    resolved = [g for g in all_gaps if g["ref"] not in still]
    gap_by_sev = {lv: {"total": 0, "resolved": 0} for lv in ["高", "中", "低", "未标注"]}
    for g in all_gaps:
        lv = g["severity"] if g["severity"] in gap_by_sev else "未标注"
        gap_by_sev[lv]["total"] += 1
        if g["ref"] not in still:
            gap_by_sev[lv]["resolved"] += 1
    gap_cov = {
        "total_gaps": total_gaps,
        "resolved": len(resolved),
        "unresolved": len(unresolved),
        "resolve_rate": round(len(resolved) / total_gaps * 100, 1) if total_gaps else 0.0,
        "by_severity": {
            lv: {
                **gap_by_sev[lv],
                "rate": (round(gap_by_sev[lv]["resolved"] / gap_by_sev[lv]["total"] * 100, 1)
                         if gap_by_sev[lv]["total"] else None),
            } for lv in ["高", "中", "低", "未标注"]
        },
        "unresolved_gaps": [{"ref": g["ref"], "severity": g["severity"], "desc": g["desc"][:60]}
                            for g in unresolved],
    }

    # ---- C) 维度级覆盖 ----
    # 每个维度引用了多少语义单元 + 覆盖了几个不同来源包
    dim_cov = []
    for key, units in sorted(skill_ref["refs_by_dim"].items(), key=lambda kv: _dim_key(kv[0])):
        pkgs = {r.split("-", 1)[0] for r in units}
        dim_cov.append({
            "dimension": key,
            "unit_refs": sorted(units),
            "unit_count": len(units),
            "pkg_count": len(pkgs),
        })

    return {"unit_coverage": unit_cov, "gap_coverage": gap_cov, "dim_coverage": dim_cov}


def _dim_key(d):
    cn = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7,
          "八": 8, "九": 9, "十": 10, "十一": 11, "十二": 12}
    m = re.search(r"维度([一二三四五六七八九十]+)", d)
    if m and m.group(1) in cn:
        return (0, cn[m.group(1)])
    return (1, d)


# ============================================================
# 报告渲染
# ============================================================
def write_reports(cov, reports, skill_md_path, skill_name):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    uc = cov["unit_coverage"]
    gc = cov["gap_coverage"]

    lines = [
        f"# 语义覆盖占比报告 · {skill_name}",
        "",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 语义报告来源：{len(reports)} 份（{'、'.join(r['path'].name for r in reports)}）",
        f"- 被测 SKILL.md：{skill_md_path.relative_to(PROJECT_ROOT)}",
        "",
        "## 一、语义单元采纳率（单元级覆盖）",
        "",
        f"- 语义发现共产出候选单元：**{uc['total_units']}** 个",
        f"- 进入最终 skill（被引用来源表采纳）：**{uc['adopted']}** 个",
        f"- 未采纳：**{uc['dropped']}** 个",
        f"- **采纳率：{uc['adopt_rate']}%**",
        "",
        "按「当前保留等级」分层的采纳率：",
        "",
        "| 保留等级 | 单元数 | 已采纳 | 采纳率 |",
        "| --- | --- | --- | --- |",
    ]
    for lv in RETENTION_LEVELS:
        b = uc["by_retention"][lv]
        if b["total"] == 0:
            continue
        rate = f"{b['rate']}%" if b["rate"] is not None else "—"
        lines.append(f"| {lv} | {b['total']} | {b['adopted']} | {rate} |")

    if uc["dropped_units"]:
        lines += ["", "未被采纳的语义单元（可能被去重合并、被反驳或判为低价值）：", ""]
        for u in uc["dropped_units"]:
            lines.append(f"- `{u['ref']}` {u['name']}（保留等级：{u['retention']}）")

    lines += [
        "",
        "## 二、GAP 消解率（缺口覆盖）",
        "",
        f"- 语义发现共登记缺口：**{gc['total_gaps']}** 个",
        f"- 已消解（最终 skill 未再作为坑位保留）：**{gc['resolved']}** 个",
        f"- 仍未消解（skill 中仍标记为待补/坑位）：**{gc['unresolved']}** 个",
        f"- **消解率：{gc['resolve_rate']}%**",
        "",
        "按严重度分层：",
        "",
        "| 严重度 | 缺口数 | 已消解 | 消解率 |",
        "| --- | --- | --- | --- |",
    ]
    for lv in ["高", "中", "低", "未标注"]:
        b = gc["by_severity"][lv]
        if b["total"] == 0:
            continue
        rate = f"{b['rate']}%" if b["rate"] is not None else "—"
        lines.append(f"| {lv} | {b['total']} | {b['resolved']} | {rate} |")

    if gc["unresolved_gaps"]:
        lines += ["", "仍未消解的缺口：", ""]
        for g in gc["unresolved_gaps"]:
            lines.append(f"- `{g['ref']}`（{g['severity']}）{g['desc']}…")

    lines += [
        "",
        "## 三、维度级证据覆盖",
        "",
        "> 每个能力维度由多少语义单元支撑、覆盖了几个来源样本包。单元数/包数越高，证据越厚。",
        "",
        "| 能力维度 | 支撑单元数 | 覆盖样本包数 | 引用的语义单元 |",
        "| --- | --- | --- | --- |",
    ]
    for d in cov["dim_coverage"]:
        refs = "、".join(d["unit_refs"])
        lines.append(f"| {d['dimension']} | {d['unit_count']} | {d['pkg_count']} | {refs} |")

    lines += [
        "",
        "---",
        "> 口径说明：",
        "> - **单元采纳率** = 语义报告里的候选单元 U-XX 出现在 SKILL『引用来源』表中的比例；"
        "未采纳多因跨包去重合并、被反驳(R-XX)或判为低价值。",
        "> - **GAP 消解率** = 语义报告缺口清单里的 GAP，在最终 skill 中不再以 `包号-GAP-XX` 形式"
        "作为待补坑位保留的比例（近似认为已被证据补齐或已并入维度）。",
        "> - **维度级覆盖** 取自 SKILL『引用来源』表的维度→单元映射。",
        "",
    ]
    (OUT_DIR / "SEMANTIC_COVERAGE.md").write_text("\n".join(lines), encoding="utf-8")

    payload = {
        "skill": skill_name,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "semantic_reports": [r["path"].name for r in reports],
        "skill_md": str(skill_md_path.relative_to(PROJECT_ROOT)),
        **cov,
    }
    (OUT_DIR / "semantic_coverage.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_args():
    ap = argparse.ArgumentParser(description="语义覆盖占比报告")
    ap.add_argument("--skill", default=None, help="指定 compiled_skill 下的 skill 目录名（默认取第一个）")
    return ap.parse_args()


def main():
    args = parse_args()
    print("=" * 60)
    print("语义覆盖占比报告")
    print("=" * 60)

    reports = load_semantic_reports()
    if not reports:
        print(f"✗ 未找到语义报告：{SEMANTIC_DIR}/*.md")
        sys.exit(1)
    total_units = sum(len(r["units"]) for r in reports)
    total_gaps = sum(len(r["gaps"]) for r in reports)
    print(f"  解析语义报告 {len(reports)} 份：候选单元 {total_units} 个，缺口 {total_gaps} 个")

    skill_md = find_skill_md(args.skill)
    if not skill_md:
        print("✗ 未找到 compiled_skill/<skill>/SKILL.md")
        sys.exit(1)
    skill_name = skill_md.parent.name
    skill_ref = parse_skill_references(skill_md)
    print(f"  解析 SKILL.md：{skill_name}（维度 {len(skill_ref['dims'])} 个，"
          f"引用语义单元 {len(skill_ref['referenced_units'])} 个）")

    cov = compute_coverage(reports, skill_ref)
    write_reports(cov, reports, skill_md, skill_name)

    uc, gc = cov["unit_coverage"], cov["gap_coverage"]
    print(f"\n  单元采纳率：{uc['adopt_rate']}%（{uc['adopted']}/{uc['total_units']}）")
    print(f"  GAP 消解率：{gc['resolve_rate']}%（{gc['resolved']}/{gc['total_gaps']}）")
    print(f"\n✓ 报告已写入：{OUT_DIR.relative_to(PROJECT_ROOT)}/SEMANTIC_COVERAGE.md")


if __name__ == "__main__":
    main()
