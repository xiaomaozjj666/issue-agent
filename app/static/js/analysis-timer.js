/* analysis-timer.js — 分析计时器模块（IA.AnalysisTimer）
 * 调查期间在 progress 区显示阶段文本与已用时间，按阶段估算进度条
 * 独立模块：自 app.js 拆出（C1 约束），app.js 经 const 别名委托调用。
 */
(function () {
  "use strict";

  const IA = window.IssueAgent;
  const t = IA.translate;


  // 实时分析计时器：调查过程中在 progress 区域显示阶段文本和已用时间
  let analysisTimerId = null;
  let analysisStartTime = 0;
  let currentPhaseText = "";
  // 阶段 key（后端 phase 事件的原phase 标识），用于稳定匹配进度百分比——
  // 本地化后的阶段文本不含英文关键词，不能再用文本匹配推算进度
  let currentPhaseKey = "";

  function formatElapsed(seconds) {
    if (seconds < 60) return seconds + "s";
    const m = Math.floor(seconds / 60);
    const s = seconds % 60;
    return m + ":" + (s < 10 ? "0" + s : s);
  }

  // 各阶段估算百分比（基于真实调查流程：fetch→preload→explore→iterate→verify→review→report）
  const PHASE_PROGRESS = {
    fetching: 5,
    preloading: 10,
    exploring: 15,
    exploring_files: 15,
    thinking: 35,
    planning: 40,
    tool_call: 50,
    verifying: 88,
    review: 80,
    reviewing: 80,
    report: 95,
    done: 100,
  };

  function phaseProgress() {
    if (currentPhaseKey && PHASE_PROGRESS[currentPhaseKey] != null) return PHASE_PROGRESS[currentPhaseKey];
    if (!currentPhaseText) return 0;
    for (const key of Object.keys(PHASE_PROGRESS)) {
      if (currentPhaseText.toLowerCase().includes(key) || currentPhaseText === t(key)) return PHASE_PROGRESS[key];
    }
    return 0;
  }

  function startAnalysisTimer() {
    stopAnalysisTimer();
    analysisStartTime = Date.now();
    analysisTimerId = window.setInterval(function () {
      const elapsed = Math.floor((Date.now() - analysisStartTime) / 1000);
      const phase = currentPhaseText || t("fetching");
      const pct = phaseProgress();
      const progressEl = document.getElementById("progress");
      if (pct > 0) {
        progressEl.innerHTML = `<span class="progress-text">${IA.escapeHtml(phase)} · ${IA.escapeHtml(t("elapsed_time", { seconds: formatElapsed(elapsed) }))}</span>` +
          `<span class="progress-bar-track"><span class="progress-bar-fill" style="width:${pct}%"></span></span>`;
      } else {
        progressEl.textContent = phase + " · " + t("elapsed_time", { seconds: formatElapsed(elapsed) });
      }
    }, 1000);
  }

  function stopAnalysisTimer() {
    if (analysisTimerId !== null) {
      window.clearInterval(analysisTimerId);
      analysisTimerId = null;
    }
    analysisStartTime = 0;
  }

  function setAnalysisPhase(text, progressKey) {
    currentPhaseText = text || "";
    currentPhaseKey = progressKey || "";
    if (analysisTimerId !== null) {
      const elapsed = Math.floor((Date.now() - analysisStartTime) / 1000);
      document.getElementById("progress").textContent =
        currentPhaseText + " · " + t("elapsed_time", { seconds: formatElapsed(elapsed) });
    } else {
      document.getElementById("progress").textContent = currentPhaseText;
    }
  }

  // 仅重置内部相位文本，不触碰 DOM（对应旧闭包写法 currentPhaseText = ""）
  function clearPhase() {
    currentPhaseText = "";
    currentPhaseKey = "";
  }

  const ns = {
    start: startAnalysisTimer,
    stop: stopAnalysisTimer,
    setPhase: setAnalysisPhase,
    clearPhase: clearPhase,
  };

  IA.AnalysisTimer = ns;
  window.IssueAgent = IA;
})();
