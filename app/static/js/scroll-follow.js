/* 消息流滚动跟随模块（IA.ScrollFollow）：
 * - pinned 状态机：贴底时新消息自动滚到底部，实时跟随分析进度
 * - 用户上翻阅读后停止自动跟随，出现「回到底部」悬浮按钮并统计未读新消息数
 * - 点击按钮平滑滚回底部并恢复跟随；会话切换时 reset 恢复贴底
 * 独立模块：app.js 仅通过 scrollToBottomIfNear → notify/reset 委托，
 * 避免向巨型 IIFE 继续堆叠（C1 约束）。
 */
(function () {
  "use strict";

  const IA = window.IssueAgent;
  const t = IA.translate;

  // 距底小于该值视为「贴底」（消除亚像素/滚动取整误差）
  const BOTTOM_EPSILON = 8;
  // 跟随判定阈值：视口高度的 15%，最少 80px——小幅滚动仍跟随，明确上翻才停止
  const MIN_FOLLOW_THRESHOLD = 80;

  const state = {
    el: null, // #messages 滚动容器
    btn: null, // #jump-latest-btn 悬浮按钮
    badge: null, // .jump-latest-badge 未读徽标
    pinned: true, // 是否跟随底部
    unread: 0, // 停止跟随时累计的新消息数
    jumping: false, // 平滑滚动进行中（防止中间态 scroll 事件误判为上翻）
  };

  function distanceFromBottom() {
    return state.el.scrollHeight - state.el.scrollTop - state.el.clientHeight;
  }

  function nearBottom() {
    const dynamic = Math.max(MIN_FOLLOW_THRESHOLD, (state.el.clientHeight || 400) * 0.15);
    return distanceFromBottom() <= dynamic;
  }

  function hasOverflow() {
    return state.el.scrollHeight - state.el.clientHeight > 4;
  }

  // 欢迎视图（hero 引导页）不是对话流，没有"跟随最新消息"的语义：
  // 跳底按钮、自动跟随、reset 滚底在该视图下全部禁用。
  function isWelcomeView() {
    return Boolean(state.el && state.el.querySelector(".msg.hero"));
  }

  // 按钮锚点避开底部 input-bar / progress 条，动态计算 bottom 偏移
  function refreshAnchor() {
    let anchor = 16;
    const bar = document.getElementById("input-bar");
    if (bar && bar.offsetParent !== null) anchor += bar.offsetHeight + 8;
    const progress = document.getElementById("progress");
    if (progress && progress.offsetParent !== null && progress.textContent) anchor += progress.offsetHeight + 4;
    state.btn.style.bottom = anchor + "px";
  }

  function updateButton() {
    const show = !isWelcomeView() && !state.pinned && hasOverflow();
    state.btn.classList.toggle("visible", show);
    state.btn.setAttribute("aria-hidden", String(!show));
    state.btn.tabIndex = show ? 0 : -1;
    if (!show) {
      state.badge.hidden = true;
      return;
    }
    refreshAnchor();
    if (state.unread > 0) {
      state.badge.hidden = false;
      state.badge.textContent = state.unread > 99 ? "99+" : String(state.unread);
      state.btn.setAttribute(
        "aria-label",
        t("jump_latest_new_items", { count: state.unread }),
      );
    } else {
      state.badge.hidden = true;
      state.btn.setAttribute("aria-label", t("jump_to_latest"));
    }
  }

  function onScroll() {
    if (state.jumping) {
      // 平滑滚动途中：到达底部即解除跳跃守卫，中途不改变跟随状态
      if (distanceFromBottom() <= BOTTOM_EPSILON) state.jumping = false;
      return;
    }
    // 欢迎视图浏览引导内容不算"上翻离开"：保持贴底状态，
    // 保证从欢迎页进入分析后跟随从贴底开始
    if (isWelcomeView()) {
      state.pinned = true;
      state.unread = 0;
      updateButton();
      return;
    }
    const wasPinned = state.pinned;
    state.pinned = nearBottom();
    if (state.pinned) {
      state.unread = 0;
    }
    if (state.pinned !== wasPinned || !state.pinned) updateButton();
  }

  function scrollToBottom(smooth) {
    if (smooth && typeof state.el.scrollTo === "function") {
      state.el.scrollTo({ top: state.el.scrollHeight, behavior: "smooth" });
      // 平滑滚动依赖渲染帧，在后台标签页/无渲染环境下会被节流而永不抵达。
      // 400ms 后校验：仍在跟随且未贴底则强制直达，保证按钮在任何环境都可靠。
      setTimeout(function () {
        if (state.pinned && distanceFromBottom() > BOTTOM_EPSILON) {
          state.el.scrollTop = state.el.scrollHeight;
        }
      }, 400);
    } else {
      state.el.scrollTop = state.el.scrollHeight;
    }
  }

  /** 贴底跟随的兜底：后台标签页/无渲染环境下 rAF 会被暂停，定时器仍会触发（仅被钳制到 ≥1s）。 */
  function scheduleFollowFallback() {
    setTimeout(function () {
      if (state.pinned && distanceFromBottom() > BOTTOM_EPSILON) {
        state.el.scrollTop = state.el.scrollHeight;
      }
    }, 200);
  }

  /** 新内容到达：贴底则跟随滚到底；上翻中则累计未读并显示跳底按钮。 */
  function notify(container) {
    if (!state.el || container !== state.el) return;
    if (isWelcomeView()) return;
    if (state.pinned) {
      // 双 rAF：先让布局稳定（图片/代码高亮完成），再滚动到真实底部
      requestAnimationFrame(function () {
        requestAnimationFrame(function () {
          if (state.pinned) scrollToBottom(false);
        });
      });
      scheduleFollowFallback();
    } else {
      state.unread += 1;
      updateButton();
    }
  }

  /** 会话切换/工作区清空：恢复贴底、清零未读并立即滚到底。 */
  function reset() {
    state.pinned = true;
    state.unread = 0;
    state.jumping = false;
    if (state.el && !isWelcomeView()) {
      scrollToBottom(false);
      scheduleFollowFallback();
    }
    if (state.btn) updateButton();
  }

  function init() {
    state.el = document.getElementById("messages");
    state.btn = document.getElementById("jump-latest-btn");
    if (!state.el || !state.btn) return;
    state.badge = state.btn.querySelector(".jump-latest-badge");

    state.el.addEventListener("scroll", onScroll, { passive: true });
    state.btn.addEventListener("click", function () {
      state.jumping = true;
      state.pinned = true;
      state.unread = 0;
      updateButton();
      scrollToBottom(true);
      // 平滑滚动兜底：若 800ms 内未触发贴底 scroll 事件（如用户立即再上翻），解除守卫
      setTimeout(function () {
        state.jumping = false;
      }, 800);
    });
    // 用户切回标签页：贴底状态下立即补滚，无需等待下一条消息触发
    document.addEventListener("visibilitychange", function () {
      if (!document.hidden && state.pinned) scrollToBottom(false);
    });
    updateButton();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

  IA.ScrollFollow = { notify: notify, reset: reset, init: init };
})();
