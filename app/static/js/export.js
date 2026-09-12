/* export.js — 报告导出模块（IA.Export）
 * Markdown / pytest 骨架 / 自包含 HTML 导出，以及风险分级、修复优先级推断、
 * 影响文件提取等纯函数。DOM 渲染与会话状态仍归 app.js，本模块只做无状态组装。
 * 独立模块：自 app.js 拆出（C1 约束），app.js 经 const 别名委托调用。
 */
(function () {
  "use strict";

  const IA = window.IssueAgent;
  const t = IA.translate;
  const enumLabel = IA.enumLabel;

  // #8 风险严重程度分级：根据关键词匹配 high/medium/low
  // 中文关键词：高/严重/关键/致命 → high；中/一般/可能 → medium；低/小/轻微 → low
  // 英文关键词：critical/high/severe → high；medium/moderate → medium；low/minor → low
  function classifyRisk(text) {
    const s = String(text || "").toLowerCase();
    if (/(严重|致命|关键|高风险|critical|severe|fatal|crash|data loss|security|vulnerab)/i.test(s)) return "high";
    if (/(中等|一般|可能|潜在|medium|moderate|warning|caution)/i.test(s)) return "medium";
    if (/(轻微|低风险|小|low|minor|cosmetic|nitpick)/i.test(s)) return "low";
    // 默认无明确级别时归为 medium，避免被忽视
    return "medium";
  }

  // #5 修复方案优先级推断：根据关键词判断 P0（必须修）/P1（建议修）/P2（可选优化）
  // 覆盖中英文关键词；idx 仅作为兜底（首条建议倾向 P1，避免全是 P2）
  function classifyChangePriority(text, idx) {
    const s = String(text || "").toLowerCase();
    // P0：涉及安全/数据丢失/崩溃/竞态/死锁/认证/授权/注入
    if (/(必须|紧急|立即|critical|security|vulnerability|crash|data loss|race|deadlock|auth|inject|remote code|cve|严重|致命|关键)/i.test(s)) {
      return { cls: "p0", label: "P0", desc: t("change_priority_p0_desc") };
    }
    // P2：明确为可选/优化/建议/考虑
    if (/(可选|建议考虑|可选优化|nice to have|optional|polish|refactor|cleanup|nitpick|考虑|后续|未来)/i.test(s)) {
      return { cls: "p2", label: "P2", desc: t("change_priority_p2_desc") };
    }
    // P1：建议修复（默认）— 但首条建议若无明显信号也归 P1
    if (/(应该|建议|需要|应当|should|recommend|need|must|fix|update|add|remove|replace)/i.test(s) || idx === 0) {
      return { cls: "p1", label: "P1", desc: t("change_priority_p1_desc") };
    }
    return { cls: "p2", label: "P2", desc: t("change_priority_p2_desc") };
  }

  // #5 从修复方案文本中提取受影响的文件路径
  // 策略：1) 文本中显式提到的路径（含 / 或 .py/.js 等后缀）；2) 与 evidence.path 匹配的文件名
  function extractAffectedFiles(text, evidence) {
    const s = String(text || "");
    // 匹配常见代码路径：word/word.ext 或 word.ext（py/js/ts/tsx/go/rs/java/cpp/c/h/json/yaml/yml/toml）
    const pathRegex = /[\w.-]+\/[\w./-]+\.(?:py|js|ts|tsx|jsx|go|rs|java|cpp|c|h|hpp|json|yaml|yml|toml|md|sh|bat|sql|html|css|scss|vue|svelte)/g;
    const direct = s.match(pathRegex) || [];
    // 从 evidence 路径中匹配文本提到的文件名（basename）
    const evidenceMatches = (evidence || []).map(function (e) { return e.path; }).filter(function (p) {
      if (!p) return false;
      const base = p.split("/").pop();
      return base && s.indexOf(base) !== -1;
    });
    // 去重 + 限制最多 3 个，避免列表过长
    const all = Array.from(new Set([].concat(direct, evidenceMatches)));
    return all.slice(0, 3);
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

  // #7 生成 pytest 测试骨架：把自然语言测试建议包装为 pytest 函数
  function generatePytestSkeleton(tests, dateStamp) {
    const lines = [
      '"""Auto-generated pytest skeleton from issue-agent report.',
      `Generated: ${dateStamp}`,
      '"""',
      "",
      "import pytest",
      "",
    ];
    tests.forEach(function (item, idx) {
      // 函数名：提取字母数字下划线，截断到 50 字符，前置 test_ 前缀
      const rawName = String(item).replace(/[^\w\s]/g, " ").trim().split(/\s+/).slice(0, 6).join("_").toLowerCase();
      const fnName = "test_" + (rawName || "case_" + (idx + 1)).substring(0, 50);
      lines.push(`def ${fnName}():`);
      lines.push(`    """${String(item).replace(/"/g, "'")}"""`);
      lines.push("    # TODO: implement the test body according to the description above");
      if (/fail|raise|error|exception/i.test(item)) {
        lines.push("    with pytest.raises(Exception):");
        lines.push("        pass  # replace with the actual call");
      } else {
        lines.push("    assert True  # replace with the actual assertion");
      }
      lines.push("");
    });
    return lines.join("\n");
  }

  // C15: 导出 HTML 与 renderReport 共用同一套结构生成逻辑。
  // 在 IIFE 作用域内调用 classifyChangePriority / extractAffectedFiles /
  // classifyRisk / t / enumLabel / IA.escapeHtml，确保导出 HTML 与在线
  // 展示功能对齐（P0/P1/P2 优先级、影响范围、审查增强、风险分级、i18n）。
  // 内部 <script> 只负责 ECharts 图表渲染，不再重复 HTML 拼接逻辑。
  function buildExportBody(r, sessionData) {
    const parts = [];
    const evCount = (r.evidence || []).length;
    const review = r.review_audit || { status: "not_run" };
    const metrics = (sessionData && sessionData.metrics) || {};
    const esc = IA.escapeHtml;
    const escA = IA.escapeAttr;

    // 1. 结论卡
    parts.push(
      '<div class="conclusion">' +
        '<div class="conclusion-label">' + esc(t("report_conclusion_label")) + '</div>' +
        '<p class="conclusion-text">' + esc(r.summary || "") + '</p>' +
        '<div style="margin-top:8px;color:var(--muted);font-size:13px;">' + esc(t("report_confidence")) + ': <b>' + esc(enumLabel("confidence", r.confidence)) + '</b></div>' +
        '</div>',
    );

    // 2. 指标网格
    const reviewLabel = review.status !== "not_run" ? enumLabel("review_status", review.status) : "—";
    const exportMetrics = [
      metricCard(t("report_metric_evidence_count"), evCount),
      metricCard(t("report_metric_files_examined"), (r.files_examined || []).length),
      metricCard(t("report_metric_confidence"), enumLabel("confidence", r.confidence)),
      metricCard(t("report_metric_review"), reviewLabel),
      metricCard(t("report_metric_proposed_changes"), (r.proposed_changes || []).length),
      metricCard(t("report_metric_risks"), (r.risks || []).length),
    ];
    if (metrics.total_tokens) {
      exportMetrics.push(
        metricCard(t("report_metric_tokens"), Number(metrics.total_tokens).toLocaleString()),
      );
    }
    if (metrics.estimated_cost_usd !== undefined && metrics.estimated_cost_usd !== null) {
      const costText = IA.formatCostUsd(metrics.estimated_cost_usd);
      if (costText) exportMetrics.push(metricCard(t("report_metric_cost"), costText));
    }
    parts.push('<div class="metrics">' + exportMetrics.join("") + '</div>');

    // 3. 图表占位（内部 script 填充）：补丁改动分布 + 证据核对
    const hasPatchExport = !!(r.patch && String(r.patch).trim());
    const exportChartBlocks = [];
    if (hasPatchExport) {
      exportChartBlocks.push('<div class="chart"><div class="chart-title">' + esc(t("diffstat_chart_title")) + '</div><div id="chart-diffstat" class="chart-canvas"></div><div class="chart-caption">' + esc(t("diffstat_chart_caption")) + '</div></div>');
    }
    if (evCount) {
      exportChartBlocks.push('<div class="chart"><div class="chart-title">' + esc(t("verify_chart_title")) + '</div><div id="chart-verify" class="chart-canvas"></div><div class="chart-caption">' + esc(t("verify_chart_caption")) + '</div></div>');
    }
    if (exportChartBlocks.length) {
      parts.push(exportChartBlocks.join(""));
    }

    // 4. 独立审查（与 renderReport 对齐：reviewer_model + review_calls + 证据验证对比 + 根因支撑）
    if (review.status !== "not_run") {
      const reviewClass = IA.safeClass(review.status);
      let body = '<div class="section"><h2>' + esc(t("report_independent_review", { status: enumLabel("review_status", review.status) })) + '</h2>';
      body += '<div class="review-head"><span class="review-chip ' + reviewClass + '">' + esc(enumLabel("review_status", review.status)) + '</span>';
      if (review.reviewer_model) {
        body += ' <span class="review-meta-item">' + esc(t("review_reviewer_model")) + ': <code>' + esc(review.reviewer_model) + '</code></span>';
      }
      const reviewCalls = metrics.review_calls || 0;
      if (reviewCalls > 0) {
        body += ' <span class="review-meta-item">' + esc(t("review_reviewer_calls", { count: reviewCalls })) + '</span>';
      }
      body += '</div>';
      const audit = r.evidence_audit || { valid_references: 0, root_cause_supported: false };
      body += '<div class="review-audit">' +
        '<div class="review-audit-item"><span class="review-audit-label">' + esc(t("review_valid_evidence")) + '</span><span class="review-audit-value">' + audit.valid_references + ' / ' + evCount + '</span></div>' +
        '<div class="review-audit-item"><span class="review-audit-label">' + esc(t("review_root_cause_supported")) + '</span><span class="review-audit-value ' + (audit.root_cause_supported ? "audit-pass" : "audit-fail") + '">' + esc(audit.root_cause_supported ? t("review_supported_yes") : t("review_supported_no")) + '</span></div>' +
        '</div>';
      if (review.summary) body += '<p>' + esc(review.summary) + '</p>';
      if (review.findings && review.findings.length) {
        body += '<ol class="review-findings">' + review.findings.map(function (f, i) { return '<li value="' + (i + 1) + '">' + esc(f) + '</li>'; }).join("") + '</ol>';
      }
      body += '</div>';
      parts.push(body);
    }

    // 5. 根因
    parts.push('<div class="section"><h2>' + esc(t("report_root_cause")) + '</h2><p>' + esc(r.root_cause || "") + '</p></div>');

    // 6. 代码证据
    if (evCount) {
      let body = '<div class="section"><h2>' + esc(t("report_evidence")) + '</h2>';
      (r.evidence || []).forEach(function (e, i) {
        body += '<div class="evidence-item" data-evidence-idx="' + i + '"><div class="evidence-path"><span class="evidence-path-text">' + esc(e.path || "") + '</span>' + (e.lines ? '<span class="evidence-lines">' + esc(e.lines) + '</span>' : '') + '</div><div class="evidence-reason">' + esc(e.reason || "") + '</div></div>';
      });
      body += '</div>';
      parts.push(body);
    }

    // 7. 修复方案（P0/P1/P2 优先级 + 影响范围，与 renderReport 一致）
    if (r.proposed_changes && r.proposed_changes.length) {
      let body = '<div class="section"><h2>' + esc(t("report_proposed_changes")) + '</h2><ul>';
      r.proposed_changes.forEach(function (c, idx) {
        const priority = classifyChangePriority(c, idx);
        const scope = extractAffectedFiles(c, r.evidence || []);
        const scopeHtml = scope.length
          ? '<div class="change-meta"><span class="change-scope">' + esc(t("change_scope")) + ': <code>' + scope.map(esc).join('</code>, <code>') + '</code></span></div>'
          : '';
        body += '<li class="change-item change-' + priority.cls + '">' +
          '<div class="change-head">' +
          '<span class="change-priority change-priority-' + priority.cls + '" title="' + escA(priority.desc) + '">' + esc(priority.label) + '</span>' +
          '<span class="change-text">' + esc(c) + '</span>' +
          '</div>' + scopeHtml + '</li>';
      });
      body += '</ul></div>';
      parts.push(body);
    }

    // 8. 补丁（修复原 \\n 字面量 bug：应按实际换行符分割）
    if (r.patch) {
      let body = '<div class="section"><h2>' + esc(t("report_patch")) + '</h2><pre>';
      String(r.patch).split("\n").forEach(function (line) {
        let cls = "diff-ctx";
        if (line.startsWith("+++") || line.startsWith("---")) cls = "";
        else if (line.startsWith("+")) cls = "diff-add";
        else if (line.startsWith("-")) cls = "diff-del";
        else if (line.startsWith("@@")) cls = "diff-hunk";
        body += '<span class="' + cls + '">' + esc(line) + '</span>';
      });
      body += '</pre></div>';
      parts.push(body);
    }

    // 9. 回归测试
    if (r.tests && r.tests.length) {
      let body = '<div class="section"><h2>' + esc(t("report_tests")) + '</h2><ul>';
      r.tests.forEach(function (tt) { body += '<li>' + esc(tt) + '</li>'; });
      body += '</ul></div>';
      parts.push(body);
    }

    // 10. 风险（high/medium/low 分级，与 renderReport 一致）
    if (r.risks && r.risks.length) {
      let body = '<div class="section"><h2>' + esc(t("report_risks")) + '</h2>';
      r.risks.forEach(function (rk) {
        const sev = classifyRisk(rk);
        body += '<div class="risk-item risk-' + sev + '"><span class="risk-badge risk-badge-' + sev + '">' + esc(t("risk_severity_" + sev)) + '</span><span class="risk-text">' + esc(rk) + '</span></div>';
      });
      body += '</div>';
      parts.push(body);
    }

    function metricCard(label, value) {
      return '<div class="metric"><span class="metric-label">' + esc(label) + '</span><span class="metric-value">' + esc(value) + '</span></div>';
    }

    return parts.join("");
  }

  // #9 生成自包含 HTML 报告：嵌入报告 JSON + ECharts CDN，离线打开即可渲染图表
  // C15: HTML 结构在生成阶段渲染（复用 buildExportBody），内部 <script> 只负责图表
  function generateSelfContainedHtml(r, sessionData) {
    const jsonStr = JSON.stringify(r, null, 2);
    const sessionJson = JSON.stringify(sessionData || {});
    const generatedAt = new Date().toISOString();
    const title = (sessionData && sessionData.issue_url) ? sessionData.issue_url : "Issue Agent Report";
    // 内联 CSS：与主应用 primer.css 设计令牌保持一致，支持亮/暗双主题
    const inlineCss = `
      :root{color-scheme:dark;--bg:#0d1117;--card:#161b22;--canvas-subtle:#161b22;--canvas-inset:#0d1117;--fg:#e6edf3;--fg-strong:#f0f6fc;--muted:#8b949e;--faint:#6e7681;--border:#30363d;--border-muted:#21262d;--accent:#58a6ff;--accent-emphasis:#79c0ff;--accent-subtle:rgba(56,139,253,0.15);--accent-muted:rgba(56,139,253,0.4);--green:#3fb950;--green-emphasis:#56d364;--green-subtle:rgba(46,160,67,0.15);--green-muted:rgba(46,160,67,0.4);--yellow:#d29922;--yellow-emphasis:#e3b341;--yellow-subtle:rgba(187,128,9,0.15);--yellow-muted:rgba(187,128,9,0.4);--red:#f85149;--red-emphasis:#ff7b72;--red-subtle:rgba(248,81,73,0.15);--red-muted:rgba(248,81,73,0.4);--neutral-emphasis:#8b949e;--neutral-subtle:rgba(110,118,129,0.14)}
      [data-theme="light"]{color-scheme:light;--bg:#fff;--card:#f6f8fa;--canvas-subtle:#f6f8fa;--canvas-inset:#fff;--fg:#1f2328;--fg-strong:#1f2328;--muted:#656d76;--faint:#8c959f;--border:#d0d7de;--border-muted:#d8dee4;--accent:#0969da;--accent-emphasis:#0550ae;--accent-subtle:#ddf4ff;--accent-muted:rgba(9,105,218,0.32);--green:#1a7f37;--green-emphasis:#116329;--green-subtle:#dafbe1;--green-muted:rgba(26,127,55,0.32);--yellow:#9a6700;--yellow-emphasis:#7d4e00;--yellow-subtle:#fff8c5;--yellow-muted:rgba(154,103,0,0.32);--red:#cf222e;--red-emphasis:#a40e26;--red-subtle:#ffebe9;--red-muted:rgba(207,34,46,0.32);--neutral-emphasis:#656d76;--neutral-subtle:rgba(208,215,222,0.4)}
      *{box-sizing:border-box;margin:0;padding:0}
      body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','Noto Sans','PingFang SC','Microsoft YaHei',sans-serif;padding:24px;background:var(--bg);color:var(--fg);line-height:1.6;font-size:14px;max-width:980px;margin:0 auto}
      h1{font-size:20px;border-bottom:1px solid var(--border);padding-bottom:8px;color:var(--accent);font-weight:700;letter-spacing:-0.01em}
      h2{font-size:16px;margin:24px 0 12px;border-bottom:1px solid var(--border-muted);padding-bottom:8px;font-weight:600}
      h3{font-size:14px;margin:16px 0 8px;color:var(--fg-strong);font-weight:600}
      .meta{color:var(--muted);font-size:12px;margin-bottom:16px}
      .theme-toggle{position:fixed;top:16px;right:16px;padding:6px 12px;border:1px solid var(--border);border-radius:8px;background:var(--card);color:var(--fg);font-size:12px;cursor:pointer}
      .conclusion{padding:16px 20px;background:linear-gradient(135deg,var(--accent-subtle) 0%,var(--card) 100%);border:1px solid var(--accent-muted);border-radius:12px;margin-bottom:16px}
      .conclusion-label{color:var(--accent);font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:0.06em}
      .conclusion-text{color:var(--fg-strong);font-size:14px;font-weight:600;margin:4px 0 0 0;line-height:1.6}
      .metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:8px;margin-bottom:16px}
      .metric{padding:12px;border:1px solid var(--border);border-radius:8px;background:var(--card)}
      .metric-label{color:var(--muted);font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:0.04em}
      .metric-value{color:var(--fg-strong);font-size:16px;font-weight:700;display:block;margin-top:2px;overflow-wrap:anywhere}
      .chart{margin:12px 0;padding:12px;border:1px solid var(--border);border-radius:8px;background:var(--card)}
      .chart-title{font-size:13px;font-weight:600;margin-bottom:8px;color:var(--fg-strong)}
      .chart-canvas{width:100%;height:260px}
      .chart-caption{color:var(--muted);font-size:12px;margin-top:4px;line-height:1.5}
      .section{margin-top:20px;padding-top:12px;border-top:1px solid var(--border-muted)}
      .section h2{margin-top:0}
      .evidence-item{border:1px solid var(--border);border-radius:8px;padding:12px;background:var(--card);margin-bottom:8px}
      .evidence-path{color:var(--accent);font-family:ui-monospace,SFMono-Regular,monospace;font-size:12px;margin-bottom:4px;display:flex;align-items:center;flex-wrap:wrap;gap:8px}
      .evidence-path-text{overflow-wrap:anywhere;min-width:0}
      .evidence-lines{flex:0 0 auto;padding:1px 8px;border-radius:9999px;background:var(--accent-subtle);color:var(--accent);font-size:11px;font-weight:600;white-space:nowrap}
      .evidence-reason{color:var(--fg);font-size:14px;line-height:1.6}
      .risk-item{padding:10px 14px;border:1px solid var(--border);border-left-width:3px;border-radius:8px;background:var(--card);margin-bottom:6px;display:flex;align-items:flex-start;gap:8px}
      .risk-high{border-left-color:var(--red);background:linear-gradient(90deg,var(--red-subtle) 0%,var(--card) 28%)}
      .risk-medium{border-left-color:var(--yellow);background:linear-gradient(90deg,var(--yellow-subtle) 0%,var(--card) 28%)}
      .risk-low{border-left-color:var(--green);background:linear-gradient(90deg,var(--green-subtle) 0%,var(--card) 28%)}
      .risk-badge{display:inline-flex;align-items:center;padding:2px 8px;border:1px solid transparent;border-radius:9999px;font-size:12px;font-weight:700;white-space:nowrap}
      .risk-badge-high{color:var(--red-emphasis);background:var(--red-subtle);border-color:var(--red-muted)}.risk-badge-medium{color:var(--yellow-emphasis);background:var(--yellow-subtle);border-color:var(--yellow-muted)}.risk-badge-low{color:var(--green-emphasis);background:var(--green-subtle);border-color:var(--green-muted)}
      .risk-text{flex:1 1 auto;overflow-wrap:anywhere}
      .change-item{padding:10px 14px;border:1px solid var(--border);border-left-width:3px;border-radius:8px;background:var(--card);margin-bottom:6px}
      .change-p0{border-left-color:var(--red);background:linear-gradient(90deg,var(--red-subtle) 0%,var(--card) 28%)}
      .change-p1{border-left-color:var(--accent);background:linear-gradient(90deg,var(--accent-subtle) 0%,var(--card) 28%)}
      .change-p2{border-left-color:var(--neutral-emphasis);background:linear-gradient(90deg,var(--neutral-subtle) 0%,var(--card) 28%)}
      .change-head{display:flex;align-items:flex-start;gap:8px}
      .change-priority{display:inline-flex;align-items:center;justify-content:center;flex:0 0 auto;min-width:28px;padding:2px 6px;border:1px solid transparent;border-radius:4px;font-size:12px;font-weight:700;white-space:nowrap}
      .change-priority-p0{color:var(--red-emphasis);background:var(--red-subtle);border-color:var(--red-muted)}.change-priority-p1{color:var(--accent-emphasis);background:var(--accent-subtle);border-color:var(--accent-muted)}.change-priority-p2{color:var(--muted);background:var(--neutral-subtle);border-color:var(--border)}
      .change-text{flex:1 1 auto;font-size:14px;line-height:1.6;overflow-wrap:anywhere}
      .change-meta{margin-top:4px;padding-left:36px;color:var(--muted);font-size:12px}
      .change-scope code{background:var(--canvas-subtle);padding:2px 6px;border-radius:4px;font-family:ui-monospace,monospace;font-size:11px;color:var(--accent)}
      pre{background:var(--canvas-inset);padding:12px;border-radius:8px;overflow-x:auto;font-family:ui-monospace,SFMono-Regular,monospace;font-size:12px;border:1px solid var(--border-muted);line-height:1.7;white-space:pre}
      .diff-add{background:var(--green-subtle);display:block;padding-left:8px;border-left:3px solid var(--green)}
      .diff-del{background:var(--red-subtle);display:block;padding-left:8px;border-left:3px solid var(--red)}
      .diff-ctx{display:block;padding-left:8px;color:var(--muted)}
      .diff-hunk{background:var(--accent-subtle);display:block;padding:4px 8px;color:var(--accent);font-weight:600}
      ul,ol{line-height:1.6;padding-left:20px}
      li{margin-bottom:4px}
      .review-chip{display:inline-flex;align-items:center;padding:3px 10px;border:1px solid transparent;border-radius:9999px;font-size:12px;font-weight:600}
      .review-chip.approved{color:var(--green-emphasis);background:var(--green-subtle);border-color:var(--green-muted)}.review-chip.revised{color:var(--accent-emphasis);background:var(--accent-subtle);border-color:var(--accent-muted)}.review-chip.unavailable{color:var(--muted);background:var(--neutral-subtle);border-color:var(--border)}
      .review-head{display:flex;align-items:center;flex-wrap:wrap;gap:8px 12px;margin-bottom:12px}
      .review-head>.review-chip{flex-basis:100%}
      .review-meta-item{color:var(--muted);font-size:12px}
      .review-meta-item code{background:var(--canvas-subtle);padding:2px 6px;border-radius:4px;font-family:ui-monospace,monospace;font-size:11px;color:var(--accent)}
      .review-audit{display:grid;grid-template-columns:repeat(2,1fr);gap:12px;margin:12px 0;padding:12px;border:1px solid var(--border-muted);border-radius:8px;background:var(--canvas-subtle)}
      .review-audit-item{display:flex;flex-direction:column;gap:2px}
      .review-audit-label{color:var(--muted);font-size:12px;font-weight:600}
      .review-audit-value{font-weight:700;font-size:14px}
      .audit-pass{color:var(--green)}.audit-fail{color:var(--red)}
      .review-findings{padding-left:20px;line-height:1.6}
      .review-findings li{margin-bottom:4px}
      @media print{body{background:#fff;color:#000;max-width:none}.chart{page-break-inside:avoid}}
    `;
    return `<!DOCTYPE html>
<html lang="zh-CN" data-theme="dark">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>${IA.escapeHtml(title)} — Issue Agent Report</title>
<style>${inlineCss}</style>
</head>
<body>
<button class="theme-toggle" type="button" onclick="var d=document.documentElement;d.setAttribute('data-theme',d.getAttribute('data-theme')==='dark'?'light':'dark');this.textContent=d.getAttribute('data-theme')==='dark'?'Light':'Dark';document.dispatchEvent(new CustomEvent('issue-agent:report-theme'))">Light</button>
<h1>${IA.escapeHtml(title)}</h1>
<div class="meta">Generated by Issue Agent · ${IA.escapeHtml(generatedAt)}</div>
<div id="report-root">${buildExportBody(r, sessionData)}</div>

<!-- ECharts CDN with SRI integrity -->
<script src="https://cdn.jsdelivr.net/npm/echarts@5.5.0/dist/echarts.min.js"
        crossorigin="anonymous" referrerpolicy="no-referrer"
        integrity="sha384-o5uz97et3bErHvpKfD4Jz4n0JfhJDWABFuF4NP+iEEDxE1VwMWJ19QGR0lqFZnr6"
        onerror="this.remove();window.__echartsFailed=true;"></script>

<script id="report-data" type="application/json">${IA.escapeHtml(jsonStr)}</script>
<script id="session-data" type="application/json">${IA.escapeHtml(sessionJson)}</script>

<script>
(function(){
  "use strict";
  var report;
  var session;
  try {
    report = JSON.parse(document.getElementById('report-data').textContent);
    session = JSON.parse(document.getElementById('session-data').textContent);
  } catch(e){ console.error('Failed to parse embedded data', e); return; }

  // C15: HTML 结构已在生成阶段渲染（buildExportBody），这里只负责图表
  // 图表逻辑与 charts.js 保持一致：补丁改动分布 + 证据核对
  if (typeof echarts === 'undefined' || window.__echartsFailed) return;
  function renderExportCharts() {
    var evCount = (report.evidence || []).length;
    var filesRead = (session.files_read && session.files_read.length) ? session.files_read : (report.files_examined || []);
    var light = document.documentElement.getAttribute('data-theme') === 'light';
    // 与主应用一致的 GitHub 语义色：状态可辨识，但不使用高饱和品牌色块。
    var C = light
      ? { blue:'#0969da', green:'#1a7f37', red:'#cf222e', orange:'#9a6700', gray:'#6e7781', text:'#1f2328', textDim:'#656d76', line:'#d0d7de', bg:'#ffffff' }
      : { blue:'#58a6ff', green:'#3fb950', red:'#f85149', orange:'#d29922', gray:'#8b949e', text:'#e6edf3', textDim:'#8b949e', line:'#30363d', bg:'#161b22' };

  function normP(p) {
    var s = String(p || '').split('\\\\').join('/');
    if (s.indexOf('./') === 0) s = s.slice(2);
    if (s.indexOf('a/') === 0 || s.indexOf('b/') === 0) s = s.slice(2);
    return s.toLowerCase();
  }
  function shortN(p) { var parts = String(p).split('/'); return parts.length > 2 ? '…/' + parts.slice(-2).join('/') : String(p); }
  function sameFile(a, b) { return a === b || a.slice(-(b.length + 1)) === '/' + b || b.slice(-(a.length + 1)) === '/' + a; }

  // unified diff → 每文件增删行数（与 charts.js parseDiffstat 同口径）
  function parseDiff(patch) {
    var files = []; var current = null; var oldPath = null;
    String(patch || '').split('\\n').forEach(function(line){
      if (line.indexOf('--- ') === 0) {
        var p0 = line.slice(4).trim();
        oldPath = p0 === '/dev/null' ? null : (p0.indexOf('a/') === 0 ? p0.slice(2) : p0);
        return;
      }
      if (line.indexOf('+++ ') === 0) {
        var p1 = line.slice(4).trim();
        var filePath = p1 === '/dev/null' ? oldPath : (p1.indexOf('b/') === 0 ? p1.slice(2) : p1);
        current = { path: filePath || 'unknown', added: 0, removed: 0 };
        files.push(current);
        return;
      }
      if (!current) return;
      if (line.charAt(0) === '+') current.added += 1;
      else if (line.charAt(0) === '-') current.removed += 1;
    });
    return files.filter(function(f){ return f.added > 0 || f.removed > 0; });
  }

  var diffFiles = parseDiff(report.patch);

  // ── 区块1：补丁改动分布 ──
  var diffEl = document.getElementById('chart-diffstat');
  if (diffEl && diffFiles.length) {
    var previousDiff = echarts.getInstanceByDom(diffEl);
    if (previousDiff) previousDiff.dispose();
    var dsRows = diffFiles.slice().sort(function(a, b){ return (a.added + a.removed) - (b.added + b.removed); });
    var MAX_ROWS = 12;
    if (dsRows.length > MAX_ROWS) {
      var rest = dsRows.slice(0, dsRows.length - (MAX_ROWS - 1));
      var agg = { path: '+' + rest.length + ' more files', added: 0, removed: 0 };
      rest.forEach(function(f){ agg.added += f.added; agg.removed += f.removed; });
      dsRows = [agg].concat(dsRows.slice(dsRows.length - (MAX_ROWS - 1)));
    }
    var totalAdd = 0; var totalDel = 0;
    diffFiles.forEach(function(f){ totalAdd += f.added; totalDel += f.removed; });
    echarts.init(diffEl).setOption({
      animationDuration: 200,
      tooltip: { confine: true, backgroundColor: C.bg, borderWidth: 0, padding: [10, 14], textStyle: { color: C.text, fontSize: 12 },
        formatter: function(p){
          var row = dsRows[p.dataIndex];
          if (!row) return '';
          return '<div style="font-weight:600">' + row.path + '</div>' +
            '<div><span style="color:' + C.green + '">+' + row.added + '</span> · <span style="color:' + C.red + '">\u2212' + row.removed + '</span></div>';
        }
      },
      legend: { top: 0, left: 0, itemWidth: 10, itemHeight: 10, textStyle: { color: C.textDim, fontSize: 11 }, data: ['Added', 'Removed'] },
      title: { text: diffFiles.length + ' files · +' + totalAdd + ' \u2212' + totalDel, left: 'center', bottom: 0, textStyle: { color: C.textDim, fontSize: 11 } },
      grid: { left: 8, right: 72, top: 28, bottom: 28, containLabel: true },
      xAxis: { type: 'value', minInterval: 1, axisLabel: { color: C.textDim, fontSize: 10 }, splitLine: { lineStyle: { color: C.line, opacity: 0.4 } } },
      yAxis: { type: 'category', data: dsRows.map(function(f){ return shortN(f.path); }), axisLabel: { color: C.textDim, fontSize: 10, width: 140, overflow: 'truncate' } },
      series: [
        { name: 'Added', type: 'bar', stack: 'diff', barMaxWidth: 14, data: dsRows.map(function(f){ return f.added; }), itemStyle: { color: C.green } },
        { name: 'Removed', type: 'bar', stack: 'diff', barMaxWidth: 14, data: dsRows.map(function(f){ return f.removed; }), itemStyle: { color: C.red, borderRadius: [0, 3, 3, 0] },
          label: { show: true, position: 'right', fontSize: 10, color: C.textDim,
            formatter: function(p){ var f = dsRows[p.dataIndex]; return f ? '+' + f.added + ' \u2212' + f.removed : ''; } } }
      ]
    });
  }

  // ── 区块2：证据核对 ──
  var verifyEl = document.getElementById('chart-verify');
  if (verifyEl && evCount) {
    var previousVerify = echarts.getInstanceByDom(verifyEl);
    if (previousVerify) previousVerify.dispose();
    var readList = []; var readSeen = {};
    filesRead.forEach(function(p){ var n = normP(p); if (n && !readSeen[n]) { readSeen[n] = true; readList.push(n); } });
    var hasReadData = readList.length > 0;
    var patchPaths = diffFiles.map(function(f){ return normP(f.path); });
    var byFile = {}; var order = [];
    (report.evidence || []).forEach(function(e){
      var n = normP(e.path || 'unknown');
      if (!byFile[n]) { byFile[n] = { path: e.path || 'unknown', count: 0 }; order.push(n); }
      byFile[n].count += 1;
    });
    var vRows = order.map(function(n){
      var it = byFile[n];
      var read = !hasReadData || readList.some(function(r0){ return sameFile(r0, n); });
      var patched = patchPaths.some(function(p0){ return sameFile(p0, n); });
      return { name: shortN(it.path), path: it.path, count: it.count, read: read, patched: patched };
    });
    vRows.sort(function(a, b){ return a.count - b.count; });
    var readCnt = vRows.filter(function(r0){ return r0.read; }).length;
    var patchedCnt = vRows.filter(function(r0){ return r0.patched; }).length;
    echarts.init(verifyEl).setOption({
      animationDuration: 200,
      tooltip: { confine: true, backgroundColor: C.bg, borderWidth: 0, padding: [10, 14], textStyle: { color: C.text, fontSize: 12 },
        formatter: function(p){
          var row = vRows[p.dataIndex];
          if (!row) return '';
          return '<div style="font-weight:600">' + row.path + '</div>' +
            '<div style="color:' + C.textDim + ';font-size:11px">Citations: <b style="color:' + C.text + '">' + row.count + '</b></div>' +
            '<div style="color:' + (row.read ? C.green : C.red) + ';font-size:11px;font-weight:600">' + (row.read ? 'Read' : 'Not read') + '</div>' +
            '<div style="color:' + C.textDim + ';font-size:11px">Patch: ' + (row.patched ? 'patched' : 'not patched') + '</div>';
        }
      },
      title: { text: readCnt + '/' + vRows.length + ' read · ' + patchedCnt + '/' + vRows.length + ' patched', left: 'center', bottom: 0, textStyle: { color: C.textDim, fontSize: 11 } },
      grid: { left: 8, right: 88, top: 16, bottom: 28, containLabel: true },
      xAxis: { type: 'value', minInterval: 1, axisLabel: { color: C.textDim, fontSize: 10 }, splitLine: { lineStyle: { color: C.line, opacity: 0.4 } } },
      yAxis: { type: 'category', data: vRows.map(function(r0){ return r0.name; }), axisLabel: { color: C.textDim, fontSize: 10, width: 140, overflow: 'truncate' } },
      series: [{ type: 'bar', barMaxWidth: 14,
        data: vRows.map(function(r0){ return { value: r0.count, itemStyle: { color: r0.read ? C.blue : C.red, borderRadius: [0, 3, 3, 0] } }; }),
        label: { show: true, position: 'right', fontSize: 10, color: C.textDim,
          formatter: function(p){ var r0 = vRows[p.dataIndex]; return r0 ? String(r0.count) + (r0.patched ? ' · patched' : '') : ''; } } }]
    });
  }

  }
  document.addEventListener('issue-agent:report-theme', renderExportCharts);
  renderExportCharts();

  // 窗口 resize 同步（120ms 防抖，与主应用一致）
  var resizeTimer = null;
  window.addEventListener('resize', function(){
    if (resizeTimer) clearTimeout(resizeTimer);
    resizeTimer = setTimeout(function(){
      resizeTimer = null;
      ['chart-diffstat','chart-verify'].forEach(function(id){
        var el = document.getElementById(id);
        if (el) { var inst = echarts.getInstanceByDom(el); if (inst) inst.resize(); }
      });
    }, 120);
  });

})();
</script>
</body>
</html>`;
  }

  const ns = {
    reportAsMarkdown: reportAsMarkdown,
    pytestSkeleton: generatePytestSkeleton,
    buildBody: buildExportBody,
    selfContainedHtml: generateSelfContainedHtml,
    classifyRisk: classifyRisk,
    classifyChangePriority: classifyChangePriority,
    extractAffectedFiles: extractAffectedFiles,
  };

  IA.Export = ns;
  window.IssueAgent = IA;
})();
