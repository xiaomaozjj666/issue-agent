/**
 * batch-analysis.js — 批量分析界面
 *
 * 在现有 /batch API 之上提供完整交互：一次提交多个 Issue / 仓库链接、轮询进度、
 * 查看单个任务报告、重试失败项。所有状态都从后端读取，前端不推断任务结果。
 *
 * 失败处理：任务级错误逐条展示；轮询失败按指数退避重试并可手动刷新；
 * 批次丢失（404/过期）时回到表单并说明原因。批次 ID 存入 localStorage，
 * 刷新页面后仍可继续查看进度。
 */
(function () {
  "use strict";

  const IA = window.IssueAgent;
  if (!IA) return;

  const t = IA.translate;
  const esc = IA.escapeHtml;
  const STORAGE_KEY = "iaLastBatchId";
  const POLL_INTERVAL = 2000;
  const MAX_TASKS = 100;
  const MAX_REPOSITORIES = 20;

  const ISSUE_URL_RE = /^https:\/\/(?:www\.)?github\.com\/[^/\s]+\/[^/\s]+\/issues\/\d+\/?$/i;
  const REPO_URL_RE = /^https:\/\/(?:www\.)?github\.com\/[^/\s]+\/[^/\s]+\/?$/i;
  const SHORT_ISSUE_RE = /^([\w.-]+)\/([\w.-]+)#(\d+)$/;
  const SHORT_REPO_RE = /^([\w.-]+)\/([\w.-]+)$/;
  const TERMINAL_STATUSES = { completed: true, partial: true };

  const state = {
    overlay: null,
    batchId: null,
    batch: null,
    timer: null,
    failures: 0,
    expanded: Object.create(null),
    details: Object.create(null),
    submitting: false,
  };

  function el(id) {
    return document.getElementById(id);
  }

  function storedBatchId() {
    try {
      return localStorage.getItem(STORAGE_KEY) || "";
    } catch (e) {
      return "";
    }
  }

  function rememberBatchId(batchId) {
    try {
      if (batchId) localStorage.setItem(STORAGE_KEY, batchId);
      else localStorage.removeItem(STORAGE_KEY);
    } catch (e) {
      /* ignore */
    }
  }

  function batchEndpoint(suffix) {
    return "/batch/" + encodeURIComponent(state.batchId || "") + (suffix || "");
  }

  function isOpen() {
    return Boolean(state.overlay) && state.overlay.getAttribute("aria-hidden") === "false";
  }

  /* ───────────────────────── 输入解析 ───────────────────────── */

  function classifyLine(rawLine) {
    const line = rawLine.trim();
    if (!line) return null;
    if (ISSUE_URL_RE.test(line)) {
      return { kind: "issue", url: line.replace(/\/+$/, "") };
    }
    if (REPO_URL_RE.test(line)) {
      return { kind: "repo", url: line.replace(/\/+$/, "") };
    }
    const shortIssue = line.match(SHORT_ISSUE_RE);
    if (shortIssue) {
      return {
        kind: "issue",
        url: "https://github.com/" + shortIssue[1] + "/" + shortIssue[2] + "/issues/" + shortIssue[3],
      };
    }
    const shortRepo = line.match(SHORT_REPO_RE);
    if (shortRepo) {
      return { kind: "repo", url: "https://github.com/" + shortRepo[1] + "/" + shortRepo[2] };
    }
    return null;
  }

  function parseSources(text) {
    const issues = [];
    const repositories = [];
    const invalid = [];
    text.split(/\r?\n/).forEach(function (line) {
      if (!line.trim()) return;
      const parsed = classifyLine(line);
      if (!parsed) {
        invalid.push(line.trim());
      } else if (parsed.kind === "issue") {
        issues.push(parsed.url);
      } else {
        repositories.push(parsed.url);
      }
    });
    return { issues: issues, repositories: repositories, invalid: invalid };
  }

  /* ───────────────────────── 界面构建 ───────────────────────── */

  function buildOverlay() {
    if (state.overlay) return state.overlay;
    const overlay = document.createElement("div");
    overlay.className = "overlay";
    overlay.id = "batch-modal";
    overlay.setAttribute("aria-hidden", "true");
    overlay.innerHTML =
      '<div class="overlay-backdrop" data-close></div>' +
      '<div class="overlay-surface batch-surface" role="dialog" aria-modal="true" aria-labelledby="batch-title">' +
      '<div class="overlay-head">' +
      '<h2 id="batch-title">' +
      esc(t("bulk_title")) +
      "</h2>" +
      '<button class="drawer-close" type="button" data-close aria-label="' +
      esc(t("bulk_close")) +
      '">×</button>' +
      "</div>" +
      '<div class="batch-body">' +
      '<p id="batch-empty" class="batch-empty">' +
      esc(t("bulk_empty")) +
      "</p>" +
      '<form id="batch-form" class="batch-form" novalidate>' +
      '<label class="batch-field-label" for="batch-input">' +
      esc(t("bulk_form_label")) +
      "</label>" +
      '<textarea id="batch-input" rows="5" maxlength="8000" placeholder="' +
      esc(t("bulk_form_placeholder")) +
      '"></textarea>' +
      '<p class="batch-hint" id="batch-form-hint"></p>' +
      '<p id="batch-form-error" class="batch-error" role="alert" hidden></p>' +
      '<div class="batch-form-actions">' +
      '<button type="submit" class="primary-action" id="batch-submit">' +
      esc(t("bulk_submit")) +
      "</button>" +
      "</div>" +
      "</form>" +
      '<section id="batch-progress" class="batch-progress" aria-labelledby="batch-progress-title" hidden>' +
      '<div class="batch-progress-head">' +
      '<h3 id="batch-progress-title">' +
      esc(t("bulk_progress_title")) +
      "</h3>" +
      '<span id="batch-progress-status" class="batch-status-chip" data-state="pending">' +
      esc(t("bulk_status_pending")) +
      "</span>" +
      "</div>" +
      '<div class="batch-progress-track" role="progressbar" aria-labelledby="batch-progress-title" ' +
      'aria-valuemin="0" aria-valuemax="100" aria-valuenow="0"><span id="batch-progress-fill"></span></div>' +
      '<p id="batch-progress-count" class="batch-progress-count"></p>' +
      '<p id="batch-progress-note" class="batch-hint"></p>' +
      '<p id="batch-poll-error" class="batch-error" role="alert" hidden>' +
      '<span id="batch-poll-error-text"></span>' +
      '<button type="button" id="batch-poll-retry" class="batch-inline-action">' +
      esc(t("bulk_poll_retry")) +
      "</button></p>" +
      '<div class="batch-actions">' +
      '<button type="button" class="batch-action" id="batch-retry" hidden>' +
      esc(t("bulk_retry_failed")) +
      "</button>" +
      '<button type="button" class="batch-action" id="batch-copy-failed" hidden>' +
      esc(t("bulk_copy_failed")) +
      "</button>" +
      '<button type="button" class="batch-action" id="batch-refresh">' +
      esc(t("bulk_refresh")) +
      "</button>" +
      '<button type="button" class="batch-action" id="batch-new">' +
      esc(t("bulk_new_batch")) +
      "</button>" +
      "</div>" +
      '<ul id="batch-task-list" class="batch-task-list" aria-label="' +
      esc(t("bulk_task_list_label")) +
      '"></ul>' +
      "</section>" +
      "</div>" +
      "</div>";
    document.body.appendChild(overlay);

    overlay.querySelectorAll("[data-close]").forEach(function (button) {
      button.addEventListener("click", close);
    });
    overlay.addEventListener("keydown", function (event) {
      if (event.key === "Escape") {
        event.stopPropagation();
        close();
      }
    });
    el("batch-form").addEventListener("submit", onSubmit);
    el("batch-retry").addEventListener("click", onRetry);
    el("batch-copy-failed").addEventListener("click", onCopyFailed);
    el("batch-refresh").addEventListener("click", function () {
      pollOnce();
    });
    el("batch-new").addEventListener("click", function () {
      resetToForm("");
    });
    el("batch-poll-retry").addEventListener("click", function () {
      pollOnce();
    });
    // limit 与后端 app/routes/batch.py 的 REPO_ISSUE_LIMIT 保持一致
    el("batch-form-hint").textContent = t("bulk_form_hint", { limit: 10, tasks: MAX_TASKS, repos: MAX_REPOSITORIES });

    state.overlay = overlay;
    return overlay;
  }

  function open() {
    const overlay = buildOverlay();
    overlay.setAttribute("aria-hidden", "false");
    if (!state.batchId) {
      state.batchId = storedBatchId() || null;
    }
    if (state.batchId) {
      showBatchState();
      if (state.batch) renderBatch(state.batch);
      pollOnce();
      const closeButton = overlay.querySelector(".drawer-close");
      if (closeButton) closeButton.focus();
    } else {
      showFormState();
      el("batch-input").focus();
    }
  }

  function close() {
    if (!state.overlay) return;
    stopPolling();
    state.overlay.setAttribute("aria-hidden", "true");
    const trigger = el("batch-open-btn");
    if (trigger) trigger.focus();
  }

  function showFormState() {
    el("batch-empty").hidden = false;
    el("batch-form").hidden = false;
    el("batch-progress").hidden = true;
  }

  function showBatchState() {
    el("batch-empty").hidden = true;
    el("batch-form").hidden = true;
    el("batch-progress").hidden = false;
  }

  function resetToForm(message) {
    stopPolling();
    state.batchId = null;
    state.batch = null;
    state.failures = 0;
    state.expanded = Object.create(null);
    state.details = Object.create(null);
    rememberBatchId("");
    showFormState();
    el("batch-task-list").innerHTML = "";
    showFormError(message || "");
    el("batch-input").focus();
  }

  function showFormError(message) {
    const node = el("batch-form-error");
    node.textContent = message || "";
    node.hidden = !message;
  }

  function showPollError(message) {
    const node = el("batch-poll-error");
    el("batch-poll-error-text").textContent = message || "";
    node.hidden = !message;
  }

  function setSubmitting(busy) {
    state.submitting = busy;
    const button = el("batch-submit");
    if (!button) return;
    button.disabled = busy;
    button.textContent = busy ? t("bulk_submitting") : t("bulk_submit");
  }

  function setRetrying(busy) {
    const button = el("batch-retry");
    if (!button) return;
    button.disabled = busy;
    button.textContent = busy ? t("bulk_retrying") : t("bulk_retry_failed");
  }

  /* ───────────────────────── 提交 ───────────────────────── */

  async function onSubmit(event) {
    event.preventDefault();
    if (state.submitting) return;
    const parsed = parseSources(el("batch-input").value);
    if (!parsed.issues.length && !parsed.repositories.length && !parsed.invalid.length) {
      showFormError(t("bulk_form_empty"));
      return;
    }
    if (parsed.invalid.length) {
      const preview = parsed.invalid.slice(0, 5).join(" · ");
      const suffix = parsed.invalid.length > 5 ? " · …" : "";
      showFormError(t("bulk_form_invalid", { lines: preview + suffix }));
      return;
    }
    if (parsed.issues.length > MAX_TASKS) {
      showFormError(t("bulk_form_too_many_tasks", { limit: MAX_TASKS, count: parsed.issues.length }));
      return;
    }
    if (parsed.repositories.length > MAX_REPOSITORIES) {
      showFormError(t("bulk_form_too_many_repos", { limit: MAX_REPOSITORIES, count: parsed.repositories.length }));
      return;
    }
    showFormError("");
    setSubmitting(true);
    try {
      const batch = await IA.apiJson("/batch", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ issue_urls: parsed.issues, repository_urls: parsed.repositories }),
      });
      state.batchId = batch.batch_id;
      state.failures = 0;
      rememberBatchId(batch.batch_id);
      showBatchState();
      renderBatch(batch);
      startPolling();
    } catch (error) {
      showFormError(t("bulk_submit_failed", { reason: IA.formatErrorDetail(error.message) }));
    } finally {
      setSubmitting(false);
    }
  }

  /* ───────────────────────── 轮询 ───────────────────────── */

  function startPolling() {
    stopPolling();
    if (!state.batchId) return;
    state.failures = 0;
    schedulePoll(0);
  }

  function stopPolling() {
    if (state.timer) {
      clearTimeout(state.timer);
      state.timer = null;
    }
  }

  function schedulePoll(delay) {
    stopPolling();
    state.timer = setTimeout(pollOnce, delay);
  }

  async function pollOnce() {
    if (!state.batchId) return;
    stopPolling();
    try {
      const batch = await IA.apiJson(batchEndpoint());
      state.failures = 0;
      showPollError("");
      renderBatch(batch);
      if (TERMINAL_STATUSES[batch.status] || !isOpen()) return;
      schedulePoll(POLL_INTERVAL);
    } catch (error) {
      const message = String(error.message || error);
      if (/HTTP 404\b/.test(message)) {
        resetToForm(t("bulk_not_found"));
        return;
      }
      state.failures += 1;
      showPollError(t("bulk_poll_failed", { reason: IA.formatErrorDetail(message) }));
      if (!isOpen()) return;
      const delay = Math.min(30000, POLL_INTERVAL * Math.pow(2, state.failures));
      schedulePoll(delay);
    }
  }

  /* ───────────────────────── 进度与结果 ───────────────────────── */

  function statusLabel(status) {
    if (status === "running") return t("bulk_status_running");
    if (status === "completed") return t("bulk_status_completed");
    if (status === "partial") return t("bulk_status_partial");
    return t("bulk_status_pending");
  }

  function taskStatusLabel(status) {
    if (status === "running") return t("bulk_task_running");
    if (status === "completed") return t("bulk_task_completed");
    if (status === "failed") return t("bulk_task_failed");
    if (status === "cancelled") return t("bulk_task_cancelled");
    return t("bulk_task_pending");
  }

  function taskStatusState(status) {
    if (status === "running") return "running";
    if (status === "completed") return "completed";
    if (status === "failed") return "failed";
    if (status === "cancelled") return "cancelled";
    return "pending";
  }

  function renderBatch(batch) {
    state.batch = batch;
    const progress = batch.progress || {};
    const counts = {
      pending: progress.pending || 0,
      running: progress.running || 0,
      completed: progress.completed || 0,
      failed: progress.failed || 0,
      cancelled: progress.cancelled || 0,
    };
    const total = counts.pending + counts.running + counts.completed + counts.failed + counts.cancelled;
    const done = counts.completed + counts.failed + counts.cancelled;
    const percent = total ? Math.round((done / total) * 100) : 0;

    const statusChip = el("batch-progress-status");
    statusChip.textContent = statusLabel(batch.status);
    statusChip.dataset.state = batch.status || "pending";

    const track = el("batch-progress").querySelector(".batch-progress-track");
    track.setAttribute("aria-valuenow", String(percent));
    el("batch-progress-fill").style.width = percent + "%";
    el("batch-progress-count").textContent = t("bulk_progress_summary", { done: done, total: total });
    el("batch-progress-note").textContent = t("bulk_progress_counts", counts);

    const retryButton = el("batch-retry");
    retryButton.hidden = !(counts.failed + counts.cancelled);
    el("batch-copy-failed").hidden = !(counts.failed + counts.cancelled);

    renderTasks(batch.tasks || []);
  }

  function rowFor(task) {
    const list = el("batch-task-list");
    let row = list.querySelector('[data-task-id="' + task.task_id + '"]');
    if (row) return row;
    row = document.createElement("li");
    row.className = "batch-task";
    row.dataset.taskId = task.task_id;
    row.innerHTML =
      '<div class="batch-task-head">' +
      '<span class="batch-task-status" data-state="pending"></span>' +
      '<a class="batch-task-url" target="_blank" rel="noopener noreferrer"></a>' +
      '<button class="batch-task-report-btn" type="button" aria-expanded="false"></button>' +
      "</div>" +
      '<p class="batch-task-error" hidden></p>' +
      '<div class="batch-task-report" hidden></div>';
    const button = row.querySelector(".batch-task-report-btn");
    button.addEventListener("click", function () {
      toggleTaskReport(task.task_id);
    });
    list.appendChild(row);
    return row;
  }

  function issueLabel(url) {
    const match = String(url).match(/github\.com\/([^/]+)\/([^/]+)\/issues\/(\d+)/i);
    return match ? match[1] + "/" + match[2] + " #" + match[3] : String(url);
  }

  function renderTasks(tasks) {
    const list = el("batch-task-list");
    tasks.forEach(function (task) {
      const row = rowFor(task);
      const status = row.querySelector(".batch-task-status");
      status.textContent = taskStatusLabel(task.status);
      status.dataset.state = taskStatusState(task.status);
      const link = row.querySelector(".batch-task-url");
      link.href = task.issue_url;
      link.textContent = issueLabel(task.issue_url);
      link.title = task.issue_url;

      const errorNode = row.querySelector(".batch-task-error");
      if (task.error) {
        errorNode.textContent = t("bulk_task_error", { reason: task.error });
        errorNode.hidden = false;
      } else {
        errorNode.textContent = "";
        errorNode.hidden = true;
      }

      const reportButton = row.querySelector(".batch-task-report-btn");
      const canReport = task.status === "completed" && task.has_report;
      reportButton.hidden = !canReport;
      if (canReport) {
        const expanded = Boolean(state.expanded[task.task_id]);
        reportButton.textContent = expanded ? t("bulk_hide_report") : t("bulk_view_report");
        reportButton.setAttribute("aria-expanded", expanded ? "true" : "false");
      }
      if (!canReport) {
        collapseTask(row, task.task_id);
      }
    });
    // 移除后端已不存在的任务行（正常不会发生，防御脏数据）
    const known = Object.create(null);
    tasks.forEach(function (task) {
      known[task.task_id] = true;
    });
    Array.prototype.slice.call(list.children).forEach(function (row) {
      if (!known[row.dataset.taskId]) list.removeChild(row);
    });
  }

  function collapseTask(row, taskId) {
    state.expanded[taskId] = false;
    const report = row.querySelector(".batch-task-report");
    if (report) {
      report.hidden = true;
      report.innerHTML = "";
    }
  }

  async function toggleTaskReport(taskId) {
    const row = el("batch-task-list").querySelector('[data-task-id="' + taskId + '"]');
    if (!row) return;
    if (state.expanded[taskId]) {
      collapseTask(row, taskId);
      const button = row.querySelector(".batch-task-report-btn");
      button.textContent = t("bulk_view_report");
      button.setAttribute("aria-expanded", "false");
      return;
    }
    state.expanded[taskId] = true;
    const button = row.querySelector(".batch-task-report-btn");
    button.textContent = t("bulk_hide_report");
    button.setAttribute("aria-expanded", "true");
    const container = row.querySelector(".batch-task-report");
    container.hidden = false;
    if (state.details[taskId]) {
      container.innerHTML = renderReport(state.details[taskId]);
      return;
    }
    container.innerHTML = '<p class="batch-hint">' + esc(t("bulk_report_loading")) + "</p>";
    try {
      const detail = await IA.apiJson(batchEndpoint("/tasks/" + encodeURIComponent(taskId)));
      state.details[taskId] = detail.report;
      if (state.expanded[taskId]) {
        container.innerHTML = renderReport(detail.report);
      }
    } catch (error) {
      container.innerHTML =
        '<p class="batch-error">' +
        esc(t("bulk_report_failed", { reason: IA.formatErrorDetail(error.message) })) +
        "</p>";
    }
  }

  function renderReport(report) {
    if (!report) {
      return '<p class="batch-hint">' + esc(t("bulk_no_report")) + "</p>";
    }
    const evidence = Array.isArray(report.evidence) ? report.evidence : [];
    const changes = Array.isArray(report.proposed_changes) ? report.proposed_changes : [];
    const confidence = report.confidence
      ? t("bulk_report_confidence", { level: IA.enumLabel("confidence", report.confidence) })
      : "";
    const meta = [confidence, t("bulk_report_evidence", { count: evidence.length }), t("bulk_report_changes", { count: changes.length })]
      .filter(Boolean)
      .map(function (item) {
        return "<span>" + esc(item) + "</span>";
      })
      .join("");
    const changeItems = changes
      .map(function (change) {
        return "<li>" + esc(change) + "</li>";
      })
      .join("");
    return (
      '<div class="batch-task-report-inner">' +
      '<p class="batch-task-report-meta">' +
      meta +
      "</p>" +
      '<h4>' +
      esc(t("bulk_report_summary")) +
      "</h4><p>" +
      esc(report.summary || "—") +
      "</p>" +
      '<h4>' +
      esc(t("bulk_report_root_cause")) +
      "</h4><p>" +
      esc(report.root_cause || "—") +
      "</p>" +
      (changeItems ? "<ul>" + changeItems + "</ul>" : "") +
      "</div>"
    );
  }

  /* ───────────────────────── 失败项处理 ───────────────────────── */

  async function onRetry() {
    if (!state.batchId) return;
    setRetrying(true);
    try {
      const batch = await IA.apiJson(batchEndpoint("/retry"), { method: "POST" });
      showPollError("");
      renderBatch(batch);
      startPolling();
    } catch (error) {
      showPollError(t("bulk_retry_failed_action", { reason: IA.formatErrorDetail(error.message) }));
    } finally {
      setRetrying(false);
    }
  }

  async function onCopyFailed() {
    const tasks = (state.batch && state.batch.tasks) || [];
    const failed = tasks
      .filter(function (task) {
        return task.status === "failed" || task.status === "cancelled";
      })
      .map(function (task) {
        return task.issue_url;
      });
    if (!failed.length) {
      if (IA.flashToast) IA.flashToast(t("bulk_copy_none"));
      return;
    }
    const copied = await IA.copyToClipboard(failed.join("\n"));
    if (IA.flashToast) IA.flashToast(copied === false ? t("copy_failed") : t("bulk_copy_done"));
  }

  /* ───────────────────────── 初始化 ───────────────────────── */

  function init() {
    const trigger = el("batch-open-btn");
    if (!trigger) return;
    trigger.addEventListener("click", open);
    // 后台标签页暂停渲染/定时器：切回时立刻补一次进度，避免看起来卡住
    document.addEventListener("visibilitychange", function () {
      if (document.visibilityState === "visible" && state.batchId && state.overlay &&
        state.overlay.getAttribute("aria-hidden") === "false" &&
        (!state.batch || !TERMINAL_STATUSES[state.batch.status])) {
        pollOnce();
      }
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

  IA.BatchAnalysis = { open: open, close: close };
})();
