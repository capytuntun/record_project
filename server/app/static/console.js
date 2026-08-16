"use strict";

/* 螢幕測錄系統 —— 管理主控台
 *
 * 兩件實作上要知道的事：
 *
 * 1. access token 只放記憶體;refresh token 存 localStorage,讓重整/閒置
 *    不會登出(見下方「連線階段持久化」的取捨說明)。
 * 2. 這個檔案裡沒有任何安全控制。隱藏按鈕只是讓畫面乾淨，伺服器對每個
 *    呼叫都會重新驗證權限（CLAUDE.md 第 25 節）。
 *    絕對不要把權限判斷搬進這個檔案。
 */

const state = {
  accessToken: null,
  refreshToken: null,
  user: null,
  permissions: [],
  auditPage: 1,
};

const $ = (id) => document.getElementById(id);
const can = (perm) => state.permissions.includes(perm);

/* ── 連線階段持久化 ────────────────────────────────────────
 *
 * 重整頁面不該登出。做法:把 refresh token 存進 localStorage,重整後自動
 * 換一張新的 access token 復原登入。access token 仍只放記憶體。
 *
 * 取捨:refresh token 進了 localStorage,XSS 有機會讀到。補償機制是既有的
 * refresh token「輪替 + 重用偵測」—— 被偷的 token 一旦被合法用戶端換過就
 * 失效,整條 family 連帶撤銷。對內部管理工具,這個取捨換來不會動不動登出。 */

const RT_KEY = "eem.rt";
const rememberRefreshToken = (t) => { try { localStorage.setItem(RT_KEY, t); } catch {} };
const loadRefreshToken = () => { try { return localStorage.getItem(RT_KEY); } catch { return null; } };
const forgetRefreshToken = () => { try { localStorage.removeItem(RT_KEY); } catch {} };

/* ── 顯示字串 ─────────────────────────────────────────── */

const ROLE_LABEL = { SUPER_ADMIN: "最高管理員", ADMIN: "一般管理員" };
const USER_STATUS_LABEL = { ACTIVE: "啟用中", SUSPENDED: "已停權" };

/* 端點與稽核狀態對應到參考色盤的固定 status 色階。
 *
 * 色彩絕不單獨表意。每個 badge 有三個通道：形狀（glyph）、文字、顏色。
 * 這是必要的而非講究：good 綠與 critical 紅在紅綠色盲下的 ΔE 只有 4.1，
 * 光看顏色分不出來。形狀與文字才是真正在傳達狀態的通道。
 *
 * 同一欄位內不並置 warning 與 serious（兩者 ΔE 13.6，低於 15 的可辨識下限），
 * 所以端點狀態用 good/warning/neutral/critical，serious 只出現在稽核結果欄。 */
const STATUS = {
  ONLINE:       { text: "線上",   tone: "good",     glyph: "dot" },
  WARNING:      { text: "警告",   tone: "warning",  glyph: "triangle" },
  OFFLINE:      { text: "離線",   tone: "neutral",  glyph: "ring" },
  DISABLED:     { text: "已停用", tone: "critical", glyph: "slash" },
  UNREGISTERED: { text: "未註冊", tone: "neutral",  glyph: "ring" },
  SUCCESS:      { text: "成功",   tone: "good",     glyph: "dot" },
  FAILURE:      { text: "失敗",   tone: "serious",  glyph: "triangle" },
  DENIED:       { text: "拒絕",   tone: "critical", glyph: "slash" },
  VALID:        { text: "可使用", tone: "good",     glyph: "dot" },
  // 永不過期的憑證可用，但值得在列表裡看得出來。
  PERPETUAL:    { text: "永久有效", tone: "warning", glyph: "triangle" },
  EXPIRED:      { text: "已過期", tone: "neutral",  glyph: "ring" },
  EXHAUSTED:    { text: "次數用完", tone: "neutral", glyph: "ring" },
  REVOKED:      { text: "已撤銷", tone: "critical", glyph: "slash" },
};

const GLYPH_PATHS = {
  dot:      [["circle", { cx: 8, cy: 8, r: 4, fill: "currentColor" }]],
  ring:     [["circle", { cx: 8, cy: 8, r: 4, fill: "none", stroke: "currentColor", "stroke-width": 2 }]],
  triangle: [["path", { d: "M8 2.8 14.6 13.6H1.4z", fill: "currentColor" }]],
  slash:    [
    ["circle", { cx: 8, cy: 8, r: 5, fill: "none", stroke: "currentColor", "stroke-width": 2 }],
    ["path", { d: "M4.6 11.4 11.4 4.6", stroke: "currentColor", "stroke-width": 2, "stroke-linecap": "round" }],
  ],
};

const SVG_NS = "http://www.w3.org/2000/svg";

function glyph(name) {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", "0 0 16 16");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("class", "glyph");
  for (const [tag, attrs] of GLYPH_PATHS[name] || GLYPH_PATHS.dot) {
    const node = document.createElementNS(SVG_NS, tag);
    for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, String(value));
    svg.appendChild(node);
  }
  return svg;
}

/* ── HTTP ─────────────────────────────────────────────── */

async function request(method, path, body, retry = true) {
  const options = { method, headers: {} };
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  if (state.accessToken) options.headers["Authorization"] = "Bearer " + state.accessToken;

  const response = await fetch(path, options);
  const payload = await response.json().catch(() => ({}));

  // 透明重試一次，失敗就回到登入畫面。
  if (response.status === 401 && retry && state.refreshToken) {
    if (await refreshSession()) return request(method, path, body, false);
    signOut("連線階段已結束，請重新登入。");
    throw new Error("session_ended");
  }

  if (!response.ok) {
    const error = new Error(payload.message || "requestFailed");
    error.status = response.status;
    error.requestId = payload.requestId;
    throw error;
  }
  return payload;
}

/** POST raw bytes (e.g. a JPEG screenshot) with the same 401-refresh handling. */
async function postBinary(path, blob, contentType, retry = true) {
  const options = { method: "POST", headers: { "Content-Type": contentType }, body: blob };
  if (state.accessToken) options.headers["Authorization"] = "Bearer " + state.accessToken;

  const response = await fetch(path, options);
  const payload = await response.json().catch(() => ({}));

  if (response.status === 401 && retry && state.refreshToken) {
    if (await refreshSession()) return postBinary(path, blob, contentType, false);
    signOut("連線階段已結束，請重新登入。");
    throw new Error("session_ended");
  }
  if (!response.ok) {
    const error = new Error(payload.message || "requestFailed");
    error.status = response.status;
    error.requestId = payload.requestId;
    throw error;
  }
  return payload;
}

/** Fetch a protected binary resource and return a blob: object URL for it. */
async function fetchAuthBlobUrl(path) {
  const response = await fetch(path, {
    headers: { Authorization: "Bearer " + state.accessToken },
  });
  if (!response.ok) throw new Error("載入失敗");
  return URL.createObjectURL(await response.blob());
}

async function refreshSession() {
  try {
    const response = await fetch("/api/auth/refresh", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ refreshToken: state.refreshToken }),
    });
    if (!response.ok) return false;
    const data = await response.json();
    applyTokens(data);
    return true;
  } catch {
    return false;
  }
}

/** Store new tokens and schedule the next silent refresh before this one expires. */
function applyTokens(data) {
  state.accessToken = data.accessToken;
  state.refreshToken = data.refreshToken;
  rememberRefreshToken(data.refreshToken);

  // Proactively refresh ~1 minute before the access token expires, so an idle
  // session renews itself instead of failing the next action with a 401.
  clearTimeout(applyTokens.timer);
  const ttlMs = Math.max(30, (data.expiresIn || 3600) - 60) * 1000;
  applyTokens.timer = setTimeout(() => { refreshSession(); }, ttlMs);
}

/* ── DOM helpers（一律 textContent，不用 innerHTML）────── */

function el(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = String(text);
  if (className) node.className = className;
  return node;
}

function badge(code) {
  const meta = STATUS[code] || { text: code || "－", tone: "neutral", glyph: "ring" };
  const wrap = el("span", null, "badge b-" + meta.tone);
  wrap.appendChild(glyph(meta.glyph));
  wrap.appendChild(el("span", meta.text));
  return wrap;
}

function pill(text, strong) {
  return el("span", text, "pill" + (strong ? " strong" : ""));
}

function row(cells) {
  const tr = document.createElement("tr");
  for (const cell of cells) {
    const td = document.createElement("td");
    if (cell instanceof Node) td.appendChild(cell);
    else td.textContent = cell === null || cell === undefined || cell === "" ? "－" : String(cell);
    tr.appendChild(td);
  }
  return tr;
}

function fillTable(tableId, rows, emptyText) {
  const table = $(tableId);
  const body = table.querySelector("tbody");
  body.replaceChildren();
  if (!rows.length) {
    const tr = document.createElement("tr");
    const td = el("td", emptyText, "empty");
    td.colSpan = table.querySelectorAll("thead th").length;
    tr.appendChild(td);
    body.appendChild(tr);
    return;
  }
  rows.forEach((r) => body.appendChild(r));
}

function when(iso) {
  if (!iso) return "－";
  const d = new Date(iso);
  if (isNaN(d)) return "－";
  return d.toLocaleString("zh-TW", {
    year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", hour12: false,
  });
}

function flash(message, isError) {
  const node = $("flash");
  node.textContent = message;
  node.classList.toggle("bad", Boolean(isError));
  node.hidden = false;
  clearTimeout(flash.timer);
  flash.timer = setTimeout(() => { node.hidden = true; }, 6000);
}

async function guard(fn) {
  try { await fn(); }
  catch (err) {
    if (err.message === "session_ended") return;
    flash(err.requestId ? `${err.message}（請求編號 ${err.requestId}）` : err.message, true);
  }
}

/* ── 連線階段 ─────────────────────────────────────────── */

$("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const errorNode = $("login-error");
  const submit = $("login-submit");
  errorNode.hidden = true;
  submit.disabled = true;
  submit.textContent = "登入中…";

  try {
    const data = await request("POST", "/api/auth/login", {
      username: $("login-username").value,
      password: $("login-password").value,
    }, false);

    applyTokens(data);
    state.user = data.user;
    state.permissions = data.permissions || [];

    $("login-password").value = "";
    enterConsole();
  } catch (err) {
    errorNode.textContent = err.message === "requestFailed" ? "登入失敗，請稍後再試。" : err.message;
    errorNode.hidden = false;
  } finally {
    submit.disabled = false;
    submit.textContent = "登入";
  }
});

$("logout").addEventListener("click", () => {
  if (state.refreshToken) {
    request("POST", "/api/auth/logout", { refreshToken: state.refreshToken }, false).catch(() => {});
  }
  signOut("已登出。");
});

function signOut(message) {
  clearTimeout(applyTokens.timer);
  stopWallStreams();
  state.accessToken = null;
  state.refreshToken = null;
  state.user = null;
  state.permissions = [];
  forgetRefreshToken();
  $("app-view").hidden = true;
  $("login-view").hidden = false;
  if (message) {
    $("login-error").textContent = message;
    $("login-error").hidden = false;
  }
}

/**
 * On page load, restore the session from the stored refresh token so a reload
 * does not force a re-login. Falls back to the login screen if there is no
 * stored token or it has expired/been revoked.
 */
async function restoreSession() {
  const stored = loadRefreshToken();
  if (!stored) {
    showLogin();
    return;
  }

  state.refreshToken = stored;
  if (!(await refreshSession())) {
    forgetRefreshToken();
    state.refreshToken = null;
    showLogin();
    return;
  }
  try {
    const me = await request("GET", "/api/auth/me");
    state.user = me.user;
    state.permissions = me.permissions || [];
    enterConsole();
  } catch {
    forgetRefreshToken();
    showLogin();
  }
}

/** Reveal the login view (and hide the booting splash / console). */
function showLogin() {
  $("booting").hidden = true;
  $("app-view").hidden = true;
  $("login-view").hidden = false;
}

function enterConsole() {
  $("booting").hidden = true;
  $("login-view").hidden = true;
  $("app-view").hidden = false;

  $("who-name").textContent = state.user.username;
  $("who-role").textContent = ROLE_LABEL[state.user.role] || state.user.role;
  $("who-initial").textContent = state.user.username.slice(0, 1);

  // 只是版面整理：伺服器仍會對每個呼叫重新驗證。
  document.querySelectorAll("[data-perm]").forEach((node) => {
    node.hidden = !can(node.dataset.perm);
  });
  $("create-user-card").hidden = !can("users:create");

  show("dashboard");
}

/* ── 導覽 ─────────────────────────────────────────────── */

