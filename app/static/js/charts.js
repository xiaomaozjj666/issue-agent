/**
 * charts.js — 报告面板 ECharts 图表的渲染模块
 *
 * 从 app.js 抽离，专注图表渲染逻辑。app.js 通过 IA.Charts.* 调用。
 *
 * 两张图表（全部基于真实调查数据，不做任何伪造关联）：
 * 1. 补丁改动分布 — 解析 unified diff，按文件统计增删行数（对应 git diff --stat 的阅读习惯）
 * 2. 证据核对 — 证据按文件聚合：引用条数、是否读取过、补丁是否改到
 *
 * 图表数据元素均支持点击下钻到对应详情（补丁章节 / 证据条目）。
 *
 * 配色体系（全局统一）：
 *   深蓝 #165DFF · 绿色 #00B42A · 红色 #F53F3F · 棕橙色 #FF7D00
 */
(function () {
  "use strict";

  const IA = window.IssueAgent;
  const t = IA.translate;
  const enumLabel = IA.enumLabel;

  // ── 配色体系 ────────────────────────────────────────────
  // 全局固定色值，深色/浅色主题共用。辅助色（text/line 等）按主题区分。
  const BRAND = {
    blue: "#165DFF",
    green: "#00B42A",
    red: "#F53F3F",
    orange: "#FF7D00",
    gray: "#86909C",
  };

  const PALETTE_DARK = {
    primary: BRAND.blue,
    success: BRAND.green,
    danger: BRAND.red,
    warning: BRAND.orange,
    muted: BRAND.gray,
    text: "#f1f5f9",
    textDim: "#94a3b8",
    line: "#334155",
    tooltipBg: "#0f172a",
    tooltipBorder: "#1e293b",
    bg: "#0f172a",
    splitArea: ["rgba(22,93,255,0.04)", "rgba(22,93,255,0.08)"],
  };

  const PALETTE_LIGHT = {
    primary: BRAND.blue,
    success: BRAND.green,
    danger: BRAND.red,
    warning: BRAND.orange,
    muted: BRAND.gray,
    text: "#0f172a",
    textDim: "#475569",
    line: "#cbd5e1",
    tooltipBg: "#ffffff",
    tooltipBorder: "#e2e8f0",
    bg: "#ffffff",
    splitArea: ["rgba(22,93,255,0.04)", "rgba(22,93,255,0.08)"],
  };

  function getPalette() {
    return document.documentElement.dataset.theme === "light" ? PALETTE_LIGHT : PALETTE_DARK;
  }

  function isAvailable() {
    return typeof window.echarts !== "undefined" && !window.__echartsFailed;
  }

  // ── 移动端适配 ──────────────────────────────────────────
  function isMobile() {
    return window.matchMedia("(max-width: 640px)").matches
      || (typeof navigator !== "undefined" && navigator.maxTouchPoints > 0
          && window.matchMedia("(pointer: coarse)").matches);
  }

  function mobileInitOpts() {
    if (!isMobile()) return undefined;
    return { renderer: "canvas", devicePixelRatio: Math.min(window.devicePixelRatio || 1, 2) };
  }

  function mobileTooltip(tooltip) {
    if (!isMobile()) return tooltip;
    return Object.assign({}, tooltip, {
      confine: true,
      appendToBody: true,
      enterable: false,
      padding: [8, 10],
      hideDelay: 100,
      textStyle: Object.assign({}, tooltip && tooltip.textStyle, { fontSize: 12 }),
    });
  }

  // 智能定位：tooltip 跟随鼠标但靠近边界时自动翻转，避免遮挡数据点。
  // 所有图表共用，确保 hover 查看详情时不盖住正在交互的图形元素。
  function smartTooltipPosition(pos, params, dom, rect, size) {
    var tw = size.viewSize[0];
    var th = size.viewSize[1];
    var dw = dom.offsetWidth;
    var dh = dom.offsetHeight;
    var x = pos[0] + 14;
    var y = pos[1] + 14;
    if (x + dw > tw - 8) x = pos[0] - dw - 14;
    if (y + dh > th - 8) y = pos[1] - dh - 14;
    if (x < 8) x = 8;
    if (y < 8) y = 8;
    return [x, y];
  }

  // ── 公共工具栏：保存图片 + 刷新重绘 + 数据视图 ────────────
  function toolbox(palette) {
    const mobile = isMobile();
    return {
      right: 0,
      top: 4,
      itemSize: mobile ? 18 : 16,
      itemGap: mobile ? 13 : 11,
      feature: {
        saveAsImage: { title: t("chart_save_image"), pixelRatio: 2, backgroundColor: palette.bg },
        restore: { title: t("chart_restore") },
        dataView: {
          title: t("chart_data_view"),
          lang: [t("chart_data_view"), t("report_close"), t("chart_data_view_refresh")],
          readOnly: true,
          backgroundColor: palette.tooltipBg,
          textColor: palette.text,
          textareaColor: palette.tooltipBorder,
          textareaBorderColor: palette.line,
        },
      },
      iconStyle: { borderColor: palette.textDim },
      emphasis: { iconStyle: { borderColor: palette.text } },
    };
  }

  // ReactBits ScaleIn 复刻：opacity + scale + translateY 复合入场
  // 比纯 opacity 更精致：先轻微下移缩小，再回弹归位
  // 尊重 prefers-reduced-motion：降级为即时显示，不做 transform 动画
  function fadeIn(container) {
    if (!container) return;
    var reduced = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (reduced) {
      container.style.opacity = "1";
      return;
    }
    container.style.opacity = "0";
    container.style.transform = "translateY(10px) scale(0.97)";
    container.style.transformOrigin = "center top";
    container.style.transition = "opacity 320ms cubic-bezier(0.16,1,0.3,1), transform 320ms cubic-bezier(0.16,1,0.3,1)";
    requestAnimationFrame(function () {
      requestAnimationFrame(function () {
        container.style.opacity = "1";
        container.style.transform = "translateY(0) scale(1)";
      });
    });
    // 动画结束后清理 transform，避免残留影响 ECharts canvas 坐标计算
    setTimeout(function () {
      container.style.transform = "";
      container.style.transformOrigin = "";
      container.style.transition = "";
    }, 360);
  }

  // 归一化文件路径：统一分隔符、去掉 ./ 与 a/ b/(diff) 前缀、转小写
  function normPath(p) {
    return String(p || "").replace(/\\/g, "/").replace(/^\.\//, "").replace(/^[ab]\//, "").toLowerCase();
  }

  // 构建"文件已读"匹配器：合并 files_read 与 files_examined 两个来源，
  // 归一化后支持精确 / 后缀匹配，容忍 src/ 等前缀差异导致的匹配失败
  function buildFileReadMatcher(report, sessionData) {
    const sources = [];
    if (sessionData && Array.isArray(sessionData.files_read)) Array.prototype.push.apply(sources, sessionData.files_read);
    if (report && Array.isArray(report.files_examined)) Array.prototype.push.apply(sources, report.files_examined);
    const normed = sources.map(normPath).filter(Boolean);
    return function (path) {
      const p = normPath(path);
      if (!p) return false;
      for (let i = 0; i < normed.length; i++) {
        const n = normed[i];
        if (n === p || n.endsWith("/" + p) || p.endsWith("/" + n)) return true;
      }
      return false;
    };
  }

  // 文件路径缩短显示：保留最后两段
  function shortName(path) {
    const parts = String(path).split("/");
    return parts.length > 2 ? "…/" + parts.slice(-2).join("/") : String(path);
  }

  // 文件名智能去重：短名冲突时回退到完整路径
  function makeLabels(rawPaths) {
    const shortNames = rawPaths.map(shortName);
    const nameCount = {};
    shortNames.forEach(function (name) { nameCount[name] = (nameCount[name] || 0) + 1; });
    return shortNames.map(function (name, i) {
      return nameCount[name] > 1 ? rawPaths[i] : name;
    });
  }

  // 数据少时压缩画布高度，避免 1~3 条数据在 300px 容器中显得空旷稀疏。
  // 放大模态框（.chart-modal-canvas）保持大画布，不压缩，便于查看细节。
  function fitChartHeight(container, rowCount, perRow, minH, maxH) {
    if (!container || container.classList.contains("chart-modal-canvas")) return;
    const rows = Math.max(1, rowCount || 1);
    const per = perRow || 42;
    const lo = minH || 150;
    const hi = maxH || 300;
    const chrome = 82; // 图例 + 底部汇总 + 上下内边距
    let h = chrome + rows * per;
    h = Math.max(lo, Math.min(h, hi));
    container.style.height = h + "px";
    container.style.minHeight = h + "px";
  }

  function setChartHeight(container, height) {
    if (!container || container.classList.contains("chart-modal-canvas")) return;
    container.style.height = height + "px";
    container.style.minHeight = height + "px";
  }

  // ── unified diff 解析：按文件统计增删行数 ────────────────
  // 与 git diff --numstat 同口径："+" 开头计新增，"-" 开头计删除，
  // 排除 +++/--- 文件头。文件路径取 +++ 行（新路径），删除文件回退到 --- 行。
  function parseDiffstat(patch) {
    const files = [];
    let current = null;
    let oldPath = null;
    String(patch || "").split("\n").forEach(function (line) {
      if (line.indexOf("--- ") === 0) {
        const p = line.slice(4).trim();
        oldPath = p === "/dev/null" ? null : p.replace(/^a\//, "");
        return;
      }
      if (line.indexOf("+++ ") === 0) {
        const p = line.slice(4).trim();
        const path = p === "/dev/null" ? oldPath : p.replace(/^b\//, "");
        current = { path: path || "unknown", added: 0, removed: 0 };
        files.push(current);
        return;
      }
      if (!current) return;
      if (line.charAt(0) === "+") current.added += 1;
      else if (line.charAt(0) === "-") current.removed += 1;
    });
    return files.filter(function (f) { return f.added > 0 || f.removed > 0; });
  }

  // ── 区块1：补丁改动分布（diffstat） ──────────────────────
  // 横向堆叠条形图：绿色=新增行，红色=删除行，右侧标注 +a −d。
  // 底部汇总与 git diff --stat 尾行同口径。改动文件若没有对应证据，
  // tooltip 中给出提示（改动缺乏证据支撑时需人工确认）。
  function renderDiffstat(container, report, sessionData) {
    if (!container) return null;
    if (!isAvailable()) {
      container.innerHTML = '<div class="report-chart-fallback">' + IA.escapeHtml(t("chart_load_failed")) + '</div>';
      return null;
    }
    const palette = getPalette();
    const stats = parseDiffstat(report.patch);
    if (!stats.length) {
      container.innerHTML = '<div class="report-chart-empty">' + IA.escapeHtml(t("diffstat_empty")) + '</div>';
      return null;
    }

    // 证据文件集合：判断改动是否有证据支撑
    const evidencePaths = (report.evidence || []).map(function (e) { return normPath(e.path); }).filter(Boolean);
    function hasEvidenceFor(path) {
      const p = normPath(path);
      return evidencePaths.some(function (n) {
        return n === p || n.endsWith("/" + p) || p.endsWith("/" + n);
      });
    }

    // 行序（类目轴自下而上）：改动量升序排列，最大的文件显示在最上方；
    // 超出 12 行时聚合尾部为一行
    const sorted = stats.slice().sort(function (a, b) { return (a.added + a.removed) - (b.added + b.removed); });
    const MAX_ROWS = 12;
    let rows = sorted;
    if (sorted.length > MAX_ROWS) {
      const rest = sorted.slice(0, sorted.length - (MAX_ROWS - 1));
      const kept = sorted.slice(sorted.length - (MAX_ROWS - 1));
      const agg = rest.reduce(function (acc, f) {
        acc.added += f.added;
        acc.removed += f.removed;
        return acc;
      }, { path: t("diffstat_others", { n: rest.length }), added: 0, removed: 0, aggregate: true });
      rows = [agg].concat(kept);
    }
    const labels = makeLabels(rows.map(function (r) { return r.path; }));

    const totalAdded = stats.reduce(function (s, f) { return s + f.added; }, 0);
    const totalRemoved = stats.reduce(function (s, f) { return s + f.removed; }, 0);

    fitChartHeight(container, rows.length);
    const barW = rows.length <= 3 ? 26 : 16;
    fadeIn(container);
    const chart = echarts.init(container, null, mobileInitOpts());
    chart.setOption({
      animationDuration: 200,
      animationEasing: "cubicOut",
      tooltip: mobileTooltip({
        confine: true,
        appendToBody: true,
        enterable: false,
        className: "ia-chart-tooltip",
        backgroundColor: palette.tooltipBg,
        borderWidth: 0,
        padding: [10, 14],
        textStyle: { color: palette.text, fontSize: 12 },
        position: smartTooltipPosition,
        formatter: function (params) {
          const row = rows[params.dataIndex];
          if (!row) return "";
          let html = '<div style="font-weight:600;margin-bottom:4px;max-width:280px;white-space:normal;word-break:break-all;">' + IA.escapeHtml(row.path) + '</div>' +
            '<div><span style="color:' + palette.success + ';font-weight:600;">+' + row.added + '</span>' +
            ' <span style="color:' + palette.danger + ';font-weight:600;">\u2212' + row.removed + '</span></div>';
          if (!row.aggregate && !hasEvidenceFor(row.path)) {
            html += '<div style="color:' + palette.warning + ';font-size:11px;margin-top:4px;max-width:260px;white-space:normal;">' + IA.escapeHtml(t("diffstat_no_evidence_hint")) + '</div>';
          }
          return html;
        },
      }),
      legend: {
        left: 0,
        top: 0,
        itemWidth: 10,
        itemHeight: 10,
        icon: "roundRect",
        textStyle: { color: palette.textDim, fontSize: 10 },
        data: [t("diffstat_added"), t("diffstat_removed")],
      },
      grid: { left: 8, right: 64, top: 32, bottom: 28, containLabel: true },
      toolbox: toolbox(palette),
      title: {
        text: t("diffstat_stat", { files: stats.length, added: totalAdded, removed: totalRemoved }),
        left: "center",
        bottom: 0,
        textStyle: { color: palette.textDim, fontSize: 11, fontWeight: 500 },
      },
      xAxis: {
        type: "value",
        minInterval: 1,
        axisLabel: { color: palette.textDim, fontSize: 10 },
        splitLine: { lineStyle: { color: palette.line, opacity: 0.4 } },
      },
      yAxis: {
        type: "category",
        data: labels,
        axisLabel: {
          color: palette.textDim,
          fontSize: 10,
          width: isMobile() ? 90 : 150,
          overflow: "truncate",
        },
        axisLine: { lineStyle: { color: palette.line } },
        axisTick: { show: false },
      },
      series: [
        {
          name: t("diffstat_added"),
          type: "bar",
          stack: "diff",
          data: rows.map(function (r) { return r.added; }),
          itemStyle: { color: palette.success, borderRadius: [0, 0, 0, 0] },
          barMaxWidth: barW,
          emphasis: { itemStyle: { shadowBlur: 8, shadowColor: "rgba(0,0,0,0.3)" } },
        },
        {
          name: t("diffstat_removed"),
          type: "bar",
          stack: "diff",
          data: rows.map(function (r) { return r.removed; }),
          itemStyle: { color: palette.danger, borderRadius: [0, 3, 3, 0] },
          barMaxWidth: barW,
          // 堆叠末端统一标注 +a −d（挂在最后一个 series 上才会显示在整条右侧）
          label: {
            show: true,
            position: "right",
            fontSize: 10,
            fontWeight: 600,
            color: palette.textDim,
            formatter: function (params) {
              const row = rows[params.dataIndex];
              if (!row) return "";
              return "+" + row.added + " \u2212" + row.removed;
            },
          },
          emphasis: { itemStyle: { shadowBlur: 8, shadowColor: "rgba(0,0,0,0.3)" } },
        },
      ],
    });

    // 点击下钻：任意条形 → 跳转到补丁章节查看完整 diff
    chart.on("click", function () {
      IA.jumpToSection("report-patch");
    });
    chart.on("mouseover", function () { container.style.cursor = "pointer"; });
    chart.on("mouseout", function () { container.style.cursor = ""; });
    return chart;
  }

  // ── 区块2：证据核对 ──────────────────────────────────────
  // 证据按文件聚合成横向条形图，条长 = 引用条数：
  //   蓝色 = 调查过程读取过该文件；红色 = 报告引用了但从未读取（需人工核实）
  // 被补丁改到的文件在条形右侧追加"已修改"标注，
  // 底部汇总读取率与补丁覆盖率 —— 结论、证据、改动三者是否对得上一眼可查。
  function renderVerify(container, report, sessionData) {
    if (!container) return null;
    if (!isAvailable()) {
      container.innerHTML = '<div class="report-chart-fallback">' + IA.escapeHtml(t("chart_load_failed")) + '</div>';
      return null;
    }
    const palette = getPalette();
    const evidence = report.evidence || [];
    if (!evidence.length) {
      container.innerHTML = '<div class="report-chart-empty">' + IA.escapeHtml(t("report_evidence_chart_empty")) + '</div>';
      return null;
    }

    const isFileRead = buildFileReadMatcher(report, sessionData);
    // 已读集合为空（旧会话缺 files_read 数据）时无法判定，全部按已读处理
    const hasReadData = (sessionData && Array.isArray(sessionData.files_read) && sessionData.files_read.length > 0)
      || (Array.isArray(report.files_examined) && report.files_examined.length > 0);

    // 补丁改动文件集合
    const patchPaths = parseDiffstat(report.patch).map(function (f) { return normPath(f.path); });
    function isPatched(path) {
      const p = normPath(path);
      return patchPaths.some(function (n) {
        return n === p || n.endsWith("/" + p) || p.endsWith("/" + n);
      });
    }

    // 证据按文件聚合：条数 + 行号列表 + 首条证据索引（点击下钻用）
    const byFile = {};
    const order = [];
    evidence.forEach(function (e, i) {
      const n = normPath(e.path || "unknown");
      if (!byFile[n]) {
        byFile[n] = { path: e.path || "unknown", count: 0, lines: [], firstIdx: i };
        order.push(n);
      }
      byFile[n].count += 1;
      if (e.lines) byFile[n].lines.push(e.lines);
    });
    const rows = order.map(function (n) {
      const item = byFile[n];
      return {
        path: item.path,
        count: item.count,
        lines: item.lines,
        firstIdx: item.firstIdx,
        read: hasReadData ? isFileRead(item.path) : true,
        patched: isPatched(item.path),
      };
    });
    // 类目轴自下而上：条数升序，引用最多的文件显示在最上方
    rows.sort(function (a, b) { return a.count - b.count; });
    const labels = makeLabels(rows.map(function (r) { return r.path; }));

    fitChartHeight(container, rows.length);
    const barW = rows.length <= 3 ? 26 : 16;
    const readCount = rows.filter(function (r) { return r.read; }).length;
    const patchedCount = rows.filter(function (r) { return r.patched; }).length;
    const statParts = [];
    if (hasReadData) statParts.push(t("verify_stat_read", { read: readCount, total: rows.length }));
    if (patchPaths.length) statParts.push(t("verify_stat_patched", { patched: patchedCount, total: rows.length }));
    const statText = statParts.join(" \u00b7 ");

    fadeIn(container);
    const chart = echarts.init(container, null, mobileInitOpts());
    chart.setOption({
      animationDuration: 200,
      animationEasing: "cubicOut",
      tooltip: mobileTooltip({
        confine: true,
        appendToBody: true,
        enterable: false,
        className: "ia-chart-tooltip",
        backgroundColor: palette.tooltipBg,
        borderWidth: 0,
        padding: [10, 14],
        textStyle: { color: palette.text, fontSize: 12 },
        position: smartTooltipPosition,
        formatter: function (params) {
          const row = rows[params.dataIndex];
          if (!row) return "";
          let html = '<div style="font-weight:600;margin-bottom:4px;max-width:280px;white-space:normal;word-break:break-all;">' + IA.escapeHtml(row.path) + '</div>' +
            '<div style="color:' + palette.textDim + ';font-size:11px;">' + IA.escapeHtml(t("verify_citations")) + ': <b style="color:' + palette.text + ';">' + row.count + '</b>' +
            (row.lines.length ? ' \u00b7 ' + IA.escapeHtml(row.lines.slice(0, 4).join(", ")) : '') + '</div>';
          if (hasReadData) {
            html += '<div style="color:' + (row.read ? palette.success : palette.danger) + ';font-size:11px;margin-top:2px;font-weight:600;">' +
              IA.escapeHtml(row.read ? t("verify_read") : t("verify_not_read")) + '</div>';
          }
          if (patchPaths.length) {
            html += '<div style="color:' + (row.patched ? palette.success : palette.textDim) + ';font-size:11px;margin-top:2px;">' +
              IA.escapeHtml(t("verify_patch_label")) + ': ' + IA.escapeHtml(row.patched ? t("verify_patched") : t("verify_not_patched")) + '</div>';
          }
          if (!row.read) {
            html += '<div style="color:' + palette.danger + ';font-size:11px;margin-top:4px;max-width:260px;white-space:normal;">' + IA.escapeHtml(t("verify_not_read_hint")) + '</div>';
          }
          return html;
        },
      }),
      grid: { left: 8, right: 88, top: 36, bottom: statText ? 28 : 12, containLabel: true },
      toolbox: toolbox(palette),
      title: statText ? {
        text: statText,
        left: "center",
        bottom: 0,
        textStyle: { color: palette.textDim, fontSize: 11, fontWeight: 500 },
      } : undefined,
      xAxis: {
        type: "value",
        minInterval: 1,
        axisLabel: { color: palette.textDim, fontSize: 10 },
        splitLine: { lineStyle: { color: palette.line, opacity: 0.4 } },
      },
      yAxis: {
        type: "category",
        data: labels,
        axisLabel: {
          color: palette.textDim,
          fontSize: 10,
          width: isMobile() ? 90 : 150,
          overflow: "truncate",
        },
        axisLine: { lineStyle: { color: palette.line } },
        axisTick: { show: false },
      },
      series: [{
        type: "bar",
        data: rows.map(function (r) {
          return {
            value: r.count,
            itemStyle: { color: r.read ? palette.primary : palette.danger, borderRadius: [0, 3, 3, 0] },
          };
        }),
        barMaxWidth: barW,
        // 条形右侧标注：条数 + 补丁覆盖状态
        label: {
          show: true,
          position: "right",
          fontSize: 10,
          fontWeight: 600,
          color: palette.textDim,
          formatter: function (params) {
            const row = rows[params.dataIndex];
            if (!row) return "";
            return String(row.count) + (row.patched ? " \u00b7 " + t("verify_patched") : "");
          },
        },
        emphasis: { itemStyle: { shadowBlur: 8, shadowColor: "rgba(0,0,0,0.3)" } },
      }],
    });
    // 存在"引用但未读取"的文件时容器层呼吸警示
    if (rows.some(function (r) { return !r.read; })) container.classList.add("pulse-ring");

    // 点击下钻：条形 / y 轴标签 → 跳到该文件的第一条证据
    chart.on("click", function (params) {
      let row = null;
      if (params.componentType === "series") row = rows[params.dataIndex];
      else if (params.componentType === "yAxis") row = rows[labels.indexOf(params.value)];
      if (row && row.firstIdx >= 0) IA.jumpToEvidence(row.firstIdx);
      else IA.jumpToSection("report-evidence");
    });
    chart.on("mouseover", function () { container.style.cursor = "pointer"; });
    chart.on("mouseout", function () { container.style.cursor = ""; });
    return chart;
  }

  // ── 区块3：证据强度 ──────────────────────────────────────
  // 每条证据按强度（弱/中/强）横向条形、按类型（code/log/test/config/docs）着色，
  // 点击下钻到对应证据条目。直接回答读者「凭什么信这个结论」。
  const KIND_COLOR = {
    code: BRAND.blue,
    log: BRAND.orange,
    test: BRAND.green,
    config: BRAND.red,
    docs: "#722ED1",
  };
  // 证据强度着色：这是本图的核心信息，必须用颜色直接区分弱/中/强。
  // （此前按 kind 着色，同类型证据颜色一致，强度差异完全不可见。）
  const STRENGTH_COLOR = {
    weak: BRAND.gray,
    moderate: BRAND.blue,
    strong: BRAND.green,
  };
  // ── 区块4：波及范围 ──────────────────────────────────────
  // 受影响模块/文件 treemap：以 impact.blast_radius 为主，补丁改动文件为补充；
  // 叶子大小 = 该文件改动行数（来自补丁）或 1，颜色按严重度。直接回答「有多严重」。
  const SEVERITY_COLOR = {
    critical: BRAND.red,
    high: BRAND.orange,
    medium: BRAND.blue,
    low: BRAND.gray,
  };
  const GENERIC_ROOTS = {
    src: 1, lib: 1, libs: 1, app: 1, apps: 1, pkg: 1, pkgs: 1, package: 1, packages: 1,
    tests: 1, test: 1, testing: 1, docs: 1, doc: 1, documentation: 1,
    bin: 1, scripts: 1, tools: 1, tool: 1, examples: 1, example: 1, demo: 1, demos: 1,
    benchmark: 1, benchmarks: 1,
  };
  function moduleOf(path) {
    const p = String(path || "").replace(/\\/g, "/").trim();
    if (!p) return "root";
    // 已是模块/组件名（无斜杠、无扩展名）
    if (p.indexOf("/") === -1 && p.indexOf(".") === -1) return p;
    const parts = p.split("/").filter(function (s) { return s; });
    if (!parts.length) return p;
    const originalDirs = parts.slice();
    // 去掉文件名
    if (parts[parts.length - 1].indexOf(".") !== -1) parts.pop();
    const dirParts = parts.length ? parts : originalDirs.slice(0, -1);
    // 去掉常见无意义根目录
    while (dirParts.length && GENERIC_ROOTS[dirParts[0].toLowerCase()]) {
      dirParts.shift();
    }
    if (dirParts.length) return dirParts.slice(0, 2).join("/");
    if (originalDirs.length > 1) return originalDirs[0];
    return originalDirs[0] || "root";
  }
  function renderBlastRadius(container, report, sessionData) {
    if (!container) return null;
    if (!isAvailable()) {
      container.innerHTML = '<div class="report-chart-fallback">' + IA.escapeHtml(t("chart_load_failed")) + "</div>";
      return null;
    }
    const impact = report.impact;
    if (!impact) {
      container.innerHTML = '<div class="report-chart-empty">' + IA.escapeHtml(t("chart_blast_radius_empty")) + "</div>";
      return null;
    }
    const palette = getPalette();
    const patchFiles = parseDiffstat(report.patch);
    let rawModules = (impact.blast_radius || []).slice();
    if (!rawModules.length) rawModules = patchFiles.map(function (f) { return f.path; });
    if (!rawModules.length) {
      container.innerHTML = '<div class="report-chart-empty">' + IA.escapeHtml(t("chart_blast_radius_empty")) + "</div>";
      return null;
    }

    // 归一化模块名，并汇总每个模块下的改动行数
    const moduleSet = {};
    rawModules.forEach(function (p) { moduleSet[moduleOf(p)] = true; });
    const moduleChanges = {};
    Object.keys(moduleSet).forEach(function (mod) { moduleChanges[mod] = 0; });
    patchFiles.forEach(function (f) {
      const mod = moduleOf(f.path);
      if (mod in moduleChanges) moduleChanges[mod] += f.added + f.removed;
    });

    const sev = impact.severity || "medium";
    const sevColor = SEVERITY_COLOR[sev] || palette.muted;
    const modules = Object.keys(moduleSet);
    const useBar = modules.length <= 3;

    // 与并排的风险矩阵保持一致高度，避免少量模块时卡片下方留下大块空白。
    setChartHeight(container, 300);
    fadeIn(container);
    const chart = echarts.init(container, null, mobileInitOpts());

    if (useBar) {
      // 少量模块时用横向条形图，避免单一大灰块，同时显示改动行数
      const rows = modules.map(function (mod) {
        return { name: mod, value: moduleChanges[mod] || 1, changed: moduleChanges[mod] || 0 };
      }).sort(function (a, b) { return b.value - a.value; });
      chart.setOption({
        animationDuration: 200,
        animationEasing: "cubicOut",
        tooltip: mobileTooltip({
          confine: true, appendToBody: true, enterable: false, className: "ia-chart-tooltip",
          backgroundColor: palette.tooltipBg, borderWidth: 0, padding: [10, 14],
          textStyle: { color: palette.text, fontSize: 12 },
          position: smartTooltipPosition,
          formatter: function (info) {
            const d = info.data || {};
            return '<div style="font-weight:600;max-width:300px;white-space:normal;word-break:break-all;">' +
              IA.escapeHtml(d.name) + "</div>" +
              '<div style="font-size:11px;color:' + palette.textDim + ';">' + IA.escapeHtml(t("diffstat_total_changes")) +
              ': <b>' + d.value + "</b></div>" +
              '<div style="font-size:11px;color:' + palette.textDim + ';">' + IA.escapeHtml(t("report_severity")) +
              ': <b style="color:' + sevColor + ';">' + IA.escapeHtml(enumLabel("severity", sev)) + "</b></div>";
          },
        }),
        grid: { left: 12, right: 92, top: 52, bottom: 28, containLabel: true },
        xAxis: {
          type: "value",
          splitLine: { lineStyle: { color: palette.line, opacity: 0.4, type: "dashed" } },
          axisLabel: { color: palette.textDim, fontSize: 11 },
        },
        yAxis: {
          type: "category",
          data: rows.map(function (r) { return r.name; }),
          axisLine: { show: false },
          axisTick: { show: false },
          axisLabel: { color: palette.text, fontSize: 11, width: isMobile() ? 88 : 128, overflow: "truncate" },
        },
        toolbox: toolbox(palette),
        series: [{
          type: "bar",
          data: rows.map(function (r) { return { value: r.value, name: r.name, changed: r.changed, itemStyle: { color: sevColor, borderRadius: [0, 3, 3, 0] } }; }),
          barMaxWidth: 28,
          label: {
            show: true,
            position: "right",
            color: palette.text,
            fontSize: 12,
            formatter: function (p) {
              return p.data.changed
                ? t("chart_changed_lines", { count: p.data.changed })
                : enumLabel("severity", sev);
            },
          },
          emphasis: { itemStyle: { shadowBlur: 8, shadowColor: "rgba(0,0,0,0.3)" } },
        }],
      });
    } else {
      const treeData = modules.map(function (mod) {
        const changed = moduleChanges[mod] || 1;
        return { name: mod, value: changed, itemStyle: { color: sevColor } };
      });
      chart.setOption({
        animationDuration: 200,
        animationEasing: "cubicOut",
        tooltip: mobileTooltip({
          confine: true, appendToBody: true, enterable: false, className: "ia-chart-tooltip",
          backgroundColor: palette.tooltipBg, borderWidth: 0, padding: [10, 14],
          textStyle: { color: palette.text, fontSize: 12 },
          position: smartTooltipPosition,
          formatter: function (info) {
            const d = info.data || {};
            const changed = d.value > 1 ? d.value : 0;
            return '<div style="font-weight:600;max-width:300px;white-space:normal;word-break:break-all;">' +
              IA.escapeHtml(d.name) + "</div>" +
              (changed ? '<div style="font-size:11px;color:' + palette.textDim + ';">' + IA.escapeHtml(t("diffstat_total_changes")) +
                ': <b>' + changed + "</b></div>" : "") +
              '<div style="font-size:11px;color:' + palette.textDim + ';">' + IA.escapeHtml(t("report_severity")) +
              ': <b style="color:' + sevColor + ';">' + IA.escapeHtml(enumLabel("severity", sev)) + "</b></div>";
          },
        }),
        toolbox: toolbox(palette),
        series: [{
          type: "treemap",
          roam: false,
          nodeClick: false,
          breadcrumb: { show: false },
          label: { color: "#ffffff", fontSize: 12, fontWeight: 600, formatter: function (p) { return p.name; } },
          itemStyle: { borderColor: palette.tooltipBg, borderWidth: 1, gapWidth: 1, borderRadius: 2 },
          data: treeData,
        }],
      });
    }
    chart.on("click", function () { IA.jumpToSection("report-impact"); });
    chart.on("mouseover", function () { container.style.cursor = "pointer"; });
    chart.on("mouseout", function () { container.style.cursor = ""; });
    return chart;
  }

  // ── 区块5：风险矩阵 ──────────────────────────────────────
  // severity × likelihood 二维网格：把本问题标在「严重度×发生可能性」上，
  // 背景单元格按组合风险着色（绿→黄→橙→红）。直接回答「该多紧急」。
  function renderRiskMatrix(container, report, sessionData) {
    if (!container) return null;
    if (!isAvailable()) {
      container.innerHTML = '<div class="report-chart-fallback">' + IA.escapeHtml(t("chart_load_failed")) + "</div>";
      return null;
    }
    const impact = report.impact;
    if (!impact || !impact.severity || !impact.likelihood) {
      container.innerHTML = '<div class="report-chart-empty">' + IA.escapeHtml(t("chart_risk_matrix_empty")) + "</div>";
      return null;
    }
    const palette = getPalette();
    const sevLevels = ["low", "medium", "high", "critical"];
    const likeLevels = ["low", "medium", "high"];
    const sevVal = { low: 1, medium: 2, high: 3, critical: 4 };
    const likeVal = { low: 1, medium: 2, high: 3 };
    const sev = sevLevels.indexOf(impact.severity) >= 0 ? impact.severity : "medium";
    const like = likeLevels.indexOf(impact.likelihood) >= 0 ? impact.likelihood : "medium";

    // 单元格背景色：组合风险 = 严重度×可能性
    function cellColor(s, l) {
      const score = sevVal[s] * likeVal[l];
      if (score >= 9) return "rgba(245,63,63,0.55)";   // 红：极高
      if (score >= 6) return "rgba(255,125,0,0.50)";   // 橙：高
      if (score >= 3) return "rgba(247,186,30,0.42)";  // 黄：中
      return "rgba(0,180,42,0.38)";                    // 绿：低
    }
    const cellData = [];
    sevLevels.forEach(function (s, yi) {
      likeLevels.forEach(function (l, xi) {
        cellData.push({ value: [xi, yi], color: cellColor(s, l) });
      });
    });
    const issueX = likeLevels.indexOf(like);
    const issueY = sevLevels.indexOf(sev);
    const markerColor = SEVERITY_COLOR[sev] || palette.muted;

    setChartHeight(container, 300);
    fadeIn(container);
    const chart = echarts.init(container, null, mobileInitOpts());
    chart.setOption({
      animationDuration: 200,
      animationEasing: "cubicOut",
      tooltip: mobileTooltip({
        confine: true, appendToBody: true, enterable: false, className: "ia-chart-tooltip",
        backgroundColor: palette.tooltipBg, borderWidth: 0, padding: [10, 14],
        textStyle: { color: palette.text, fontSize: 12 },
        position: smartTooltipPosition,
        formatter: function () {
          return '<div style="font-weight:600;">' + IA.escapeHtml(t("chart_risk_matrix")) + "</div>" +
            '<div style="font-size:11px;color:' + palette.textDim + ';">' + IA.escapeHtml(t("report_severity")) +
            ': <b style="color:' + markerColor + ';">' + IA.escapeHtml(enumLabel("severity", sev)) + "</b></div>" +
            '<div style="font-size:11px;color:' + palette.textDim + ';">' + IA.escapeHtml(t("report_likelihood")) +
            ': <b>' + IA.escapeHtml(enumLabel("likelihood", like)) + "</b></div>";
        },
      }),
      grid: { left: 16, right: 28, top: 52, bottom: 42, containLabel: true },
      toolbox: toolbox(palette),
      xAxis: {
        type: "category",
        name: t("report_likelihood"),
        nameLocation: "middle",
        nameGap: 28,
        nameTextStyle: { color: palette.textDim, fontSize: 11 },
        data: likeLevels.map(function (l) { return enumLabel("likelihood", l); }),
        axisLine: { lineStyle: { color: palette.line } },
        axisTick: { show: false },
        axisLabel: { color: palette.textDim, fontSize: 11 },
        splitLine: { show: false },
      },
      yAxis: {
        type: "category",
        name: t("report_severity"),
        nameLocation: "middle",
        nameGap: 34,
        nameTextStyle: { color: palette.textDim, fontSize: 11 },
        data: sevLevels.map(function (s) { return enumLabel("severity", s); }),
        axisLine: { lineStyle: { color: palette.line } },
        axisTick: { show: false },
        axisLabel: { color: palette.textDim, fontSize: 11 },
        splitLine: { show: false },
      },
      series: [
        {
          type: "custom",
          renderItem: function (params, api) {
            const d = cellData[params.dataIndex];
            const center = api.coord([api.value(0), api.value(1)]);
            const size = api.size([1, 1]);
            const w = Math.max(12, size[0] - 8);
            const h = Math.max(12, size[1] - 8);
            return {
              type: "rect",
              shape: { x: center[0] - w / 2, y: center[1] - h / 2, width: w, height: h, r: 5 },
              style: { fill: d.color },
            };
          },
          data: cellData.map(function (d) { return { value: d.value }; }),
          silent: true,
          z: 1,
        },
        {
          type: "scatter",
          symbolSize: 18,
          data: [{ value: [issueX, issueY] }],
          itemStyle: {
            color: markerColor,
            borderColor: palette.bg || "#fff",
            borderWidth: 2.5,
            shadowBlur: 4,
            shadowColor: "rgba(0,0,0,0.2)",
          },
          label: {
            show: true,
            position: issueX === likeLevels.length - 1 ? "left" : "right",
            distance: 8,
            formatter: t("risk_matrix_root_cause"),
            color: palette.text,
            fontWeight: 600,
            fontSize: 11,
            width: 72,
            overflow: "truncate",
          },
          z: 10,
        },
      ],
    });
    chart.on("click", function () { IA.jumpToSection("report-impact"); });
    chart.on("mouseover", function () { container.style.cursor = "pointer"; });
    chart.on("mouseout", function () { container.style.cursor = ""; });
    return chart;
  }

  // ── 区块6：根因证据链 ────────────────────────────────────
  // 树状图：根因为根节点，各条证据为子节点，节点颜色=证据强度，可点击下钻。
  // 把「结论 ← 由哪些证据支撑」的逻辑关系画出来，是说服力的核心。
  function renderEvidenceMap(container, report, sessionData) {
    if (!container) return null;
    if (!isAvailable()) {
      container.innerHTML = '<div class="report-chart-fallback">' + IA.escapeHtml(t("chart_load_failed")) + "</div>";
      return null;
    }
    const evidence = (report.evidence || []).filter(function (e) { return e && e.path; });
    if (!evidence.length) {
      container.innerHTML = '<div class="report-chart-empty">' + IA.escapeHtml(t("chart_evidence_map_empty")) + "</div>";
      return null;
    }
    const palette = getPalette();
    const rootCause = String(report.root_cause || t("risk_matrix_root_cause")).trim();
    const chartWidth = Math.max(320, container.clientWidth || 320);
    const compact = chartWidth < 480;
    const rootLabelW = compact ? 112 : Math.max(132, Math.min(280, Math.round(chartWidth * 0.3)));
    const evidenceLabelW = compact ? 64 : Math.max(108, Math.min(220, Math.round(chartWidth * 0.24)));
    const horizontalPadding = compact ? 20 : 28;
    const rootMaxLength = compact ? 42 : chartWidth < 640 ? 68 : 84;
    const rootLabel = rootCause.length > rootMaxLength ? rootCause.slice(0, rootMaxLength).trimEnd() + "…" : rootCause;
    setChartHeight(container, Math.max(300, Math.min(500, 160 + evidence.length * 42)));

    const children = evidence.map(function (e, i) {
      const pathParts = String(e.path).split("/");
      const fileName = pathParts[pathParts.length - 1] || e.path;
      const nodeLabel = compact
        ? (e.lines || fileName)
        : (e.lines ? e.lines + " · " + fileName : fileName);
      return {
        name: nodeLabel,
        idx: i,
        fullPath: e.path,
        lines: e.lines || "",
        strength: e.strength || "moderate",
        kind: e.kind || "code",
        reason: e.reason || "",
        itemStyle: { color: STRENGTH_COLOR[e.strength || "moderate"] || palette.muted },
      };
    });
    const treeData = [{
      name: rootLabel,
      itemStyle: { color: palette.primary, borderColor: palette.primary },
      label: {
        position: "left",
        color: palette.text,
        fontWeight: 700,
        fontSize: 11,
        lineHeight: 17,
        width: rootLabelW,
        overflow: "break",
        backgroundColor: palette.tooltipBg,
        borderColor: palette.line,
        borderWidth: 1,
        borderRadius: 4,
        padding: [5, 8],
      },
      children: children,
    }];

    fadeIn(container);
    const chart = echarts.init(container, null, mobileInitOpts());
    chart.setOption({
      animationDuration: 200,
      animationEasing: "cubicOut",
      tooltip: mobileTooltip({
        confine: true, appendToBody: true, enterable: false, className: "ia-chart-tooltip",
        backgroundColor: palette.tooltipBg, borderWidth: 0, padding: [10, 14],
        textStyle: { color: palette.text, fontSize: 12 },
        position: smartTooltipPosition,
        formatter: function (params) {
          const d = params.data || {};
          if (params.dataIndex === 0 || !d.fullPath) {
            return '<div style="font-weight:600;max-width:300px;white-space:normal;">' + IA.escapeHtml(rootCause) + "</div>";
          }
          return '<div style="font-weight:600;margin-bottom:4px;max-width:300px;white-space:normal;word-break:break-all;">' +
            IA.escapeHtml(d.fullPath) + (d.lines ? ' <span style="color:' + palette.textDim + ';">' + IA.escapeHtml(d.lines) + "</span>" : "") + "</div>" +
            '<div style="font-size:11px;color:' + palette.textDim + ';">' + IA.escapeHtml(t("evidence_kind_legend")) +
            ': <b style="color:' + (KIND_COLOR[d.kind] || palette.muted) + ';">' + IA.escapeHtml(enumLabel("kind", d.kind)) + "</b></div>" +
            '<div style="font-size:11px;color:' + palette.textDim + ';">' + IA.escapeHtml(t("evidence_strength_legend")) +
            ': <b style="color:' + (STRENGTH_COLOR[d.strength] || palette.muted) + ';">' + IA.escapeHtml(enumLabel("strength", d.strength)) + "</b></div>" +
            (d.reason ? '<div style="color:' + palette.textDim + ';font-size:11px;margin-top:4px;max-width:280px;white-space:normal;">' + IA.escapeHtml(d.reason) + "</div>" : "");
        },
      }),
      toolbox: toolbox(palette),
      series: [{
        type: "tree",
        data: treeData,
        orient: "LR",
        left: rootLabelW + horizontalPadding,
        right: evidenceLabelW + horizontalPadding,
        top: 48,
        bottom: 24,
        symbol: "circle",
        symbolSize: 13,
        edgeShape: "curve",
        edgeForkPosition: "50%",
        expandAndCollapse: false,
        initialTreeDepth: -1,
        lineStyle: { color: palette.line, width: 1.5, curveness: 0.5 },
        itemStyle: { borderColor: palette.tooltipBg, borderWidth: 1.5 },
        label: {
          position: "right",
          verticalAlign: "middle",
          align: "left",
          color: palette.text,
          fontSize: 11,
          overflow: "truncate",
          width: evidenceLabelW,
        },
        leaves: {
          label: {
            position: "right",
            verticalAlign: "middle",
            align: "left",
            color: palette.text,
            fontSize: 11,
            overflow: "truncate",
            width: evidenceLabelW,
          },
        },
        emphasis: { focus: "descendant", itemStyle: { shadowBlur: 8, shadowColor: "rgba(0,0,0,0.3)" } },
      }],
    });
    chart.on("click", function (params) {
      const d = params.data || {};
      if (typeof d.idx === "number" && d.idx >= 0) IA.jumpToEvidence(d.idx);
      else IA.jumpToSection("report-evidence");
    });
    chart.on("mouseover", function () { container.style.cursor = "pointer"; });
    chart.on("mouseout", function () { container.style.cursor = ""; });
    return chart;
  }

  // ── 导出 ────────────────────────────────────────────────
  IA.Charts = {
    renderDiffstat: renderDiffstat,
    renderVerify: renderVerify,
    renderBlastRadius: renderBlastRadius,
    renderRiskMatrix: renderRiskMatrix,
    renderEvidenceMap: renderEvidenceMap,
    isAvailable: isAvailable,
    getPalette: getPalette,
  };
})();
