#!/usr/bin/env python3
"""
多次 benchmark 交集 + 多 session 复跑（Hermes 版）

场景：
  你会**独立**跑 N 次（默认 3 次）benchmark 题库构建（`python3 run_benchmark.py --build-only`），
  每次出的题目可能不一样。本脚本负责在这 N 次之上做“稳定性复跑”：

    1) snapshot ：每次构建完成后，把当前 benchmark.jsonl 快照留存为一个 build 版本；
    2) intersect：按 **情境文本相似度** 判定哪些题在 N 次构建里“都出现”（= 三次都覆盖到的样例），
                  以第 1 个 build 的版本作为该题的代表（canonical）；
    3) run      ：对每道交集题，跑 **M 个完整多轮 session**（默认 3 个），
                  每个 session 的**完整对话过程**单独留档，并逐题/整体汇总。

  → 例如：3 道题都在三次构建里出现，每题跑 3 个 session，就是 3×3 = 9 个 session。

子命令：
  snapshot   把当前 benchmark.jsonl 存为一个 build 快照（--slot 指定序号，默认自增）
  status     打印已有快照与交集概况（不调用模型）
  intersect  计算交集并写出交集清单（不调用模型）
  run        计算交集（若未算）并对每道交集题各跑 M 个 session（**会调用模型**）

设计原则：
  - 只读地复用 run_benchmark.py 的对话引擎（run_dialogue）、阅卷（judge_prompt_dialogue/parse_verdict）
    与留档渲染（render_transcript），不重复造轮子；
  - snapshot / status / intersect **不调用模型**，可随时安全执行（不影响正在跑的重建）；
  - run 才会调用模型，请在你自己的重建结束后再执行，避免与其抢占额度 / 部署目录。

产物目录：benchmark_sessions/
  snapshots/build-1.jsonl ...        每次构建的题库快照
  intersection.json / .md            交集题清单（含相似度与各 build 的 id 对应）
  <QKEY>/session-1.md ... -M.md      每道交集题的每个 session 完整对话 + 阅卷
  <QKEY>/summary.json                该题 M 个 session 的裁决汇总与稳定性
  SESSIONS_REPORT.md                 整体报告
"""

import sys
import json
import time
import hashlib
import argparse
import difflib
from pathlib import Path
from datetime import datetime

# 复用已验证的题库/对话/阅卷逻辑
import run_benchmark as rb
rst = rb.rst          # run_skill_test
rp = rst.rp           # run_pipeline

PROJECT_ROOT = Path(__file__).parent.resolve()
SESS_DIR = PROJECT_ROOT / "benchmark_sessions"
SNAP_DIR = SESS_DIR / "snapshots"

DEFAULT_SESSIONS = 3        # 每道交集题跑几个 session
DEFAULT_SIM_THRESHOLD = 0.6  # 情境文本相似度阈值（>= 视为同一道题）


# ============================================================
# 文本相似度：判定“同一道题在多次构建里都出现”
# ============================================================
def _normalize_text(s):
    """情境文本归一化：去空白、去常见标点/括号标记，仅保留可比字符。

    目的：抹平三次出题在标点、空格、全半角上的细微差异，让相似度更聚焦语义骨架。
    """
    if not s:
        return ""
    drop = set(" \t\r\n　，,。.；;：:！!？?、（）()【】[]「」“”\"'《》<>—-…·")
    return "".join(ch for ch in s if ch not in drop).lower()


def similarity(a, b):
    """两段情境文本的相似度（0~1）。基于归一化后的 difflib 序列比。"""
    na, nb = _normalize_text(a), _normalize_text(b)
    if not na or not nb:
        return 0.0
    return difflib.SequenceMatcher(None, na, nb).ratio()


def _qkey(canonical):
    """交集题的稳定 key：build-1 的 id + 情境文本短哈希（跨 build 的 id 不稳定，故加哈希）。"""
    h = hashlib.md5(_normalize_text(canonical["input"]).encode("utf-8")).hexdigest()[:6]
    qid = (canonical.get("id") or "Q").replace("/", "_")
    return f"{qid}_{h}"


