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
  // 动画结束后必须清理 inline 的 transform/transition/opacity，
  // 否则会覆盖 CSS :hover 规则（如 .risk-item:hover { transform: translateX(2px) }），
  // 导致列表项 hover 交互全部失效
  function applyStaggerAnimation(container, selector, opts) {
    if (!container || prefersReducedMotion()) return;
    const items = container.querySelectorAll(selector);
    if (!items.length) return;
    const stagger = (opts && opts.stagger) || 40;
    const maxAnimated = (opts && opts.max) || 10;
    const baseDur = 280;
    items.forEach(function (item, idx) {
      if (idx >= maxAnimated) return;
      item.style.opacity = "0";
      item.style.transform = "translateY(6px)";
      item.style.transition = "opacity " + baseDur + "ms cubic-bezier(0.16,1,0.3,1), transform " + baseDur + "ms cubic-bezier(0.16,1,0.3,1)";
      item.style.transitionDelay = (idx * stagger) + "ms";
      requestAnimationFrame(function () {
        requestAnimationFrame(function () {
          item.style.opacity = "1";
          item.style.transform = "translateY(0)";
        });
      });
      // 动画结束后清理 inline 样式，恢复 CSS hover 规则的优先级
      const cleanupDelay = baseDur + idx * stagger + 60;
      setTimeout(function () {
        item.style.transition = "";
        item.style.transitionDelay = "";
        item.style.transform = "";
        item.style.opacity = "";
      }, cleanupDelay);
    });
  }

  // 报告列表动效一键应用：章节入场 + 证据、修复方案、风险、审查发现
  function applyReportListAnimation(container) {
    if (!container) return;
    // 报告章节逐段入场，引导从上到下阅读，max 12 覆盖完整报告
    applyStaggerAnimation(container, ".report-section", { stagger: 60, max: 12 });
    applyStaggerAnimation(container, ".evidence-list .evidence-item", { stagger: 50, max: 8 });
    applyStaggerAnimation(container, ".change-list .change-item", { stagger: 45, max: 8 });
    applyStaggerAnimation(container, ".risk-list .risk-item", { stagger: 50, max: 6 });
    applyStaggerAnimation(container, ".review-findings li", { stagger: 40, max: 6 });
  }

  // ── 会话历史列表入场：ReactBits AnimatedList 复刻 ─────────
  // 接受 row 数组（首次渲染全量 / 增量 diff 仅新增），逐项 fadeInUp
  // 只对前 12 项动画，避免长列表卡顿；动画结束后清理 inline transition，
  // 不影响 hover 过渡。尊重 prefers-reduced-motion：直接显示。
  function applySessionListMotion(rows) {
    if (!rows || !rows.length || prefersReducedMotion()) return;
    const maxAnimated = 12;
    const stagger = 35;
    const baseDur = 280;
    Array.prototype.slice.call(rows).forEach(function (row, idx) {
      if (idx >= maxAnimated) return;
      row.style.opacity = "0";
      row.style.transform = "translateY(6px)";
      row.style.transition =
        "opacity " + baseDur + "ms cubic-bezier(0.16,1,0.3,1), " +
        "transform " + baseDur + "ms cubic-bezier(0.16,1,0.3,1)";
      row.style.transitionDelay = (idx * stagger) + "ms";
      requestAnimationFrame(function () {
        requestAnimationFrame(function () {
          row.style.opacity = "1";
          row.style.transform = "translateY(0)";
        });
      });
      // 动画结束后清理 inline 样式，避免覆盖全局 hover 过渡规则
      const cleanupDelay = baseDur + idx * stagger + 60;
      setTimeout(function () {
        row.style.transition = "";
        row.style.transitionDelay = "";
        row.style.transform = "";
        row.style.opacity = "";
      }, cleanupDelay);
    });
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

  // ── 4. Ripple：点击涟漪（全局事件委托） ─────────────────
  // ReactBits Animations/Ripple 复刻
  // 从点击点扩散的涟漪反馈，确认操作已接收
  // 用全局 pointerdown 委托，一次注册覆盖所有匹配按钮，动态新增元素也自动生效
  const RIPPLE_SELECTOR = [
    ".primary-action",
    "#report-toggle",
    ".report-action",
    ".patch-copy",
    ".patch-download",
    ".patch-view-btn",
    ".session-action",
    ".batch-btn",
    "#theme-toggle-btn",
    "#chat-send-btn",
    "#chat-stop-btn",
    ".retry-button",
    "#report-fullscreen-btn",
    "#report-split-btn",
    "#report-default-fs-btn",
    "#report-close-btn",
    "#report-print-btn",
    ".msg-action-btn",
    "#cancel-analysis",
    ".chart-zoom-btn",
    ".evidence-load-more",
    ".timeline-expand-btn",
    ".report-back-top",
    ".chart-modal-close",
    ".history-empty-cta-btn",
    ".import-session-btn",
    ".icon-button",
    ".theme-toggle",
    ".mobile-history-toggle",
    ".back-button",
    "button[type='button']",
  ].join(",");

  let rippleDelegated = false;
  function initRippleDelegation() {
    if (rippleDelegated || prefersReducedMotion()) return;
    rippleDelegated = true;
    document.addEventListener("pointerdown", function (e) {
      if (e.button !== 0 && e.pointerType !== "touch") return; // 仅左键/触屏
      const el = e.target.closest(RIPPLE_SELECTOR);
      if (!el || el.disabled) return;
      // 移动端触觉反馈：轻触按钮时 8ms 震动，增强操作确认感
      // 仅在支持 vibrate API 且触屏场景下触发，桌面端无感
      if (e.pointerType === "touch" && navigator.vibrate) {
        try { navigator.vibrate(8); } catch (vErr) { /* ignore */ }
      }
      // 确保元素是定位上下文且不溢出
      const cs = getComputedStyle(el);
      if (cs.position === "static") el.style.position = "relative";
      if (cs.overflow === "visible") el.style.overflow = "hidden";

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
        "background:currentColor;opacity:0.22;" +
        "transform:scale(0);transition:transform 480ms cubic-bezier(0.16,1,0.3,1),opacity 480ms ease-out;";
      el.appendChild(ripple);

      requestAnimationFrame(function () {
        ripple.style.transform = "scale(2)";
        ripple.style.opacity = "0";
      });
      setTimeout(function () {
        if (ripple.parentNode) ripple.parentNode.removeChild(ripple);
      }, 520);
    }, { passive: true });
  }

  // 单元素挂载（向后兼容，内部委托已覆盖则跳过）
  function attachRipple(el) {
    if (!el || prefersReducedMotion()) return;
    // 委托模式无需单独挂载，init 时已全局注册
  }

  // 批量挂载（向后兼容，委托模式已覆盖）
  function attachRipples(root, selector) {
    // 委托模式已覆盖所有匹配元素，无需操作
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

  // ── 7. SplitText：标题逐字入场 ─────────────────────────
  // ReactBits Text Animations/SplitText 复刻
  // 将标题文字按字符拆分，逐字 fadeInUp 入场，营造"打字机"式精致感
  // 仅对短标题（≤60 字符）启用，长标题退化为整体 fadeIn 避免卡顿
  function applySplitText(el) {
    if (!el || prefersReducedMotion()) return;
    const text = el.textContent;
    if (!text || text.length > 60) return; // 长标题跳过，避免逐字过多
    const chars = Array.from(text);
    el.textContent = "";
    el.style.opacity = "1";
    const frag = document.createDocumentFragment();
    chars.forEach(function (ch, i) {
      const span = document.createElement("span");
      span.textContent = ch === " " ? "\u00A0" : ch;
      span.style.display = "inline-block";
      span.style.opacity = "0";
      span.style.transform = "translateY(8px)";
      span.style.transition = "opacity 240ms cubic-bezier(0.16,1,0.3,1), transform 240ms cubic-bezier(0.16,1,0.3,1)";
      span.style.transitionDelay = (i * 18) + "ms";
      frag.appendChild(span);
    });
    el.appendChild(frag);
    requestAnimationFrame(function () {
      requestAnimationFrame(function () {
        el.querySelectorAll("span").forEach(function (span) {
          span.style.opacity = "1";
          span.style.transform = "translateY(0)";
        });
      });
    });
    // 动画结束后清理 inline 样式，避免影响后续布局
    setTimeout(function () {
      el.querySelectorAll("span").forEach(function (span) {
        span.style.transition = "";
        span.style.transitionDelay = "";
      });
    }, chars.length * 18 + 320);
  }

  // ── 8. SpotlightCard：鼠标跟随聚光灯 ────────────────────
  // ReactBits Components/SpotlightCard 复刻
  // 鼠标在卡片上移动时，以鼠标为中心绘制柔和径向聚光灯，强化交互指向感
  // 通过 CSS 变量 --spot-x / --spot-y / --spot-o 驱动 ::after 伪元素渐变，
  // 不直接操作 DOM 样式，避免与现有 box-shadow / border-color 过渡规则冲突
  // 触屏端禁用（无 hover 态）；尊重 prefers-reduced-motion
  function attachSpotlight(el, opts) {
    if (!el || prefersReducedMotion()) return;
    if (window.matchMedia("(hover: none)").matches) return; // 触屏禁用
    const intensity = (opts && opts.intensity) || 0.18;
    const size = (opts && opts.size) || 380;

    function onMove(e) {
      const rect = el.getBoundingClientRect();
      const x = e.clientX - rect.left;
      const y = e.clientY - rect.top;
      el.style.setProperty("--spot-x", x + "px");
      el.style.setProperty("--spot-y", y + "px");
      el.style.setProperty("--spot-o", String(intensity));
      el.style.setProperty("--spot-size", size + "px");
    }
    function onLeave() {
      el.style.setProperty("--spot-o", "0");
    }
    el.addEventListener("mousemove", onMove);
    el.addEventListener("mouseleave", onLeave);
    return function detach() {
      el.removeEventListener("mousemove", onMove);
      el.removeEventListener("mouseleave", onLeave);
    };
  }

  // 批量挂载 SpotlightCard：hero 步骤卡 / 案例卡 / 报告指标卡 / 核心结论卡
  function applySpotlights(root) {
    if (!root || prefersReducedMotion()) return;
    const selectors = [
      ".hero-step",
      ".hero-example",
      ".report-metric-card",
      ".report-conclusion",
      ".matrix-stat",
    ];
    selectors.forEach(function (sel) {
      root.querySelectorAll(sel).forEach(function (el) {
        attachSpotlight(el);
      });
    });
  }

  // ── 9. Magnet：磁吸按钮 ───────────────────────────────
  // ReactBits Animations/Magnet 复刻
  // 光标靠近时元素向光标方向轻微偏移，离开后弹回原位，
  // 提供克制的触觉反馈而非视觉炫技。监听挂在父容器上，
  // 半径内才生效；触屏端跳过；尊重 prefers-reduced-motion
  function attachMagnet(el, opts) {
    if (!el || prefersReducedMotion()) return;
    if (window.matchMedia("(hover: none)").matches) return;
    const strength = (opts && opts.strength) || 0.22;
    const radius = (opts && opts.radius) || 110;
    const zone = el.parentElement || el;

    function onMove(e) {
      const rect = el.getBoundingClientRect();
      const cx = rect.left + rect.width / 2;
      const cy = rect.top + rect.height / 2;
      const dx = e.clientX - cx;
      const dy = e.clientY - cy;
      const dist = Math.hypot(dx, dy);
      if (dist < radius) {
        el.style.transform = "translate(" + (dx * strength).toFixed(1) + "px," + (dy * strength).toFixed(1) + "px)";
      } else {
        el.style.transform = "";
      }
    }
    function onLeave() {
      el.style.transform = "";
    }
    zone.addEventListener("mousemove", onMove);
    zone.addEventListener("mouseleave", onLeave);
    return function detach() {
      zone.removeEventListener("mousemove", onMove);
      zone.removeEventListener("mouseleave", onLeave);
    };
  }

  // ── 10. GridGlow：Hero 网格单元高亮 ───────────────
  // ReactBits Backgrounds/Squares 思路的轻量实现
  // 光标在 hero 区移动时，吸附到 48px 工程网格并高亮当前单元格，
  // 呼应 .msg.hero::before 的网格纸背景——工具感而非星空/科幻感。
  // 仅更新 CSS 变量，渲染由 .msg.hero::after 承担；触屏端跳过
  function attachGridGlow(heroEl) {
    if (!heroEl || prefersReducedMotion()) return;
    if (window.matchMedia("(hover: none)").matches) return;
    const CELL = 48; // 与 .msg.hero::before 的 background-size 保持一致

    function onMove(e) {
      const rect = heroEl.getBoundingClientRect();
      const x = e.clientX - rect.left;
      const y = e.clientY - rect.top;
      // 吸附到网格单元左上角（-1px 与背景网格线对齐）
      heroEl.style.setProperty("--grid-x", (Math.floor(x / CELL) * CELL - 1) + "px");
      heroEl.style.setProperty("--grid-y", (Math.floor(y / CELL) * CELL - 1) + "px");
      heroEl.style.setProperty("--grid-o", "1");
    }
    function onLeave() {
      heroEl.style.setProperty("--grid-o", "0");
    }
    heroEl.addEventListener("mousemove", onMove);
    heroEl.addEventListener("mouseleave", onLeave);
  }

  // ── 报告动效一键应用 ───────────────────────────────────
  // 在 renderReport 完成后调用，统一挂载所有报告相关动效
  function applyReportMotion(container) {
    if (!container) return;
    applyCounters(container);
    applyReportListAnimation(container);
    applyTiltToMetricCards(container);
    // SpotlightCard 报告指标卡 + 核心结论卡
    applySpotlights(container);
    // SplitText 报告大标题逐字入场
    const titleEl = container.querySelector(".report-summary-title, #report-summary h3, .report-header h2");
    if (titleEl) applySplitText(titleEl);
    // 核心结论区 ScaleIn 入场：作为报告最顶端卡片，用 scale+translateY 强调"结论前置"
    const conclusionEl = container.querySelector(".report-conclusion");
    if (conclusionEl) {
      const reduced = prefersReducedMotion();
      if (!reduced) {
        conclusionEl.style.opacity = "0";
        conclusionEl.style.transform = "translateY(12px) scale(0.96)";
        conclusionEl.style.transition = "opacity 380ms cubic-bezier(0.16,1,0.3,1), transform 380ms cubic-bezier(0.16,1,0.3,1)";
        requestAnimationFrame(function () {
          requestAnimationFrame(function () {
            conclusionEl.style.opacity = "1";
            conclusionEl.style.transform = "translateY(0) scale(1)";
          });
        });
        setTimeout(function () { conclusionEl.style.transition = ""; }, 420);
      }
    }
    // 平滑展开挂载到 TOC 和 patch details
    container.querySelectorAll(".report-toc, details#report-patch").forEach(function (d) {
      attachSmoothExpand(d);
    });
  }

  // ── Hero 区动效一键应用 ────────────────────────────────
  // 在 renderHero 完成后调用：网格单元高亮 + 标题逐字入场 + 卡片聚光灯 + CTA 磁吸
  function applyHeroMotion(heroEl) {
    if (!heroEl) return;
    // 网格单元高亮（呼应工程网格纸背景）
    attachGridGlow(heroEl);
    // 标题 SplitText 逐字入场（仅主标题，克制不炫技）
    const titleEl = heroEl.querySelector(".hero-title");
    if (titleEl) {
      applySplitText(titleEl);
    }
    // 卡片 SpotlightCard 聚光灯
    applySpotlights(heroEl);
    // CTA 磁吸
    const cta = heroEl.querySelector(".hero-cta-button");
    if (cta) attachMagnet(cta);
  }

  // ── 全局动效初始化（页面加载后调用一次） ─────────────────
  function init() {
    attachThemeTransition();
    // 全局涟漪委托：覆盖所有按钮类元素，动态新增也自动生效
    initRippleDelegation();
  }

  // ── 暴露命名空间 ───────────────────────────────────────
  const ns = {
    animateCounter: animateCounter,
    applyCounters: applyCounters,
    applyStaggerAnimation: applyStaggerAnimation,
    applyReportListAnimation: applyReportListAnimation,
    applySessionListMotion: applySessionListMotion,
    attachTilt: attachTilt,
    applyTiltToMetricCards: applyTiltToMetricCards,
    attachRipple: attachRipple,
    attachRipples: attachRipples,
    attachSmoothExpand: attachSmoothExpand,
    attachThemeTransition: attachThemeTransition,
    applySplitText: applySplitText,
    attachSpotlight: attachSpotlight,
    applySpotlights: applySpotlights,
    attachMagnet: attachMagnet,
    attachGridGlow: attachGridGlow,
    applyReportMotion: applyReportMotion,
    applyHeroMotion: applyHeroMotion,
    init: init,
    prefersReducedMotion: prefersReducedMotion,
  };

  IA.Motion = ns;
  window.IssueAgent = IA;
})();
