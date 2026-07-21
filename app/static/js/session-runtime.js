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
    const steps = meaningful
      .slice(-10)
      .map(function (event) {
        let label = event.message || event.type;
        if (event.type === "phase" && event.data) label = event.data.label || event.data.phase;
        if (event.type === "tool_call" && event.data) label = `${t("tool_call_label")}: ${event.data.name}`;
        if (event.type === "review" && event.data) label = t("review_progress", { status: event.data.status });
        return `<div class="timeline-step"><span>${IA.escapeHtml(label)}</span></div>`;
      })
      .join("");
    const metricHtml = metricItems
      .map(function (item) {
        return `<span class="timeline-metric">${IA.escapeHtml(item)}</span>`;
      })
      .join("");
    card.innerHTML =
      `<div class="timeline-header"><span class="timeline-title">${IA.escapeHtml(t("investigation_trail"))}</span>` +
      `<div class="timeline-metrics">${metricHtml}</div></div><div class="timeline-steps">${steps}</div>`;
    container.appendChild(card);
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
