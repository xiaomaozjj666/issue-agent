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
    card.title = session.phase ? session.phase.replace(/_/g, " ") : session.status;
    card.innerHTML =
      `<div class="session-repo"><span class="status-dot ${IA.safeClass(session.status)}" aria-hidden="true"></span><span>${IA.escapeHtml(repository + issue)}</span></div>` +
      `<div class="session-title">${IA.escapeHtml(session.title)}</div>` +
      `<div class="session-time">${IA.escapeHtml(IA.formatRelativeTime(session.updated_at))}</div>`;
    row.appendChild(card);

    const actions = document.createElement("div");
    actions.className = "session-actions";
    actions.appendChild(sessionAction("rename", t("dialog_rename_title").split(" ")[0] || "Rename", function () {
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
    return match ? match[1] + "/" + match[2] : "GitHub Issue";
  }

  async function restoreSession(id, recordHistory) {
    if (recordHistory === undefined) recordHistory = true;
    const messagesContainer = document.getElementById("messages");
    messagesContainer.innerHTML = `<div class="history-loading">${IA.escapeHtml(t("loading_session"))}</div>`;
    try {
      const session = await IA.apiJson("/session/" + encodeURIComponent(id));
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
      addMsg("system", t("welcome"));
    }
    updateBackButton();
  }

  function addMsg(role, content, cls) {
    const d = document.getElementById("messages");
    const m = document.createElement("div");
    m.className = "msg " + role + (cls ? " " + cls : "");
    if (role === "user") m.dataset.speaker = t("speaker_you");
    if (role === "assistant") m.dataset.speaker = t("speaker_agent");
    m.textContent = content;
    d.appendChild(m);
    // requestAnimationFrame 避免同步回流
    requestAnimationFrame(function () {
      d.scrollTop = d.scrollHeight;
    });
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
    requestAnimationFrame(function () {
      container.scrollTop = container.scrollHeight;
    });
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
    requestAnimationFrame(function () {
      d.scrollTop = d.scrollHeight;
    });
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

  async function analyze() {
    const url = document.getElementById("issueUrl").value.trim();
    if (!url) return;
    if (sessionId) navigationStack.push(sessionId);
    sessionId = null;
    IA.sessionId = null;
    report = null;
    activeSession = null;
    resetWorkspace(false);
    document.getElementById("progress").textContent = t("fetching");
    addMsg("assistant", t("analyzing_prefix") + url);

    try {
      const resp = await fetch("/stream", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ issue_url: url }),
      });
      if (!resp.ok) {
        let detail = "Unable to start analysis";
        try {
          detail = (await resp.json()).detail || detail;
        } catch (e) {
          /* ignore */
        }
        throw new Error(detail);
      }
      currentStream = resp.body.getReader();
      const decoder = new TextDecoder();
      let buf = "";
      let toolCard = null;

      while (true) {
        const { value, done } = await currentStream.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        const lines = buf.split("\n");
        buf = lines.pop() || "";
        for (const line of lines) {
          if (!line.startsWith("data: ")) continue;
          const data = line.slice(6);
          if (data === "[DONE]") {
            document.getElementById("progress").textContent = t("done");
            break;
          }
          try {
            const evt = JSON.parse(data);
            await handleStreamEvent(evt, { getToolCard: () => toolCard, setToolCard: (c) => (toolCard = c) });
          } catch (e) {
            console.warn("Ignored malformed stream event", e);
          }
        }
      }
    } catch (e) {
      addMsg("error", t("connection_error") + e.message);
      document.getElementById("progress").textContent = "";
      IA.Runtime.setCancelVisible(false);
    } finally {
      currentStream = null;
    }
  }

  async function handleStreamEvent(evt, toolCardRef) {
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
        document.getElementById("progress").textContent = evt.data.label || evt.data.phase;
        break;
      case "start":
        document.getElementById("progress").textContent = t("exploring_files", { count: evt.data.file_count });
        if (evt.data.title) document.querySelector(".conversation-label").textContent = evt.data.title;
        break;
      case "tool_call": {
        document.getElementById("progress").textContent =
          evt.data.name + ": " + (() => {
            try {
              return JSON.stringify(evt.data.args).substring(0, 60);
            } catch (e) {
              return "";
            }
          })();
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
      case "review":
        document.getElementById("progress").textContent = t("review_progress", { status: evt.data.status });
        break;
      case "report":
        report = evt.data;
        activeSession = activeSession || {};
        renderReport(report);
        document.getElementById("input-bar").style.display = "flex";
        document.getElementById("report-toggle").style.display = "inline-flex";
        document.getElementById("progress").textContent = "";
        addReportPreview(report);
        document.getElementById("chatInput").focus();
        loadSessions();
        break;
      case "error":
        addMsg("error", evt.message || t("error_prefix").trim());
        document.getElementById("progress").textContent = "";
        loadSessions();
        break;
      case "cancelled":
        addMsg("system", t("cancelled_message"));
        document.getElementById("progress").textContent = "";
        IA.Runtime.setCancelVisible(false);
        loadSessions();
        break;
      case "done":
        IA.Runtime.setCancelVisible(false);
        loadSessions();
        break;
      default:
        break;
    }
  }

  function renderReport(r) {
    const d = document.getElementById("report");
    const parts = [];
    const toc = [];

    function pushSection(id, title, bodyHtml) {
      toc.push(`<li><a href="#${id}">${IA.escapeHtml(title)}</a></li>`);
      return `<section class="report-section" id="${id}"><h4>${IA.escapeHtml(title)}</h4>${bodyHtml}</section>`;
    }

    parts.push(`<h3>${IA.escapeHtml(r.summary)}</h3>`);
    const confClass = IA.safeClass(r.confidence);
    parts.push(
      `<div class="report-meta"><span>${IA.escapeHtml(t("report_confidence"))}</span><span class="badge ${confClass}">${IA.escapeHtml(enumLabel("confidence", r.confidence))}</span></div>`,
    );

    // 报告工具栏：复制 JSON、下载 JSON、下载 Markdown
    parts.push(
      `<div class="report-toolbar">` +
        `<button class="report-action" type="button" data-action="copy-json">${IA.svgIcon("copy")}<span>${IA.escapeHtml(t("copy_button"))}</span></button>` +
        `<button class="report-action" type="button" data-action="download-json">${IA.svgIcon("download")}<span>${IA.escapeHtml(t("download_json"))}</span></button>` +
        `<button class="report-action" type="button" data-action="download-md">${IA.svgIcon("download")}<span>${IA.escapeHtml(t("download_markdown"))}</span></button>` +
        `</div>`,
    );

    const review = r.review_audit || { status: "not_run", summary: "", findings: [] };
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

    parts.push(pushSection("report-root", t("report_root_cause"), `<p>${IA.escapeHtml(r.root_cause)}</p>`));

    if (r.evidence && r.evidence.length) {
      const items = r.evidence
        .map(function (e, idx) {
          const ghUrl = IA.buildGitHubUrl(activeSession, e.path, e.lines);
          const linkHtml = ghUrl
            ? ` <a class="evidence-link" href="${IA.escapeAttr(ghUrl)}" target="_blank" rel="noopener noreferrer" title="${IA.escapeAttr(t("view_source"))}">${IA.svgIcon("external")}<span class="sr-only">${IA.escapeHtml(t("view_source"))}</span></a>`
            : "";
          return `<div class="evidence-item"><div class="evidence-path">${IA.escapeHtml(e.path)} · ${IA.escapeHtml(e.lines || "")}${linkHtml}</div><p>${IA.escapeHtml(e.reason || "")}</p></div>`;
        })
        .join("");
      parts.push(pushSection("report-evidence", t("report_evidence"), `<div class="evidence-list">${items}</div>`));
    }

    if (r.proposed_changes && r.proposed_changes.length) {
      const list = r.proposed_changes
        .map(function (c) {
          return `<li>${IA.escapeHtml(c)}</li>`;
        })
        .join("");
      parts.push(pushSection("report-changes", t("report_proposed_changes"), `<ul>${list}</ul>`));
    }

    if (r.patch) {
      const patchId = "report-patch";
      toc.push(`<li><a href="#${patchId}">${IA.escapeHtml(t("report_patch"))}</a></li>`);
      const patchHtml =
        `<details id="${patchId}"><summary>${IA.escapeHtml(t("report_patch"))}</summary>` +
        `<div class="patch-wrap"><div class="patch-actions"><button type="button" class="patch-copy" data-action="copy-patch">${IA.svgIcon("copy")}<span>${IA.escapeHtml(t("copy_button"))}</span></button></div>` +
        `<pre class="diff-block">${IA.highlightDiff(r.patch)}</pre></div></details>`;
      parts.push(patchHtml);
    }

    if (r.tests && r.tests.length) {
      const list = r.tests
        .map(function (item) {
          return `<li>${IA.escapeHtml(item)}</li>`;
        })
        .join("");
      parts.push(pushSection("report-tests", t("report_tests"), `<ul>${list}</ul>`));
    }

    if (r.risks && r.risks.length) {
      const list = r.risks
        .map(function (item) {
          return `<li>${IA.escapeHtml(item)}</li>`;
        })
        .join("");
      parts.push(pushSection("report-risks", t("report_risks"), `<ul>${list}</ul>`));
    }

    if (toc.length) {
      parts.unshift(`<details class="report-toc" open><summary>${IA.escapeHtml(t("toc_title"))}</summary><ol>${toc.join("")}</ol></details>`);
    }

    d.innerHTML = parts.join("");
  }

  function toggleReport(open) {
    document.getElementById("main").classList.toggle("report-open", open);
    document.getElementById("report-toggle").setAttribute("aria-expanded", String(open));
    if (open) {
      document.getElementById("report").scrollTop = 0;
      const closeBtn = document.querySelector(".report-close");
      if (closeBtn) closeBtn.focus();
    } else if (document.getElementById("input-bar").style.display !== "none") {
      document.getElementById("chatInput").focus();
    }
    updateBackButton();
  }

  async function chat(messageOverride) {
    const inp = document.getElementById("chatInput");
    const msg = (messageOverride !== undefined ? messageOverride : inp.value).trim();
    if (!msg || !sessionId) return;
    inp.value = "";
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
      if (!resp.ok) throw new Error(data.detail || "Unable to continue session");
      document.getElementById("progress").textContent = "";
      addMsg("assistant", data.reply);
      if (data.tools_used && data.tools_used.length) addMsg("system", t("tools_used_label") + data.tools_used.join(", "));
      loadSessions();
    } catch (e) {
      document.getElementById("progress").textContent = "";
      lastFailedChat = msg;
      addErrorWithRetry(t("error_prefix") + e.message);
    }
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
    retryBtn.addEventListener("click", function () {
      m.remove();
      if (lastFailedChat !== null) chat(lastFailedChat);
    });
    m.appendChild(retryBtn);
    d.appendChild(m);
    requestAnimationFrame(function () {
      d.scrollTop = d.scrollHeight;
    });
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
    } else if (action === "download-md") {
      const stamp = new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19);
      IA.downloadFile(`report-${stamp}.md`, reportAsMarkdown(reportData), "text/markdown;charset=utf-8");
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

    // ESC 关闭报告
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && document.getElementById("main").classList.contains("report-open")) {
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
