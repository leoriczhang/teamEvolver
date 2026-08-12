// ============================================================
// 控制台前端逻辑：配置 → 运行 → SSE 实时进度 → 检查点提问 → 评估
// ============================================================

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

const state = {
  schema: null,
  evtSource: null,
  running: false,
  lastSeq: 0,           // 已处理的最大事件 seq，用于去重（防重放/重连翻倍）
  currentQuestionId: null, // 当前弹窗对应的提问 id，提交答案时回传给后端校验
};

// ---------- 初始化：拉取配置 schema ----------
async function loadConfig() {
  const res = await fetch("/api/config");
  const schema = await res.json();
  state.schema = schema;

  // 输入目录
  const inp = $("#inputDir");
  inp.innerHTML = "";
  schema.input_dirs.forEach((d) => {
    const o = document.createElement("option");
    o.value = d; o.textContent = d;
    if (d === schema.default_input_dir) o.selected = true;
    inp.appendChild(o);
  });

  // 轮数
  $("#maxRounds").value = schema.max_rounds_default;
  $("#roundsVal").textContent = schema.max_rounds_default;
  const [lo, hi] = schema.max_rounds_range;
  $("#maxRounds").min = lo; $("#maxRounds").max = hi;

  // 模型信息
  const m = schema.model || {};
  $("#modelInfo").textContent = (m.id || "未探测到模型") + (m.base_url ? `\n${m.base_url}` : "");

  // 检查点
  const list = $("#checkpointList");
  list.innerHTML = "";
  schema.checkpoints.forEach((c) => {
    const wrap = document.createElement("label");
    wrap.className = "check-item";
    wrap.innerHTML = `
      <input type="checkbox" data-key="${c.key}" checked />
      <div>
        <div class="ci-title">${c.label}</div>
        <div class="ci-desc">${c.desc}</div>
      </div>`;
    list.appendChild(wrap);
  });

  // 评测与报告：benchmark 默认值 + 跑分方式
  const bm = schema.benchmark || {};
  if (bm.default_dist !== undefined) $("#benchDist").value = bm.default_dist;
  if (bm.default_total) $("#benchTotal").value = bm.default_total;
  const bmSel = $("#benchMode");
  bmSel.innerHTML = "";
  (bm.modes || [{ value: "dialogue", label: "多轮对话" }]).forEach((m) => {
    const o = document.createElement("option");
    o.value = m.value; o.textContent = m.label;
    bmSel.appendChild(o);
  });
}

// ---------- 收集配置 ----------
function collectConfig() {
  const checkpoints = {};
  $$('#checkpointList input[type="checkbox"]').forEach((cb) => {
    checkpoints[cb.dataset.key] = cb.checked;
  });
  return {
    input_dir: $("#inputDir").value,
    max_rounds: parseInt($("#maxRounds").value, 10),
    ask_enabled: $("#askEnabled").checked,
    checkpoints,
    model: state.schema ? state.schema.model : {},
  };
}

// ---------- 状态 pill ----------
function setStatus(s) {
  const pill = $("#statusPill");
  const map = {
    idle: ["空闲", "pill-idle"],
    running: ["运行中", "pill-running"],
    waiting: ["等待你回答", "pill-waiting"],
    done: ["已完成", "pill-done"],
    error: ["出错", "pill-error"],
  };
  const [txt, cls] = map[s] || map.idle;
  pill.textContent = txt;
  pill.className = "pill " + cls;

  state.running = s === "running" || s === "waiting";
  $("#runBtn").disabled = state.running;
  $("#stopBtn").disabled = !state.running;
}

// ---------- 流程节点点亮 ----------
function setNode(node, cls) {
  const el = document.querySelector(`.node[data-node="${node}"]`);
  if (el) {
    el.classList.remove("active", "done");
    if (cls) el.classList.add(cls);
  }
  if (node === "reflect") {
    const lbl = document.querySelector('.loop-label[data-node="reflect"]');
    if (lbl) lbl.classList.toggle("active", cls === "active");
  }
}
function resetNodes() {
  ["step1", "step2", "step3"].forEach((n) => setNode(n, null));
  document.querySelector('.loop-label[data-node="reflect"]').classList.remove("active");
}

