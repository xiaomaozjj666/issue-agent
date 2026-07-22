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

    // 默认折叠：只显示最后 10 条，超出部分隐藏，点击"展开全部"显示完整轨迹
    const VISIBLE_LIMIT = 10;
    const total = meaningful.length;
    const buildStepHtml = function (event) {
      let label = event.message || event.type;
      if (event.type === "phase" && event.data) label = event.data.label || event.data.phase;
      if (event.type === "tool_call" && event.data) label = `${t("tool_call_label")}: ${event.data.name}`;
      if (event.type === "review" && event.data) label = t("review_progress", { status: event.data.status });
      return `<div class="timeline-step"><span>${IA.escapeHtml(label)}</span></div>`;
    };
    const allStepsHtml = meaningful.map(buildStepHtml).join("");
    const metricHtml = metricItems
      .map(function (item) {
        return `<span class="timeline-metric">${IA.escapeHtml(item)}</span>`;
      })
      .join("");
    const hasMore = total > VISIBLE_LIMIT;
    const collapsedCount = hasMore ? total - VISIBLE_LIMIT : 0;
    card.innerHTML =
      `<div class="timeline-header"><span class="timeline-title">${IA.escapeHtml(t("investigation_trail"))}</span>` +
      `<div class="timeline-metrics">${metricHtml}</div></div>` +
      `<div class="timeline-steps" data-collapsed="${hasMore ? "1" : "0"}">${allStepsHtml}</div>` +
      (hasMore ? `<button type="button" class="timeline-expand-btn" aria-expanded="false">${IA.escapeHtml(t("timeline_expand"))} (${collapsedCount})</button>` : "");
    container.appendChild(card);
    // 展开/折叠交互
    if (hasMore) {
      const stepsEl = card.querySelector(".timeline-steps");
      const btn = card.querySelector(".timeline-expand-btn");
      if (btn && stepsEl) {
        // 初始折叠：隐藏前 collapsedCount 个 step
        const stepEls = stepsEl.querySelectorAll(".timeline-step");
        for (let i = 0; i < collapsedCount; i++) {
          stepEls[i].style.display = "none";
        }
        btn.addEventListener("click", function () {
          const expanded = btn.getAttribute("aria-expanded") === "true";
          if (expanded) {
            // 折叠
            for (let i = 0; i < collapsedCount; i++) stepEls[i].style.display = "none";
            btn.setAttribute("aria-expanded", "false");
            btn.textContent = `${t("timeline_expand")} (${collapsedCount})`;
            stepsEl.dataset.collapsed = "1";
          } else {
            // 展开
            stepEls.forEach(function (el) { el.style.display = ""; });
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
    for (let attempt = 0; attempt < 12; attempt += 1) {
      await new Promise(function (resolve) {
        window.setTimeout(resolve, 500);
      });
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

  // 向后兼容：保留 window.cancelAnalysis
  window.cancelAnalysis = cancelAnalysis;
  window.setCancelVisible = setCancelVisible;
  window.addEventTimeline = addEventTimeline;
  window.formatDuration = IA.formatDuration;

  // 暴露运行时辅助接口给主应用
  IA.Runtime = {
    addEventTimeline,
    setCancelVisible,
    cancelAnalysis,
    setOnCancellationComplete,
  };
})();
