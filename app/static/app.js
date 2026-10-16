const $ = (s, root = document) => root.querySelector(s);
const $$ = (s, root = document) => [...root.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtNum = (n) => (n >= 10000 ? (n / 1000).toFixed(1) + "k" : Number(n ?? 0).toLocaleString());
const fmtAge = (min) => (min < 60 ? `${Math.max(1, Math.round(min))} 分钟` : min < 1440 ? `${(min / 60).toFixed(1)} 小时` : `${(min / 1440).toFixed(1)} 天`);
const fmtAgeShort = (min) => (min < 60 ? `${Math.round(min)}m` : `${(min / 60).toFixed(min < 600 ? 1 : 0)}h`);
const fmtTime = (ts) => (ts ? new Date(ts * 1000).toLocaleString("zh-CN", { hour12: false, month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }) : "—");
const ago = (ts) => (ts ? fmtAge((Date.now() / 1000 - ts) / 60) + "前" : "—");
const freshClass = (min) => (min < 60 ? "fresh-1" : min < 180 ? "fresh-2" : "fresh-3");
// Post time: absolute local time + how long ago, colour-coded by freshness.
const postTime = (p) => `<span class="ptime ${freshClass(p.age_min)}"><b>${fmtTime(p.created_utc)}</b><i>${fmtAge(p.age_min)}前</i></span>`;

const STATUS = {
  tracking: "跟踪中", candidate: "候选", drafted: "待使用", used: "已使用", discarded: "已丢弃",
  expired: "已过期", removed: "已删除", spam: "垃圾/广告", draft: "草稿", skipped: "未选用",
};
const TYPE = { text: "文字", link: "链接", image: "图片", video: "视频", gallery: "图集" };
const statusTag = (s) => `<span class="status ${s}">${STATUS[s] || s}</span>`;
const hotColor = (v) => (v >= 80 ? "#f85149" : v >= 60 ? "#ff8a4c" : v >= 40 ? "#d29922" : "#4da3ff");
const hotBar = (v) => `<div class="hot"><div class="bar"><div style="width:${Math.min(100, v)}%;background:${hotColor(v)}"></div></div><span class="n" style="color:${hotColor(v)}">${Math.round(v)}</span></div>`;
const IS_STATIC = Boolean(window.RADAR_STATIC || location.hostname.endsWith(".github.io"));
const proxied = (u) => (IS_STATIC ? "https://wsrv.nl/?url=" + encodeURIComponent(u) : "/api/image-proxy?url=" + encodeURIComponent(u));

let _bundleCache = { ts: 0, data: null };
async function _getStaticBundle(force = false) {
  const now = Date.now();
  if (force || !_bundleCache.data || now - _bundleCache.ts > 15000) {
    const r = await fetch(`static-bundle.json?t=${now}`, { cache: "no-store" });
    if (!r.ok) throw new Error("静态数据包加载失败");
    _bundleCache = { ts: now, data: await r.json() };
  }
  return _bundleCache.data;
}

function _localOverrides() {
  try { return JSON.parse(localStorage.getItem("radar.overrides") || "{}"); } catch { return {}; }
}
function _saveOverrides(ov) {
  localStorage.setItem("radar.overrides", JSON.stringify(ov));
}

async function _staticApi(path, opts = {}) {
  const b = await _getStaticBundle(opts.force);
  const ov = _localOverrides();
  const tweetOv = ov.tweets || {};
  const postOv = ov.posts || {};
  const method = (opts.method || "GET").toUpperCase();
  const [pathname, qs] = path.split("?");
  const params = new URLSearchParams(qs || "");

  const applyPost = (p) => ({ ...p, ...(postOv[p.id] || {}), age_min: Math.max(1, Math.round(((Date.now() / 1000 - p.created_utc) / 60) * 10) / 10) });
  const applyTweet = (t) => ({ ...t, ...(tweetOv[t.id] || {}) });

  if (method === "GET") {
    if (pathname === "/status") return b.status;
    if (pathname === "/overview") {
      const posts = (b.posts || []).map(applyPost);
      const tweets = (b.tweets || []).map(applyTweet);
      const pending = new Set(tweets.filter((t) => t.status === "draft").map((t) => t.post_id)).size;
      const valid = posts.filter((p) => ["tracking", "candidate", "drafted"].includes(p.status));
      const latest = [...valid].sort((x, y) => y.created_utc - x.created_utc).slice(0, 10);
      const top = [...valid].sort((x, y) => (y.hot_score || 0) - (x.hot_score || 0)).slice(0, 10);
      return { ...b.overview, pending_review: pending, top, latest };
    }
    if (pathname === "/usage") return b.usage;
    if (pathname === "/estimate") return b.estimate;
    if (pathname === "/settings") return { ...b.settings, ...(ov.settings || {}) };
    if (pathname === "/subreddits") return b.subreddits;
    if (pathname === "/posts") {
      const st = (params.get("status") || "").split(",").filter(Boolean);
      const sub = (params.get("subreddit") || "").toLowerCase();
      const sort = params.get("sort") || "hot";
      const q = (params.get("q") || "").toLowerCase();
      const maxH = Number(params.get("max_age_hours") || 12);
      const cutoff = (b.status?.server_time || Date.now() / 1000) - maxH * 3600;
      let list = (b.posts || []).map(applyPost).filter((p) => p.created_utc >= cutoff);
      if (st.length) list = list.filter((p) => st.includes(p.status));
      if (sub) list = list.filter((p) => (p.subreddit || "").toLowerCase() === sub);
      if (q) list = list.filter((p) => (p.title || "").toLowerCase().includes(q) || (p.title_zh || "").toLowerCase().includes(q));
      list.sort((x, y) => (sort === "new" ? y.created_utc - x.created_utc : sort === "comments" ? y.num_comments - x.num_comments : y.hot_score - x.hot_score));
      return list;
    }
    if (pathname.startsWith("/posts/")) {
      const id = pathname.split("/")[2];
      const d = b.post_details && b.post_details[id];
      if (!d) throw new Error("帖子不存在");
      return { ...d, post: applyPost(d.post), tweets: (d.tweets || []).map(applyTweet).filter((t) => ["draft", "used"].includes(t.status)) };
    }
    if (pathname === "/tweets") {
      const st = (params.get("status") || "draft").split(",").filter(Boolean);
      return (b.tweets || []).map(applyTweet).filter((t) => st.includes(t.status));
    }
    if (pathname === "/official") {
      const src = (params.get("source") || "").toLowerCase();
      const q = (params.get("q") || "").toLowerCase();
      let list = b.official_blogs || [];
      if (src) list = list.filter((item) => (item.source || "").toLowerCase().includes(src));
      if (q) list = list.filter((item) => (item.title || "").toLowerCase().includes(q) || (item.title_zh || "").toLowerCase().includes(q) || (item.summary || "").toLowerCase().includes(q) || (item.summary_zh || "").toLowerCase().includes(q));
      return list;
    }
  }
  if (pathname.startsWith("/tweets/")) {
    const parts = pathname.split("/");
    const tid = Number(parts[2]);
    const action = parts[3];
    tweetOv[tid] = tweetOv[tid] || {};
    if (method === "PUT" && opts.body) tweetOv[tid].content = opts.body.content;
    else if (action === "use") Object.assign(tweetOv[tid], { status: "used", used_at: Math.floor(Date.now() / 1000) });
    else if (action === "unuse") Object.assign(tweetOv[tid], { status: "draft", used_at: null });
    else if (action === "discard") Object.assign(tweetOv[tid], { status: "discarded" });
    ov.tweets = tweetOv;
    _saveOverrides(ov);
    return { ok: true };
  }
  if (pathname.startsWith("/posts/")) {
    const [, , pid, action] = pathname.split("/");
    if (["discard", "spam", "restore"].includes(action)) {
      postOv[pid] = { status: action === "restore" ? "tracking" : action };
      ov.posts = postOv;
      _saveOverrides(ov);
      return { ok: true };
    }
    throw new Error("GitHub Pages 静态模式下由 GitHub Actions 每 20 分钟自动云端生成，无需手动触发");
  }
  if (pathname === "/settings" && method === "PUT") {
    ov.settings = { ...(ov.settings || {}), ...(opts.body || {}) };
    _saveOverrides(ov);
    return { ...b.settings, ...ov.settings };
  }
  throw new Error("GitHub Pages 静态模式下该操作由 GitHub Actions 定时任务自动执行");
}

async function api(path, opts = {}) {
  if (IS_STATIC) return _staticApi(path, opts);
  const r = await fetch("/api" + path, {
    method: opts.method || "GET",
    headers: { "Content-Type": "application/json" },
    body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
  });
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch {}
    throw new Error(msg);
  }
  return r.json();
}