document.querySelectorAll(".nav-item").forEach((item) => {
  item.addEventListener("click", () => show(item.dataset.view));
});

function show(name) {
  // Leaving the wall must tear down its live streams (stops the agents capturing).
  if (name !== "wall") stopWallStreams();
  document.querySelectorAll(".view").forEach((v) => { v.hidden = true; });
  document.querySelectorAll(".nav-item").forEach((t) => t.classList.remove("active"));
  $("view-" + name).hidden = false;
  document.querySelector(`.nav-item[data-view="${name}"]`).classList.add("active");

  guard({
    dashboard: loadDashboard,
    endpoints: loadEndpoints,
    wall: loadWall,
    users: loadUsers,
    groups: loadGroups,
    recordings: loadRecordings,
    tokens: loadPackages,
    alerts: loadAlerts,
    audit: loadAudit,
    storage: loadStorage,
  }[name]);
}

/* ── 總覽 ─────────────────────────────────────────────── */

async function loadDashboard() {
  const summary = await request("GET", "/api/endpoints/summary");
  const stats = $("stats");
  stats.replaceChildren();

  // 與端點狀態欄使用同一組形狀與顏色，兩處才對得起來。
  [
    ["端點總數", summary.total,    "total",    null],
    ["線上",     summary.online,   "good",     "dot"],
    ["警告",     summary.warning,  "warning",  "triangle"],
    ["離線",     summary.offline,  "neutral",  "ring"],
    ["已停用",   summary.disabled, "critical", "slash"],
  ].forEach(([label, value, tone, shape]) => {
    const card = el("div", null, "stat");
    const key = el("div", null, "k t-" + tone);
    if (shape) key.appendChild(glyph(shape));
    key.appendChild(el("span", label));
    card.appendChild(key);
    card.appendChild(el("div", value ?? 0, "n"));
    stats.appendChild(card);
  });

  await loadUninstallAlerts();

  const list = await request("GET", "/api/endpoints?pageSize=10");
  fillTable("recent-table", list.items.map((e) => row([
    e.deviceName, e.localUser, e.ip, e.agentVersion, when(e.lastSeenAt), badge(e.status),
  ])), "尚未有任何端點註冊。到「安裝包」產生註冊憑證後，Agent 完成註冊就會出現在這裡。");
}

/* 有人在端點上試圖移除 Agent 但密碼不對 —— 這是防竄改事件，不該只躺在稽核紀錄
 * 裡等人去翻，所以放在總覽最上面。只有看得到稽核的人（預設＝最高管理員）會拿到。 */
async function loadUninstallAlerts() {
  const box = $("uninstall-alert");
  if (!can("audit_logs:read")) { box.hidden = true; return; }

  let data;
  try {
    data = await request("GET", "/api/audit-logs?action=UNINSTALL_ATTEMPT&pageSize=5");
  } catch {
    box.hidden = true;          // 看不到就算了，不要讓總覽整頁失敗
    return;
  }
  if (!data.items.length) { box.hidden = true; return; }

  box.replaceChildren();
  box.appendChild(el("strong", `有人嘗試移除端點上的 Agent（${data.total} 次）`));
  data.items.forEach((item) => {
    const meta = item.metadata || {};      // 伺服器已經解析好，這裡是物件不是字串
    const who = meta.localUser ? `使用者 ${meta.localUser}` : "未知使用者";
    const what = {
      CANCELLED: "取消了密碼輸入",
      NO_PASSWORD: "以靜默方式嘗試移除（未提供密碼）",
    }[meta.outcome] || "密碼輸入錯誤";
    const more = meta.suppressed ? `（另有 ${meta.suppressed} 次未逐筆記錄）` : "";
    box.appendChild(el("div",
      `${when(item.timestamp)}　${meta.deviceName || item.targetId}　${who}　${what}${more}`));
  });
  box.hidden = false;
}

/* ── 端點 ─────────────────────────────────────────────── */

$("ep-refresh").addEventListener("click", () => guard(loadEndpoints));
$("ep-search").addEventListener("keydown", (e) => { if (e.key === "Enter") guard(loadEndpoints); });
$("ep-status").addEventListener("change", () => guard(loadEndpoints));
$("ep-group").addEventListener("change", () => guard(loadEndpoints));

/** 依目前端點清單裡出現過的群組,重建群組篩選下拉;保留目前選擇。 */
function populateGroupFilter(items) {
  const select = $("ep-group");
  const current = select.value;
  const names = [...new Set(items.flatMap((e) => e.groups || []))]
    .sort((a, b) => a.localeCompare(b, "zh-Hant"));
  select.replaceChildren();
  select.appendChild(new Option("所有群組", ""));
  names.forEach((n) => select.appendChild(new Option(n, n)));
  select.value = names.includes(current) ? current : "";
}

/** 群組欄：把端點所屬群組畫成小標籤;沒有群組顯示「－」。 */
function groupsCell(groups) {
  if (!groups || !groups.length) return "－";
  const wrap = el("span", null, "row-tags");
  groups.forEach((g) => wrap.appendChild(pill(g)));
  return wrap;
}

async function loadEndpoints() {
  const params = new URLSearchParams({ pageSize: "100" });
  if ($("ep-search").value.trim()) params.set("search", $("ep-search").value.trim());
  if ($("ep-status").value) params.set("status", $("ep-status").value);

  const data = await request("GET", "/api/endpoints?" + params);

  // Group filter is applied client-side (like the status filter): rebuild the
  // options from the groups present in the result, keep the current pick, then
  // filter the rows shown.
  populateGroupFilter(data.items);
  const groupPick = $("ep-group").value;
  const shown = groupPick
    ? data.items.filter((e) => (e.groups || []).includes(groupPick))
    : data.items;

  fillTable("ep-table", shown.map((e) => {
    const actions = el("span", null, "row-actions");

    if (can("endpoints:screen:view") && e.state !== "DISABLED") {
      const view = el("button", "檢視畫面", "ghost");
      view.addEventListener("click", () => guard(() => openScreenViewer(e)));
      actions.appendChild(view);
    }
    if (can("endpoints:screen:view")) {
      const playback = el("button", "回放", "ghost");
      if (e.recordingAvailable) {
        playback.addEventListener("click", () => guard(() => openPlayback(e)));
      } else {
        // Nothing recorded and no recording policy -> nothing to replay.
        playback.disabled = true;
        playback.title = "此端點沒有錄影政策，也沒有可回放的錄影";
      }
      actions.appendChild(playback);
    }
    if (can("endpoints:screen:view")) {
      const shots = el("button", "截圖", "ghost");
      if (e.hasScreenshots) {
        shots.addEventListener("click", () => guard(() => openGallery(e)));
      } else {
        shots.disabled = true;
        shots.title = "此端點尚無截圖";
      }
      actions.appendChild(shots);
    }
    {
      const inv = el("button", "資產", "ghost");
      inv.addEventListener("click", () => guard(() => openInventory(e)));
      actions.appendChild(inv);
    }
    if (can("endpoints:disable")) {
      const isDisabled = e.state === "DISABLED";
      const toggle = el("button", isDisabled ? "啟用" : "停用",
                        isDisabled ? "ghost" : "ghost danger");
      toggle.addEventListener("click", () => guard(async () => {
        if (!isDisabled && !confirm(
          `確定停用「${e.deviceName || e.id}」？\n\n` +
          `這會撤銷該端點的憑證，Agent 必須重新註冊才能再次連線。`
        )) return;
        await request("POST", `/api/endpoints/${e.id}/${isDisabled ? "enable" : "disable"}`, {});
        flash(isDisabled ? "端點已啟用。" : "端點已停用，憑證已撤銷。");
        loadEndpoints();
      }));
      actions.appendChild(toggle);
    }
    if (can("endpoints:delete")) {
      const del = el("button", "刪除", "ghost danger");
      del.addEventListener("click", () => guard(async () => {
        if (!confirm(
          `確定刪除端點「${e.deviceName || e.id}」？\n\n` +
          `採軟刪除並撤銷憑證；稽核紀錄仍可追溯。之後 Agent 需重新註冊才會再出現。`
        )) return;
        await request("DELETE", `/api/endpoints/${e.id}`);
        flash("端點已刪除。");
        loadEndpoints();
      }));
      actions.appendChild(del);
    }
    return row([e.deviceName, e.organizationId || "－", groupsCell(e.groups),
                e.localUser, e.ip, e.os, when(e.lastSeenAt), badge(e.status), actions]);
  }), "沒有符合條件的端點。");
}

/* ── 端點資產清單（inventory）─────────────────────────────
 *
 * Agent 每 6 小時（安裝後首次心跳也會）回報一次延伸資產：作業系統版本、
 * CPU、記憶體、系統磁碟、開機時間、已安裝軟體清單。這裡以唯讀 modal 呈現，
 * 只有資產資料 —— 沒有文件內容、沒有活動軌跡（資料最小化）。 */

