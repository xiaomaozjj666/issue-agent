(function () {
  "use strict";

  const IA = window.IssueAgent;
  const t = IA.translate;

  // 应用状态
  let sessionId = null;
  IA.sessionId = null;
  let report = null;
  let showArchived = false;
  let historySearchTimer = null;
  let sessionsRequestId = 0;
  let dialogSession = null;
  let dialogMode = null;
  let navigationStack = [];
  let backInProgress = false;
  let currentStream = null;
  let lastFailedChat = null;
  let activeSession = null; // 当前活跃会话详情（用于生成 GitHub 链接）

  function enumLabel(prefix, value) {
    const key = `${prefix}_${IA.safeClass(value)}`;
    const translated = t(key);
    return translated === key ? String(value || "") : translated;
  }

  // 主题切换
  function applyStoredTheme() {
    const stored = localStorage.getItem("ds-theme");
    if (stored) document.documentElement.dataset.theme = stored;
    const themeBtn = document.getElementById("theme-toggle-btn");
    if (themeBtn) {
      themeBtn.setAttribute("aria-pressed", String(stored === "light"));
    }
  }

  function toggleTheme() {
    const html = document.documentElement;
    const next = html.dataset.theme === "dark" ? "light" : "dark";
    html.dataset.theme = next;
    localStorage.setItem("ds-theme", next);
    const themeBtn = document.getElementById("theme-toggle-btn");
    if (themeBtn) themeBtn.setAttribute("aria-pressed", String(next === "light"));
    // 主题切换后，若报告面板已展开则重渲染图表以同步配色
    if (report && document.getElementById("main").classList.contains("report-open")) {
      renderReport(report);
    }
  }

  // 移动端侧边栏切换
  function toggleMobileHistory() {
    const sidebar = document.getElementById("sidebar");
    const open = sidebar.classList.toggle("mobile-history-open");
    const btn = document.getElementById("toggle-history-btn");
    if (btn) btn.setAttribute("aria-expanded", String(open));
    updateBackButton();
  }

  function updateBackButton() {
    const reportOpen = document.getElementById("main").classList.contains("report-open");
    const historyOpen = document.getElementById("sidebar").classList.contains("mobile-history-open");
    document.getElementById("back-button").disabled = backInProgress || !(reportOpen || historyOpen || sessionId || navigationStack.length);
  }

  async function goBack() {
    if (backInProgress) return;
    if (document.getElementById("main").classList.contains("report-open")) {
      toggleReport(false);
      return;
    }
    const sidebar = document.getElementById("sidebar");
    if (sidebar.classList.contains("mobile-history-open")) {
      sidebar.classList.remove("mobile-history-open");
      document.getElementById("toggle-history-btn").setAttribute("aria-expanded", "false");
      updateBackButton();
      return;
    }
    backInProgress = true;
    updateBackButton();
    const target = navigationStack.length ? navigationStack.pop() : null;
    try {
      if (target) {
        const restored = await restoreSession(target, false);
        if (!restored) navigationStack.push(target);
      } else {
        sessionId = null;
        IA.sessionId = null;
        report = null;
        activeSession = null;
        resetWorkspace(true);
        await loadSessions();
      }
    } finally {
      backInProgress = false;
      updateBackButton();
    }
  }

  function scheduleHistorySearch() {
    clearTimeout(historySearchTimer);
    historySearchTimer = setTimeout(loadSessions, 220);
  }

  function toggleArchiveView() {
    showArchived = !showArchived;
    const button = document.getElementById("archive-toggle");
    button.classList.toggle("active", showArchived);
    button.textContent = showArchived ? t("archive_toggle_active") : t("archive_toggle_show");
    document.getElementById("history-title").textContent = showArchived ? t("history_title_archive") : t("history_title_sessions");
    loadSessions();
  }

  // 历史列表：增量更新，避免每事件后整片闪烁
  let sessionsCache = [];
  const SESSION_ROW_KEY = "session-row-";

  async function loadSessions() {
    const requestId = ++sessionsRequestId;
    const list = document.getElementById("history-list");
    const query = (document.getElementById("history-search").value || "").trim();
    if (!list.querySelector(".history-loading")) {
      list.innerHTML = `<div class="history-loading">${IA.escapeHtml(t("loading_session"))}</div>`;
    }
    try {
      const sessions = await IA.apiJson("/sessions?archived=" + showArchived + "&q=" + encodeURIComponent(query));
      if (requestId !== sessionsRequestId) return;
      sessionsCache = sessions || [];
      renderSessions(sessionsCache);
    } catch (error) {
      if (requestId !== sessionsRequestId) return;
      list.innerHTML = `<div class="history-empty history-error">${IA.escapeHtml(error.message)}</div>`;
    }
  }

  function renderSessions(sessions) {
    const list = document.getElementById("history-list");
    list.innerHTML = "";
    if (!sessions.length) {
      const emptyKey = showArchived ? "history_empty_archive" : "history_empty_active";
      list.innerHTML = `<div class="history-empty">${t(emptyKey)}</div>`;
      return;
    }
    const groups = new Map();
    sessions.forEach(function (session) {
      const group = session.status === "running" ? t("history_group_running") : historyGroup(session.updated_at);
      if (!groups.has(group)) groups.set(group, []);
      groups.get(group).push(session);
    });
    [t("history_group_running"), t("history_group_today"), t("history_group_week"), t("history_group_older")].forEach(function (groupName) {
      const items = groups.get(groupName);
      if (!items) return;
      const group = document.createElement("section");
      group.className = "session-group";
      const heading = document.createElement("div");
      heading.className = "session-group-title";
      heading.textContent = groupName;
      group.appendChild(heading);
      items.forEach(function (item) {
        group.appendChild(createSessionRow(item));
      });
      list.appendChild(group);
    });
  }

  function createSessionRow(session) {
    const row = document.createElement("div");
    row.className = "session-row" + (session.session_id === sessionId ? " active" : "");
    row.dataset.sessionId = session.session_id;
    row.id = SESSION_ROW_KEY + session.session_id;

    const card = document.createElement("button");
    card.type = "button";
    card.className = "session-card";
    card.dataset.sessionId = session.session_id;
    const repository = session.owner && session.repo ? session.owner + "/" + session.repo : repositoryFromUrl(session.issue_url);
    const issue = session.issue_number ? " #" + session.issue_number : "";
    // H9 修复：tooltip 显示本地化的 phase / status，而非原始 enum 值
    card.title = session.phase
      ? enumLabel("phase", session.phase)
      : session.status
        ? enumLabel("status", session.status)
        : "";
    card.innerHTML =
      `<div class="session-repo"><span class="status-dot ${IA.safeClass(session.status)}" aria-hidden="true"></span><span>${IA.escapeHtml(repository + issue)}</span></div>` +
      `<div class="session-title">${IA.escapeHtml(session.title)}</div>` +
      `<div class="session-time">${IA.escapeHtml(IA.formatRelativeTime(session.updated_at))}</div>`;
    row.appendChild(card);

    const actions = document.createElement("div");
    actions.className = "session-actions";
    actions.appendChild(sessionAction("rename", t("action_rename_short"), function () {
      renameSession(session);
    }));
    actions.appendChild(sessionAction(showArchived ? "restore" : "archive", showArchived ? t("restore_session") : t("archive_session"), function () {
      archiveSession(session, !showArchived);
    }));
    if (showArchived) actions.appendChild(sessionAction("delete", t("dialog_delete_forever"), function () {
      openDeleteDialog(session);
    }));
    row.appendChild(actions);
    return row;
  }

  function sessionAction(iconKey, label, handler) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "session-action";
    button.title = label;
    button.setAttribute("aria-label", label);
    button.innerHTML = IA.svgIcon(iconKey);
    button.addEventListener("click", function (event) {
      event.stopPropagation();
      handler();
    });
    return button;
  }

  function historyGroup(value) {
    const date = new Date(value);
    const now = new Date();
    if (date.toDateString() === now.toDateString()) return t("history_group_today");
    return now - date < 7 * 24 * 60 * 60 * 1000 ? t("history_group_week") : t("history_group_older");
  }

  function repositoryFromUrl(url) {
    const match = String(url || "").match(/github\.com\/([^/]+)\/([^/]+)/i);
    return match ? match[1] + "/" + match[2] : t("fallback_repo_name");
  }

  // 抽取公共的"取消当前 stream"逻辑：
  // 用于 restoreSession/goBack/analyze 互相切换时清理上一次分析的 reader 与状态
  // 避免旧 stream 事件继续往新视图写入造成 UI 错乱
  async function cancelCurrentStream() {
    if (currentStream) {
      try {
        await currentStream.cancel();
      } catch (e) {
        /* ignore */
      }
      currentStream = null;
    }
    stopAnalysisTimer();
    currentPhaseText = "";
    analyzeInProgress = false;
    document.getElementById("progress").textContent = "";
  }

  // restoreSession 请求追踪：避免快速连续切换会话时旧响应覆盖新视图
  let restoreRequestId = 0;

  async function restoreSession(id, recordHistory) {
    if (recordHistory === undefined) recordHistory = true;
    // C1/C2 修复：切换会话前必须取消正在进行的分析流，否则旧 stream 事件
    // 会继续写 sessionId/渲染消息，覆盖用户刚切换到的会话视图
    await cancelCurrentStream();
    // C3 修复：chat 进行中切换会话时也要中断 chat 请求，否则响应会落到新视图
    if (chatAbortController) {
      try {
        chatAbortController.abort();
      } catch (e) {
        /* ignore */
      }
      chatAbortController = null;
      chatInProgress = false;
    }
    const myRequestId = ++restoreRequestId;
    const messagesContainer = document.getElementById("messages");
    messagesContainer.innerHTML = `<div class="history-loading">${IA.escapeHtml(t("loading_session"))}</div>`;
    try {
      const session = await IA.apiJson("/session/" + encodeURIComponent(id));
      // 防竞态：await 期间用户可能又点了另一个会话，丢弃过期响应
      if (myRequestId !== restoreRequestId) return false;
      if (recordHistory && sessionId !== session.session_id) navigationStack.push(sessionId || null);
      sessionId = session.session_id;
      IA.sessionId = sessionId;
      report = session.report;
      activeSession = session;
      resetWorkspace(false);
      document.getElementById("issueUrl").value = "";
      document.querySelector(".conversation-label").textContent =
        session.owner && session.repo
          ? session.owner + "/" + session.repo + (session.issue_number ? " #" + session.issue_number : "")
          : t("conversation_label");
      IA.Runtime.setCancelVisible(session.status === "running");
      if (session.events && session.events.length) window.addEventTimeline(session.events, session.metrics);
      if (report) {
        renderReport(report);
        document.getElementById("report-toggle").style.display = "inline-flex";
        addReportPreview(report);
      }
      session.messages.forEach(function (message) {
        if ((message.role === "user" || message.role === "assistant") && message.content) addMsg(message.role, message.content);
      });
      if (report) {
        if (!session.archived && session.status !== "running") document.getElementById("input-bar").style.display = "flex";
        if (session.archived) addMsg("system", t("archived_readonly"));
      } else if (session.status === "failed") {
        addMsg("error", session.error_message || t("failed_default"));
      } else if (session.status === "cancelled") {
        addMsg("system", t("cancelled_default"));
      } else if (session.status === "running") {
        addMsg("system", t("running_default"));
      } else {
        addMsg("system", t("no_report_default"));
      }
      document.getElementById("sidebar").classList.remove("mobile-history-open");
      const toggleBtn = document.getElementById("toggle-history-btn");
      if (toggleBtn) toggleBtn.setAttribute("aria-expanded", "false");
      updateBackButton();
      await loadSessions();
      return true;
    } catch (error) {
      if (myRequestId !== restoreRequestId) return false;
      messagesContainer.innerHTML = "";
      addMsg("error", error.message);
      return false;
    }
  }

  async function renameSession(session) {
    dialogSession = session;
    dialogMode = "rename";
    document.getElementById("dialog-title").textContent = t("dialog_rename_title");
    document.getElementById("dialog-message").textContent = t("dialog_rename_message");
    const input = document.getElementById("dialog-input");
    input.style.display = "block";
    input.value = session.title;
    const confirmButton = document.getElementById("dialog-confirm");
    confirmButton.textContent = t("dialog_save");
    confirmButton.className = "confirm";
    const dialog = document.getElementById("session-dialog");
    dialog.showModal();
    input.select();
  }

  function openDeleteDialog(session) {
    dialogSession = session;
    dialogMode = "delete";
    document.getElementById("dialog-title").textContent = t("dialog_delete_title");
    document.getElementById("dialog-message").textContent = t("dialog_delete_message", { title: session.title });
    document.getElementById("dialog-input").style.display = "none";
    const confirmButton = document.getElementById("dialog-confirm");
    confirmButton.textContent = t("dialog_delete_forever");
    confirmButton.className = "danger";
    document.getElementById("session-dialog").showModal();
    confirmButton.focus();
  }

  function closeSessionDialog() {
    document.getElementById("session-dialog").close();
    dialogSession = null;
    dialogMode = null;
  }

  async function submitSessionDialog(event) {
    event.preventDefault();
    if (!dialogSession) return;
    if (dialogMode === "delete") {
      const session = dialogSession;
      closeSessionDialog();
      await deleteSession(session);
      return;
    }
    const title = document.getElementById("dialog-input").value.trim();
    if (!title) return;
    if (title === dialogSession.title) {
      closeSessionDialog();
      return;
    }
    try {
      await IA.apiJson("/session/" + encodeURIComponent(dialogSession.session_id), {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ display_title: title.trim() }),
      });
      closeSessionDialog();
      await loadSessions();
    } catch (error) {
      addMsg("error", error.message);
    }
  }

  async function archiveSession(session, archived) {
    try {
      await IA.apiJson("/session/" + encodeURIComponent(session.session_id), {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ archived: archived }),
      });
      if (archived && session.session_id === sessionId) {
        sessionId = null;
        IA.sessionId = null;
        report = null;
        activeSession = null;
        resetWorkspace(true);
      }
      if (!archived) {
        showArchived = false;
        document.getElementById("archive-toggle").classList.remove("active");
        document.getElementById("archive-toggle").textContent = t("archive_toggle_show");
        document.getElementById("history-title").textContent = t("history_title_sessions");
        if (session.session_id === sessionId) {
          await restoreSession(session.session_id, false);
          return;
        }
      }
      await loadSessions();
    } catch (error) {
      addMsg("error", error.message);
    }
  }

  async function deleteSession(session) {
    try {
      await IA.apiJson("/session/" + encodeURIComponent(session.session_id), { method: "DELETE" });
      if (session.session_id === sessionId) {
        sessionId = null;
        IA.sessionId = null;
        report = null;
        activeSession = null;
        resetWorkspace(true);
      }
      await loadSessions();
    } catch (error) {
      addMsg("error", error.message);
    }
  }

  function resetWorkspace(showWelcome) {
    document.getElementById("messages").innerHTML = "";
    document.getElementById("main").classList.remove("report-open");
    document.getElementById("report-toggle").style.display = "none";
    document.getElementById("report").innerHTML = "";
    document.getElementById("input-bar").style.display = "none";
    IA.Runtime.setCancelVisible(false);
    document.getElementById("progress").textContent = "";
    document.querySelector(".conversation-label").textContent = t("conversation_label");
    if (showWelcome) {
      document.getElementById("issueUrl").value = "";
      renderHero();
    }
    updateBackButton();
  }

  // 渲染高级 Hero 欢迎页：全屏可交互背景式设计
  // 替代原简陋的 system 消息气泡，作为初始空状态的视觉锚点
  function renderHero() {
    const container = document.getElementById("messages");
    const hero = document.createElement("div");
    hero.className = "msg hero";

    // 三个示例案例：点击后填充 URL 并自动开始分析
    const examples = [
      { repo: t("hero_example_1_repo"), desc: t("hero_example_1_desc"), url: "https://github.com/psf/requests/issues/6102" },
      { repo: t("hero_example_2_repo"), desc: t("hero_example_2_desc"), url: "https://github.com/python/cpython/issues/118658" },
      { repo: t("hero_example_3_repo"), desc: t("hero_example_3_desc"), url: "https://github.com/pallets/flask/issues/5680" },
    ];

    const stepsHtml = [
      { label: t("hero_step1_label"), title: t("hero_step1_title"), desc: t("hero_step1_desc") },
      { label: t("hero_step2_label"), title: t("hero_step2_title"), desc: t("hero_step2_desc") },
      { label: t("hero_step3_label"), title: t("hero_step3_title"), desc: t("hero_step3_desc") },
    ]
      .map(function (s) {
        return (
          `<div class="hero-step">` +
          `<span class="hero-step-label">${IA.escapeHtml(s.label)}</span>` +
          `<div class="hero-step-title">${IA.escapeHtml(s.title)}</div>` +
          `<p class="hero-step-desc">${IA.escapeHtml(s.desc)}</p>` +
          `</div>`
        );
      })
      .join("");

    const examplesHtml = examples
      .map(function (ex, idx) {
        return (
          `<button type="button" class="hero-example" data-hero-url="${IA.escapeAttr(ex.url)}">` +
          `<span class="hero-example-repo">${IA.escapeHtml(ex.repo)}</span>` +
          `<span class="hero-example-desc">${IA.escapeHtml(ex.desc)}</span>` +
          `<span class="hero-example-cta">${IA.escapeHtml(t("hero_start_button"))} →</span>` +
          `</button>`
        );
      })
      .join("");

    hero.innerHTML =
      `<div class="hero-inner">` +
      // 标题区
      `<header class="hero-head">` +
      `<span class="hero-eyebrow"><span class="hero-eyebrow-dot"></span>${IA.escapeHtml(t("hero_eyebrow_text"))}</span>` +
      `<h1 class="hero-title">${IA.escapeHtml(t("hero_title"))}` +
      `<span class="hero-title-accent">${IA.escapeHtml(t("hero_title_accent"))}</span></h1>` +
      `<p class="hero-subtitle">${IA.escapeHtml(t("hero_subtitle"))}</p>` +
      `</header>` +
      // 三步流程卡片
      `<section class="hero-steps">${stepsHtml}</section>` +
      // 示例案例区
      `<section class="hero-examples">` +
      `<div class="hero-examples-head">` +
      `<span class="hero-examples-title">${IA.escapeHtml(t("hero_examples_title"))}</span>` +
      `<span class="hero-examples-desc">${IA.escapeHtml(t("hero_examples_desc"))}</span>` +
      `</div>` +
      `<div class="hero-example-list">${examplesHtml}</div>` +
      `</section>` +
      // 主 CTA 按钮：聚焦到 URL 输入框
      `<div class="hero-cta">` +
      `<button type="button" class="hero-cta-button" id="hero-cta-btn">` +
      IA.svgIcon("plus") +
      `<span>${IA.escapeHtml(t("hero_start_button"))}</span>` +
      `</button>` +
      `</div>` +
      `</div>`;

    container.appendChild(hero);

    // 绑定示例案例点击：填充 URL 并自动开始分析
    hero.querySelectorAll(".hero-example").forEach(function (btn) {
      btn.addEventListener("click", function () {
        const url = btn.dataset.heroUrl;
        if (!url) return;
        document.getElementById("issueUrl").value = url;
        analyze();
      });
    });

    // 主 CTA：聚焦 URL 输入框
    const ctaBtn = hero.querySelector("#hero-cta-btn");
    if (ctaBtn) {
      ctaBtn.addEventListener("click", function () {
        const input = document.getElementById("issueUrl");
        input.focus();
        input.scrollIntoView({ behavior: "smooth", block: "center" });
      });
    }
  }

  // H5 修复：用户上滑阅读时不要把视图拉回底部。threshold=80px 容许小幅滚动仍跟随。
  function scrollToBottomIfNear(container, threshold) {
    if (!container) return;
    const distance = container.scrollHeight - container.scrollTop - container.clientHeight;
    if (distance > (threshold || 80)) return;
    requestAnimationFrame(function () {
      container.scrollTop = container.scrollHeight;
    });
  }

  function addMsg(role, content, cls) {
    const d = document.getElementById("messages");
    const m = document.createElement("div");
    m.className = "msg " + role + (cls ? " " + cls : "");
    if (role === "user") m.dataset.speaker = t("speaker_you");
    if (role === "assistant") m.dataset.speaker = t("speaker_agent");
    // M8 修复：错误消息声明 role=alert，让屏幕阅读器立即朗读；普通系统消息保持 polite
    if (role === "error") {
      m.setAttribute("role", "alert");
      m.setAttribute("aria-live", "assertive");
    } else if (role === "system") {
      m.setAttribute("role", "status");
    }
    m.textContent = content;
    d.appendChild(m);
    // H5 修复：仅在用户已在底部附近时自动滚动，避免打断阅读
    scrollToBottomIfNear(d, 80);
    return m;
  }

  function addReportPreview(data) {
    const container = document.getElementById("messages");
    const card = document.createElement("article");
    card.className = "msg assistant report-preview";
    const review = data.review_audit || { status: "not_run" };
    const reviewChip =
      review.status !== "not_run"
        ? `<span class="review-chip ${IA.safeClass(review.status)}">${IA.escapeHtml(
            t("report_independent_review", { status: enumLabel("review_status", review.status) }),
          )}</span>`
        : "";
    card.innerHTML =
      `<div class="report-preview-label">${IA.escapeHtml(t("analysis_complete_label"))}</div>` +
      `<h3 class="report-preview-title">${IA.escapeHtml(data.summary)}</h3>` +
      `<p class="report-preview-root"><strong>${IA.escapeHtml(t("report_root_cause"))}</strong><br>${IA.escapeHtml(data.root_cause)}</p>` +
      `<div class="report-preview-footer"><span class="badge ${IA.safeClass(data.confidence)}">${IA.escapeHtml(enumLabel("confidence", data.confidence))}</span>${reviewChip}` +
      `<button class="report-preview-button" type="button">${IA.escapeHtml(t("open_full_report"))}</button></div>`;
    card.querySelector(".report-preview-button").addEventListener("click", function () {
      toggleReport(true);
    });
    container.appendChild(card);
    // H5 修复：报告预览出现时也尊重用户当前滚动位置
    scrollToBottomIfNear(container, 120);
    return card;
  }

  // 工具卡片：tool_call 时仅创建头部，等到 tool_result 再展开
  function addToolCard(name, args) {
    const d = document.getElementById("messages");
    const m = document.createElement("div");
    m.className = "msg tool";
    m.setAttribute("data-tool-name", IA.safeClass(name));
    const argsText = (() => {
      try {
        return JSON.stringify(args).substring(0, 80);
      } catch (e) {
        return "";
      }
    })();
    m.innerHTML =
      `<div class="preview"><b>${IA.escapeHtml(name)}</b> ${IA.escapeHtml(argsText)}${argsText.length >= 80 ? "..." : ""}</div>` +
      `<div class="full" aria-hidden="true"></div>`;
    m.addEventListener("click", function () {
      m.classList.toggle("expanded");
    });
    d.appendChild(m);
    // H5 修复：工具卡片创建时尊重用户滚动位置
    scrollToBottomIfNear(d, 80);
    return m;
  }

  function fillToolCard(card, preview) {
    if (!card) return;
    const full = card.querySelector(".full");
    if (full) {
      full.textContent = preview || "";
      full.setAttribute("aria-hidden", String(!preview));
    }
    if (preview) card.classList.add("expanded");
  }

  const ISSUE_URL_PATTERN = /^https:\/\/github\.com\/[^/\s]+\/[^/\s]+\/issues\/\d+(?:[/?#].*)?$/i;

  function formatErrorDetail(detail) {
    if (detail == null) return "";
    if (typeof detail === "string") return detail;
    // FastAPI 422 返回 [{loc, msg, type}, ...] 数组，需要序列化为可读字符串
    if (Array.isArray(detail)) {
      return detail
        .map((item) => {
          if (item && typeof item === "object") {
            const loc = Array.isArray(item.loc) ? item.loc.join(".") : String(item.loc ?? "");
            return (loc ? loc + ": " : "") + (item.msg || item.type || "");
          }
          return String(item);
        })
        .join("; ");
    }
    try {
      return JSON.stringify(detail);
    } catch (e) {
      return String(detail);
    }
  }

  let analyzeInProgress = false;

  // 实时分析计时器：调查过程中在 progress 区域显示阶段文本和已用时间
  let analysisTimerId = null;
  let analysisStartTime = 0;
  let currentPhaseText = "";

  function formatElapsed(seconds) {
    if (seconds < 60) return seconds + "s";
    const m = Math.floor(seconds / 60);
    const s = seconds % 60;
    return m + ":" + (s < 10 ? "0" + s : s);
  }

  function startAnalysisTimer() {
    stopAnalysisTimer();
    analysisStartTime = Date.now();
    analysisTimerId = window.setInterval(function () {
      const elapsed = Math.floor((Date.now() - analysisStartTime) / 1000);
      const phase = currentPhaseText || t("fetching");
      document.getElementById("progress").textContent = phase + " · " + t("elapsed_time", { seconds: formatElapsed(elapsed) });
    }, 1000);
  }

  function stopAnalysisTimer() {
    if (analysisTimerId !== null) {
      window.clearInterval(analysisTimerId);
      analysisTimerId = null;
    }
    analysisStartTime = 0;
  }

  function setAnalysisPhase(text) {
    currentPhaseText = text || "";
    if (analysisTimerId !== null) {
      const elapsed = Math.floor((Date.now() - analysisStartTime) / 1000);
      document.getElementById("progress").textContent =
        currentPhaseText + " · " + t("elapsed_time", { seconds: formatElapsed(elapsed) });
    } else {
      document.getElementById("progress").textContent = currentPhaseText;
    }
  }

  async function analyze() {
    // 防重入：快速双击或回车多次时只允许一个流，避免状态错乱和旧 reader 泄漏
    if (analyzeInProgress) return;
    // C3 修复：chat 进行中开始新分析会丢失 chat 响应，需要先中断 chat
    if (chatInProgress) {
      if (chatAbortController) {
        try {
          chatAbortController.abort();
        } catch (e) {
          /* ignore */
        }
        chatAbortController = null;
      }
      chatInProgress = false;
    }
    const raw = document.getElementById("issueUrl").value.trim();
    if (!raw) return;
    // 从可能粘贴的多行文本中提取第一个 GitHub issue URL，避免把终端日志整体当 URL 发送
    const match = raw.match(/https:\/\/github\.com\/[^/\s]+\/[^/\s]+\/issues\/\d+(?:[/?#][^\s]*)?/i);
    const url = match ? match[0] : raw.split(/\s+/)[0];
    if (!ISSUE_URL_PATTERN.test(url)) {
      addMsg("error", t("error_prefix") + t("invalid_issue_url"));
      // M11 修复：URL 错误时输入框加视觉提示
      const input = document.getElementById("issueUrl");
      input.setAttribute("aria-invalid", "true");
      input.classList.add("invalid-input");
      input.focus();
      setTimeout(function () {
        input.removeAttribute("aria-invalid");
        input.classList.remove("invalid-input");
      }, 2000);
      return;
    }

    // 取消任何仍存在的旧流，防止 reader 泄漏和后端会话被遗弃为 interrupted
    if (currentStream) {
      try {
        await currentStream.cancel();
      } catch (e) {
        /* ignore */
      }
      currentStream = null;
    }

    analyzeInProgress = true;
    if (sessionId) navigationStack.push(sessionId);
    sessionId = null;
    IA.sessionId = null;
    report = null;
    activeSession = null;
    resetWorkspace(false);
    currentPhaseText = t("fetching");
    startAnalysisTimer();
    document.getElementById("progress").textContent = currentPhaseText;
    addMsg("assistant", t("analyzing_prefix") + url);

    try {
      const resp = await fetch("/stream", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ issue_url: url }),
      });
      if (!resp.ok) {
        let detail = t("error_unable_to_start");
        try {
          const body = await resp.json();
          detail = formatErrorDetail(body.detail) || detail;
        } catch (e) {
          /* ignore */
        }
        throw new Error(detail);
      }
      currentStream = resp.body.getReader();
      const decoder = new TextDecoder();
      let buf = "";
      let toolCard = null;

      // C4 修复：stream 看门狗。30s 无事件显示"连接似乎变慢"提示，
      // 90s 仍未恢复则提示用户取消。每次收到任意事件重置计时器。
      let lastEventTime = Date.now();
      let slowWarned = false;
      let stallWarned = false;
      const watchdogTimer = window.setInterval(function () {
        const elapsed = Date.now() - lastEventTime;
        if (elapsed >= 90000 && !stallWarned) {
          stallWarned = true;
          addMsg("system", t("stream_stalled"));
        } else if (elapsed >= 30000 && !slowWarned && !stallWarned) {
          slowWarned = true;
          addMsg("system", t("stream_slow"));
        }
      }, 5000);

      try {
        while (true) {
          const { value, done } = await currentStream.read();
          if (done) break;
          // 收到数据即重置看门狗
          lastEventTime = Date.now();
          buf += decoder.decode(value, { stream: true });
          const lines = buf.split("\n");
          buf = lines.pop() || "";
          for (const line of lines) {
            if (!line.startsWith("data: ")) continue;
            const data = line.slice(6);
            // 后端从不发送 [DONE]；done 状态通过 done 事件类型处理，无需此分支
            try {
              const evt = JSON.parse(data);
              await handleStreamEvent(evt, { getToolCard: () => toolCard, setToolCard: (c) => (toolCard = c) });
            } catch (e) {
              console.warn("Ignored malformed stream event", e);
            }
          }
        }
      } finally {
        window.clearInterval(watchdogTimer);
      }
    } catch (e) {
      addMsg("error", t("connection_error") + e.message);
      document.getElementById("progress").textContent = "";
      IA.Runtime.setCancelVisible(false);
    } finally {
      stopAnalysisTimer();
      currentStream = null;
      analyzeInProgress = false;
    }
  }

  // 流式 reasoning 卡片：首个 delta 创建 details/summary，后续 delta 追加到 body。
  // 收到任意非 reasoning 事件时关闭当前卡片，避免与后续消息交叉。
  let currentReasoningCard = null;

  function closeReasoningCard() {
    if (!currentReasoningCard) return;
    currentReasoningCard.open = false;
    // 完成后将 summary 从"思考中…"更新为"思考完成"，避免结果出来后仍显示思考中
    const summary = currentReasoningCard.querySelector(".reasoning-summary");
    if (summary) summary.textContent = t("thinking_complete");
    currentReasoningCard = null;
  }

  function appendReasoningDelta(delta) {
    if (!delta) return;
    const container = document.getElementById("messages");
    if (!currentReasoningCard) {
      const card = document.createElement("details");
      card.className = "msg assistant reasoning-card";
      card.open = true;
      const summary = document.createElement("summary");
      summary.className = "reasoning-summary";
      summary.textContent = t("thinking");
      card.appendChild(summary);
      const body = document.createElement("pre");
      body.className = "reasoning-body";
      card.appendChild(body);
      container.appendChild(card);
      currentReasoningCard = card;
    }
    const body = currentReasoningCard.querySelector(".reasoning-body");
    body.textContent += delta;
    // H5 修复：思考流式输出仅在用户在底部时跟随，避免抢占阅读
    scrollToBottomIfNear(container, 80);
  }

  async function handleStreamEvent(evt, toolCardRef) {
    if (evt.type !== "reasoning") closeReasoningCard();
    switch (evt.type) {
      case "session":
        sessionId = evt.data.session_id;
        IA.sessionId = sessionId;
        document.getElementById("issueUrl").value = "";
        IA.Runtime.setCancelVisible(true);
        updateBackButton();
        loadSessions();
        break;
      case "phase":
        setAnalysisPhase(evt.data.label || evt.data.phase);
        break;
      case "start":
        setAnalysisPhase(t("exploring_files", { count: evt.data.file_count }));
        if (evt.data.title) document.querySelector(".conversation-label").textContent = evt.data.title;
        break;
      case "tool_call": {
        setAnalysisPhase(
          evt.data.name +
            ": " +
            (() => {
              try {
                return JSON.stringify(evt.data.args).substring(0, 60);
              } catch (e) {
                return "";
              }
            })(),
        );
        const card = addToolCard(evt.data.name, evt.data.args);
        toolCardRef.setToolCard(card);
        break;
      }
      case "tool_result":
        fillToolCard(toolCardRef.getToolCard(), evt.data.preview || "");
        break;
      case "thinking":
        addMsg("assistant", evt.data.content);
        break;
      case "reasoning":
        appendReasoningDelta(evt.data.delta || "");
        break;
      case "review":
        setAnalysisPhase(t("review_progress", { status: evt.data.status }));
        break;
      case "report":
        report = evt.data;
        activeSession = activeSession || {};
        // 报告生成后停止计时器并清空 progress，避免仍显示"思考中"
        stopAnalysisTimer();
        currentPhaseText = "";
        renderReport(report);
        document.getElementById("input-bar").style.display = "flex";
        document.getElementById("report-toggle").style.display = "inline-flex";
        document.getElementById("progress").textContent = "";
        addReportPreview(report);
        document.getElementById("chatInput").focus();
        loadSessions();
        break;
      case "error":
        stopAnalysisTimer();
        currentPhaseText = "";
        addMsg("error", evt.message || t("error_prefix").trim());
        document.getElementById("progress").textContent = "";
        loadSessions();
        break;
      case "cancelled":
        stopAnalysisTimer();
        currentPhaseText = "";
        addMsg("system", t("cancelled_message"));
        document.getElementById("progress").textContent = "";
        IA.Runtime.setCancelVisible(false);
        loadSessions();
        break;
      case "done":
        // 兜底：确保 done 事件一定停止计时器并清空 progress
        stopAnalysisTimer();
        currentPhaseText = "";
        document.getElementById("progress").textContent = "";
        IA.Runtime.setCancelVisible(false);
        loadSessions();
        break;
      default:
        break;
    }
  }

  // ── ECharts 可视化 ────────────────────────────────────
  // 配色与设计体系一致：深色用 slate-100/slate-400/slate-700，浅色用 slate-900/slate-600/slate-300
  const ECHARTS_PALETTE_DARK = {
    primary: "#3b82f6",
    primaryDim: "#60a5fa",
    success: "#10b981",
    warning: "#f59e0b",
    danger: "#ef4444",
    text: "#f1f5f9",
    textDim: "#94a3b8",
    line: "#334155",
    fill: "rgba(59, 130, 246, 0.45)",
    fillDim: "rgba(59, 130, 246, 0.05)",
    splitArea: ["rgba(59,130,246,0.04)", "rgba(59,130,246,0.08)"],
  };
  const ECHARTS_PALETTE_LIGHT = {
    primary: "#2563eb",
    primaryDim: "#3b82f6",
    success: "#059669",
    warning: "#d97706",
    danger: "#dc2626",
    text: "#0f172a",
    textDim: "#475569",
    line: "#cbd5e1",
    fill: "rgba(37, 99, 235, 0.4)",
    fillDim: "rgba(37, 99, 235, 0.05)",
    splitArea: ["rgba(37,99,235,0.04)", "rgba(37,99,235,0.08)"],
  };

  function getEchartsPalette() {
    return document.documentElement.dataset.theme === "light" ? ECHARTS_PALETTE_LIGHT : ECHARTS_PALETTE_DARK;
  }

  function echartsAvailable() {
    return typeof window.echarts !== "undefined" && !window.__echartsFailed;
  }

  // 报告图表实例缓存：页面切换或重渲染时统一销毁，避免内存泄漏
  let reportChartInstances = [];

  function disposeReportCharts() {
    reportChartInstances.forEach(function (chart) {
      try {
        chart.dispose();
      } catch (e) {
        /* ignore */
      }
    });
    reportChartInstances = [];
  }

  // 窗口尺寸变化时同步调整图表，避免溢出和留白
  let chartResizeTimer = null;
  window.addEventListener("resize", function () {
    if (!reportChartInstances.length) return;
    clearTimeout(chartResizeTimer);
    chartResizeTimer = setTimeout(function () {
      reportChartInstances.forEach(function (chart) {
        try {
          chart.resize();
        } catch (e) {
          /* ignore */
        }
      });
    }, 120);
  });

  // 证据模块分布（横向柱状图）：按目录前缀聚合证据数量
  // 真实意义：回答"问题集中在哪里"，比按单文件聚合更有决策价值
  function renderEvidenceChart(container, report) {
    if (!container) return null;
    if (!echartsAvailable()) {
      // M12 修复：图表库加载失败与"暂无证据"是两件事，文案必须区分
      container.innerHTML = `<div class="report-chart-fallback">${IA.escapeHtml(t("chart_load_failed"))}</div>`;
      return null;
    }
    const palette = getEchartsPalette();

    // 按目录前缀聚合：src/auth/login.py → src/auth/；根目录文件 → 文件名
    // 同时记录每个模块下的具体文件，供 tooltip 展示
    const moduleMap = new Map();
    (report.evidence || []).forEach(function (e) {
      const path = e.path || "unknown";
      const slashIdx = path.lastIndexOf("/");
      let module;
      if (slashIdx === -1) {
        module = path;
      } else if (slashIdx === 0) {
        module = "/(root)";
      } else {
        // 取最后两级目录作为模块名，避免过深
        const parts = path.split("/");
        if (parts.length <= 3) {
          module = parts.slice(0, -1).join("/") + "/";
        } else {
          module = "…/" + parts.slice(-3, -1).join("/") + "/";
        }
      }
      if (!moduleMap.has(module)) {
        moduleMap.set(module, { count: 0, files: new Set() });
      }
      const entry = moduleMap.get(module);
      entry.count += 1;
      entry.files.add(path);
    });

    if (!moduleMap.size) {
      container.innerHTML = `<div class="report-chart-empty">${IA.escapeHtml(t("report_evidence_chart_empty"))}</div>`;
      return null;
    }

    // 按证据数升序排列（横向柱状图：最大的在顶部更易读）
    const entries = Array.from(moduleMap.entries())
      .sort(function (a, b) {
        return a[1].count - b[1].count;
      })
      .slice(-10); // 最多展示 10 个模块
    const modules = entries.map(function (entry) {
      return entry[0];
    });
    const counts = entries.map(function (entry) {
      return entry[1].count;
    });
    const fileLists = entries.map(function (entry) {
      return Array.from(entry[1].files);
    });

    const tooltipBg = palette.text === "#f1f5f9" ? "#0f172a" : "#ffffff";
    const chart = echarts.init(container);
    chart.setOption({
      grid: { left: 8, right: 32, top: 12, bottom: 12, containLabel: true },
      tooltip: {
        trigger: "axis",
        axisPointer: { type: "shadow" },
        confine: true,
        backgroundColor: tooltipBg,
        borderWidth: 0,
        padding: [10, 14],
        textStyle: { color: palette.text, fontSize: 12 },
        formatter: function (params) {
          const idx = params[0].dataIndex;
          const module = entries[idx][0];
          const count = entries[idx][1].count;
          const files = fileLists[idx];
          const filesHtml = files
            .slice(0, 5)
            .map(function (f) {
              return `<div style="color:${palette.textDim};font-size:11px;margin-top:2px;">· ${IA.escapeHtml(f)}</div>`;
            })
            .join("");
          const more = files.length > 5 ? `<div style="color:${palette.textDim};font-size:11px;margin-top:2px;">+${files.length - 5}</div>` : "";
          return `<div style="font-weight:600;margin-bottom:4px;">${IA.escapeHtml(module)}</div>` +
            `<div>${IA.escapeHtml(t("report_legend_evidence"))}: <b>${count}</b></div>` +
            filesHtml + more;
        },
      },
      xAxis: {
        type: "value",
        minInterval: 1,
        axisLabel: { color: palette.textDim, fontSize: 11 },
        axisLine: { lineStyle: { color: palette.line } },
        splitLine: { lineStyle: { color: palette.line, type: "dashed" } },
      },
      yAxis: {
        type: "category",
        data: modules,
        axisLabel: {
          color: palette.textDim,
          fontSize: 11,
          width: 120,
          overflow: "truncate",
          margin: 12,
        },
        axisLine: { lineStyle: { color: palette.line } },
        axisTick: { show: false },
      },
      series: [
        {
          type: "bar",
          data: counts,
          barMaxWidth: 22,
          itemStyle: {
            color: {
              type: "linear",
              x: 0,
              y: 0,
              x2: 1,
              y2: 0,
              colorStops: [
                { offset: 0, color: palette.primaryDim },
                { offset: 1, color: palette.primary },
              ],
            },
            borderRadius: [0, 4, 4, 0],
          },
          emphasis: { itemStyle: { color: palette.primary } },
          label: {
            show: true,
            position: "right",
            color: palette.text,
            fontSize: 11,
            fontWeight: 600,
          },
        },
      ],
    });
    return chart;
  }

  // 报告产出构成（环形图）：展示报告各部分实际产出数量
  // 真实意义：一眼看出报告重心 —— 证据为主/修复为主/测试为主/风险为主
  // 比硬编码雷达图诚实：所有数据都来自 report 实际字段
  function renderConfidenceRadar(container, report) {
    if (!container) return null;
    if (!echartsAvailable()) {
      // M12 修复：图表库加载失败与"暂无产出"是两件事，文案必须区分
      container.innerHTML = `<div class="report-chart-fallback">${IA.escapeHtml(t("chart_load_failed"))}</div>`;
      return null;
    }
    const palette = getEchartsPalette();

    const evidenceCount = (report.evidence || []).length;
    const changesCount = (report.proposed_changes || []).length;
    const testCount = (report.tests || []).length;
    const riskCount = (report.risks || []).length;
    const total = evidenceCount + changesCount + testCount + riskCount;

    if (total === 0) {
      container.innerHTML = `<div class="report-chart-empty">${IA.escapeHtml(t("report_composition_empty"))}</div>`;
      return null;
    }

    const data = [
      { name: t("report_composition_evidence"), value: evidenceCount, color: palette.primary },
      { name: t("report_composition_changes"), value: changesCount, color: palette.success },
      { name: t("report_composition_tests"), value: testCount, color: palette.warning },
      { name: t("report_composition_risks"), value: riskCount, color: palette.danger },
    ].filter(function (d) {
      return d.value > 0;
    });

    const tooltipBg = palette.text === "#f1f5f9" ? "#0f172a" : "#ffffff";
    const chart = echarts.init(container);
    chart.setOption({
      tooltip: {
        trigger: "item",
        confine: true,
        backgroundColor: tooltipBg,
        borderWidth: 0,
        padding: [10, 14],
        textStyle: { color: palette.text, fontSize: 12 },
        formatter: function (params) {
          const pct = total > 0 ? ((params.value / total) * 100).toFixed(1) : "0";
          return `<div style="font-weight:600;margin-bottom:4px;">${IA.escapeHtml(params.name)}</div>` +
            `<div>${IA.escapeHtml(t("report_legend_evidence"))}: <b>${params.value}</b> (${pct}%)</div>`;
        },
      },
      legend: {
        orient: "horizontal",
        bottom: 4,
        icon: "circle",
        itemWidth: 8,
        itemHeight: 8,
        itemGap: 12,
        textStyle: { color: palette.textDim, fontSize: 11 },
      },
      graphic: [
        {
          type: "text",
          left: "center",
          top: "38%",
          style: {
            text: String(total),
            fontSize: 28,
            fontWeight: 700,
            fill: palette.text,
            textAlign: "center",
          },
        },
        {
          type: "text",
          left: "center",
          top: "52%",
          style: {
            text: t("report_composition_total"),
            fontSize: 11,
            fill: palette.textDim,
            textAlign: "center",
          },
        },
      ],
      series: [
        {
          type: "pie",
          radius: ["52%", "72%"],
          center: ["50%", "44%"],
          avoidLabelOverlap: true,
          itemStyle: {
            borderColor: palette.text === "#f1f5f9" ? "#1e293b" : "#ffffff",
            borderWidth: 2,
          },
          label: { show: false },
          labelLine: { show: false },
          emphasis: {
            scale: true,
            scaleSize: 6,
            itemStyle: { shadowBlur: 12, shadowColor: "rgba(0,0,0,0.3)" },
          },
          data: data.map(function (d) {
            return {
              name: d.name,
              value: d.value,
              itemStyle: { color: d.color },
            };
          }),
        },
      ],
    });
    return chart;
  }

  function renderReport(r) {
    disposeReportCharts();
    const d = document.getElementById("report");
    const parts = [];
    const toc = [];

    function pushSection(id, title, bodyHtml) {
      toc.push(`<li><a href="#${id}">${IA.escapeHtml(title)}</a></li>`);
      return `<section class="report-section" id="${id}"><h4>${IA.escapeHtml(title)}</h4>${bodyHtml}</section>`;
    }

    // 1. 核心结论卡（金字塔顶端：结论前置）
    parts.push(
      `<div class="report-conclusion">` +
        `<span class="report-conclusion-label">${IA.escapeHtml(t("report_conclusion_label"))}</span>` +
        `<p class="report-conclusion-text">${IA.escapeHtml(r.summary)}</p>` +
        `</div>`,
    );

    // 2. 关键指标网格（六维度速览）
    const review = r.review_audit || { status: "not_run" };
    const reviewLabel =
      review.status === "not_run" ? t("report_review_pending") : enumLabel("review_status", review.status);
    const metrics = [
      { label: t("report_metric_evidence_count"), value: String((r.evidence || []).length) },
      { label: t("report_metric_files_examined"), value: String((r.files_examined || []).length) },
      { label: t("report_metric_confidence"), value: enumLabel("confidence", r.confidence) },
      { label: t("report_metric_review"), value: reviewLabel },
      { label: t("report_metric_proposed_changes"), value: String((r.proposed_changes || []).length) },
      { label: t("report_metric_risks"), value: String((r.risks || []).length) },
    ];
    const metricsHtml = metrics
      .map(function (m) {
        return (
          `<div class="report-metric-card"><span class="report-metric-label">${IA.escapeHtml(m.label)}</span>` +
          `<span class="report-metric-value">${IA.escapeHtml(m.value)}</span></div>`
        );
      })
      .join("");
    parts.push(`<div class="report-metrics-grid">${metricsHtml}</div>`);

    // 3 & 4. ECharts 双图表并排：证据模块分布 + 报告产出构成
    // 仅当存在证据时才渲染证据图；产出构成图始终渲染（除非全为 0）
    const hasEvidence = r.evidence && r.evidence.length;
    const hasComposition =
      (r.evidence || []).length +
        (r.proposed_changes || []).length +
        (r.tests || []).length +
        (r.risks || []).length >
      0;
    if (hasEvidence || hasComposition) {
      const evidenceChartHtml = hasEvidence
        ? `<div class="report-chart report-chart-half">` +
          `<div class="report-chart-title">${IA.escapeHtml(t("report_evidence_chart_title"))}</div>` +
          `<div id="report-evidence-chart" class="report-chart-canvas"></div>` +
          `<div class="report-chart-caption">${IA.escapeHtml(t("report_evidence_chart_caption"))}</div>` +
          `</div>`
        : "";
      const compositionChartHtml = hasComposition
        ? `<div class="report-chart report-chart-half">` +
          `<div class="report-chart-title">${IA.escapeHtml(t("report_confidence_chart_title"))}</div>` +
          `<div id="report-confidence-chart" class="report-chart-canvas"></div>` +
          `<div class="report-chart-caption">${IA.escapeHtml(t("report_confidence_chart_caption"))}</div>` +
          `</div>`
        : "";
      parts.push(`<div class="report-charts-row">${evidenceChartHtml}${compositionChartHtml}</div>`);
    }

    // 5. 报告工具栏（移至次级位置：核心结论和图表之后）
    parts.push(
      `<div class="report-toolbar">` +
        `<span class="report-toolbar-label">${IA.escapeHtml(t("report_export_label"))}</span>` +
        `<button class="report-action" type="button" data-action="copy-json">${IA.svgIcon("copy")}<span>${IA.escapeHtml(t("copy_button"))}</span></button>` +
        `<button class="report-action" type="button" data-action="download-json">${IA.svgIcon("download")}<span>${IA.escapeHtml(t("download_json"))}</span></button>` +
        `<button class="report-action" type="button" data-action="download-md">${IA.svgIcon("download")}<span>${IA.escapeHtml(t("download_markdown"))}</span></button>` +
        `</div>`,
    );

    // 6. 独立审查（如果执行过）
    if (review.status !== "not_run") {
      const reviewClass = IA.safeClass(review.status);
      let body = `<span class="review-chip ${reviewClass}">${IA.escapeHtml(
        t("report_independent_review", { status: enumLabel("review_status", review.status) }),
      )}</span>`;
      if (review.summary) body += `<p class="review-summary">${IA.escapeHtml(review.summary)}</p>`;
      if (review.findings && review.findings.length) {
        body += `<ul class="review-findings">${review.findings
          .map(function (f) {
            return `<li>${IA.escapeHtml(f)}</li>`;
          })
          .join("")}</ul>`;
      }
      parts.push(`<section class="report-section review-section ${reviewClass}">${body}</section>`);
    }

    // 7. 问题根因
    parts.push(pushSection("report-root", t("report_root_cause"), `<p>${IA.escapeHtml(r.root_cause)}</p>`));

    // 8. 代码证据
    if (r.evidence && r.evidence.length) {
      const items = r.evidence
        .map(function (e) {
          const ghUrl = IA.buildGitHubUrl(activeSession, e.path, e.lines);
          const linkHtml = ghUrl
            ? ` <a class="evidence-link" href="${IA.escapeAttr(ghUrl)}" target="_blank" rel="noopener noreferrer" title="${IA.escapeAttr(t("view_source"))}">${IA.svgIcon("external")}<span class="sr-only">${IA.escapeHtml(t("view_source"))}</span></a>`
            : "";
          return `<div class="evidence-item"><div class="evidence-path">${IA.escapeHtml(e.path)} · ${IA.escapeHtml(e.lines || "")}${linkHtml}</div><p>${IA.escapeHtml(e.reason || "")}</p></div>`;
        })
        .join("");
      parts.push(pushSection("report-evidence", t("report_evidence"), `<div class="evidence-list">${items}</div>`));
    }

    // 9. 修复方案
    if (r.proposed_changes && r.proposed_changes.length) {
      const list = r.proposed_changes
        .map(function (c) {
          return `<li>${IA.escapeHtml(c)}</li>`;
        })
        .join("");
      parts.push(pushSection("report-changes", t("report_proposed_changes"), `<ul>${list}</ul>`));
    }

    // 10. 修复补丁
    if (r.patch) {
      const patchId = "report-patch";
      toc.push(`<li><a href="#${patchId}">${IA.escapeHtml(t("report_patch"))}</a></li>`);
      const patchHtml =
        `<details id="${patchId}"><summary>${IA.escapeHtml(t("report_patch"))}</summary>` +
        `<div class="patch-wrap"><div class="patch-actions"><button type="button" class="patch-copy" data-action="copy-patch">${IA.svgIcon("copy")}<span>${IA.escapeHtml(t("copy_button"))}</span></button></div>` +
        `<pre class="diff-block">${IA.highlightDiff(r.patch)}</pre></div></details>`;
      parts.push(patchHtml);
    }

    // 11. 回归测试
    if (r.tests && r.tests.length) {
      const list = r.tests
        .map(function (item) {
          return `<li>${IA.escapeHtml(item)}</li>`;
        })
        .join("");
      parts.push(pushSection("report-tests", t("report_tests"), `<ul>${list}</ul>`));
    }

    // 12. 风险提示
    if (r.risks && r.risks.length) {
      const list = r.risks
        .map(function (item) {
          return `<li>${IA.escapeHtml(item)}</li>`;
        })
        .join("");
      parts.push(pushSection("report-risks", t("report_risks"), `<ul>${list}</ul>`));
    }

    // 目录前置（金字塔结构下，TOC 作为快速跳转入口）
    if (toc.length) {
      parts.unshift(
        `<details class="report-toc" open><summary>${IA.escapeHtml(t("toc_title"))}</summary><ol>${toc.join("")}</ol></details>`,
      );
    }

    d.innerHTML = parts.join("");

    // 渲染 ECharts 图表（必须在 innerHTML 设置后才能拿到 DOM 节点）
    const evidenceEl = document.getElementById("report-evidence-chart");
    if (evidenceEl) {
      const chart = renderEvidenceChart(evidenceEl, r);
      if (chart) reportChartInstances.push(chart);
    }
    const confidenceEl = document.getElementById("report-confidence-chart");
    if (confidenceEl) {
      const chart = renderConfidenceRadar(confidenceEl, r);
      if (chart) reportChartInstances.push(chart);
    }
  }

  function toggleReport(open) {
    document.getElementById("main").classList.toggle("report-open", open);
    document.getElementById("report-toggle").setAttribute("aria-expanded", String(open));
    if (open) {
      document.getElementById("report").scrollTop = 0;
      const closeBtn = document.querySelector(".report-close");
      if (closeBtn) closeBtn.focus();
      // M1 修复：panel 展开后容器宽度变化，下一帧触发 ECharts resize 以适配新宽度
      requestAnimationFrame(function () {
        if (reportChartInstances && reportChartInstances.length) {
          reportChartInstances.forEach(function (chart) {
            try { chart.resize(); } catch (e) { /* ignore */ }
          });
        }
      });
    } else if (document.getElementById("input-bar").style.display !== "none") {
      document.getElementById("chatInput").focus();
    }
    updateBackButton();
  }

  let chatInProgress = false;

  async function chat(messageOverride) {
    // 防重入：避免快速发送多条消息导致响应乱序
    if (chatInProgress) return;
    const inp = document.getElementById("chatInput");
    const msg = (messageOverride !== undefined ? messageOverride : inp.value).trim();
    if (!msg || !sessionId) return;
    chatInProgress = true;
    inp.value = "";
    setChatDisabled(true);
    addMsg("user", msg);
    lastFailedChat = null;
    document.getElementById("progress").textContent = t("thinking");
    try {
      const resp = await fetch("/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: sessionId, message: msg }),
      });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || t("error_unable_to_continue"));
      document.getElementById("progress").textContent = "";
      addMsg("assistant", data.reply);
      if (data.tools_used && data.tools_used.length) addMsg("system", t("tools_used_label") + data.tools_used.join(", "));
      loadSessions();
    } catch (e) {
      document.getElementById("progress").textContent = "";
      lastFailedChat = msg;
      addErrorWithRetry(t("error_prefix") + e.message);
    } finally {
      chatInProgress = false;
      setChatDisabled(false);
      inp.focus();
    }
  }

  function setChatDisabled(disabled) {
    const sendBtn = document.getElementById("chat-send-btn");
    const input = document.getElementById("chatInput");
    if (sendBtn) sendBtn.disabled = disabled;
    if (input) input.disabled = disabled;
  }

  function addErrorWithRetry(message) {
    const d = document.getElementById("messages");
    const m = document.createElement("div");
    m.className = "msg error with-retry";
    const span = document.createElement("span");
    span.textContent = message;
    m.appendChild(span);
    const retryBtn = document.createElement("button");
    retryBtn.type = "button";
    retryBtn.className = "retry-button";
    retryBtn.innerHTML = IA.svgIcon("retry") + `<span>${IA.escapeHtml(t("retry_send"))}</span>`;
    // once: true 防止事件队列中重复 click 触发多次 chat()
    retryBtn.addEventListener(
      "click",
      function () {
        m.remove();
        if (lastFailedChat !== null) chat(lastFailedChat);
      },
      { once: true },
    );
    m.appendChild(retryBtn);
    d.appendChild(m);
    // H5 修复：错误消息出现时尊重用户当前滚动位置
    scrollToBottomIfNear(d, 120);
  }

  function reportAsMarkdown(r) {
    const lines = [];
    lines.push(`# ${r.summary}`);
    lines.push("");
    lines.push(`**${t("report_confidence")}:** ${enumLabel("confidence", r.confidence)}`);
    lines.push("");
    lines.push(`## ${t("report_root_cause")}`);
    lines.push("");
    lines.push(r.root_cause);
    lines.push("");
    if (r.evidence && r.evidence.length) {
      lines.push(`## ${t("report_evidence")}`);
      lines.push("");
      r.evidence.forEach(function (e) {
        lines.push(`- \`${e.path}\` ${e.lines || ""}: ${e.reason || ""}`);
      });
      lines.push("");
    }
    if (r.proposed_changes && r.proposed_changes.length) {
      lines.push(`## ${t("report_proposed_changes")}`);
      lines.push("");
      r.proposed_changes.forEach(function (c, idx) {
        lines.push(`${idx + 1}. ${c}`);
      });
      lines.push("");
    }
    if (r.patch) {
      lines.push(`## ${t("report_patch_export")}`);
      lines.push("");
      lines.push("```diff");
      lines.push(r.patch);
      lines.push("```");
      lines.push("");
    }
    if (r.tests && r.tests.length) {
      lines.push(`## ${t("report_tests")}`);
      lines.push("");
      r.tests.forEach(function (item, idx) {
        lines.push(`${idx + 1}. ${item}`);
      });
      lines.push("");
    }
    if (r.risks && r.risks.length) {
      lines.push(`## ${t("report_risks")}`);
      lines.push("");
      r.risks.forEach(function (item) {
        lines.push(`- ${item}`);
      });
      lines.push("");
    }
    const review = r.review_audit || { status: "not_run", summary: "", findings: [] };
    if (review.status !== "not_run") {
      lines.push(`## ${t("report_independent_review", { status: enumLabel("review_status", review.status) })}`);
      if (review.summary) lines.push("", review.summary);
      if (review.findings && review.findings.length) {
        lines.push("");
        review.findings.forEach(function (f) {
          lines.push(`- ${f}`);
        });
      }
      lines.push("");
    }
    return lines.join("\n");
  }

  async function handleReportAction(action, reportData) {
    if (!reportData) return;
    if (action === "copy-json") {
      const text = JSON.stringify(reportData, null, 2);
      const ok = await IA.copyToClipboard(text);
      flashToast(ok ? t("copied") : t("copy_failed"));
    } else if (action === "download-json") {
      const stamp = new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19);
      IA.downloadFile(`report-${stamp}.json`, JSON.stringify(reportData, null, 2), "application/json;charset=utf-8");
      // M6 修复：下载按钮也要有反馈，否则用户不知道是否触发
      flashToast(t("download_started"));
    } else if (action === "download-md") {
      const stamp = new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19);
      IA.downloadFile(`report-${stamp}.md`, reportAsMarkdown(reportData), "text/markdown;charset=utf-8");
      flashToast(t("download_started"));
    } else if (action === "copy-patch") {
      if (!reportData.patch) return;
      const ok = await IA.copyToClipboard(reportData.patch);
      flashToast(ok ? t("copied") : t("copy_failed"));
    }
  }

  function flashToast(message) {
    let toast = document.getElementById("toast");
    if (!toast) {
      toast = document.createElement("div");
      toast.id = "toast";
      toast.className = "toast";
      toast.setAttribute("role", "status");
      toast.setAttribute("aria-live", "polite");
      document.body.appendChild(toast);
    }
    toast.textContent = message;
    toast.classList.add("visible");
    clearTimeout(toast._timer);
    toast._timer = setTimeout(function () {
      toast.classList.remove("visible");
    }, 1600);
  }

  // ── 事件绑定 ────────────────────────────────────────
  function bindEvents() {
    document.getElementById("theme-toggle-btn").addEventListener("click", toggleTheme);
    document.getElementById("toggle-history-btn").addEventListener("click", toggleMobileHistory);
    document.getElementById("analyze-btn").addEventListener("click", analyze);
    document.getElementById("archive-toggle").addEventListener("click", toggleArchiveView);
    document.getElementById("history-search").addEventListener("input", scheduleHistorySearch);
    document.getElementById("back-button").addEventListener("click", goBack);
    document.getElementById("cancel-analysis").addEventListener("click", function () {
      IA.Runtime.cancelAnalysis();
    });
    document.getElementById("report-toggle").addEventListener("click", function () {
      toggleReport(true);
    });
    document.getElementById("report-close-btn").addEventListener("click", function () {
      toggleReport(false);
    });
    document.getElementById("chat-send-btn").addEventListener("click", function () {
      chat();
    });
    document.getElementById("chatInput").addEventListener("keydown", function (event) {
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        chat();
      }
    });
    document.getElementById("issueUrl").addEventListener("keydown", function (event) {
      if (event.key === "Enter") {
        event.preventDefault();
        analyze();
      }
    });
    document.getElementById("dialog-cancel-btn").addEventListener("click", closeSessionDialog);
    document.getElementById("session-dialog-form").addEventListener("submit", submitSessionDialog);

    // 事件委托：会话列表点击
    document.getElementById("history-list").addEventListener("click", function (event) {
      const card = event.target.closest(".session-card");
      if (!card) return;
      const id = card.dataset.sessionId;
      if (id) restoreSession(id);
    });

    // 事件委托：报告内 action 按钮
    document.getElementById("report").addEventListener("click", function (event) {
      const btn = event.target.closest("[data-action]");
      if (!btn) return;
      handleReportAction(btn.dataset.action, report);
    });

    // ESC 关闭报告（H7 修复：对话框打开时让原生 ESC 优先关闭对话框，不叠加关闭报告）
    document.addEventListener("keydown", function (event) {
      if (event.key !== "Escape") return;
      const dialog = document.getElementById("session-dialog");
      if (dialog && dialog.open) return;
      if (document.getElementById("main").classList.contains("report-open")) {
        toggleReport(false);
      }
    });

    // 对话框 ESC 关闭后清理状态
    document.getElementById("session-dialog").addEventListener("close", function () {
      dialogSession = null;
      dialogMode = null;
    });
  }

  // 暴露给运行时和外部
  IA.sessionId = null;
  IA.restoreSession = restoreSession;
  IA.addMsg = addMsg;
  IA.chat = chat;
  IA.analyze = analyze;
  IA.loadSessions = loadSessions;
  // 向后兼容：旧的 inline onclick 引用 window.chat / window.analyze
  window.chat = chat;
  window.analyze = analyze;
  window.restoreSession = restoreSession;
  window.loadSessions = loadSessions;
  window.addMsg = addMsg;

  document.addEventListener("DOMContentLoaded", function () {
    applyStoredTheme();
    IA.applyI18n(document);
    bindEvents();
    loadSessions();
  });

  // DOM 已就绪时立即加载；否则由 DOMContentLoaded 处理（保留向后兼容入口）
  if (document.readyState !== "loading") loadSessions();
})();