function toast(msg, type = "ok") {
  const t = document.createElement("div");
  t.className = `toast ${type}`;
  t.textContent = msg;
  $("#toasts").appendChild(t);
  setTimeout(() => t.remove(), 3800);
}

async function act(btn, fn, okMsg) {
  if (btn) btn.disabled = true;
  try {
    const r = await fn();
    if (okMsg) toast(typeof okMsg === "function" ? okMsg(r) : okMsg);
    return r;
  } catch (e) {
    toast(e.message, "error");
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const ta = document.createElement("textarea");
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    document.execCommand("copy");
    ta.remove();
  }
}

// Mirrors generator.weighted_len on the server.
const URL_RE = /https?:\/\/\S+/g;
const LIGHT = [[0, 4351], [8192, 8205], [8208, 8223], [8242, 8247]];
function weightedLen(text) {
  let n = (text.match(URL_RE) || []).length * 23;
  for (const ch of text.replace(URL_RE, "")) {
    const c = ch.codePointAt(0);
    n += LIGHT.some(([lo, hi]) => c >= lo && c <= hi) ? 1 : 2;
  }
  return n;
}

// ---------- shell ----------
const state = { status: null, timer: null, maxWeight: 280 };

function maybeNotifyDesktop(st, ov) {
  if (!st.desktop_notify || !("Notification" in window) || Notification.permission !== "granted") return;
  const seen = new Set(JSON.parse(localStorage.getItem("radar.notified") || "[]"));
  let changed = false;
  for (const p of ov.top || []) {
    if ((p.status === "candidate" || p.status === "drafted") && !seen.has(p.id)) {
      seen.add(p.id);
      changed = true;
      const n = new Notification(`🔥 r/${p.subreddit} 爆款候选 (热度 ${Math.round(p.hot_score)})`, {
        body: `${p.title}\n评论 ${p.num_comments} · ${p.comment_velocity} 条/分钟`,
        tag: `radar-${p.id}`,
      });
      n.onclick = () => { window.focus(); location.hash = `#/queue?post=${p.id}`; n.close(); };
    }
  }
  if (changed) localStorage.setItem("radar.notified", JSON.stringify([...seen].slice(-200)));
}

async function refreshShell() {
  try {
    const [st, ov] = await Promise.all([api("/status"), api("/overview")]);
    state.status = st;
    state.maxWeight = st.max_tweet_weight || 280;
    const pill = (ok, label, warnLabel) => `<span class="pill ${ok ? "ok" : "warn"}"><span class="dot"></span>${ok ? label : warnLabel}</span>`;
    const rLabel = st.reddit_provider === "redditdate" ? "RedditDate API 已连接" : "Reddit 已连接";
    $("#mode-pills").innerHTML = [
      st.demo_mode ? `<span class="pill warn"><span class="dot"></span>演示模式（模拟数据）</span>` : pill(true, rLabel),
      pill(st.llm_configured, `LLM: ${st.llm_model}`, "LLM 未配置（模板）"),
      pill(st.image_configured, `AI 配图: ${st.image_model}`, "AI 配图未配置"),
    ].join("");
    const b = ov.budget;
    const pct = b.pressure * 100;
    $("#budget-mini").innerHTML = `今日调用 <b style="color:var(--text)">${b.used_today}</b> / ${Math.round(b.daily_budget)}
      <div class="progress ${pct > 100 ? "danger" : pct > 80 ? "warn" : ""}" style="width:120px;margin:0"><div style="width:${Math.min(100, pct)}%"></div></div>
      剩余额度 <b style="color:var(--text)">${fmtNum(b.remaining)}</b>`;
    $("#nav-pending").textContent = ov.pending_review || "";
    maybeNotifyDesktop(st, ov);
    return ov;
  } catch (e) {
    console.error(e);
  }
}

const ROUTES = {
  dashboard: { title: "仪表盘", render: renderDashboard },
  official: { title: "官方动态", render: renderOfficial },
  radar: { title: "热帖雷达", render: renderRadar },
  queue: { title: "二创文案", render: renderQueue },
  subs: { title: "社区管理", render: renderSubs },
  settings: { title: "设置", render: renderSettings },
};

async function route() {
  const [name, query] = (location.hash.replace("#/", "") || "dashboard").split("?");
  const r = ROUTES[name] || ROUTES.dashboard;
  $$("nav a").forEach((a) => a.classList.toggle("active", a.dataset.route === name));
  $("#page-title").textContent = r.title;
  clearInterval(state.timer);
  await r.render($("#view"));
  const q = new URLSearchParams(query || "");
  if (q.get("post")) openPost(q.get("post"), q.get("studio") === "1");
}