// ---------- 日志 ----------
function appendLog(msg, level, ts) {
  const stream = $("#logStream");
  const line = document.createElement("div");
  line.className = "log-line" + (level === "warn" ? " log-warn" : level === "error" ? " log-error" : "");
  if (/【本轮评估】|=====|运行结束|✓ 运行/.test(msg)) line.classList.add("hl");
  line.innerHTML = `<span class="lt">${ts || ""}</span>${escapeHtml(msg)}`;
  stream.appendChild(line);
  stream.scrollTop = stream.scrollHeight;
}
function escapeHtml(s) {
  return String(s).replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
}

// ---------- 逐轮评估卡片 ----------
function addRoundEval(ev) {
  const box = $("#rounds");
  const empty = box.querySelector(".empty");
  if (empty) empty.remove();

  const badgeCls = ev.confidence === "生产级" ? "badge-prod"
    : ev.confidence === "候选级" ? "badge-cand"
    : ev.confidence === "草稿级" ? "badge-draft" : "badge-unknown";

  const card = document.createElement("div");
  card.className = "round-card";
  card.innerHTML = `
    <div class="rc-head">
      <span class="rc-title">第 ${ev.round} 轮</span>
      <span class="badge ${badgeCls}">${ev.confidence}</span>
    </div>
    <div class="rc-gaps">缺口 ${ev.gap_count} 项${ev.gaps && ev.gaps.length ? "：" + ev.gaps.join("、") : ""}</div>`;
  box.appendChild(card);
}

// ---------- 提问弹窗（阻塞式）：渲染系统发现的具体缺口问题清单 ----------
function showQuestion(ev) {
  state.currentQuestionId = ev.id || null;
  const tagMap = {
    after_semantic: "Skill 生成前关键补证",
    after_compile: "编译后校验",
    on_gap_low_confidence: "缺口补充",
    before_reflection: "反思前 · 指定优先级",
  };
  $("#qTag").textContent = `知识补证 · ${tagMap[ev.checkpoint] || ev.checkpoint} · 第 ${ev.round} 轮`;
  $("#qTitle").textContent = ev.title || "请补充以下内容";
  $("#qIntro").textContent = ev.intro || "";

  const sevCls = (s) => s === "高" ? "q-sev-high" : s === "中" ? "q-sev-mid" : "q-sev-low";
  const list = $("#qList");
  list.innerHTML = "";
  (ev.questions || []).forEach((q, i) => {
    const item = document.createElement("div");
    item.className = "q-item";
    const sev = q.severity ? `<span class="q-sev ${sevCls(q.severity)}">${q.severity}严重度</span>` : "";
    const dim = q.dimension ? `<span class="q-dim">${escapeHtml(q.dimension)}</span>` : "";
    const context = q.context ? `<div class="q-context">${escapeHtml(q.context)}</div>` : "";
    const source = q.source ? `<div class="q-source">参考来源：${escapeHtml(q.source)}</div>` : "";
    const fieldLabel = escapeHtml(q.field_label || "你的回答");
    const placeholder = escapeHtml(q.placeholder || "请填写明确结论、适用条件和例外");
    const rows = q.answer_type === "short_text" ? 1 : 3;
    item.innerHTML = `
      <div class="q-head">
        <span class="q-num">${i + 1}</span>${sev}${dim}
      </div>
      <div class="q-text">${escapeHtml(q.question)}</div>
      ${context}${source}
      <label class="q-field-label" for="answer-${escapeHtml(q.qid)}">${fieldLabel}</label>
      <textarea id="answer-${escapeHtml(q.qid)}" rows="${rows}" data-qid="${escapeHtml(q.qid)}" placeholder="${placeholder}"></textarea>`;
    list.appendChild(item);
  });

  // 反思前检查点允许「结束反思环」
  const stopRow = $("#qStopRow");
  stopRow.classList.toggle("hidden", !ev.allow_stop);
  $("#qStop").checked = false;

  $("#modalOverlay").classList.remove("hidden");
  setTimeout(() => {
    const first = list.querySelector("textarea");
    if (first) first.focus();
  }, 50);
}
function hideQuestion() {
  $("#modalOverlay").classList.add("hidden");
  state.currentQuestionId = null;
}
async function submitAnswer(skipAll) {
  // 立刻隐藏当前弹窗（同步）：避免与后端紧接着推来的下一个 question 事件竞态。
  const questionId = state.currentQuestionId;
  const answers = {};
  if (!skipAll) {
    $$('#qList textarea').forEach((ta) => {
      const v = ta.value.trim();
      if (v) answers[ta.dataset.qid] = v;
    });
  }
  const stop = $("#qStop").checked;
  hideQuestion();
  try {
    const res = await fetch("/api/answer", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question_id: questionId, answers, stop }),
    });
    let data = null;
    try { data = await res.json(); } catch (_) {}
    if (!data || !data.ok) {
      appendLog("提交答案未被接受：" + ((data && data.msg) || `HTTP ${res.status}`), "warn", "");
    }
  } catch (e) {
    appendLog("提交答案请求异常：" + e, "error", "");
  }
}

