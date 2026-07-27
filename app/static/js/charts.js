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
 * 配色体系跟随 GitHub 风格的明暗主题语义色，避免图表与报告外壳割裂。
 */
(function () {
  "use strict";

  const IA = window.IssueAgent;
  const t = IA.translate;
  const enumLabel = IA.enumLabel;

  const PALETTE_DARK = {
    primary: "#58a6ff",
    success: "#3fb950",
    danger: "#f85149",
    warning: "#d29922",
    muted: "#8b949e",
    docs: "#bc8cff",
    text: "#e6edf3",
    textDim: "#8b949e",
    line: "#30363d",
    tooltipBg: "#161b22",
    tooltipBorder: "#30363d",
    bg: "#0d1117",
    splitArea: ["rgba(88,166,255,0.035)", "rgba(88,166,255,0.07)"],
    // Opaque semantic surfaces stay legible on #0d1117; low-alpha fills
    // collapse into indistinguishable gray/brown on dark canvases.
    riskLow: "#1f4d32",
    riskMedium: "#51421e",
    riskHigh: "#693b23",
    riskCritical: "#642b37",
    riskLowHover: "#2d6a40",
    riskMediumHover: "#725b26",
    riskHighHover: "#8a4d2b",
    riskCriticalHover: "#873e4b",
    riskMarkerHigh: "#f0883e",
    riskGridBorder: "#161b22",
    riskMarkerBorder: "#f0f6fc",
  };

  const PALETTE_LIGHT = {
    primary: "#0969da",
    success: "#1a7f37",
    danger: "#cf222e",
    warning: "#9a6700",
    muted: "#6e7781",
    docs: "#8250df",
    text: "#1f2328",
    textDim: "#656d76",
    line: "#d0d7de",
    tooltipBg: "#ffffff",
    tooltipBorder: "#d0d7de",
    bg: "#ffffff",
    splitArea: ["rgba(9,105,218,0.03)", "rgba(9,105,218,0.065)"],
    riskLow: "#dafbe1",
    riskMedium: "#fff8c5",
    riskHigh: "#ffebc8",
    riskCritical: "#ffebe9",
    riskLowHover: "#aceebb",
    riskMediumHover: "#fae17d",
    riskHighHover: "#ffc680",
    riskCriticalHover: "#ffcecb",
    riskMarkerHigh: "#bc6b00",
    riskGridBorder: "#ffffff",
    riskMarkerBorder: "#ffffff",
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
      appendToBody: tooltip && tooltip.appendToBody === false ? false : true,
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

  // 矩阵 tooltip 放在当前单元格外侧，避免遮住用户正在比较的数据。
  function matrixTooltipPosition(pos, params, dom, rect, size) {
    const viewWidth = size.viewSize[0];
    const viewHeight = size.viewSize[1];
    if (viewWidth < 420) {
      let mobileX = pos[0] - dom.offsetWidth / 2;
      let mobileY = pos[1] - dom.offsetHeight - 46;
      if (mobileY < 8) mobileY = pos[1] + 46;
      mobileX = Math.max(8, Math.min(mobileX, viewWidth - dom.offsetWidth - 8));
      mobileY = Math.max(8, Math.min(mobileY, viewHeight - dom.offsetHeight - 8));
      return [mobileX, mobileY];
    }
    // 三列矩阵中，光标通常位于格子中心；预留半格宽度和稳定的安全间距，
    // 比依赖 ECharts 在不同 renderer 下语义不一致的 rect 更稳定。
    const clearance = Math.max(120, Math.min(200, (viewWidth - 96) / 6 + 76));
    let x = pos[0] + clearance;
    if (x + dom.offsetWidth > viewWidth - 8) x = pos[0] - dom.offsetWidth - clearance;
    let y = pos[1] - dom.offsetHeight / 2;
    y = Math.max(8, Math.min(y, viewHeight - dom.offsetHeight - 8));
    return [Math.max(8, x), y];
  }

  // ── 公共工具栏：保存图片 + 刷新重绘 + 数据视图 ────────────
  function toolbox(palette, container) {
    const mobile = isMobile();
    return {
      show: Boolean(container && container.classList.contains("chart-modal-canvas")),
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
      toolbox: toolbox(palette, container),
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
      toolbox: toolbox(palette, container),
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
  function kindColor(kind, palette) {
    return ({
      code: palette.primary,
      log: palette.warning,
      test: palette.success,
      config: palette.muted,
      docs: palette.docs,
    })[kind] || palette.muted;
  }

  function strengthColor(strength, palette) {
    return ({ weak: palette.muted, moderate: palette.primary, strong: palette.success })[strength] || palette.muted;
  }
  // ── 区块4：波及范围 ──────────────────────────────────────
  // 受影响模块/文件 treemap：以 impact.blast_radius 为主，补丁改动文件为补充；
  // 叶子大小 = 该文件改动行数（来自补丁）或 1，颜色按严重度。直接回答「有多严重」。
  function severityColor(severity, palette) {
    return ({ critical: palette.danger, high: palette.warning, medium: palette.primary, low: palette.muted })[severity] || palette.muted;
  }

  function riskMarkerColor(severity, palette) {
    return ({ critical: palette.danger, high: palette.riskMarkerHigh, medium: palette.warning, low: palette.success })[severity] || palette.muted;
  }
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
    const sevColor = severityColor(sev, palette);
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
        toolbox: toolbox(palette, container),
        series: [{
          type: "bar",
          data: rows.map(function (r) { return { value: r.value, name: r.name, changed: r.changed, itemStyle: { color: palette.primary, borderRadius: [0, 3, 3, 0] } }; }),
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
        return { name: mod, value: changed, itemStyle: { color: palette.primary } };
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
        toolbox: toolbox(palette, container),
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
    function riskBand(s, l) {
      const score = sevVal[s] * likeVal[l];
      if (score >= 9) return "critical";
      if (score >= 6) return "high";
      if (score >= 3) return "medium";
      return "low";
    }
    const cellData = [];
    sevLevels.forEach(function (s, yi) {
      likeLevels.forEach(function (l, xi) {
        const band = riskBand(s, l);
        const key = band.charAt(0).toUpperCase() + band.slice(1);
        cellData.push({
          value: [xi, yi, sevVal[s] * likeVal[l]],
          severity: s,
          likelihood: l,
          band: band,
          itemStyle: {
            color: palette["risk" + key],
            borderColor: palette.riskGridBorder,
            borderWidth: 3,
            borderRadius: 7,
          },
          emphasis: {
            itemStyle: {
              color: palette["risk" + key + "Hover"],
              borderColor: palette.riskGridBorder,
              borderWidth: 3,
              shadowBlur: 3,
              shadowColor: "rgba(0,0,0,0.14)",
            },
          },
        });
      });
    });
    const issueX = likeLevels.indexOf(like);
    const issueY = sevLevels.indexOf(sev);
    const markerColor = riskMarkerColor(sev, palette);

    setChartHeight(container, 300);
    fadeIn(container);
    const chart = echarts.init(container, null, mobileInitOpts());
    chart.setOption({
      animationDuration: 200,
      animationEasing: "cubicOut",
      tooltip: mobileTooltip({
        confine: true, appendToBody: false, enterable: false, className: "ia-chart-tooltip",
        backgroundColor: palette.tooltipBg, borderWidth: 0, padding: [8, 10],
        textStyle: { color: palette.text, fontSize: 12 },
        transitionDuration: 0,
        position: matrixTooltipPosition,
        formatter: function (params) {
          const d = (params && params.data) || {};
          const activeSev = d.severity || sev;
          const activeLike = d.likelihood || like;
          const activeColor = riskMarkerColor(activeSev, palette);
          const prefix = params.seriesType === "scatter"
            ? '<b style="color:' + palette.text + ';">' + IA.escapeHtml(t("risk_matrix_root_cause")) + "</b>" +
              '<span style="color:' + palette.textDim + ';"> · </span>'
            : "";
          return '<div style="font-size:11px;white-space:nowrap;">' + prefix +
            '<span style="color:' + palette.textDim + ';">' + IA.escapeHtml(t("report_severity")) + "</span> " +
            '<b style="color:' + activeColor + ';">' + IA.escapeHtml(enumLabel("severity", activeSev)) + "</b>" +
            '<span style="color:' + palette.textDim + ';"> · ' + IA.escapeHtml(t("report_likelihood")) + "</span> " +
            "<b>" + IA.escapeHtml(enumLabel("likelihood", activeLike)) + "</b></div>";
        },
      }),
      grid: { left: 16, right: 28, top: 52, bottom: 42, containLabel: true },
      toolbox: toolbox(palette, container),
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
          type: "heatmap",
          data: cellData,
          progressive: 0,
          cursor: "pointer",
          z: 1,
        },
        {
          type: "scatter",
          symbolSize: 18,
          data: [{ value: [issueX, issueY], severity: sev, likelihood: like }],
          itemStyle: {
            color: markerColor,
            borderColor: palette.riskMarkerBorder || palette.bg || "#fff",
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
          emphasis: {
            scale: 1.18,
            itemStyle: { shadowBlur: 10, shadowColor: "rgba(0,0,0,0.34)" },
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

    const children = evidence.map(function (e, i) {
      const pathParts = String(e.path).split("/");
      const fileName = pathParts[pathParts.length - 1] || e.path;
      return {
        name: fileName,
        idx: i,
        fullPath: e.path,
        shortPath: pathParts.slice(-2).join("/") || fileName,
        fileName: fileName,
        lines: e.lines || "",
        strength: e.strength || "moderate",
        kind: e.kind || "code",
        reason: e.reason || "",
        itemStyle: { color: strengthColor(e.strength || "moderate", palette) },
      };
    });

    function evidenceLayout(width) {
      const safeWidth = Math.max(320, width || 320);
      // 常见的 2-4 条证据始终自上而下展示，避免宽屏时叶子标签挤在最右侧被裁切。
      // 只有节点较多时才使用横向紧凑树；超宽容器可容纳最多 6 个纵向叶子。
      const vertical = evidence.length <= 4 || (safeWidth >= 860 && evidence.length <= 6);
      if (vertical) {
        const columns = Math.max(1, evidence.length);
        return {
          vertical: true,
          width: safeWidth,
          height: evidence.length > 4 ? 380 : 350,
          rootLabelW: Math.max(180, Math.min(520, safeWidth - 128)),
          evidenceLabelW: Math.max(64, Math.min(168, Math.floor((safeWidth - 48) / columns) - 40)),
          rootMaxLength: 104,
          top: 140,
          bottom: 108,
          left: 32,
          right: 32,
        };
      }
      return {
        vertical: false,
        width: safeWidth,
        height: Math.max(260, Math.min(440, 164 + evidence.length * 32)),
        rootLabelW: Math.max(150, Math.min(250, Math.round(safeWidth * 0.26))),
        evidenceLabelW: Math.max(124, Math.min(220, Math.round(safeWidth * 0.22))),
        rootMaxLength: 140,
        top: 52,
        bottom: 30,
        left: 0,
        right: 0,
      };
    }

    function evidenceSeries(layout) {
      const rootLabel = rootCause.length > layout.rootMaxLength
        ? rootCause.slice(0, layout.rootMaxLength).trimEnd() + "…"
        : rootCause;
      const rootPosition = layout.vertical ? "top" : "left";
      const leafPosition = layout.vertical ? "bottom" : "right";
      return {
        id: "evidence-map-tree",
        type: "tree",
        roam: container.classList.contains("chart-modal-canvas"),
        data: [{
          name: rootLabel,
          itemStyle: { color: palette.primary, borderColor: palette.primary },
          label: {
            position: rootPosition,
            verticalAlign: layout.vertical ? "bottom" : "middle",
            align: layout.vertical ? "center" : "right",
            distance: 10,
            color: palette.text,
            fontWeight: 700,
            fontSize: 11,
            lineHeight: 17,
            width: layout.rootLabelW,
            overflow: "break",
            backgroundColor: palette.tooltipBg,
            borderColor: palette.line,
            borderWidth: 1,
            borderRadius: 4,
            padding: [6, 8],
          },
          children: children,
        }],
        orient: layout.vertical ? "TB" : "LR",
        left: layout.vertical ? layout.left : layout.rootLabelW + 32,
        right: layout.vertical ? layout.right : layout.evidenceLabelW + 36,
        top: layout.top,
        bottom: layout.bottom,
        symbol: "circle",
        symbolSize: 13,
        edgeShape: "curve",
        edgeForkPosition: "50%",
        expandAndCollapse: false,
        initialTreeDepth: -1,
        lineStyle: { color: palette.line, width: 1.5, curveness: layout.vertical ? 0.42 : 0.5 },
        itemStyle: { borderColor: palette.tooltipBg, borderWidth: 1.5 },
        label: {
          color: palette.text,
          fontSize: 11,
        },
        leaves: {
          label: {
            position: leafPosition,
            verticalAlign: layout.vertical ? "top" : "middle",
            align: layout.vertical ? "center" : "left",
            distance: 9,
            color: palette.text,
            fontSize: 10,
            lineHeight: 15,
            width: layout.evidenceLabelW,
            overflow: "truncate",
            backgroundColor: palette.tooltipBg,
            borderColor: palette.line,
            borderWidth: 1,
            borderRadius: 4,
            padding: [4, 6],
            formatter: function (params) {
              const d = params.data || {};
              const path = layout.vertical ? d.fileName : d.shortPath;
              const strength = enumLabel("strength", d.strength);
              if (layout.vertical) return [path, d.lines, strength].filter(Boolean).join("\n");
              const detail = [d.lines, strength].filter(Boolean).join(" · ");
              return path + (detail ? "\n" + detail : "");
            },
          },
        },
        emphasis: { focus: "descendant", itemStyle: { shadowBlur: 8, shadowColor: "rgba(0,0,0,0.3)" } },
      };
    }

    let layout = evidenceLayout(container.clientWidth);
    setChartHeight(container, layout.height);

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
            ': <b style="color:' + kindColor(d.kind, palette) + ';">' + IA.escapeHtml(enumLabel("kind", d.kind)) + "</b></div>" +
            '<div style="font-size:11px;color:' + palette.textDim + ';">' + IA.escapeHtml(t("evidence_strength_legend")) +
            ': <b style="color:' + strengthColor(d.strength, palette) + ';">' + IA.escapeHtml(enumLabel("strength", d.strength)) + "</b></div>" +
            (d.reason ? '<div style="color:' + palette.textDim + ';font-size:11px;margin-top:4px;max-width:280px;white-space:normal;">' + IA.escapeHtml(d.reason) + "</div>" : "");
        },
      }),
      toolbox: toolbox(palette, container),
      series: [evidenceSeries(layout)],
    });

    // 报告侧栏、分屏和全屏会改变容器宽度；必须重算方向与标签，而不只是拉伸 canvas。
    if (typeof ResizeObserver !== "undefined") {
      let resizeTimer = null;
      const observer = new ResizeObserver(function (entries) {
        const width = entries[0] && entries[0].contentRect.width;
        clearTimeout(resizeTimer);
        resizeTimer = setTimeout(function () {
          const nextLayout = evidenceLayout(width || container.clientWidth);
          if (Math.abs(nextLayout.width - layout.width) < 2 && nextLayout.vertical === layout.vertical) return;
          layout = nextLayout;
          setChartHeight(container, layout.height);
          chart.resize();
          chart.setOption({ animationDurationUpdate: 180, series: [evidenceSeries(layout)] });
        }, 80);
      });
      observer.observe(container);
      chart.__iaResizeObserver = observer;
      chart.__iaClearResizeTimer = function () { clearTimeout(resizeTimer); };
    }
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