// ---------- dashboard ----------
async function renderDashboard(view) {
  const ov = await refreshShell();
  const [usage, est, settings] = await Promise.all([api("/usage?days=30"), api("/estimate"), api("/settings")]);
  const b = ov.budget;
  const st = state.status;
  const pctToday = b.pressure * 100;
  const pctPeriod = (b.used_period / b.limit) * 100;
  const banner = st.demo_mode
    ? `<div class="banner">当前是<b>演示模式</b>：用模拟数据跑完整流程（采集 → 过滤垃圾 → 按评论数打分 → 二创文案 → 配图），调用次数同样计入额度统计。
       在 <code>.env</code> 中填写 <code>REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET</code> 并重启后切换为真实数据。</div>`
    : "";
  view.innerHTML = `${banner}
    <div class="grid kpis">
      <div class="card kpi"><div class="label">今日 Reddit 调用</div><div class="value">${b.used_today}<small> / ${Math.round(b.daily_budget)}</small></div>
        <div class="progress ${pctToday > 100 ? "danger" : pctToday > 80 ? "warn" : ""}"><div style="width:${Math.min(100, pctToday)}%"></div></div>
        <div class="hint" style="margin-top:6px">日预算 = 剩余额度 ÷ 剩余 ${b.days_left} 天</div></div>
      <div class="card kpi"><div class="label">${b.mode === "monthly" ? "本月" : "累计"}额度</div><div class="value">${fmtNum(b.used_period)}<small> / ${fmtNum(b.limit)}</small></div>
        <div class="progress ${pctPeriod > 90 ? "danger" : pctPeriod > 70 ? "warn" : ""}"><div style="width:${Math.min(100, pctPeriod)}%"></div></div>
        <div class="hint" style="margin-top:6px">按当前配置预计 ≈ ${est.total} 次/天</div></div>
      <div class="card kpi"><div class="label">跟踪中帖子</div><div class="value">${fmtNum(ov.tracking)}</div><div class="hint">今日新发现 ${ov.new_today} 条</div></div>
      <div class="card kpi"><div class="label">今日过滤垃圾/广告</div><div class="value">${ov.spam_today}</div><div class="hint"><a href="#/radar" id="see-spam">查看被过滤的帖子 →</a></div></div>
      <div class="card kpi"><div class="label">待使用文案</div><div class="value">${ov.pending_review}</div><div class="hint">今日已使用 ${ov.used_today} 条</div></div>
    </div>
    <div class="grid cols-2" style="margin-top:16px">
      <div class="card">
        <h3>
          <span style="display:inline-flex;gap:6px;align-items:center">
            <button class="btn sm" id="tab-top" style="padding:4px 10px;font-size:12px;font-weight:600">🔥 热帖评分榜 TOP 10</button>
            <button class="btn sm ghost" id="tab-latest" style="padding:4px 10px;font-size:12px">⚡ 最新收录新帖 TOP 10</button>
          </span>
          <a href="#/radar" class="muted" style="font-weight:400;font-size:12px">查看全部 →</a>
        </h3>
        <div id="top-list"></div>
      </div>
      <div class="card"><h3>后台任务 <small>自动定时执行</small></h3><div id="jobs"></div></div>
    </div>
    <div class="grid cols-2" style="margin-top:16px">
      <div class="card"><h3>近 30 天 API 调用 <small>虚线为今日预算</small></h3><div id="usage-chart"></div></div>
      <div class="card"><h3>调用预算估算 <small>不抓评论区，只用列表接口</small></h3>
        <table><tbody style="cursor:default">
          <tr><td>启用社区</td><td class="num">${est.subreddits} 个（${Math.max(1, Math.ceil(est.subreddits / 20))} 组合并请求）</td></tr>
          <tr><td>近 24h 有效新帖（已剔除垃圾）</td><td class="num">${est.new_posts_per_day} 条</td></tr>
          <tr><td>拉取新帖</td><td class="num">${est.collect_calls} 次/天</td></tr>
          <tr><td>刷新评论数（每批 100 条，每帖约 ${est.checks_per_post} 次）</td><td class="num">≤ ${est.refresh_calls} 次/天</td></tr>
          <tr><td>拉取评论区</td><td class="num">0（已关闭）</td></tr>
          <tr><td><b>合计</b></td><td class="num"><b>≈ ${est.total} 次/天 · ${fmtNum(est.total * 30)} 次/月</b></td></tr>
        </tbody></table>
      </div>
    </div>`;
  $("#see-spam").addEventListener("click", () => { Object.assign(radarFilter, { status: "spam", max_age_hours: 24, sort: "new" }); });
  const rankBadge = (idx) => {
    const cls = idx === 0 ? "rank-1" : idx === 1 ? "rank-2" : idx === 2 ? "rank-3" : "rank-n";
    return `<span class="rank-badge ${cls}">${idx + 1}</span>`;
  };
  const renderList = (items) => {
    const box = $("#top-list");
    if (!box) return;
    box.innerHTML = items && items.length
      ? items.slice(0, 10).map((p, idx) => `
        <div class="list-item" data-id="${p.id}">
          ${rankBadge(idx)}
          <div class="score-ring" style="border:2px solid ${hotColor(p.hot_score)};color:${hotColor(p.hot_score)}" title="热度评分: ${Math.round(p.hot_score)} 分">${Math.round(p.hot_score)}</div>
          <div class="main"><div class="t">${esc(p.title_zh || p.title)}</div>
            ${p.title_zh ? `<div class="m" style="opacity:0.78">${esc(p.title)}</div>` : ""}
            <div class="m"><span class="sub-tag">r/${esc(p.subreddit)}</span> · ${postTime(p)} · 评分 <b>${Math.round(p.hot_score)}</b> · 评论 <b>${fmtNum(p.num_comments)}</b> · ${p.comment_velocity} 条/分 · 赞 <b>${fmtNum(p.score)}</b></div></div>
          ${statusTag(p.status)}
        </div>`).join("")
      : '<div class="empty">暂无数据，等待首次采集…</div>';
    $$("#top-list .list-item").forEach((n) => n.addEventListener("click", () => openPost(n.dataset.id)));
  };
  renderList(ov.top && ov.top.length ? ov.top : ov.latest);
  $("#tab-top").addEventListener("click", () => {
    $("#tab-top").className = "btn sm";
    $("#tab-latest").className = "btn sm ghost";
    renderList(ov.top || []);
  });
  $("#tab-latest").addEventListener("click", () => {
    $("#tab-top").className = "btn sm ghost";
    $("#tab-latest").className = "btn sm";
    renderList(ov.latest || []);
  });
  renderJobs();
  Charts.stackedBar($("#usage-chart"), {
    labels: usage.days.map((d) => d.date),
    xFmt: (d) => d.slice(5),
    stacks: [
      { name: "拉新帖 new", color: "#4da3ff", values: usage.days.map((d) => d.new || 0) },
      { name: "刷新 info", color: "#ff8a4c", values: usage.days.map((d) => d.info || 0) },
      { name: "评论 comments", color: "#a371f7", values: usage.days.map((d) => d.comments || 0) },
    ],
    line: { name: "日预算", value: b.daily_budget, color: "#3fb950" },
  });
  state.timer = setInterval(async () => { await refreshShell(); renderJobs(); }, 15000);
}

async function renderJobs() {
  const box = $("#jobs");
  if (!box) return;
  const st = await api("/status");
  box.innerHTML = st.jobs.map((j) => {
    const res = j.last_error ? `<span class="err">失败：${esc(j.last_error)}</span>` : j.last_result ? esc(JSON.stringify(j.last_result)) : "尚未运行";
    return `<div class="job"><div class="main"><div>${j.label} ${j.running ? '<span class="pill warn"><span class="dot"></span>运行中</span>' : ""}</div>
      <div class="m">上次 ${ago(j.last_run)} · 下次 ${fmtTime(j.next_run)}</div><div class="m" title="${esc(res)}">${res}</div></div>
      <button class="btn sm" data-job="${j.name}">立即运行</button></div>`;
  }).join("");
  $$("[data-job]", box).forEach((b) => b.addEventListener("click", async () => {
    const r = await act(b, () => api(`/jobs/${b.dataset.job}/run`, { method: "POST" }));
    if (r) toast(r.error ? `失败：${r.error}` : r.skipped || "完成：" + JSON.stringify(r.result), r.error ? "error" : "ok");
    route();
  }));
}

// ---------- official blogs ----------
const LAB_COLORS = {
  "OpenAI": { cls: "openai", dot: "openai", label: "OpenAI" },
  "Anthropic": { cls: "anthropic", dot: "anthropic", label: "Anthropic" },
  "Google DeepMind": { cls: "deepmind", dot: "deepmind", label: "DeepMind" },
  "Google AI": { cls: "google-ai", dot: "google-ai", label: "Google AI" },
  "Meta AI": { cls: "meta-ai", dot: "meta-ai", label: "Meta AI" },
};

function labBadge(source) {
  const info = LAB_COLORS[source] || { cls: "openai", dot: "openai", label: source };
  return `<span class="lab-badge ${info.cls}"><span class="lab-dot ${info.dot}"></span>${esc(source)}</span>`;
}