function fmtUptime(seconds) {
  const d = Math.floor(seconds / 86400);
  const h = Math.floor((seconds % 86400) / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (d > 0) return `${d} 天 ${h} 小時`;
  if (h > 0) return `${h} 小時 ${m} 分`;
  return `${m} 分`;
}

async function openInventory(endpoint) {
  const detail = await request("GET", `/api/endpoints/${endpoint.id}`);
  const inv = detail.inventory;

  const overlay = el("div", null, "modal");
  const box = el("div", null, "modal-box");
  box.style.width = "min(760px, 100%)";

  const head = el("div", null, "modal-head");
  const titleWrap = el("div");
  titleWrap.appendChild(el("strong", `資產清單 — ${endpoint.deviceName || endpoint.id}`));
  titleWrap.appendChild(el("span", inv ? "更新於 " + when(inv.collectedAt) : "尚未回報資產資料", "muted"));
  head.appendChild(titleWrap);
  const close = el("button", "關閉", "ghost");
  close.style.marginLeft = "auto";
  head.appendChild(close);
  box.appendChild(head);

  const bodyWrap = el("div");
  bodyWrap.style.padding = "1rem 1.1rem";
  bodyWrap.style.overflow = "auto";

  if (!inv) {
    bodyWrap.appendChild(el("p",
      "此端點的 Agent 尚未回報延伸資產清單。安裝後首次心跳、之後每 6 小時各回報一次。",
      "muted"));
  } else {
    const gb = (mb) => (mb / 1024).toFixed(1);
    const facts = [
      ["作業系統", (detail.os || "－") + (inv.osBuild ? `（版本 ${inv.osBuild}）` : "")],
      ["處理器", inv.cpu ? `${inv.cpu}　${inv.cpuCores || "?"} 核` : "－"],
      ["記憶體", inv.memoryTotalMb ? `可用 ${gb(inv.memoryFreeMb)} / ${gb(inv.memoryTotalMb)} GB` : "－"],
      ["系統磁碟", inv.diskTotalGb
        ? `可用 ${inv.diskFreeGb} / ${inv.diskTotalGb} GB（${inv.diskFreePercent}%）`
        : "－"],
      ["開機時間", inv.uptimeSeconds ? fmtUptime(inv.uptimeSeconds) : "－"],
      ["已安裝軟體", (inv.softwareCount ?? 0) + " 個"],
    ];
    const ft = el("table", null, "kv-table");
    ft.style.width = "100%";
    ft.style.marginBottom = "1rem";
    facts.forEach(([k, v]) => {
      const tr = el("tr");
      const th = el("td", k);
      th.style.color = "var(--muted)";
      th.style.width = "9rem";
      th.style.padding = ".25rem .5rem .25rem 0";
      th.style.verticalAlign = "top";
      const td = el("td", v);
      td.style.padding = ".25rem 0";
      tr.appendChild(th); tr.appendChild(td);
      ft.appendChild(tr);
    });
    bodyWrap.appendChild(ft);

    // Low disk gets a visible flag here too, mirroring what the alert engine sees.
    if (typeof inv.diskFreePercent === "number" && inv.diskFreePercent < 10) {
      const warn = el("p", `⚠ 系統磁碟可用空間偏低（${inv.diskFreePercent}%）。`, "muted");
      warn.style.color = "var(--danger, #c0392b)";
      bodyWrap.appendChild(warn);
    }

    const software = inv.software || [];
    if (software.length) {
      const search = el("input");
      search.type = "search";
      search.placeholder = "篩選軟體…";
      search.style.width = "100%";
      search.style.marginBottom = ".5rem";
      bodyWrap.appendChild(search);

      const table = el("table", null, "data");
      table.style.width = "100%";
      const thead = el("thead");
      const htr = el("tr");
      ["名稱", "版本", "發行者"].forEach((h) => htr.appendChild(el("th", h)));
      thead.appendChild(htr);
      table.appendChild(thead);
      const tbody = el("tbody");
      table.appendChild(tbody);
      bodyWrap.appendChild(table);

      const renderSoftware = (filter) => {
        const q = (filter || "").trim().toLowerCase();
        tbody.replaceChildren();
        const rows = software.filter((s) =>
          !q || (s.name || "").toLowerCase().includes(q) ||
          (s.publisher || "").toLowerCase().includes(q));
        rows.slice(0, 500).forEach((s) => {
          tbody.appendChild(row([s.name, s.version, s.publisher]));
        });
        if (!rows.length) {
          const tr = el("tr");
          const td = el("td", "沒有符合的軟體。", "empty");
          td.colSpan = 3;
          tr.appendChild(td); tbody.appendChild(tr);
        }
      };
      search.addEventListener("input", () => renderSoftware(search.value));
      renderSoftware("");
    }
  }

  box.appendChild(bodyWrap);
  overlay.appendChild(box);
  document.body.appendChild(overlay);

  const dispose = () => {
    overlay.remove();
    document.removeEventListener("keydown", onKey);
  };
  const onKey = (ev) => { if (ev.key === "Escape") dispose(); };
  close.addEventListener("click", dispose);
  overlay.addEventListener("click", (ev) => { if (ev.target === overlay) dispose(); });
  document.addEventListener("keydown", onKey);
}

/* ── 端點畫面即時檢視（WebSocket）─────────────────────────
 *
 * 流程：先用一般認證取得一次性 ticket，再用 ticket 開 WebSocket
 * （瀏覽器的 WebSocket 無法帶 Authorization 標頭）。影格是 JPEG binary，
 * 直接畫到 <img>；不落地、不快取。關閉時關閉 socket，伺服器隨即通知
 * Agent 停止擷取 —— 沒有人看時不會擷取畫面。 */

const screenViewer = {
  ws: null,
  objectUrl: null,
  endpoint: null,
  lastFrame: null,   // 最近一張畫面（原始 JPEG blob），供截圖用
  monitorIndex: 0,   // 目前檢視中的螢幕
};

async function openScreenViewer(endpoint) {
  // 每次開啟都會在伺服器寫一筆稽核（誰、看哪台、何時）。
  const session = await request("POST", `/api/endpoints/${endpoint.id}/screen/ticket`, {});

  screenViewer.endpoint = endpoint;
  screenViewer.lastFrame = null;
  screenViewer.monitorIndex = 0;
  $("sv-title").textContent = endpoint.deviceName || endpoint.id;
  // 主機以 IP 標示（而非使用者）。
  $("sv-sub").textContent = endpoint.ip ? `IP：${endpoint.ip}` : "";
  $("sv-shot").disabled = true;
  $("sv-image").hidden = true;
  $("sv-monitors").replaceChildren();
  setScreenStatus(session.agentOnline ? "連線中…" : "等待 Agent 上線…", false);
  $("screen-modal").hidden = false;

  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  const ws = new WebSocket(proto + "//" + window.location.host + session.wsPath);
  ws.binaryType = "blob";
  screenViewer.ws = ws;

  ws.addEventListener("message", (event) => {
    if (typeof event.data === "string") {
      handleScreenControl(JSON.parse(event.data));
    } else {
      renderFrame(event.data);
    }
  });
  ws.addEventListener("close", () => {
    if (screenViewer.ws === ws) setScreenStatus("連線已關閉。", true);
  });
  ws.addEventListener("error", () => setScreenStatus("連線發生錯誤。", true));
}

function handleScreenControl(msg) {
  if (msg.type === "status") {
    setScreenStatus(msg.agentOnline ? "已連線，等待畫面…" : "等待 Agent 上線…", !msg.agentOnline);
  } else if (msg.type === "agent_offline") {
    setScreenStatus("Agent 已離線。", true);
  } else if (msg.type === "monitors") {
    renderMonitorButtons(msg.monitors);
  } else if (msg.type === "error") {
    setScreenStatus("無法檢視：" + (msg.message || ""), true);
  }
}

function renderFrame(blob) {
  const img = $("sv-image");
  const previous = screenViewer.objectUrl;
  // Frames arrive as untyped binary blobs; label them image/jpeg so the browser
  // decodes them without relying on content sniffing.
  const jpeg = blob.type ? blob : new Blob([blob], { type: "image/jpeg" });
  screenViewer.lastFrame = jpeg;         // 留著最近一張，截圖時直接上傳
  $("sv-shot").disabled = false;
  screenViewer.objectUrl = URL.createObjectURL(jpeg);
  img.src = screenViewer.objectUrl;
  img.hidden = false;
  setScreenStatus("", false);
  // 換掉上一張的 object URL，否則影格會一直堆積在記憶體。
  if (previous) URL.revokeObjectURL(previous);
}

function renderMonitorButtons(monitors) {
  const bar = $("sv-monitors");
  bar.replaceChildren();
  if (!monitors || monitors.length <= 1) return;
  monitors.forEach((m, i) => {
    const label = m.primary ? `螢幕 ${i + 1}（主）` : `螢幕 ${i + 1}`;
    const btn = el("button", label, "ghost");
    btn.addEventListener("click", () => {
      if (screenViewer.ws && screenViewer.ws.readyState === WebSocket.OPEN) {
        screenViewer.ws.send(JSON.stringify({ type: "set_monitor", index: i }));
        screenViewer.monitorIndex = i;
        [...bar.children].forEach((c) => c.classList.remove("active"));
        btn.classList.add("active");
      }
    });
    if (i === 0) btn.classList.add("active");
    bar.appendChild(btn);
  });
}

function setScreenStatus(text, isError) {
  const node = $("sv-status");
  node.textContent = text;
  node.classList.toggle("bad", Boolean(isError));
  node.hidden = !text;
}

function closeScreenViewer() {
  if (screenViewer.ws) {
    screenViewer.ws.close();
    screenViewer.ws = null;
  }
  if (screenViewer.objectUrl) {
    URL.revokeObjectURL(screenViewer.objectUrl);
    screenViewer.objectUrl = null;
  }
  $("screen-modal").hidden = true;
  $("sv-image").removeAttribute("src");
}

async function captureScreenshot() {
  const blob = screenViewer.lastFrame;
  const endpoint = screenViewer.endpoint;
  if (!blob || !endpoint) { flash("尚未收到畫面，無法截圖。", true); return; }
  const btn = $("sv-shot");
  btn.disabled = true;
  try {
    const mon = screenViewer.monitorIndex || 0;
    await postBinary(`/api/endpoints/${endpoint.id}/screenshot?monitor=${mon}`,
                     blob, "image/jpeg");
    flash("已儲存截圖，可按下方「查看截圖」檢視。");
    // 立刻反映：端點列的「截圖」鈕即時可點；若相簿正開著也即時刷新（免重整瀏覽器）。
    loadEndpoints().catch(() => {});
    if (!$("gallery-modal").hidden && gallery.endpoint && gallery.endpoint.id === endpoint.id) {
      loadGallery().catch(() => {});
    }
  } finally {
    // 只要還在檢視、仍有畫面，就恢復可截圖。
    btn.disabled = !screenViewer.lastFrame || $("screen-modal").hidden;
  }
}

$("sv-shot").addEventListener("click", () => guard(captureScreenshot));
$("sv-gallery").addEventListener("click", () => guard(() => {
  if (screenViewer.endpoint) return openGallery(screenViewer.endpoint);
}));
$("sv-close").addEventListener("click", closeScreenViewer);
$("screen-modal").addEventListener("click", (event) => {
  // 點背景遮罩關閉，但點對話框本身不關。
  if (event.target === $("screen-modal")) closeScreenViewer();
});
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  if (!$("gl-light").hidden) { $("gl-light").hidden = true; return; }
  if (!$("gallery-modal").hidden) { closeGallery(); return; }
  if (!$("screen-modal").hidden) closeScreenViewer();
});

/* ── 螢幕牆：一頁看所有畫面 ───────────────────────────────
 *
 * 對每台在線端點各開一條即時串流(同單機檢視:ticket → WebSocket → 影格),
 * 縮小成一格顯示。點任一格開全螢幕檢視。離開此頁會關掉所有串流 —— 伺服器隨即
 * 通知各 Agent 停止擷取,沒有人看時不擷取。每格開啟都寫一筆稽核。 */

const WALL_MAX_STREAMS = 24;   // 同時串流上限,避免一次開太多連線
const wall = { streams: [] };

function stopWallStreams() {
  wall.streams.forEach((s) => {
    try { if (s.ws) s.ws.close(); } catch { /* already closed */ }
    if (s.objectUrl) URL.revokeObjectURL(s.objectUrl);
  });
  wall.streams = [];
}

async function loadWall() {
  stopWallStreams();
  const grid = $("wall-grid");
  grid.replaceChildren();
  $("wall-note").hidden = true;

  const data = await request("GET", "/api/endpoints?pageSize=200");
  const eps = (data.items || []).slice()
    .sort((a, b) => (a.status === "ONLINE" ? 0 : 1) - (b.status === "ONLINE" ? 0 : 1));
  $("wall-empty").hidden = eps.length > 0;

  let streaming = 0;
  let capped = 0;
  for (const e of eps) {
    const tile = el("figure", null, "wall-tile");
    const img = el("img");
    img.alt = e.deviceName || e.id;
    const status = el("span", null, "wall-status");
    const monitors = el("span", null, "wall-monitors");   // 雙螢幕切換鈕（有多螢幕才顯示）
    const cap = el("figcaption", null, "wall-cap");
    cap.append(el("span", e.deviceName || e.id, "wall-name"),
               el("span", e.ip || "", "wall-ip"));
    tile.append(img, status, monitors, cap);
    tile.addEventListener("click", () => guard(() => openScreenViewer(e)));
    grid.appendChild(tile);

    if (e.status !== "ONLINE") {
      tile.classList.add("offline");
      status.textContent = e.status === "DISABLED" ? "已停用" : "離線";
      continue;
    }
    if (streaming >= WALL_MAX_STREAMS) {
      capped++;
      tile.classList.add("offline");
      status.textContent = "未串流（超過上限）";
      continue;
    }
    streaming++;
    status.textContent = "連線中…";
    openWallTile(e, img, status, tile, monitors);
  }

  if (capped) {
    const note = $("wall-note");
    note.textContent =
      `同時最多串流 ${WALL_MAX_STREAMS} 台，其餘 ${capped} 台線上端點未即時串流；點該格仍可單獨全螢幕檢視。`;
    note.hidden = false;
  }
}

async function openWallTile(endpoint, img, status, tile, monitorsBar) {
  const stream = { endpoint, ws: null, objectUrl: null, monitorIndex: 0 };
  wall.streams.push(stream);
  try {
    // 每格都走一次性 ticket + WebSocket，與單機檢視相同（含 RBAC scope 與稽核）。
    const session = await request("POST", `/api/endpoints/${endpoint.id}/screen/ticket`, {});
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    const ws = new WebSocket(proto + "//" + window.location.host + session.wsPath);
    ws.binaryType = "blob";
    stream.ws = ws;

    ws.addEventListener("message", (event) => {
      if (typeof event.data === "string") {
        const msg = JSON.parse(event.data);
        if (msg.type === "agent_offline") { status.textContent = "Agent 已離線"; tile.classList.add("offline"); }
        else if (msg.type === "error") { status.textContent = "無法檢視"; }
        else if (msg.type === "monitors") { renderWallMonitors(monitorsBar, msg.monitors, ws, stream); }
        return;
      }
      const jpeg = event.data.type ? event.data : new Blob([event.data], { type: "image/jpeg" });
      const previous = stream.objectUrl;
      stream.objectUrl = URL.createObjectURL(jpeg);
      img.src = stream.objectUrl;
      tile.classList.add("live");
      status.textContent = "";
      if (previous) URL.revokeObjectURL(previous);
    });
    ws.addEventListener("close", () => {
      if (!tile.classList.contains("live")) status.textContent = "連線已關閉";
    });
    ws.addEventListener("error", () => { status.textContent = "連線發生錯誤"; });
  } catch (err) {
    status.textContent = "無法連線";
  }
}

