/**
 * motion.js — ReactBits 动效组件的原生 JS 复刻模块
 *
 * 参考 ReactBits（https://www.reactbits.dev）的动画交互设计理念，
 * 用纯原生 JS + CSS 在无构建链架构内实现等效交互增强。
 *
 * 6 个动效各自服务一个交互目标，拒绝纯装饰：
 * 1. Counter         — 指标数字滚动，让用户感知数据量级
 * 2. AnimatedList    — 列表逐项入场，引导阅读节奏
 * 3. TiltCard        — 卡片悬停 3D 视差，增加交互层次感
 * 4. Ripple          — 按钮点击涟漪，确认操作已接收
 * 5. SmoothExpand    — 折叠区块平滑展开，避免突变
 * 6. ThemeTransition — 主题切换颜色平滑过渡，消除闪烁
 *
 * 全局规范：尊重 prefers-reduced-motion；200ms 缓动为主；不阻塞主线程。
 */
(function () {
  "use strict";

  const IA = window.IssueAgent;

  // ── 无障碍：尊重用户系统级减少动效偏好 ──────────────────
  function prefersReducedMotion() {
    return window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  }

  // ── 1. Counter：数字滚动动画 ────────────────────────────
  // ReactBits Components/Counter 复刻
  // 从 0 滚动到目标值，easeOutCubic 缓动让结尾减速，感知更自然
  function animateCounter(el, target, duration) {
    if (!el || prefersReducedMotion()) return;
    const isInteger = Number.isInteger(target);
    const start = 0;
    const startTime = performance.now();
    const dur = duration || 800;

    function easeOutCubic(t) {
      return 1 - Math.pow(1 - t, 3);
    }

    function tick(now) {
      const elapsed = now - startTime;
      const progress = Math.min(elapsed / dur, 1);
      const eased = easeOutCubic(progress);
      const current = start + (target - start) * eased;
      el.textContent = isInteger ? String(Math.round(current)) : current.toFixed(1);
      if (progress < 1) {
        requestAnimationFrame(tick);
      } else {
        el.textContent = String(target);
      }
    }
    requestAnimationFrame(tick);
  }

  // 扫描容器内的数字指标卡片，自动触发滚动
  // 仅对纯数字 value 的卡片启用，文本类指标（置信度/审查状态）保持原样
  function applyCounters(container) {
    if (!container || prefersReducedMotion()) return;
    const cards = container.querySelectorAll(".report-metric-card .report-metric-value");
    cards.forEach(function (el) {
      const raw = (el.textContent || "").trim();
      const num = parseInt(raw, 10);
      if (!isNaN(num) && num > 0 && String(num) === raw) {
        el.dataset.counterTarget = String(num);
        el.textContent = "0";
        // 用 IntersectionObserver 在卡片可见时才触发，避免视口外浪费帧
        if ("IntersectionObserver" in window) {
          const io = new IntersectionObserver(function (entries) {
            entries.forEach(function (entry) {
              if (entry.isIntersecting) {
                animateCounter(el, num, 700);
                io.disconnect();
              }
            });
          }, { threshold: 0.5 });
          io.observe(el);
        } else {
          animateCounter(el, num, 700);
        }
      }
    });
  }

  // ── 2. AnimatedList：列表逐项入场 ──────────────────────
  // ReactBits Components/AnimatedList 复刻
  // 列表项逐项 fadeInUp，每项延迟 40ms，最多前 10 项有动画，后续直接显示
  function applyStaggerAnimation(container, selector, opts) {
    if (!container || prefersReducedMotion()) return;
    const items = container.querySelectorAll(selector);
    if (!items.length) return;
    const stagger = (opts && opts.stagger) || 40;
    const maxAnimated = (opts && opts.max) || 10;
    items.forEach(function (item, idx) {
      if (idx >= maxAnimated) return;
      item.style.opacity = "0";
      item.style.transform = "translateY(6px)";
      item.style.transition = "opacity 280ms cubic-bezier(0.16,1,0.3,1), transform 280ms cubic-bezier(0.16,1,0.3,1)";
      item.style.transitionDelay = (idx * stagger) + "ms";
      requestAnimationFrame(function () {
        requestAnimationFrame(function () {
          item.style.opacity = "1";
          item.style.transform = "translateY(0)";
        });
      });
    });
  }

  // 报告列表动效一键应用：证据、修复方案、风险、审查发现
  function applyReportListAnimation(container) {
    if (!container) return;
    applyStaggerAnimation(container, ".evidence-list .evidence-item", { stagger: 50, max: 8 });
    applyStaggerAnimation(container, ".change-list .change-item", { stagger: 45, max: 8 });
    applyStaggerAnimation(container, ".risk-list .risk-item", { stagger: 50, max: 6 });
    applyStaggerAnimation(container, ".review-findings li", { stagger: 40, max: 6 });
  }

  // ── 3. TiltCard：悬停 3D 倾斜（克制版） ─────────────────
  // ReactBits Components/TiltCard 复刻
  // 鼠标移动时卡片轻微倾斜，幅度 ±2deg（B 端克制，非炫技）
  // 触屏端禁用；尊重 prefers-reduced-motion
  function attachTilt(el, opts) {
    if (!el || prefersReducedMotion()) return;
    if (window.matchMedia("(hover: none)").matches) return; // 触屏禁用
    const maxTilt = (opts && opts.maxTilt) || 2; // 克制幅度
    let rafId = null;

    el.style.transformStyle = "preserve-3d";
    el.style.willChange = "transform";

    function onMove(e) {
      if (rafId) cancelAnimationFrame(rafId);
      rafId = requestAnimationFrame(function () {
        const rect = el.getBoundingClientRect();
        const x = e.clientX - rect.left;
        const y = e.clientY - rect.top;
        const cx = rect.width / 2;
        const cy = rect.height / 2;
        const rotateY = ((x - cx) / cx) * maxTilt;
        const rotateX = -((y - cy) / cy) * maxTilt;
        el.style.transform = "perspective(600px) rotateX(" + rotateX.toFixed(2) + "deg) rotateY(" + rotateY.toFixed(2) + "deg) translateY(-2px)";
      });
    }

    function onLeave() {
      if (rafId) cancelAnimationFrame(rafId);
      el.style.transform = "";
    }

    el.addEventListener("mousemove", onMove);
    el.addEventListener("mouseleave", onLeave);
    return function detach() {
      el.removeEventListener("mousemove", onMove);
      el.removeEventListener("mouseleave", onLeave);
      if (rafId) cancelAnimationFrame(rafId);
    };
  }

  // 报告指标卡片批量挂载倾斜
  function applyTiltToMetricCards(container) {
    if (!container) return;
    const cards = container.querySelectorAll(".report-metric-card");
    cards.forEach(function (card) { attachTilt(card, { maxTilt: 1.5 }); });
  }

  // ── 4. Ripple：点击涟漪 ────────────────────────────────
  // ReactBits Animations/Ripple 复刻
  // 从点击点扩散的涟漪反馈，确认操作已接收
  function attachRipple(el) {
    if (!el || prefersReducedMotion()) return;
    el.style.position = el.style.position || "relative";
    el.style.overflow = "hidden";

    el.addEventListener("click", function (e) {
      const rect = el.getBoundingClientRect();
      const size = Math.max(rect.width, rect.height);
      const x = e.clientX - rect.left - size / 2;
      const y = e.clientY - rect.top - size / 2;

      const ripple = document.createElement("span");
      ripple.className = "motion-ripple";
      ripple.style.cssText =
        "position:absolute;border-radius:50%;pointer-events:none;" +
        "width:" + size + "px;height:" + size + "px;" +
        "left:" + x + "px;top:" + y + "px;" +
        "background:currentColor;opacity:0.25;" +
        "transform:scale(0);transition:transform 480ms cubic-bezier(0.16,1,0.3,1),opacity 480ms ease-out;";
      el.appendChild(ripple);

      requestAnimationFrame(function () {
        ripple.style.transform = "scale(2)";
        ripple.style.opacity = "0";
      });
      setTimeout(function () {
        if (ripple.parentNode) ripple.parentNode.removeChild(ripple);
      }, 520);
    });
  }

  // 批量挂载涟漪到指定选择器
  function attachRipples(root, selector) {
    if (!root || prefersReducedMotion()) return;
    root.querySelectorAll(selector).forEach(function (el) { attachRipple(el); });
  }

  // ── 5. SmoothExpand：折叠区块平滑展开 ──────────────────
  // ReactBits Components/Accordion 增强
  // 用 grid-template-rows 0fr→1fr 过渡，比 max-height 更平滑且无需固定高度
  function attachSmoothExpand(detailsEl) {
    if (!detailsEl || prefersReducedMotion()) return;
    // 仅对 report-toc 和 patch details 应用
    const wrapper = detailsEl.querySelector(".patch-wrap") || detailsEl;
    if (!wrapper) return;

    let isAnimating = false;
    detailsEl.addEventListener("toggle", function () {
      if (isAnimating) return;
      isAnimating = true;
      if (detailsEl.open) {
        // 展开：0fr → 1fr
        wrapper.style.display = "grid";
        wrapper.style.gridTemplateRows = "0fr";
        wrapper.style.opacity = "0";
        wrapper.style.overflow = "hidden";
        wrapper.style.transition = "grid-template-rows 240ms cubic-bezier(0.16,1,0.3,1), opacity 240ms ease-out";
        requestAnimationFrame(function () {
          requestAnimationFrame(function () {
            wrapper.style.gridTemplateRows = "1fr";
            wrapper.style.opacity = "1";
          });
        });
        setTimeout(function () {
          wrapper.style.gridTemplateRows = "";
          wrapper.style.opacity = "";
          wrapper.style.overflow = "";
          wrapper.style.transition = "";
          wrapper.style.display = "";
          isAnimating = false;
        }, 280);
      } else {
        // 折叠：1fr → 0fr
        wrapper.style.gridTemplateRows = "1fr";
        wrapper.style.opacity = "1";
        wrapper.style.overflow = "hidden";
        wrapper.style.transition = "grid-template-rows 200ms cubic-bezier(0.4,0,0.2,1), opacity 200ms ease-in";
        requestAnimationFrame(function () {
          requestAnimationFrame(function () {
            wrapper.style.gridTemplateRows = "0fr";
            wrapper.style.opacity = "0";
          });
        });
        setTimeout(function () {
          wrapper.style.gridTemplateRows = "";
          wrapper.style.opacity = "";
          wrapper.style.overflow = "";
          wrapper.style.transition = "";
          isAnimating = false;
        }, 240);
      }
    });
  }

  // ── 6. ThemeTransition：主题切换平滑过渡 ────────────────
  // 全局体验：深浅色切换时所有颜色属性 350ms 平滑过渡，消除闪烁
  let themeTransitionActive = false;
  function attachThemeTransition() {
    if (prefersReducedMotion()) return;
    const btn = document.getElementById("theme-toggle-btn");
    if (!btn) return;
    btn.addEventListener("click", function () {
      if (themeTransitionActive) return;
      themeTransitionActive = true;
      const body = document.body;
      body.style.transition = "background-color 350ms ease, color 350ms ease";
      const allEls = body.querySelectorAll("*");
      // 仅对关键元素加过渡，避免性能问题
      allEls.forEach(function (el) {
        if (el.querySelectorAll("*").length > 50) return; // 跳过大型容器
        el.style.transition = (el.style.transition ? el.style.transition + ", " : "") +
          "background-color 350ms ease, border-color 350ms ease, color 350ms ease";
      });
      setTimeout(function () {
        body.style.transition = "";
        allEls.forEach(function (el) {
          if (el.querySelectorAll("*").length > 50) return;
          el.style.transition = "";
        });
        themeTransitionActive = false;
      }, 400);
    });
  }

  // ── 报告动效一键应用 ───────────────────────────────────
  // 在 renderReport 完成后调用，统一挂载所有报告相关动效
  function applyReportMotion(container) {
    if (!container) return;
    applyCounters(container);
    applyReportListAnimation(container);
    applyTiltToMetricCards(container);
    // 涟漪挂载到报告工具栏按钮
    attachRipples(container, ".report-action, .patch-copy, .patch-download, .patch-view-btn");
    // 平滑展开挂载到 TOC 和 patch details
    container.querySelectorAll(".report-toc, details#report-patch").forEach(function (d) {
      attachSmoothExpand(d);
    });
  }

  // ── 全局动效初始化（页面加载后调用一次） ─────────────────
  function init() {
    attachThemeTransition();
    // 主按钮和会话卡片涟漪
    attachRipples(document, ".primary-action, #report-toggle, .report-action");
  }

  // ── 暴露命名空间 ───────────────────────────────────────
  const ns = {
    animateCounter: animateCounter,
    applyCounters: applyCounters,
    applyStaggerAnimation: applyStaggerAnimation,
    applyReportListAnimation: applyReportListAnimation,
    attachTilt: attachTilt,
    applyTiltToMetricCards: applyTiltToMetricCards,
    attachRipple: attachRipple,
    attachRipples: attachRipples,
    attachSmoothExpand: attachSmoothExpand,
    attachThemeTransition: attachThemeTransition,
    applyReportMotion: applyReportMotion,
    init: init,
    prefersReducedMotion: prefersReducedMotion,
  };

  IA.Motion = ns;
  window.IssueAgent = IA;
})();