async function renderOfficial(view) {
  refreshShell();
  view.innerHTML = `
    <div class="official-header">
      <div class="lab-tabs" id="lab-tabs">
        <div class="lab-tab active" data-src="">全部 <span class="badge-num" id="cnt-all">…</span></div>
        <div class="lab-tab" data-src="OpenAI"><span class="lab-dot openai"></span>OpenAI <span class="badge-num" id="cnt-openai">…</span></div>
        <div class="lab-tab" data-src="Anthropic"><span class="lab-dot anthropic"></span>Anthropic <span class="badge-num" id="cnt-anthropic">…</span></div>
        <div class="lab-tab" data-src="Google DeepMind"><span class="lab-dot deepmind"></span>DeepMind <span class="badge-num" id="cnt-deepmind">…</span></div>
        <div class="lab-tab" data-src="Google AI"><span class="lab-dot google-ai"></span>Google AI <span class="badge-num" id="cnt-google-ai">…</span></div>
        <div class="lab-tab" data-src="Meta AI"><span class="lab-dot meta-ai"></span>Meta AI <span class="badge-num" id="cnt-meta-ai">…</span></div>
      </div>
      <div class="row" style="gap:8px">
        <input type="text" id="official-search" placeholder="搜索官方发文标题、关键词、技术名词…" style="width:280px;padding:7px 12px;font-size:13px;border-radius:20px;border:1px solid var(--line);background:var(--panel)" />
      </div>
    </div>
    <div class="official-grid" id="official-list"><div class="empty">加载中…</div></div>
  `;

  let currentSource = "";
  let searchQuery = "";
  let allBlogs = [];

  const loadBlogs = async () => {
    try {
      allBlogs = await api("/official?limit=150");
      const counts = { all: allBlogs.length, openai: 0, anthropic: 0, deepmind: 0, "google-ai": 0, "meta-ai": 0 };
      allBlogs.forEach((b) => {
        const s = (b.source || "").toLowerCase();
        if (s.includes("openai")) counts.openai++;
        else if (s.includes("anthropic")) counts.anthropic++;
        else if (s.includes("deepmind")) counts.deepmind++;
        else if (s.includes("google")) counts["google-ai"]++;
        else if (s.includes("meta")) counts["meta-ai"]++;
      });
      const setCnt = (id, n) => { const el = $(id, view); if (el) el.textContent = n; };
      setCnt("#cnt-all", counts.all);
      setCnt("#cnt-openai", counts.openai);
      setCnt("#cnt-anthropic", counts.anthropic);
      setCnt("#cnt-deepmind", counts.deepmind);
      setCnt("#cnt-google-ai", counts["google-ai"]);
      setCnt("#cnt-meta-ai", counts["meta-ai"]);

      renderCards();
    } catch (e) {
      const el = $("#official-list", view);
      if (el) el.innerHTML = `<div class="empty">加载失败: ${esc(e.message)}</div>`;
    }
  };

  const renderCards = () => {
    const listEl = $("#official-list", view);
    if (!listEl) return;
    let filtered = allBlogs;
    if (currentSource) {
      filtered = filtered.filter((b) => (b.source || "").toLowerCase().includes(currentSource.toLowerCase()));
    }
    if (searchQuery) {
      const q = searchQuery.toLowerCase();
      filtered = filtered.filter((b) =>
        (b.title || "").toLowerCase().includes(q) ||
        (b.title_zh || "").toLowerCase().includes(q) ||
        (b.summary || "").toLowerCase().includes(q) ||
        (b.summary_zh || "").toLowerCase().includes(q)
      );
    }

    if (!filtered.length) {
      listEl.innerHTML = `<div class="empty" style="grid-column:1/-1">暂无符合条件的官方发文</div>`;
      return;
    }

    listEl.innerHTML = filtered.map((b) => {
      const titleZh = b.title_zh || b.title;
      const showEnSub = b.title_zh && b.title_zh !== b.title;
      const summaryText = b.summary_zh || b.summary || "暂无官方摘要";
      return `
        <article class="official-card">
          <div class="official-card-meta">
            ${labBadge(b.source)}
            ${b.category ? `<span class="official-cat">${esc(b.category)}</span>` : ""}
            <span class="official-card-time">${ago(b.published_at)} · ${fmtTime(b.published_at)}</span>
          </div>
          <div class="official-card-title">
            <a href="${esc(b.url)}" target="_blank" rel="noopener noreferrer">${esc(titleZh)}</a>
          </div>
          ${showEnSub ? `<div class="official-card-en">${esc(b.title)}</div>` : ""}
          <div class="official-card-summary">${esc(summaryText)}</div>
          <div class="official-card-foot">
            <div class="official-card-actions">
              <a href="${esc(b.url)}" target="_blank" rel="noopener noreferrer" class="btn sm">官方原文 ↗</a>
              <button class="btn sm" data-copy-link="${esc(b.url)}">复制链接</button>
              <button class="btn sm" data-draft-tweet="${esc(b.id)}">推文二创</button>
            </div>
          </div>
        </article>
      `;
    }).join("");

    $$("[data-copy-link]", listEl).forEach((btn) => {
      btn.addEventListener("click", () => {
        copyText(btn.dataset.copyLink);
        toast("官方链接已复制到剪贴板");
      });
    });

    $$("[data-draft-tweet]", listEl).forEach((btn) => {
      btn.addEventListener("click", () => {
        const item = allBlogs.find((x) => x.id === btn.dataset.draftTweet);
        if (!item) return;
        const text = `【${item.source} 官方动态】${item.title_zh || item.title}\n\n${item.summary_zh || item.summary || ""}\n\n🔗 官方传送门: ${item.url}`;
        copyText(text);
        toast("已复制官方动态二创草稿与链接，可直接粘贴发布！");
      });
    });
  };

  $$(".lab-tab", view).forEach((tab) => {
    tab.addEventListener("click", () => {
      $$(".lab-tab", view).forEach((t) => t.classList.remove("active"));
      tab.classList.add("active");
      currentSource = tab.dataset.src;
      renderCards();
    });
  });

  const searchInput = $("#official-search", view);
  if (searchInput) {
    searchInput.addEventListener("input", (e) => {
      searchQuery = e.target.value.trim();
      renderCards();
    });
  }

  await loadBlogs();
}

// ---------- radar ----------
const radarFilter = { status: "tracking,candidate,drafted", subreddit: "", sort: "new", q: "", max_age_hours: 24 };