function renderWallMonitors(bar, monitors, ws, stream) {
  bar.replaceChildren();
  if (!monitors || monitors.length <= 1) return;   // 單螢幕不顯示切換鈕
  monitors.forEach((m, i) => {
    const active = i === (stream.monitorIndex || 0);
    const btn = el("button", String(i + 1), "wall-mon" + (active ? " active" : ""));
    btn.title = m.primary ? `螢幕 ${i + 1}（主）` : `螢幕 ${i + 1}`;
    btn.addEventListener("click", (event) => {
      event.stopPropagation();          // 只切換螢幕，不要開全螢幕檢視
      if (ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: "set_monitor", index: i }));
        stream.monitorIndex = i;
        [...bar.children].forEach((c) => c.classList.remove("active"));
        btn.classList.add("active");
      }
    });
    bar.appendChild(btn);
  });
}

$("wall-refresh").addEventListener("click", () => guard(loadWall));

/* ── 截圖紀錄（相簿）─────────────────────────────────────
 *
 * 端點存過的截圖列表。每張都是伺服器端加密的 JPEG,用 fetch 帶授權標頭抓回、
 * 轉成 blob 顯示;點縮圖可放大／下載。刪除會同時移除加密檔與索引。每次讀取與
 * 刪除都在伺服器寫稽核。 */

const gallery = { endpoint: null, urls: [] };

function revokeGalleryUrls() {
  gallery.urls.forEach((u) => URL.revokeObjectURL(u));
  gallery.urls = [];
}

async function openGallery(endpoint) {
  gallery.endpoint = endpoint;
  $("gl-title").textContent = endpoint.deviceName || endpoint.id;
  $("gl-sub").textContent = endpoint.ip ? `IP：${endpoint.ip}` : "";
  $("gl-light").hidden = true;
  $("gl-grid").replaceChildren();
  $("gl-status").textContent = "載入中…";
  $("gl-status").hidden = false;
  $("gallery-modal").hidden = false;
  await loadGallery();
}

async function loadGallery() {
  revokeGalleryUrls();
  const grid = $("gl-grid");
  grid.replaceChildren();
  const data = await request("GET",
    `/api/endpoints/${gallery.endpoint.id}/screenshots?pageSize=100`);
  if (!data.items.length) {
    $("gl-status").textContent = "尚無截圖。";
    $("gl-status").hidden = false;
    return;
  }
  $("gl-status").hidden = true;
  for (const shot of data.items) {
    const cell = el("figure", null, "gl-cell");
    const img = el("img");
    img.alt = "截圖"; img.loading = "lazy";
    try {
      const url = await fetchAuthBlobUrl(`/api/screenshots/${shot.id}/image`);
      gallery.urls.push(url);
      img.src = url;
      img.addEventListener("click", () => openLightbox(url, shot));
    } catch { img.alt = "（無法載入）"; }
    const cap = el("figcaption", when(shot.takenAt), "gl-cap");
    const del = el("button", "刪除", "ghost danger small");
    del.addEventListener("click", (event) => {
      event.stopPropagation();
      guard(() => deleteScreenshot(shot));
    });
    cell.append(img, cap, del);
    grid.appendChild(cell);
  }
}

function openLightbox(url, shot) {
  $("gl-light-img").src = url;
  const dl = $("gl-light-dl");
  dl.href = url;
  const stamp = (shot.takenAt || "").replace(/[:T]/g, "-").slice(0, 19);
  dl.download = `screenshot-${stamp || shot.id}.jpg`;
  $("gl-light").hidden = false;
}

async function deleteScreenshot(shot) {
  if (!confirm("確定刪除這張截圖？此動作無法復原。")) return;
  await request("DELETE", `/api/screenshots/${shot.id}`);
  flash("截圖已刪除。");
  $("gl-light").hidden = true;
  await loadGallery();
  // 相簿可能已清空 → 重新整理端點列表，讓「截圖」按鈕停用。
  if (!$("gl-grid").children.length) loadEndpoints();
}

function closeGallery() {
  revokeGalleryUrls();
  $("gallery-modal").hidden = true;
  $("gl-light").hidden = true;
  $("gl-grid").replaceChildren();
  $("gl-light-img").removeAttribute("src");
}

$("gl-close").addEventListener("click", closeGallery);
$("gl-light-close").addEventListener("click", () => { $("gl-light").hidden = true; });
$("gallery-modal").addEventListener("click", (event) => {
  if (event.target === $("gallery-modal")) closeGallery();
});

/* ── 錄影回放 ─────────────────────────────────────────────
 *
 * 依端點 + 日期取出當天的錄影片段(每段一個加密 H.264 檔),排成時間軸。
 * 點色塊即播放:用 fetch 帶授權標頭把片段抓回、解密由伺服器處理,轉成
 * blob 餵給 <video>(瀏覽器的 <video src> 無法帶標頭)。一段播完自動接下一段。
 * 每次抓片段都在伺服器寫稽核(誰、哪台、哪段時間)。 */

const playback = { endpoint: null, segments: [], index: -1, objectUrl: null, span: null };
// 時間軸拖曳狀態。拖曳中要壓掉 timeupdate 對標頭的更新，否則播放中的時間會蓋掉
// 使用者正在拉的目標時刻。
const scrub = { active: false };

async function openPlayback(endpoint) {
  playback.endpoint = endpoint;
  $("pb-title").textContent = `回放 — ${endpoint.deviceName || endpoint.id}`;
  $("pb-time").textContent = "";
  // default to today (local date)
  const now = new Date();
  $("pb-date").value = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;
  $("pb-seek-time").value = "";
  $("playback-modal").hidden = false;
  await loadTimeline();
}

async function loadTimeline() {
  releasePlaybackUrl();
  $("pb-video").removeAttribute("src");
  $("pb-video").load();
  setPbStatus("載入中…", false);

  const [y, m, d] = $("pb-date").value.split("-").map(Number);
  const dayStart = new Date(y, m - 1, d, 0, 0, 0);
  const dayEnd = new Date(y, m - 1, d, 23, 59, 59);

  // A day can hold more segments than one page, so page through them all.
  const items = [];
  let page = 1;
  for (;;) {
    const params = new URLSearchParams({
      from: dayStart.toISOString(), to: dayEnd.toISOString(),
      page: String(page), pageSize: "200",
    });
    const data = await request("GET",
      `/api/recordings/endpoints/${playback.endpoint.id}/segments?${params}`);
    items.push(...(data.items || []));
    if (!data.hasMore || page > 50) break;
    page += 1;
  }

  // API returns newest-first; play in chronological order.
  playback.segments = items.sort((a, b) => new Date(a.startedAt) - new Date(b.startedAt));
  playback.index = -1;

  renderTimeline();
  renderBookmarks();   // chips + timeline markers (re-added after renderTimeline rebuilds the bar)
  if (!playback.segments.length) {
    setPbStatus("這一天沒有錄影。", false);
  } else {
    setPbStatus(
      `共 ${playback.segments.length} 段，${timeLabel(playback.span.first)}–${timeLabel(playback.span.last)}。`
      + "在時間軸上拖曳到想看的時刻，或填「跳到」的時間（可到秒）。", false);
  }
}

const segStart = (seg) => new Date(seg.startedAt).getTime();
const segEnd = (seg) => new Date(seg.endedAt || seg.startedAt).getTime();

function renderTimeline() {
  const bar = $("pb-timeline");
  // 播放頭是時間軸的固定成員，不要跟著片段一起被清掉。
  const head = $("pb-playhead");
  bar.replaceChildren();
  if (head) bar.appendChild(head);
  const segs = playback.segments;
  if (!segs.length) {
    $("pb-start-label").textContent = "";
    $("pb-end-label").textContent = "";
    playback.span = null;
    if (head) head.hidden = true;
    return;
  }
  // Span the actual recorded range so sparse recordings are still clickable.
  const first = segStart(segs[0]);
  const last = segEnd(segs[segs.length - 1]);
  const span = Math.max(1, last - first);
  // 記下來，讓點時間軸可以反推「點到的是哪一個時刻」。
  playback.span = { first, last, span };
  $("pb-start-label").textContent = timeLabel(first);
  $("pb-end-label").textContent = timeLabel(last);

  segs.forEach((seg, i) => {
    const start = segStart(seg);
    const end = segEnd(seg);
    const block = document.createElement("button");
    block.className = "pb-block";
    block.style.left = `${((start - first) / span) * 100}%`;
    block.style.width = `${Math.max(0.6, ((end - start) / span) * 100)}%`;
    block.title = `${timeLabel(start)} – ${timeLabel(end)}`;
    // 定位一律交給時間軸本身處理（見下方拖曳），色塊只負責顯示哪裡有錄影。
    block.tabIndex = -1;
    bar.appendChild(block);
  });
}

/* ── 依時刻定位 ────────────────────────────────────────────
 *
 * 錄影在磁碟上是一段一段的加密檔（預設 5 分鐘一段），這是儲存與加密的單位，
 * 不是能不能精準定位的限制：每段都有 startedAt/endedAt，段內又可以 seek，
 * 所以任何一個「年月日時分秒」都能換算成「第幾段的第幾秒」。 */

function locateTime(targetMs) {
  const segs = playback.segments;
  for (let i = 0; i < segs.length; i++) {
    if (targetMs >= segStart(segs[i]) && targetMs < segEnd(segs[i])) {
      return { index: i, offset: (targetMs - segStart(segs[i])) / 1000, exact: true };
    }
  }
  // 落在空檔（當時沒錄影）→ 往後找最近的一段，並告訴使用者跳了多少。
  for (let i = 0; i < segs.length; i++) {
    if (segStart(segs[i]) >= targetMs) return { index: i, offset: 0, exact: false };
  }
  return null;
}

async function seekToTime(targetMs) {
  const hit = locateTime(targetMs);
  if (!hit) {
    setPbStatus(`${stampLabel(targetMs)} 之後沒有錄影。`, true);
    return;
  }
  await playSegment(hit.index, hit.offset);
  if (!hit.exact) {
    const jumped = segStart(playback.segments[hit.index]);
    setPbStatus(
      `${stampLabel(targetMs)} 沒有錄影，已跳到之後最近的 ${stampLabel(jumped)}。`, false);
  }
}

async function playSegment(index, offsetSeconds) {
  const seg = playback.segments[index];
  if (!seg) return;
  playback.index = index;
  highlightBlock(index);
  setPbStatus("載入片段…", false);

  const response = await fetch(`/api/recordings/segments/${seg.id}/video`, {
    headers: { Authorization: "Bearer " + state.accessToken },
  });
  if (!response.ok) {
    setPbStatus("無法載入此片段。", true);
    return;
  }
  releasePlaybackUrl();
  const blob = await response.blob();
  playback.objectUrl = URL.createObjectURL(blob);

  const video = $("pb-video");
  video.src = playback.objectUrl;
  video.onloadedmetadata = () => {
    if (offsetSeconds > 0) video.currentTime = Math.min(offsetSeconds, video.duration || offsetSeconds);
    applyPlaybackRate();   // keep the chosen speed across segment boundaries
    video.play().catch(() => {});
  };
  setPbStatus("", false);
}

/* ── 回放：倍速、段落跳轉、書籤、時間軸滑過預覽 ─────────── */

function applyPlaybackRate() {
  $("pb-video").playbackRate = parseFloat($("pb-speed").value) || 1;
}
$("pb-speed").addEventListener("change", applyPlaybackRate);

$("pb-prev-seg").addEventListener("click", () => {
  if (playback.index > 0) guard(() => playSegment(playback.index - 1, 0));
});
$("pb-next-seg").addEventListener("click", () => {
  if (playback.index >= 0 && playback.index < playback.segments.length - 1) {
    guard(() => playSegment(playback.index + 1, 0));
  }
});

function bookmarkKey() { return "pb-bookmarks:" + (playback.endpoint ? playback.endpoint.id : ""); }
function getBookmarks() {
  try { return JSON.parse(localStorage.getItem(bookmarkKey()) || "[]"); } catch { return []; }
}
function setBookmarks(list) {
  localStorage.setItem(bookmarkKey(), JSON.stringify(list));
}

function renderBookmarks() {
  const wrap = $("pb-bookmarks");
  wrap.replaceChildren();
  getBookmarks().sort((a, b) => a.ms - b.ms).forEach((bm) => {
    const chip = el("span", null, "pill");
    const label = el("span", stampLabel(bm.ms));
    label.style.cursor = "pointer";
    label.addEventListener("click", () => guard(() => seekToTime(bm.ms)));
    const x = el("button", "✕");
    x.title = "移除書籤";
    x.addEventListener("click", () => {
      setBookmarks(getBookmarks().filter((b) => b.ms !== bm.ms));
      renderBookmarks();
    });
    chip.appendChild(label); chip.appendChild(x);
    wrap.appendChild(chip);
  });
  renderTimelineMarkers();
}

