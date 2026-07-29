/*
 * enhancements.js — 交互体验增强层
 * 所有新功能（应用内设置 / 命令面板 / 快捷键帮助 / 后端健康灯 / 重新生成 /
 * 报告内搜索 / Hero 真实入口 / 批量对比 / 图表无障碍回退 / 流式预期提示）
 * 集中于此，复用 core.js 暴露的 IA.* helper，仅在 app.js 做了最小侵入式钩子。
 *
 * 动效素材参考 ReactBits：Dot Field（点阵背景）、Spotlight（光标聚光）、
 * Animated List（列表逐项入场）均以原生 CSS/JS 复刻，风格与现有 motion.js 一致。
 */
(function () {
  "use strict";

  const IA = window.IssueAgent;
  if (!IA) return;
  const t = IA.translate;
  const esc = IA.escapeHtml;
  const svg = IA.svgIcon;

  // 持久化的设置（localStorage），结构与后端可覆盖字段对齐
  const SETTINGS_KEY = "iaSettings";
  function loadSettings() {
    try {
      return Object.assign({ language: "zh", model: "", thinking: "enabled", reasoning_effort: "high", review: true }, JSON.parse(localStorage.getItem(SETTINGS_KEY) || "{}"));
    } catch (e) {
      return { language: "zh", model: "", thinking: "enabled", reasoning_effort: "high", review: true };
    }
  }
  function saveSettings(s) {
    try { localStorage.setItem(SETTINGS_KEY, JSON.stringify(s)); } catch (e) { /* ignore */ }
  }
  let settings = loadSettings();

  // 同步到 window.IA_SETTINGS，供 app.js 的 analyze/chat 请求合并覆盖
  function syncWindowSettings() {
    const ov = {};
    if (settings.language) ov.language = settings.language;
    if (settings.model) ov.model = settings.model;
    if (settings.thinking) ov.thinking = settings.thinking;
    if (settings.reasoning_effort) ov.reasoning_effort = settings.reasoning_effort;
    if (settings.review !== null && settings.review !== undefined) ov.review = !!settings.review;
    window.IA_SETTINGS = ov;
  }
  syncWindowSettings();

  function el(id) { return document.getElementById(id); }
  function make(tag, cls, html) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (html !== undefined) e.innerHTML = html;
    return e;
  }
  function toast(msg) {
    let t2 = el("ia-toast");
    if (!t2) {
      t2 = make("div", "", "");
      t2.id = "ia-toast";
      document.body.appendChild(t2);
    }
    t2.textContent = msg;
    t2.classList.add("show");
    clearTimeout(t2.__timer);
    t2.__timer = setTimeout(function () { t2.classList.remove("show"); }, 2600);
  }

  /* ───────────────────────── 应用内设置面板 ───────────────────────── */
  let settingsPanel = null;
  // 焦点恢复：overlay 打开前记录活动元素，关闭后恢复，保持键盘导航上下文
  let overlayLastFocus = null;
  function buildSettingsPanel() {
    const panel = make("aside", "drawer");
    panel.id = "settings-panel";
    panel.setAttribute("aria-hidden", "true");
    panel.setAttribute("aria-label", t("settings_title"));
    panel.innerHTML =
      '<div class="drawer-backdrop" data-close></div>' +
      '<div class="drawer-surface" role="dialog" aria-modal="true">' +
        '<header class="drawer-head">' +
          '<h2>' + esc(t("settings_title")) + '</h2>' +
          '<button class="drawer-close" type="button" data-close aria-label="' + esc(t("settings_close")) + '">×</button>' +
        '</header>' +
        '<div class="drawer-body">' +
          '<label class="drawer-field"><span>' + esc(t("settings_language")) + '</span>' +
            '<select id="set-language"><option value="zh">' + esc(t("settings_lang_zh")) + '</option><option value="en">' + esc(t("settings_lang_en")) + '</option></select></label>' +
          '<label class="drawer-field"><span>' + esc(t("settings_model")) + '</span>' +
            '<input id="set-model" type="text" maxlength="128" placeholder="' + esc(t("settings_model_placeholder")) + '" autocomplete="off"></label>' +
          '<label class="drawer-field"><span>' + esc(t("settings_thinking")) + '</span>' +
            '<select id="set-thinking"><option value="enabled">' + esc(t("settings_thinking_enabled")) + '</option><option value="disabled">' + esc(t("settings_thinking_disabled")) + '</option></select></label>' +
          '<label class="drawer-field"><span>' + esc(t("settings_effort")) + '</span>' +
            '<select id="set-effort"><option value="high">high</option><option value="max">max</option></select></label>' +
          '<label class="drawer-field"><span>' + esc(t("settings_review")) + '</span>' +
            '<select id="set-review"><option value="1">' + esc(t("settings_on")) + '</option><option value="0">' + esc(t("settings_off")) + '</option></select></label>' +
          '<p class="drawer-note">' + esc(t("settings_note")) + '</p>' +
        '</div>' +
      '</div>';
    document.body.appendChild(panel);
    panel.querySelectorAll("[data-close]").forEach(function (b) {
      b.addEventListener("click", closeSettings);
    });
    // 控件回填
    el("set-language").value = settings.language || "zh";
    el("set-model").value = settings.model || "";
    el("set-thinking").value = settings.thinking || "enabled";
    el("set-effort").value = settings.reasoning_effort || "high";
    el("set-review").value = settings.review === false ? "0" : "1";
    // 变更处理
    el("set-language").addEventListener("change", function (e) {
      settings.language = e.target.value;
      saveSettings(settings); syncWindowSettings();
      applyLanguage(settings.language);
    });
    ["set-model", "set-thinking", "set-effort", "set-review"].forEach(function (id) {
      el(id).addEventListener("change", function (e) {
        if (id === "set-model") settings.model = e.target.value.trim();
        else if (id === "set-thinking") settings.thinking = e.target.value;
        else if (id === "set-effort") settings.reasoning_effort = e.target.value;
        else if (id === "set-review") settings.review = e.target.value === "1";
        saveSettings(settings); syncWindowSettings();
      });
    });
    return panel;
  }
  function openSettings() {
    if (!settingsPanel) settingsPanel = buildSettingsPanel();
    overlayLastFocus = document.activeElement;
    settingsPanel.setAttribute("aria-hidden", "false");
    document.body.classList.add("drawer-open");
    setTimeout(function () { const s = el("set-language"); if (s) s.focus(); }, 60);
  }
  function closeSettings() {
    if (settingsPanel) settingsPanel.setAttribute("aria-hidden", "true");
    document.body.classList.remove("drawer-open");
    if (overlayLastFocus && typeof overlayLastFocus.focus === "function") {
      try { overlayLastFocus.focus(); } catch (e) { /* ignore */ }
      overlayLastFocus = null;
    }
  }

  // 运行时热切换语言：拉取目标语言字符串表 → 更新 IA 内部表 → 重渲染界面与动态内容
  let langSwitching = false;
  async function applyLanguage(lang) {
    if (langSwitching) return;
    langSwitching = true;
    try {
      const strings = await IA.apiJson("/i18n?lang=" + encodeURIComponent(lang));
      IA.updateI18n(strings);
      IA.applyI18n(document);
      document.documentElement.lang = lang;
      // 重渲染动态内容：历史列表 与 当前会话（含报告标签）
      if (IA.loadSessions) IA.loadSessions();
      if (IA.sessionId && IA.restoreSession) {
        await IA.restoreSession(IA.sessionId);
      }
      toast(t("settings_language") + ": " + (lang === "zh" ? "中文" : "English"));
    } catch (e) {
      toast(t("connection_error") + (e.message || ""));
    } finally {
      langSwitching = false;
    }
  }

  /* ───────────────────────── 命令面板 (Cmd/Ctrl+K) ───────────────────────── */
  let palette = null, paletteInput = null, paletteResults = null, paletteItems = [], paletteIndex = 0;
  function buildPalette() {
    const p = make("div", "palette");
    p.id = "command-palette";
    p.setAttribute("aria-hidden", "true");
    p.setAttribute("aria-label", t("palette_placeholder"));
    p.innerHTML =
      '<div class="palette-spotlight" aria-hidden="true"></div>' +
      '<div class="palette-surface" role="dialog" aria-modal="true">' +
        '<div class="palette-search">' + svg("search") +
          '<input id="palette-input" class="palette-input" type="text" autocomplete="off" placeholder="' + esc(t("palette_placeholder")) + '">' +
        '</div>' +
        '<div id="palette-results" class="palette-results"></div>' +
        '<div class="palette-footer"><span>' + esc(t("palette_hint")) + '</span></div>' +
      '</div>';
    document.body.appendChild(p);
    paletteInput = el("palette-input");
    paletteResults = el("palette-results");
    paletteInput.addEventListener("input", function () { renderPalette(paletteInput.value); });
    paletteInput.addEventListener("keydown", onPaletteKey);
    // 光标聚光（ReactBits Spotlight 复刻）
    p.addEventListener("mousemove", function (e) {
      const r = p.getBoundingClientRect();
      p.style.setProperty("--mx", (e.clientX - r.left) + "px");
      p.style.setProperty("--my", (e.clientY - r.top) + "px");
    });
    p.addEventListener("click", function (e) { if (e.target === p || e.target.classList.contains("palette-spotlight")) closePalette(); });
    return p;
  }
  function paletteActions() {
    return [
      { id: "new", label: t("palette_new_analysis"), icon: "plus", run: function () { const i = el("issueUrl"); if (i) { i.value = ""; i.focus(); } } },
      { id: "theme", label: t("palette_toggle_theme"), icon: "sun", run: function () { const b = el("theme-toggle-btn"); if (b) b.click(); } },
      { id: "archive", label: t("palette_toggle_archive"), icon: "archive", run: function () { const b = el("archive-toggle"); if (b) b.click(); } },
      { id: "settings", label: t("palette_open_settings"), icon: "rename", run: function () { openSettings(); } },
      { id: "help", label: t("palette_help"), icon: "alert", run: function () { openHelp(); } },
      { id: "import", label: t("palette_import"), icon: "download", run: function () { const b = el("import-session-btn"); if (b) b.click(); } },
      { id: "export", label: t("palette_export_current"), icon: "download", run: function () { if (IA.sessionId) downloadSession(IA.sessionId); else toast(t("no_report_default")); } },
    ];
  }
  function fuzzyMatch(q, text) {
    q = q.trim().toLowerCase();
    if (!q) return true;
    text = String(text).toLowerCase();
    let i = 0;
    for (let c = 0; c < text.length && i < q.length; c++) {
      if (text[c] === q[i]) i++;
    }
    return i === q.length;
  }
  async function renderPalette(query) {
    if (!palette) return;
    let items = [];
    // 操作
    paletteActions().forEach(function (a) {
      if (fuzzyMatch(query, a.label)) items.push({ type: "action", data: a });
    });
    // 近期会话
    try {
      const sessions = await IA.apiJson("/sessions?archived=false&limit=12");
      (sessions || []).forEach(function (s) {
        const repo = s.owner && s.repo ? s.owner + "/" + s.repo : (s.issue_url || "");
        const label = (repo + (s.issue_number ? " #" + s.issue_number : "") + " " + (s.title || "")).trim();
        if (fuzzyMatch(query, label)) items.push({ type: "session", id: s.session_id, label: label, repo: repo, title: s.title || "" });
      });
    } catch (e) { /* ignore */ }
    paletteItems = items;
    paletteIndex = 0;
    if (!items.length) {
      paletteResults.innerHTML = '<div class="palette-empty">' + esc(t("palette_empty")) + '</div>';
      return;
    }
    let html = "";
    let sessionStarted = false, lastType = null, idx = 0;
    items.forEach(function (it) {
      if (lastType && lastType !== it.type) html += "</div>";
      if (lastType !== it.type) {
        const head = it.type === "session" ? t("palette_sessions") : t("palette_actions");
        html += '<div class="palette-group"><div class="palette-group-title">' + esc(head) + "</div>";
        lastType = it.type;
      }
      const icon = it.type === "action" ? (svg(it.data.icon) || "") : (svg("report") || "");
      const sub = it.type === "session" ? esc(it.title || "") : "";
      html += '<button type="button" class="palette-item" data-idx="' + (idx++) + '" style="--i:' + idx + '">' +
        '<span class="palette-item-icon" aria-hidden="true">' + icon + '</span>' +
        '<span class="palette-item-text"><span class="palette-item-label">' + esc(it.label) + '</span>' + (sub ? '<span class="palette-item-sub">' + sub + '</span>' : '') + '</span>' +
        '</button>';
    });
    if (lastType) html += "</div>";
    paletteResults.innerHTML = html;
    Array.prototype.forEach.call(paletteResults.querySelectorAll(".palette-item"), function (b) {
      b.addEventListener("mousemove", function () { setPaletteIndex(parseInt(b.dataset.idx, 10)); });
      b.addEventListener("click", function () { activatePalette(parseInt(b.dataset.idx, 10)); });
    });
    updatePaletteActive();
  }
  function setPaletteIndex(i) {
    if (i < 0) i = 0;
    if (i >= paletteItems.length) i = paletteItems.length - 1;
    paletteIndex = i;
    updatePaletteActive();
  }
  function updatePaletteActive() {
    const nodes = paletteResults.querySelectorAll(".palette-item");
    Array.prototype.forEach.call(nodes, function (n, i) {
      n.classList.toggle("active", i === paletteIndex);
      if (i === paletteIndex) n.scrollIntoView({ block: "nearest" });
    });
  }
  function onPaletteKey(e) {
    if (e.key === "ArrowDown") { e.preventDefault(); setPaletteIndex(paletteIndex + 1); }
    else if (e.key === "ArrowUp") { e.preventDefault(); setPaletteIndex(paletteIndex - 1); }
    else if (e.key === "Enter") { e.preventDefault(); activatePalette(paletteIndex); }
    else if (e.key === "Escape") { e.preventDefault(); closePalette(); }
  }
  function activatePalette(i) {
    const it = paletteItems[i];
    if (!it) return;
    closePalette();
    if (it.type === "action") it.data.run();
    else if (it.type === "session" && IA.restoreSession) IA.restoreSession(it.id);
  }
  function openPalette() {
    if (!palette) palette = buildPalette();
    overlayLastFocus = document.activeElement;
    palette.setAttribute("aria-hidden", "false");
    document.body.classList.add("palette-open");
    renderPalette("");
    setTimeout(function () { if (paletteInput) paletteInput.focus(); }, 60);
  }
  function closePalette() {
    if (palette) palette.setAttribute("aria-hidden", "true");
    document.body.classList.remove("palette-open");
    if (overlayLastFocus && typeof overlayLastFocus.focus === "function") {
      try { overlayLastFocus.focus(); } catch (e) { /* ignore */ }
      overlayLastFocus = null;
    }
  }
  async function downloadSession(id) {
    try {
      const resp = await fetch("/session/" + encodeURIComponent(id) + "/export");
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      const blob = await resp.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url; a.download = "session-" + id + ".json";
      document.body.appendChild(a); a.click(); a.remove();
      setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
    } catch (e) { toast(t("connection_error") + e.message); }
  }

  /* ───────────────────────── 快捷键帮助 (? / Cmd+K) ───────────────────────── */
  let helpOverlay = null;
  function buildHelp() {
    const o = make("div", "overlay");
    o.id = "help-overlay";
    o.setAttribute("aria-hidden", "true");
    o.innerHTML =
      '<div class="overlay-backdrop" data-close></div>' +
      '<div class="overlay-surface" role="dialog" aria-modal="true">' +
        '<header class="overlay-head"><h2>' + esc(t("help_title")) + '</h2><button class="drawer-close" type="button" data-close aria-label="' + esc(t("help_close")) + '">×</button></header>' +
        '<ul class="shortcut-list">' +
          shortcutRow("Ctrl/⌘ + K", t("shortcut_search")) +
          shortcutRow("Ctrl/⌘ + Enter", t("shortcut_send")) +
          shortcutRow("Esc", t("shortcut_cancel")) +
          shortcutRow("?", t("shortcut_help")) +
        '</ul>' +
      '</div>';
    document.body.appendChild(o);
    o.querySelectorAll("[data-close]").forEach(function (b) { b.addEventListener("click", closeHelp); });
    return o;
  }
  function shortcutRow(key, desc) {
    return '<li class="shortcut-row"><kbd>' + esc(key) + '</kbd><span>' + esc(desc) + '</span></li>';
  }
  function openHelp() {
    if (!helpOverlay) helpOverlay = buildHelp();
    overlayLastFocus = document.activeElement;
    helpOverlay.setAttribute("aria-hidden", "false");
  }
  function closeHelp() {
    if (helpOverlay) helpOverlay.setAttribute("aria-hidden", "true");
    if (overlayLastFocus && typeof overlayLastFocus.focus === "function") {
      try { overlayLastFocus.focus(); } catch (e) { /* ignore */ }
      overlayLastFocus = null;
    }
  }

  /* ───────────────────────── 后端健康灯 ───────────────────────── */
  let healthTimer = null;
  async function pollHealth() {
    const dot = el("health-dot");
    const btn = el("health-btn");
    if (!dot) return;
    dot.dataset.state = "checking";
    if (btn) { btn.title = t("health_checking"); btn.setAttribute("aria-label", t("health_checking")); }
    try {
      const r = await fetch("/health");
      if (r.ok) {
        dot.dataset.state = "ok";
        if (btn) { btn.title = t("health_ok"); btn.setAttribute("aria-label", t("health_ok")); }
      } else throw new Error("bad");
    } catch (e) {
      dot.dataset.state = "down";
      if (btn) { btn.title = t("health_down"); btn.setAttribute("aria-label", t("health_down")); }
    }
  }

  /* ───────────────────────── 报告内搜索 ───────────────────────── */
  let searchMarks = [], searchIndex = -1;
  function injectReportSearch() {
    const header = document.querySelector(".report-header-actions");
    if (!header || el("report-search-btn")) return;
    const btn = make("button", "report-action-btn");
    btn.id = "report-search-btn";
    btn.type = "button";
    btn.setAttribute("aria-label", t("report_search_placeholder"));
    btn.setAttribute("aria-pressed", "false");
    btn.title = t("report_search_placeholder");
    btn.innerHTML = '<svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true"><path fill="currentColor" d="M6.75 1.5a5.25 5.25 0 1 0 3.2 9.4l3.32 3.33a.75.75 0 1 0 1.06-1.06l-3.32-3.33A5.25 5.25 0 0 0 6.75 1.5ZM3.25 6.75a3.5 3.5 0 1 1 7 0 3.5 3.5 0 0 1-7 0Z"/></svg>';
    header.insertBefore(btn, el("report-close-btn"));
    btn.addEventListener("click", function () {
      const bar = el("report-search-bar");
      if (bar) { bar.remove(); btn.setAttribute("aria-pressed", "false"); return; }
      btn.setAttribute("aria-pressed", "true");
      const bar2 = make("div", "report-search-bar");
      bar2.id = "report-search-bar";
      bar2.innerHTML =
        '<input id="report-search-input" type="text" placeholder="' + esc(t("report_search_placeholder")) + '" aria-label="' + esc(t("report_search_placeholder")) + '">' +
        '<button type="button" id="report-search-prev" class="report-search-nav" aria-label="' + esc(t("report_search_prev")) + '">↑</button>' +
        '<button type="button" id="report-search-next" class="report-search-nav" aria-label="' + esc(t("report_search_next")) + '">↓</button>' +
        '<span id="report-search-count" class="report-search-count"></span>' +
        '<button type="button" id="report-search-close" class="report-search-nav" aria-label="' + esc(t("report_search_close")) + '">×</button>';
      header.parentNode.insertBefore(bar2, header.nextSibling);
      const input = el("report-search-input");
      input.focus();
      // 250ms 防抖：大报告 TreeWalker 全量扫描，每次按键触发会卡顿
      let searchTimer = null;
      input.addEventListener("input", function () {
        if (searchTimer) clearTimeout(searchTimer);
        searchTimer = setTimeout(function () {
          searchTimer = null;
          runReportSearch(input.value);
        }, 250);
      });
      el("report-search-prev").addEventListener("click", function () { gotoMark(-1); });
      el("report-search-next").addEventListener("click", function () { gotoMark(1); });
      el("report-search-close").addEventListener("click", function () { clearSearchMarks(); bar2.remove(); btn.setAttribute("aria-pressed", "false"); });
    });
  }
  function clearSearchMarks() {
    searchMarks.forEach(function (m) {
      const p = m.parentNode;
      if (p) p.replaceChild(document.createTextNode(m.textContent), m);
      if (p) p.normalize();
    });
    searchMarks = []; searchIndex = -1;
  }
  function runReportSearch(q) {
    clearSearchMarks();
    q = (q || "").trim();
    if (!q) { const c = el("report-search-count"); if (c) c.textContent = ""; return; }
    const root = el("report");
    if (!root) return;
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, null);
    const nodes = [];
    let n;
    while ((n = walker.nextNode())) {
      if (n.nodeValue && n.nodeValue.toLowerCase().indexOf(q.toLowerCase()) !== -1 && n.parentNode && !n.parentNode.closest("script,style")) nodes.push(n);
    }
    const ql = q.toLowerCase();
    nodes.forEach(function (node) {
      const text = node.nodeValue, lower = text.toLowerCase();
      const frag = document.createDocumentFragment();
      let from = 0, idx;
      while ((idx = lower.indexOf(ql, from)) !== -1) {
        if (idx > from) frag.appendChild(document.createTextNode(text.slice(from, idx)));
        const mark = document.createElement("mark");
        mark.className = "report-search-hit";
        mark.textContent = text.slice(idx, idx + q.length);
        searchMarks.push(mark);
        frag.appendChild(mark);
        from = idx + q.length;
      }
      if (from < text.length) frag.appendChild(document.createTextNode(text.slice(from)));
      node.parentNode.replaceChild(frag, node);
    });
    const c = el("report-search-count");
    if (c) c.textContent = searchMarks.length ? t("report_search_count", { n: searchMarks.length }) : t("report_search_none");
    if (searchMarks.length) { searchIndex = 0; scrollToMark(); }
  }
  function gotoMark(dir) {
    if (!searchMarks.length) return;
    searchIndex = (searchIndex + dir + searchMarks.length) % searchMarks.length;
    scrollToMark();
  }
  function scrollToMark() {
    if (searchIndex < 0 || searchIndex >= searchMarks.length) return;
    searchMarks.forEach(function (m, i) { m.classList.toggle("current", i === searchIndex); });
    searchMarks[searchIndex].scrollIntoView({ block: "center", behavior: "smooth" });
  }

  /* ───────────────────────── 图表无障碍回退（隐藏数据表） ───────────────────────── */
  function setupChartA11y() {
    const report = el("report");
    if (!report) return;
    const observer = new MutationObserver(function () {
      enhanceChartsA11y();
    });
    observer.observe(report, { childList: true, subtree: true });
    enhanceChartsA11y();
  }
  function enhanceChartsA11y() {
    const rep = IA.getReport && IA.getReport();
    if (!rep) return;
    const enumLabel = IA.enumLabel || function (prefix, value) { return value || ""; };
    // 根因证据链隐藏表
    const evidenceCanvas = el("report-evidence-map-chart");
    if (evidenceCanvas && !evidenceCanvas.dataset.a11y) {
      const evidenceRows = (rep.evidence || []).map(function (e) {
        return [
          e.path || "",
          e.lines || "",
          enumLabel("kind", e.kind || "code"),
          enumLabel("strength", e.strength || "moderate"),
          e.reason || "",
        ];
      });
      addChartTable(
        evidenceCanvas,
        t("chart_evidence_map"),
        [t("chart_table_path"), t("chart_table_lines"), t("evidence_kind_legend"), t("evidence_strength_legend"), t("chart_table_reason")],
        evidenceRows,
      );
    }
    // 风险矩阵隐藏表
    const riskCanvas = el("report-risk-matrix-chart");
    if (riskCanvas && !riskCanvas.dataset.a11y && rep.impact) {
      addChartTable(
        riskCanvas,
        t("chart_risk_matrix"),
        [t("report_severity"), t("report_likelihood")],
        [[enumLabel("severity", rep.impact.severity), enumLabel("likelihood", rep.impact.likelihood)]],
      );
    }
    // 波及范围隐藏表
    const blastCanvas = el("report-blast-radius-chart");
    if (blastCanvas && !blastCanvas.dataset.a11y && rep.impact) {
      let modules = (rep.impact.blast_radius || []).slice();
      if (!modules.length) modules = parsePatchStat(rep.patch).map(function (row) { return row[0]; });
      const seen = {};
      const blastRows = modules.filter(function (name) {
        if (!name || seen[name]) return false;
        seen[name] = true;
        return true;
      }).map(function (name) {
        return [name, enumLabel("severity", rep.impact.severity)];
      });
      addChartTable(blastCanvas, t("chart_blast_radius"), [t("chart_table_module"), t("report_severity")], blastRows);
    }
    // diffstat 隐藏表
    const diffCanvas = el("report-diffstat-chart");
    if (diffCanvas && !diffCanvas.dataset.a11y) {
      const rows = parsePatchStat(rep.patch);
      addChartTable(diffCanvas, t("diffstat_chart_title"), [t("chart_table_path"), t("diffstat_added"), t("diffstat_removed")], rows);
    }
    // verify 隐藏表
    const verifyCanvas = el("report-verify-chart");
    if (verifyCanvas && !verifyCanvas.dataset.a11y) {
      const ev = (rep.evidence || []).map(function (e) { return [e.path, e.lines || "", e.reason || ""]; });
      addChartTable(verifyCanvas, t("verify_chart_title"), [t("chart_table_path"), t("chart_table_lines"), t("chart_table_reason")], ev);
    }
  }
  function addChartTable(canvas, caption, headers, rows) {
    canvas.dataset.a11y = "1";
    const table = buildSrOnlyTable(caption, headers, rows);
    table.id = canvas.id + "-data";
    canvas.setAttribute("aria-describedby", table.id);
    canvas.parentNode.insertBefore(table, canvas.nextSibling);
  }
  function parsePatchStat(patch) {
    if (!patch) return [];
    const rows = [];
    const files = {};
    patch.split("\n").forEach(function (line) {
      const m = line.match(/^diff --git a\/(.+?) b\/(.+?)$/);
      if (m) files[m[2]] = { add: 0, del: 0 };
    });
    const lines = patch.split("\n");
    let cur = null;
    lines.forEach(function (line) {
      const dm = line.match(/^diff --git a\/(.+?) b\/(.+?)$/);
      if (dm) { cur = dm[2]; if (!files[cur]) files[cur] = { add: 0, del: 0 }; return; }
      if (!cur) return;
      const am = line.match(/^@@ .*?\+(.+?) .*?@@/);
      if (line[0] === "+" && !line.startsWith("+++")) files[cur].add++;
      else if (line[0] === "-" && !line.startsWith("---")) files[cur].del++;
    });
    Object.keys(files).forEach(function (f) { rows.push([f, String(files[f].add), String(files[f].del)]); });
    return rows;
  }
  function buildSrOnlyTable(caption, headers, rows) {
    const tbl = make("table", "sr-only");
    tbl.setAttribute("aria-label", caption);
    let html = "<caption>" + esc(caption) + "</caption><thead><tr>";
    headers.forEach(function (h) { if (h) html += "<th scope='col'>" + esc(h) + "</th>"; });
    html += "</tr></thead><tbody>";
    rows.forEach(function (r) {
      html += "<tr>" + r.map(function (c) { return "<td>" + esc(c == null ? "" : c) + "</td>"; }).join("") + "</tr>";
    });
    html += "</tbody>";
    tbl.innerHTML = html;
    return tbl;
  }

  /* ───────────────────────── Hero 真实近期入口 ───────────────────────── */
  async function setupHeroRecent() {
    const list = document.querySelector(".hero-example-list");
    if (!list) return;
    try {
      const sessions = await IA.apiJson("/sessions?archived=false&limit=3");
      if (!sessions || !sessions.length) return;
      const html = sessions.map(function (s) {
        const repo = s.owner && s.repo ? s.owner + "/" + s.repo : (s.issue_url || "");
        const url = s.issue_url || "";
        return '<button type="button" class="hero-example" data-hero-url="' + esc(url) + '">' +
          '<span class="hero-example-repo">' + esc(repo) + (s.issue_number ? " #" + esc(String(s.issue_number)) : "") + '</span>' +
          '<span class="hero-example-desc">' + esc(s.title || "") + '</span>' +
          '<span class="hero-example-cta">' + esc(t("hero_start_button")) + ' →</span></button>';
      }).join("");
      // 在标题后插入"你最近的排查"分组
      const head = document.querySelector(".hero-examples-head");
      if (head) {
        const sub = head.querySelector(".hero-examples-desc");
        if (sub) sub.textContent = t("hero_recent_title");
      }
      list.innerHTML = html;
      Array.prototype.forEach.call(list.querySelectorAll(".hero-example"), function (btn) {
        if (IA.Motion && typeof IA.Motion.attachSpotlight === "function") {
          IA.Motion.attachSpotlight(btn, { intensity: 0.16, size: 320 });
        }
        btn.addEventListener("click", function () {
          const url = btn.dataset.heroUrl;
          if (!url) return;
          const inp = el("issueUrl");
          if (inp) inp.value = url;
          if (IA.analyze) IA.analyze();
        });
      });
    } catch (e) { /* ignore */ }
  }

  /* ───────────────────────── 批量对比 ───────────────────────── */
  let compareModal = null;
  function setupBatchCompare() {
    const list = el("history-list");
    if (!list) return;
    const obs = new MutationObserver(function () { injectCompareButton(); });
    obs.observe(list.parentNode, { childList: true, subtree: true });
    injectCompareButton();
  }
  function injectCompareButton() {
    const bar = el("batch-toolbar");
    if (!bar || el("batch-compare-btn")) return;
    const count = (IA.getSelectedSessions ? IA.getSelectedSessions().length : 0);
    if (count < 2) return;
    const btn = make("button", "batch-btn batch-compare");
    btn.id = "batch-compare-btn";
    btn.type = "button";
    btn.innerHTML = svg("report") + "<span>" + esc(t("compare_selected")) + "</span>";
    btn.addEventListener("click", openCompare);
    bar.appendChild(btn);
  }
  async function openCompare() {
    const ids = IA.getSelectedSessions ? IA.getSelectedSessions() : [];
    if (ids.length < 2) { toast(t("compare_empty")); return; }
    const sessions = [];
    for (const id of ids) {
      try { sessions.push(await IA.apiJson("/session/" + encodeURIComponent(id))); } catch (e) { /* ignore */ }
    }
    if (!compareModal) compareModal = buildCompareModal();
    const body = el("compare-body");
    body.innerHTML = sessions.map(function (s) {
      const rep = s.report || {};
      const repo = s.owner && s.repo ? s.owner + "/" + s.repo : (s.issue_url || "");
      const conf = rep.confidence ? '<span class="badge ' + IA.safeClass(rep.confidence) + '">' + esc(IA.enumLabel("confidence", rep.confidence)) + "</span>" : "";
      return '<div class="compare-col">' +
        '<div class="compare-col-head">' + esc(repo) + (s.issue_number ? " #" + esc(String(s.issue_number)) : "") + "</div>" +
        '<div class="compare-title">' + esc(s.title || "") + "</div>" +
        '<h4>' + esc(t("compare_summary")) + "</h4><p>" + esc(rep.summary || t("no_report_default")) + "</p>" +
        '<h4>' + esc(t("compare_root")) + "</h4><p>" + esc(rep.root_cause || "—") + "</p>" +
        "<div class='compare-conf'>" + esc(t("compare_confidence")) + ": " + conf + "</div>" +
        "</div>";
    }).join("");
    compareModal.setAttribute("aria-hidden", "false");
  }
  function buildCompareModal() {
    const o = make("div", "overlay");
    o.id = "compare-modal";
    o.setAttribute("aria-hidden", "true");
    o.innerHTML =
      '<div class="overlay-backdrop" data-close></div>' +
      '<div class="overlay-surface compare-surface" role="dialog" aria-modal="true">' +
        '<header class="overlay-head"><h2>' + esc(t("compare_title")) + '</h2><button class="drawer-close" type="button" data-close aria-label="' + esc(t("compare_close")) + '">×</button></header>' +
        '<div id="compare-body" class="compare-body"></div>' +
      '</div>';
    document.body.appendChild(o);
    o.querySelectorAll("[data-close]").forEach(function (b) { b.addEventListener("click", function () { o.setAttribute("aria-hidden", "true"); }); });
    return o;
  }

  /* ───────────────────────── 重新生成 ───────────────────────── */
  function setupRegenerate() {
    const messages = el("messages");
    if (!messages) return;
    const obs = new MutationObserver(function () { maybeAddRegenerate(); });
    obs.observe(messages, { childList: true, subtree: false });
    maybeAddRegenerate();
  }
  function maybeAddRegenerate() {
    const inputBar = el("input-bar");
    if (!inputBar || inputBar.style.display === "none") return;
    if (!IA.sessionId) return;
    const msgs = document.querySelectorAll("#messages > .msg.assistant");
    if (!msgs.length) return;
    const last = msgs[msgs.length - 1];
    if (last.querySelector(".regenerate-btn")) return;
    const btn = make("button", "msg-action-btn regenerate-btn");
    btn.type = "button";
    btn.setAttribute("aria-label", t("regenerate_label"));
    btn.innerHTML = svg("retry") + "<span>" + esc(t("regenerate")) + "</span>";
    btn.addEventListener("click", function () {
      const users = document.querySelectorAll("#messages > .msg.user");
      if (!users.length) return;
      const lastUser = users[users.length - 1];
      const text = lastUser.textContent || "";
      if (last && last.parentNode) last.parentNode.removeChild(last);
      window.__iaRegenerate = true;
      try { if (IA.chat) IA.chat(text); } finally { delete window.__iaRegenerate; }
    });
    const actions = last.querySelector(".msg-actions") || last;
    actions.appendChild(btn);
  }

  /* ───────────────────────── 流式预期提示 + 断线恢复 ───────────────────────── */
  function setupStreamingHints() {
    const origAnalyze = IA.analyze;
    if (typeof origAnalyze !== "function") return;
    IA.analyze = function () {
      const hint = IA.addMsg ? IA.addMsg("system", t("expecting_thinking")) : null;
      // 报告生成后（report-toggle 出现）移除提示
      const toggle = el("report-toggle");
      let removed = false;
      const removeHint = function () {
        if (removed || !hint || !hint.parentNode) return;
        removed = true;
        hint.parentNode.removeChild(hint);
        if (observer) observer.disconnect();
      };
      let observer = null;
      if (toggle) {
        observer = new MutationObserver(function () {
          if (toggle.style.display !== "none") removeHint();
        });
        observer.observe(toggle, { attributes: true, attributeFilter: ["style"] });
      }
      setTimeout(removeHint, 25000);
      const p = origAnalyze.apply(this, arguments);
      if (p && typeof p.then === "function") {
        p.then(removeHint).catch(function () { recoverIfReportReady(); removeHint(); });
      }
      return p;
    };
  }
  // 分析连接断开但服务端可能已完成：若会话已有报告则恢复展示
  async function recoverIfReportReady() {
    if (!IA.sessionId) return;
    try {
      const rep = await IA.apiJson("/session/" + encodeURIComponent(IA.sessionId) + "/report");
      if (rep && IA.restoreSession) { IA.restoreSession(IA.sessionId); toast(t("retry_analysis")); }
    } catch (e) { /* no report yet */ }
  }

  /* ───────────────────────── 全局快捷键 ───────────────────────── */
  function setupGlobalKeys() {
    document.addEventListener("keydown", function (e) {
      const mod = e.ctrlKey || e.metaKey;
      // Cmd/Ctrl+K：拦截并打开命令面板（阻止 app.js 默认的"聚焦搜索"）
      if (mod && (e.key === "k" || e.key === "K")) {
        e.preventDefault();
        e.stopImmediatePropagation();
        if (palette && palette.getAttribute("aria-hidden") === "false") closePalette();
        else openPalette();
        return;
      }
      // ? 打开帮助（输入框/文本域中不打断输入）
      if (e.key === "?" && !mod) {
        const tag = (e.target && e.target.tagName) || "";
        if (tag === "INPUT" || tag === "TEXTAREA") return;
        e.preventDefault();
        if (helpOverlay && helpOverlay.getAttribute("aria-hidden") === "false") closeHelp();
        else openHelp();
      }
    }, true);
    document.addEventListener("keydown", function (e) {
      if (e.key !== "Escape") return;
      // 统一 ESC 调度：按层级从上到下关闭最上层的 overlay，关闭后阻止其他监听器
      // 避免一次 ESC 同时关闭 overlay 和背景报告面板
      if (palette && palette.getAttribute("aria-hidden") === "false") {
        closePalette();
        e.stopImmediatePropagation();
        return;
      }
      if (helpOverlay && helpOverlay.getAttribute("aria-hidden") === "false") {
        closeHelp();
        e.stopImmediatePropagation();
        return;
      }
      if (compareModal && compareModal.getAttribute("aria-hidden") === "false") {
        compareModal.setAttribute("aria-hidden", "true");
        e.stopImmediatePropagation();
        return;
      }
      if (settingsPanel && settingsPanel.getAttribute("aria-hidden") === "false") {
        closeSettings();
        e.stopImmediatePropagation();
        return;
      }
    });
  }

  /* ───────────────────────── 初始化 ───────────────────────── */
  function init() {
    // 设置按钮
    const sBtn = el("settings-btn");
    if (sBtn) sBtn.addEventListener("click", openSettings);
    // 健康灯
    const hBtn = el("health-btn");
    if (hBtn) hBtn.addEventListener("click", pollHealth);
    pollHealth();
    if (healthTimer) clearInterval(healthTimer);
    healthTimer = setInterval(pollHealth, 15000);
    // 报告内搜索 / 图表无障碍：报告内容变化或主布局打开报告时挂载。
    // report-open 类在 #main 上，不在 #report-panel 上。
    const reportPanel = el("report-panel");
    const main = el("main");
    const report = el("report");
    if (reportPanel && main && report) {
      const enhanceOpenReport = function () {
        if (!main.classList.contains("report-open")) return;
        injectReportSearch();
        enhanceChartsA11y();
      };
      const layoutObserver = new MutationObserver(enhanceOpenReport);
      layoutObserver.observe(main, { attributes: true, attributeFilter: ["class"] });
      const reportObserver = new MutationObserver(enhanceOpenReport);
      reportObserver.observe(report, { childList: true, subtree: true });
      setupChartA11y();
      enhanceOpenReport();
    }
    // Hero 真实入口
    setupHeroRecent();
    // 批量对比
    setupBatchCompare();
    // 重新生成
    setupRegenerate();
    // 流式预期 + 恢复
    setupStreamingHints();
    // 全局快捷键
    setupGlobalKeys();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