async function renderRadar(view) {
  refreshShell();
  const subs = await api("/subreddits");
  view.innerHTML = `
    <div class="row filters">
      <select id="f-status">
        <option value="tracking,candidate,drafted">活跃（跟踪/候选/待使用）</option>
        <option value="candidate,drafted">仅爆款候选</option>
        <option value="tracking">跟踪中</option>
        <option value="used">已使用</option>
        <option value="spam">垃圾/广告（已过滤）</option>
        <option value="discarded">已丢弃</option>
        <option value="expired,removed">已过期 / 删除</option>
        <option value="">全部</option>
      </select>
      <select id="f-sub"><option value="">全部社区</option>${subs.map((s) => `<option value="${esc(s.name)}">r/${esc(s.name)}</option>`).join("")}</select>
      <select id="f-sort"><option value="new">按发帖时间（最新）</option><option value="hot">按爆款热度</option><option value="comments">按评论数</option></select>
      <select id="f-age"><option value="1">1 小时内</option><option value="3">3 小时内</option><option value="6">6 小时内</option><option value="12">12 小时内</option><option value="24">24 小时内</option><option value="72">3 天内</option></select>
      <input type="text" id="f-q" placeholder="搜索中英文标题…" style="width:200px" />
      <div class="spacer"></div>
      <span class="legend-fresh"><i class="fresh-1"></i>1h 内 <i class="fresh-2"></i>1-3h <i class="fresh-3"></i>3h+</span>
      <span id="radar-sync-time" class="pill ok"><span class="dot"></span>自动同步中</span>
      <span class="muted" id="radar-count"></span>
      <button class="btn" id="radar-refresh">刷新</button>
    </div>
    <div class="card" style="padding:0;overflow:auto"><table class="radar-table">
      <thead><tr><th style="width:130px">爆款热度</th><th>标题（中文翻译 / 原文）</th><th style="width:150px">发帖时间</th><th class="num">评论</th><th class="num">评论/分钟</th><th class="num">赞</th><th>状态</th></tr></thead>
      <tbody id="radar-body"></tbody></table></div>`;
  $("#f-status").value = radarFilter.status;
  $("#f-sub").value = radarFilter.subreddit;
  $("#f-sort").value = radarFilter.sort;
  $("#f-age").value = String(radarFilter.max_age_hours);
  $("#f-q").value = radarFilter.q;
  const bind = (id, key) => $(id).addEventListener(id === "#f-q" ? "input" : "change", (e) => { radarFilter[key] = e.target.value; loadRadar(); });
  bind("#f-status", "status"); bind("#f-sub", "subreddit"); bind("#f-sort", "sort"); bind("#f-age", "max_age_hours"); bind("#f-q", "q");
  $("#radar-refresh").addEventListener("click", async () => {
    const btn = $("#radar-refresh");
    btn.disabled = true;
    btn.textContent = "刷新中…";
    try {
      await loadRadar(true);
      toast("已同步最新数据", "ok");
    } finally {
      btn.disabled = false;
      btn.textContent = "刷新";
    }
  });
  await loadRadar(true);
  state.timer = setInterval(() => loadRadar(true), 30000);
}