// ---------- 面板清空（由后端 reset 事件驱动，新任务开始时触发） ----------
function resetPipelineUI() {
  $("#rounds").innerHTML = '<p class="empty">尚未运行</p>';
  $("#logStream").innerHTML = "";
  $("#finalCard").classList.add("hidden");
  resetNodes();
  hideQuestion();
}
function resetBenchUI() {
  $("#logStream").innerHTML = "";
  $("#benchResult").classList.add("hidden");
  resetBenchPhases();
}

// ---------- SSE 事件处理 ----------
function handleEvent(ev) {
  switch (ev.type) {
    case "reset":
      // 新任务开始：按 scope 清空对应面板（benchmark 保留流水线结果展示）
      if (ev.scope === "benchmark") resetBenchUI();
      else resetPipelineUI();
      break;
    case "status":
      setStatus(ev.state);
      break;
    case "log":
      appendLog(ev.msg, ev.level, ev.ts);
      break;
    case "round_start":
      resetNodes();
      break;
    case "phase":
      if (ev.state === "active") setNode(ev.phase, "active");
      else if (ev.state === "done") setNode(ev.phase, "done");
      break;
    case "round_eval":
      addRoundEval(ev);
      break;
    case "question":
      setNode("reflect", "active");
      showQuestion(ev);
      break;
    case "answer_ack":
      setNode("reflect", null);
      // 关键：重放历史时，一个 question 事件后紧跟它的 answer_ack —— 在此隐藏，
      // 使「已回答过的问题」成对抵消，只有真正未回答的问题才会残留在屏幕上。
      hideQuestion();
      break;
    case "done":
      resetNodes();
      ["step1", "step2", "step3"].forEach((n) => setNode(n, "done"));
      hideQuestion();
      showFinal(ev);
      break;
    case "error":
      appendLog(ev.msg, "error", ev.ts);
      break;
    // ---- 评测与报告事件 ----
    case "bench_status":
      if (ev.state === "running") setStatus("running");
      break;
    case "bench_phase":
      setBenchPhase(ev.phase, ev.state);
      break;
    case "bench_result":
      renderBenchResult(ev);
      break;
    case "bench_done":
      setStatus("done");
      setBenchRunning(false);
      break;
    case "bench_error":
      appendLog(ev.msg, "error", ev.ts);
      setStatus("error");
      setBenchRunning(false);
      break;
  }
}

function showFinal(ev) {
  const card = $("#finalCard");
  card.classList.remove("hidden");
  let body = `终止原因：${ev.stop_reason || "—"}`;
  if (ev.final) body += `<br/>最终置信档：<b>${ev.final.confidence}</b> · 剩余缺口 ${ev.final.gap_count} 项`;
  $("#finalBody").innerHTML = body;
}

function connectSSE() {
  // 整个页面生命周期只建一条连接：断线由浏览器自动重连，并通过
  // Last-Event-ID 从断点续传；seq 去重兜底，事件流全程幂等。
  if (state.evtSource) state.evtSource.close();
  const es = new EventSource("/api/events");
  es.onmessage = (e) => {
    let ev;
    try { ev = JSON.parse(e.data); } catch (_) { return; }
    if (typeof ev.seq === "number") {
      if (ev.seq <= state.lastSeq) return; // 已处理过，跳过（防重放翻倍）
      state.lastSeq = ev.seq;
    }
    handleEvent(ev);
  };
  es.onerror = () => { /* 浏览器会自动重连，并自动带上 Last-Event-ID */ };
  state.evtSource = es;
}

