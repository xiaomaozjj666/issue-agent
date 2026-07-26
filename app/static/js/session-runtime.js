(function () {
  "use strict";

  const IA = window.IssueAgent;
  const t = IA.translate;

  function countLabel(value, key) {
    return t(key, { count: value });
  }

  function addEventTimeline(events, metrics) {
    const meaningful = (events || []).filter(function (event) {
      return ["phase", "tool_call", "review", "interrupted", "cancelled"].includes(event.type);
    });
    if (!meaningful.length) return null;

    const container = document.getElementById("messages");
    const card = document.createElement("section");
    card.className = "msg assistant investigation-timeline";
    const metricItems = [];
    if (metrics && metrics.duration_ms !== undefined) metricItems.push(IA.formatDuration(metrics.duration_ms));
    if (metrics && metrics.model_calls !== undefined) metricItems.push(countLabel(metrics.model_calls, "timeline_model_calls"));
    if (metrics && metrics.tool_calls !== undefined) metricItems.push(countLabel(metrics.tool_calls, "timeline_tool_calls"));
    if (metrics && metrics.review_calls !== undefined) metricItems.push(countLabel(metrics.review_calls, "timeline_reviews"));
    if (metrics && metrics.files_read !== undefined) metricItems.push(countLabel(metrics.files_read, "timeline_files_read"));

    // 默认展示聚合摘要：同名工具调用合并计数，长轨迹保留开头与结尾各 4 条。
    // 用户仍可展开查看完整时序，兼顾扫描效率和审计完整性。
    const VISIBLE_LIMIT = 8;
    const total = meaningful.length;
    const buildStepHtml = function (event, extra) {
      let label = event.message || event.type;
      if (event.type === "phase" && event.data) {
        // 历史事件的 data.label 存的是英文原文，优先用 phase 枚举的 i18n 文案，
        // 翻译缺失（enumLabel 回退为原值）时再降级到原始 label
        const phaseLabel = event.data.phase ? IA.enumLabel("phase", event.data.phase) : "";
        if (phaseLabel && phaseLabel !== String(event.data.phase)) label = phaseLabel;
        else label = event.data.label || event.data.phase;
      }
      if (event.type === "tool_call" && event.data) label = `${t("tool_call_label")}: ${event.data.name}`;
      if (event.type === "review" && event.data) label = t("review_progress", { status: IA.enumLabel("review_status", event.data.status) });
      if (event._count > 1) label += ` × ${event._count}`;
      return `<div class="timeline-step${extra ? " timeline-step-extra" : ""}"><span>${IA.escapeHtml(label)}</span></div>`;
    };
    const metricHtml = metricItems
      .map(function (item) {
        return `<span class="timeline-metric">${IA.escapeHtml(item)}</span>`;
      })
      .join("");
    const toolIndexes = Object.create(null);
    let summaryEvents = [];
    meaningful.forEach(function (event) {
      if (event.type !== "tool_call" || !event.data) {
        summaryEvents.push(event);
        return;
      }
      const toolName = String(event.data.name || "");
      if (toolIndexes[toolName] !== undefined) {
        summaryEvents[toolIndexes[toolName]]._count += 1;
        return;
      }
      toolIndexes[toolName] = summaryEvents.length;
      summaryEvents.push(Object.assign({}, event, { _count: 1 }));
    });
    if (summaryEvents.length > VISIBLE_LIMIT) {
      summaryEvents = summaryEvents.slice(0, 4).concat(summaryEvents.slice(-4));
    }
    const hasMore = total > summaryEvents.length;
    const collapsedCount = hasMore ? total - summaryEvents.length : 0;
    const visibleStepsHtml = summaryEvents.map(function (event) { return buildStepHtml(event, false); }).join("");
    const allStepsHtml = hasMore
      ? meaningful.map(function (event) { return buildStepHtml(event, false); }).join("")
      : "";
    card.innerHTML =
      `<div class="timeline-header"><span class="timeline-title">${IA.escapeHtml(t("investigation_trail"))}</span>` +
      `<div class="timeline-metrics">${metricHtml}</div></div>` +
      `<div class="timeline-steps" data-collapsed="${hasMore ? "1" : "0"}">${visibleStepsHtml}</div>` +
      (hasMore ? `<button type="button" class="timeline-expand-btn" aria-expanded="false">${IA.escapeHtml(t("timeline_expand"))} (${collapsedCount})</button>` : "");
    container.appendChild(card);
    // 展开时再创建历史项，收起时移除，保持默认视图轻量且布局稳定。
    if (hasMore) {
      const stepsEl = card.querySelector(".timeline-steps");
      const btn = card.querySelector(".timeline-expand-btn");
      if (btn && stepsEl) {
        btn.addEventListener("click", function () {
          const expanded = btn.getAttribute("aria-expanded") === "true";
          if (expanded) {
            stepsEl.innerHTML = visibleStepsHtml;
            btn.setAttribute("aria-expanded", "false");
            btn.textContent = `${t("timeline_expand")} (${collapsedCount})`;
            stepsEl.dataset.collapsed = "1";
          } else {
            stepsEl.innerHTML = allStepsHtml;
            btn.setAttribute("aria-expanded", "true");
            btn.textContent = t("timeline_collapse");
            stepsEl.dataset.collapsed = "0";
          }
        });
      }
    }
    return card;
  }

  function setCancelVisible(visible) {
    const button = document.getElementById("cancel-analysis");
    if (!button) return;
    button.style.display = visible ? "inline-flex" : "none";
    button.disabled = false;
    button.textContent = t("cancel_button");
  }

  let onCancellationComplete = null;

  function setOnCancellationComplete(handler) {
    onCancellationComplete = handler;
  }

  async function cancelAnalysis() {
    const sessionId = window.IssueAgent.sessionId;
    if (!sessionId) return;
    const button = document.getElementById("cancel-analysis");
    button.disabled = true;
    button.textContent = t("cancelling");
    document.getElementById("progress").textContent = t("cancelling");
    try {
      await IA.apiJson(`/session/${encodeURIComponent(sessionId)}/cancel`, { method: "POST" });
      button.style.display = "none";
      await pollCancellation(sessionId);
    } catch (error) {
      button.disabled = false;
      button.textContent = t("cancel_button");
      document.getElementById("progress").textContent = "";
      if (window.IssueAgent.addMsg) window.IssueAgent.addMsg("error", error.message);
    }
  }

  async function pollCancellation(sessionId) {
    let consecutiveErrors = 0;
    // 轮询 60 次 × 500ms = 30s，覆盖 search_code 等耗时工具调用的取消等待
    for (let attempt = 0; attempt < 60; attempt += 1) {
      await new Promise(function (resolve) {
        window.setTimeout(resolve, 500);
      });
      // L7：会话已切换或用户已发起新分析时，停止对旧会话的轮询，
      // 避免旧轮询的 restoreSession 覆盖新视图
      if (window.IssueAgent.sessionId !== sessionId) {
        return;
      }
      try {
        const session = await IA.apiJson(`/session/${encodeURIComponent(sessionId)}`);
        consecutiveErrors = 0;
        if (session.status !== "running") {
          if (window.IssueAgent.sessionId === sessionId && window.IssueAgent.restoreSession) {
            await window.IssueAgent.restoreSession(sessionId, false);
          }
          if (onCancellationComplete) onCancellationComplete(session);
          return;
        }
      } catch (error) {
        consecutiveErrors += 1;
        console.warn("Unable to refresh cancellation state", error);
        // 容忍单次抖动；连续 3 次失败说明网络/后端确有问题，必须给用户恢复路径
        if (consecutiveErrors >= 3) {
          const button = document.getElementById("cancel-analysis");
          if (button) {
            button.style.display = "inline-flex";
            button.disabled = false;
            button.textContent = t("cancel_button");
          }
          document.getElementById("progress").textContent = "";
          if (window.IssueAgent.addMsg) {
            window.IssueAgent.addMsg("error", t("cancel_failed_retry"));
          }
          return;
        }
      }
    }
    document.getElementById("progress").textContent = t("cancellation_requested");
  }

  // 暴露运行时辅助接口给主应用（统一通过 IA.Runtime 命名空间访问，
  // 不再向 window 暴露裸全局，避免命名污染）
  IA.Runtime = {
    addEventTimeline,
    setCancelVisible,
    cancelAnalysis,
    setOnCancellationComplete,
  };
})();
