(function () {
  "use strict";

  const I18N_DEFAULTS = {
    doc_title: "GitHub Issue Agent",
    brand_title: "Issue Agent",
    brand_subtitle: "Repository intelligence",
    issue_label: "GitHub Issue URL",
    issue_placeholder: "https://github.com/owner/repo/issues/123",
    new_analysis: "New analysis",
    history_title_sessions: "Issue history",
    history_title_archive: "Archive",
    archive_toggle_show: "Archived",
    archive_toggle_active: "Active",
    archive_session: "Archive session",
    restore_session: "Restore session",
    action_export_session: "Export session",
    action_import_session: "Import session",
    import_session_success: "Session imported successfully",
    import_session_failed: "Session import failed: {reason}",
    import_session_invalid: "Invalid session file format",
    history_toggle: "Session history",
    theme_toggle: "Toggle theme",
    history_search_placeholder: "Search issues and repositories",
    history_search_label: "Search sessions",
    session_list_label: "Session list",
    history_empty_active: "No sessions yet.<br>Paste an Issue URL to begin.",
    history_empty_archive: "No archived sessions.",
    history_group_running: "Running",
    history_group_today: "Today",
    history_group_week: "Previous 7 days",
    history_group_older: "Older",
    conversation_label: "Investigation thread",
    back_button_title: "Back",
    back_button_label: "Back to previous view",
    cancel_button: "Cancel",
    cancelling: "Cancelling…",
    report_toggle: "View report",
    report_close: "Close",
    report_title: "Analysis report",
    input_placeholder: "Ask follow-up questions...",
    send_button: "Send",
    messages_label: "Conversation messages",
    speaker_you: "You",
    speaker_agent: "Issue Agent",
    dialog_input_label: "Session title",
    dialog_rename_title: "Rename session",
    dialog_rename_message: "Choose a concise title that will be easy to find later.",
    dialog_delete_title: "Delete session permanently?",
    dialog_delete_message: "\u201c{title}\u201d and its conversation history will be removed. This cannot be undone.",
    dialog_save: "Save",
    dialog_cancel: "Cancel",
    dialog_delete_forever: "Delete forever",
    welcome: "Choose a previous session or start a new Issue analysis.",
    analyzing_prefix: "Analyzing: ",
    fetching: "Fetching issue...",
    exploring_files: "Exploring {count} files...",
    thinking: "Thinking...",
    review_progress: "Independent review: {status}",
    done: "Done.",
    cancelled_message: "Investigation cancelled.",
    archived_readonly: "Archived sessions are read-only. Restore this session from the sidebar to continue.",
    failed_default: "This investigation failed before producing a report.",
    cancelled_default: "This investigation was cancelled. Start a new analysis when you are ready.",
    running_default: "This investigation is still running. Its durable event history is shown above.",
    no_report_default: "This investigation has not produced a report yet.",
    tools_used_label: "Tools: ",
    connection_error: "Connection error: ",
    connection_slow: "Connection seems slow, waiting for response…",
    connection_stalled: "Connection may have stalled. Consider stopping and retrying",
    error_prefix: "Error: ",
    invalid_issue_url: "Please enter a valid GitHub issue URL (https://github.com/owner/repo/issues/123).",
    analysis_complete_label: "Analysis complete",
    open_full_report: "Open full report",
    report_confidence: "Confidence",
    confidence_high: "High",
    confidence_medium: "Medium",
    confidence_low: "Low",
    report_root_cause: "Root cause",
    report_evidence: "Code evidence",
    report_proposed_changes: "Proposed changes",
    report_patch: "View generated patch",
    report_patch_export: "Patch",
    report_tests: "Suggested tests",
    report_risks: "Risks",
    report_independent_review: "Independent review \u00b7 {status}",
    review_status_approved: "Approved",
    review_status_revised: "Revised",
    review_status_rejected: "Rejected",
    review_status_unavailable: "Unavailable",
    investigation_trail: "Investigation trail",
    copy_button: "Copy",
    copied: "Copied",
    copy_failed: "Copy failed",
    copy_code: "Copy code",
    copy_text: "Copy text",
    chat_stopped: "Generation stopped",
    send_button_hint: "Enter to send, Shift+Enter for newline",
    stop_button: "Stop generating",
    history_empty_cta_text: "No sessions yet. Paste an Issue URL to start your first analysis.",
    history_empty_cta_btn: "Start new analysis",
    download_report: "Download report",
    download_json: "Download JSON",
    download_markdown: "Download Markdown",
    retry_send: "Retry",
    loading_session: "Loading session...",
    tool_call_label: "Tool",
    no_investigation: "Investigation not yet complete. No conclusions available.",
    cancellation_requested: "Cancellation requested. The current provider operation may finish first.",
    timeline_model_calls: "{count} model call(s)",
    timeline_tool_calls: "{count} tool call(s)",
    timeline_reviews: "{count} review(s)",
    timeline_files_read: "{count} file(s) read",
    timeline_expand: "Show all",
    timeline_collapse: "Collapse",
    view_source: "View source",
    toc_title: "Contents",
    report_panel_label: "Analysis report",
    matrix_chart_title: "Evidence credibility matrix",
    matrix_chart_caption: "Each evidence validated across 4 dimensions: green=pass, red=fail",
    matrix_dim_file_read: "File read",
    matrix_dim_lines_valid: "Lines valid",
    matrix_dim_has_reason: "Has reason",
    matrix_dim_review_verified: "Review verified",
    matrix_pass: "Pass",
    matrix_fail: "Fail",
    sankey_chart_title: "Evidence-to-root-cause support",
    sankey_chart_caption: "Reasoning chain: Issue → root cause → evidence",
    sankey_issue_node: "Issue",
    sankey_default_cause: "Root cause",
    sankey_cause_node_label: "Cause {n}",
    sankey_strong_support: "Strong support (read + valid lines)",
    sankey_weak_support: "Weak support (reason only)",
    funnel_chart_title: "Investigation efficiency",
    funnel_chart_caption: "Model calls → tool calls → files read → valid evidence",
    funnel_model_calls: "Model calls",
    funnel_tool_calls: "Tool calls",
    funnel_files_read: "Files read",
    funnel_valid_evidence: "Valid evidence",
    funnel_count: "Count",
    funnel_conversion: "Step conversion",
    funnel_overall: "Overall conversion",
    funnel_empty: "No investigation data available",
    chart_save_image: "Save as image",
    chart_restore: "Restore",
    report_fullscreen: "Fullscreen",
    report_exit_fullscreen: "Exit fullscreen",
    report_default_fullscreen: "Default fullscreen (auto-fullscreen on report done)",
    report_default_fullscreen_on: "Enabled: auto-fullscreen on report done",
    report_print: "Print / Export PDF",
    cdn_offline_notice: "Some resources failed to load; charts or code highlighting may be unavailable",
    batch_select_all: "Select all",
    batch_select_none: "Clear selection",
    batch_selected_count: "{count} selected",
    batch_archive_selected: "Archive selected",
    batch_restore_selected: "Restore selected",
    batch_delete_selected: "Delete selected",
    batch_confirm_delete: "Permanently delete {count} selected session(s)? This cannot be undone.",
    batch_archive_done: "Archived {count} session(s)",
    batch_restore_done: "Restored {count} session(s)",
    batch_delete_done: "Deleted {count} session(s)",
    batch_partial_error: "{failed} session(s) failed",
    back_to_top: "Back to top",
    patch_view_unified: "Unified",
    patch_view_split: "Split view",
    patch_download: "Download .patch",
    tests_copy_pytest: "Copy as pytest",
    risk_severity_high: "High",
    risk_severity_medium: "Medium",
    risk_severity_low: "Low",
    chart_zoom: "Zoom",
    chart_zoom_title: "Chart zoom view",
    evidence_show_more: "Show {count} more",
    evidence_show_less: "Collapse",
    files_tracker_title: "Files explored",
    files_tracker_summary: "{files} files · {dirs} dirs explored",
    files_tracker_empty: "No files explored yet",
    files_tracker_expand: "Expand dirs",
    files_tracker_collapse: "Collapse dirs",
    sankey_medium_support: "Medium support (read, lines invalid)",
    chart_data_view: "View data",
    chart_data_view_refresh: "Refresh",
    report_evidence_chart_empty: "No code evidence yet",
    // #5 修复方案优先级 + 影响范围
    change_scope: "Affected",
    change_priority_p0_desc: "P0 — must fix (security/crash/data loss)",
    change_priority_p1_desc: "P1 — should fix (recommended)",
    change_priority_p2_desc: "P2 — optional (nice to have)",
    // #9 HTML 导出
    download_html: "Download HTML",
    // #10 独立审查增强
    review_reviewer_model: "Reviewer model",
    review_reviewer_calls: "{count} review call(s)",
    review_valid_evidence: "Valid evidence",
    review_root_cause_supported: "Root cause supported",
    review_supported_yes: "Yes",
    review_supported_no: "No",
    // #14 分屏模式
    report_split: "Split view",
    report_exit_split: "Exit split view",
    // #22 Heatmap 部分通过
    matrix_partial: "Partial",
  };

  function loadI18n() {
    const payload = document.getElementById("i18n-payload");
    if (!payload) return Object.assign({}, I18N_DEFAULTS);
    try {
      return Object.assign({}, I18N_DEFAULTS, JSON.parse(payload.textContent || "{}"));
    } catch (error) {
      console.warn("Failed to parse i18n payload", error);
      return Object.assign({}, I18N_DEFAULTS);
    }
  }

  const i18nTable = loadI18n();

  function translate(key, params) {
    const template = i18nTable[key] !== undefined ? i18nTable[key] : I18N_DEFAULTS[key];
    if (template === undefined) return key;
    if (!params) return String(template);
    return String(template).replace(/\{(\w+)\}/g, function (_match, name) {
      return params[name] !== undefined ? String(params[name]) : `{${name}}`;
    });
  }

  function applyI18n(root) {
    const scope = root || document;
    scope.querySelectorAll("[data-i18n]").forEach(function (node) {
      const key = node.getAttribute("data-i18n");
      const value = translate(key);
      const fragments = String(value).split(/<br\s*\/?\s*>/i);
      node.replaceChildren();
      fragments.forEach(function (fragment, index) {
        if (index) node.appendChild(document.createElement("br"));
        node.appendChild(document.createTextNode(fragment));
      });
    });
    scope.querySelectorAll("[data-i18n-placeholder]").forEach(function (node) {
      const key = node.getAttribute("data-i18n-placeholder");
      node.setAttribute("placeholder", translate(key));
    });
    scope.querySelectorAll("[data-i18n-title]").forEach(function (node) {
      node.setAttribute("title", translate(node.getAttribute("data-i18n-title")));
    });
    scope.querySelectorAll("[data-i18n-aria-label]").forEach(function (node) {
      node.setAttribute("aria-label", translate(node.getAttribute("data-i18n-aria-label")));
    });
    const titleKey = i18nTable.doc_title;
    if (titleKey) document.title = titleKey;
  }

  function escapeHtml(value) {
    if (value === null || value === undefined) return "";
    return String(value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }

  function escapeAttr(value) {
    return escapeHtml(value).replace(/`/g, "&#096;");
  }

  function safeClass(value) {
    return String(value || "").replace(/[^a-zA-Z0-9_-]/g, "");
  }

  // 枚举值本地化：构造 `${prefix}_${value}` 的 i18n key，
  // 翻译失败时回退为原始 value 字符串
  function enumLabel(prefix, value) {
    const key = prefix + "_" + safeClass(value);
    const translated = translate(key);
    return translated === key ? String(value || "") : translated;
  }

  async function apiJson(url, options) {
    const response = await fetch(url, options);
    if (!response.ok) {
      let detail = "Request failed";
      try {
        detail = (await response.json()).detail || detail;
      } catch (error) {
        console.warn("Unable to decode API error response", error);
      }
      throw new Error(detail);
    }
    return response.status === 204 ? null : response.json();
  }

  // SSE 流解析：从 buffer 中提取完整的 SSE 事件，返回剩余未完成的 buffer
  // analyze() 和 chat() 共用此解析器，避免两套不一致的 data: 行解析逻辑
  function parseSseEvents(buffer) {
    const events = [];
    let remaining = buffer;
    let sepIdx;
    while ((sepIdx = remaining.indexOf("\n\n")) !== -1) {
      const rawEvent = remaining.slice(0, sepIdx);
      remaining = remaining.slice(sepIdx + 2);
      // SSE 规范：data: 字段可能跨多行，需拼接
      const dataLines = rawEvent
        .split("\n")
        .filter(function (line) { return line.startsWith("data:"); })
        .map(function (line) { return line.slice(5).replace(/^ /, ""); });
      if (!dataLines.length) continue;
      const dataStr = dataLines.join("\n");
      try {
        events.push(JSON.parse(dataStr));
      } catch (e) {
        // 非 JSON 的 data 行（如 [DONE]）跳过
      }
    }
    return { events: events, remaining: remaining };
  }

  function formatDuration(milliseconds) {
    if (milliseconds < 1000) return `${milliseconds} ms`;
    const seconds = Math.round(milliseconds / 1000);
    return seconds < 60 ? `${seconds} s` : `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
  }

  function formatRelativeTime(value) {
    // #9 修复：null/undefined/空字符串都会让 new Date() 返回 epoch（1970-01-01），
    // 与 Invalid Date 不同（getTime 返回 0 而非 NaN）。后端字段未落库时显示
    // "1970年1月1日" 极其荒谬，这里提前拦截。
    if (value === null || value === undefined || value === "") return "";
    const date = new Date(value);
    const time = date.getTime();
    if (Number.isNaN(time)) return "";
    if (time === 0) return "";
    const locale = document.documentElement.lang || navigator.language || "en";
    const seconds = Math.max(0, Math.floor((Date.now() - time) / 1000));
    const formatter = new Intl.RelativeTimeFormat(locale, { numeric: "auto" });
    if (seconds < 60) return formatter.format(0, "second");
    if (seconds < 3600) return formatter.format(-Math.floor(seconds / 60), "minute");
    if (seconds < 86400) return formatter.format(-Math.floor(seconds / 3600), "hour");
    if (seconds < 604800) return formatter.format(-Math.floor(seconds / 86400), "day");
    return new Intl.DateTimeFormat(locale).format(date);
  }

  const ICONS = {
    plus: '<svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true"><path fill="currentColor" d="M7.25 1.75a.75.75 0 0 1 1.5 0v5.5h5.5a.75.75 0 0 1 0 1.5h-5.5v5.5a.75.75 0 0 1-1.5 0v-5.5h-5.5a.75.75 0 0 1 0-1.5h5.5v-5.5Z"/></svg>',
    send: '<svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true"><path fill="currentColor" d="M1.29 1.17a1 1 0 0 1 1.05-.1l12 6a1 1 0 0 1 0 1.79l-12 6A1 1 0 0 1 .9 13.8l1.24-4.56 6.48-1.24-6.48-1.24L.9 2.2a1 1 0 0 1 .39-1.03Z"/></svg>',
    back: '<svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true"><path fill="currentColor" d="M7.78 12.53a.75.75 0 0 1-1.06 0l-4-4a.75.75 0 0 1 0-1.06l4-4a.75.75 0 0 1 1.06 1.06L5.06 7.25h7.19a.75.75 0 0 1 0 1.5H5.06l2.72 2.72a.75.75 0 0 1 0 1.06Z"/></svg>',
    report: '<svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true"><path fill="currentColor" d="M3.75 1.5h5.5c.2 0 .39.08.53.22l3.5 3.5c.14.14.22.33.22.53v8.5a.75.75 0 0 1-.75.75h-9.5a.75.75 0 0 1-.75-.75v-12.5a.75.75 0 0 1 .75-.75Zm.25 1v11h8V6.06L8.94 3H4Zm1.75 5.25A.75.75 0 0 1 6.5 7h3a.75.75 0 0 1 0 1.5h-3a.75.75 0 0 1-.75-.75Zm0 3A.75.75 0 0 1 6.5 10h3a.75.75 0 0 1 0 1.5h-3a.75.75 0 0 1-.75-.75Z"/></svg>',
    close: '<svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true"><path fill="currentColor" d="M3.72 3.72a.75.75 0 0 1 1.06 0L8 6.94l3.22-3.22a.75.75 0 1 1 1.06 1.06L9.06 8l3.22 3.22a.75.75 0 1 1-1.06 1.06L8 9.06l-3.22 3.22a.75.75 0 0 1-1.06-1.06L6.94 8 3.72 4.78a.75.75 0 0 1 0-1.06Z"/></svg>',
    copy: '<svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true"><path fill="currentColor" d="M0 6.75C0 5.78.78 5 1.75 5h1.5a.75.75 0 0 1 0 1.5h-1.5a.25.25 0 0 0-.25.25v7.5c0 .14.11.25.25.25h7.5a.25.25 0 0 0 .25-.25v-1.5a.75.75 0 0 1 1.5 0v1.5C11 15.22 10.22 16 9.25 16h-7.5C.78 16 0 15.22 0 14.25v-7.5Z"/><path fill="currentColor" d="M5 1.75C5 .78 5.78 0 6.75 0h7.5C15.22 0 16 .78 16 1.75v7.5C16 10.22 15.22 11 14.25 11h-7.5C5.78 11 5 10.22 5 9.25v-7.5Zm1.75-.25a.25.25 0 0 0-.25.25v7.5c0 .14.11.25.25.25h7.5a.25.25 0 0 0 .25-.25v-7.5a.25.25 0 0 0-.25-.25h-7.5Z"/></svg>',
    check: '<svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true"><path fill="currentColor" d="M13.78 4.22a.75.75 0 0 1 0 1.06l-7.25 7.25a.75.75 0 0 1-1.06 0L2.22 9.28a.75.75 0 0 1 1.06-1.06L6 10.94l6.72-6.72a.75.75 0 0 1 1.06 0Z"/></svg>',
    download: '<svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true"><path fill="currentColor" d="M7.25 1.75a.75.75 0 0 1 1.5 0v7.69l2.72-2.72a.75.75 0 1 1 1.06 1.06l-4 4a.75.75 0 0 1-1.06 0l-4-4a.75.75 0 0 1 1.06-1.06l2.72 2.72V1.75ZM2.5 13.75a.75.75 0 0 1 .75-.75h9.5a.75.75 0 0 1 0 1.5h-9.5a.75.75 0 0 1-.75-.75Z"/></svg>',
    external: '<svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true"><path fill="currentColor" d="M3.75 2h3.5a.75.75 0 0 1 0 1.5H4.5v7h7v-2.75a.75.75 0 0 1 1.5 0v3.5a.75.75 0 0 1-.75.75h-8.5a.75.75 0 0 1-.75-.75v-8.5A.75.75 0 0 1 3.75 2Zm9.69 0H9.5a.75.75 0 0 1 0-1.5h4.75a.75.75 0 0 1 .75.75V6a.75.75 0 0 1-1.5 0V2.56L8.28 7.78a.75.75 0 1 1-1.06-1.06L12.44 1.5Z"/></svg>',
    retry: '<svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true"><path fill="currentColor" d="M1.22 8.27a.75.75 0 0 1 1.06 0l1.27 1.27a5.5 5.5 0 0 1 10.4-2.04.75.75 0 1 1-1.38.59 4 4 0 0 0-7.61 1.18l1.5-1.18a.75.75 0 0 1 1.06 1.06l-2.83 2.83a.75.75 0 0 1-1.06 0L1.22 9.33a.75.75 0 0 1 0-1.06Zm12.5-2.04a.75.75 0 0 1-1.06 0l-1.27-1.27a5.5 5.5 0 0 0-10.4 2.04.75.75 0 0 0 1.38.59 4 4 0 0 1 7.61-1.18l-1.5 1.18a.75.75 0 1 0 1.06 1.06l2.83-2.83a.75.75 0 0 0 0-1.06l-1.66-1.66Z"/></svg>',
    rename: '<svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true"><path fill="currentColor" d="M11.013 1.427a1.75 1.75 0 0 1 2.474 0l1.086 1.086a1.75 1.75 0 0 1 0 2.474l-8.61 8.61c-.21.21-.47.364-.756.445l-3.251.93a.75.75 0 0 1-.927-.928l.929-3.25c.081-.286.235-.547.445-.758l8.61-8.61Z"/></svg>',
    archive: '<svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true"><path fill="currentColor" d="M1.75 2.5a.75.75 0 0 0 0 1.5h12.5a.75.75 0 0 0 0-1.5H1.75ZM2 6.25A.75.75 0 0 1 2.75 5.5h10.5a.75.75 0 0 1 .75.75v7.5a.75.75 0 0 1-.75.75H2.75a.75.75 0 0 1-.75-.75v-7.5ZM7 7.5a.75.75 0 0 0 0 1.5h2a.75.75 0 0 0 0-1.5H7Z"/></svg>',
    restore: '<svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true"><path fill="currentColor" d="M1.22 8.27a.75.75 0 0 1 1.06 0l1.27 1.27a5.5 5.5 0 0 1 10.4-2.04.75.75 0 1 1-1.38.59 4 4 0 0 0-7.61 1.18l1.5-1.18a.75.75 0 0 1 1.06 1.06l-2.83 2.83a.75.75 0 0 1-1.06 0L1.22 9.33a.75.75 0 0 1 0-1.06Z"/></svg>',
    delete: '<svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true"><path fill="currentColor" d="M11 1.75V3h2.25a.75.75 0 0 1 0 1.5H2.75a.75.75 0 0 1 0-1.5H5V1.75C5 .78 5.78 0 6.75 0h2.5C10.22 0 11 .78 11 1.75ZM4.496 6.675l.66 6.6a.25.25 0 0 0 .249.225h5.19a.25.25 0 0 0 .249-.225l.66-6.6a.75.75 0 0 1 1.492.149l-.66 6.6A1.75 1.75 0 0 1 10.595 15h-5.19a1.75 1.75 0 0 1-1.741-1.575l-.66-6.6a.75.75 0 0 1 1.492-.15Z"/></svg>',
    menu: '<svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true"><path fill="currentColor" d="M2.75 2.5a.75.75 0 0 0 0 1.5h10.5a.75.75 0 0 0 0-1.5H2.75Zm0 4.75a.75.75 0 0 0 0 1.5h10.5a.75.75 0 0 0 0-1.5H2.75Zm0 4.75a.75.75 0 0 0 0 1.5h10.5a.75.75 0 0 0 0-1.5H2.75Z"/></svg>',
    sun: '<svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true"><path fill="currentColor" d="M8 10.5a2.5 2.5 0 1 0 0-5 2.5 2.5 0 0 0 0 5ZM8 0a.75.75 0 0 1 .75.75V2a.75.75 0 0 1-1.5 0V.75A.75.75 0 0 1 8 0Zm0 13.25a.75.75 0 0 1 .75.75v1.25a.75.75 0 0 1-1.5 0V14a.75.75 0 0 1 .75-.75ZM16 8a.75.75 0 0 1-.75.75H14a.75.75 0 0 1 0-1.5h1.25A.75.75 0 0 1 16 8ZM2.75 8a.75.75 0 0 1-.75.75H.75a.75.75 0 0 1 0-1.5H2A.75.75 0 0 1 2.75 8Z"/></svg>',
    alert: '<svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true"><path fill="currentColor" d="M6.457 1.047c.659-1.234 2.427-1.234 3.086 0l6.082 11.378A1.75 1.75 0 0 1 14.082 15H1.918a1.75 1.75 0 0 1-1.543-2.575L6.457 1.047ZM8 5a.75.75 0 0 0-.75.75v2.5a.75.75 0 0 0 1.5 0v-2.5A.75.75 0 0 0 8 5Zm0 6a1 1 0 1 0 0 2 1 1 0 0 0 0-2Z"/></svg>',
  };

  function svgIcon(name) {
    return ICONS[name] || "";
  }

  async function copyToClipboard(text) {
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text);
        return true;
      }
    } catch (error) {
      console.warn("Clipboard API failed, falling back", error);
    }
    try {
      const textarea = document.createElement("textarea");
      textarea.value = text;
      textarea.setAttribute("readonly", "");
      textarea.style.position = "absolute";
      textarea.style.left = "-9999px";
      document.body.appendChild(textarea);
      textarea.select();
      const ok = document.execCommand("copy");
      document.body.removeChild(textarea);
      return ok;
    } catch (error) {
      console.warn("execCommand copy failed", error);
      return false;
    }
  }

  function downloadFile(filename, content, mime) {
    const blob = new Blob([content], { type: mime || "text/plain;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    setTimeout(function () {
      URL.revokeObjectURL(url);
    }, 0);
  }

  function highlightDiff(patch) {
    if (!patch) return "";
    const escaped = escapeHtml(patch);
    // CSS 中 .diff-* 设置 display: block 已强制换行，这里不再 join "\n"，否则会出现双倍行距
    return escaped
      .split("\n")
      .map(function (line) {
        if (line.startsWith("+++")) return `<span class="diff-add-file">${line}</span>`;
        if (line.startsWith("---")) return `<span class="diff-del-file">${line}</span>`;
        if (line.startsWith("+")) return `<span class="diff-add">${line}</span>`;
        if (line.startsWith("-")) return `<span class="diff-del">${line}</span>`;
        if (line.startsWith("@@")) return `<span class="diff-hunk">${line}</span>`;
        return `<span class="diff-ctx">${line}</span>`;
      })
      .join("");
  }

  // #6 双栏 diff 对比：左栏显示删除/上下文，右栏显示新增/上下文
  // 按 hunk 分组对齐，删除行与新增行一一对照
  function renderSideBySideDiff(patch) {
    if (!patch) return "";
    const lines = String(patch).split("\n");
    const leftLines = [];
    const rightLines = [];
    lines.forEach(function (rawLine) {
      // 数据收集阶段统一转义，防止 patch 内容中的 HTML 被注入
      const safe = escapeHtml(rawLine);
      if (rawLine.startsWith("+++") || rawLine.startsWith("---")) {
        // 文件头：只显示在左侧
        leftLines.push({ cls: rawLine.startsWith("+++") ? "diff-add-file" : "diff-del-file", text: safe });
        rightLines.push({ cls: "diff-empty", text: "" });
        return;
      }
      if (rawLine.startsWith("@@")) {
        leftLines.push({ cls: "diff-hunk", text: safe });
        rightLines.push({ cls: "diff-hunk", text: safe });
        return;
      }
      if (rawLine.startsWith("+")) {
        // 新增：只进右栏
        rightLines.push({ cls: "diff-add", text: safe });
        return;
      }
      if (rawLine.startsWith("-")) {
        // 删除：只进左栏
        leftLines.push({ cls: "diff-del", text: safe });
        return;
      }
      // 上下文行：两边都显示
      leftLines.push({ cls: "diff-ctx", text: safe });
      rightLines.push({ cls: "diff-ctx", text: safe });
    });
    // 补齐：让左右两栏行数相同，便于对齐
    while (leftLines.length < rightLines.length) {
      leftLines.push({ cls: "diff-empty", text: "" });
    }
    while (rightLines.length < leftLines.length) {
      rightLines.push({ cls: "diff-empty", text: "" });
    }
    const renderCol = function (items, side) {
      return `<div class="diff-split-col diff-split-${side}">` +
        items.map(function (item) {
          return `<span class="${item.cls}">${item.text || "&nbsp;"}</span>`;
        }).join("") +
        `</div>`;
    };
    return `<div class="diff-split-wrap">` +
      `<div class="diff-split-header"><span>— before</span><span>++ after</span></div>` +
      `<div class="diff-split-body">` +
        renderCol(leftLines, "left") +
        renderCol(rightLines, "right") +
      `</div></div>`;
  }

  function buildGitHubUrl(session, path, lines) {
    if (!session || !session.owner || !session.repo) return null;
    const owner = encodeURIComponent(String(session.owner));
    const repo = encodeURIComponent(String(session.repo));
    const base = `https://github.com/${owner}/${repo}`;
    if (!path) return base;
    // 优先使用分析时刻的 commit SHA，确保链接指向当时的代码快照；
    // 无 SHA 时降级为 HEAD（最新分支头）
    const ref = session.head_sha || "HEAD";
    const encodedPath = String(path)
      .split("/")
      .filter(Boolean)
      .map(function (segment) {
        return encodeURIComponent(segment);
      })
      .join("/");
    const numbers = lines ? String(lines).match(/\d+/g) : null;
    if (!numbers || !numbers.length) return `${base}/blob/${ref}/${encodedPath}`;
    const linePart = numbers.length > 1 ? `#L${numbers[0]}-L${numbers[1]}` : `#L${numbers[0]}`;
    return `${base}/blob/${ref}/${encodedPath}${linePart}`;
  }

  const ns = {
    apiJson,
    escapeHtml,
    escapeAttr,
    safeClass,
    enumLabel,
    translate,
    applyI18n,
    formatDuration,
    formatRelativeTime,
    svgIcon,
    copyToClipboard,
    downloadFile,
    highlightDiff,
    renderSideBySideDiff,
    buildGitHubUrl,
    parseSseEvents,
  };

  window.IssueAgent = ns;
})();