// ---------- 运行 / 中止 ----------
async function run() {
  // 本地先清一次（即时反馈）；权威清空由后端 reset 事件驱动（所有客户端同步）
  resetPipelineUI();

  const cfg = collectConfig();
  const res = await fetch("/api/run", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(cfg),
  });
  const data = await res.json();
  if (!data.ok) {
    appendLog("无法启动：" + data.msg, "error", "");
  }
}

async function stop() {
  await fetch("/api/stop", { method: "POST" });
}

// ============================================================
// 评测与报告：benchmark 跑分 + 语义覆盖报告
// ============================================================
function setBenchRunning(on) {
  $("#benchBtn").disabled = on;
  $("#coverageBtn").disabled = on;
  $("#benchBtn").textContent = on ? "跑分中…" : "构建 + 跑分";
}

function setBenchPhase(phase, st) {
  $("#benchPhases").classList.remove("hidden");
  const el = document.querySelector(`.ephase[data-ephase="${phase}"]`);
  if (!el) return;
  el.classList.remove("active", "done");
  if (st === "active") el.classList.add("active");
  else if (st === "done") el.classList.add("done");
}
function resetBenchPhases() {
  ["build", "run", "aggregate"].forEach((p) => {
    const el = document.querySelector(`.ephase[data-ephase="${p}"]`);
    if (el) el.classList.remove("active", "done");
  });
}