function renderTimelineMarkers() {
  const bar = $("pb-timeline");
  [...bar.querySelectorAll(".pb-mark")].forEach((n) => n.remove());
  if (!playback.span) return;
  getBookmarks().forEach((bm) => {
    if (bm.ms < playback.span.first || bm.ms > playback.span.last) return;
    const mark = el("div", null, "pb-mark");
    mark.style.left = `${((bm.ms - playback.span.first) / playback.span.span) * 100}%`;
    mark.title = "書籤：" + stampLabel(bm.ms);
    mark.addEventListener("click", (e) => { e.stopPropagation(); guard(() => seekToTime(bm.ms)); });
    bar.appendChild(mark);
  });
}

$("pb-bookmark").addEventListener("click", () => {
  const seg = playback.segments[playback.index];
  if (!seg) { setPbStatus("先播放到想標記的時刻，再加書籤。", true); return; }
  const wall = segStart(seg) + $("pb-video").currentTime * 1000;
  const list = getBookmarks();
  if (!list.some((b) => Math.abs(b.ms - wall) < 1000)) {
    list.push({ ms: wall });
    setBookmarks(list);
    renderBookmarks();
    flash("已加入書籤。");
  }
});

// Keyboard shortcuts, only while the playback modal is open and focus is not in a field.
document.addEventListener("keydown", (e) => {
  if ($("playback-modal").hidden) return;
  const tag = document.activeElement && document.activeElement.tagName;
  if (tag === "INPUT" || tag === "SELECT" || tag === "TEXTAREA") return;
  const video = $("pb-video");
  switch (e.key) {
    case " ": e.preventDefault(); if (video.paused) video.play().catch(() => {}); else video.pause(); break;
    case "ArrowLeft": video.currentTime = Math.max(0, video.currentTime - 5); break;
    case "ArrowRight": video.currentTime = Math.min(video.duration || 1e9, video.currentTime + 5); break;
    case "[": $("pb-prev-seg").click(); break;
    case "]": $("pb-next-seg").click(); break;
    case "b": case "B": $("pb-bookmark").click(); break;
  }
});

function highlightBlock(index) {
  // Highlight by SEGMENT among the block elements only. The timeline also holds
  // the playhead (child 0) and bookmark markers, so indexing all children would
  // land one block early -- the off-by-one that made the active block sit one
  // slot left of the playhead.
  $("pb-timeline").querySelectorAll(".pb-block").forEach((b, i) => {
    b.classList.toggle("active", i === index);
  });
}

function setPbStatus(text, isError) {
  const node = $("pb-status");
  node.textContent = text;
  node.classList.toggle("bad", Boolean(isError));
  node.hidden = !text;
}