async function loadRadar(force = false) {
  const qs = new URLSearchParams({ ...radarFilter, limit: 300 }).toString();
  const items = await api("/posts?" + qs, { force });
  const body = $("#radar-body");
  if (!body) return;
  $("#radar-count").textContent = `${items.length} 条`;
  const syncTag = $("#radar-sync-time");
  if (syncTag) {
    const nowTime = new Date().toLocaleTimeString("zh-CN", { hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit" });
    syncTag.innerHTML = `<span class="dot"></span>${nowTime} 已同步`;
  }
  body.innerHTML = items.length
    ? items.map((p) => `
      <tr data-id="${p.id}">
        <td class="cell-hot">${p.status === "spam" ? '<span class="muted">—</span>' : hotBar(p.hot_score)}</td>
        <td class="title-cell"><span class="t">${p.images.length ? '<span class="img-dot" title="原帖带图">图</span>' : ""}${esc(p.title_zh || p.title)}</span>
          ${p.title_zh ? `<div class="m en-sub" style="opacity:0.78">${esc(p.title)}</div>` : ""}
          <div class="m"><span class="sub-tag">r/${esc(p.subreddit)}</span> · ${TYPE[p.post_type] || p.post_type}${p.flair ? " · " + esc(p.flair) : ""} · u/${esc(p.author)}
          ${p.spam_reason ? ` · <span style="color:var(--red)">${esc(p.spam_reason)}</span>` : ""}</div></td>
        <td class="cell-time">${postTime(p)}</td>
        <td class="num cell-cmt" data-label="评论 "><b>${fmtNum(p.num_comments)}</b></td>
        <td class="num cell-vel" data-label="增速 ">${p.comment_velocity}/m</td>
        <td class="num muted cell-score" data-label="赞 ">${fmtNum(p.score)}</td>
        <td class="cell-status">${statusTag(p.status)}</td>
      </tr>`).join("")
    : `<tr><td colspan="7" class="empty">没有符合条件的帖子</td></tr>`;
  $$("tr[data-id]", body).forEach((tr) => tr.addEventListener("click", () => openPost(tr.dataset.id)));
}

// ---------- copy editor ----------
function closeDrawer() { document.body.classList.remove("drawer-open"); }
$("#drawer-mask").addEventListener("click", closeDrawer);
document.addEventListener("keydown", (e) => e.key === "Escape" && closeDrawer());

function tweetEditor(t, onChange) {
  const editable = t.status === "draft";
  const maxW = state.maxWeight || 280;
  const div = document.createElement("div");
  div.className = "tweet";
  div.innerHTML = `
    <div class="head"><span class="style">${esc(t.style || "文案")}</span>${statusTag(t.status)}
      <span class="spacer"></span><span class="counter"></span></div>
    ${editable ? `<textarea>${esc(t.content)}</textarea>` : `<div class="published-text">${esc(t.content)}</div>`}
    ${t.card_title ? `<div class="card-title-hint">配图标题：${esc(t.card_title)}</div>` : ""}
    <div class="actions">${editable
      ? `<button class="btn sm ghost danger" data-a="discard">丢弃</button><button class="btn sm" data-a="save">保存</button>
         <button class="btn sm" data-a="copy">复制文案</button><button class="btn sm" data-a="use">复制并标记已用</button>
         <button class="btn sm primary" data-a="post-x" title="复制文案、标记已用并直接打开 X 发帖框">去 X 发帖 ↗</button>`
      : `<span class="muted" style="font-size:12px">${t.used_at ? "使用于 " + fmtTime(t.used_at) : ""}</span>
         <button class="btn sm" data-a="copy">再次复制</button><button class="btn sm" data-a="open-x">打开 X ↗</button><button class="btn sm ghost" data-a="unuse">撤销</button>`}</div>`;
  const counter = $(".counter", div);
  const ta = $("textarea", div);
  const text = () => (ta ? ta.value : t.content);
  const update = () => {
    const n = weightedLen(text());
    counter.textContent = `${n} / ${maxW}`;
    counter.classList.toggle("over", n > maxW);
  };
  update();
  const on = (a, fn) => { const b = $(`[data-a="${a}"]`, div); if (b) b.addEventListener("click", (e) => fn(e.target)); };
  const save = () => api(`/tweets/${t.id}`, { method: "PUT", body: { content: ta.value } });
  const openX = (txt) => window.open("https://x.com/intent/post?text=" + encodeURIComponent(txt), "_blank", "noopener");
  if (ta) ta.addEventListener("input", update);
  on("copy", (b) => act(b, async () => { await copyText(text()); if (ta) await save(); }, "文案已复制"));
  on("save", (b) => act(b, save, "已保存"));
  on("use", async (b) => {
    const r = await act(b, async () => { await copyText(text()); await save(); return api(`/tweets/${t.id}/use`, { method: "POST" }); }, "已复制并标记为已使用");
    if (r) onChange();
  });
  on("post-x", async (b) => {
    const cur = text();
    openX(cur);
    const r = await act(b, async () => { await copyText(cur); await save(); return api(`/tweets/${t.id}/use`, { method: "POST" }); }, "已打开 X 发帖框并标记为已用");
    if (r) onChange();
  });
  on("open-x", () => openX(text()));
  on("discard", async (b) => { if (await act(b, () => api(`/tweets/${t.id}/discard`, { method: "POST" }), "已丢弃")) onChange(); });
  on("unuse", async (b) => { if (await act(b, () => api(`/tweets/${t.id}/unuse`, { method: "POST" }), "已撤销")) onChange(); });
  return div;
}

// ---------- post drawer ----------
async function openPost(id, focusStudio = false) {
  const d = $("#drawer");
  d.innerHTML = '<div class="empty">加载中…</div>';
  document.body.classList.add("drawer-open");
  const data = await api(`/posts/${id}`);
  const p = data.post;
  const drafts = data.tweets;
  const isSpam = p.status === "spam", isOff = ["discarded", "spam"].includes(p.status);
  d.innerHTML = `
    <button class="btn sm ghost close" id="dr-close">✕</button>
    <div class="row"><span class="sub-tag">r/${esc(p.subreddit)}</span>${statusTag(p.status)}<span class="muted">${TYPE[p.post_type] || ""} · u/${esc(p.author)}${p.flair ? " · " + esc(p.flair) : ""}</span></div>
    <h2>${esc(p.title_zh || p.title)}</h2>
    ${p.title_zh ? `<div class="muted" style="margin:-6px 0 10px;font-size:13px">${esc(p.title)}</div>` : ""}
    <div class="row">${postTime(p)}<a href="${esc(p.permalink)}" target="_blank">打开 Reddit 原帖 ↗</a>${p.url && !p.url.includes("reddit.com") ? `<a href="${esc(p.url)}" target="_blank">外链 ↗</a>` : ""}
      <span class="spacer"></span>
      ${isOff ? `<button class="btn sm" id="dr-restore">${isSpam ? "不是垃圾，恢复" : "恢复"}</button>`
              : '<button class="btn sm ghost danger" id="dr-spam">标为垃圾</button><button class="btn sm ghost" id="dr-discard">丢弃</button>'}
      <button class="btn sm" id="dr-trans">${p.title_zh || p.selftext_zh ? "重新翻译" : "翻译原文"}</button>
      <button class="btn sm primary" id="dr-gen">${drafts.length ? "重新二创" : "生成二创文案"}</button></div>
    ${isSpam ? `<div class="banner danger" style="margin-top:14px">已被过滤：${esc(p.spam_reason || "")}。被过滤的帖子不再刷新，不消耗额度。</div>` : ""}
    <section><div class="stat-row">
      <div class="stat"><div class="l">爆款热度</div><div class="v" style="color:${hotColor(p.hot_score)}">${Math.round(p.hot_score)}</div></div>
      <div class="stat"><div class="l">评论数</div><div class="v">${fmtNum(p.num_comments)}</div></div>
      <div class="stat"><div class="l">评论/分钟</div><div class="v">${p.comment_velocity}</div></div>
      <div class="stat"><div class="l">帖龄</div><div class="v">${fmtAge(p.age_min)}</div></div>
      <div class="stat"><div class="l">赞 / 好评率</div><div class="v">${fmtNum(p.score)} <small class="muted">${Math.round(p.upvote_ratio * 100)}%</small></div></div>
    </div></section>
    <section><h4>判断依据（只看评论数，不抓评论区）</h4><ul class="reasons">${p.reasons.map((r) => `<li>${esc(r)}</li>`).join("") || "<li>尚未评分</li>"}</ul></section>
    <section><h4>评论数走势 vs 社区同帖龄基线</h4><div id="dr-chart"></div></section>
    ${(p.title_zh || p.selftext_zh) ? `<section><div class="row" style="margin-bottom:6px"><h4 style="margin:0">原帖中文翻译（MiniMax）</h4><span class="spacer"></span><button class="btn sm" id="dr-copy-zh">复制中文译文</button></div><div class="selftext">${esc((p.title_zh ? "【标题】" + p.title_zh : "") + (p.selftext_zh ? "\n\n" + p.selftext_zh : ""))}</div></section>` : ""}
    ${p.selftext ? `<section><h4>原帖英文正文</h4><div class="selftext">${esc(p.selftext)}</div></section>` : ""}
    ${p.link_context ? `<section><h4>外链网页摘要（自动提取供二创参考）</h4><div class="selftext">${esc(p.link_context)}</div></section>` : ""}
    <section><h4>二创文案（MiniMax）</h4><div id="dr-tweets">${drafts.length ? "" : '<div class="muted">还没有文案，点击右上角“生成二创文案”。</div>'}</div></section>
    <section id="dr-studio-sec"><h4>配图工作台</h4><div id="dr-studio"></div></section>`;
  const reload = () => { openPost(id); refreshShell(); if ($("#radar-body")) loadRadar(); if ($("#queue-list")) loadQueue(); };
  $("#dr-close").addEventListener("click", closeDrawer);
  $("#dr-trans").addEventListener("click", async (e) => {
    const r = await act(e.target, () => api(`/posts/${id}/translate`, { method: "POST" }), "已完成原文中文翻译");
    if (r) reload();
  });
  const copyZhBtn = $("#dr-copy-zh");
  if (copyZhBtn) {
    copyZhBtn.addEventListener("click", (e) => {
      const zhText = (p.title_zh ? p.title_zh : p.title) + (p.selftext_zh ? "\n\n" + p.selftext_zh : "");
      act(e.target, () => copyText(zhText), "中文译文已复制");
    });
  }
  $("#dr-gen").addEventListener("click", async (e) => {
    const r = await act(e.target, () => api(`/posts/${id}/generate`, { method: "POST" }), (r) => `已生成 ${r.drafts} 条文案（${r.source}）`);
    if (r) reload();
  });
  const bindPost = (sel, path, msg) => { const b = $(sel); if (b) b.addEventListener("click", async (e) => { if (await act(e.target, () => api(`/posts/${id}/${path}`, { method: "POST" }), msg)) reload(); }); };
  bindPost("#dr-discard", "discard", "已丢弃");
  bindPost("#dr-spam", "spam", "已标记为垃圾，停止跟踪");
  bindPost("#dr-restore", "restore", "已恢复");
  const box = $("#dr-tweets");
  drafts.forEach((t) => box.appendChild(tweetEditor(t, reload)));
  Studio.mount($("#dr-studio"), {
    post: p, tweets: drafts, assets: data.assets, imageConfigured: state.status && state.status.image_configured,
    fmtTime, toast, api, onAssets: () => openPost(id, true),
  });
  const maxAge = Math.max(p.age_min, 15);
  const base = data.baseline.filter((b) => b.age_min <= maxAge * 1.15);
  Charts.lineChart($("#dr-chart"), {
    xFmt: (x) => fmtAgeShort(x), yFmt: (y) => fmtNum(Math.round(y)),
    series: [
      { name: "该帖评论数", color: "#a371f7", points: data.snapshots.map((s) => ({ x: s.age_min, y: s.num_comments })), dots: true, fill: true, width: 2.5 },
      { name: "社区 P90", color: "#f85149", dash: "5 4", points: base.map((b) => ({ x: b.age_min, y: b.c90 })) },
      { name: "社区中位数", color: "#8b98a8", dash: "3 4", points: base.map((b) => ({ x: b.age_min, y: b.c50 })) },
    ],
  });
  if (focusStudio) $("#dr-studio-sec").scrollIntoView({ block: "start" });
}

// ---------- queue ----------
let queueTab = "draft";

async function renderQueue(view) {
  refreshShell();
  view.innerHTML = `
    <div class="row filters">
      <div class="tabs" id="q-tabs">
        <button data-t="draft">待使用</button><button data-t="used">已使用</button><button data-t="discarded,skipped">已丢弃</button>
      </div>
      <div class="spacer"></div><span class="muted" id="q-hint">复制文案 + 配图后，自己到 X 发布</span>
    </div>
    <div id="queue-list"></div>`;
  $$("#q-tabs button").forEach((b) => b.addEventListener("click", () => { queueTab = b.dataset.t; loadQueue(); }));
  await loadQueue();
}

async function loadQueue() {
  $$("#q-tabs button").forEach((b) => b.classList.toggle("active", b.dataset.t === queueTab));
  const items = await api(`/tweets?status=${queueTab}`);
  const list = $("#queue-list");
  if (!list) return;
  if (!items.length) { list.innerHTML = '<div class="card empty">这里空空如也</div>'; return; }
  const groups = new Map();
  items.forEach((t) => { if (!groups.has(t.post_id)) groups.set(t.post_id, []); groups.get(t.post_id).push(t); });
  list.innerHTML = "";
  for (const [pid, ts] of groups) {
    const t0 = ts[0];
    const card = document.createElement("div");
    card.className = "card queue-card";
    card.innerHTML = `
      <div class="post-head">
        ${t0.images.length ? `<img class="qthumb" src="${esc(proxied(t0.images[0]))}" loading="lazy" onerror="this.remove()"/>` : ""}
        <div class="main"><div style="font-weight:600;cursor:pointer" class="open">${esc(t0.title_zh || t0.title)}</div>
          ${t0.title_zh ? `<div class="muted" style="font-size:12px;margin-top:2px">${esc(t0.title)}</div>` : ""}
          <div class="muted" style="font-size:12px;margin-top:5px;display:flex;gap:8px;align-items:center;flex-wrap:wrap">
            <span class="sub-tag">r/${esc(t0.subreddit)}</span>${postTime(t0)}<span>评论 <b style="color:var(--text)">${fmtNum(t0.num_comments)}</b></span>
            <span style="color:${hotColor(t0.hot_score)}">热度 ${Math.round(t0.hot_score)}</span><a href="${esc(t0.permalink)}" target="_blank">原帖 ↗</a></div></div>
        <button class="btn sm" data-a="studio">制作配图</button>
        ${queueTab === "draft" ? '<button class="btn sm ghost danger" data-a="drop">整帖丢弃</button><button class="btn sm" data-a="regen">重新二创</button>' : ""}
      </div>
      ${t0.selftext_zh ? `<div class="selftext" style="margin:10px 0 4px;font-size:13px;max-height:140px"><b>【原帖中文译文】</b>${esc(t0.selftext_zh)}</div>` : ""}
      <div class="variants"></div>`;
    const v = $(".variants", card);
    ts.forEach((t) => v.appendChild(tweetEditor(t, () => { loadQueue(); refreshShell(); })));
    $(".open", card).addEventListener("click", () => openPost(pid));
    $('[data-a="studio"]', card).addEventListener("click", () => openPost(pid, true));
    const drop = $('[data-a="drop"]', card), regen = $('[data-a="regen"]', card);
    if (drop) drop.addEventListener("click", async (e) => { if (await act(e.target, () => api(`/posts/${pid}/discard`, { method: "POST" }), "已丢弃")) { loadQueue(); refreshShell(); } });
    if (regen) regen.addEventListener("click", async (e) => { if (await act(e.target, () => api(`/posts/${pid}/generate`, { method: "POST" }), (r) => `已重新生成（${r.source}）`)) loadQueue(); });
    list.appendChild(card);
  }
}

// ---------- subreddits ----------
async function renderSubs(view) {
  refreshShell();
  const subs = await api("/subreddits");
  view.innerHTML = `
    <div class="card" style="margin-bottom:16px"><div class="row">
      <input type="text" id="sub-name" placeholder="输入社区名，例如 LocalLLaMA 或 r/technology" style="flex:1;min-width:180px;max-width:340px" />
      <button class="btn primary" id="sub-add">添加社区</button>
      <span class="muted">所有启用的社区每 20 个合并成一次 <code>/r/a+b+c/new</code> 请求，增加社区几乎不增加调用。</span>
    </div></div>
    <div class="card" style="padding:0;overflow:auto"><table>
      <thead><tr><th>社区</th><th>启用</th><th class="num">7 天帖子</th><th class="num">7 天爆款候选</th><th class="num">7 天已使用</th><th class="num">7 天过滤垃圾</th><th class="num">7 天最高评论</th>
        <th class="num">评论 P90 @1h</th><th class="num">评论 P90 @4h</th><th class="num">基线样本</th><th></th></tr></thead>
      <tbody style="cursor:default">${subs.map((s) => `
        <tr>
          <td><a href="https://www.reddit.com/r/${esc(s.name)}/new" target="_blank" class="sub-tag">r/${esc(s.name)}</a></td>
          <td><label class="switch"><input type="checkbox" data-toggle="${esc(s.name)}" ${s.enabled ? "checked" : ""}/></label></td>
          <td class="num">${s.posts_7d}</td><td class="num">${s.hits_7d || 0}</td><td class="num">${s.used_7d || 0}</td><td class="num">${s.spam_7d || 0}</td><td class="num">${fmtNum(s.max_comments_7d || 0)}</td>
          <td class="num">${s.baseline_60_c90 ?? "—"}</td><td class="num">${s.baseline_240_c90 ?? "—"}</td>
          <td class="num">${s.baseline_samples}${s.baseline_samples < 15 ? ' <span class="muted">(不足)</span>' : ""}</td>
          <td><button class="btn sm ghost danger" data-del="${esc(s.name)}">移除</button></td>
        </tr>`).join("") || '<tr><td colspan="11" class="empty">还没有关注的社区</td></tr>'}</tbody></table></div>`;
  const add = async (e) => {
    const name = $("#sub-name").value.trim();
    if (!name) return;
    if (await act(e.target, () => api("/subreddits", { method: "POST", body: { name } }), `已添加 ${name}`)) renderSubs(view);
  };
  $("#sub-add").addEventListener("click", add);
  $("#sub-name").addEventListener("keydown", (e) => e.key === "Enter" && add(e));
  $$("[data-toggle]").forEach((c) => c.addEventListener("change", () =>
    act(null, () => api(`/subreddits/${encodeURIComponent(c.dataset.toggle)}`, { method: "PATCH", body: { enabled: c.checked } }), c.checked ? "已启用" : "已停用")));
  $$("[data-del]").forEach((b) => b.addEventListener("click", async () => {
    if (!confirm(`移除 r/${b.dataset.del}？已采集的历史数据会保留。`)) return;
    if (await act(b, () => api(`/subreddits/${encodeURIComponent(b.dataset.del)}`, { method: "DELETE" }), "已移除")) renderSubs(view);
  }));
}

// ---------- settings ----------
const SETTINGS_FORM = [
  ["调用额度", [
    ["quota_mode", "额度类型", "select", "每月重置 or 一次性总量", [["monthly", "每月额度"], ["total", "总量（不重置）"]]],
    ["quota_limit", "额度上限", "number", "例如 100000"],
    ["quota_total_days", "总量模式下计划使用天数", "number", "仅总量模式生效：日预算 = 剩余 ÷ 剩余天数"],
  ]],
  ["采集节奏", [
    ["poll_new_minutes", "拉取新帖间隔（分钟）", "number", "越短发现越早，调用 = 1440 ÷ 间隔 × 组数"],
    ["refresh_check_minutes", "评论数刷新检查间隔（分钟）", "number", "只刷新到期的帖子，每批最多 100 条 = 1 次调用"],
    ["track_hours", "跟踪时长（小时）", "number", "只要新帖，超过这个时长就停止刷新"],
    ["early_drop_hours", "冷帖提前降频（小时）", "number", "超过此帖龄且热度 < 20 的帖子不再频繁刷新"],
  ]],
  ["爆款判断（按评论数）", [
    ["candidate_threshold", "爆款热度阈值（0-100）", "number", "50 ≈ 评论数持平社区同帖龄 P90；60 ≈ 1.7 倍；70 ≈ 3 倍"],
    ["candidate_max_age_hours", "爆款最大帖龄（小时）", "number", "只追最新的帖子，超过这个时长不再进入候选"],
    ["min_comments", "最少评论数", "number", "过滤小社区的噪声"],
    ["dedupe_similarity", "去重相似度（0-1）", "number", "24 小时内标题相似度或相同外链视为同一话题"],
    ["allow_nsfw", "允许 NSFW 帖子", "bool"],
  ]],
  ["垃圾 / 广告过滤", [
    ["spam_filter", "启用过滤（被过滤的帖子不再刷新，省额度）", "bool"],
    ["spam_keywords", "广告关键词（逗号分隔，匹配标题和正文）", "textarea"],
    ["spam_domains", "推广域名（逗号分隔，精确匹配域名/子域名）", "textarea"],
    ["spam_flairs", "屏蔽的 flair（逗号分隔，完全匹配）", "textarea"],
    ["max_author_posts_24h", "同作者 24h 最多发帖数", "number", "超过视为刷屏"],
    ["min_upvote_ratio", "最低好评率（0-1）", "number", "发帖 30 分钟后低于此值视为广告/引战"],
  ]],
  ["二创文案", [
    ["auto_generate", "成为爆款候选后自动生成文案", "bool"],
    ["fetch_link_context", "外链帖自动抓取网页摘要补充背景（0 额度消耗）", "bool"],
    ["variants", "每帖生成版本数", "number"],
    ["tweet_language", "文案语言", "select", "", [["zh", "简体中文"], ["en", "English"]]],
    ["max_tweet_weight", "推文字数上限", "select", "标准推文 280 权重（140 汉字），X Premium 可选长文", [
      ["280", "280 标准推文（约 140 汉字）"],
      ["600", "600 中长推文（约 300 汉字）"],
      ["1500", "1500 深度长推（约 750 汉字）"],
    ]],
    ["tweet_styles", "二创角度（逗号分隔）", "text", "按顺序对应每个版本"],
    ["custom_persona", "自定义账号人设 / 额外写作指令（可选）", "textarea", "例如：科技毒舌博主，多用短句，带一点冷幽默"],
    ["append_link", "文案末尾附原帖链接", "bool"],
  ]],
  ["通知与系统维护", [
    ["desktop_notify", "发现爆款候选时发送浏览器桌面通知", "bool"],
    ["webhook_url", "Webhook 推送地址（支持飞书 / 企微 / Discord / Bark / Slack）", "text", "留空不推送；保存前可点击下方按钮测试连通性"],
    ["webhook_min_hot", "触发 Webhook 推送的最低热度", "number", "例如 70"],
    ["retention_days", "历史快照与图片缓存保留天数", "number", "默认 14 天，后台每 6 小时自动清理过期数据"],
  ]],
];

async function renderSettings(view) {
  refreshShell();
  const [s, st] = await Promise.all([api("/settings"), api("/status")]);
  const field = ([key, label, type, help, opts]) => {
    const v = s[key];
    let input;
    if (type === "bool") input = `<label class="switch"><input type="checkbox" name="${key}" ${v ? "checked" : ""}/><span>${label}</span></label>`;
    else if (type === "select") input = `<select name="${key}">${opts.map(([o, l]) => `<option value="${o}" ${String(o) === String(v) ? "selected" : ""}>${l}</option>`).join("")}</select>`;
    else if (type === "textarea") input = `<textarea name="${key}" style="min-height:70px">${esc(v)}</textarea>`;
    else input = `<input type="${type}" name="${key}" value="${esc(v)}" ${type === "number" ? 'step="any"' : ""}/>`;
    const extra = key === "webhook_url" ? `<div style="margin-top:6px"><button type="button" class="btn sm" id="btn-test-webhook">测试 Webhook 推送</button></div>` : "";
    return `<div class="field">${type === "bool" ? "" : `<label>${label}</label>`}${input}${help ? `<div class="help">${help}</div>` : ""}${extra}</div>`;
  };
  const cred = (ok, name, env) => `<tr><td>${name}</td><td>${ok ? '<span class="pill ok"><span class="dot"></span>已配置</span>' : '<span class="pill warn"><span class="dot"></span>未配置</span>'}</td><td class="muted"><code>${env}</code></td></tr>`;
  const ru = st.redditdate_usage;
  const rDetail = st.reddit_provider === "redditdate"
    ? `RedditDate API${ru ? ` · ${ru.plan_name || ru.plan}（剩余 ${fmtNum(ru.remaining_records)} / ${fmtNum(ru.monthly_quota)} 条）` : ""}`
    : "Reddit API" + (st.demo_mode ? "（当前演示模式）" : "");
  const rEnv = st.reddit_provider === "redditdate" ? "REDDITDATE_API_KEY / REDDITDATE_BASE_URL" : "REDDITDATE_API_KEY 或 REDDIT_CLIENT_ID";
  view.innerHTML = `
    <div class="card" style="margin-bottom:16px"><h3>凭证状态 <small>密钥只保存在服务器 .env 文件中，不经过前端</small></h3>
      <table><tbody style="cursor:default">
        ${cred(st.reddit_configured, rDetail, rEnv)}
        ${cred(st.llm_configured, "LLM 二创（OpenAI 兼容）" + (st.llm_model ? " · " + st.llm_model : ""), "LLM_API_KEY / LLM_BASE_URL / LLM_MODEL")}
        ${cred(st.image_configured, "AI 配图（可选）" + (st.image_model ? " · " + st.image_model : ""), "IMAGE_MODEL（可选 IMAGE_API_KEY / IMAGE_BASE_URL）")}
      </tbody></table></div>
    <form id="settings-form"><div class="form-grid">
      ${SETTINGS_FORM.map(([title, fields]) => `<div class="card"><h3>${title}</h3>${fields.map(field).join("")}</div>`).join("")}
    </div>
    <div class="row" style="margin-top:16px"><div class="spacer"></div><button class="btn" type="button" id="s-reset">还原</button><button class="btn primary" type="submit">保存设置</button></div></form>`;
  $("#s-reset").addEventListener("click", () => renderSettings(view));
  const notifyToggle = $('[name="desktop_notify"]', view);
  if (notifyToggle) {
    notifyToggle.addEventListener("change", async () => {
      if (notifyToggle.checked && "Notification" in window && Notification.permission !== "granted") {
        const perm = await Notification.requestPermission();
        if (perm !== "granted") {
          notifyToggle.checked = false;
          toast("浏览器桌面通知权限被拒绝", "error");
        }
      }
    });
  }
  const testBtn = $("#btn-test-webhook", view);
  if (testBtn) {
    testBtn.addEventListener("click", (e) => {
      const url = $('[name="webhook_url"]', view).value.trim();
      act(e.target, () => api("/webhook/test", { method: "POST", body: { url } }), "测试推送已发送，请检查接收端");
    });
  }
  $("#settings-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const body = {};
    SETTINGS_FORM.flatMap(([, f]) => f).forEach(([key, , type]) => {
      const inp = $(`[name="${key}"]`, e.target);
      body[key] = type === "bool" ? inp.checked : type === "number" ? Number(inp.value) : inp.value;
    });
    await act(e.submitter, () => api("/settings", { method: "PUT", body }), "设置已保存，定时任务已按新间隔重排");
    await refreshShell();
  });
}

window.addEventListener("hashchange", route);
route();
