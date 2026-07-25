/**
 * charts.js — 报告面板三张 ECharts 图表的渲染模块
 *
 * 从 app.js 抽离，专注图表渲染逻辑。app.js 通过 IA.Charts.* 调用。
 *
 * 三张图表（全部基于真实调查数据，不做任何伪造关联）：
 * 1. 证据可信度矩阵 — 4 维度核验每条证据
 * 2. 调查覆盖图 — 已读文件是否转化为证据，暴露"引用未读取"的幻觉风险
 * 3. 调查阶段耗时 — 基于事件日志真实时间戳，定位时间开销集中点
 *
 * 所有图表数据元素（单元格/节点/层级/扇区）均支持点击下钻到对应详情。
 *
 * 配色体系（全局统一）：
 *   深蓝 #165DFF · 绿色 #00B42A · 红色 #F53F3F · 棕橙色 #FF7D00
 */
(function () {
  "use strict";

  const IA = window.IssueAgent;
  const t = IA.translate;

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
    return {
      right: 8,
      top: 0,
      feature: {
        saveAsImage: { title: t("chart_save_image"), pixelRatio: 2, backgroundColor: "transparent" },
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

  // ── 数据预处理：证据校验评分 ────────────────────────────
  // 4 个维度各打 0/0.5/1 分，综合评估证据可信度
  // 返回 { scores: [[dim0,dim1,dim2,dim3], ...], labels: [...], details: [...] }
  function scoreEvidence(report, sessionData) {
    const evidence = report.evidence || [];
    const isFileRead = buildFileReadMatcher(report, sessionData);
    // approved=审查直接通过；revised=审查后已修订（展示的即修订版，证据同样经过验证）
    // 仅 not_run / unavailable 视为未验证
    const reviewStatus = (report.review_audit && report.review_audit.status) || "not_run";
    const reviewPassed = reviewStatus === "approved" || reviewStatus === "revised";

    // 文件名智能去重：短名冲突时回退到完整路径
    const rawPaths = evidence.map(function (e) { return e.path || "unknown"; });
    const shortNames = rawPaths.map(shortName);
    const nameCount = {};
    shortNames.forEach(function (name) { nameCount[name] = (nameCount[name] || 0) + 1; });
    const labels = shortNames.map(function (name, i) {
      return nameCount[name] > 1 ? rawPaths[i] : name;
    });

    const scores = [];
    const details = [];
    evidence.forEach(function (e, i) {
      // 维度 0：文件已读取
      const fileReadScore = isFileRead(e.path) ? 1 : 0;
      // 维度 1：行号有效
      let linesScore = 0;
      if (e.lines && /^L\d+(-L?\d+)?$/.test(e.lines)) linesScore = 1;
      else if (e.lines && /^L/i.test(e.lines)) linesScore = 0.5;
      // 维度 2：理由有说明
      const reasonText = (e.reason || "").trim();
      let reasonScore = 0;
      if (reasonText.length >= 20) reasonScore = 1;
      else if (reasonText.length > 0) reasonScore = 0.5;
      // 维度 3：审查已验证
      const reviewScore = reviewPassed ? 1 : 0;

      scores.push([fileReadScore, linesScore, reasonScore, reviewScore]);
      details.push({
        path: e.path || "unknown",
        lines: e.lines || "",
        reason: reasonText,
        fileRead: fileReadScore,
        linesValid: linesScore,
        hasReason: reasonScore,
        reviewVerified: reviewScore,
        avg: (fileReadScore + linesScore + reasonScore + reviewScore) / 4,
        label: labels[i],
      });
    });

    return { scores: scores, labels: labels, details: details, evidence: evidence, reviewStatus: reviewStatus };
  }

  // ── 区块1：证据可信度矩阵 ────────────────────────────────
  // Y轴4维度 × X轴证据文件，单元格标注0~1分数，≥0.6绿/<0.6红
  // 顶部汇总卡片：总证据数、不合格数、风险清单
  function renderMatrix(container, report, sessionData) {
    if (!container) return null;
    if (!isAvailable()) {
      container.innerHTML = '<div class="report-chart-fallback">' + IA.escapeHtml(t("chart_load_failed")) + '</div>';
      return null;
    }
    const palette = getPalette();
    const data = scoreEvidence(report, sessionData);
    if (!data.evidence.length) {
      container.innerHTML = '<div class="report-chart-empty">' + IA.escapeHtml(t("report_evidence_chart_empty")) + '</div>';
      return null;
    }

    // 汇总统计卡片（插入到容器上方）
    const total = data.details.length;
    const failed = data.details.filter(function (d) { return d.avg < 0.6; });
    const failedCount = failed.length;
    const riskList = failed.map(function (d) { return d.label; }).slice(0, 5);
    const summaryEl = document.createElement("div");
    summaryEl.className = "matrix-summary";
    summaryEl.innerHTML =
      '<div class="matrix-stat">' +
        '<span class="matrix-stat-value">' + total + '</span>' +
        '<span class="matrix-stat-label">' + IA.escapeHtml(t("matrix_summary_total")) + '</span>' +
      '</div>' +
      '<div class="matrix-stat ' + (failedCount > 0 ? "matrix-stat-warn" : "") + '">' +
        '<span class="matrix-stat-value">' + failedCount + '</span>' +
        '<span class="matrix-stat-label">' + IA.escapeHtml(t("matrix_summary_failed")) + '</span>' +
      '</div>' +
      '<div class="matrix-stat matrix-stat-risk">' +
        '<span class="matrix-stat-label">' + IA.escapeHtml(t("matrix_summary_risks")) + '</span>' +
        '<span class="matrix-stat-risk-list">' + (riskList.length
          ? riskList.map(function (l) { return '<code>' + IA.escapeHtml(l) + '</code>'; }).join(" ")
          : IA.escapeHtml(t("matrix_summary_no_risks"))) + '</span>' +
      '</div>';
    // 清理可能残留的旧汇总卡片（防御性：主题切换等场景可能重复触发渲染）
    const matrixParent = container.parentElement;
    Array.from(matrixParent.querySelectorAll(".matrix-summary")).forEach(function (el) { el.remove(); });
    matrixParent.insertBefore(summaryEl, container);

    const dimensions = [
      t("matrix_dim_review_verified"),
      t("matrix_dim_has_reason"),
      t("matrix_dim_lines_valid"),
      t("matrix_dim_file_read"),
    ];

    // 构造 heatmap 数据：[x, y, value]，ECharts 类目 y 轴自下而上：
    // y=0 审查已验证 / y=1 有理由说明 / y=2 行号有效 / y=3 文件已读取，
    // 与 scores 数组 [文件已读, 行号, 理由, 审查] 顺序相反，需反向映射
    // 所有数据统一用数组格式，避免对象格式与 visualMap 冲突导致颜色映射失效
    const heatData = [];
    data.scores.forEach(function (scores, xIdx) {
      scores.forEach(function (val, yIdx) {
        heatData.push([xIdx, 3 - yIdx, val]);
      });
    });

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
          const d = data.details[params.data[0]];
          const dimIdx = params.data[1];
          const dim = dimensions[dimIdx];
          const v = params.data[2];
          const scoreText = v.toFixed(2);
          const pass = v >= 0.6;
          const statusColor = pass ? palette.success : palette.danger;
          const statusText = pass ? t("matrix_pass") : t("matrix_fail");
          // 该维度的核验原文
          let detail = "";
          if (dimIdx === 0) {
            // 区分"直接通过"与"修订后通过"，避免 revised 状态显示误导性文案
            if (d.reviewVerified >= 1) {
              detail = data.reviewStatus === "revised" ? t("matrix_detail_review_revised") : t("matrix_detail_review_pass");
            } else {
              detail = t("matrix_detail_review_fail");
            }
          }
          else if (dimIdx === 1) detail = d.reason || t("matrix_detail_no_reason");
          else if (dimIdx === 2) detail = d.lines ? (t("matrix_detail_lines") + ": " + d.lines) : t("matrix_detail_no_lines");
          else if (dimIdx === 3) detail = d.fileRead >= 1 ? (t("matrix_detail_file_read") + ": " + d.path) : t("matrix_detail_file_not_read");
          return '<div style="font-weight:600;margin-bottom:4px;">' + IA.escapeHtml(d.label) + '</div>' +
            '<div style="color:' + palette.textDim + ';font-size:11px;margin-bottom:4px;">' + IA.escapeHtml(dim) + '</div>' +
            '<div style="color:' + statusColor + ';font-weight:600;">' + IA.escapeHtml(statusText) + ' · ' + scoreText + '</div>' +
            '<div style="color:' + palette.textDim + ';font-size:11px;margin-top:4px;max-width:260px;white-space:normal;">' + IA.escapeHtml(detail) + '</div>';
        },
      }),
      grid: { left: 8, right: 16, top: 36, bottom: 60, containLabel: true },
      toolbox: toolbox(palette),
      xAxis: {
        type: "category",
        data: data.labels,
        splitArea: { show: true, areaStyle: { color: palette.splitArea } },
        axisLabel: { color: palette.textDim, fontSize: 10, rotate: 45, width: 70, overflow: "truncate", interval: 0 },
        axisLine: { lineStyle: { color: palette.line } },
        axisTick: { show: false },
      },
      yAxis: {
        type: "category",
        data: dimensions,
        splitArea: { show: true, areaStyle: { color: palette.splitArea } },
        axisLabel: { color: palette.textDim, fontSize: 11, width: 100, overflow: "truncate" },
        axisLine: { lineStyle: { color: palette.line } },
        axisTick: { show: false },
      },
      visualMap: {
        min: 0,
        max: 1,
        show: false,
        // ≥0.6 绿色，<0.6 红色；中间过渡区域极窄实现双档效果
        inRange: { color: [palette.danger, palette.danger, palette.success, palette.success] },
      },
      series: [{
        type: "heatmap",
        data: heatData,
        itemStyle: { borderRadius: 3, borderColor: palette.tooltipBorder, borderWidth: 2 },
        emphasis: { itemStyle: { shadowBlur: 14, shadowColor: "rgba(0,0,0,0.4)" } },
        // 单元格内标注精确分数
        label: {
          show: true,
          color: "#ffffff",
          fontSize: 10,
          fontWeight: 600,
          formatter: function (params) {
            return params.data[2].toFixed(1);
          },
        },
      }],
    });
    // 低分警示：容器层 CSS 呼吸动画，完全不碰 echarts 内部状态
    if (failedCount > 0) container.classList.add("pulse-ring");

    // 点击下钻：单元格 / x 轴标签 → 跳转到对应证据条目并高亮
    // 行业惯例（GitHub Insights、Datadog）：图表数据元素可点击跳转到详情视图
    chart.on("click", function (params) {
      if (params.componentType === "series" && params.seriesType === "heatmap") {
        // 单元格 → 跳到对应证据条目
        const xIdx = params.data[0];
        IA.jumpToEvidence(xIdx);
      } else if (params.componentType === "xAxis") {
        // x 轴标签（证据文件名）→ 同样跳到对应证据条目
        const idx = data.labels.indexOf(params.value);
        if (idx >= 0) IA.jumpToEvidence(idx);
      } else if (params.componentType === "yAxis") {
        // y 轴标签（维度名）→ 高亮该维度所有不合格单元格 1.6s
        const dimIdx = dimensions.indexOf(params.value);
        if (dimIdx < 0) return;
        const failedCells = [];
        data.scores.forEach(function (scores, xIdx) {
          if (scores[dimIdx] < 0.6) failedCells.push([xIdx, dimIdx, scores[dimIdx]]);
        });
        if (!failedCells.length) return;
        chart.setOption({
          series: [{
            data: heatData,
            emphasis: { itemStyle: { shadowBlur: 18, shadowColor: "rgba(245,63,63,0.6)" } },
          }],
        });
        // 临时高亮不合格单元格
        chart.dispatchAction({ type: "highlight", seriesIndex: 0, dataIndex: 0 });
        setTimeout(function () {
          chart.dispatchAction({ type: "downplay", seriesIndex: 0, dataIndex: 0 });
        }, 1600);
      }
    });
    // 鼠标悬停在数据元素上时切换为 pointer 光标，提示可点击
    chart.on("mouseover", function () { container.style.cursor = "pointer"; });
    chart.on("mouseout", function () { container.style.cursor = ""; });
    return chart;
  }

  // ── 区块2：调查覆盖图 ────────────────────────────────────
  // 横向条形图对照"读过的文件"与"报告引用的证据"，全部来自真实数据：
  //   绿 = 已读取且转化为证据（按证据条数排序）
  //   灰 = 已读取但报告未引用（覆盖面的一部分，不算问题）
  //   红 = 报告引用但从未读取 —— 幻觉风险，置顶警示
  function renderCoverage(container, report, sessionData) {
    if (!container) return null;
    if (!isAvailable()) {
      container.innerHTML = '<div class="report-chart-fallback">' + IA.escapeHtml(t("chart_load_failed")) + '</div>';
      return null;
    }
    const palette = getPalette();
    const evidence = report.evidence || [];

    // 已读文件集合（归一化去重）
    const readSources = [];
    if (sessionData && Array.isArray(sessionData.files_read)) Array.prototype.push.apply(readSources, sessionData.files_read);
    if (Array.isArray(report.files_examined)) Array.prototype.push.apply(readSources, report.files_examined);
    const readPaths = [];
    const readSeen = {};
    readSources.forEach(function (p) {
      const n = normPath(p);
      if (n && !readSeen[n]) { readSeen[n] = true; readPaths.push(n); }
    });
    const isFileRead = buildFileReadMatcher(report, sessionData);

    // 证据按文件聚合：条数 + 首条证据索引（点击下钻用）
    const evidenceByFile = {};
    const evidenceOrder = [];
    evidence.forEach(function (e, i) {
      const n = normPath(e.path || "unknown");
      if (!evidenceByFile[n]) {
        evidenceByFile[n] = { count: 0, firstIdx: i, raw: e.path || "unknown" };
        evidenceOrder.push(n);
      }
      evidenceByFile[n].count += 1;
    });

    if (!evidenceOrder.length && !readPaths.length) {
      container.innerHTML = '<div class="report-chart-empty">' + IA.escapeHtml(t("coverage_empty")) + '</div>';
      return null;
    }

    // 分类证据文件：supported（已读且引用）/ phantom（引用但从未读取）
    // 已读集合为空（旧会话缺 files_read 数据）时无法判定幻觉，全部按 supported 处理
    const supported = [];
    const phantom = [];
    evidenceOrder.forEach(function (n) {
      const item = evidenceByFile[n];
      const row = { name: shortName(item.raw), path: item.raw, count: item.count, firstIdx: item.firstIdx };
      if (readPaths.length && !isFileRead(item.raw)) {
        row.status = "phantom";
        phantom.push(row);
      } else {
        row.status = "supported";
        supported.push(row);
      }
    });
    // 类目轴自下而上，升序排列后条数最多的靠上
    supported.sort(function (a, b) { return a.count - b.count; });

    // 已读但报告未引用的文件
    const unused = [];
    readPaths.forEach(function (n) {
      const referenced = evidenceOrder.some(function (ev) {
        return ev === n || ev.endsWith("/" + n) || n.endsWith("/" + ev);
      });
      if (!referenced) unused.push({ name: shortName(n), path: n, count: 0, firstIdx: -1, status: "unused" });
    });

    // 行数控制：phantom / supported 全保留，unused 超出预算时聚合为一行
    const MAX_ROWS = 14;
    const budget = Math.max(MAX_ROWS - phantom.length - supported.length, 1);
    let unusedShown = unused;
    let aggregated = null;
    if (unused.length > budget) {
      unusedShown = unused.slice(0, budget - 1);
      aggregated = {
        name: t("coverage_others", { n: unused.length - unusedShown.length }),
        path: "", count: 0, firstIdx: -1, status: "unused", aggregate: true,
      };
    }

    // 行序（自下而上）：聚合行 → 未引用 → 证据文件（升序）→ 幻觉引用置顶
    const rows = [];
    if (aggregated) rows.push(aggregated);
    Array.prototype.push.apply(rows, unusedShown.slice().reverse());
    Array.prototype.push.apply(rows, supported);
    Array.prototype.push.apply(rows, phantom);

    const statusColor = { supported: palette.success, unused: palette.muted, phantom: palette.danger };
    const statusLabel = {
      supported: t("coverage_read_supported"),
      unused: t("coverage_read_unused"),
      phantom: t("coverage_not_read"),
    };
    const maxCount = rows.reduce(function (m, r) { return Math.max(m, r.count); }, 1);

    // 覆盖率统计：已读文件中有多少转化为证据
    let statText = "";
    if (readPaths.length) {
      const usedCount = readPaths.length - unused.length;
      const pct = Math.round((usedCount / readPaths.length) * 100);
      statText = t("coverage_stat", { used: usedCount, total: readPaths.length, pct: pct });
    }

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
          let html = '<div style="font-weight:600;margin-bottom:4px;max-width:280px;white-space:normal;word-break:break-all;">' + IA.escapeHtml(row.path || row.name) + '</div>' +
            '<div style="color:' + statusColor[row.status] + ';font-weight:600;font-size:11px;">' + IA.escapeHtml(statusLabel[row.status]) + '</div>';
          if (row.count > 0) {
            html += '<div style="color:' + palette.textDim + ';font-size:11px;margin-top:2px;">' + IA.escapeHtml(t("coverage_evidence_count")) + ': <b style="color:' + palette.text + ';">' + row.count + '</b></div>';
          }
          if (row.status === "phantom") {
            html += '<div style="color:' + palette.danger + ';font-size:11px;margin-top:4px;max-width:260px;white-space:normal;">' + IA.escapeHtml(t("coverage_phantom_hint")) + '</div>';
          }
          return html;
        },
      }),
      grid: { left: 8, right: 48, top: 36, bottom: statText ? 28 : 12, containLabel: true },
      toolbox: toolbox(palette),
      title: statText ? {
        text: statText,
        left: "center",
        bottom: 0,
        textStyle: { color: palette.textDim, fontSize: 11, fontWeight: 500 },
      } : undefined,
      xAxis: {
        type: "value",
        max: maxCount,
        minInterval: 1,
        axisLabel: { color: palette.textDim, fontSize: 10 },
        splitLine: { lineStyle: { color: palette.line, opacity: 0.4 } },
      },
      yAxis: {
        type: "category",
        data: rows.map(function (r) { return r.name; }),
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
            itemStyle: { color: statusColor[r.status], borderRadius: [0, 3, 3, 0], opacity: r.status === "unused" ? 0.55 : 1 },
          };
        }),
        barMaxWidth: 16,
        // 条形右侧标注：有证据显示条数，零值行显示状态文本
        label: {
          show: true,
          position: "right",
          fontSize: 10,
          fontWeight: 600,
          formatter: function (params) {
            const row = rows[params.dataIndex];
            if (!row) return "";
            if (row.count > 0) return String(row.count) + (row.status === "phantom" ? " !" : "");
            return statusLabel.unused;
          },
          color: palette.textDim,
        },
        emphasis: { itemStyle: { shadowBlur: 8, shadowColor: "rgba(0,0,0,0.3)" } },
      }],
    });
    // 存在幻觉引用时容器层呼吸警示（与矩阵图一致的提醒机制）
    if (phantom.length) container.classList.add("pulse-ring");

    // 点击下钻：证据行 → 跳到对应证据条目；其余行 → 跳到证据章节
    chart.on("click", function (params) {
      const row = rows[params.dataIndex];
      if (row && row.firstIdx >= 0) IA.jumpToEvidence(row.firstIdx);
      else IA.jumpToSection("report-evidence");
    });
    chart.on("mouseover", function () { container.style.cursor = "pointer"; });
    chart.on("mouseout", function () { container.style.cursor = ""; });
    return chart;
  }

  // ── 区块3：调查阶段耗时 ─────────────────────────────────
  // 基于事件日志真实时间戳差分各阶段耗时，并统计阶段内工具调用次数。
  // 耗时最长的阶段用橙色高亮，一眼定位时间开销集中点。
  // 旧会话无阶段事件时回退为调查活动量概览（同样是真实指标）。
  function renderTimeline(container, report, sessionData) {
    if (!container) return null;
    if (!isAvailable()) {
      container.innerHTML = '<div class="report-chart-fallback">' + IA.escapeHtml(t("chart_load_failed")) + '</div>';
      return null;
    }
    const palette = getPalette();
    const events = (sessionData && sessionData.events) || [];

    // 解析事件时间戳（created_at 为秒级 ISO 字符串）
    const stamps = [];
    events.forEach(function (ev) {
      const ts = Date.parse(ev.created_at || "");
      if (!isNaN(ts)) stamps.push({ type: ev.type, phase: ev.data && ev.data.phase, ts: ts });
    });
    const phases = stamps.filter(function (s) { return s.type === "phase" && s.phase; });

    // 秒级时长格式化：不足 1 秒统一显示 <1s
    function formatSecs(seconds) {
      if (seconds < 1) return "<1s";
      return IA.formatDuration(Math.round(seconds) * 1000);
    }

    if (phases.length) {
      // 各阶段耗时 = 下一阶段开始时间 − 本阶段开始时间；末段截止到最后一个事件
      const endTs = stamps.reduce(function (m, s) { return Math.max(m, s.ts); }, phases[phases.length - 1].ts);
      const segs = [];
      phases.forEach(function (p, i) {
        const start = p.ts;
        const end = i + 1 < phases.length ? phases[i + 1].ts : endTs;
        let tools = 0;
        stamps.forEach(function (s) {
          if (s.type !== "tool_call") return;
          if (s.ts >= start && (i + 1 < phases.length ? s.ts < end : s.ts <= end)) tools += 1;
        });
        segs.push({
          label: IA.enumLabel("phase", p.phase),
          seconds: Math.max(0, (end - start) / 1000),
          tools: tools,
        });
      });
      const totalSecs = Math.max(0, (endTs - phases[0].ts) / 1000);
      // 瓶颈阶段：耗时最长且 >0 的阶段用橙色高亮
      let maxIdx = -1;
      let maxSecs = 0;
      segs.forEach(function (s, i) {
        if (s.seconds > maxSecs) { maxSecs = s.seconds; maxIdx = i; }
      });
      // 类目轴自下而上：反转数组让第一个阶段显示在最上方
      const displaySegs = segs.slice().reverse();
      const bottleneckDisplayIdx = maxIdx >= 0 ? segs.length - 1 - maxIdx : -1;

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
            const seg = displaySegs[params.dataIndex];
            if (!seg) return "";
            const pct = totalSecs > 0 ? Math.round((seg.seconds / totalSecs) * 100) : 0;
            return '<div style="font-weight:600;margin-bottom:4px;">' + IA.escapeHtml(seg.label) + '</div>' +
              '<div>' + IA.escapeHtml(formatSecs(seg.seconds)) + ' · ' + pct + '%</div>' +
              '<div style="color:' + palette.textDim + ';font-size:11px;margin-top:2px;">' + IA.escapeHtml(t("timeline_tools", { n: seg.tools })) + '</div>';
          },
        }),
        grid: { left: 8, right: 96, top: 36, bottom: 28, containLabel: true },
        toolbox: toolbox(palette),
        title: {
          text: t("timeline_total", { duration: formatSecs(totalSecs) }),
          left: "center",
          bottom: 0,
          textStyle: { color: palette.textDim, fontSize: 11, fontWeight: 500 },
        },
        xAxis: {
          type: "value",
          axisLabel: {
            color: palette.textDim,
            fontSize: 10,
            formatter: function (v) { return v + "s"; },
          },
          splitLine: { lineStyle: { color: palette.line, opacity: 0.4 } },
        },
        yAxis: {
          type: "category",
          data: displaySegs.map(function (s) { return s.label; }),
          axisLabel: { color: palette.textDim, fontSize: 11, width: isMobile() ? 72 : 110, overflow: "truncate" },
          axisLine: { lineStyle: { color: palette.line } },
          axisTick: { show: false },
        },
        series: [{
          type: "bar",
          data: displaySegs.map(function (s, i) {
            return {
              value: Math.round(s.seconds * 10) / 10,
              itemStyle: {
                color: i === bottleneckDisplayIdx ? palette.warning : palette.primary,
                borderRadius: [0, 3, 3, 0],
              },
            };
          }),
          barMaxWidth: 18,
          label: {
            show: true,
            position: "right",
            fontSize: 10,
            fontWeight: 600,
            color: palette.textDim,
            formatter: function (params) {
              const seg = displaySegs[params.dataIndex];
              if (!seg) return "";
              return formatSecs(seg.seconds) + (seg.tools > 0 ? " · " + t("timeline_tools", { n: seg.tools }) : "");
            },
          },
          emphasis: { itemStyle: { shadowBlur: 8, shadowColor: "rgba(0,0,0,0.3)" } },
        }],
      });
      return chart;
    }

    // 回退：无阶段事件（旧会话）→ 调查活动量概览
    const metrics = (sessionData && sessionData.metrics) || {};
    const filesRead = parseInt(metrics.files_read, 10)
      || (sessionData && sessionData.files_read ? sessionData.files_read.length : 0)
      || (report.files_examined || []).length
      || 0;
    const bars = [
      { label: t("timeline_metric_evidence"), value: (report.evidence || []).length, color: palette.success },
      { label: t("timeline_metric_files_read"), value: filesRead, color: palette.muted },
      { label: t("timeline_metric_tool_calls"), value: parseInt(metrics.tool_calls, 10) || 0, color: palette.warning },
      { label: t("timeline_metric_model_calls"), value: parseInt(metrics.model_calls, 10) || 0, color: palette.primary },
    ];
    if (!bars.some(function (b) { return b.value > 0; })) {
      container.innerHTML = '<div class="report-chart-empty">' + IA.escapeHtml(t("timeline_empty")) + '</div>';
      return null;
    }

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
          return '<div style="font-weight:600;">' + IA.escapeHtml(params.name) + ': ' + params.value + '</div>';
        },
      }),
      grid: { left: 8, right: 48, top: 36, bottom: 28, containLabel: true },
      toolbox: toolbox(palette),
      title: {
        text: t("timeline_fallback_note"),
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
        data: bars.map(function (b) { return b.label; }),
        axisLabel: { color: palette.textDim, fontSize: 11, width: isMobile() ? 72 : 110, overflow: "truncate" },
        axisLine: { lineStyle: { color: palette.line } },
        axisTick: { show: false },
      },
      series: [{
        type: "bar",
        data: bars.map(function (b) {
          return { value: b.value, itemStyle: { color: b.color, borderRadius: [0, 3, 3, 0] } };
        }),
        barMaxWidth: 18,
        label: { show: true, position: "right", fontSize: 10, fontWeight: 600, color: palette.textDim },
        emphasis: { itemStyle: { shadowBlur: 8, shadowColor: "rgba(0,0,0,0.3)" } },
      }],
    });
    return chart;
  }

  // ── 导出 ────────────────────────────────────────────────
  IA.Charts = {
    renderMatrix: renderMatrix,
    renderCoverage: renderCoverage,
    renderTimeline: renderTimeline,
    isAvailable: isAvailable,
    getPalette: getPalette,
  };
})();