function timeLabel(ms) {
  const d = new Date(ms);
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}:${String(d.getSeconds()).padStart(2, "0")}`;
}

/** 含日期的時刻，用在跨日或需要講清楚是哪一天的訊息裡。 */
function stampLabel(ms) {
  const d = new Date(ms);
  return `${d.getFullYear()}/${d.getMonth() + 1}/${d.getDate()} ${timeLabel(ms)}`;
}

function releasePlaybackUrl() {
  if (playback.objectUrl) {
    URL.revokeObjectURL(playback.objectUrl);
    playback.objectUrl = null;
  }
}

// live wall-clock while playing, and auto-advance to the next segment
$("pb-video").addEventListener("timeupdate", () => {
  const seg = playback.segments[playback.index];
  if (!seg) return;
  if (scrub.active) return;        // 正在拖曳時，標頭顯示的是目標時刻
  const wall = segStart(seg) + $("pb-video").currentTime * 1000;
  $("pb-time").textContent = "播放時間：" + stampLabel(wall);
  movePlayhead(wall);
  // 讓「跳到」欄位跟著目前位置走，微調時不必重打整個時間。
  const d = new Date(wall);
  $("pb-seek-time").value =
    `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}:${String(d.getSeconds()).padStart(2, "0")}`;
});
$("pb-video").addEventListener("ended", () => {
  if (playback.index >= 0 && playback.index < playback.segments.length - 1) {
    guard(() => playSegment(playback.index + 1, 0));
  }
});

$("pb-load").addEventListener("click", () => guard(loadTimeline));
$("pb-date").addEventListener("change", () => guard(loadTimeline));

/** 「跳到」：把日期欄 + 時間欄合成一個本地時刻，再定位到該秒。 */
function seekFromInputs() {
  if (!playback.segments.length) { setPbStatus("這一天沒有錄影。", true); return; }
  const [y, m, d] = $("pb-date").value.split("-").map(Number);
  const parts = ($("pb-seek-time").value || "").split(":").map(Number);
  if (!y || !parts.length || Number.isNaN(parts[0])) {
    setPbStatus("請先填入要跳到的時間。", true);
    return;
  }
  const [hh, mm, ss] = [parts[0] || 0, parts[1] || 0, parts[2] || 0];
  return seekToTime(new Date(y, m - 1, d, hh, mm, ss).getTime());
}

$("pb-seek").addEventListener("click", () => guard(seekFromInputs));
$("pb-seek-time").addEventListener("keydown", (event) => {
  if (event.key === "Enter") { event.preventDefault(); guard(seekFromInputs); }
});

/* ── 時間軸拖曳（像監視器系統那樣拉到某個時刻）────────────────
 *
 * 按住時間軸拖動 → 播放頭跟著走、標頭即時顯示會跳到的時刻；放開才真的去載入
 * 那一段。拖曳過程中不載入是刻意的：每次定位都要向伺服器抓一個加密片段回來
 * 解密，邊拖邊載會把頻寬和伺服器都打爆，而且畫面反而更卡。
 *
 * 整條軸都可拖，包含色塊之間的空白（當時沒錄影）—— 放開會跳到之後最近的片段
 * 並說明跳了多少，而不是安靜地什麼都不做。 */

function timeAtClientX(clientX) {
  const rect = $("pb-timeline").getBoundingClientRect();
  const ratio = Math.min(1, Math.max(0, (clientX - rect.left) / (rect.width || 1)));
  return playback.span.first + ratio * playback.span.span;
}

function movePlayhead(ms) {
  const head = $("pb-playhead");
  if (!playback.span) { head.hidden = true; return; }
  const ratio = (ms - playback.span.first) / playback.span.span;
  head.style.left = `${Math.min(100, Math.max(0, ratio * 100))}%`;
  head.hidden = false;
}

function previewAt(clientX) {
  const target = timeAtClientX(clientX);
  movePlayhead(target);
  $("pb-time").textContent = "拉到：" + stampLabel(target);
  return target;
}

$("pb-timeline").addEventListener("pointerdown", (event) => {
  if (!playback.span) return;
  scrub.active = true;
  event.preventDefault();
  try { $("pb-timeline").setPointerCapture(event.pointerId); } catch { /* 舊瀏覽器 */ }
  previewAt(event.clientX);
});

$("pb-timeline").addEventListener("pointermove", (event) => {
  if (scrub.active) { previewAt(event.clientX); return; }
  // Hover preview: show the wall-clock time under the cursor (a lightweight
  // stand-in for frame thumbnails, which would need server-side extraction).
  if (!playback.span) return;
  const hint = $("pb-hint");
  const wrapRect = hint.parentElement.getBoundingClientRect();
  hint.textContent = stampLabel(timeAtClientX(event.clientX));
  hint.style.left = `${event.clientX - wrapRect.left}px`;
  hint.hidden = false;
});
$("pb-timeline").addEventListener("pointerleave", () => { $("pb-hint").hidden = true; });

function endScrub(event) {
  if (!scrub.active) return;
  scrub.active = false;
  try { $("pb-timeline").releasePointerCapture(event.pointerId); } catch { /* 已釋放 */ }
  guard(() => seekToTime(timeAtClientX(event.clientX)));
}

$("pb-timeline").addEventListener("pointerup", endScrub);
$("pb-timeline").addEventListener("pointercancel", () => { scrub.active = false; });
$("pb-close").addEventListener("click", closePlayback);
$("playback-modal").addEventListener("click", (e) => {
  if (e.target === $("playback-modal")) closePlayback();
});

function closePlayback() {
  const video = $("pb-video");
  video.pause();
  video.removeAttribute("src");
  video.load();
  releasePlaybackUrl();
  $("playback-modal").hidden = true;
}

/* ── 管理員 ───────────────────────────────────────────── */

async function loadUsers() {
  const data = await request("GET", "/api/users?pageSize=100");
  fillTable("user-table", data.items.map((u) => {
    const actions = el("span", null, "row-actions");

    // Only a regular ADMIN has a scope to set; SUPER_ADMIN sees everything.
    if (can("groups:manage") && u.role === "ADMIN" && !u.deletedAt) {
      const scope = el("button", "可見範圍", "ghost");
      scope.addEventListener("click", () => guard(() => openScopeModal(u)));
      actions.appendChild(scope);
    }
    // Granting hidden features is SUPER_ADMIN-only (never delegated).
    if (state.user.role === "SUPER_ADMIN" && u.role === "ADMIN" && !u.deletedAt) {
      const feats = el("button", "功能授權", "ghost");
      feats.addEventListener("click", () => guard(() => openFeaturesModal(u)));
      actions.appendChild(feats);
    }
    // 重設別人的密碼僅限最高管理員（伺服器強制）。自己的密碼走側欄的「修改密碼」，
    // 那條路徑需要目前密碼，不能從這裡繞過。
    if (state.user.role === "SUPER_ADMIN" && u.id !== state.user.id && !u.deletedAt) {
      const pw = el("button", "重設密碼", "ghost");
      pw.addEventListener("click", () => openPasswordModal(u, false));
      actions.appendChild(pw);
    }
    // 伺服器本來就會拒絕，這裡隱藏只是避免使用者按了才吃 403。
    if (can("users:delete") && u.id !== state.user.id) {
      const remove = el("button", "刪除", "ghost danger");
      remove.addEventListener("click", () => guard(async () => {
        if (!confirm(`確定刪除「${u.username}」？\n\n採軟刪除，稽核紀錄仍可追溯此帳號的操作。`)) return;
        await request("DELETE", `/api/users/${u.id}`);
        flash(`已刪除 ${u.username}。`);
        loadUsers();
      }));
      actions.appendChild(remove);
    }
    return row([
      u.username,
      pill(ROLE_LABEL[u.role] || u.role, u.role === "SUPER_ADMIN"),
      USER_STATUS_LABEL[u.status] || u.status,
      when(u.lastLoginAt), when(u.createdAt), actions,
    ]);
  }), "沒有管理員帳號。");
}

$("create-user-form").addEventListener("submit", (event) => {
  event.preventDefault();
  guard(async () => {
    // 角色固定 ADMIN:最高管理員只有安裝時建立的那一位,伺服器也會拒絕 SUPER_ADMIN。
    await request("POST", "/api/users", {
      username: $("nu-username").value,
      password: $("nu-password").value,
      role: "ADMIN",
    });
    $("nu-username").value = "";
    $("nu-password").value = "";
    flash("管理員帳號已建立。");
    loadUsers();
  });
});

/* ── 修改密碼 ─────────────────────────────────────────────
 *
 * 同一個 modal 兩種用途:
 *   自己的密碼 —— 任何角色都能改,必須輸入目前密碼。
 *   別人的密碼 —— 只有最高管理員能重設,不需要舊密碼。
 *
 * 兩種情況伺服器都會撤銷該帳號的所有連線階段(密碼變更等於憑證變更),
 * 所以改完自己的密碼一定會被登出 —— 這裡主動導回登入頁,比讓後續每個
 * 請求都吃 401 清楚。前端的顯示條件只是版面整理,伺服器仍逐次驗證。 */

const passwordModal = { userId: null, username: "", isSelf: true };

function openPasswordModal(user, isSelf) {
  passwordModal.userId = user.id;
  passwordModal.username = user.username;
  passwordModal.isSelf = isSelf;

  $("pw-title").textContent = isSelf ? "修改我的密碼" : `重設密碼 — ${user.username}`;
  $("pw-hint").textContent = isSelf
    ? "變更後這個帳號的所有連線階段都會失效，需要用新密碼重新登入。"
    : `直接設定「${user.username}」的新密碼，不需要對方目前的密碼。`
      + "此操作會寫入稽核紀錄，並讓該帳號所有連線階段立即失效。";
  $("pw-foot").textContent = isSelf ? "" : "請透過安全管道把新密碼交給對方。";
  $("pw-current-wrap").hidden = !isSelf;
  $("pw-current").required = isSelf;
  $("pw-username").value = user.username;

  $("pw-current").value = "";
  $("pw-new").value = "";
  $("pw-confirm").value = "";
  $("pw-error").hidden = true;
  $("password-modal").hidden = false;
  (isSelf ? $("pw-current") : $("pw-new")).focus();
}

function closePasswordModal() {
  // 不要把密碼留在 DOM 裡。
  $("pw-current").value = "";
  $("pw-new").value = "";
  $("pw-confirm").value = "";
  $("password-modal").hidden = true;
}

function passwordError(message) {
  $("pw-error").textContent = message;
  $("pw-error").hidden = false;
}

async function savePassword() {
  const next = $("pw-new").value;
  if (!next) return passwordError("請輸入新密碼。");
  if (next !== $("pw-confirm").value) return passwordError("兩次輸入的新密碼不一致。");
  if (passwordModal.isSelf && !$("pw-current").value) return passwordError("請輸入目前密碼。");

  const body = { newPassword: next };
  if (passwordModal.isSelf) body.currentPassword = $("pw-current").value;

  const save = $("pw-save");
  save.disabled = true;
  try {
    await request("POST", `/api/users/${passwordModal.userId}/password`, body);
  } catch (err) {
    // 連線階段已經結束的話 request() 已經導回登入頁了,不要再蓋一層錯誤訊息。
    if (err.message === "session_ended") { closePasswordModal(); return; }
    passwordError(err.requestId ? `${err.message}（請求編號 ${err.requestId}）` : err.message);
    return;
  } finally {
    save.disabled = false;
  }

  const wasSelf = passwordModal.isSelf;
  const who = passwordModal.username;
  closePasswordModal();
  if (wasSelf) {
    signOut("密碼已變更，請用新密碼重新登入。");
  } else {
    flash(`已重設「${who}」的密碼，該帳號的連線階段已全部失效。`);
  }
}

$("change-my-password").addEventListener("click", () => openPasswordModal(state.user, true));
$("password-form").addEventListener("submit", (event) => {
  event.preventDefault();
  guard(savePassword);
});
$("pw-save").addEventListener("click", () => guard(savePassword));
$("pw-close").addEventListener("click", closePasswordModal);
$("password-modal").addEventListener("click", (event) => {
  if (event.target === $("password-modal")) closePasswordModal();
});

/* ── 群組 + 可見範圍 ──────────────────────────────────────
 *
 * 群組是可見範圍的單位:管理員被指派到群組，就看得到群組內的端點,
 * 再加個別 INCLUDE/EXCLUDE 例外。全部僅最高管理員可管理(伺服器強制)。 */

async function loadGroups() {
  const data = await request("GET", "/api/groups?pageSize=200");
  fillTable("group-table", data.items.map((g) => {
    const actions = el("span", null, "row-actions");
    const members = el("button", "管理成員", "ghost");
    members.addEventListener("click", () => guard(() => openMembersModal(g)));
    actions.appendChild(members);
    const remove = el("button", "刪除", "ghost danger");
    remove.addEventListener("click", () => guard(async () => {
      if (!confirm(`確定刪除群組「${g.name}」？\n\n所有管理員對此群組的指派也會一併移除。`)) return;
      await request("DELETE", `/api/groups/${g.id}`);
      flash("群組已刪除。");
      loadGroups();
    }));
    actions.appendChild(remove);
    return row([g.name, g.description, g.memberCount, when(g.createdAt), actions]);
  }), "尚未建立任何群組。");
}

$("create-group-form").addEventListener("submit", (event) => {
  event.preventDefault();
  guard(async () => {
    await request("POST", "/api/groups", {
      name: $("ng-name").value,
      description: $("ng-desc").value || undefined,
    });
    $("ng-name").value = "";
    $("ng-desc").value = "";
    flash("群組已建立。");
    loadGroups();
  });
});

/* 成員編輯 modal:列出所有端點,勾選=在群組內 */

const membersModal = { groupId: null };

async function openMembersModal(group) {
  membersModal.groupId = group.id;
  $("gm-title").textContent = `群組成員 — ${group.name}`;
  const [detail, all] = await Promise.all([
    request("GET", `/api/groups/${group.id}`),
    request("GET", "/api/endpoints?pageSize=200"),
  ]);
  const inGroup = new Set((detail.members || []).map((m) => m.id));

  const list = $("gm-list");
  list.replaceChildren();
  all.items.forEach((e) => {
    // Show which organization each endpoint belongs to, so members can be
    // chosen with the org in view (not just device name / user).
    const org = e.organizationId ? `組織：${e.organizationId}` : "未設定組織";
    const subtitle = [org, e.localUser].filter(Boolean).join("　·　");
    list.appendChild(pickerRow(e.id, e.deviceName || e.id, subtitle, inGroup.has(e.id)));
  });
  updateMemberCount();
  list.querySelectorAll("input").forEach((i) => i.addEventListener("change", updateMemberCount));
  $("members-modal").hidden = false;
}

function updateMemberCount() {
  const n = $("gm-list").querySelectorAll("input:checked").length;
  $("gm-count").textContent = `已選 ${n} 個端點`;
}

$("gm-save").addEventListener("click", () => guard(async () => {
  const ids = [...$("gm-list").querySelectorAll("input:checked")].map((i) => i.value);
  await request("PUT", `/api/groups/${membersModal.groupId}/members`, { endpointIds: ids });
  $("members-modal").hidden = true;
  flash("群組成員已更新。");
  loadGroups();
}));
$("gm-close").addEventListener("click", () => { $("members-modal").hidden = true; });

/* 一列可勾選的端點(共用於成員編輯) */
function pickerRow(value, title, subtitle, checked) {
  const label = el("label", null, "picker-row");
  const box = document.createElement("input");
  box.type = "checkbox";
  box.value = value;
  box.checked = checked;
  label.appendChild(box);
  const text = el("span", null, "picker-text");
  text.appendChild(el("span", title, "picker-title"));
  if (subtitle) text.appendChild(el("span", subtitle, "picker-sub"));
  label.appendChild(text);
  return label;
}

/* ── 功能授權 modal（僅最高管理員）─────────────────────────
 *
 * 一般管理員預設看不到:螢幕牆、群組、錄影、安裝包、稽核、儲存、端點停用/刪除。
 * 這裡由最高管理員逐項授予;授予高權限項目時顯示警告。授權管理本身不可委派
 * (伺服器端只認 super_admin),被授權者無法再授權他人。 */

const featuresModal = { userId: null, sensitive: new Set() };

async function openFeaturesModal(user) {
  featuresModal.userId = user.id;
  featuresModal.sensitive = new Set();
  $("ft-title").textContent = `功能授權 — ${user.username}`;
  const data = await request("GET", `/api/users/${user.id}/features`);
  const box = $("ft-list");
  box.replaceChildren();
  data.features.forEach((f) => {
    if (f.sensitive) featuresModal.sensitive.add(f.key);
    const rowEl = pickerRow(f.key, f.label,
      f.sensitive ? "高權限：被授權者可據此擴大自身可及範圍" : "", f.granted);
    if (f.sensitive) rowEl.classList.add("sensitive");
    box.appendChild(rowEl);
  });
  updateFeatureWarning();
  $("features-modal").hidden = false;
}

function updateFeatureWarning() {
  const any = [...$("ft-list").querySelectorAll("input:checked")]
    .some((c) => featuresModal.sensitive.has(c.value));
  $("ft-warn").hidden = !any;
}

async function saveFeatures() {
  const features = [...$("ft-list").querySelectorAll("input:checked")].map((c) => c.value);
  await request("PUT", `/api/users/${featuresModal.userId}/features`, { features });
  flash("功能授權已更新。");
  $("features-modal").hidden = true;
}

$("ft-list").addEventListener("change", updateFeatureWarning);
$("ft-save").addEventListener("click", () => guard(saveFeatures));
$("ft-close").addEventListener("click", () => { $("features-modal").hidden = true; });
$("features-modal").addEventListener("click", (event) => {
  if (event.target === $("features-modal")) $("features-modal").hidden = true;
});

/* 可見範圍 modal:群組勾選 + 個別例外(每端點三選:預設/加入/排除) */

const scopeModal = { userId: null };

async function openScopeModal(user) {
  scopeModal.userId = user.id;
  $("sc-title").textContent = `可見範圍 — ${user.username}`;

  const [scope, groups, endpoints] = await Promise.all([
    request("GET", `/api/users/${user.id}/scope`),
    request("GET", "/api/groups?pageSize=200"),
    request("GET", "/api/endpoints?pageSize=200"),
  ]);
  const assigned = new Set(scope.groupIds);
  const includes = new Set(scope.includeEndpointIds);
  const excludes = new Set(scope.excludeEndpointIds);

  // groups checkboxes
  const gbox = $("sc-groups");
  gbox.replaceChildren();
  groups.items.forEach((g) => {
    gbox.appendChild(pickerRow(g.id, g.name,
      `${g.memberCount} 個端點`, assigned.has(g.id)));
  });

  // per-endpoint exception radios
  fillTable("sc-exceptions", endpoints.items.map((e) => {
    const mode = excludes.has(e.id) ? "exclude" : includes.has(e.id) ? "include" : "default";
    return row([e.deviceName || e.id, exceptionRadios(e.id, mode)]);
  }), "沒有端點。");

  $("sc-count").textContent = user.username;
  $("scope-modal").hidden = false;
}

function exceptionRadios(endpointId, mode) {
  const wrap = el("span", null, "radios");
  [["default", "預設"], ["include", "強制加入"], ["exclude", "強制排除"]].forEach(([val, text]) => {
    const label = el("label", null, "radio");
    const input = document.createElement("input");
    input.type = "radio";
    input.name = "exc-" + endpointId;
    input.value = val;
    input.checked = mode === val;
    label.appendChild(input);
    label.appendChild(el("span", text));
    wrap.appendChild(label);
  });
  return wrap;
}

$("sc-save").addEventListener("click", () => guard(async () => {
  const groupIds = [...$("sc-groups").querySelectorAll("input:checked")].map((i) => i.value);
  const includeEndpointIds = [];
  const excludeEndpointIds = [];
  $("sc-exceptions").querySelectorAll("tbody tr").forEach((tr) => {
    const checked = tr.querySelector("input:checked");
    if (!checked) return;
    const id = checked.name.slice(4); // strip "exc-"
    if (checked.value === "include") includeEndpointIds.push(id);
    else if (checked.value === "exclude") excludeEndpointIds.push(id);
  });

  const result = await request("PUT", `/api/users/${scopeModal.userId}/scope`, {
    groupIds, includeEndpointIds, excludeEndpointIds,
  });
  $("scope-modal").hidden = true;
  flash(`可見範圍已更新（共 ${result.effectiveEndpointCount} 個端點）。`);
}));
$("sc-close").addEventListener("click", () => { $("scope-modal").hidden = true; });

[$("members-modal"), $("scope-modal")].forEach((m) => {
  m.addEventListener("click", (e) => { if (e.target === m) m.hidden = true; });
});

/* ── 螢幕錄影政策 ─────────────────────────────────────────
 *
 * 設定持續錄影的對象(端點或群組)、模式、fps、保留天數。建立後若對象的
 * Agent 在線,立即開始錄影;停用或刪除則停止。全部僅最高管理員可操作。 */

const RECORDING_MODE_LABEL = { DIFFERENTIAL: "差異式", FULL: "完全" };

/* ── 儲存位置（多個 NAS 目標）────────────────────────────
 *
 * 可建立多個具名儲存目標(FTP/SMB)。錄影政策各自挑一個;截圖存到標記「預設」的
 * 目標。檔案在伺服器端加密後才送出,NAS 上只有密文。密碼封存於伺服器、不回傳
 * 前端(編輯時留空 = 沿用原密碼)。存檔前可先「測試連線」。 */

const STORAGE_BACKEND_LABEL = { FTP: "FTP／FTPS", SMB: "SMB" };

function stTargetWhere(t) {
  const parts = [t.host || ""];
  if (t.share) parts.push(t.share);
  if (t.basePath) parts.push(t.basePath);
  return parts.filter(Boolean).join(" / ");
}

function stFields() {
  const backend = $("st-backend").value;
  document.querySelectorAll(".st-smb").forEach((n) => { n.hidden = backend !== "SMB"; });
  document.querySelectorAll(".st-ftp").forEach((n) => { n.hidden = backend !== "FTP"; });
}

function stCollect() {
  const backend = $("st-backend").value;
  const body = {
    name: $("st-name").value.trim(),
    backend,
    host: $("st-host").value.trim(),
    basePath: $("st-base").value.trim(),
    username: $("st-user").value.trim(),
    isDefault: $("st-default").value === "1",
  };
  const port = $("st-port").value.trim();
  if (port) body.port = Number(port);
  const pass = $("st-pass").value;
  if (pass) body.password = pass;        // 留空 = 不變更（更新時沿用原密碼）
  if (backend === "SMB") {
    body.share = $("st-share").value.trim();
    body.domain = $("st-domain").value.trim();
  }
  if (backend === "FTP") body.useTls = $("st-tls").value === "1";
  return body;
}

function stResult(msg, isError, hide) {
  const node = $("st-result");
  if (hide) { node.hidden = true; return; }
  node.textContent = msg;
  node.classList.toggle("bad", !!isError);
  node.classList.toggle("good", !isError);
  node.hidden = false;
}

function stResetForm() {
  $("st-id").value = "";
  $("st-name").value = "";
  $("st-backend").value = "SMB";
  $("st-host").value = "";
  $("st-port").value = "";
  $("st-share").value = "";
  $("st-base").value = "";
  $("st-user").value = "";
  $("st-domain").value = "";
  $("st-tls").value = "1";
  $("st-pass").value = "";
  $("st-default").value = "0";
  $("st-form-title").textContent = "新增儲存目標";
  $("st-save").textContent = "新增";
  $("st-cancel").hidden = true;
  $("st-pass").placeholder = "NAS 密碼";
  stFields();
  stResult("", false, true);
}

function stEdit(t) {
  $("st-id").value = t.id;
  $("st-name").value = t.name || "";
  $("st-backend").value = t.backend;
  $("st-host").value = t.host || "";
  $("st-port").value = t.port || "";
  $("st-share").value = t.share || "";
  $("st-base").value = t.basePath || "";
  $("st-user").value = t.username || "";
  $("st-domain").value = t.domain || "";
  $("st-tls").value = t.useTls === false ? "0" : "1";
  $("st-pass").value = "";
  $("st-pass").placeholder = t.hasPassword ? "留空 = 不變更" : "NAS 密碼";
  $("st-default").value = t.isDefault ? "1" : "0";
  $("st-form-title").textContent = `編輯：${t.name}`;
  $("st-save").textContent = "更新";
  $("st-cancel").hidden = false;
  stFields();
  stResult("", false, true);
  $("st-form-panel").open = true;
  $("st-name").focus();
}

async function loadStorage() {
  stResetForm();
  const data = await request("GET", "/api/storage/targets");
  fillTable("storage-table", (data.items || []).map((t) => {
    const test = el("button", "測試", "ghost small");
    test.addEventListener("click", () => guard(async () => {
      test.disabled = true;
      try {
        const r = await request("POST", `/api/storage/targets/${t.id}/test`, {});
        flash(r.message || (r.ok ? "連線成功。" : "連線失敗。"), !r.ok);
      } finally { test.disabled = false; }
    }));
    const mkDefault = el("button", "設為預設", "ghost small");
    mkDefault.disabled = t.isDefault;
    mkDefault.addEventListener("click", () => guard(async () => {
      await request("PUT", `/api/storage/targets/${t.id}`, {
        name: t.name, backend: t.backend, host: t.host, port: t.port,
        share: t.share, basePath: t.basePath, username: t.username,
        domain: t.domain, useTls: t.useTls, isDefault: true,
      });
      flash(`已將「${t.name}」設為預設。`);
      loadStorage();
    }));
    const edit = el("button", "編輯", "ghost small");
    edit.addEventListener("click", () => stEdit(t));
    const remove = el("button", "刪除", "ghost danger small");
    remove.addEventListener("click", () => guard(async () => {
      if (!confirm(`確定刪除儲存目標「${t.name}」？`)) return;
      await request("DELETE", `/api/storage/targets/${t.id}`);
      flash("儲存目標已刪除。");
      loadStorage();
    }));
    const actions = el("span", null, "row-actions");
    actions.append(test, mkDefault, edit, remove);
    return row([
      t.name,
      STORAGE_BACKEND_LABEL[t.backend] || t.backend,
      stTargetWhere(t),
      t.isDefault ? pill("預設", true) : "－",
      actions,
    ]);
  }), "尚未建立任何儲存目標，錄影與截圖將存於本機磁碟。");
}

$("st-backend").addEventListener("change", () => { stFields(); stResult("", false, true); });
$("st-cancel").addEventListener("click", stResetForm);

$("st-test").addEventListener("click", () => guard(async () => {
  const btn = $("st-test");
  btn.disabled = true;
  stResult("測試中…", false);
  try {
    const id = $("st-id").value;
    // Editing with a blank password -> test the stored target (uses saved password).
    const r = (id && !$("st-pass").value)
      ? await request("POST", `/api/storage/targets/${id}/test`, {})
      : await request("POST", "/api/storage/targets/test", stCollect());
    stResult(r.message || (r.ok ? "連線成功。" : "連線失敗。"), !r.ok);
  } catch (err) {
    stResult(err.message || "測試失敗。", true);
  } finally {
    btn.disabled = false;
  }
}));

$("storage-form").addEventListener("submit", (event) => {
  event.preventDefault();
  guard(async () => {
    const id = $("st-id").value;
    if (id) {
      await request("PUT", `/api/storage/targets/${id}`, stCollect());
      flash("儲存目標已更新。");
    } else {
      await request("POST", "/api/storage/targets", stCollect());
      flash("已新增儲存目標。");
    }
    loadStorage();
  });
});

async function loadRecordings() {
  const status = await request("GET", "/api/recordings/status");
  const warning = $("rec-warning");
  if (!status.enabled) {
    warning.textContent =
      "錄影未啟用：伺服器需設定 EEM_RECORDING_KEY（加密金鑰）且 FFmpeg 存在，才能錄影。目前可設定政策但不會實際錄製。";
    warning.hidden = false;
  } else {
    warning.hidden = true;
  }

  // populate the target selector for the currently-chosen target type
  await refreshRecordingTargets();
  await refreshRecordingStorageOptions();

  const data = await request("GET", "/api/recordings/policies");
  fillTable("recording-table", (data.items || []).map((p) => {
    const toggle = el("button", p.enabled ? "停用" : "啟用", "ghost");
    toggle.addEventListener("click", () => guard(async () => {
      await request("PATCH", `/api/recordings/policies/${p.id}`, { enabled: !p.enabled });
      flash(p.enabled ? "已停用（停止錄影）。" : "已啟用（開始錄影）。");
      loadRecordings();
    }));
    const remove = el("button", "刪除", "ghost danger");
    remove.addEventListener("click", () => guard(async () => {
      if (!confirm(`確定刪除「${p.targetName || p.targetId}」的錄影政策？\n\n會停止錄影，已錄製的影片仍保留到期限。`)) return;
      await request("DELETE", `/api/recordings/policies/${p.id}`);
      flash("錄影政策已刪除。");
      loadRecordings();
    }));
    const actions = el("span", null, "row-actions");
    actions.appendChild(toggle);
    actions.appendChild(remove);

    return row([
      p.targetName || p.targetId,
      p.targetType === "GROUP" ? "群組" : "端點",
      RECORDING_MODE_LABEL[p.mode] || p.mode,
      p.fps,
      `${p.retentionDays} 天`,
      p.storageTargetName || "本機磁碟",
      badge(p.enabled ? "VALID" : "EXPIRED"),
      actions,
    ]);
  }), "尚未設定任何錄影政策。");
}

async function refreshRecordingTargets() {
  const type = $("rp-target-type").value;
  const select = $("rp-target");
  select.replaceChildren();
  if (type === "GROUP") {
    const groups = await request("GET", "/api/groups?pageSize=200");
    groups.items.forEach((g) => {
      const opt = document.createElement("option");
      opt.value = g.id;
      opt.textContent = `${g.name}（${g.memberCount} 端點）`;
      select.appendChild(opt);
    });
  } else {
    const eps = await request("GET", "/api/endpoints?pageSize=200");
    eps.items.forEach((e) => {
      const opt = document.createElement("option");
      opt.value = e.id;
      opt.textContent = e.deviceName || e.id;
      select.appendChild(opt);
    });
  }
}

async function refreshRecordingStorageOptions() {
  const select = $("rp-storage");
  const chosen = select.value;
  select.replaceChildren();
  const local = document.createElement("option");
  local.value = ""; local.textContent = "本機磁碟";
  select.appendChild(local);
  const data = await request("GET", "/api/storage/targets");
  (data.items || []).forEach((t) => {
    const opt = document.createElement("option");
    opt.value = t.id;
    opt.textContent = t.name + (t.isDefault ? "（預設）" : "");
    select.appendChild(opt);
  });
  select.value = chosen;   // keep the choice if it still exists
}

$("rp-target-type").addEventListener("change", () => guard(refreshRecordingTargets));

$("create-recording-form").addEventListener("submit", (event) => {
  event.preventDefault();
  guard(async () => {
    const targetId = $("rp-target").value;
    if (!targetId) { flash("請先選擇對象。", true); return; }
    await request("POST", "/api/recordings/policies", {
      targetType: $("rp-target-type").value,
      targetId,
      mode: $("rp-mode").value,
      fps: Number($("rp-fps").value),
      retentionDays: Number($("rp-retention").value),
      storageTargetId: $("rp-storage").value || null,
    });
    flash("錄影政策已建立，對象在線即開始錄影。");
    loadRecordings();
  });
});

/* ── 安裝包（MSI）產生器 ──────────────────────────────── */

/* ── 重建 Agent 程式 ────────────────────────────────────────
 *
 * 產生安裝包只是把「事先建好的 EndpointAgent.exe」重新包一次(約 6 秒),
 * 不會編譯 C#。所以改了 Agent 原始碼卻沒重建,產出的安裝包會裝到舊程式,
 * 而且完全沒有徵兆 —— 這裡負責讓那件事看得見,並且可以直接在網頁上修好。 */

async function loadAgentBuild() {
  const note = $("agent-build-note");
  const button = $("agent-rebuild");

  let data;
  try {
    data = await request("GET", "/api/packages/agent-build");
  } catch {
    note.hidden = true; button.hidden = true; return;
  }

  const build = data.build || {};
  const running = build.status === "RUNNING";

  // 重建的是會裝到每一台端點的執行檔,比產生單一安裝包更敏感 —— 僅最高管理員。
  button.hidden = !(state.user.role === "SUPER_ADMIN" && data.canRebuild);
  button.disabled = running;
  button.textContent = running ? "重建中…" : "重建 Agent 程式";

  const show = (text, bad) => {
    note.textContent = text;
    note.classList.toggle("bad", Boolean(bad));
    note.hidden = false;
  };

  if (running) {
    show("正在重建 Agent 程式，需要一到兩分鐘。完成前產生的安裝包仍是舊版。");
    clearTimeout(loadAgentBuild.timer);
    // 只在這一頁還開著時繼續輪詢。
    if (!$("view-tokens").hidden) {
      loadAgentBuild.timer = setTimeout(() => guard(loadAgentBuild), 5000);
    }
    return;
  }
  if (build.status === "FAILED") {
    show(`Agent 重建失敗：${build.message || ""}`, true);
    return;
  }
  if (!data.sourceAvailable) {
    // 只複製產物、沒帶原始碼的部署方式 —— 沒有東西可講。
    note.hidden = true;
    return;
  }
  if (data.stale) {
    show("Agent 原始碼比已建置的執行檔新：現在產生的安裝包會裝到舊版 Agent。"
       + "請先按「重建 Agent 程式」。", true);
    return;
  }
  if (build.status === "SUCCEEDED") { show(build.message); return; }
  note.hidden = true;
}

$("agent-rebuild").addEventListener("click", () => guard(async () => {
  await request("POST", "/api/packages/agent-build");
  flash("已開始重建 Agent 程式。");
  await loadAgentBuild();
}));

async function loadPackages() {
  // 先確認伺服器具備建置工具，否則按下去只會拿到一個看不懂的錯誤。
  const tools = await request("GET", "/api/packages/toolchain");
  const warning = $("toolchain-warning");
  if (!tools.ready) {
    warning.textContent = "此伺服器目前無法建置安裝包：" + tools.problems.join("；");
    warning.classList.add("bad");
    warning.hidden = false;
  } else if (!tools.signingEnabled) {
    // Builds work, but the MSI will be unsigned -- worth flagging, not blocking.
    warning.textContent =
      "注意：目前未設定程式碼簽章，產生的 MSI 未簽章，於企業環境可能被 SmartScreen／AppLocker 阻擋。";
    warning.classList.remove("bad");
    warning.hidden = false;
  } else {
    warning.hidden = true;
  }
  $("pk-submit").disabled = !tools.ready;

  // 沒帶憑證的安裝包，端點裝完會連不回來（Agent 不繞過 TLS 驗證），而且
  // 症狀是「一直離線」而非明確錯誤 —— 所以在按下產生之前就講清楚。
  const certNote = $("pk-cert-note");
  if (tools.certificateEmbedded) {
    certNote.textContent =
      "安裝包會夾帶此伺服器的 TLS 憑證，安裝時自動寫入端點的「受信任的根憑證授權」"
      + "（解除安裝時移除）。端點不需要另外設定就能連回來。";
    certNote.classList.remove("bad");
  } else {
    certNote.textContent =
      "注意：伺服器未設定要夾帶的憑證（EEM_PACKAGE_CA_CERT／EEM_TLS_CERT），"
      + "安裝包不會帶憑證。若伺服器用的是自簽憑證，端點裝完會因為不信任憑證而一直顯示離線 —— "
      + "需要另外把憑證匯入端點，見 docs/deployment-windows.md §7。";
    certNote.classList.add("bad");
  }
  certNote.hidden = false;

  if (!$("pk-server").value) $("pk-server").value = window.location.origin;

  await loadAgentBuild();

  const data = await request("GET", "/api/packages?pageSize=100");
  fillTable("package-table", data.items.map((p) => {
    const actions = el("span", null, "row-actions");

    if (p.status === "READY" && can("packages:download")) {
      const download = el("button", "下載", "ghost");
      download.addEventListener("click", () => guard(() => downloadPackage(p)));
      actions.appendChild(download);
    }
    if (can("packages:create") && p.status !== "DELETED") {
      const remove = el("button", "刪除", "ghost danger");
      remove.addEventListener("click", () => guard(async () => {
        if (!confirm(
          `確定刪除「${p.label}」？\n\n` +
          `檔案會從伺服器移除，已經下載出去的副本也將無法再用於安裝。`
        )) return;
        await request("DELETE", `/api/packages/${p.id}`);
        flash("安裝包已刪除。");
        loadPackages();
      }));
      actions.appendChild(remove);
    }

    const stateCode = p.status === "READY" ? "VALID"
      : p.status === "FAILED" ? "REVOKED" : "EXPIRED";

    return row([
      p.label,
      p.organizationId,
      p.sizeBytes ? `${(p.sizeBytes / 1048576).toFixed(1)} MB` : "－",
      p.hasAdminPassword ? "已設定" : "未設定",
      p.signed ? "已簽章" : "未簽章",
      p.downloadCount,
      when(p.createdAt),
      badge(stateCode),
      actions,
    ]);
  }), "尚未產生任何安裝包。");
}

/**
 * 下載需要帶 Authorization 標頭，所以不能直接把網址丟給瀏覽器。
 * 改成用 fetch 取回再產生 blob 連結觸發下載。
 */
async function downloadPackage(pkg) {
  const response = await fetch(`/api/packages/${pkg.id}/download`, {
    headers: { Authorization: "Bearer " + state.accessToken },
  });
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}));
    throw new Error(detail.message || "下載失敗。");
  }

  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = pkg.filename || "endpoint-agent.msi";
  document.body.appendChild(link);
  link.click();
  link.remove();
  // 釋放 blob，否則整個 MSI 會一直留在記憶體裡。
  setTimeout(() => URL.revokeObjectURL(url), 10_000);
}

/* 有效期間輸入：勾選「永不過期」時停用年月日欄位，並即時預覽到期日。 */

function periodInputs() {
  return ["pk-years", "pk-months", "pk-days"].map($);
}

function updatePeriodPreview() {
  const never = $("pk-never").checked;
  periodInputs().forEach((input) => { input.disabled = never; });

  const preview = $("pk-preview");
  if (never) {
    preview.textContent = "此安裝包不會自動失效，只能手動撤銷。";
    preview.classList.add("warn");
    return;
  }
  preview.classList.remove("warn");

  const [y, m, d] = periodInputs().map((i) => Number(i.value) || 0);
  if (y === 0 && m === 0 && d === 0) {
    preview.textContent = "至少要設定 1 天。";
    preview.classList.add("warn");
    return;
  }
  // 用與伺服器相同的日曆規則預估（月份進位、月底夾到當月最後一天）。
  const now = new Date();
  const target = new Date(now);
  target.setMonth(target.getMonth() + m + y * 12);
  if (target.getDate() !== now.getDate()) target.setDate(0);
  target.setDate(target.getDate() + d);
  preview.textContent = "到期日約為 " + target.toLocaleDateString("zh-TW");
}

$("pk-never").addEventListener("change", updatePeriodPreview);
periodInputs().forEach((input) => input.addEventListener("input", updatePeriodPreview));
$("pk-unlimited").addEventListener("change", () => {
  $("pk-uses").disabled = $("pk-unlimited").checked;
});
updatePeriodPreview();

$("create-package-form").addEventListener("submit", (event) => {
  event.preventDefault();
  guard(async () => {
    const never = $("pk-never").checked;
    const password = $("pk-password").value;

    if (never && !confirm(
      "確定建立永不過期的安裝包？\n\n" +
      "此安裝包不會自動失效。若檔案外流，在你手動撤銷之前，" +
      "任何人都能用它把電腦註冊進系統。"
    )) return;

    // 沒設移除密碼 = 裝上去的 Agent 可被任何人從「應用程式與功能」直接移除，
    // 不會跳密碼。這常是「解除安裝沒要求密碼」的真正原因，所以明講一次。
    if (!password && !confirm(
      "這個安裝包沒有設定移除密碼。\n\n" +
      "裝上去的 Agent 任何人都能從「應用程式與功能」直接移除，不會要求輸入密碼。\n\n" +
      "要繼續產生「無移除保護」的安裝包嗎？"
    )) return;

    const payload = {
      label: $("pk-label").value,
      organizationId: $("pk-org").value || undefined,
      serverUrl: $("pk-server").value,
      maxUses: $("pk-unlimited").checked ? 0 : Number($("pk-uses").value),
    };
    if (password) payload.adminPassword = password;
    if (never) {
      payload.neverExpires = true;
    } else {
      payload.years = Number($("pk-years").value) || 0;
      payload.months = Number($("pk-months").value) || 0;
      payload.days = Number($("pk-days").value) || 0;
    }

    const submit = $("pk-submit");
    const status = $("pk-status");
    submit.disabled = true;
    submit.textContent = "建置中…";
    // 建置需要幾秒鐘，說明一下免得以為當掉了。
    status.textContent = "正在編譯 MSI，約需 5～10 秒…";

    try {
      const pkg = await request("POST", "/api/packages", payload);
      status.textContent = "";
      $("pk-password").value = "";
      $("pk-label").value = "";
      flash(`安裝包已產生（${(pkg.sizeBytes / 1048576).toFixed(1)} MB），開始下載。`);
      await downloadPackage(pkg);
      loadPackages();
    } finally {
      submit.disabled = false;
      submit.textContent = "產生並下載 MSI";
      status.textContent = "";
    }
  });
});

/* ── 稽核紀錄 ─────────────────────────────────────────── */

$("au-refresh").addEventListener("click", () => { state.auditPage = 1; guard(loadAudit); });
/* ── 告警中心 ─────────────────────────────────────────── */

const ALERT_SEV = {
  info:     { text: "資訊", tone: "neutral" },
  warning:  { text: "警告", tone: "warning" },
  critical: { text: "嚴重", tone: "critical" },
};
const ALERT_TYPE = {
  OFFLINE: "端點離線",
  LOW_DISK: "磁碟不足",
  CREDENTIAL_EXPIRING: "憑證即將到期",
  UNINSTALL_ATTEMPT: "移除嘗試",
};
const ALERT_STATUS = { OPEN: "未處理", ACKNOWLEDGED: "已確認", RESOLVED: "已解除" };

async function loadAlerts() {
  // Refresh offline detection before listing, so the list is current on open.
  try { await request("POST", "/api/alerts/evaluate", {}); } catch (e) { /* best effort */ }

  const status = $("al-status").value;
  const data = await request("GET", "/api/alerts" + (status ? "?status=" + encodeURIComponent(status) : ""));

  fillTable("alerts-table", data.items.map((a) => {
    const sev = ALERT_SEV[a.severity] || { text: a.severity, tone: "neutral" };
    const sevBadge = el("span", null, "badge b-" + sev.tone);
    sevBadge.appendChild(el("span", sev.text));

    const actions = el("span", null, "row-actions");
    if (a.status === "OPEN") {
      const ack = el("button", "確認", "ghost");
      ack.addEventListener("click", () => guard(async () => {
        await request("POST", `/api/alerts/${a.id}/acknowledge`, {});
        loadAlerts();
      }));
      actions.appendChild(ack);
    }
    return row([when(a.createdAt), sevBadge, ALERT_TYPE[a.type] || a.type,
                a.title, ALERT_STATUS[a.status] || a.status, actions]);
  }), "目前沒有告警。");

  const panel = $("al-channels-panel");
  if (can("alerts:manage")) {
    panel.hidden = false;
    await loadChannels();
  } else {
    panel.hidden = true;
  }
}

async function loadChannels() {
  const data = await request("GET", "/api/alert-channels");
  fillTable("channels-table", data.items.map((c) => {
    const toggle = el("button", c.enabled ? "停用" : "啟用", "ghost");
    toggle.addEventListener("click", () => guard(async () => {
      await request("PATCH", `/api/alert-channels/${c.id}`, { enabled: !c.enabled });
      loadChannels();
    }));
    const test = el("button", "測試", "ghost");
    test.addEventListener("click", () => guard(async () => {
      const r = await request("POST", `/api/alert-channels/${c.id}/test`, {});
      flash(r.ok ? "測試通知已送出。" : ("測試失敗：" + (r.error || "未知錯誤")), !r.ok);
    }));
    const del = el("button", "刪除", "ghost danger");
    del.addEventListener("click", () => guard(async () => {
      if (!confirm(`刪除通道「${c.name}」？`)) return;
      await request("DELETE", `/api/alert-channels/${c.id}`);
      loadChannels();
    }));
    const acts = el("span", null, "row-actions");
    acts.appendChild(toggle); acts.appendChild(test); acts.appendChild(del);
    return row([c.name, c.type, c.target, c.minSeverity, c.enabled ? "是" : "否", acts]);
  }), "尚未設定任何通道。告警仍會彙整在上方，只是不會外送。");
}

$("al-refresh").addEventListener("click", () => guard(loadAlerts));
$("al-status").addEventListener("change", () => guard(loadAlerts));
$("al-channel-form").addEventListener("submit", (event) => {
  event.preventDefault();
  guard(async () => {
    await request("POST", "/api/alert-channels", {
      name: $("ch-name").value,
      type: $("ch-type").value,
      target: $("ch-target").value,
      minSeverity: $("ch-sev").value,
    });
    $("ch-name").value = ""; $("ch-target").value = "";
    flash("通道已新增。");
    loadChannels();
  });
});

$("au-prev").addEventListener("click", () => {
  if (state.auditPage > 1) { state.auditPage--; guard(loadAudit); }
});
$("au-next").addEventListener("click", () => { state.auditPage++; guard(loadAudit); });

async function loadAudit() {
  const params = new URLSearchParams({ page: String(state.auditPage), pageSize: "50" });
  if ($("au-action").value.trim()) params.set("action", $("au-action").value.trim().toUpperCase());
  if ($("au-result").value) params.set("result", $("au-result").value);

  const data = await request("GET", "/api/audit-logs?" + params);
  fillTable("audit-table", data.items.map((a) => row([
    when(a.timestamp),
    a.actorUsername || (a.actorType === "AGENT" ? "Agent" : "系統"),
    a.action,
    a.targetType ? `${a.targetType}:${(a.targetId || "").slice(0, 8)}` : "－",
    a.sourceIp,
    badge(a.result),
  ])), "沒有符合條件的稽核紀錄。");

  const from = data.total === 0 ? 0 : (data.page - 1) * data.pageSize + 1;
  const to = from + data.items.length - (data.items.length ? 1 : 0);
  $("au-page").textContent = `第 ${from}–${to} 筆，共 ${data.total} 筆`;
  $("au-prev").disabled = data.page <= 1;
  $("au-next").disabled = !data.hasMore;
}

/* ── 啟動 ─────────────────────────────────────────────────
 * 頁面載入時嘗試以儲存的 refresh token 復原登入,重整不再被登出。
 *
 * 若有存著的 token,先顯示「載入中」而非登入畫面 —— 否則復原的網路往返
 * 空檔會讓登入畫面閃現一下再跳回主控台。沒有 token 就直接顯示登入。 */
if (loadRefreshToken()) {
  $("login-view").hidden = true;
  $("booting").hidden = false;
}
restoreSession();
