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
  let chatAbortController = null;
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
    // 主题切换后，若报告面板已展开则仅刷新图表配色
    // 不重建报告 DOM（避免丢失滚动位置、焦点、hover 状态）
    if (report && document.getElementById("main").classList.contains("report-open")) {
      refreshReportCharts();
    }
  }

  // 仅刷新报告内 ECharts 图表实例，不重建整个 DOM
  // 用于主题切换等仅需更新配色的场景，保留 #report 滚动位置和用户当前 focus/hover 状态
  function refreshReportCharts() {
    disposeReportCharts();
    const sessionData = activeSession || {};
    const evidenceEl = document.getElementById("report-evidence-chart");
    if (evidenceEl) {
      const chart = renderEvidenceMatrix(evidenceEl, report, sessionData);
      if (chart) reportChartInstances.push(chart);
    }
    const confidenceEl = document.getElementById("report-confidence-chart");
    if (confidenceEl) {
      const chart = renderEvidenceSankey(confidenceEl, report, sessionData);
      if (chart) reportChartInstances.push(chart);
    }
    const funnelEl = document.getElementById("report-funnel-chart");
    if (funnelEl) {
      const chart = renderInvestigationFunnel(funnelEl, report, sessionData);
      if (chart) reportChartInstances.push(chart);
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
      // #5 修复：关闭 sidebar 后焦点移回主交互区
      const issueUrlEl = document.getElementById("issueUrl");
      if (issueUrlEl && issueUrlEl.offsetParent !== null) {
        issueUrlEl.focus();
      } else {
        const messages = document.getElementById("messages");
        if (messages) {
          messages.setAttribute("tabindex", "-1");
          messages.focus();
        }
      }
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
    // 骨架屏：仅在首次加载或刷新时显示，避免每次搜索都闪烁
    if (!list.querySelector(".session-group") && !list.querySelector(".history-skeleton")) {
      list.innerHTML =
        `<div class="history-skeleton" aria-hidden="true">` +
          `<div class="history-skeleton-row"><div class="skeleton-bar medium"></div><div class="skeleton-bar long"></div><div class="skeleton-bar short"></div></div>` +
          `<div class="history-skeleton-row"><div class="skeleton-bar medium"></div><div class="skeleton-bar long"></div><div class="skeleton-bar short"></div></div>` +
          `<div class="history-skeleton-row"><div class="skeleton-bar medium"></div><div class="skeleton-bar long"></div><div class="skeleton-bar short"></div></div>` +
        `</div>`;
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

  // #10 修复：key-based diff 渲染，避免每次 loadSessions 全量重建 DOM。
  // 复用已有 .session-row 节点（按 session_id 匹配），仅更新 title/time/active 类，
  // 保留用户当前 hover 状态和滚动位置。新增/消失的节点按最小变更原则插入/移除。
  function renderSessions(sessions) {
    const list = document.getElementById("history-list");
    const wasScrolling = list.scrollTop;
    const hadFocus = document.activeElement && list.contains(document.activeElement)
      ? document.activeElement.dataset.sessionId
      : null;

    if (!sessions.length) {
      const emptyKey = showArchived ? "history_empty_archive" : "history_empty_active";
      if (showArchived) {
        list.innerHTML = `<div class="history-empty">${t(emptyKey)}</div>`;
      } else {
        // 空状态 CTA：引导用户开始第一次分析
        list.innerHTML =
          `<div class="history-empty-cta">` +
            `<div class="history-empty-cta-icon">${IA.svgIcon("plus")}</div>` +
            `<div class="history-empty-cta-text">${IA.escapeHtml(t("history_empty_cta_text"))}</div>` +
            `<button class="history-empty-cta-btn" type="button" id="empty-cta-btn">${IA.svgIcon("plus")}<span>${IA.escapeHtml(t("history_empty_cta_btn"))}</span></button>` +
          `</div>`;
        const ctaBtn = document.getElementById("empty-cta-btn");
        if (ctaBtn) {
          ctaBtn.addEventListener("click", function () {
            const issueUrl = document.getElementById("issueUrl");
            if (issueUrl) {
              issueUrl.focus();
              issueUrl.scrollIntoView({ behavior: "smooth", block: "center" });
            }
          });
        }
      }
      return;
    }

    // 收集新数据按分组顺序的扁平列表
    const groups = new Map();
    sessions.forEach(function (session) {
      const group = session.status === "running" ? t("history_group_running") : historyGroup(session.updated_at);
      if (!groups.has(group)) groups.set(group, []);
      groups.get(group).push(session);
    });
    const orderedGroupNames = [
      t("history_group_running"),
      t("history_group_today"),
      t("history_group_week"),
      t("history_group_older"),
    ];

    // 第一渲染直接走全量构建路径
    if (!list.querySelector(".session-group")) {
      list.innerHTML = "";
      buildSessionList(list, orderedGroupNames, groups);
      if (hadFocus) {
        const restored = document.getElementById(SESSION_ROW_KEY + hadFocus);
        if (restored) {
          const card = restored.querySelector(".session-card");
          if (card) card.focus();
        }
      }
      return;
    }

    // 增量 diff：按 session_id 复用 row 节点，重建分组容器
    // 策略：清空 list 重新组装分组，但 row 节点从旧 DOM 复用（DOM 节点引用不变）
    const existingRows = new Map();
    list.querySelectorAll(".session-row").forEach(function (row) {
      existingRows.set(row.dataset.sessionId, row);
    });

    list.innerHTML = "";
    orderedGroupNames.forEach(function (groupName) {
      const items = groups.get(groupName);
      if (!items) return;
      const group = document.createElement("section");
      group.className = "session-group";
      const heading = document.createElement("div");
      heading.className = "session-group-title";
      heading.textContent = groupName;
      group.appendChild(heading);
      items.forEach(function (item) {
        const oldRow = existingRows.get(item.session_id);
        if (oldRow) {
          // 复用旧节点，仅更新动态字段
          updateSessionRow(oldRow, item);
          group.appendChild(oldRow);
        } else {
          group.appendChild(createSessionRow(item));
        }
      });
      list.appendChild(group);
    });

    // 恢复滚动位置和焦点
    list.scrollTop = wasScrolling;
    if (hadFocus) {
      const restored = document.getElementById(SESSION_ROW_KEY + hadFocus);
      if (restored) {
        const card = restored.querySelector(".session-card");
        if (card) card.focus();
      }
    }
  }

  function buildSessionList(list, orderedGroupNames, groups) {
    orderedGroupNames.forEach(function (groupName) {
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

  // #10 修复：仅更新 row 内动态字段，不重建 DOM，避免 hover 状态丢失
  function updateSessionRow(row, session) {
    const card = row.querySelector(".session-card");
    if (!card) return;
    row.classList.toggle("active", session.session_id === sessionId);
    const repository = session.owner && session.repo ? session.owner + "/" + session.repo : repositoryFromUrl(session.issue_url);
    const issue = session.issue_number ? " #" + session.issue_number : "";
    card.title = session.phase
      ? enumLabel("phase", session.phase)
      : session.status
        ? enumLabel("status", session.status)
        : "";
    const repoEl = card.querySelector(".session-repo span:last-child");
    if (repoEl) repoEl.textContent = repository + issue;
    const titleEl = card.querySelector(".session-title");
    if (titleEl) titleEl.textContent = session.title;
    const timeEl = card.querySelector(".session-time");
    if (timeEl) timeEl.textContent = IA.formatRelativeTime(session.updated_at);
    const dotEl = card.querySelector(".status-dot");
    if (dotEl) dotEl.className = "status-dot " + IA.safeClass(session.status);
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
      // #5 修复：移动端关闭 sidebar 后焦点原本留在隐藏的 session-card 上，
      // 主动移到主交互区，避免屏幕阅读器读屏焦点消失
      if (window.matchMedia("(max-width: 900px)").matches) {
        const chatInput = document.getElementById("chatInput");
        if (chatInput && chatInput.offsetParent !== null) {
          chatInput.focus();
        } else {
          const issueUrlEl = document.getElementById("issueUrl");
          if (issueUrlEl && issueUrlEl.offsetParent !== null) issueUrlEl.focus();
        }
      }
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

  // #3 修复：dialog 操作完成后焦点恢复到触发该操作的 session 行（或相邻行/搜索框）
  // 调用时机：loadSessions 完成后。返回 true 表示焦点已恢复。
  function restoreFocusToSessionRow(sessionIdToFocus) {
    if (!sessionIdToFocus) return false;
    const row = document.getElementById(SESSION_ROW_KEY + sessionIdToFocus);
    if (row) {
      const card = row.querySelector(".session-card");
      if (card) {
        card.focus();
        return true;
      }
    }
    // 触发行已被删除：focus 到列表中第一个可聚焦元素，避免焦点跌到 body
    const firstCard = document.querySelector(".session-card");
    if (firstCard) {
      firstCard.focus();
      return true;
    }
    const search = document.getElementById("history-search");
    if (search) {
      search.focus();
      return true;
    }
    return false;
  }

  async function submitSessionDialog(event) {
    event.preventDefault();
    if (!dialogSession) return;
    if (dialogMode === "delete") {
      const session = dialogSession;
      closeSessionDialog();
      await deleteSession(session);
      // #3 修复：delete 后触发行已不存在，focus 到相邻行保持键盘上下文
      restoreFocusToSessionRow(null);
      return;
    }
    const title = document.getElementById("dialog-input").value.trim();
    if (!title) return;
    if (title === dialogSession.title) {
      const focusId = dialogSession.session_id;
      closeSessionDialog();
      restoreFocusToSessionRow(focusId);
      return;
    }
    const focusId = dialogSession.session_id;
    try {
      await IA.apiJson("/session/" + encodeURIComponent(dialogSession.session_id), {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ display_title: title.trim() }),
      });
      closeSessionDialog();
      await loadSessions();
      // #3 修复：loadSessions 重建 DOM 后焦点恢复到重命名的那一行
      restoreFocusToSessionRow(focusId);
    } catch (error) {
      addMsg("error", error.message);
    }
  }

  async function archiveSession(session, archived) {
    const focusId = session && session.session_id;
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
      // #3 修复：归档/恢复后焦点回到原行（若仍在当前列表中）
      restoreFocusToSessionRow(focusId);
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
    const wasReportOpen = document.getElementById("main").classList.contains("report-open");
    // M7 修复：切到无 report 的会话时必须 dispose ECharts 实例，
    // 否则 canvas 绑定的事件监听不会释放，长期切换会累积内存泄漏
    disposeReportCharts();
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
    // #4 修复：若报告面板原本是展开的，焦点可能落在已被清空的 report 容器内，
    // 主动把焦点移回主交互区，避免键盘用户 Tab 落空
    if (wasReportOpen) {
      const issueUrl = document.getElementById("issueUrl");
      if (issueUrl && issueUrl.offsetParent !== null) {
        issueUrl.focus();
      } else {
        const messages = document.getElementById("messages");
        if (messages) {
          messages.setAttribute("tabindex", "-1");
          messages.focus();
        }
      }
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
    // 动态 threshold：视口高度的比例，避免大代码块/长表格时贴底用户被甩出
    const dynamicThreshold = Math.max(threshold || 80, (container.clientHeight || 400) * 0.15);
    const distance = container.scrollHeight - container.scrollTop - container.clientHeight;
    if (distance > dynamicThreshold) return;
    requestAnimationFrame(function () {
      container.scrollTop = container.scrollHeight;
    });
  }

  // Markdown 渲染：assistant 消息走 marked + DOMPurify + highlight.js，
  // 任意一个库加载失败则降级为 escapeHtml 纯文本，保证可用性
  function renderMarkdown(text) {
    if (typeof text !== "string") return "";
    if (!window.marked || !window.DOMPurify) {
      // 降级：保留换行，转义 HTML
      return IA.escapeHtml(text).replace(/\n/g, "<br>");
    }
    try {
      marked.setOptions({
        gfm: true,
        breaks: true,
        headerIds: false,
        mangle: false,
      });
      const rawHtml = marked.parse(text);
      const cleanHtml = window.DOMPurify.sanitize(rawHtml, {
        ALLOWED_TAGS: [
          "h1", "h2", "h3", "h4", "h5", "h6",
          "p", "br", "hr",
          "strong", "em", "del", "mark", "sub", "sup",
          "ul", "ol", "li",
          "blockquote", "code", "pre",
          "a", "span", "div",
          "table", "thead", "tbody", "tr", "th", "td",
          "img",
        ],
        ALLOWED_ATTR: ["href", "title", "src", "alt", "class", "target", "rel", "colspan", "rowspan"],
      });
      return cleanHtml;
    } catch (e) {
      return IA.escapeHtml(text).replace(/\n/g, "<br>");
    }
  }

  // 为 pre>code 块注入语言标签 + 复制按钮 + 语法高亮
  function enhanceCodeBlocks(container) {
    if (!container) return;
    const blocks = container.querySelectorAll("pre > code");
    blocks.forEach(function (codeEl, idx) {
      const pre = codeEl.parentElement;
      if (pre.dataset.enhanced) return;
      pre.dataset.enhanced = "1";

      // 语言标签
      const langMatch = codeEl.className.match(/language-([\w-]+)/);
      const lang = langMatch ? langMatch[1] : "text";
      const langLabel = document.createElement("span");
      langLabel.className = "code-lang-label";
      langLabel.textContent = lang;
      pre.appendChild(langLabel);

      // 复制按钮
      const copyBtn = document.createElement("button");
      copyBtn.type = "button";
      copyBtn.className = "code-copy-btn";
      copyBtn.setAttribute("aria-label", t("copy_code"));
      copyBtn.innerHTML = IA.svgIcon("copy");
      copyBtn.addEventListener("click", function () {
        const text = codeEl.textContent;
        IA.copyToClipboard(text).then(
          function () {
            copyBtn.classList.add("copied");
            copyBtn.innerHTML = IA.svgIcon("check");
            setTimeout(function () {
              copyBtn.classList.remove("copied");
              copyBtn.innerHTML = IA.svgIcon("copy");
            }, 1500);
          },
          function () { /* ignore */ },
        );
      });
      pre.appendChild(copyBtn);

      // 语法高亮（库加载失败则跳过，code 块仍可读）
      if (window.hljs) {
        try {
          window.hljs.highlightElement(codeEl);
        } catch (e) { /* ignore */ }
      }
    });
  }

  // textarea 自适应高度：内容增长时撑高，删除时收缩，限制最大高度避免撑满屏幕
  function autoResizeTextarea(el) {
    if (!el) return;
    el.style.height = "auto";
    const maxHeight = 160;
    const newHeight = Math.min(el.scrollHeight, maxHeight);
    el.style.height = newHeight + "px";
    el.style.overflowY = el.scrollHeight > maxHeight ? "auto" : "hidden";
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
    // assistant 消息走 Markdown 渲染（GFM + 代码高亮 + 复制按钮），
    // 其余角色保持 textContent 天然防 XSS
    if (role === "assistant") {
      const body = document.createElement("div");
      body.className = "msg-body markdown-body";
      body.innerHTML = renderMarkdown(content);
      enhanceCodeBlocks(body);
      m.appendChild(body);
      // action 按钮 + 时间戳
      appendMsgActions(m, content);
    } else {
      m.textContent = content;
    }
    d.appendChild(m);
    // H5 修复：仅在用户已在底部附近时自动滚动，避免打断阅读
    scrollToBottomIfNear(d, 80);
    return m;
  }

  // assistant 消息底部 action 行：复制全文 + 时间戳
  function appendMsgActions(msgEl, content) {
    const actions = document.createElement("div");
    actions.className = "msg-actions";

    // 复制全文按钮
    const copyBtn = document.createElement("button");
    copyBtn.type = "button";
    copyBtn.className = "msg-action-btn";
    copyBtn.setAttribute("aria-label", t("copy_text"));
    copyBtn.innerHTML = IA.svgIcon("copy");
    copyBtn.addEventListener("click", function () {
      IA.copyToClipboard(content).then(function (ok) {
        if (ok) {
          copyBtn.classList.add("copied");
          copyBtn.innerHTML = IA.svgIcon("check");
          setTimeout(function () {
            copyBtn.classList.remove("copied");
            copyBtn.innerHTML = IA.svgIcon("copy");
          }, 1500);
        }
      });
    });
    actions.appendChild(copyBtn);

    // 时间戳
    const timestamp = document.createElement("span");
    timestamp.className = "msg-timestamp";
    const now = new Date();
    timestamp.textContent = now.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    actions.appendChild(timestamp);

    msgEl.appendChild(actions);
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
    // #2 修复：用 button 元素让键盘用户也能展开/折叠工具结果
    const m = document.createElement("button");
    m.type = "button";
    m.className = "msg tool";
    m.setAttribute("data-tool-name", IA.safeClass(name));
    m.setAttribute("aria-expanded", "false");
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
      const expanded = m.classList.toggle("expanded");
      m.setAttribute("aria-expanded", String(expanded));
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

  // ── 图表 1：证据可信度矩阵（Heatmap） ──────────────────────
  // 回答"这个分析的可信度到底如何"：每条证据在 4 个维度上的通过/未通过状态。
  // 绿色 = 通过，红色 = 未通过。用户一眼看出哪些证据扎实、哪些是凑数。
  function renderEvidenceMatrix(container, report, sessionData) {
    if (!container) return null;
    if (!echartsAvailable()) {
      container.innerHTML = `<div class="report-chart-fallback">${IA.escapeHtml(t("chart_load_failed"))}</div>`;
      return null;
    }
    const palette = getEchartsPalette();
    const evidence = report.evidence || [];
    if (!evidence.length) {
      container.innerHTML = `<div class="report-chart-empty">${IA.escapeHtml(t("report_evidence_chart_empty"))}</div>`;
      return null;
    }

    // 从 session 数据提取已读文件列表和行数信息
    const filesRead = (sessionData && sessionData.files_read) || report.files_examined || [];
    const filesReadSet = new Set(filesRead);
    // 审查状态：approved = 审查通过
    const reviewPassed = report.review_audit && report.review_audit.status === "approved";

    // 4 个验证维度
    const dimensions = [
      t("matrix_dim_file_read"),
      t("matrix_dim_lines_valid"),
      t("matrix_dim_has_reason"),
      t("matrix_dim_review_verified"),
    ];

    // 构造 heatmap 数据：[x, y, value]
    // value: 1 = 通过, 0 = 未通过
    const heatData = [];
    const fileLabels = evidence.map(function (e, i) {
      // 文件名缩短显示
      const path = e.path || "unknown";
      const parts = path.split("/");
      return parts.length > 2 ? "…/" + parts.slice(-2).join("/") : path;
    });

    evidence.forEach(function (e, i) {
      // 维度 0：文件是否被实际读取
      const fileRead = filesReadSet.has(e.path) ? 1 : 0;
      heatData.push([i, 0, fileRead]);

      // 维度 1：行号是否有效（非空且格式正确）
      const linesValid = e.lines && /^L\d+(-L?\d+)?$/.test(e.lines) ? 1 : 0;
      heatData.push([i, 1, linesValid]);

      // 维度 2：是否有 reason 说明
      const hasReason = e.reason && e.reason.trim() ? 1 : 0;
      heatData.push([i, 2, hasReason]);

      // 维度 3：是否被独立审查验证
      heatData.push([i, 3, reviewPassed ? 1 : 0]);
    });

    const tooltipBg = palette.text === "#f1f5f9" ? "#0f172a" : "#ffffff";
    const chart = echarts.init(container);
    chart.setOption({
      tooltip: {
        confine: true,
        backgroundColor: tooltipBg,
        borderWidth: 0,
        padding: [10, 14],
        textStyle: { color: palette.text, fontSize: 12 },
        formatter: function (params) {
          const e = evidence[params.data[0]];
          const dim = dimensions[params.data[1]];
          const passed = params.data[2] === 1;
          const status = passed ? t("matrix_pass") : t("matrix_fail");
          const statusColor = passed ? palette.success : palette.danger;
          return `<div style="font-weight:600;margin-bottom:4px;">${IA.escapeHtml(e.path)}</div>` +
            `<div style="color:${palette.textDim};font-size:11px;margin-bottom:4px;">${IA.escapeHtml(dim)}</div>` +
            `<div style="color:${statusColor};font-weight:600;">${IA.escapeHtml(status)}</div>` +
            (e.lines ? `<div style="color:${palette.textDim};font-size:11px;margin-top:2px;">${IA.escapeHtml(e.lines)}</div>` : "");
        },
      },
      grid: { left: 8, right: 16, top: 8, bottom: 60, containLabel: true },
      xAxis: {
        type: "category",
        data: fileLabels,
        splitArea: { show: true },
        axisLabel: {
          color: palette.textDim,
          fontSize: 10,
          rotate: 30,
          width: 80,
          overflow: "truncate",
        },
        axisLine: { lineStyle: { color: palette.line } },
        axisTick: { show: false },
      },
      yAxis: {
        type: "category",
        data: dimensions,
        splitArea: { show: true },
        axisLabel: {
          color: palette.textDim,
          fontSize: 11,
          width: 100,
          overflow: "truncate",
        },
        axisLine: { lineStyle: { color: palette.line } },
        axisTick: { show: false },
      },
      visualMap: {
        min: 0,
        max: 1,
        show: false,
        inRange: { color: [palette.danger, palette.success] },
      },
      series: [
        {
          type: "heatmap",
          data: heatData,
          itemStyle: { borderRadius: 3, borderColor: palette.text === "#f1f5f9" ? "#1e293b" : "#ffffff", borderWidth: 2 },
          emphasis: { itemStyle: { shadowBlur: 8, shadowColor: "rgba(0,0,0,0.3)" } },
          label: { show: false },
        },
      ],
    });
    return chart;
  }

  // ── 图表 2：证据-根因支撑关系图（Sankey） ──────────────────────
  // 回答"结论是怎么推导出来的"：issue → 根因 → 证据的流向，
  // 流量粗细表示支撑强度（强支撑=有效行号+已读，弱支撑=仅有 reason）
  function renderEvidenceSankey(container, report, sessionData) {
    if (!container) return null;
    if (!echartsAvailable()) {
      container.innerHTML = `<div class="report-chart-fallback">${IA.escapeHtml(t("chart_load_failed"))}</div>`;
      return null;
    }
    const palette = getEchartsPalette();
    const evidence = report.evidence || [];
    if (!evidence.length) {
      container.innerHTML = `<div class="report-chart-empty">${IA.escapeHtml(t("report_evidence_chart_empty"))}</div>`;
      return null;
    }

    const filesRead = (sessionData && sessionData.files_read) || report.files_examined || [];
    const filesReadSet = new Set(filesRead);

    // 从 root_cause 提取关键短语作为中间节点（按句号/分号拆分，取前 2 段）
    const causeText = report.root_cause || t("sankey_default_cause");
    const causeParts = causeText.split(/[。.；;]/).filter(function (s) { return s.trim(); });
    const causeNodes = causeParts.slice(0, 2).map(function (s, i) {
      const trimmed = s.trim();
      // 缩短为 40 字符
      return trimmed.length > 40 ? trimmed.substring(0, 40) + "…" : trimmed;
    });
    if (!causeNodes.length) causeNodes.push(t("sankey_default_cause"));

    // 构造 Sankey 节点
    const nodes = [];
    // 左侧：issue
    nodes.push({ name: t("sankey_issue_node"), itemStyle: { color: palette.primary } });
    // 中间：根因论点
    causeNodes.forEach(function (c) {
      nodes.push({ name: c, itemStyle: { color: palette.warning } });
    });
    // 右侧：证据文件
    const fileNames = evidence.map(function (e) {
      const path = e.path || "unknown";
      const parts = path.split("/");
      return parts.length > 2 ? "…/" + parts.slice(-2).join("/") : path;
    });
    fileNames.forEach(function (f, i) {
      const e = evidence[i];
      const isStrong = filesReadSet.has(e.path) && e.lines && /^L\d+/.test(e.lines);
      nodes.push({ name: f, itemStyle: { color: isStrong ? palette.success : palette.textDim } });
    });

    // 构造连线
    const links = [];
    // issue → 每个根因论点
    causeNodes.forEach(function (c) {
      links.push({ source: t("sankey_issue_node"), target: c, value: 1 });
    });
    // 根因论点 → 证据（轮流分配到各论点，避免单点过载）
    evidence.forEach(function (e, i) {
      const targetCause = causeNodes[i % causeNodes.length];
      const fileName = fileNames[i];
      const isStrong = filesReadSet.has(e.path) && e.lines && /^L\d+/.test(e.lines);
      links.push({ source: targetCause, target: fileName, value: isStrong ? 2 : 1 });
    });

    const tooltipBg = palette.text === "#f1f5f9" ? "#0f172a" : "#ffffff";
    const chart = echarts.init(container);
    chart.setOption({
      tooltip: {
        confine: true,
        backgroundColor: tooltipBg,
        borderWidth: 0,
        padding: [10, 14],
        textStyle: { color: palette.text, fontSize: 12 },
        formatter: function (params) {
          if (params.dataType === "edge") {
            const strong = params.data.value >= 2;
            const label = strong ? t("sankey_strong_support") : t("sankey_weak_support");
            const color = strong ? palette.success : palette.textDim;
            return `<div style="font-weight:600;">${IA.escapeHtml(params.data.source)} → ${IA.escapeHtml(params.data.target)}</div>` +
              `<div style="color:${color};font-size:11px;margin-top:2px;">${IA.escapeHtml(label)}</div>`;
          }
          return `<div style="font-weight:600;">${IA.escapeHtml(params.name)}</div>`;
        },
      },
      series: [
        {
          type: "sankey",
          data: nodes,
          links: links,
          orient: "horizontal",
          left: 16,
          right: 80,
          top: 16,
          bottom: 16,
          nodeWidth: 14,
          nodeGap: 10,
          nodeAlign: "justify",
          layoutIterations: 32,
          label: {
            color: palette.text,
            fontSize: 11,
            fontWeight: 500,
          },
          lineStyle: {
            color: "gradient",
            curveness: 0.5,
            opacity: 0.5,
          },
          emphasis: {
            focus: "adjacency",
            lineStyle: { opacity: 0.8 },
          },
        },
      ],
    });
    return chart;
  }

  // ── 图表 3：调查过程效率漏斗（Funnel） ──────────────────────
  // 回答"这次分析花了多少步、效率如何"：模型调用 → 工具调用 → 文件读取 → 有效证据。
  // 每层宽度按比例收缩，hover 显示转化率。
  function renderInvestigationFunnel(container, report, sessionData) {
    if (!container) return null;
    if (!echartsAvailable()) {
      container.innerHTML = `<div class="report-chart-fallback">${IA.escapeHtml(t("chart_load_failed"))}</div>`;
      return null;
    }
    const palette = getEchartsPalette();
    const metrics = (sessionData && sessionData.metrics) || {};

    const modelCalls = parseInt(metrics.model_calls, 10) || 0;
    const toolCalls = parseInt(metrics.tool_calls, 10) || 0;
    const filesRead = (sessionData && sessionData.files_read ? sessionData.files_read.length : (report.files_examined || []).length) || 0;
    const validEvidence = report.evidence_audit ? report.evidence_audit.valid_references : (report.evidence || []).length;

    // 如果所有值都是 0，展示空状态
    if (!modelCalls && !toolCalls && !filesRead && !validEvidence) {
      container.innerHTML = `<div class="report-chart-empty">${IA.escapeHtml(t("funnel_empty"))}</div>`;
      return null;
    }

    const data = [
      { name: t("funnel_model_calls"), value: Math.max(modelCalls, 1), raw: modelCalls, color: palette.primary },
      { name: t("funnel_tool_calls"), value: Math.max(toolCalls, 1), raw: toolCalls, color: palette.warning },
      { name: t("funnel_files_read"), value: Math.max(filesRead, 1), raw: filesRead, color: palette.success },
      { name: t("funnel_valid_evidence"), value: Math.max(validEvidence, 1), raw: validEvidence, color: palette.danger },
    ];

    const tooltipBg = palette.text === "#f1f5f9" ? "#0f172a" : "#ffffff";
    const chart = echarts.init(container);
    chart.setOption({
      tooltip: {
        confine: true,
        backgroundColor: tooltipBg,
        borderWidth: 0,
        padding: [10, 14],
        textStyle: { color: palette.text, fontSize: 12 },
        formatter: function (params) {
          const idx = params.dataIndex;
          const raw = data[idx].raw;
          const prevRaw = idx > 0 ? data[idx - 1].raw : 0;
          const conversionRate = prevRaw > 0 ? ((raw / prevRaw) * 100).toFixed(1) : "100";
          const overallRate = modelCalls > 0 ? ((raw / modelCalls) * 100).toFixed(1) : "100";
          return `<div style="font-weight:600;margin-bottom:4px;">${IA.escapeHtml(params.name)}</div>` +
            `<div>${IA.escapeHtml(t("funnel_count"))}: <b>${raw}</b></div>` +
            (idx > 0 ? `<div style="color:${palette.textDim};font-size:11px;">${IA.escapeHtml(t("funnel_conversion"))}: ${conversionRate}%</div>` : "") +
            `<div style="color:${palette.textDim};font-size:11px;">${IA.escapeHtml(t("funnel_overall"))}: ${overallRate}%</div>`;
        },
      },
      series: [
        {
          type: "funnel",
          data: data.map(function (d) {
            return { name: d.name, value: d.value, itemStyle: { color: d.color } };
          }),
          left: "10%",
          right: "10%",
          top: 16,
          bottom: 16,
          width: "80%",
          minSize: "20%",
          maxSize: "100%",
          sort: "descending",
          gap: 4,
          label: {
            show: true,
            color: palette.text,
            fontSize: 11,
            fontWeight: 600,
            formatter: function (params) {
              const idx = params.dataIndex;
              return params.name + ": " + data[idx].raw;
            },
          },
          labelLine: { show: false },
          itemStyle: {
            borderWidth: 0,
            borderRadius: 2,
          },
          emphasis: {
            itemStyle: { shadowBlur: 8, shadowColor: "rgba(0,0,0,0.3)" },
          },
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

    // 3. ECharts 三图表：证据可信度矩阵 + 证据-根因支撑图 + 调查效率漏斗
    // 每个图表回答一个用户真正会问的问题，而非堆砌数量统计
    const hasEvidence = r.evidence && r.evidence.length;
    const sessionData = activeSession || {};
    if (hasEvidence) {
      // 图表 1 + 图表 2 并排
      const matrixHtml =
        `<div class="report-chart report-chart-half">` +
        `<div class="report-chart-title">${IA.escapeHtml(t("matrix_chart_title"))}</div>` +
        `<div id="report-evidence-chart" class="report-chart-canvas report-chart-canvas-tall"></div>` +
        `<div class="report-chart-caption">${IA.escapeHtml(t("matrix_chart_caption"))}</div>` +
        `</div>`;
      const sankeyHtml =
        `<div class="report-chart report-chart-half">` +
        `<div class="report-chart-title">${IA.escapeHtml(t("sankey_chart_title"))}</div>` +
        `<div id="report-confidence-chart" class="report-chart-canvas report-chart-canvas-tall"></div>` +
        `<div class="report-chart-caption">${IA.escapeHtml(t("sankey_chart_caption"))}</div>` +
        `</div>`;
      parts.push(`<div class="report-charts-row">${matrixHtml}${sankeyHtml}</div>`);
      // 图表 3 单独一行（漏斗图居中显示）
      const funnelHtml =
        `<div class="report-chart report-chart-full">` +
        `<div class="report-chart-title">${IA.escapeHtml(t("funnel_chart_title"))}</div>` +
        `<div id="report-funnel-chart" class="report-chart-canvas"></div>` +
        `<div class="report-chart-caption">${IA.escapeHtml(t("funnel_chart_caption"))}</div>` +
        `</div>`;
      parts.push(`<div class="report-charts-row">${funnelHtml}</div>`);
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
      const chart = renderEvidenceMatrix(evidenceEl, r, sessionData);
      if (chart) reportChartInstances.push(chart);
    }
    const confidenceEl = document.getElementById("report-confidence-chart");
    if (confidenceEl) {
      const chart = renderEvidenceSankey(confidenceEl, r, sessionData);
      if (chart) reportChartInstances.push(chart);
    }
    const funnelEl = document.getElementById("report-funnel-chart");
    if (funnelEl) {
      const chart = renderInvestigationFunnel(funnelEl, r, sessionData);
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
    if (!msg || !sessionId) {
      return;
    }
    // H1 修复：捕获当前会话 ID。catch/finally 中若 sessionId 已变（用户切换了会话），
    // 不向新视图写入"已停止生成"等脏数据，也不操作新视图的输入框/progress
    const chatSessionId = sessionId;
    chatInProgress = true;
    // M3 修复：重试（messageOverride）时不清空用户可能已输入的新文本
    if (messageOverride === undefined) {
      inp.value = "";
      autoResizeTextarea(inp);
    }
    setChatDisabled(true);
    // 显示停止生成按钮
    const stopBtn = document.getElementById("chat-stop-btn");
    if (stopBtn) stopBtn.style.display = "inline-flex";
    addMsg("user", msg);
    lastFailedChat = null;
    // Thinking dots + 骨架屏消息：让等待反馈接近主流 agent
    const progressEl = document.getElementById("progress");
    progressEl.innerHTML = `<span class="thinking-dots"><span></span></span>${IA.escapeHtml(t("thinking"))}`;
    const messagesEl = document.getElementById("messages");
    const skeletonMsg = document.createElement("div");
    skeletonMsg.className = "msg assistant msg-skeleton";
    skeletonMsg.setAttribute("aria-hidden", "true");
    skeletonMsg.innerHTML =
      `<div class="skeleton-bar long"></div>` +
      `<div class="skeleton-bar medium"></div>` +
      `<div class="skeleton-bar long"></div>` +
      `<div class="skeleton-bar short"></div>`;
    messagesEl.appendChild(skeletonMsg);
    scrollToBottomIfNear(messagesEl, 80);
    // AbortController：让用户能中断长时间等待
    chatAbortController = new AbortController();
    let stopped = false;
    const onStop = function () {
      stopped = true;
      if (chatAbortController) {
        try { chatAbortController.abort(); } catch (e) { /* ignore */ }
      }
    };
    if (stopBtn) stopBtn.addEventListener("click", onStop, { once: true });

    // assistant 消息容器：首个 delta 抵达时 lazy 创建（替换骨架屏）
    let assistantMsg = null;
    let assistantBody = null;
    let assistantContent = "";
    let toolsUsed = [];
    let finalized = false;

    function ensureAssistantMsg() {
      if (assistantMsg) return;
      if (skeletonMsg.parentNode) skeletonMsg.remove();
      assistantMsg = document.createElement("div");
      assistantMsg.className = "msg assistant";
      assistantMsg.dataset.speaker = t("speaker_agent");
      assistantBody = document.createElement("div");
      assistantBody.className = "msg-body markdown-body";
      assistantMsg.appendChild(assistantBody);
      // 打字光标：持久节点，不随 delta 重建
      const cursor = document.createElement("span");
      cursor.className = "typewriter-cursor";
      cursor.setAttribute("aria-hidden", "true");
      assistantBody.appendChild(cursor);
      messagesEl.appendChild(assistantMsg);
      scrollToBottomIfNear(messagesEl, 80);
    }

    function renderAssistantContent() {
      if (!assistantBody) return;
      assistantBody.innerHTML = renderMarkdown(assistantContent);
      // 重新追加持久光标
      const cursor = document.createElement("span");
      cursor.className = "typewriter-cursor";
      cursor.setAttribute("aria-hidden", "true");
      assistantBody.appendChild(cursor);
      scrollToBottomIfNear(messagesEl, 80);
    }

    // H3 修复：rAF 节流 — 多个 delta 在同一帧内合并为一次 markdown 渲染，
    // 避免高频 delta 下 marked.parse + DOMPurify.sanitize + innerHTML 的 O(N²) 开销
    let pendingRender = false;
    function scheduleRender() {
      if (pendingRender) return;
      pendingRender = true;
      requestAnimationFrame(function () {
        pendingRender = false;
        // H1 守卫：会话已切换时不渲染
        if (chatSessionId !== sessionId) return;
        renderAssistantContent();
      });
    }

    function appendDelta(text) {
      ensureAssistantMsg();
      assistantContent += text;
      scheduleRender();
    }

    function finalizeAssistant() {
      if (finalized) return;
      finalized = true;
      // 刷新尚未渲染的 delta
      if (pendingRender) {
        pendingRender = false;
        renderAssistantContent();
      }
      if (!assistantMsg) {
        ensureAssistantMsg();
      }
      assistantBody.innerHTML = renderMarkdown(assistantContent);
      enhanceCodeBlocks(assistantBody);
      appendMsgActions(assistantMsg, assistantContent);
      scrollToBottomIfNear(messagesEl, 80);
    }

    // M1 修复：chat 看门狗 — 30s 无 delta 提示"连接变慢"，90s 提示"连接可能已断开"
    let lastEventTime = Date.now();
    let watchdogTimer = setInterval(function () {
      if (chatSessionId !== sessionId) { clearInterval(watchdogTimer); return; }
      const elapsed = Date.now() - lastEventTime;
      if (elapsed > 90000) {
        progressEl.textContent = t("connection_stalled");
        clearInterval(watchdogTimer);
      } else if (elapsed > 30000) {
        progressEl.textContent = t("connection_slow");
      }
    }, 5000);
    function resetWatchdog() {
      lastEventTime = Date.now();
      if (progressEl.textContent === t("connection_slow") || progressEl.textContent === t("connection_stalled")) {
        progressEl.textContent = "";
      }
    }

    // firstEventReceived 需在 try 块外声明，catch 块也要访问它判断是否清理 progressEl
    let firstEventReceived = false;
    try {
      const resp = await fetch("/chat/stream", {
        method: "POST",
        headers: { "Content-Type": "application/json", "Accept": "text/event-stream" },
        body: JSON.stringify({ session_id: sessionId, message: msg }),
        signal: chatAbortController.signal,
      });
      if (!resp.ok) {
        let detail = "";
        try { detail = (await resp.json()).detail || ""; } catch (e) { /* ignore */ }
        throw new Error(detail || t("error_unable_to_continue"));
      }
      // SSE 事件处理：解析单个 data: 事件并分发到 delta/tool_call/done/error
      function processSseEvent(event) {
        resetWatchdog();
        if (!firstEventReceived) {
          firstEventReceived = true;
          progressEl.textContent = "";
        }
        if (event.type === "delta") {
          appendDelta(event.content || "");
        } else if (event.type === "tool_call") {
          if (toolsUsed.indexOf(event.name) === -1) toolsUsed.push(event.name);
          const toolHint = document.createElement("div");
          toolHint.className = "msg system tool-hint";
          toolHint.textContent = t("tool_call_label") + ": " + event.name;
          messagesEl.appendChild(toolHint);
          scrollToBottomIfNear(messagesEl, 80);
        } else if (event.type === "done") {
          if (event.tools_used && event.tools_used.length) {
            toolsUsed = event.tools_used;
          }
          if (event.reply && !assistantContent) {
            assistantContent = event.reply;
          }
          finalizeAssistant();
          if (toolsUsed.length) {
            addMsg("system", t("tools_used_label") + toolsUsed.join(", "));
          }
          loadSessions();
        } else if (event.type === "error") {
          throw new Error(event.message || t("error_unable_to_continue"));
        }
      }

      // 从 buffer 中解析所有完整 SSE 事件（以 \n\n 分隔）
      function drainBuffer(buf) {
        let remaining = buf;
        let sepIdx;
        while ((sepIdx = remaining.indexOf("\n\n")) !== -1) {
          const rawEvent = remaining.slice(0, sepIdx);
          remaining = remaining.slice(sepIdx + 2);
          const dataLines = rawEvent
            .split("\n")
            .filter(function (line) { return line.startsWith("data:"); })
            .map(function (line) { return line.slice(5).replace(/^ /, ""); });
          if (!dataLines.length) continue;
          const dataStr = dataLines.join("\n");
          let event;
          try {
            event = JSON.parse(dataStr);
          } catch (e) {
            continue;
          }
          processSseEvent(event);
        }
        return remaining;
      }

      // SSE 流式解析：fetch + ReadableStream + TextDecoder
      // 不用 EventSource，因为 EventSource 不支持 POST + body
      // 降级路径：resp.body 为 null 时（部分环境不支持 streaming）用 resp.text() 一次性处理
      if (!resp.body) {
        const fullText = await resp.text();
        drainBuffer(fullText + "\n\n");
        if (!finalized && assistantContent) finalizeAssistant();
      } else {
        const reader = resp.body.getReader();
        const decoder = new TextDecoder("utf-8");
        let buffer = "";
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          buffer = drainBuffer(buffer);
        }
        // 流结束但未收到 done 事件（网络中断等兜底）
        if (!finalized && assistantContent) {
          finalizeAssistant();
        }
      }
    } catch (e) {
      // H1 守卫：会话已切换时不向新视图写入任何 UI 副作用
      if (chatSessionId !== sessionId) return;
      if (skeletonMsg.parentNode) skeletonMsg.remove();
      if (!firstEventReceived) progressEl.textContent = "";
      if (stopped || e.name === "AbortError") {
        // 用户主动停止：保留已收到的部分内容
        if (assistantContent) {
          finalizeAssistant();
        }
        addMsg("system", t("chat_stopped"));
      } else {
        // 其他错误：保留已收到的部分内容，再追加错误提示
        if (assistantContent) {
          finalizeAssistant();
        }
        lastFailedChat = msg;
        addErrorWithRetry(t("error_prefix") + e.message);
      }
    } finally {
      if (watchdogTimer) clearInterval(watchdogTimer);
      // 刷新尚未渲染的剩余 delta
      if (pendingRender && chatSessionId === sessionId) {
        pendingRender = false;
        renderAssistantContent();
      }
      chatInProgress = false;
      chatAbortController = null;
      if (stopBtn) {
        stopBtn.style.display = "none";
        stopBtn.removeEventListener("click", onStop);
      }
      // H1 守卫：会话已切换时不操作新视图的输入框
      if (chatSessionId !== sessionId) return;
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
        // M2 修复：chat 进行中时不销毁重试按钮，避免消息丢失且 UI 无反馈
        if (chatInProgress) return;
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
    const chatInputEl = document.getElementById("chatInput");
    // textarea 自适应高度：输入/粘贴/删除时撑高或收缩
    chatInputEl.addEventListener("input", function () {
      autoResizeTextarea(chatInputEl);
    });
    chatInputEl.addEventListener("keydown", function (event) {
      // Enter 发送 / Shift+Enter 换行（textarea 原生支持换行）
      if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
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

    // 全局快捷键：Cmd/Ctrl+K 聚焦搜索，Cmd/Ctrl+Enter 发送 chat
    document.addEventListener("keydown", function (event) {
      const mod = event.ctrlKey || event.metaKey;
      if (!mod) return;
      if (event.key === "k" || event.key === "K") {
        event.preventDefault();
        const search = document.getElementById("history-search");
        if (search) {
          search.focus();
          search.select();
        }
      } else if (event.key === "Enter") {
        // Cmd/Ctrl+Enter 在任何位置都触发发送（仅当 chat 可用时）
        const inputBar = document.getElementById("input-bar");
        if (inputBar && inputBar.style.display !== "none" && !chatInProgress) {
          event.preventDefault();
          chat();
        }
      }
    });

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
