/*
 * pr-panel.js — 「一键创建 PR」面板（前端此前完全缺失的最后一公里）
 *
 * 背景：README/首页承诺「一键创建 PR」，后端两个接口早已就绪
 *   GET  /session/{id}/proposal   → 提案（无提案 404；含 write_mode 便于提前置灰）
 *   POST /session/{id}/apply-fix  → 真正开分支 + 提交 + 开 PR（写模式关闭 403）
 * 但前端从未实现，用户只能下载 .patch 手动到 GitHub 建分支。
 *
 * 本模块不改 app.js：用 MutationObserver 观察 #report，在报告渲染完成后
 * 以幂等方式追加一块 .pr-panel 控件。报告每次 renderReport() 都会整体重建
 * #report 的 innerHTML，因此「观察 + 重新注入」是唯一不侵入 app.js 的可靠接法。
 *
 * 安全姿态：
 *  - 提交前必须让用户看到「到底要写什么」（逐文件 path + 行数 + 内容预览）
 *  - 确认按钮为危险色、默认焦点在「取消」，Escape 可退出确认视图
 *  - 所有插值走 IA.escapeHtml / IA.escapeAttr；所有网络请求带 IA.authHeaders()
 *  - 任何失败都在面板内提示（读后端 detail），不产生未捕获的 promise rejection
 */
