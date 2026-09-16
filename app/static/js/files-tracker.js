/* FilesTracker.js — E28 实时文件追踪模块（IA.FilesTracker）
 * 从工具调用参数提取文件路径，按目录分组实时渲染追踪面板
 * 独立模块：自 app.js 拆出（C1 约束），app.js 经 const 别名委托调用。
 */
(function () {
  "use strict";

  const IA = window.IssueAgent;
  const t = IA.translate;

  // E28: 实时文件追踪 — 记录 Agent 调查过程中浏览过的文件，按目录分组展示
  let exploredFiles = null; // Map<path, {tool: string, count: number, time: number}>

  // ── E28 实时文件树浏览 ─────────────────────────────────
  // 从工具调用参数中提取文件路径。支持 read_file/grep_content/list_dir 等常见工具
  function extractFilePaths(toolName, args) {
    const paths = [];
    if (!args || typeof args !== "object") return paths;
    // 常见字段名：file_path / path / file / filepath
    const directFields = ["file_path", "path", "file", "filepath", "filename"];
    for (const f of directFields) {
      const v = args[f];
      if (typeof v === "string" && v.trim() && !v.includes("*")) {
        paths.push(v.trim());
      }
    }
    // grep_content 的 path 字段可能是文件或目录，都记录
    // 多文件字段：files / paths
    const multiFields = ["files", "paths"];
    for (const f of multiFields) {
      const v = args[f];
      if (Array.isArray(v)) {
        v.forEach(function (p) {
          if (typeof p === "string" && p.trim()) paths.push(p.trim());
        });
      }
    }
    return paths;
  }

  // 规范化路径：去除 ./ 前缀，去除首尾空白
  function normalizePath(p) {
    if (!p) return "";
    let s = String(p).trim();
    if (s.startsWith("./")) s = s.slice(2);
    return s;
  }

  // 重置文件追踪状态，移除旧卡片
  function resetFilesTracker() {
    exploredFiles = new Map();
    const old = document.querySelector(".files-tracker");
    if (old) old.remove();
  }

  // 记录一个被浏览的文件，并更新追踪面板
  function trackExploredFile(toolName, args) {
    if (!exploredFiles) exploredFiles = new Map();
    const paths = extractFilePaths(toolName, args);
    if (!paths.length) return;
    let changed = false;
    const now = Date.now();
    paths.forEach(function (raw) {
      const p = normalizePath(raw);
      if (!p || p.length > 512) return; // 跳过异常长路径
      const existing = exploredFiles.get(p);
      if (existing) {
        existing.count += 1;
        existing.time = now;
      } else {
        exploredFiles.set(p, { tool: toolName, count: 1, time: now });
        changed = true;
      }
    });
    if (changed || paths.length) updateFilesTracker();
  }

  // 按目录分组文件路径
  function groupFilesByDir(files) {
    const groups = {};
    files.forEach(function (p) {
      const idx = p.lastIndexOf("/");
      const dir = idx > 0 ? p.slice(0, idx) : "(root)";
      const name = idx > 0 ? p.slice(idx + 1) : p;
      if (!groups[dir]) groups[dir] = [];
      groups[dir].push(name);
    });
    return groups;
  }

  // 渲染或更新文件追踪面板
  function updateFilesTracker() {
    const container = document.getElementById("messages");
    if (!container) return;
    let card = container.querySelector(".files-tracker");
    const files = exploredFiles ? Array.from(exploredFiles.keys()).sort() : [];
    const dirs = Object.keys(groupFilesByDir(files));

    if (!card) {
      card = document.createElement("section");
      card.className = "msg assistant files-tracker";
      card.setAttribute("aria-label", t("files_tracker_title"));
      // 插入到对话区顶部（第一条消息之前）
      container.insertBefore(card, container.firstChild);
    }

    if (!files.length) {
      card.innerHTML =
        `<div class="files-tracker-header">` +
        `<span class="files-tracker-icon" aria-hidden="true">📁</span>` +
        `<span class="files-tracker-title">${IA.escapeHtml(t("files_tracker_title"))}</span>` +
        `<span class="files-tracker-summary">${IA.escapeHtml(t("files_tracker_empty"))}</span>` +
        `</div>`;
      return;
    }

    const summary = t("files_tracker_summary", { files: files.length, dirs: dirs.length });
    const groups = groupFilesByDir(files);
    // 按目录名字母排序，每个目录下的文件也排序
    const sortedDirs = Object.keys(groups).sort();
    const dirItems = sortedDirs.map(function (dir) {
      const fileItems = groups[dir].sort().map(function (name) {
        return `<li class="files-tracker-file"><code>${IA.escapeHtml(name)}</code></li>`;
      }).join("");
      return `<li class="files-tracker-dir">` +
        `<span class="files-tracker-dirname">${IA.escapeHtml(dir)}</span>` +
        `<ul class="files-tracker-filelist">${fileItems}</ul>` +
        `</li>`;
    }).join("");

    // 每次工具调用都会重建面板：先记住用户当前的展开状态，重建后恢复，
    // 避免「正在看文件树，被下一次工具调用强制合上」。
    const wasExpanded = card.dataset.treeExpanded === "true";

    card.innerHTML =
      `<div class="files-tracker-header">` +
      `<span class="files-tracker-icon" aria-hidden="true">📁</span>` +
      `<span class="files-tracker-title">${IA.escapeHtml(t("files_tracker_title"))}</span>` +
      `<span class="files-tracker-summary">${IA.escapeHtml(summary)}</span>` +
      `<button type="button" class="files-tracker-toggle" aria-expanded="${wasExpanded ? "true" : "false"}">${IA.escapeHtml(wasExpanded ? t("files_tracker_collapse") : t("files_tracker_expand"))}</button>` +
      `</div>` +
      `<ul class="files-tracker-tree"${wasExpanded ? "" : " hidden"}>${dirItems}</ul>`;

    card.dataset.treeExpanded = wasExpanded ? "true" : "false";

    const toggleBtn = card.querySelector(".files-tracker-toggle");
    const tree = card.querySelector(".files-tracker-tree");
    if (toggleBtn && tree) {
      toggleBtn.addEventListener("click", function () {
        const expanded = toggleBtn.getAttribute("aria-expanded") === "true";
        card.dataset.treeExpanded = expanded ? "false" : "true";
        if (expanded) {
          tree.setAttribute("hidden", "");
          toggleBtn.setAttribute("aria-expanded", "false");
          toggleBtn.textContent = t("files_tracker_expand");
        } else {
          tree.removeAttribute("hidden");
          toggleBtn.setAttribute("aria-expanded", "true");
          toggleBtn.textContent = t("files_tracker_collapse");
        }
      });
    }
  }


  const ns = {
    reset: resetFilesTracker,
    track: trackExploredFile,
    update: updateFilesTracker,
  };

  IA.FilesTracker = ns;
  window.IssueAgent = IA;
})();
