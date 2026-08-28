/* markdown.js — Markdown 渲染模块（IA.Markdown）
 * marked + DOMPurify + highlight.js 渲染管线；任一 CDN 库加载失败时降级为纯文本
 * 独立模块：自 app.js 拆出（C1 约束），app.js 经 const 别名委托调用。
 */
(function () {
  "use strict";

  const IA = window.IssueAgent;
  const t = IA.translate;

  // Markdown 渲染：assistant 消息走 marked + DOMPurify + highlight.js，
  // 任意一个库加载失败则降级为 escapeHtml 纯文本，保证可用性
  var domPurifyHooked = false;
  function ensureDomPurifyHook() {
    if (domPurifyHooked || !window.DOMPurify || typeof window.DOMPurify.addHook !== "function") return;
    domPurifyHooked = true;
    window.DOMPurify.addHook("afterSanitizeAttributes", function (node) {
      if (node.tagName === "A" && node.hasAttribute("href")) {
        var href = node.getAttribute("href") || "";
        if (/^https?:/i.test(href)) {
          node.setAttribute("target", "_blank");
          node.setAttribute("rel", "noopener noreferrer nofollow");
        }
      }
    });
  }

  function renderMarkdown(text) {
    ensureDomPurifyHook();
    if (typeof text !== "string") return "";
    if (!window.marked || !window.DOMPurify) {
      // 降级：保留换行，转义 HTML
      return IA.escapeHtml(text).replace(/\n/g, "<br>");
    }
    try {
      marked.setOptions({
        gfm: true,
        breaks: true,
        headerIds: false,
        mangle: false,
      });
      const rawHtml = marked.parse(text);
      const cleanHtml = window.DOMPurify.sanitize(rawHtml, {
        ALLOWED_TAGS: [
          "h1", "h2", "h3", "h4", "h5", "h6",
          "p", "br", "hr",
          "strong", "em", "del", "mark", "sub", "sup",
          "ul", "ol", "li",
          "blockquote", "code", "pre",
          "a", "span", "div",
          "table", "thead", "tbody", "tr", "th", "td",
          "img",
        ],
        ALLOWED_ATTR: ["href", "title", "src", "alt", "class", "target", "rel", "colspan", "rowspan"],
      });
      return cleanHtml;
    } catch (e) {
      return IA.escapeHtml(text).replace(/\n/g, "<br>");
    }
  }

  // 为 pre>code 块注入语言标签 + 复制按钮 + 语法高亮
  function enhanceCodeBlocks(container) {
    if (!container) return;
    const blocks = container.querySelectorAll("pre > code");
    blocks.forEach(function (codeEl, idx) {
      const pre = codeEl.parentElement;
      if (pre.dataset.enhanced) return;
      pre.dataset.enhanced = "1";

      // 语言标签
      const langMatch = codeEl.className.match(/language-([\w-]+)/);
      const lang = langMatch ? langMatch[1] : "text";
      const langLabel = document.createElement("span");
      langLabel.className = "code-lang-label";
      langLabel.textContent = lang;
      pre.appendChild(langLabel);

      // 复制按钮
      const copyBtn = document.createElement("button");
      copyBtn.type = "button";
      copyBtn.className = "code-copy-btn";
      copyBtn.setAttribute("aria-label", t("copy_code"));
      copyBtn.innerHTML = IA.svgIcon("copy");
      copyBtn.addEventListener("click", function () {
        const text = codeEl.textContent;
        IA.copyToClipboard(text).then(
          function () {
            copyBtn.classList.add("copied");
            copyBtn.innerHTML = IA.svgIcon("check");
            setTimeout(function () {
              // 切换会话或重渲染报告后，copyBtn 可能已脱离文档，操作前检查
              if (!document.body.contains(copyBtn)) return;
              copyBtn.classList.remove("copied");
              copyBtn.innerHTML = IA.svgIcon("copy");
            }, 1500);
          },
          function () { /* ignore */ },
        );
      });
      pre.appendChild(copyBtn);

    });

    if (!blocks.length) return;
    const applyHighlight = function () {
      if (!window.hljs) return;
      blocks.forEach(function (codeEl) {
        if (!document.documentElement.contains(codeEl) || codeEl.dataset.highlighted) return;
        try { window.hljs.highlightElement(codeEl); } catch (e) { /* readable without highlighting */ }
      });
    };
    if (window.hljs) {
      applyHighlight();
    } else if (IA.ensureVendor) {
      IA.ensureVendor("highlight").then(function (loaded) {
        if (loaded) applyHighlight();
        else checkCdnFailures();
      });
    }
  }


  const ns = {
    render: renderMarkdown,
    enhanceCodeBlocks: enhanceCodeBlocks,
  };

  IA.Markdown = ns;
  window.IssueAgent = IA;
})();