(function () {
  "use strict";

  const IA = window.IssueAgent;
  if (!IA) return;

  const t = IA.translate;
  const esc = IA.escapeHtml;

  const REPORT_ID = "report";
  const PATCH_ID = "report-patch";
  const PANEL_CLASS = "pr-panel";
  const MARKER_ATTR = "data-pr-panel";
  const REQUEST_TIMEOUT_MS = 15000;

  let panelEl = null;          // 当前注入的 .pr-panel（随 #report 重建而失效）
  let observedReport = null;   // 当前被观察的 #report 节点
  let sessionId = null;        // 面板状态归属的会话（切换即重置）
  let mode = "idle";           // idle | checking | no-proposal | write-disabled | confirm | creating | created | error
  let proposal = null;         // GET /proposal 的响应
  let noticeKey = "";          // 提示型状态的 i18n key
  let errorTag = "";           // 失败状态：HTTP 状态码 / 错误名（已转义前原文）
  let errorMessage = "";       // 失败状态：后端 detail 或本地错误文案
  let extraWriteDisabled = false; // 失败状态是否额外附加「写模式关闭」说明
  let createdPr = null;        // { url, label }
  let touchOnly = false;       // 状态是否只改了文案（无需重建 DOM）
  let forcePaint = false;      // 会话切换后强制重绘（mode 恰好相同的场景）

  let confirmEl = null;        // 确认视图根节点
  let confirmRefs = null;      // { action, cancel }
  let lastFocus = null;        // 打开确认视图前的焦点元素（关闭后归还）
  let inFlight = false;        // 防重复点击的硬开关（独立于 mode，避免状态竞态）
  let rafId = 0;               // MutationObserver 去抖句柄（高频触发合并到一帧）

  /* ── 小工具 ─────────────────────────────────────────────── */

  function hasOwn(obj, key) {
    return !!obj && Object.prototype.hasOwnProperty.call(obj, key);
  }

  function captureElement(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function captureMessage(className, text) {
    const node = captureElement("p", className);
    if (text) node.textContent = String(text);
    return node;
  }

  // 会话归属：报告属于哪个 session。优先用 app.js 暴露的 IA.sessionId
  // （切换会话时它立即更新），再退回 IA.getActiveSession().session_id。
  function currentSessionId() {
    if (typeof IA.sessionId === "string" && IA.sessionId) return IA.sessionId;
    if (typeof IA.getActiveSession === "function") {
      const info = IA.getActiveSession();
      if (info && info.session_id) return String(info.session_id);
    }
    return "";
  }

  // owner/repo：从会话详情取；取不到再退回 issue_url 解析；都没有就留空
  // （宁可不显示，也不瞎编一个仓库名）。
  function repoLabel() {
    let info = null;
    if (typeof IA.getActiveSession === "function") info = IA.getActiveSession();
    const owner = info && info.owner ? String(info.owner) : "";
    const repo = info && info.repo ? String(info.repo) : "";
    if (owner && repo) return owner + "/" + repo;
    if (owner || repo) return owner || repo;
    const url = info && info.issue_url ? String(info.issue_url) : "";
    const match = url.match(/^https?:\/\/(?:www\.)?github\.com\/([^/\s?#]+)\/([^/\s?#]+)/i);
    if (match) return match[1] + "/" + match[2];
    return "";
  }

  // 行数文案：i18n 表里没有「N 行」这个键（且不允许新增），这里按文档语言
  // 就地给出中/英两种写法，其余语言回退英文。
  function linesLabel(count) {
    const lang = (document.documentElement.lang || "").toLowerCase();
    const value = String(count);
    return lang.indexOf("zh") === 0 ? value + " 行" : value + " lines";
  }

  // 非 2xx 统一解析：后端 FastAPI 用 {detail: ...}，detail 可能是字符串或数组
  async function readProblem(response) {
    let detail = "";
    try {
      const payload = await response.json();
      if (payload && hasOwn(payload, "detail")) {
        detail = typeof IA.formatErrorDetail === "function"
          ? IA.formatErrorDetail(payload.detail)
          : String(payload.detail);
      }
    } catch (error) {
      detail = "";
    }
    const status = response && response.status ? String(response.status) : "";
    return {
      status: status,
      detail: detail || (status ? "HTTP " + status : ""),
    };
  }

  async function fetchJson(url, options) {
    const opts = options || {};
    opts.headers = IA.authHeaders(opts.headers || {});
    // 超时兜底：既避免按钮永远卡在「检查中」，也保证 promise 一定会 settle
    let timer = 0;
    if (typeof AbortController === "function") {
      const controller = new AbortController();
      opts.signal = controller.signal;
      timer = window.setTimeout(function () { controller.abort(); }, REQUEST_TIMEOUT_MS);
    }
    try {
      const response = await fetch(url, opts);
      if (!response.ok) {
        const problem = await readProblem(response);
        const error = new Error(problem.detail);
        error.status = problem.status;
        error.detail = problem.detail;
        throw error;
      }
      if (response.status === 204) return null;
      try {
        return await response.json();
      } catch (error) {
        return null;
      }
    } catch (error) {
      if (error && error.name === "AbortError") {
        const timeout = new Error("Request timed out");
        timeout.detail = "Request timed out";
        timeout.status = "";
        throw timeout;
      }
      throw error;
    } finally {
      if (timer) window.clearTimeout(timer);
    }
  }

  /* ── 注入点 ─────────────────────────────────────────────── */

  function reportRoot() {
    return document.getElementById(REPORT_ID);
  }

  // 是否已经渲染出真实报告（骨架屏 / 空容器都不算）
  function reportReady(root) {
    if (!root || !root.firstElementChild) return false;
    if (root.querySelector(".report-skeleton")) return false;
    return !!(root.querySelector(".report-section") || root.querySelector("#" + PATCH_ID) ||
      root.querySelector(".report-conclusion") || root.querySelector(".report-toc"));
  }

  function hasPanel(root, id) {
    const panels = root.querySelectorAll("[" + MARKER_ATTR + "]");
    for (let i = 0; i < panels.length; i++) {
      if (panels[i].getAttribute(MARKER_ATTR) === id) return true;
    }
    return false;
  }

  function dropPanel(root) {
    if (!root) return;
    const panels = root.querySelectorAll("[" + MARKER_ATTR + "]");
    for (let i = 0; i < panels.length; i++) panels[i].remove();
  }

  function resetState() {
    mode = "idle";
    proposal = null;
    noticeKey = "";
    errorTag = "";
    errorMessage = "";
    extraWriteDisabled = false;
    createdPr = null;
    touchOnly = false;
    inFlight = false;
    confirmEl = null;
    confirmRefs = null;
    lastFocus = null;
  }

  function reconcile() {
    const root = reportRoot();
    if (!root) return;

    // 报告容器被整体替换（re-render）时，旧面板已成为游离节点，主动丢弃
    if (panelEl && panelEl.parentNode !== root) {
      panelEl = null;
      confirmEl = null;
      confirmRefs = null;
    }

    const id = currentSessionId();
    if (id !== sessionId) {
      // 会话切换：清掉上一会话的提案 / 确认视图 / 焦点，避免「用 A 会话的提案
      // 去写 B 会话的仓库」这类跨会话误操作
      sessionId = id;
      dropPanel(root);
      panelEl = null;
      resetState();
      forcePaint = true;
    }

    if (!id) return;
    if (!reportReady(root)) return;

    if (!panelEl || panelEl.parentNode !== root) {
      injectPanel(root);
      return;
    }
    if (touchOnly || forcePaint) {
      touchOnly = false;
      forcePaint = false;
      paintPanel();
    }
  }

  function scheduleReconcile() {
    if (rafId) return;
    rafId = window.requestAnimationFrame(function () {
      rafId = 0;
      reconcile();
    });
  }

  function ensureObserved() {
    const root = reportRoot();
    if (!root) return false;
    if (root !== observedReport) {
      // #report 是 app.js 里长期存在的容器，正常只 observe 一次；
      // 这里仍处理节点被替换的情况，避免观察一个已脱离文档的旧节点
      if (observedReport) observer.disconnect();
      observer.observe(root, { childList: true, subtree: true });
      observedReport = root;
      scheduleReconcile();
    }
    return true;
  }

  function injectPanel(root) {
    const patch = root.querySelector("#" + PATCH_ID);
    const sections = root.querySelectorAll(".report-section");
    const holder = patch || (sections.length ? sections[sections.length - 1] : null);

    const panel = document.createElement("div");
    panel.className = PANEL_CLASS;
    panel.setAttribute(MARKER_ATTR, "1");
    panel.setAttribute("role", "group");
    panel.setAttribute("aria-label", t("pr_section_title"));
    panel.appendChild(captureElement("div", "pr-title", t("pr_section_title")));

    const body = document.createElement("div");
    body.className = "pr-body";
    panel.appendChild(body);

    const host = holder && holder.parentNode ? holder.parentNode : root;
    if (holder && holder.nextSibling) host.insertBefore(panel, holder.nextSibling);
    else host.appendChild(panel);

    panelEl = panel;
    paintPanel();
  }

  /* ── 渲染 ───────────────────────────────────────────────── */

  function buildNotice(text, variant) {
    const node = captureElement("p", "pr-notice pr-notice-" + variant);
    node.setAttribute("role", variant === "error" ? "alert" : "status");
    node.textContent = String(text || "");
    return node;
  }

  function buildErrorNotice() {
    const wrap = document.createElement("div");
    wrap.className = "pr-notice pr-notice-error";
    wrap.setAttribute("role", "alert");
    if (errorMessage) wrap.appendChild(captureElement("p", "pr-notice-text", errorMessage));
    if (errorTag) {
      const meta = captureElement("p", "pr-notice-meta");
      meta.appendChild(captureElement("span", "pr-error-tag", errorTag));
      wrap.appendChild(meta);
    }
    if (extraWriteDisabled) wrap.appendChild(buildNotice(t("pr_write_disabled"), "hint"));
    return wrap;
  }

  function creatingSpinner() {
    const spinner = captureElement("span", "pr-spinner");
    spinner.setAttribute("aria-hidden", "true");
    return spinner;
  }

  function paintPanel() {
    if (!panelEl) return;
    const body = panelEl.querySelector(".pr-body");
    if (!body) return;
    body.replaceChildren();

    switch (mode) {
      case "checking": {
        const btn = captureElement("button", "pr-create pr-create-busy", t("pr_button_checking"));
        btn.type = "button";
        btn.disabled = true;
        btn.setAttribute("aria-busy", "true");
        btn.appendChild(creatingSpinner());
        body.appendChild(btn);
        break;
      }
      case "no-proposal":
        body.appendChild(buildNotice(t("pr_no_proposal"), "muted"));
        break;
      case "write-disabled": {
        const btn = captureElement("button", "pr-create", t("pr_button_create"));
        btn.type = "button";
        btn.disabled = true;
        body.appendChild(btn);
        body.appendChild(buildNotice(t("pr_write_disabled"), "warn"));
        break;
      }
      case "confirm":
      case "creating":
        paintConfirm();
        break;
      case "created": {
        const wrap = document.createElement("div");
        wrap.className = "pr-success";
        wrap.setAttribute("role", "status");
        wrap.appendChild(captureElement("p", "pr-success-text", t("pr_created", { number: createdPr.number })));
        if (createdPr.url) {
          const link = captureElement("a", "pr-open", t("pr_open"));
          link.href = createdPr.url;
          link.target = "_blank";
          // noopener: 防止新页面通过 window.opener 反向操作本页
          link.rel = "noopener noreferrer";
          wrap.appendChild(link);
        }
        body.appendChild(wrap);
        break;
      }
      case "error":
        body.appendChild(buildErrorNotice());
        break;
      default: {
        const btn = captureElement("button", "pr-create", t("pr_button_create"));
        btn.type = "button";
        btn.addEventListener("click", requestProposal);
        body.appendChild(btn);
        break;
      }
    }
  }

  function paintConfirm() {
    if (!panelEl) return;
    const body = panelEl.querySelector(".pr-body");
    if (!body) return;
    body.replaceChildren();
    confirmEl = null;
    confirmRefs = null;

    const changes = (proposal && proposal.changes) || [];
    const box = document.createElement("div");
    box.className = "pr-confirm";
    box.setAttribute("role", "group");
    box.setAttribute("aria-label", t("pr_confirm_title"));

    box.appendChild(captureElement("h5", "pr-confirm-title", t("pr_confirm_title")));
    box.appendChild(captureMessage("p", "pr-confirm-text", t("pr_confirm_body", {
      repo: repoLabel(),
      branch: proposal && proposal.branch ? String(proposal.branch) : "",
      count: String(changes.length),
    })));

    if (changes.length) {
      const listWrap = document.createElement("div");
      listWrap.className = "pr-changes";
      listWrap.appendChild(captureElement("div", "pr-changes-title", t("pr_confirm_changes")));
      const list = document.createElement("ul");
      list.className = "pr-change-list";
      changes.forEach(function (change) {
        const item = change || {};
        const li = document.createElement("li");
        li.className = "pr-change";
        const head = document.createElement("div");
        head.className = "pr-change-head";
        head.appendChild(captureElement("code", "pr-change-path", item.path ? String(item.path) : ""));
        if (typeof item.proposed_lines === "number") {
          head.appendChild(captureElement("span", "pr-change-lines", linesLabel(item.proposed_lines)));
        }
        li.appendChild(head);
        if (item.preview) {
          const pre = document.createElement("pre");
          pre.className = "pr-change-preview";
          // 预览是仓库文件内容，可能含 HTML——一律 textContent，绝不 innerHTML
          pre.textContent = String(item.preview);
          li.appendChild(pre);
        }
        list.appendChild(li);
      });
      listWrap.appendChild(list);
      box.appendChild(listWrap);
    }

    const actions = document.createElement("div");
    actions.className = "pr-confirm-actions";

    const cancel = captureElement("button", "pr-cancel", t("dialog_cancel"));
    cancel.type = "button";
    cancel.addEventListener("click", closeConfirm);

    const busy = mode === "creating" || inFlight;
    const action = captureElement("button", "pr-confirm-btn", busy ? t("pr_creating") : t("pr_confirm_action"));
    action.type = "button";
    action.addEventListener("click", submitFix);
    if (busy) {
      action.disabled = true;
      action.setAttribute("aria-busy", "true");
      cancel.disabled = true;
      action.appendChild(creatingSpinner());
    }

    actions.appendChild(cancel);
    actions.appendChild(action);
    box.appendChild(actions);
    body.appendChild(box);

    confirmEl = box;
    confirmRefs = { action: action, cancel: cancel };

    // 写操作确认视图：默认焦点必须落在「取消」上，键盘用户 Enter 不会误提交
    if (mode === "confirm") {
      if (!box.contains(document.activeElement)) cancel.focus();
    } else if (box.contains(document.activeElement)) {
      action.focus();
    }
  }

  /* ── 状态迁移 ───────────────────────────────────────────── */

  function setChecking() {
    if (!panelEl) return;
    proposal = null;
    noticeKey = "";
    errorTag = "";
    errorMessage = "";
    extraWriteDisabled = false;
    mode = "checking";
    paintPanel();
  }

  function setNotice(nextMode) {
    if (!panelEl) return;
    mode = nextMode;
    noticeKey = nextMode;
    paintPanel();
  }

  function setError(error, status, withWriteHint) {
    if (!panelEl) return;
    mode = "error";
    const raw = (error && (error.detail || error.message)) || String(error || "");
    // 403/401 有两种来源：写模式关闭（可行动：下载 .patch 手动应用）与鉴权失败
    // （要去设置里填/改 API key）。混为一谈会把用户引向错误的修法，因此后者交给
    // core.js 的全局引导统一提示，并且不再显示「写模式关闭」这句话。
    const tag = (error && error.status) || status || "";
    const isAuthProblem =
      (tag === "401" || tag === "403") && String(raw).toLowerCase().indexOf("api key") !== -1;
    if (isAuthProblem && typeof IA.notifyUnauthorized === "function") {
      IA.notifyUnauthorized(Number(tag));
    }
    // 后端 detail 或 HTTP 状态统一套进 pr_failed 模板，保持与其它提示同一语域；
    // 模板缺失（返回 key 本身）时退回原文，绝不显示 "pr_failed" 这种键名
    const template = t("pr_failed", { message: "@@" });
    errorMessage = template.indexOf("@@") === -1 ? raw : template.replace("@@", raw);
    errorTag = tag;
    extraWriteDisabled = !!withWriteHint && !isAuthProblem;
    paintPanel();
  }

  function openConfirm() {
    if (!panelEl) return;
    if (!confirmEl) lastFocus = document.activeElement;
    mode = "confirm";
    paintConfirm();
  }

  function closeConfirm() {
    confirmEl = null;
    confirmRefs = null;
    if (panelEl) {
      mode = "idle";
      paintPanel();
    } else {
      mode = "idle";
    }
    const target = lastFocus;
    lastFocus = null;
    if (target && target.isConnected && typeof target.focus === "function") target.focus();
  }

  /* ── 网络动作 ───────────────────────────────────────────── */

  async function requestProposal() {
    if (inFlight) return;
    const id = sessionId;
    if (!id) return;
    inFlight = true;
    setChecking();

    try {
      const payload = await fetchJson("/session/" + encodeURIComponent(id) + "/proposal");
      if (id !== sessionId || !panelEl) return;
      proposal = payload && typeof payload === "object" ? payload : {};
      const changes = proposal.changes || [];
      if (proposal.write_mode === false) {
        setNotice("write-disabled");
        return;
      }
      if (!changes.length) {
        // 200 但没有任何变更：没有可写入内容，等价于「无提案」
        setNotice("no-proposal");
        return;
      }
      openConfirm();
    } catch (error) {
      if (id !== sessionId || !panelEl) return;
      if (error && error.status === "404") {
        setNotice("no-proposal");
        return;
      }
      setError(error, "", false);
    } finally {
      inFlight = false;
    }
  }

  async function submitFix() {
    if (inFlight) return;
    const id = sessionId;
    if (!id) return;
    inFlight = true;
    mode = "creating";
    paintConfirm();

    try {
      const payload = await fetchJson("/session/" + encodeURIComponent(id) + "/apply-fix", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ confirm: true }),
      });
      if (id !== sessionId || !panelEl) return;
      const url = payload && payload.pr_url ? String(payload.pr_url) : "";
      const branch = payload && payload.branch ? String(payload.branch) : "";
      // pr_url 形如 https://github.com/owner/repo/pull/123 —— 取末段数字；
      // 解析不到就退化为显示分支名（有信息总好过空着）
      const match = url.match(/\/(\d+)\/?$/);
      createdPr = {
        url: url,
        number: match ? match[1] : (branch || ""),
      };
      mode = "created";
      paintPanel();
    } catch (error) {
      if (id !== sessionId || !panelEl) return;
      const status = error && error.status ? error.status : "";
      // 写模式关闭（403）时除了失败原因，额外给出可行动说明（去下载 .patch）
      setError(error, status, status === "403");
    } finally {
      inFlight = false;
    }
  }

  /* ── 事件绑定 ───────────────────────────────────────────── */

  // Escape 只在自己处理：仅在确认视图打开时响应，且不 preventDefault /
  // 不 stopPropagation，避免影响全局其它 Escape 行为（如关闭报告面板）
  function onKeydown(event) {
    if (!event || event.key !== "Escape") return;
    if (mode !== "confirm" || !panelEl || !confirmEl) return;
    closeConfirm();
  }

  const observer = new MutationObserver(scheduleReconcile);

  // 面板注入发生在 app.js 拿到会话详情之前（defer 脚本按顺序执行），
  // 此时 sessionId 仍为空；补一次延迟对账，确保首屏报告也能挂上面板。
  function scheduleStartupPasses() {
    window.setTimeout(scheduleReconcile, 0);
    window.setTimeout(scheduleReconcile, 500);
    window.setTimeout(scheduleReconcile, 1500);
  }

  // 只在 IA.sessionId 真正变化时重置（轮询兜底，成本 1 次字符串比较）
  window.setInterval(function () {
    const id = currentSessionId();
    if (id !== sessionId) scheduleReconcile();
  }, 1000);

  document.addEventListener("keydown", onKeydown);
  document.addEventListener("DOMContentLoaded", function () {
    ensureObserved();
    scheduleStartupPasses();
  });
  if (document.readyState !== "loading") {
    ensureObserved();
    scheduleStartupPasses();
  } else if (!ensureObserved()) {
    // #report 尚未出现（模板改版等极端情况）：短轮询等待容器
    const waitRoot = window.setInterval(function () {
      if (ensureObserved()) {
        window.clearInterval(waitRoot);
        scheduleStartupPasses();
      }
    }, 200);
  }

  // 供 e2e / 调试读取的面板状态（不参与业务逻辑）
  IA.prPanel = {
    getState: function () {
      return {
        mode: mode,
        sessionId: sessionId,
        panel: !!panelEl,
        confirm: !!confirmEl,
        noticeKey: noticeKey,
        changes: proposal && proposal.changes ? proposal.changes.length : 0,
        prUrl: createdPr ? createdPr.url : "",
      };
    },
  };
})();