async function runBenchmark() {
  const body = {
    difficulty_dist: $("#benchDist").value.trim(),
    target_total: parseInt($("#benchTotal").value, 10) || 18,
    mode: $("#benchMode").value,
    skip_build: $("#benchSkipBuild").checked,
  };
  $("#benchResult").classList.add("hidden");
  resetBenchPhases();
  setBenchRunning(true);
  const res = await fetch("/api/benchmark", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await res.json();
  if (!data.ok) {
    appendLog("无法启动 benchmark：" + data.msg, "error", "");
    setBenchRunning(false);
  }
}

function renderBenchResult(ev) {
  const box = $("#benchResult");
  box.classList.remove("hidden");
  const oc = ev.overall_counts || {};
  let diffTable = `<table class="rt"><thead><tr><th>难度</th>`;
  if (ev.has_target) diffTable += `<th>目标题数</th><th>目标占比</th>`;
  diffTable += `<th>实际题数</th><th>实际占比</th><th>该档得分率</th></tr></thead><tbody>`;
  (ev.difficulty_rows || []).forEach((r) => {
    diffTable += `<tr><td>${r.level}</td>`;
    if (ev.has_target) diffTable += `<td>${r.target}</td><td>${r.target_pct}%</td>`;
    const sr = r.score_rate == null ? "—" : r.score_rate + "%";
    diffTable += `<td>${r.actual}</td><td>${r.actual_pct}%</td><td>${sr}</td></tr>`;
  });
  diffTable += `</tbody></table>`;

  const dlg = ev.dialogue_n
    ? `<div class="rt-sub">多轮对话 ${ev.dialogue_n} 题 · 客户满意收尾 ${ev.ended_ok} · 平均 ${ev.avg_turns ?? "—"} 轮</div>`
    : "";

  box.innerHTML = `
    <div class="report-head">📊 Benchmark 跑分 · ${escapeHtml(ev.skill || "")}</div>
    <div class="metric-row">
      <div class="metric"><div class="m-val">${ev.bench_score}</div><div class="m-lab">Benchmark Score</div></div>
      <div class="metric"><div class="m-val">${ev.pass_rate}%</div><div class="m-lab">通过率</div></div>
      <div class="metric"><div class="m-val">${ev.n}</div><div class="m-lab">题量</div></div>
      <div class="metric"><div class="m-val">${ev.safety_pass}/${ev.safety_known}</div><div class="m-lab">安全门槛</div></div>
    </div>
    <div class="rt-sub">总评分布：优秀 ${oc["优秀"] || 0} · 合格 ${oc["合格"] || 0} · 不合格 ${oc["不合格"] || 0}</div>
    ${dlg}
    <div class="rt-title">难度分布${ev.has_target ? "（目标 vs 实际）" : "（实际）"}</div>
    ${diffTable}
    <div class="rt-foot">详见 benchmark_results/REPORT.md</div>`;
}

async function runCoverage() {
  $("#coverageResult").classList.add("hidden");
  $("#coverageBtn").disabled = true;
  $("#coverageBtn").textContent = "解析中…";
  try {
    const res = await fetch("/api/coverage", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
    const data = await res.json();
    if (data.ok) renderCoverage(data.coverage);
    else appendLog("覆盖报告失败：" + data.msg, "error", "");
  } catch (e) {
    appendLog("覆盖报告请求异常：" + e, "error", "");
  } finally {
    $("#coverageBtn").disabled = false;
    $("#coverageBtn").textContent = "生成语义覆盖报告";
  }
}

function renderCoverage(cov) {
  const box = $("#coverageResult");
  box.classList.remove("hidden");
  const u = cov.unit, g = cov.gap;

  const retRows = ["高", "中", "低", "未标注"].map((lv) => {
    const b = u.by_retention[lv];
    if (!b || b.total === 0) return "";
    const rate = b.rate == null ? "—" : b.rate + "%";
    return `<tr><td>${lv}</td><td>${b.total}</td><td>${b.adopted}</td><td>${rate}</td></tr>`;
  }).join("");

  const sevRows = ["高", "中", "低", "未标注"].map((lv) => {
    const b = g.by_severity[lv];
    if (!b || b.total === 0) return "";
    const rate = b.rate == null ? "—" : b.rate + "%";
    return `<tr><td>${lv}</td><td>${b.total}</td><td>${b.resolved}</td><td>${rate}</td></tr>`;
  }).join("");

  const dimRows = (cov.dim || []).map((d) =>
    `<tr><td>${escapeHtml(d.dimension)}</td><td>${d.unit_count}</td><td>${d.pkg_count}</td>
     <td class="rt-refs">${escapeHtml((d.unit_refs || []).join("、"))}</td></tr>`
  ).join("");

  const dropped = (u.dropped_units || []).length
    ? `<div class="rt-sub">未采纳单元：${(u.dropped_units).map((x) => escapeHtml(x.ref + " " + x.name)).join("；")}</div>`
    : "";

  box.innerHTML = `
    <div class="report-head">🧬 语义覆盖占比 · ${escapeHtml(cov.skill || "")}</div>
    <div class="metric-row">
      <div class="metric"><div class="m-val">${u.adopt_rate}%</div><div class="m-lab">单元采纳率</div></div>
      <div class="metric"><div class="m-val">${u.adopted}/${u.total}</div><div class="m-lab">采纳/总数</div></div>
      <div class="metric"><div class="m-val">${g.resolve_rate}%</div><div class="m-lab">GAP 消解率</div></div>
      <div class="metric"><div class="m-val">${g.resolved}/${g.total}</div><div class="m-lab">消解/总数</div></div>
    </div>
    <div class="rt-title">① 语义单元采纳率（按保留等级）</div>
    <table class="rt"><thead><tr><th>保留等级</th><th>单元数</th><th>已采纳</th><th>采纳率</th></tr></thead><tbody>${retRows}</tbody></table>
    ${dropped}
    <div class="rt-title">② GAP 消解率（按严重度）</div>
    <table class="rt"><thead><tr><th>严重度</th><th>缺口数</th><th>已消解</th><th>消解率</th></tr></thead><tbody>${sevRows}</tbody></table>
    <div class="rt-title">③ 维度级证据覆盖</div>
    <table class="rt"><thead><tr><th>能力维度</th><th>支撑单元</th><th>覆盖包数</th><th>引用单元</th></tr></thead><tbody>${dimRows}</tbody></table>
    <div class="rt-foot">详见 coverage_reports/SEMANTIC_COVERAGE.md</div>`;
}

// ---------- 事件绑定 ----------
function bind() {
  $("#maxRounds").addEventListener("input", (e) => {
    $("#roundsVal").textContent = e.target.value;
  });
  $("#askEnabled").addEventListener("change", (e) => {
    $("#checkpointsWrap").classList.toggle("disabled", !e.target.checked);
  });
  $("#runBtn").addEventListener("click", run);
  $("#stopBtn").addEventListener("click", stop);
  $("#benchBtn").addEventListener("click", runBenchmark);
  $("#coverageBtn").addEventListener("click", runCoverage);
  $("#qSubmit").addEventListener("click", () => submitAnswer(false));
  $("#qSkip").addEventListener("click", () => submitAnswer(true));
  // Cmd/Ctrl+Enter 快速提交整份问卷
  $("#qList").addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter") submitAnswer(false);
  });
}

// ---------- 启动 ----------
(async function init() {
  bind();
  await loadConfig();
  connectSSE();
})();
