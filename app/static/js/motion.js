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
  // rafId 存到元素 dataset，disposeReportCharts 时可取消
  function animateCounter(el, target, duration) {
    if (!el || prefersReducedMotion()) return;
    // 取消上一次未完成的动画
    if (el.dataset.counterRafId) {
      cancelAnimationFrame(parseInt(el.dataset.counterRafId, 10));
    }
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
        el.dataset.counterRafId = String(requestAnimationFrame(tick));
      } else {
        el.textContent = String(target);
        delete el.dataset.counterRafId;
      }
    }
    el.dataset.counterRafId = String(requestAnimationFrame(tick));
  }

  // 取消所有正在进行的 Counter 动画（disposeReportCharts 调用）
  function cancelCounters(container) {
    if (!container) return;
    const els = container.querySelectorAll("[data-counter-raf-id]");
    els.forEach(function (el) {
      cancelAnimationFrame(parseInt(el.dataset.counterRafId, 10));
      delete el.dataset.counterRafId;
    });
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
      // 已被 ScrollReveal 接管的元素跳过，避免两套入场动画互相覆盖
      if (item.dataset.scrollReveal === "1") return;
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
    const maxAnimated = 8;
    const stagger = 24;
    const baseDur = 200;
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
  // 用 CSS class 触发过渡，避免 querySelectorAll("*") 的 O(n²) 遍历
  // .theme-transitioning 类下的元素由 CSS 统一加过渡，切换完移除类
  let themeTransitionActive = false;
  function attachThemeTransition() {
    if (prefersReducedMotion()) return;
    const btn = document.getElementById("theme-toggle-btn");
    if (!btn) return;
    btn.addEventListener("click", function () {
      if (themeTransitionActive) return;
      themeTransitionActive = true;
      document.body.classList.add("theme-transitioning");
      setTimeout(function () {
        document.body.classList.remove("theme-transitioning");
        themeTransitionActive = false;
      }, 400);
    });
  }

  // ── 7. SplitText：标题逐字入场 ─────────────────────────
  // ReactBits Text Animations/SplitText 复刻
  // 将标题文字按字符拆分，逐字 fadeInUp 入场，营造"打字机"式精致感
  // 仅对短标题（≤60 字符）启用，长标题退化为整体 fadeIn 避免卡顿。
  // 按文本节点递归拆分而非重写 textContent，保留子元素结构不被破坏
  function applySplitText(el) {
    if (!el || prefersReducedMotion()) return;
    const accessibleText = (el.textContent || "").trim();
    const total = accessibleText.length;
    if (!total || total > 60) return; // 长标题跳过，避免逐字过多
    el.setAttribute("aria-label", accessibleText);
    const spans = [];
    let charIdx = 0;

    function splitNode(node) {
      const children = Array.prototype.slice.call(node.childNodes);
      children.forEach(function (child) {
        if (child.nodeType === 3) {
          const frag = document.createDocumentFragment();
          Array.from(child.textContent || "").forEach(function (ch) {
            const span = document.createElement("span");
            span.setAttribute("aria-hidden", "true");
            span.textContent = ch === " " ? "\u00A0" : ch;
            span.style.display = "inline-block";
            span.style.opacity = "0";
            span.style.transform = "translateY(8px)";
            span.style.transition = "opacity 240ms cubic-bezier(0.16,1,0.3,1), transform 240ms cubic-bezier(0.16,1,0.3,1)";
            span.style.transitionDelay = (charIdx * 18) + "ms";
            charIdx++;
            spans.push(span);
            frag.appendChild(span);
          });
          node.replaceChild(frag, child);
        } else if (child.nodeType === 1) {
          splitNode(child); // 元素子节点保留结构，递归拆分内部文本
        }
      });
    }

    splitNode(el);
    el.style.opacity = "1";
    requestAnimationFrame(function () {
      requestAnimationFrame(function () {
        spans.forEach(function (span) {
          span.style.opacity = "1";
          span.style.transform = "translateY(0)";
        });
      });
    });
    // 动画结束后清理 inline 样式，避免影响后续布局
    setTimeout(function () {
      spans.forEach(function (span) {
        span.style.transition = "";
        span.style.transitionDelay = "";
      });
    }, total * 18 + 320);
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
    var defaultIntensity = el.classList.contains("report-chart") ? 0.26 : 0.12;
    const intensity = opts && typeof opts.intensity === "number" ? opts.intensity : defaultIntensity;
    const size = (opts && opts.size) || 380;

    function onMove(e) {
      const rect = el.getBoundingClientRect();
      const x = e.clientX - rect.left;
      const y = e.clientY - rect.top;
      el.style.setProperty("--spot-x", x + "px");
      el.style.setProperty("--spot-y", y + "px");
      el.style.setProperty("--spot-o", String(intensity));
      el.style.setProperty("--spot-size", size + "px");
      el.dataset.spotlightActive = "true";
    }
    function onLeave() {
      el.style.setProperty("--spot-o", "0");
      delete el.dataset.spotlightActive;
    }
    el.addEventListener("mousemove", onMove);
    el.addEventListener("mouseleave", onLeave);
    return function detach() {
      el.removeEventListener("mousemove", onMove);
      el.removeEventListener("mouseleave", onLeave);
    };
  }

  // 图表使用清晰的边缘聚光；可操作报告项只用低强度高光提示点击区域。
  function applySpotlights(root) {
    if (!root || prefersReducedMotion()) return;
    root.querySelectorAll(".report-chart").forEach(function (el) {
      attachSpotlight(el);
    });
    root.querySelectorAll(".change-item, .risk-item").forEach(function (el) {
      attachSpotlight(el, { intensity: 0.14, size: 260 });
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

  // ── 10. ParticleNet：Hero 粒子连线拓扑背景 ───────────────
  // ReactBits Backgrounds/Particles 复刻（canvas 版，追加近邻连线成拓扑网）
  // 少量粒子低速漂移，近距离粒子间以极淡细线相连，光标附近的粒子
  // 会与光标轻微连线——贴合代码调用链/链路追踪的意象。
  // 30fps 限帧 + 低对比半透明配色，只做氛围衬托不抢主体视线；
  // 触屏端与 prefers-reduced-motion 仅绘制一帧静态拓扑；
  // hero 被移除后自动停止并释放监听
  function attachParticleNet(heroEl) {
    if (!heroEl) return;
    const canvas = document.createElement("canvas");
    canvas.className = "hero-net-canvas";
    canvas.setAttribute("aria-hidden", "true");
    heroEl.insertBefore(canvas, heroEl.firstChild);
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    const LINK = 130;        // 粒子间连线的距离上限
    const CURSOR_LINK = 160; // 光标与粒子连线的距离上限
    const REPEL = 120;       // 光标避让半径：范围内粒子被缓慢推开
    const PUSH = 0.9;        // 避让最大推力（px/帧，随距离衰减，保持缓慢）
    const SPEED = 0.24;      // 最大漂移速度（px/帧，30fps 下约 7px/s）
    const FRAME_MS = 33;     // 30fps 限帧：氛围背景不需要满帧，省 CPU
    const animated = !prefersReducedMotion() && !window.matchMedia("(hover: none)").matches;
    let parts = [];
    let dpr = 1;
    let pointerX = -1e4;
    let pointerY = -1e4;
    let rafId = null;
    let lastTs = 0;
    let heroVisible = true;
    let baseColor = "203,213,225";
    let nodeAlpha = 0.5;
    let linkAlpha = 0.22;
    let accentColor = "59,130,246";

    // 从主题 CSS 变量取色；两端都往对比度高的方向取：白底用深灰（slate-600）、
    // 黑底用亮灰（slate-300），否则同等透明度下背景几乎不可见
    function readColors() {
      const accent = getComputedStyle(document.documentElement).getPropertyValue("--accent").trim();
      const m = /^#([0-9a-f]{6})$/i.exec(accent);
      if (m) {
        accentColor =
          parseInt(m[1].slice(0, 2), 16) + "," +
          parseInt(m[1].slice(2, 4), 16) + "," +
          parseInt(m[1].slice(4, 6), 16);
      }
      const light = document.documentElement.getAttribute("data-theme") === "light";
      baseColor = light ? "71,85,105" : "203,213,225";
      nodeAlpha = light ? 0.6 : 0.5;
      linkAlpha = light ? 0.3 : 0.22;
    }

    function rebuild() {
      const w = heroEl.clientWidth;
      const h = heroEl.clientHeight;
      if (!w || !h) return;
      dpr = window.devicePixelRatio || 1;
      canvas.width = Math.round(w * dpr);
      canvas.height = Math.round(h * dpr);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      // 粒子密度随面积走，限制上限避免连线 O(n²) 撑大开销
      const count = Math.max(24, Math.min(64, Math.round((w * h) / 26000)));
      parts = [];
      for (let i = 0; i < count; i++) {
        parts.push({
          x: Math.random() * w,
          y: Math.random() * h,
          vx: (Math.random() - 0.5) * 2 * SPEED,
          vy: (Math.random() - 0.5) * 2 * SPEED,
        });
      }
      draw();
    }

    function step() {
      const w = canvas.width / dpr;
      const h = canvas.height / dpr;
      const hasPointer = pointerX > -1e3;
      for (let i = 0; i < parts.length; i++) {
        const p = parts[i];
        p.x += p.vx;
        p.y += p.vy;
        // 光标靠近时缓慢避让：推力随距离线性衰减，只改位置不改速度，
        // 光标离开后粒子按原速度继续漂移，不会被永久“吹散”
        if (hasPointer) {
          const rx = p.x - pointerX;
          const ry = p.y - pointerY;
          const rd = Math.hypot(rx, ry);
          if (rd < REPEL && rd > 0.5) {
            const f = PUSH * (1 - rd / REPEL);
            p.x += (rx / rd) * f;
            p.y += (ry / rd) * f;
          }
        }
        // 出界从对侧回来，避免粒子在边缘堆积
        if (p.x < -8) p.x = w + 8;
        else if (p.x > w + 8) p.x = -8;
        if (p.y < -8) p.y = h + 8;
        else if (p.y > h + 8) p.y = -8;
      }
    }

    function draw() {
      const w = canvas.width / dpr;
      const h = canvas.height / dpr;
      ctx.clearRect(0, 0, w, h);
      ctx.lineWidth = 1;
      // 近邻粒子连线：越近越实，淡到看不见的直接跳过省绘制
      for (let i = 0; i < parts.length; i++) {
        for (let j = i + 1; j < parts.length; j++) {
          const dx = parts[i].x - parts[j].x;
          const dy = parts[i].y - parts[j].y;
          const dist = Math.hypot(dx, dy);
          if (dist >= LINK) continue;
          const a = linkAlpha * (1 - dist / LINK);
          if (a < 0.01) continue;
          ctx.strokeStyle = "rgba(" + baseColor + "," + a.toFixed(3) + ")";
          ctx.beginPath();
          ctx.moveTo(parts[i].x, parts[i].y);
          ctx.lineTo(parts[j].x, parts[j].y);
          ctx.stroke();
        }
      }
      // 光标连线用主题色但依旧克制透明度：轻微的“追踪”反馈而非高亮特效
      if (pointerX > -1e3) {
        for (let i = 0; i < parts.length; i++) {
          const dist = Math.hypot(parts[i].x - pointerX, parts[i].y - pointerY);
          if (dist >= CURSOR_LINK) continue;
          const a = 0.32 * (1 - dist / CURSOR_LINK);
          ctx.strokeStyle = "rgba(" + accentColor + "," + a.toFixed(3) + ")";
          ctx.beginPath();
          ctx.moveTo(parts[i].x, parts[i].y);
          ctx.lineTo(pointerX, pointerY);
          ctx.stroke();
        }
      }
      for (let i = 0; i < parts.length; i++) {
        ctx.beginPath();
        ctx.arc(parts[i].x, parts[i].y, 1.6, 0, Math.PI * 2);
        ctx.fillStyle = "rgba(" + baseColor + "," + nodeAlpha + ")";
        ctx.fill();
      }
    }

    function tick(ts) {
      rafId = null;
      if (!canvas.isConnected) { teardown(); return; }
      if (document.hidden || !heroVisible) return;
      // 30fps 限帧：不到间隔只续帧不重绘
      if (ts - lastTs >= FRAME_MS) {
        lastTs = ts;
        step();
        draw();
      }
      rafId = requestAnimationFrame(tick);
    }

    function syncAnimation() {
      const shouldRun = animated && heroVisible && !document.hidden && canvas.isConnected;
      if (shouldRun && rafId === null) {
        lastTs = 0;
        rafId = requestAnimationFrame(tick);
      } else if (!shouldRun && rafId !== null) {
        cancelAnimationFrame(rafId);
        rafId = null;
      }
    }

    function onMove(e) {
      const rect = canvas.getBoundingClientRect();
      pointerX = e.clientX - rect.left;
      pointerY = e.clientY - rect.top;
    }

    function onLeave() {
      pointerX = -1e4;
      pointerY = -1e4;
    }

    let ro = null;
    let mo = null;
    let io = null;
    function teardown() {
      if (rafId !== null) cancelAnimationFrame(rafId);
      rafId = null;
      if (ro) ro.disconnect();
      if (mo) mo.disconnect();
      if (io) io.disconnect();
      heroEl.removeEventListener("pointermove", onMove);
      heroEl.removeEventListener("pointerleave", onLeave);
      document.removeEventListener("visibilitychange", syncAnimation);
    }

    readColors();
    rebuild();
    if ("ResizeObserver" in window) {
      ro = new ResizeObserver(function () { rebuild(); });
      ro.observe(heroEl);
    }
    // 主题切换（data-theme 挂在 <html>）后重取配色重绘
    if ("MutationObserver" in window) {
      mo = new MutationObserver(function () {
        if (!canvas.isConnected) { teardown(); return; }
        readColors();
        draw();
      });
      mo.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    }
    if (animated) {
      heroEl.addEventListener("pointermove", onMove);
      heroEl.addEventListener("pointerleave", onLeave);
      document.addEventListener("visibilitychange", syncAnimation);
      if ("IntersectionObserver" in window) {
        io = new IntersectionObserver(function (entries) {
          heroVisible = entries.some(function (entry) { return entry.isIntersecting; });
          syncAnimation();
        }, { threshold: 0 });
        io.observe(heroEl);
      }
      syncAnimation();
    }
  }

  // ── 11. ClickSpark：关键动作点击火花 ─────────────────────
  // ReactBits Animations/ClickSpark 复刻（DOM 版，免 canvas）
  // 用于关键 CTA 与图表操作：点击时短线从点击点向外迸发，
  // 与 Ripple（面反馈）互补，确认操作已被接收。图表使用更克制的 6 线版本，
  // 且事件层始终 pointer-events:none，不干扰 ECharts 的 tooltip / click / toolbox。
  const SPARK_SELECTOR = ".primary-action, .hero-cta-button, .hero-example, .report-chart-canvas, .chart-zoom-btn";
  let sparkDelegated = false;
  function initClickSpark() {
    if (sparkDelegated || prefersReducedMotion()) return;
    sparkDelegated = true;
    document.addEventListener("pointerdown", function (e) {
      if (e.button !== 0 && e.pointerType !== "touch") return;
      const el = e.target.closest(SPARK_SELECTOR);
      if (!el || el.disabled) return;
      const isChartInteraction = el.matches(".report-chart-canvas, .chart-zoom-btn");
      const burst = document.createElement("span");
      burst.className = "motion-spark-burst" + (isChartInteraction ? " motion-spark-burst--chart" : "");
      // 固定定位到视口坐标，避免受按钮 overflow:hidden 裁剪
      burst.style.left = e.clientX + "px";
      burst.style.top = e.clientY + "px";
      const rayCount = isChartInteraction ? 6 : 8;
      for (let i = 0; i < rayCount; i++) {
        const line = document.createElement("span");
        line.className = "motion-spark-line";
        line.style.setProperty("--spark-angle", (i * (360 / rayCount)) + "deg");
        burst.appendChild(line);
      }
      document.body.appendChild(burst);
      setTimeout(function () {
        if (burst.parentNode) burst.parentNode.removeChild(burst);
      }, 480);
    }, { passive: true });
  }

  // ── 12. BlurText：模糊→清晰入场 ──────────────────────
  // ReactBits Text Animations/BlurText 复刻
  // 整段文字从 blur(8px)+下移 淡入到清晰，与主标题 SplitText 逐字
  // 入场形成"先逐字后整段"的层次节奏；动画结束后清理 inline 样式
  function applyBlurText(el, delay) {
    if (!el || prefersReducedMotion()) return;
    const startDelay = delay || 0;
    el.style.opacity = "0";
    el.style.filter = "blur(8px)";
    el.style.transform = "translateY(6px)";
    el.style.transition =
      "opacity 480ms cubic-bezier(0.16,1,0.3,1) " + startDelay + "ms, " +
      "filter 480ms cubic-bezier(0.16,1,0.3,1) " + startDelay + "ms, " +
      "transform 480ms cubic-bezier(0.16,1,0.3,1) " + startDelay + "ms";
    requestAnimationFrame(function () {
      requestAnimationFrame(function () {
        el.style.opacity = "1";
        el.style.filter = "blur(0)";
        el.style.transform = "translateY(0)";
      });
    });
    setTimeout(function () {
      el.style.transition = "";
      el.style.filter = "";
      el.style.transform = "";
      el.style.opacity = "";
    }, startDelay + 540);
  }

  // ── 13. ScrollReveal：滚动进入视口时入场 ──────────────
  // ReactBits Text Animations/ScrollReveal 思路的块级实现
  // 现有 stagger 入场在渲染时一次性播完，用户滚到下方章节时无感；
  // 改用 IntersectionObserver：视口外的章节到达时才播入场动画，
  // root 传报告滚动容器（#report）；不支持 IO 时退化为直接显示
  let scrollRevealObserver = null;
  function applyScrollReveal(scrollRoot, items) {
    if (!items || !items.length || prefersReducedMotion()) return;
    if (!("IntersectionObserver" in window)) return;
    // 清理上一次报告的 observer，避免持有已移除 DOM 节点的引用
    if (scrollRevealObserver) { scrollRevealObserver.disconnect(); scrollRevealObserver = null; }
    const rootRect = scrollRoot ? scrollRoot.getBoundingClientRect() : { top: 0, bottom: window.innerHeight };
    const toObserve = [];
    Array.prototype.slice.call(items).forEach(function (item) {
      const r = item.getBoundingClientRect();
      // 已在视口内的交给渲染时的 stagger 动画，不重复处理
      if (r.top < rootRect.bottom && r.bottom > rootRect.top) return;
      item.dataset.scrollReveal = "1";
      item.style.opacity = "0";
      item.style.transform = "translateY(14px)";
      toObserve.push(item);
    });
    if (!toObserve.length) return;
    const io = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (!entry.isIntersecting) return;
        const item = entry.target;
        io.unobserve(item);
        item.style.transition = "opacity 420ms cubic-bezier(0.16,1,0.3,1), transform 420ms cubic-bezier(0.16,1,0.3,1)";
        requestAnimationFrame(function () {
          item.style.opacity = "1";
          item.style.transform = "translateY(0)";
        });
        // 动画结束后清理 inline 样式，恢复 CSS hover 规则优先级
        setTimeout(function () {
          item.style.transition = "";
          item.style.transform = "";
          item.style.opacity = "";
          delete item.dataset.scrollReveal;
        }, 480);
      });
    }, { root: scrollRoot || null, rootMargin: "0px 0px -8% 0px", threshold: 0.05 });
    scrollRevealObserver = io;
    toObserve.forEach(function (item) { io.observe(item); });
  }

  // ── 报告动效一键应用 ───────────────────────────────────
  // 在 renderReport 完成后调用，统一挂载所有报告相关动效
  function applyReportMotion(container) {
    if (!container) return;
    // AnimatedList + SpotlightCard + Magnet：克制地提示层级、点击区域和快捷操作。
    applyScrollReveal(container, container.querySelectorAll(".change-item, .risk-item"));
    applyReportListAnimation(container);
    applySpotlights(container);
    container.querySelectorAll(".report-item-action").forEach(function (button) {
      attachMagnet(button, { strength: 0.08, radius: 56 });
    });
    // 平滑展开挂载到 TOC 和 patch details
    container.querySelectorAll(".report-toc, details#report-patch").forEach(function (d) {
      attachSmoothExpand(d);
    });
  }

  // ── Hero 区动效一键应用 ────────────────────────────────
  // 在 renderHero 完成后调用：全屏点阵背景 + 标题逐字入场 + 卡片聚光灯 + CTA 磁吸
  function applyHeroMotion(heroEl) {
    if (!heroEl) return;
    // 背景保持低对比，前景交互用于提示“这里可探索/可操作”。
    attachParticleNet(heroEl);
    const titleEl = heroEl.querySelector(".hero-title");
    if (titleEl) applySplitText(titleEl);
    const subtitleEl = heroEl.querySelector(".hero-subtitle");
    if (subtitleEl) applyBlurText(subtitleEl, 220);
    heroEl.querySelectorAll(".hero-step").forEach(function (card) {
      attachSpotlight(card, { intensity: 0.2, size: 300 });
    });
    heroEl.querySelectorAll(".hero-example").forEach(function (card) {
      attachSpotlight(card, { intensity: 0.16, size: 320 });
    });
    const cta = heroEl.querySelector(".hero-cta-button");
    if (cta) attachMagnet(cta, { strength: 0.12, radius: 90 });
  }

  // ── 全局动效初始化（页面加载后调用一次） ─────────────────
  function init() {
    attachThemeTransition();
    // 操作反馈由统一的 hover / active / focus 样式承担，避免涟漪与火花叠加。
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
    attachParticleNet: attachParticleNet,
    initClickSpark: initClickSpark,
    applyBlurText: applyBlurText,
    applyScrollReveal: applyScrollReveal,
    applyReportMotion: applyReportMotion,
    applyHeroMotion: applyHeroMotion,
    cancelCounters: cancelCounters,
    init: init,
    prefersReducedMotion: prefersReducedMotion,
  };

  IA.Motion = ns;
  window.IssueAgent = IA;
})();
