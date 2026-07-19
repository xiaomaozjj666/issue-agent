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
    history_search_placeholder: "Search issues and repositories",
    history_empty_active: "No sessions yet.<br>Paste an Issue URL to begin.",
    history_empty_archive: "No archived sessions.",
    history_group_running: "Running",
    history_group_today: "Today",
    history_group_week: "Previous 7 days",
    history_group_older: "Older",
    conversation_label: "Investigation thread",
    back_button_title: "Back",
    cancel_button: "Cancel",
    cancelling: "Cancelling…",
    report_toggle: "View report",
    report_close: "Close",
    report_title: "Analysis report",
    input_placeholder: "Ask follow-up questions...",
    send_button: "Send",
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
    error_prefix: "Error: ",
    analysis_complete_label: "Analysis complete",
    open_full_report: "Open full report",
    report_confidence: "Confidence",
    report_root_cause: "Root cause",
    report_evidence: "Code evidence",
    report_proposed_changes: "Proposed changes",
    report_patch: "View generated patch",
    report_tests: "Suggested tests",
    report_risks: "Risks",
    report_independent_review: "Independent review \u00b7 {status}",
    investigation_trail: "Investigation trail",
    copy_button: "Copy",
    copied: "Copied",
    copy_failed: "Copy failed",
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
    view_source: "View source",
    toc_title: "Contents",
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
      // 允许 i18n 字符串中包含 <br> 等基础换行标签
      node.innerHTML = value;
    });
    scope.querySelectorAll("[data-i18n-placeholder]").forEach(function (node) {
      const key = node.getAttribute("data-i18n-placeholder");
      node.setAttribute("placeholder", translate(key));
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

  function formatDuration(milliseconds) {
    if (milliseconds < 1000) return `${milliseconds} ms`;
    const seconds = Math.round(milliseconds / 1000);
    return seconds < 60 ? `${seconds} s` : `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
  }

  function formatRelativeTime(value) {
    const seconds = Math.max(0, Math.floor((Date.now() - new Date(value).getTime()) / 1000));
    if (seconds < 60) return "just now";
    if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
    if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
    if (seconds < 604800) return `${Math.floor(seconds / 86400)}d ago`;
    return new Date(value).toLocaleDateString();
  }

  const ICONS = {
    plus: '<svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true"><path fill="currentColor" d="M7.25 1.75a.75.75 0 0 1 1.5 0v5.5h5.5a.75.75 0 0 1 0 1.5h-5.5v5.5a.75.75 0 0 1-1.5 0v-5.5h-5.5a.75.75 0 0 1 0-1.5h5.5v-5.5Z"/></svg>',
    send: '<svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true"><path fill="currentColor" d="M1.29 1.17a1 1 0 0 1 1.05-.1l12 6a1 1 0 0 1 0 1.79l-12 6A1 1 0 0 1 .9 13.8l1.24-4.56 6.48-1.24-6.48-1.24L.9 2.2a1 1 0 0 1 .39-1.03Z"/></svg>',
    back: '<svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true"><path fill="currentColor" d="M7.78 12.53a.75.75 0 0 1-1.06 0l-4-4a.75.75 0 0 1 0-1.06l4-4a.75.75 0 0 1 1.06 1.06L5.06 7.25h7.19a.75.75 0 0 1 0 1.5H5.06l2.72 2.72a.75.75 0 0 1 0 1.06Z"/></svg>',
    report: '<svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true"><path fill="currentColor" d="M3.75 1.5h5.5c.2 0 .39.08.53.22l3.5 3.5c.14.14.22.33.22.53v8.5a.75.75 0 0 1-.75.75h-9.5a.75.75 0 0 1-.75-.75v-12.5a.75.75 0 0 1 .75-.75Zm.25 1v11h8V6.06L8.94 3H4Zm1.75 5.25A.75.75 0 0 1 6.5 7h3a.75.75 0 0 1 0 1.5h-3a.75.75 0 0 1-.75-.75Zm0 3A.75.75 0 0 1 6.5 10h3a.75.75 0 0 1 0 1.5h-3a.75.75 0 0 1-.75-.75Z"/></svg>',
    close: '<svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true"><path fill="currentColor" d="M3.72 3.72a.75.75 0 0 1 1.06 0L8 6.94l3.22-3.22a.75.75 0 1 1 1.06 1.06L9.06 8l3.22 3.22a.75.75 0 1 1-1.06 1.06L8 9.06l-3.22 3.22a.75.75 0 0 1-1.06-1.06L6.94 8 3.72 4.78a.75.75 0 0 1 0-1.06Z"/></svg>',
    copy: '<svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true"><path fill="currentColor" d="M0 6.75C0 5.78.78 5 1.75 5h1.5a.75.75 0 0 1 0 1.5h-1.5a.25.25 0 0 0-.25.25v7.5c0 .14.11.25.25.25h7.5a.25.25 0 0 0 .25-.25v-1.5a.75.75 0 0 1 1.5 0v1.5C11 15.22 10.22 16 9.25 16h-7.5C.78 16 0 15.22 0 14.25v-7.5Z"/><path fill="currentColor" d="M5 1.75C5 .78 5.78 0 6.75 0h7.5C15.22 0 16 .78 16 1.75v7.5C16 10.22 15.22 11 14.25 11h-7.5C5.78 11 5 10.22 5 9.25v-7.5Zm1.75-.25a.25.25 0 0 0-.25.25v7.5c0 .14.11.25.25.25h7.5a.25.25 0 0 0 .25-.25v-7.5a.25.25 0 0 0-.25-.25h-7.5Z"/></svg>',
    download: '<svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true"><path fill="currentColor" d="M7.25 1.75a.75.75 0 0 1 1.5 0v7.69l2.72-2.72a.75.75 0 1 1 1.06 1.06l-4 4a.75.75 0 0 1-1.06 0l-4-4a.75.75 0 0 1 1.06-1.06l2.72 2.72V1.75ZM2.5 13.75a.75.75 0 0 1 .75-.75h9.5a.75.75 0 0 1 0 1.5h-9.5a.75.75 0 0 1-.75-.75Z"/></svg>',
    external: '<svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true"><path fill="currentColor" d="M3.75 2h3.5a.75.75 0 0 1 0 1.5H4.5v7h7v-2.75a.75.75 0 0 1 1.5 0v3.5a.75.75 0 0 1-.75.75h-8.5a.75.75 0 0 1-.75-.75v-8.5A.75.75 0 0 1 3.75 2Zm9.69 0H9.5a.75.75 0 0 1 0-1.5h4.75a.75.75 0 0 1 .75.75V6a.75.75 0 0 1-1.5 0V2.56L8.28 7.78a.75.75 0 1 1-1.06-1.06L12.44 1.5Z"/></svg>',
    retry: '<svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true"><path fill="currentColor" d="M1.22 8.27a.75.75 0 0 1 1.06 0l1.27 1.27a5.5 5.5 0 0 1 10.4-2.04.75.75 0 1 1-1.38.59 4 4 0 0 0-7.61 1.18l1.5-1.18a.75.75 0 0 1 1.06 1.06l-2.83 2.83a.75.75 0 0 1-1.06 0L1.22 9.33a.75.75 0 0 1 0-1.06Zm12.5-2.04a.75.75 0 0 1-1.06 0l-1.27-1.27a5.5 5.5 0 0 0-10.4 2.04.75.75 0 0 0 1.38.59 4 4 0 0 1 7.61-1.18l-1.5 1.18a.75.75 0 1 0 1.06 1.06l2.83-2.83a.75.75 0 0 0 0-1.06l-1.66-1.66Z"/></svg>',
    rename: '<svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true"><path fill="currentColor" d="M11.013 1.427a1.75 1.75 0 0 1 2.474 0l1.086 1.086a1.75 1.75 0 0 1 0 2.474l-8.61 8.61c-.21.21-.47.364-.756.445l-3.251.93a.75.75 0 0 1-.927-.928l.929-3.25c.081-.286.235-.547.445-.758l8.61-8.61Z"/></svg>',
    archive: '<svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true"><path fill="currentColor" d="M1.75 2.5a.75.75 0 0 0 0 1.5h12.5a.75.75 0 0 0 0-1.5H1.75ZM2 6.25A.75.75 0 0 1 2.75 5.5h10.5a.75.75 0 0 1 .75.75v7.5a.75.75 0 0 1-.75.75H2.75a.75.75 0 0 1-.75-.75v-7.5ZM7 7.5a.75.75 0 0 0 0 1.5h2a.75.75 0 0 0 0-1.5H7Z"/></svg>',
    restore: '<svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true"><path fill="currentColor" d="M1.22 8.27a.75.75 0 0 1 1.06 0l1.27 1.27a5.5 5.5 0 0 1 10.4-2.04.75.75 0 1 1-1.38.59 4 4 0 0 0-7.61 1.18l1.5-1.18a.75.75 0 0 1 1.06 1.06l-2.83 2.83a.75.75 0 0 1-1.06 0L1.22 9.33a.75.75 0 0 1 0-1.06Z"/></svg>',
    delete: '<svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true"><path fill="currentColor" d="M11 1.75V3h2.25a.75.75 0 0 1 0 1.5H2.75a.75.75 0 0 1 0-1.5H5V1.75C5 .78 5.78 0 6.75 0h2.5C10.22 0 11 .78 11 1.75ZM4.496 6.675l.66 6.6a.25.25 0 0 0 .249.225h5.19a.25.25 0 0 0 .249-.225l.66-6.6a.75.75 0 0 1 1.492.149l-.66 6.6A1.75 1.75 0 0 1 10.595 15h-5.19a1.75 1.75 0 0 1-1.741-1.575l-.66-6.6a.75.75 0 0 1 1.492-.15Z"/></svg>',
    menu: '<svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true"><path fill="currentColor" d="M2.75 2.5a.75.75 0 0 0 0 1.5h10.5a.75.75 0 0 0 0-1.5H2.75Zm0 4.75a.75.75 0 0 0 0 1.5h10.5a.75.75 0 0 0 0-1.5H2.75Zm0 4.75a.75.75 0 0 0 0 1.5h10.5a.75.75 0 0 0 0-1.5H2.75Z"/></svg>',
    sun: '<svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true"><path fill="currentColor" d="M8 10.5a2.5 2.5 0 1 0 0-5 2.5 2.5 0 0 0 0 5ZM8 0a.75.75 0 0 1 .75.75V2a.75.75 0 0 1-1.5 0V.75A.75.75 0 0 1 8 0Zm0 13.25a.75.75 0 0 1 .75.75v1.25a.75.75 0 0 1-1.5 0V14a.75.75 0 0 1 .75-.75ZM16 8a.75.75 0 0 1-.75.75H14a.75.75 0 0 1 0-1.5h1.25A.75.75 0 0 1 16 8ZM2.75 8a.75.75 0 0 1-.75.75H.75a.75.75 0 0 1 0-1.5H2A.75.75 0 0 1 2.75 8Z"/></svg>',
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
    return escaped.split("\n").map(function (line) {
      if (line.startsWith("+++")) return `<span class="diff-add-file">${line}</span>`;
      if (line.startsWith("---")) return `<span class="diff-del-file">${line}</span>`;
      if (line.startsWith("+")) return `<span class="diff-add">${line}</span>`;
      if (line.startsWith("-")) return `<span class="diff-del">${line}</span>`;
      if (line.startsWith("@@")) return `<span class="diff-hunk">${line}</span>`;
      return `<span class="diff-ctx">${line}</span>`;
    }).join("\n");
  }

  function buildGitHubUrl(session, path, lines) {
    if (!session || !session.owner || !session.repo) return null;
    const base = `https://github.com/${session.owner}/${session.repo}`;
    if (!path) return base;
    const linePart = lines ? String(lines).replace(/[^\dL,\-]/g, "") : "";
    if (!linePart) return `${base}/blob/HEAD/${path}`;
    return `${base}/blob/HEAD/${path}#L${linePart}`;
  }

  const ns = {
    apiJson,
    escapeHtml,
    escapeAttr,
    safeClass,
    translate,
    applyI18n,
    formatDuration,
    formatRelativeTime,
    svgIcon,
    copyToClipboard,
    downloadFile,
    highlightDiff,
    buildGitHubUrl,
  };

  window.IssueAgent = ns;
  // 向后兼容：保留旧的全局导出
  window.apiJson = apiJson;
  window.escapeHtml = escapeHtml;
})();
