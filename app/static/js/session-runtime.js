(function () {
  "use strict";

  window.addEventTimeline = function addEventTimeline(events, metrics) {
    const meaningful = events.filter(function (event) {
      return ["phase", "tool_call", "review", "interrupted", "cancelled"].includes(event.type);
    });
    if (!meaningful.length) return null;

    const container = document.getElementById("messages");
    const card = document.createElement("section");
    card.className = "msg assistant investigation-timeline";
    const metricItems = [];
    if (metrics && metrics.duration_ms !== undefined) metricItems.push(formatDuration(metrics.duration_ms));
    if (metrics && metrics.model_calls !== undefined) metricItems.push(countLabel(metrics.model_calls, "model call"));
    if (metrics && metrics.tool_calls !== undefined) metricItems.push(countLabel(metrics.tool_calls, "tool call"));
    if (metrics && metrics.review_calls !== undefined) metricItems.push(countLabel(metrics.review_calls, "review"));
    if (metrics && metrics.files_read !== undefined) metricItems.push(countLabel(metrics.files_read, "file read", "files read"));
    const steps = meaningful
      .slice(-10)
      .map(function (event) {
        let label = event.message || event.type;
        if (event.type === "phase" && event.data) label = event.data.label || event.data.phase;
        if (event.type === "tool_call" && event.data) label = `Tool: ${event.data.name}`;
        if (event.type === "review" && event.data) label = `Independent review: ${event.data.status}`;
        return `<div class="timeline-step"><span>${escapeHtml(label)}</span></div>`;
      })
      .join("");
    const metricHtml = metricItems
      .map(function (item) {
        return `<span class="timeline-metric">${escapeHtml(item)}</span>`;
      })
      .join("");
    card.innerHTML =
      `<div class="timeline-header"><span class="timeline-title">Investigation trail</span>` +
      `<div class="timeline-metrics">${metricHtml}</div></div><div class="timeline-steps">${steps}</div>`;
    container.appendChild(card);
    return card;
  };

  window.formatDuration = function formatDuration(milliseconds) {
    if (milliseconds < 1000) return `${milliseconds} ms`;
    const seconds = Math.round(milliseconds / 1000);
    return seconds < 60 ? `${seconds} s` : `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
  };

  function countLabel(value, singular, plural) {
    return `${value} ${value === 1 ? singular : plural || `${singular}s`}`;
  }

  window.setCancelVisible = function setCancelVisible(visible) {
    const button = document.getElementById("cancel-analysis");
    button.style.display = visible ? "inline-flex" : "none";
    button.disabled = false;
    button.textContent = "Cancel";
  };

  window.cancelAnalysis = async function cancelAnalysis() {
    if (!window.sessionId) return;
    const button = document.getElementById("cancel-analysis");
    button.disabled = true;
    button.textContent = "Cancelling…";
    document.getElementById("progress").textContent = "Cancellation requested…";
    try {
      await apiJson(`/session/${encodeURIComponent(window.sessionId)}/cancel`, { method: "POST" });
      button.style.display = "none";
      await pollCancellation(window.sessionId);
    } catch (error) {
      button.disabled = false;
      button.textContent = "Cancel";
      document.getElementById("progress").textContent = "";
      window.addMsg("error", error.message);
    }
  };

  async function pollCancellation(sessionId) {
    for (let attempt = 0; attempt < 12; attempt += 1) {
      await new Promise(function (resolve) {
        window.setTimeout(resolve, 500);
      });
      try {
        const session = await apiJson(`/session/${encodeURIComponent(sessionId)}`);
        if (session.status !== "running") {
          if (window.sessionId === sessionId) await window.restoreSession(sessionId, false);
          return;
        }
      } catch (error) {
        console.warn("Unable to refresh cancellation state", error);
        return;
      }
    }
    document.getElementById("progress").textContent =
      "Cancellation requested. The current provider operation may finish first.";
  }
})();