# ============================================================
# 快照
# ============================================================
def _find_skill_dir():
    skill_dir = rst.find_skill_to_test()
    if not skill_dir:
        print("✗ compiled_skill/ 下没有找到含 SKILL.md 的 skill")
        sys.exit(1)
    return skill_dir


def _load_jsonl_questions(path):
    """读一个 jsonl 题库并做规整（补齐 customer_sim 等字段）。"""
    if not path.exists():
        return None
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    return rb.normalize_questions(rows)


def _snapshot_slots():
    """已有快照序号（升序）。"""
    if not SNAP_DIR.exists():
        return []
    slots = []
    for p in SNAP_DIR.glob("build-*.jsonl"):
        try:
            slots.append(int(p.stem.split("-", 1)[1]))
        except Exception:
            continue
    return sorted(slots)


def cmd_snapshot(args):
    """把当前 benchmark.jsonl 存为一个 build 快照。"""
    skill_dir = _find_skill_dir()
    src = Path(args.from_file) if args.from_file else (skill_dir / "benchmark.jsonl")
    if not src.exists():
        print(f"✗ 找不到题库文件：{src}")
        print("  提示：请先跑 `python3 run_benchmark.py --build-only` 生成 benchmark.jsonl")
        sys.exit(1)

    questions = _load_jsonl_questions(src)
    if not questions:
        print(f"✗ 题库为空或解析失败：{src}")
        sys.exit(1)

    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    slot = args.slot if args.slot else (max(_snapshot_slots(), default=0) + 1)
    dst = SNAP_DIR / f"build-{slot}.jsonl"

    # 直接落规整后的题库，保证后续 intersect/run 读到统一结构
    with dst.open("w", encoding="utf-8") as f:
        for q in questions:
            f.write(json.dumps(q, ensure_ascii=False) + "\n")

    # 记录快照元信息
    meta = {
        "slot": slot,
        "captured_at": datetime.now().isoformat(timespec="seconds"),
        "source": str(src.relative_to(PROJECT_ROOT)) if src.is_relative_to(PROJECT_ROOT) else str(src),
        "n_questions": len(questions),
    }
    (SNAP_DIR / f"build-{slot}.meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"✓ 已保存快照 build-{slot}：{len(questions)} 道题 -> {dst.relative_to(PROJECT_ROOT)}")
    print(f"  当前快照序号：{_snapshot_slots()}")


def _load_all_snapshots():
    """读回全部快照，返回 [(label, questions), ...]，按序号升序。"""
    snaps = []
    for slot in _snapshot_slots():
        qs = _load_jsonl_questions(SNAP_DIR / f"build-{slot}.jsonl")
        if qs:
            snaps.append((f"build-{slot}", qs))
    return snaps


# ============================================================
# 交集
# ============================================================
def compute_intersection(snapshots, threshold):
    """按情境文本相似度求“所有 build 都出现”的题。

    以第 1 个 build 为基准，为其每道题在其余每个 build 里找最相似的题；
    只要在**每个**其余 build 里都能找到相似度 >= 阈值的题，就算“都出现”。
    代表版本(canonical)取基准 build 的那道题。
    """
    if len(snapshots) < 2:
        return []
    base_label, base_qs = snapshots[0]
    others = snapshots[1:]
    triples = []
    for bq in base_qs:
        matches = {base_label: {"id": bq.get("id"), "score": 1.0}}
        ok = True
        for lbl, oqs in others:
            best, best_r = None, 0.0
            for oq in oqs:
                r = similarity(bq["input"], oq["input"])
                if r > best_r:
                    best_r, best = r, oq
            if best is not None and best_r >= threshold:
                matches[lbl] = {"id": best.get("id"), "score": round(best_r, 3)}
            else:
                ok = False
                break
        if ok:
            triples.append({
                "qkey": _qkey(bq),
                "canonical": bq,
                "matches": matches,
                "min_score": round(min(m["score"] for m in matches.values()), 3),
            })
    return triples


def compute_intersection_by_dimension(snapshots):
    """按 **考核维度** 求“三次构建都覆盖到”的交集。

    背景：每次构建都会重新措辞情境，纯文本相似度普遍偏低（实测三次构建两两最高仅 ~0.43），
    因此“同一句话反复出现”几乎不可能。真正稳定复现的，是**能力维度**：EVAL-01..04 每次都被覆盖。
    本函数以“维度”为交集单元——取所有 build 都出现的 target_dimension，
    每个共同维度用 build-1 里命中该维度的第一道题作为代表（canonical），
    并记录其余 build 中命中同维度的一道题作为对应项。
    """
    if len(snapshots) < 2:
        return []
    # 各 build 覆盖的维度集合
    dim_sets = []
    for _, qs in snapshots:
        s = set()
        for q in qs:
            for d in q.get("target_dimensions", []):
                s.add(d)
        dim_sets.append(s)
    common = set.intersection(*dim_sets) if dim_sets else set()

    def first_q_for_dim(qs, dim):
        for q in qs:
            if dim in q.get("target_dimensions", []):
                return q
        return None

    _, base_qs = snapshots[0]
    triples = []
    for dim in sorted(common):
        canonical = first_q_for_dim(base_qs, dim)
        if canonical is None:
            continue
        matches = {}
        for lbl, qs in snapshots:
            mq = first_q_for_dim(qs, dim)
            matches[lbl] = {"id": (mq.get("id") if mq else None), "score": 1.0}
        h = hashlib.md5(dim.encode("utf-8")).hexdigest()[:6]
        triples.append({
            "qkey": f"DIM_{dim.replace('/', '_')}_{h}",
            "canonical": canonical,
            "matches": matches,
            "min_score": 1.0,
        })
    return triples


def _write_intersection(triples, snapshots, threshold, mode="text"):
    SESS_DIR.mkdir(parents=True, exist_ok=True)
    labels = [lbl for lbl, _ in snapshots]
    payload = {
        "computed_at": datetime.now().isoformat(timespec="seconds"),
        "builds": labels,
        "mode": mode,
        "sim_threshold": threshold,
        "n_intersection": len(triples),
        "items": [
            {
                "qkey": t["qkey"],
                "target_dimensions": t["canonical"]["target_dimensions"],
                "difficulty": t["canonical"]["difficulty"],
                "source": t["canonical"].get("source", ""),
                "matches": t["matches"],
                "min_score": t["min_score"],
                "input_preview": t["canonical"]["input"][:80],
            }
            for t in triples
        ],
    }
    (SESS_DIR / "intersection.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    mode_note = {
        "text": f"情境文本相似度（阈值 {threshold}）——同一措辞在多次构建里复现",
        "dimension": "考核维度覆盖——多次构建都触及的能力维度（EVAL-*）；"
                     "情境每次重新措辞、文本相似度普遍偏低，故以维度为稳定的交集单元",
    }.get(mode, mode)
    lines = [
        "# Benchmark 交集清单（多次构建都出现的题）",
        "",
        f"- 计算时间：{payload['computed_at']}",
        f"- 参与 build：{'、'.join(labels)}",
        f"- 交集口径：{mode_note}",
        f"- 交集题数：**{len(triples)}**",
        "",
        "| QKEY | 难度 | 维度 | 各 build 对应 id | 最低相似度 | 情境预览 |",
        "|------|------|------|------------------|-----------|----------|",
    ]
    for t in triples:
        ids = " / ".join(f"{lbl}:{m['id']}({m['score']})" for lbl, m in t["matches"].items())
        dims = "、".join(t["canonical"]["target_dimensions"]) or "-"
        prev = t["canonical"]["input"][:40].replace("\n", " ")
        lines.append(f"| {t['qkey']} | {t['canonical']['difficulty']} | {dims} | {ids} "
                     f"| {t['min_score']} | {prev}… |")
    (SESS_DIR / "intersection.md").write_text("\n".join(lines), encoding="utf-8")


def _resolve_intersection(snaps, sim_threshold, force_dimension=False):
    """统一的交集解析：
      - 默认先按情境文本相似度求交集；
      - 若文本交集为空（三次构建每次都重新措辞、相似度普遍偏低），
        自动回退到 **按考核维度** 求交集，保证 run 有稳定可跑的共同项；
      - --by-dimension 可强制走维度口径。
    返回 (triples, mode)。
    """
    if not force_dimension:
        triples = compute_intersection(snaps, sim_threshold)
        if triples:
            return triples, "text"
        print("  ⚠️ 文本相似度交集为空（三次构建情境措辞差异大），"
              "自动回退到「考核维度」口径求交集。")
    return compute_intersection_by_dimension(snaps), "dimension"


def cmd_intersect(args):
    snaps = _load_all_snapshots()
    if len(snaps) < 2:
        print(f"✗ 至少需要 2 个快照才能求交集，当前只有 {len(snaps)} 个。")
        print("  用法：每次 `run_benchmark.py --build-only` 后执行 `run_multi_session.py snapshot`")
        sys.exit(1)
    triples, mode = _resolve_intersection(snaps, args.sim_threshold,
                                          force_dimension=getattr(args, "by_dimension", False))
    _write_intersection(triples, snaps, args.sim_threshold, mode=mode)
    print(f"✓ 交集计算完成（口径：{mode}）：{len(snaps)} 个 build，交集 {len(triples)} 项")
    print(f"  清单：{(SESS_DIR / 'intersection.md').relative_to(PROJECT_ROOT)}")
    for t in triples:
        dims = "、".join(t["canonical"]["target_dimensions"]) or "-"
        print(f"    - {t['qkey']}  [{t['canonical']['difficulty']}] {dims}  最低相似度 {t['min_score']}")


def cmd_status(args):
    slots = _snapshot_slots()
    print(f"快照目录：{SNAP_DIR.relative_to(PROJECT_ROOT)}")
    if not slots:
        print("  （暂无快照）每次构建后执行：python3 run_multi_session.py snapshot")
        return
    for slot in slots:
        qs = _load_jsonl_questions(SNAP_DIR / f"build-{slot}.jsonl")
        meta_p = SNAP_DIR / f"build-{slot}.meta.json"
        cap = ""
        if meta_p.exists():
            try:
                cap = json.loads(meta_p.read_text(encoding="utf-8")).get("captured_at", "")
            except Exception:
                pass
        print(f"  build-{slot}: {len(qs) if qs else 0} 题  {cap}")
    inter = SESS_DIR / "intersection.json"
    if inter.exists():
        d = json.loads(inter.read_text(encoding="utf-8"))
        print(f"交集：{d.get('n_intersection')} 题（阈值 {d.get('sim_threshold')}，"
              f"builds={'、'.join(d.get('builds', []))}）")
    else:
        print("交集：尚未计算（运行 intersect 或 run）")


# ============================================================
# 多 session 复跑（会调用模型）
# ============================================================
def _bootstrap_for_run():
    """run 子命令的引导：定位 hermes、skill、凭据、EVALUATION，部署被测 skill。"""
    if not rp.check_hermes_installed():
        sys.exit(1)
    skill_dir = _find_skill_dir()
    skill_md = skill_dir / "SKILL.md"
    skill_name = rst.parse_skill_name(skill_md)
    print(f"被测 skill: {skill_name}")

    rp.ensure_hermes_home()
    hermes_env, has_key = rp.build_hermes_env()
    print("  ✓ 已注入 ARK_API_KEY" if has_key else "  ⚠️ 未找到 ARK_API_KEY，模型调用可能失败")
    if not rp.test_model_connection(hermes_env):
        print("  ✗ 模型连接测试未通过，已停止。请检查凭据、额度、网络与模型配置。")
        sys.exit(1)

    _, eval_text, _ = rb.load_skill_and_eval(skill_dir)
    print("[部署被测 skill]")
    rst.deploy_test_skill(skill_dir, skill_name)
    return skill_dir, skill_name, eval_text, hermes_env


def _run_one_session(skill_name, q, eval_text, hermes_env, max_turns, qdir, idx):
    """跑一个完整 session：多轮对话 -> 整体阅卷 -> 落一份完整对话留档。返回该 session 摘要。"""
    transcript, meta = rb.run_dialogue(skill_name, q, hermes_env, max_turns=max_turns)
    end_flag = "参与者认可收尾" if meta["ended_by_customer"] else "跑满轮数未收尾"

    ok_j, judge_out = rst.run_hermes(
        ["-z", rb.judge_prompt_dialogue(q, transcript, meta, eval_text)], hermes_env)
    verdict = rb.parse_verdict(judge_out) if ok_j else rb._blank_verdict()

    sim = q["customer_sim"]
    md = (
        f"# {qdir.name} · Session {idx}\n\n"
        f"- 被测题(canonical id)：{q.get('id')}\n"
        f"- 难度：{q['difficulty']}　维度：{'、'.join(q['target_dimensions'])}\n"
        f"- 本 session：{meta['turns']} 轮，{end_flag}\n"
        f"- 总评：**{verdict['overall']}**\n\n"
        f"## 情境\n\n{q['input']}\n\n"
        f"## 🎭 模拟参与者剧本（customer_sim，兼容字段名）\n\n"
        f"- 角色/情绪：{sim['persona']}\n"
        f"- 诉求目标：{sim['goal']}\n"
        f"- 隐藏事实（问到才说）：{sim['hidden_facts']}\n"
        f"- 透露规则：{sim['reveal_rules']}\n"
        f"- 施压手段：{sim['pressure_tactics']}\n"
        f"- 满意收尾条件：{sim['stop_when']}\n\n"
        f"## 💬 完整对话过程（🧑 情境参与者 ↔ 🎧 被测 skill）\n\n{rb.render_transcript(transcript)}\n\n"
        f"## ⚖️ 裁判评分（整体阅卷）\n\n{judge_out}\n"
    )
    (qdir / f"session-{idx}.md").write_text(md, encoding="utf-8")

    return {
        "session": idx,
        "turns": meta["turns"],
        "ended_by_customer": meta["ended_by_customer"],
        "agent_fail": meta["agent_fail"],
        "customer_fail": meta["customer_fail"],
        "overall": verdict["overall"],
        "safety_gate_passed": verdict["safety_gate_passed"],
        "judge_ok": ok_j,
    }


def cmd_run(args):
    # 先确保有交集
    snaps = _load_all_snapshots()
    if len(snaps) < 2:
        print(f"✗ 至少需要 2 个快照，当前 {len(snaps)} 个。请先多次 build 并 snapshot。")
        sys.exit(1)
    triples, mode = _resolve_intersection(snaps, args.sim_threshold,
                                          force_dimension=getattr(args, "by_dimension", False))
    _write_intersection(triples, snaps, args.sim_threshold, mode=mode)
    if not triples:
        print("✗ 交集为空：三次构建既无文本相似达标题，也无共同考核维度。")
        sys.exit(1)
    # --only：只重跑匹配的交集项（按 qkey 或维度名做子串匹配）；其余项复用已有 summary.json，
    # 这样定向修题后重跑，不会浪费额度重算已完成的维度，报告仍保持完整。
    only = [s.strip() for s in (getattr(args, "only", "") or "").split(",") if s.strip()]

    def _matches_only(t):
        if not only:
            return True
        hay = [t["qkey"]] + t["canonical"].get("target_dimensions", [])
        return any(o in h for o in only for h in hay)

    to_run = [t for t in triples if _matches_only(t)]
    skipped = [t for t in triples if not _matches_only(t)]
    if only and not to_run:
        print(f"✗ --only {only} 未匹配到任何交集项。可选项：{[t['qkey'] for t in triples]}")
        sys.exit(1)
    if only:
        print(f"过滤 --only={only}：重跑 {len(to_run)} 项，复用 {len(skipped)} 项已有结果")
    print(f"本次将跑 {len(to_run)} 项（口径：{mode}），每项 {args.sessions} 个 session"
          f"（预计 {len(to_run) * args.sessions} 个 session）")

    skill_dir, skill_name, eval_text, hermes_env = _bootstrap_for_run()
    SESS_DIR.mkdir(parents=True, exist_ok=True)

    all_summary = []
    # 先把被跳过维度的既有结果读回来，保证报告完整
    for t in skipped:
        prev = SESS_DIR / t["qkey"] / "summary.json"
        if prev.exists():
            all_summary.append(json.loads(prev.read_text(encoding="utf-8")))
        else:
            print(f"  ⚠️ {t['qkey']} 无既有 summary.json，本次未跑，将不出现在报告中")

    for ti, t in enumerate(to_run, start=1):
        q = t["canonical"]
        qdir = SESS_DIR / t["qkey"]
        qdir.mkdir(parents=True, exist_ok=True)
        print(f"\n[{ti}/{len(to_run)}] {t['qkey']}　[{q['difficulty']}] "
              f"{'、'.join(q['target_dimensions'])}")

        sessions = []
        for i in range(1, args.sessions + 1):
            print(f"    session {i}/{args.sessions} ...", end="", flush=True)
            s = _run_one_session(skill_name, q, eval_text, hermes_env,
                                 args.max_turns, qdir, i)
            print(f" {s['turns']} 轮 -> {s['overall']}"
                  f"{'（满意收尾）' if s['ended_by_customer'] else '（未收尾）'}")
            sessions.append(s)

        overalls = [s["overall"] for s in sessions]
        stable = len(set(overalls)) == 1
        summary = {
            "qkey": t["qkey"],
            "canonical_id": q.get("id"),
            "difficulty": q["difficulty"],
            "target_dimensions": q["target_dimensions"],
            "matches": t["matches"],
            "min_similarity": t["min_score"],
            "sessions": sessions,
            "overall_distribution": {k: overalls.count(k) for k in set(overalls)},
            "stable_across_sessions": stable,
        }
        (qdir / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        all_summary.append(summary)

    all_summary.sort(key=lambda s: s["qkey"])
    _write_sessions_report(all_summary, snaps, args)
    print(f"\n✓ 完成：本次跑 {len(to_run)} 题 × {args.sessions} session"
          f"（报告含全部 {len(all_summary)} 个交集维度）")
    print(f"  报告：{(SESS_DIR / 'SESSIONS_REPORT.md').relative_to(PROJECT_ROOT)}")


def _write_sessions_report(all_summary, snaps, args):
    total_sessions = sum(len(s["sessions"]) for s in all_summary)
    ended = sum(1 for s in all_summary for x in s["sessions"] if x["ended_by_customer"])
    lines = [
        "# 多 Session 复跑报告",
        "",
        f"- 生成时间：{datetime.now().isoformat(timespec='seconds')}",
        f"- 参与 build 快照：{'、'.join(lbl for lbl, _ in snaps)}",
        f"- 交集题数：{len(all_summary)}　每题 session 数：{args.sessions}　"
        f"总 session：{total_sessions}",
        f"- 参与者认可收尾：{ended}/{total_sessions} 个 session",
        f"- 每轮对话上限：{args.max_turns}　相似度阈值：{args.sim_threshold}",
        "",
        "## 逐题稳定性",
        "",
        "| QKEY | 难度 | 维度 | 各 session 总评 | 是否稳定 | 平均轮数 |",
        "|------|------|------|-----------------|----------|----------|",
    ]
    for s in all_summary:
        overalls = " / ".join(x["overall"] for x in s["sessions"])
        avg_turns = round(sum(x["turns"] for x in s["sessions"]) / len(s["sessions"]), 1)
        stable = "✅ 一致" if s["stable_across_sessions"] else "⚠️ 有波动"
        dims = "、".join(s["target_dimensions"]) or "-"
        lines.append(f"| {s['qkey']} | {s['difficulty']} | {dims} | {overalls} "
                     f"| {stable} | {avg_turns} |")
    lines += [
        "",
        "> 说明：同一道题跑多个 session，用于观察 skill 在多轮对话下表现的**稳定性**——",
        "> 总评一致说明行为稳定；有波动则提示该情境下 skill 输出对随机性敏感，值得进一步加固。",
        "",
        "各题每个 session 的**完整对话过程**见对应目录下的 `session-N.md`。",
    ]
    (SESS_DIR / "SESSIONS_REPORT.md").write_text("\n".join(lines), encoding="utf-8")


# ============================================================
# CLI
# ============================================================
def parse_args():
    ap = argparse.ArgumentParser(
        description="多次 benchmark 交集 + 多 session 复跑")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("snapshot", help="把当前 benchmark.jsonl 存为一个 build 快照")
    sp.add_argument("--slot", type=int, default=None, help="快照序号（默认自增）")
    sp.add_argument("--from-file", dest="from_file", default=None,
                    help="指定题库来源文件（默认 compiled_skill/<skill>/benchmark.jsonl）")

    sub.add_parser("status", help="打印快照与交集概况（不调用模型）")

    ip = sub.add_parser("intersect", help="计算交集并写清单（不调用模型）")
    ip.add_argument("--sim-threshold", type=float, default=DEFAULT_SIM_THRESHOLD,
                    help=f"情境文本相似度阈值（默认 {DEFAULT_SIM_THRESHOLD}）")
    ip.add_argument("--by-dimension", dest="by_dimension", action="store_true",
                    help="强制按考核维度求交集（默认先按文本相似度，为空时自动回退到维度）")

    rpp = sub.add_parser("run", help="对交集题各跑 M 个 session（会调用模型）")
    rpp.add_argument("--sessions", type=int, default=DEFAULT_SESSIONS,
                     help=f"每道交集题跑几个 session（默认 {DEFAULT_SESSIONS}）")
    rpp.add_argument("--max-turns", type=int, default=rb.DEFAULT_MAX_TURNS,
                     help=f"每个 session 中被测 skill 最多回应几轮（默认 {rb.DEFAULT_MAX_TURNS}）")
    rpp.add_argument("--sim-threshold", type=float, default=DEFAULT_SIM_THRESHOLD,
                     help=f"情境文本相似度阈值（默认 {DEFAULT_SIM_THRESHOLD}）")
    rpp.add_argument("--by-dimension", dest="by_dimension", action="store_true",
                     help="强制按考核维度求交集（默认先按文本相似度，为空时自动回退到维度）")
    rpp.add_argument("--only", default="",
                     help="只重跑匹配的交集项（逗号分隔，按 qkey 或维度名子串匹配，"
                          "如 --only EVAL-01）；其余项复用已有 summary.json，报告仍完整")
    return ap.parse_args()


def main():
    args = parse_args()
    print("=" * 60)
    print("多次 benchmark 交集 + 多 session 复跑")
    print("=" * 60)
    if args.cmd == "snapshot":
        cmd_snapshot(args)
    elif args.cmd == "status":
        cmd_status(args)
    elif args.cmd == "intersect":
        cmd_intersect(args)
    elif args.cmd == "run":
        cmd_run(args)


if __name__ == "__main__":
    main()
