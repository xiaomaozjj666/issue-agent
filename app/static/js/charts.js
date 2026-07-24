/**
 * charts.js — 报告面板三张 ECharts 图表的渲染模块
 *
 * 从 app.js 抽离，专注图表渲染逻辑。app.js 通过 IA.Charts.* 调用。
 *
 * 三张图表各自回答一个核心问题：
 * 1. 证据可信度矩阵 — 哪些证据扎实、哪些是凑数？
 * 2. 证据-根因桑基图 — 结论是怎么推导出来的？根因是否有充足支撑？
 * 3. 调查效率漏斗+损耗饼图 — Agent 执行链路的效率瓶颈在哪？
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

  // 200ms 淡入动画：图表初始化后给容器加一个 opacity 过渡
  function fadeIn(container) {
    if (!container) return;
    container.style.opacity = "0";
    container.style.transition = "opacity 200ms ease";
    requestAnimationFrame(function () {
      container.style.opacity = "1";
    });
  }

  // Sankey 窄容器自适应
  function sankeyLayoutFor(container) {
    const w = (container && container.clientWidth) || 600;
    if (w < 320) return { left: 8, right: 56, fontSize: 9, nodeGap: 6, nodeWidth: 10 };
    if (w < 480) return { left: 10, right: 64, fontSize: 10, nodeGap: 8, nodeWidth: 12 };
    if (w < 640) return { left: 14, right: 72, fontSize: 10, nodeGap: 10, nodeWidth: 14 };
    return { left: 16, right: 80, fontSize: 11, nodeGap: 10, nodeWidth: 14 };
  }

  // 复合实例：多个 echarts 实例包装成一个（供 funnel+pie 使用）
  function composite(charts) {
    return {
      getDom: function () { return charts[0] ? charts[0].getDom() : null; },
      setOption: function (opt, opts) { charts.forEach(function (c) { try { c.setOption(opt, opts); } catch (e) {} }); },
      resize: function () { charts.forEach(function (c) { try { c.resize(); } catch (e) {} }); },
      dispose: function () { charts.forEach(function (c) { try { c.dispose(); } catch (e) {} }); },
    };
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

  // ── 数据预处理：证据校验评分 ────────────────────────────
  // 4 个维度各打 0/0.5/1 分，综合评估证据可信度
  // 返回 { scores: [[dim0,dim1,dim2,dim3], ...], labels: [...], details: [...] }
  function scoreEvidence(report, sessionData) {
    const evidence = report.evidence || [];
    const isFileRead = buildFileReadMatcher(report, sessionData);
    const reviewPassed = report.review_audit && report.review_audit.status === "approved";

    // 文件名智能去重：短名冲突时回退到完整路径
    const rawPaths = evidence.map(function (e) { return e.path || "unknown"; });
    const shortNames = rawPaths.map(function (path) {
      const parts = path.split("/");
      return parts.length > 2 ? "…/" + parts.slice(-2).join("/") : path;
    });
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

    return { scores: scores, labels: labels, details: details, evidence: evidence };
  }

  // 计算证据-根因支撑权重 (0~1)
  function supportWeight(e, isFileRead) {
    const fileRead = isFileRead(e.path) ? 1 : 0;
    let linesValid = 0;
    if (e.lines && /^L\d+(-L?\d+)?$/.test(e.lines)) linesValid = 1;
    else if (e.lines && /^L/i.test(e.lines)) linesValid = 0.5;
    const reasonText = (e.reason || "").trim();
    let reasonQuality = 0;
    if (reasonText.length >= 20) reasonQuality = 1;
    else if (reasonText.length > 0) reasonQuality = 0.5;
    // 权重 = 文件读取(40%) + 行号有效(30%) + 理由质量(30%)
    return fileRead * 0.4 + linesValid * 0.3 + reasonQuality * 0.3;
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

    // 构造 heatmap 数据：[x, y, value]，y 轴从上到下 = dimensions 数组顺序
    const heatData = [];
    data.scores.forEach(function (scores, xIdx) {
      scores.forEach(function (val, yIdx) {
        heatData.push([xIdx, yIdx, val]);
      });
    });

    fadeIn(container);
    const chart = echarts.init(container, null, mobileInitOpts());
    chart.setOption({
      animationDuration: 200,
      animationEasing: "cubicOut",
      tooltip: mobileTooltip({
        confine: true,
        backgroundColor: palette.tooltipBg,
        borderWidth: 0,
        padding: [10, 14],
        textStyle: { color: palette.text, fontSize: 12 },
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
          if (dimIdx === 0) detail = d.reviewVerified >= 1 ? t("matrix_detail_review_pass") : t("matrix_detail_review_fail");
          else if (dimIdx === 1) detail = d.reason || t("matrix_detail_no_reason");
          else if (dimIdx === 2) detail = d.lines ? (t("matrix_detail_lines") + ": " + d.lines) : t("matrix_detail_no_lines");
          else if (dimIdx === 3) detail = d.fileRead >= 1 ? (t("matrix_detail_file_read") + ": " + d.path) : t("matrix_detail_file_not_read");
          return '<div style="font-weight:600;margin-bottom:4px;">' + IA.escapeHtml(d.label) + '</div>' +
            '<div style="color:' + palette.textDim + ';font-size:11px;margin-bottom:4px;">' + IA.escapeHtml(dim) + '</div>' +
            '<div style="color:' + statusColor + ';font-weight:600;">' + IA.escapeHtml(statusText) + ' · ' + scoreText + '</div>' +
            '<div style="color:' + palette.textDim + ';font-size:11px;margin-top:4px;max-width:260px;white-space:normal;">' + IA.escapeHtml(detail) + '</div>';
        },
      }),
      grid: { left: 8, right: 16, top: 16, bottom: 60, containLabel: true },
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
        emphasis: { itemStyle: { shadowBlur: 8, shadowColor: "rgba(0,0,0,0.3)" } },
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
    return chart;
  }

  // ── 区块2：证据-根因支撑桑基流向图 ──────────────────────
  // Issue → 根因论点 → 证据文件
  // 连线粗细 = 支撑权重(0~1)，≥0.7 绿色强支撑 / <0.7 灰色弱支撑
  function renderSankey(container, report, sessionData) {
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

    // 从 root_cause 提取关键论点作为中间节点
    const causeText = report.root_cause || t("sankey_default_cause");
    const causeParts = causeText.split(/[。.；;]/).filter(function (s) { return s.trim(); });
    const causeFullTexts = causeParts.slice(0, 2).map(function (s) { return s.trim(); });
    if (!causeFullTexts.length) causeFullTexts.push(t("sankey_default_cause"));
    const causeLabels = causeFullTexts.map(function (_text, i) {
      return t("sankey_cause_node_label", { n: i + 1 });
    });

    // 构造节点
    const nodes = [];
    nodes.push({ name: t("sankey_issue_node"), itemStyle: { color: palette.primary } });
    causeLabels.forEach(function (c) {
      nodes.push({ name: c, itemStyle: { color: palette.warning } });
    });
    // 证据节点：明文文件名 + 行号
    const fileNames = evidence.map(function (e) {
      const path = e.path || "unknown";
      const parts = path.split("/");
      const shortPath = parts.length > 2 ? "…/" + parts.slice(-2).join("/") : path;
      return e.lines ? shortPath + " " + e.lines : shortPath;
    });
    fileNames.forEach(function (f, i) {
      const w = supportWeight(evidence[i], isFileRead);
      nodes.push({ name: f, itemStyle: { color: w >= 0.7 ? palette.success : palette.muted } });
    });

    // 构造连线
    const links = [];
    // Issue → 每个根因论点（等权）
    causeLabels.forEach(function (c) {
      links.push({ source: t("sankey_issue_node"), target: c, value: 1 });
    });
    // 根因论点 → 证据，value = 支撑权重（放大到 1~10 以便 Sankey 渲染）
    const edgeDetails = [];
    evidence.forEach(function (e, i) {
      const targetCause = causeLabels[i % causeLabels.length];
      const fileName = fileNames[i];
      const w = supportWeight(e, isFileRead);
      // Sankey value 需 > 0，权重 0 时给最小值 0.1
      const sankeyValue = Math.max(w * 10, 0.1);
      links.push({
        source: targetCause,
        target: fileName,
        value: sankeyValue,
        lineStyle: { color: w >= 0.7 ? palette.success : palette.muted, opacity: w >= 0.7 ? 0.6 : 0.3 },
      });
      edgeDetails.push({ source: targetCause, target: fileName, weight: w, reason: e.reason || "" });
    });

    const layout = sankeyLayoutFor(container);
    fadeIn(container);
    const chart = echarts.init(container, null, mobileInitOpts());
    chart.setOption({
      animationDuration: 200,
      animationEasing: "cubicOut",
      tooltip: mobileTooltip({
        confine: true,
        backgroundColor: palette.tooltipBg,
        borderWidth: 0,
        padding: [10, 14],
        textStyle: { color: palette.text, fontSize: 12 },
        formatter: function (params) {
          if (params.dataType === "edge") {
            const edge = edgeDetails.find(function (e) {
              return e.source === params.data.source && e.target === params.data.target;
            });
            if (edge) {
              const strong = edge.weight >= 0.7;
              const label = strong ? t("sankey_strong_support") : t("sankey_weak_support");
              const color = strong ? palette.success : palette.muted;
              return '<div style="font-weight:600;margin-bottom:4px;">' + IA.escapeHtml(edge.source) + ' → ' + IA.escapeHtml(edge.target) + '</div>' +
                '<div style="color:' + color + ';font-size:11px;">' + IA.escapeHtml(t("sankey_weight")) + ': ' + edge.weight.toFixed(2) + ' · ' + IA.escapeHtml(label) + '</div>' +
                (edge.reason ? '<div style="color:' + palette.textDim + ';font-size:11px;margin-top:4px;max-width:280px;white-space:normal;">' + IA.escapeHtml(edge.reason) + '</div>' : '');
            }
          }
          // 节点：根因论点显示完整文本
          const causeIdx = causeLabels.indexOf(params.name);
          if (causeIdx >= 0 && causeFullTexts[causeIdx]) {
            return '<div style="font-weight:600;margin-bottom:4px;">' + IA.escapeHtml(params.name) + '</div>' +
              '<div style="color:' + palette.textDim + ';font-size:11px;max-width:280px;white-space:normal;">' + IA.escapeHtml(causeFullTexts[causeIdx]) + '</div>';
          }
          return '<div style="font-weight:600;">' + IA.escapeHtml(params.name) + '</div>';
        },
      }),
      toolbox: toolbox(palette),
      legend: {
        show: true,
        right: 8,
        top: 0,
        data: [
          { name: t("sankey_legend_strong"), icon: "circle", itemStyle: { color: palette.success } },
          { name: t("sankey_legend_weak"), icon: "circle", itemStyle: { color: palette.muted } },
        ],
        textStyle: { color: palette.textDim, fontSize: 10 },
        itemWidth: 8,
        itemHeight: 8,
        itemGap: 8,
      },
      series: [{
        type: "sankey",
        data: nodes,
        links: links,
        orient: "horizontal",
        left: layout.left,
        right: layout.right,
        top: 28,
        bottom: 16,
        nodeWidth: layout.nodeWidth,
        nodeGap: layout.nodeGap,
        nodeAlign: "justify",
        layoutIterations: 32,
        label: {
          color: palette.text,
          fontSize: layout.fontSize,
          fontWeight: 500,
          formatter: function (params) {
            const name = params && params.name != null ? params.name : "";
            const max = layout.fontSize <= 10 ? 14 : 20;
            if (typeof name !== "string") return String(name);
            return name.length > max ? name.slice(0, max) + "…" : name;
          },
        },
        lineStyle: { curveness: 0.5 },
        emphasis: { focus: "adjacency", lineStyle: { opacity: 0.8 } },
      }],
    });
    return chart;
  }

  // ── 区块3：调查效率漏斗 + 损耗饼图 ──────────────────────
  // 漏斗：模型调用 → 工具调用 → 文件读取 → 有效证据
  // 每层标注【数值 + 环比转化率】，层级间隙标注损耗原因
  // 底部饼图：无效算力开销分类占比
  function renderFunnel(container, report, sessionData) {
    if (!container) return null;
    if (!isAvailable()) {
      container.innerHTML = '<div class="report-chart-fallback">' + IA.escapeHtml(t("chart_load_failed")) + '</div>';
      return null;
    }
    const palette = getPalette();
    const metrics = (sessionData && sessionData.metrics) || {};
    const modelCalls = parseInt(metrics.model_calls, 10) || 0;
    const toolCalls = parseInt(metrics.tool_calls, 10) || 0;
    const filesRead = parseInt(metrics.files_read, 10)
      || (sessionData && sessionData.files_read ? sessionData.files_read.length : 0)
      || (report.files_examined || []).length
      || 0;
    const validEvidence = report.evidence_audit ? report.evidence_audit.valid_references : (report.evidence || []).length;

    if (!modelCalls && !toolCalls && !filesRead && !validEvidence) {
      container.innerHTML = '<div class="report-chart-empty">' + IA.escapeHtml(t("funnel_empty")) + '</div>';
      return null;
    }

    const layers = [
      { name: t("funnel_model_calls"), raw: modelCalls, color: palette.primary },
      { name: t("funnel_tool_calls"), raw: toolCalls, color: palette.warning },
      { name: t("funnel_files_read"), raw: filesRead, color: palette.danger },
      { name: t("funnel_valid_evidence"), raw: validEvidence, color: palette.success },
    ].filter(function (d) { return d.raw > 0; });

    // 损耗原因：层级间隙标注
    const lossReasons = [
      { from: t("funnel_model_calls"), to: t("funnel_tool_calls"), reason: t("funnel_loss_model_to_tool") },
      { from: t("funnel_tool_calls"), to: t("funnel_files_read"), reason: t("funnel_loss_tool_to_files") },
      { from: t("funnel_files_read"), to: t("funnel_valid_evidence"), reason: t("funnel_loss_files_to_evidence") },
    ];

    // 全局资源利用率
    const globalUtil = modelCalls > 0 ? ((validEvidence / modelCalls) * 100).toFixed(1) : "0";

    // 损耗原因文本块（插入到漏斗下方、饼图上方）
    const lossEl = document.createElement("div");
    lossEl.className = "funnel-loss-notes";
    const applicableLosses = lossReasons.filter(function (lr) {
      const fromLayer = layers.find(function (d) { return d.name === lr.from; });
      const toLayer = layers.find(function (d) { return d.name === lr.to; });
      return fromLayer && toLayer && fromLayer.raw > toLayer.raw;
    });
    lossEl.innerHTML = applicableLosses.length
      ? applicableLosses.map(function (lr) {
          return '<span class="funnel-loss-item">' + IA.escapeHtml(lr.from) + ' → ' + IA.escapeHtml(lr.to) + ': ' + IA.escapeHtml(lr.reason) + '</span>';
        }).join("")
      : '<span class="funnel-loss-item">' + IA.escapeHtml(t("funnel_no_waste")) + '</span>';

    fadeIn(container);
    const chart = echarts.init(container, null, mobileInitOpts());
    chart.setOption({
      animationDuration: 200,
      animationEasing: "cubicOut",
      tooltip: mobileTooltip({
        confine: true,
        backgroundColor: palette.tooltipBg,
        borderWidth: 0,
        padding: [10, 14],
        textStyle: { color: palette.text, fontSize: 12 },
        formatter: function (params) {
          const item = layers.find(function (d) { return d.name === params.name; });
          if (!item) return '<div style="font-weight:600;">' + IA.escapeHtml(params.name) + '</div>';
          const raw = item.raw;
          const idx = layers.indexOf(item);
          const prevItem = idx > 0 ? layers[idx - 1] : null;
          const prevRaw = prevItem ? prevItem.raw : 0;
          const convRate = prevRaw > 0 ? ((raw / prevRaw) * 100).toFixed(1) : "100";
          const overallRate = modelCalls > 0 ? ((raw / modelCalls) * 100).toFixed(1) : "100";
          const hasPrev = idx > 0;
          let html = '<div style="font-weight:600;margin-bottom:4px;">' + IA.escapeHtml(params.name) + '</div>';
          html += '<div>' + IA.escapeHtml(t("funnel_count")) + ': <b>' + raw + '</b></div>';
          if (hasPrev) {
            html += '<div style="color:' + palette.textDim + ';font-size:11px;">' + IA.escapeHtml(t("funnel_conversion")) + ': ' + convRate + '%</div>';
            // 显示该层级与上一级之间的损耗原因
            const loss = lossReasons.find(function (lr) { return lr.to === item.name; });
            if (loss) {
              html += '<div style="color:' + palette.danger + ';font-size:11px;margin-top:2px;">' + IA.escapeHtml(loss.reason) + '</div>';
            }
          }
          html += '<div style="color:' + palette.textDim + ';font-size:11px;">' + IA.escapeHtml(t("funnel_overall")) + ': ' + overallRate + '%</div>';
          return html;
        },
      }),
      toolbox: toolbox(palette),
      // 标题：全局利用率
      title: {
        text: IA.escapeHtml(t("funnel_global_utilization")) + ': ' + globalUtil + '%',
        left: "center",
        bottom: 0,
        textStyle: { color: palette.textDim, fontSize: 11, fontWeight: 500 },
      },
      series: [{
        type: "funnel",
        data: layers.map(function (d) {
          return { name: d.name, value: d.raw, itemStyle: { color: d.color } };
        }),
        left: "10%",
        right: "10%",
        top: 16,
        bottom: 30,
        width: "80%",
        minSize: "20%",
        maxSize: "100%",
        sort: "descending",
        gap: 4,
        label: {
          show: true,
          position: "inside",
          color: "#ffffff",
          fontSize: 11,
          fontWeight: 600,
          formatter: function (params) {
            const item = layers.find(function (d) { return d.name === params.name; });
            if (!item) return params.name;
            const idx = layers.indexOf(item);
            const prevItem = idx > 0 ? layers[idx - 1] : null;
            const prevRaw = prevItem ? prevItem.raw : 0;
            const conv = prevRaw > 0 ? ((item.raw / prevRaw) * 100).toFixed(0) : "100";
            return item.name + ': ' + item.raw + (idx > 0 ? ' (' + conv + '%)' : '');
          },
        },
        labelLine: { show: false },
        itemStyle: { borderWidth: 0, borderRadius: 2 },
        emphasis: { itemStyle: { shadowBlur: 8, shadowColor: "rgba(0,0,0,0.3)" } },
      }],
    });

    // 损耗饼图：在漏斗下方插入损耗原因文本块 + 饼图容器
    // 先清理可能残留的旧元素（防御性：refreshReportCharts 不会重建 DOM，但主题切换等场景可能重复触发）
    const parent = container.parentElement;
    Array.from(parent.querySelectorAll(".funnel-loss-notes, .funnel-pie-container")).forEach(function (el) {
      el.remove();
    });
    // 顺序：漏斗(container) → 损耗原因(lossEl) → 饼图(pieContainer) → 图表说明(caption)
    parent.insertBefore(lossEl, container.nextSibling);
    const pieContainer = document.createElement("div");
    pieContainer.className = "funnel-pie-container";
    pieContainer.style.width = "100%";
    pieContainer.style.height = "160px";
    parent.insertBefore(pieContainer, lossEl.nextSibling);

    // 损耗分类数据
    const wasteData = [];
    if (modelCalls - toolCalls > 0) {
      wasteData.push({ name: t("funnel_waste_invalid_calls"), value: modelCalls - toolCalls, color: palette.primary });
    }
    if (toolCalls - filesRead > 0) {
      wasteData.push({ name: t("funnel_waste_failed_tools"), value: toolCalls - filesRead, color: palette.warning });
    }
    if (filesRead - validEvidence > 0) {
      wasteData.push({ name: t("funnel_waste_unused_files"), value: filesRead - validEvidence, color: palette.danger });
    }

    let pieChart = null;
    if (wasteData.length) {
      fadeIn(pieContainer);
      pieChart = echarts.init(pieContainer, null, mobileInitOpts());
      pieChart.setOption({
        animationDuration: 200,
        animationEasing: "cubicOut",
        title: {
          text: IA.escapeHtml(t("funnel_waste_title")),
          left: "center",
          top: 0,
          textStyle: { color: palette.textDim, fontSize: 11, fontWeight: 500 },
        },
        tooltip: mobileTooltip({
          confine: true,
          backgroundColor: palette.tooltipBg,
          borderWidth: 0,
          padding: [8, 12],
          textStyle: { color: palette.text, fontSize: 12 },
          formatter: function (params) {
            const total = wasteData.reduce(function (s, d) { return s + d.value; }, 0);
            const pct = total > 0 ? ((params.value / total) * 100).toFixed(1) : "0";
            return '<div style="font-weight:600;">' + IA.escapeHtml(params.name) + '</div>' +
              '<div>' + IA.escapeHtml(t("funnel_count")) + ': <b>' + params.value + '</b> (' + pct + '%)</div>';
          },
        }),
        toolbox: toolbox(palette),
        series: [{
          type: "pie",
          radius: ["30%", "55%"],
          center: ["50%", "60%"],
          data: wasteData.map(function (d) {
            return { name: d.name, value: d.value, itemStyle: { color: d.color } };
          }),
          label: {
            color: palette.textDim,
            fontSize: 10,
            formatter: "{b}: {c}",
          },
          labelLine: { length: 8, length2: 8 },
          itemStyle: { borderWidth: 2, borderColor: palette.tooltipBg },
        }],
      });
    } else {
      pieContainer.innerHTML = '<div style="text-align:center;color:' + palette.textDim + ';font-size:11px;padding:48px 0;">' + IA.escapeHtml(t("funnel_no_waste")) + '</div>';
    }

    // 返回复合实例：销毁时同时销毁漏斗和饼图
    return composite([chart, pieChart].filter(Boolean));
  }

  // ── 导出 ────────────────────────────────────────────────
  IA.Charts = {
    renderMatrix: renderMatrix,
    renderSankey: renderSankey,
    renderFunnel: renderFunnel,
    isAvailable: isAvailable,
    getPalette: getPalette,
  };
})();
