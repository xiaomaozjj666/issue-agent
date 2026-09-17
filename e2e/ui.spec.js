const { test, expect } = require("@playwright/test");
const fs = require("node:fs/promises");
const path = require("node:path");

// axe-core 的可及性审计（devDependency）：解析包根目录后取 axe.min.js，
// 这样无论包的 exports 字段如何限制子路径都能拿到文件。
const AXE_PATH = path.join(path.dirname(require.resolve("axe-core/package.json")), "axe.min.js");

const report = {
  summary: "修复特殊路径中的解析错误",
  root_cause: "路径未经过编码。",
  confidence: "high",
  evidence: [
    { path: "src/a #1.py", lines: "L10-L12", reason: "这里会触发问题。", strength: "strong", kind: "code" },
    { path: "src/a #1.py", lines: "L20-L24", reason: "调用链将错误值传到这里。", strength: "moderate", kind: "code" },
    { path: "tests/test_a.py", lines: "L5-L18", reason: "回归用例可以稳定复现。", strength: "strong", kind: "test" },
  ],
  proposed_changes: ["规范化行号并编码路径。"],
  patch: "--- a/src/a #1.py\n+++ b/src/a #1.py\n@@ -10 +10 @@\n-old\n+new",
  tests: ["验证源码链接。"],
  risks: ["无已知风险。"],
  impact: { severity: "high", likelihood: "medium", blast_radius: ["src/a #1.py"] },
  review_audit: { status: "approved", summary: "证据充分。", findings: [] },
};

function summary(id, title, daysAgo = 4) {
  return {
    session_id: id,
    issue_url: `https://github.com/acme/widget/issues/${id === "session-1" ? 1 : 2}`,
    owner: "acme",
    repo: "widget",
    issue_number: id === "session-1" ? 1 : 2,
    title,
    status: "completed",
    phase: "done",
    error_message: null,
    archived: false,
    version: 1,
    metrics: {},
    created_at: new Date(Date.now() - daysAgo * 86_400_000).toISOString(),
    updated_at: new Date(Date.now() - daysAgo * 86_400_000).toISOString(),
  };
}

function detail(id, title) {
  return {
    ...summary(id, title),
    messages: [],
    events: [],
    report,
  };
}

async function mockCompletedSessions(page, sessions = [summary("session-1", "路径解析失败")]) {
  await page.route("**/sessions?**", (route) => route.fulfill({ json: sessions }));
  await page.route(/\/session\/(session-1|session-2)$/, (route) => {
    const id = route.request().url().endsWith("session-2") ? "session-2" : "session-1";
    const session = sessions.find((item) => item.session_id === id);
    return route.fulfill({ json: detail(id, session ? session.title : "会话") });
  });
}

function historyCard(page, issueNumber) {
  return page.locator(".session-card", { hasText: `acme/widget #${issueNumber}` });
}

test("renders responsive decision charts without overlaps or console errors", async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem("ds-theme", "light"));
  const optionalVendorRequests = [];
  page.on("request", (request) => {
    if (/\/static\/vendor\/(echarts|highlight)\.min\.js/.test(request.url())) {
      optionalVendorRequests.push(request.url());
    }
  });
  // 带完整调查数据的会话：已读文件 + 阶段事件时间戳，驱动覆盖图与阶段耗时图
  const base = Date.parse("2026-07-20T10:00:00Z");
  const at = (sec) => new Date(base + sec * 1000).toISOString();
  const richDetail = {
    ...detail("session-1", "路径解析失败"),
    files_read: ["src/a #1.py", "src/util.py"],
    metrics: { model_calls: 4, tool_calls: 6, files_read: 2 },
    events: [
      { type: "phase", data: { phase: "fetching" }, created_at: at(0) },
      { type: "phase", data: { phase: "exploring" }, created_at: at(3) },
      { type: "tool_call", data: { name: "read_file" }, created_at: at(5) },
      { type: "phase", data: { phase: "reviewing" }, created_at: at(12) },
      { type: "done", data: {}, created_at: at(15) },
    ],
  };
  await page.route("**/sessions?**", (route) => route.fulfill({ json: [summary("session-1", "路径解析失败")] }));
  await page.route(/\/session\/session-1$/, (route) => route.fulfill({ json: richDetail }));
  const consoleErrors = [];
  page.on("pageerror", (error) => consoleErrors.push(String(error)));
  page.on("console", (msg) => {
    if (msg.type() === "error") consoleErrors.push(msg.text());
  });

  await page.goto("/");
  expect(optionalVendorRequests, "首页不应提前加载图表或代码高亮库").toEqual([]);
  // 每屏唯一的 h1 是会话标题（详情视图里 hero 会被移除，而 axe 的
  // page-has-heading-one 要求那时仍有可见 h1）；hero 标语作为有名称的 h2。
  await expect(page.getByRole("heading", { level: 2, name: "Issue 溯源・自动生成修复补丁" })).toBeVisible();
  await expect(page.getByRole("heading", { level: 1 })).toHaveCount(1);
  // 去 AI 味（2026-08）：hero 不再挂载聚光/视差/磁吸等 JS 装饰动效——
  // 悬停时无 spotlight 状态、无 --spot-o 变量、CTA 无 inline transform
  //（保留 CSS 的克制 hover 上浮反馈）。
  const heroStep = page.locator(".hero-step").first();
  await heroStep.hover();
  expect(await heroStep.getAttribute("data-spotlight-active")).toBeNull();
  // --spot-o 的 CSS 默认值为 0（聚光关闭态）；开启时 JS 会设为 >0
  const stepSpot = await heroStep.evaluate((card) => getComputedStyle(card).getPropertyValue("--spot-o").trim());
  expect(Number(stepSpot || 0)).toBe(0);
  const heroExample = page.locator(".hero-example").first();
  await heroExample.hover();
  expect(await heroExample.getAttribute("data-spotlight-active")).toBeNull();
  const exampleSpot = await heroExample.evaluate((card) => getComputedStyle(card).getPropertyValue("--spot-o").trim());
  expect(Number(exampleSpot || 0)).toBe(0);
  const heroCta = page.getByRole("button", { name: "开始排查", exact: true });
  await heroCta.hover();
  expect(await heroCta.evaluate((button) => button.style.transform)).toBe("");
  await historyCard(page, 1).click();
  await page.getByRole("button", { name: "查看完整报告" }).click();
  await expect(page.getByRole("complementary", { name: "分析报告" })).toBeVisible();

  await expect(page.locator("#report-evidence-map-section .report-chart-title")).toHaveText("根因证据链");
  await expect(page.locator("#report-risk-matrix-section .report-chart-title")).toHaveText("风险矩阵");
  await expect(page.getByRole("button", { name: "在报告中搜索…" })).toBeVisible();

  const chartIds = [
    "report-evidence-map-chart",
    "report-risk-matrix-chart",
    "report-blast-radius-chart",
    "report-diffstat-chart",
    "report-verify-chart",
  ];
  // 所有图表都是懒加载：滚动进入视口后应渲染出有效像素。
  for (const id of chartIds) {
    const el = page.locator(`#${id}`);
    await el.scrollIntoViewIfNeeded();
    await expect(el.locator("canvas").first(), `${id} 应渲染出 canvas`).toBeVisible({ timeout: 10_000 });
    const paintedPixels = await el.locator("canvas").first().evaluate((canvas) => {
      const context = canvas.getContext("2d");
      const pixels = context.getImageData(0, 0, canvas.width, canvas.height).data;
      let count = 0;
      for (let index = 3; index < pixels.length; index += 4) {
        if (pixels[index] > 0) count += 1;
      }
      return count;
    });
    expect(paintedPixels, `${id} 不应是空白画布`).toBeGreaterThan(100);
    await expect(el.locator(".chart-zoom-btn"), "放大按钮不应覆盖 ECharts 工具栏").toHaveCount(0);
    const zoomButton = el.locator("xpath=..").locator(`.chart-zoom-btn[data-chart-id="${id}"]`);
    await expect(zoomButton).toHaveCount(1);
    const actionBar = el.locator("xpath=..").locator(".chart-action-bar");
    await expect(actionBar).toHaveAttribute("role", "toolbar");
    await expect(actionBar.getByRole("button")).toHaveCount(4);
    await expect(actionBar.getByRole("button", { name: "保存为图片" })).toBeEnabled();
    await expect(actionBar.getByRole("button", { name: "还原" })).toBeEnabled();
    await expect(actionBar.getByRole("button", { name: "查看数据" })).toBeEnabled();
    const zoomSize = await zoomButton.evaluate((button) => button.getBoundingClientRect().width);
    expect(zoomSize).toBeGreaterThanOrEqual(32);
    await expect(el).toHaveAttribute("aria-describedby", `${id}-data`);
    await expect(page.locator(`#${id}-data`)).toHaveCount(1);
  }
  expect(optionalVendorRequests.filter((url) => url.includes("echarts.min.js"))).toHaveLength(1);
  expect(optionalVendorRequests.filter((url) => url.includes("highlight.min.js"))).toHaveLength(0);

  const evidenceCard = page.locator("#report-evidence-map-section");
  const chartDownload = page.waitForEvent("download");
  await evidenceCard.getByRole("button", { name: "保存为图片" }).click();
  await expect((await chartDownload).suggestedFilename()).toMatch(/\.png$/);
  const restoreButton = evidenceCard.getByRole("button", { name: "还原" });
  await restoreButton.click();
  await expect(restoreButton).toHaveClass(/chart-action-complete/);
  const dataButton = evidenceCard.getByRole("button", { name: "查看数据" });
  await dataButton.click();
  await expect(dataButton).toHaveAttribute("aria-expanded", "true");
  await expect(evidenceCard.locator(".chart-data-panel")).toBeVisible();
  await expect(evidenceCard.locator(".chart-data-table")).toContainText("src/a #1.py");
  await evidenceCard.locator(".chart-data-close").click();
  await expect(evidenceCard.locator(".chart-data-panel")).toBeHidden();
  await expect(dataButton).toBeFocused();

  const minorOpacity = await page.locator("#report-diffstat-section").evaluate((card) => getComputedStyle(card).opacity);
  expect(minorOpacity).toBe("1");
  const sidebarEvidenceLayout = await page.evaluate(() => {
    const chart = window.echarts.getInstanceByDom(document.getElementById("report-evidence-map-chart"));
    const series = chart.getOption().series[0];
    return { orient: series.orient, childName: series.data[0].children[0].name };
  });
  expect(sidebarEvidenceLayout).toEqual({ orient: "TB", childName: "a #1.py" });
  const lightChartColors = await page.evaluate(() => {
    const evidence = window.echarts.getInstanceByDom(document.getElementById("report-evidence-map-chart"));
    const risk = window.echarts.getInstanceByDom(document.getElementById("report-risk-matrix-chart"));
    return {
      evidenceRoot: evidence.getOption().series[0].data[0].itemStyle.color,
      riskMarker: risk.getOption().series[1].itemStyle.color,
      riskLow: risk.getOption().series[0].data[0].itemStyle.color,
      riskHigh: risk.getOption().series[0].data[7].itemStyle.color,
      riskCritical: risk.getOption().series[0].data[11].itemStyle.color,
      riskSeriesType: risk.getOption().series[0].type,
      riskSeriesCount: risk.getOption().series.length,
      riskCriticalHover: risk.getOption().series[0].data[11].emphasis.itemStyle.color,
    };
  });
  expect(lightChartColors).toEqual({
    evidenceRoot: "#0969da",
    riskMarker: "#8250df",
    riskLow: "#dafbe1",
    riskHigh: "#fbd3ab",
    riskCritical: "#ffc4c0",
    riskSeriesType: "heatmap",
    riskSeriesCount: 2,
    riskCriticalHover: "#ffb3ae",
  });

  // 报告状态色使用轻量底色而非高饱和实心色块。
  const semanticColors = await page.evaluate(() => {
    function colors(selector) {
      const style = getComputedStyle(document.querySelector(selector));
      return { background: style.backgroundColor, foreground: style.color };
    }
    return {
      evidence: colors('.evidence-badge[class*="evidence-strength-"]'),
      risk: colors(".risk-badge"),
      change: colors(".change-priority"),
      review: colors(".review-chip"),
    };
  });
  Object.values(semanticColors).forEach(({ background, foreground }) => {
    expect(background).not.toBe(foreground);
    expect(background).not.toBe("rgba(0, 0, 0, 0)");
  });
  const riskItem = page.locator(".risk-item").first();
  await riskItem.hover();
  await expect(riskItem).not.toHaveCSS("box-shadow", "none");
  // 去 AI 味：报告项不再挂载聚光
  expect(await riskItem.getAttribute("data-spotlight-active")).toBeNull();
  const riskLocate = riskItem.getByRole("button", { name: "在风险矩阵中定位" });
  await expect(riskLocate).toBeVisible();
  await expect(riskItem.getByRole("button", { name: "复制风险提示" })).toBeVisible();
  await riskLocate.hover();
  await expect.poll(() => riskLocate.evaluate((button) => getComputedStyle(button, "::after").opacity)).toBe("1");
  const riskActionFeedback = await riskLocate.evaluate((button) => ({
    tooltip: getComputedStyle(button, "::after").content,
    tooltipOpacity: getComputedStyle(button, "::after").opacity,
    magnet: button.style.transform,
  }));
  expect(riskActionFeedback.tooltip).toContain("在风险矩阵中定位");
  expect(riskActionFeedback.tooltipOpacity).toBe("1");
  // 去 AI 味：磁吸已关闭，按钮无 inline transform
  expect(riskActionFeedback.magnet).toBe("");
  await riskItem.locator(".report-item-primary").click();
  await expect(page.locator("#report-risk-matrix-section")).toHaveClass(/section-highlight/);
  const changeItem = page.locator(".change-item").first();
  const changeLocate = changeItem.getByRole("button", { name: "定位关联依据" });
  await expect(changeLocate).toBeVisible();
  await expect(changeItem.getByRole("button", { name: "复制修复方案" })).toBeVisible();
  await changeItem.locator(".report-item-primary").click();
  await expect(page.locator("#report-patch")).toHaveClass(/section-highlight/);
  await page.evaluate(() => { window.IssueAgent.copyToClipboard = async () => true; });
  await changeItem.getByRole("button", { name: "复制修复方案" }).click();
  await expect(page.locator("#toast")).toHaveText("已复制");
  const riskDot = await riskItem.locator(".risk-badge").evaluate((badge) => ({
    width: getComputedStyle(badge, "::before").width,
    color: getComputedStyle(badge, "::before").backgroundColor,
  }));
  expect(riskDot.width).toBe("6px");
  expect(riskDot.color).not.toBe("rgba(0, 0, 0, 0)");
  await page.locator("#report-risk-matrix-section").hover();
  const hoverTransform = await page.locator("#report-risk-matrix-section").evaluate((card) => getComputedStyle(card).transform);
  expect(hoverTransform).toBe("none");
  // 去 AI 味：图表卡片不再挂载聚光（无 data-spotlight-active、--spot-o 为默认 0）
  const spotlight = await page.locator("#report-risk-matrix-section").evaluate((card) => ({
    active: card.dataset.spotlightActive,
    opacity: getComputedStyle(card).getPropertyValue("--spot-o").trim(),
    borderLayer: getComputedStyle(card, "::after").backgroundImage,
    borderLayerZIndex: getComputedStyle(card, "::after").zIndex,
  }));
  expect(spotlight.active).toBeUndefined();
  expect(Number(spotlight.opacity || 0)).toBe(0);
  // ::after 的 radial-gradient 是 CSS 静态边框层（透明度 0，JS 聚光不激活时不可见）
  expect(spotlight.borderLayer).toContain("radial-gradient");
  expect(spotlight.borderLayerZIndex).not.toBe("-1");

  const staticReportCards = await page.evaluate(() => ({
    conclusionSpotlight: document.querySelector(".report-conclusion").dataset.spotlightActive || "",
    metricSpotlight: document.querySelector(".report-metric-card").dataset.spotlightActive || "",
    conclusionInlineTransform: document.querySelector(".report-conclusion").style.transform,
    metricInlineTransform: document.querySelector(".report-metric-card").style.transform,
  }));
  expect(staticReportCards).toEqual({
    conclusionSpotlight: "",
    metricSpotlight: "",
    conclusionInlineTransform: "",
    metricInlineTransform: "",
  });

  // 图表画布保留 ECharts tooltip，点击反馈不再叠加装饰性火花。
  const criticalCellPoint = await page.evaluate(() => {
    const target = document.getElementById("report-risk-matrix-chart");
    return window.echarts.getInstanceByDom(target).convertToPixel({ xAxisIndex: 0, yAxisIndex: 0 }, [2, 3]);
  });
  const riskCanvas = page.locator("#report-risk-matrix-chart canvas").first();
  await riskCanvas.scrollIntoViewIfNeeded();
  const riskCanvasBox = await riskCanvas.boundingBox();
  await page.mouse.move(riskCanvasBox.x + criticalCellPoint[0], riskCanvasBox.y + criticalCellPoint[1]);
  const criticalTooltip = page.locator(".ia-chart-tooltip", { hasText: "严重度 严重" });
  await expect(criticalTooltip).toBeVisible();
  await expect(criticalTooltip).toContainText("严重度 严重");
  await expect(criticalTooltip).toContainText("发生可能性 高");
  const tooltipBox = await criticalTooltip.boundingBox();
  const criticalCellBox = await page.evaluate(() => {
    const target = document.getElementById("report-risk-matrix-chart");
    const chart = window.echarts.getInstanceByDom(target);
    const rect = target.getBoundingClientRect();
    const center = chart.convertToPixel({ xAxisIndex: 0, yAxisIndex: 0 }, [2, 3]);
    const previousX = chart.convertToPixel({ xAxisIndex: 0, yAxisIndex: 0 }, [1, 3]);
    const previousY = chart.convertToPixel({ xAxisIndex: 0, yAxisIndex: 0 }, [2, 2]);
    const width = Math.abs(center[0] - previousX[0]) - 8;
    const height = Math.abs(center[1] - previousY[1]) - 8;
    return {
      x: rect.x + center[0] - width / 2,
      y: rect.y + center[1] - height / 2,
      width,
      height,
    };
  });
  const overlapsHoveredCell = !(
    tooltipBox.x + tooltipBox.width <= criticalCellBox.x
    || tooltipBox.x >= criticalCellBox.x + criticalCellBox.width
    || tooltipBox.y + tooltipBox.height <= criticalCellBox.y
    || tooltipBox.y >= criticalCellBox.y + criticalCellBox.height
  );
  expect(overlapsHoveredCell, JSON.stringify({ tooltipBox, criticalCellBox })).toBe(false);
  const hoveredCell = await page.evaluate(() => {
    const chart = window.echarts.getInstanceByDom(document.getElementById("report-risk-matrix-chart"));
    return {
      seriesCount: chart.getOption().series.length,
      hoverColor: chart.getOption().series[0].data[11].emphasis.itemStyle.color,
    };
  });
  expect(hoveredCell).toEqual({ seriesCount: 2, hoverColor: "#ffb3ae" });
  await riskCanvas.click({ position: { x: 24, y: 24 } });
  await expect(page.locator(".motion-spark-burst--chart")).toHaveCount(0);
  await expect(page.locator(".motion-ripple")).toHaveCount(0);

  // 普通报告侧栏宽度不足时单列排布。
  const sidePanelTops = await page.evaluate(() => ({
    risk: document.getElementById("report-risk-matrix-section").getBoundingClientRect().top,
    blast: document.getElementById("report-blast-radius-section").getBoundingClientRect().top,
  }));
  expect(sidePanelTops.blast).toBeGreaterThan(sidePanelTops.risk + 100);

  // 全屏报告有足够宽度时恢复双列，两张次主图顶部对齐。
  await page.getByRole("button", { name: "全屏", exact: true }).click();
  await expect(page.locator("#report-theme-btn")).toBeVisible();
  await page.waitForTimeout(150);
  const fullscreenTops = await page.evaluate(() => ({
    risk: document.getElementById("report-risk-matrix-section").getBoundingClientRect().top,
    blast: document.getElementById("report-blast-radius-section").getBoundingClientRect().top,
  }));
  expect(Math.abs(fullscreenTops.blast - fullscreenTops.risk)).toBeLessThan(2);
  await expect.poll(async () => page.evaluate(() => {
    const chart = window.echarts.getInstanceByDom(document.getElementById("report-evidence-map-chart"));
    return chart.getOption().series[0].orient;
  })).toBe("TB");
  const fullscreenEvidenceHeight = await page.locator("#report-evidence-map-chart").evaluate((el) => el.clientHeight);
  expect(fullscreenEvidenceHeight).toBeLessThanOrEqual(380);

  // 放大交互应能创建有内容的模态图表，Escape 可正常关闭。
  const riskZoom = page.locator('.chart-zoom-btn[data-chart-id="report-risk-matrix-chart"]');
  await riskZoom.click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await expect(page.getByRole("dialog")).toHaveAccessibleName("风险矩阵");
  await expect(page.locator("#chart-modal-canvas canvas").first()).toBeVisible();
  await expect(page.locator(".chart-modal-close")).toBeFocused();
  // 模态框打开时背景按设计不可点；脚本触发等价于系统主题变化，验证两层图表同步刷新。
  await page.evaluate(() => document.getElementById("theme-toggle-btn").click());
  await expect.poll(async () => page.evaluate(() => {
    const mainEvidence = window.echarts.getInstanceByDom(document.getElementById("report-evidence-map-chart"));
    const modalRisk = window.echarts.getInstanceByDom(document.getElementById("chart-modal-canvas"));
    return {
      evidenceRoot: mainEvidence.getOption().series[0].data[0].itemStyle.color,
      riskMarker: modalRisk.getOption().series[1].itemStyle.color,
      riskLow: modalRisk.getOption().series[0].data[0].itemStyle.color,
      riskCritical: modalRisk.getOption().series[0].data[11].itemStyle.color,
      riskCriticalHover: modalRisk.getOption().series[0].data[11].emphasis.itemStyle.color,
    };
  })).toEqual({
    // 证据链根节点使用强调深蓝（strengthStrong），不再是普通 primary 蓝
    evidenceRoot: "#2f81f7",
    riskMarker: "#bc8cff",
    riskLow: "#173525",
    riskCritical: "#8e2a48",
    riskCriticalHover: "#a83a58",
  });
  await page.keyboard.press("Tab");
  await expect(page.locator(".chart-modal-close")).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).toBeHidden();
  await expect(riskZoom).toBeFocused();

  // 证据树在卡片中保持稳定，在放大模式下开放拖拽和滚轮缩放。
  const evidenceZoom = page.locator('.chart-zoom-btn[data-chart-id="report-evidence-map-chart"]');
  await evidenceZoom.click();
  await expect(page.getByRole("dialog")).toHaveAccessibleName("根因证据链");
  // 模态图的 ECharts 实例在对话框出现后才异步创建：同文件其他图表读取处均用
  // expect.poll 等待实例就绪，这里保持同样口径，杜绝偶发"undefined.getOption"。
  await expect.poll(async () => page.evaluate(() => {
    const target = document.getElementById("chart-modal-canvas");
    return window.echarts.getInstanceByDom(target)?.getOption().series[0].roam;
  })).toBe(true);
  await page.keyboard.press("Escape");

  // CDN 资源加载失败不算应用错误（离线环境降级路径另有兼容）
  const appErrors = consoleErrors.filter((text) => !/net::|Failed to load resource/i.test(text));
  expect(appErrors).toEqual([]);
});

test("localizes interface chrome, relative time, and untrusted session text", async ({ page }) => {
  await mockCompletedSessions(page, [summary("session-1", '<img src=x onerror="window.__xss=1">')]);
  await page.goto("/");

  await expect(page.getByRole("button", { name: "会话历史" })).toBeVisible();
  await expect(page.getByRole("button", { name: "切换主题" })).toBeVisible();
  await expect(page.getByRole("searchbox", { name: "搜索会话" })).toBeVisible();
  await expect(page.getByLabel("会话列表")).toContainText("4天前");
  await expect(page.locator("#history-list img")).toHaveCount(0);
  await expect(page.locator("#history-list")).toContainText('<img src=x onerror="window.__xss=1">');
  expect(await page.evaluate(() => window.__xss || 0)).toBe(0);
});

test("opens reports without hiding the conversation and builds valid GitHub links", async ({ page }) => {
  await mockCompletedSessions(page);
  await page.goto("/");
  await historyCard(page, 1).click();
  await page.getByRole("button", { name: "查看完整报告" }).click();

  await expect(page.getByRole("complementary", { name: "分析报告" })).toBeVisible();
  await expect(page.locator(".report-toc")).not.toHaveAttribute("open", "");
  await expect(page.locator("#report-toggle")).toHaveAttribute("aria-expanded", "true");
  await expect(page.locator('#report-toggle > span[data-i18n]')).toHaveText("关闭");
  await expect(page.getByLabel("对话消息")).toBeVisible();
  await expect(page.getByRole("complementary", { name: "分析报告" })).toContainText("可信度高");
  await expect(page.getByRole("complementary", { name: "分析报告" })).toContainText("独立审查 · 已通过");
  await expect(page.locator(".confidence-badge")).toHaveCSS("-webkit-text-fill-color", /rgb/);
  await expect(page.getByRole("link", { name: "查看源码" }).first()).toHaveAttribute(
    "href",
    "https://github.com/acme/widget/blob/HEAD/src/a%20%231.py#L10-L12",
  );
  const layout = await page.evaluate(() => ({
    documentWidth: document.documentElement.scrollWidth,
    viewportWidth: document.documentElement.clientWidth,
    conversationWidth: document.getElementById("conversation").getBoundingClientRect().width,
  }));
  expect(layout.documentWidth).toBe(layout.viewportWidth);
  expect(layout.conversationWidth).toBeGreaterThan(500);

  // 中等桌面宽度下隐藏侧栏并为报告预留空间，报告不再覆盖对话。
  await page.setViewportSize({ width: 1040, height: 800 });
  const compactDesktop = await page.evaluate(() => {
    const conversation = document.getElementById("conversation").getBoundingClientRect();
    const reportPanel = document.getElementById("report-panel").getBoundingClientRect();
    return {
      sidebarDisplay: getComputedStyle(document.getElementById("sidebar")).display,
      conversationRight: conversation.right,
      conversationWidth: conversation.width,
      reportLeft: reportPanel.left,
    };
  });
  expect(compactDesktop.sidebarDisplay).toBe("none");
  expect(compactDesktop.conversationWidth).toBeGreaterThan(360);
  expect(compactDesktop.conversationRight).toBeLessThanOrEqual(compactDesktop.reportLeft + 1);

  // 对话区的报告按钮同时承担关闭操作，关闭后历史侧栏应恢复。
  await page.locator("#report-toggle").click();
  await expect(page.getByRole("complementary", { name: "分析报告" })).toBeHidden();
  await expect(page.locator("#sidebar")).toBeVisible();
  await expect(page.locator('#report-toggle > span[data-i18n]')).toHaveText("查看报告");
  await page.locator("#report-toggle").click();
  await expect(page.getByRole("complementary", { name: "分析报告" })).toBeVisible();

  const downloadPromise = page.waitForEvent("download");
  await page.getByRole("button", { name: "下载 Markdown" }).click();
  const download = await downloadPromise;
  const downloadPath = await download.path();
  expect(downloadPath).not.toBeNull();
  const markdown = await fs.readFile(downloadPath, "utf-8");
  expect(markdown).toContain("## 问题根因");
  expect(markdown).toContain("**置信度:** 高");

  await page.getByRole("button", { name: "返回上一步" }).click();
  await expect(page.getByRole("complementary", { name: "分析报告" })).toBeHidden();
  await page.getByRole("button", { name: "返回上一步" }).click();
  // 退出会话后回到 Hero 欢迎页（而非旧版的空状态提示）
  await expect(page.getByLabel("对话消息")).toContainText("Issue 溯源");
});

test("restores the back button after a failed history request", async ({ page }) => {
  const sessions = [summary("session-1", "第一条会话"), summary("session-2", "第二条会话", 2)];
  let firstSessionRequests = 0;
  await page.route("**/sessions?**", (route) => route.fulfill({ json: sessions }));
  await page.route(/\/session\/(session-1|session-2)$/, (route) => {
    if (route.request().url().endsWith("session-1")) {
      firstSessionRequests += 1;
      if (firstSessionRequests > 1) return route.fulfill({ status: 503, json: { detail: "暂时不可用" } });
      return route.fulfill({ json: detail("session-1", "第一条会话") });
    }
    return route.fulfill({ json: detail("session-2", "第二条会话") });
  });
  await page.goto("/");
  await historyCard(page, 1).click();
  await historyCard(page, 2).click();
  const back = page.getByRole("button", { name: "返回上一步" });
  await back.click();

  await expect(page.getByLabel("对话消息")).toContainText("暂时不可用");
  await expect(back).toBeEnabled();
});

test("clears follow-up input after sending", async ({ page }) => {
  await mockCompletedSessions(page);
  // 追问已迁移到 /chat/stream SSE 接口，mock 需返回事件流格式
  await page.route("**/chat/stream", (route) =>
    route.fulfill({
      contentType: "text/event-stream",
      body:
        'data: {"type":"delta","content":"已收到。"}\n\n' +
        'data: {"type":"done","reply":"已收到。","tools_used":[]}\n\n',
    }),
  );
  await page.goto("/");
  await historyCard(page, 1).click();
  const input = page.getByRole("textbox", { name: "继续提问…" });
  await input.fill("请继续解释");
  await page.getByRole("button", { name: "发送" }).click();

  await expect(input).toHaveValue("");
  await expect(page.getByLabel("对话消息")).toContainText("已收到。");
  expect(
    await page.locator(".msg.user").evaluate((element) => getComputedStyle(element, "::before").content),
  ).toBe('"你"');
});

test("keeps large investigation histories collapsed and report actions stable", async ({ page }) => {
  const manyEvents = Array.from({ length: 585 }, (_, index) => ({
    type: "tool_call",
    data: { name: `read_file_${index}` },
    created_at: new Date(Date.now() + index * 1000).toISOString(),
  }));
  await page.route("**/sessions?**", (route) => route.fulfill({ json: [summary("session-1", "大型调查记录")] }));
  await page.route(/\/session\/session-1$/, (route) => route.fulfill({
    json: { ...detail("session-1", "大型调查记录"), events: manyEvents },
  }));

  await page.goto("/");
  await historyCard(page, 1).click();
  await expect(page.locator(".timeline-step")).toHaveCount(8);
  await page.getByRole("button", { name: "展开全部 (577)" }).click();
  await expect(page.locator(".timeline-step")).toHaveCount(585);
  await page.getByRole("button", { name: "收起" }).click();
  await expect(page.locator(".timeline-step")).toHaveCount(8);

  await page.getByRole("button", { name: "查看完整报告" }).click();
  await expect(page.getByRole("complementary", { name: "分析报告" })).toBeVisible();
});

test("explains historical report capabilities and offers an explicit reanalysis path", async ({ page }) => {
  const historicalReport = {
    ...report,
    patch: "",
    reproduction: null,
    review_audit: { status: "not_run", summary: "", findings: [] },
  };
  const historicalDetail = {
    ...detail("session-1", "旧报告"),
    events: [],
    report: historicalReport,
  };
  let reanalysisPayload = null;
  await page.route("**/sessions?**", (route) => route.fulfill({ json: [summary("session-1", "旧报告")] }));
  await page.route(/\/session\/session-1$/, (route) => route.fulfill({ json: historicalDetail }));
  await page.route("**/stream", async (route) => {
    reanalysisPayload = route.request().postDataJSON();
    await route.fulfill({
      contentType: "text/event-stream",
      body: 'data: {"type":"cancelled"}\n\ndata: {"type":"done"}\n\n',
    });
  });

  await page.goto("/");
  await historyCard(page, 1).click();
  await expect(page.locator(".historical-summary")).toContainText("历史报告摘要");
  await expect(page.locator(".historical-summary")).toContainText("不会补写不存在的过程");

  await page.getByRole("button", { name: "查看完整报告" }).click();
  const capabilities = page.locator(".report-capabilities");
  await expect(capabilities).toContainText("1/5 可用");
  await capabilities.locator("summary").click();
  await expect(capabilities.locator('[data-capability="evidence"]')).toContainText("可用");
  await expect(capabilities.locator('[data-capability="patch"]')).toContainText("本次调查只给出诊断");
  await expect(capabilities.locator('[data-capability="timeline"]')).toContainText("没有可回放的逐步事件");

  const historicalChart = page.locator("#report-evidence-map-chart");
  await historicalChart.scrollIntoViewIfNeeded();
  await expect(historicalChart.locator("canvas").first()).toBeVisible();
  const historicalActions = historicalChart.locator("xpath=..").locator(".chart-action-bar");
  await expect(historicalActions.getByRole("button")).toHaveCount(4);
  await expect(historicalActions.getByRole("button", { name: "查看数据" })).toBeEnabled();
  await expect(page.locator(".change-item").first().getByRole("button", { name: "定位关联依据" })).toBeVisible();
  await expect(page.locator(".risk-item").first().getByRole("button", { name: "在风险矩阵中定位" })).toBeVisible();
  await expect(page.locator(".change-item .report-item-primary").first()).toHaveRole("button");
  await expect(page.locator(".risk-item .report-item-primary").first()).toHaveRole("button");

  await page.getByRole("button", { name: "用当前 Agent 重新分析" }).click();
  await expect.poll(() => reanalysisPayload).not.toBeNull();
  expect(reanalysisPayload.issue_url).toBe("https://github.com/acme/widget/issues/1");
  await expect(page.getByRole("complementary", { name: "分析报告" })).toBeHidden();
});

test("@mobile keeps history and report flows inside the viewport", async ({ page }) => {
  await mockCompletedSessions(page);
  await page.goto("/");
  await page.getByRole("button", { name: "会话历史" }).click();
  await expect(page.getByLabel("会话列表")).toBeVisible();
  await historyCard(page, 1).click();
  await page.getByRole("button", { name: "查看完整报告" }).click();
  await expect(page.getByRole("complementary", { name: "分析报告" })).toBeVisible();

  const width = await page.evaluate(() => ({
    document: document.documentElement.scrollWidth,
    viewport: document.documentElement.clientWidth,
    report: document.getElementById("report-panel").getBoundingClientRect().width,
    reportBody: document.getElementById("report").getBoundingClientRect().width,
    reportBodyClient: document.getElementById("report").clientWidth,
    reportBodyScroll: document.getElementById("report").scrollWidth,
    evidenceCard: document.getElementById("report-evidence-map-section").getBoundingClientRect().width,
  }));
  expect(width.document).toBe(width.viewport);
  expect(width.report).toBeLessThanOrEqual(width.viewport);
  expect(width.reportBody).toBeLessThanOrEqual(width.viewport);
  expect(width.reportBodyScroll).toBe(width.reportBodyClient);
  expect(width.evidenceCard).toBeLessThanOrEqual(width.viewport);

  // 触屏最小可点区域：可见的图标按钮必须 ≥ 44×44（此前是 34×34，手机上难以稳定点中）
  const undersized = await page.evaluate(() =>
    Array.from(document.querySelectorAll(".brand-actions .icon-button, #back-button, .mobile-history-toggle"))
      .map((el) => {
        const rect = el.getBoundingClientRect();
        return { id: el.id || String(el.className), w: Math.round(rect.width), h: Math.round(rect.height) };
      })
      .filter((item) => item.w > 0 && item.h > 0 && (item.w < 44 || item.h < 44)),
  );
  expect(undersized).toEqual([]);
});

test("sends the stored API key with every API request when present", async ({ page }) => {
  // API_KEY 认证：设置面板保存的密钥应作为 X-API-Key 头随请求发送。
  const seenKeys = [];
  await page.route("**/sessions?**", (route) => {
    seenKeys.push(route.request().headers()["x-api-key"] || null);
    return route.fulfill({ json: [] });
  });
  // 只在首次加载时写入密钥；reload 后不再重设（sessionStorage 在同标签页跨导航保留）
  await page.addInitScript(() => {
    if (!sessionStorage.getItem("ia-api-key-seeded")) {
      localStorage.setItem("iaApiKey", "test-key-123");
      sessionStorage.setItem("ia-api-key-seeded", "1");
    }
  });
  await page.goto("/");
  await expect(page.getByLabel("会话列表")).toBeVisible();
  expect(seenKeys).toContain("test-key-123");

  // 清除密钥后不再发送该头
  await page.evaluate(() => localStorage.removeItem("iaApiKey"));
  await page.reload();
  await expect(page.getByLabel("会话列表")).toBeVisible();
  expect(seenKeys[seenKeys.length - 1]).toBeNull();
});

test("updates the document title to the active session and restores it on back", async ({ page }) => {
  await mockCompletedSessions(page);
  await page.goto("/");
  await expect(page).toHaveTitle("GitHub Issue Agent");
  await historyCard(page, 1).click();
  await expect(page).toHaveTitle(/路径解析失败 · GitHub Issue Agent/);
  // 返回首页后标题还原
  await page.locator("#back-button").click();
  await expect(page).toHaveTitle("GitHub Issue Agent");
});

test("shows a friendly hint when the API requires a key", async ({ page }) => {
  // 服务器要求 API_KEY 但请求未携带：应显示引导提示而非原始错误
  await page.route("**/sessions?**", (route) =>
    route.fulfill({ status: 401, json: { detail: "Missing X-API-Key header" } })
  );
  await page.goto("/");
  await expect(page.getByText(/需要 API 密钥/)).toBeVisible();
  // 设置按钮出现脉冲高亮
  await expect(page.locator("#settings-btn.settings-attention")).toBeVisible();
  await page.waitForTimeout(5200);
  await expect(page.locator("#settings-btn.settings-attention")).toHaveCount(0);
});

test("shows inline URL validation error next to the input, not in the thread", async ({ page }) => {
  await mockCompletedSessions(page);
  await page.goto("/");
  await expect(page.locator(".hero")).toBeVisible();
  await page.fill("#issueUrl", "not a valid url");
  await page.locator("#analyze-btn").click();
  // 错误出现在侧栏输入框正下方（用户视线所在处），而不是主区消息流底部
  const inlineError = page.locator("#url-error");
  await expect(inlineError).toBeVisible();
  await expect(inlineError).toContainText("请输入合法的 GitHub issue 链接");
  await expect(page.locator("#messages .msg.error")).toHaveCount(0);
  // 数秒后自动消失
  await expect(inlineError).toBeHidden({ timeout: 6000 });
});

test("distinguishes empty search results from the first-run empty state", async ({ page }) => {
  // mock 按 q 参数过滤：搜索无结果时前端应展示"没有匹配"文案，而非首次空态引导
  await page.route("**/sessions?**", (route) => {
    const q = new URL(route.request().url()).searchParams.get("q") || "";
    const sessions = q.includes("zzz") ? [] : [summary("session-1", "路径解析失败")];
    route.fulfill({ json: sessions });
  });
  await page.route(/\/session\/session-1$/, (route) => route.fulfill({ json: detail("session-1", "路径解析失败") }));
  await page.goto("/");
  await expect(historyCard(page, 1)).toBeVisible();

  await page.locator("#history-search").fill("zzz-no-match");
  const list = page.locator("#history-list");
  await expect(list).toContainText("没有匹配的会话");
  await expect(list).not.toContainText("粘贴一个 Issue 链接");

  // 一键清除：输入框清空并重新拉取会话列表
  await page.locator("#empty-clear-search").click();
  await expect(page.locator("#history-search")).toHaveValue("");
  await expect(historyCard(page, 1)).toBeVisible();
});

test("creates a pull request from the report only after an explicit preview confirm", async ({ page }) => {
  await mockCompletedSessions(page);
  const posted = [];
  await page.route(/\/session\/session-1\/proposal$/, (route) =>
    route.fulfill({
      json: {
        branch: "issue-agent/fix-parser",
        title: "fix: encode paths",
        body: "Closes #1",
        write_mode: true,
        changes: [
          {
            path: "src/a #1.py",
            message: "fix: encode",
            proposed_lines: 2,
            proposed_bytes: 24,
            preview: "-  return raw;\n+  return encode(raw);",
          },
        ],
      },
    }),
  );
  await page.route(/\/session\/session-1\/apply-fix$/, (route) => {
    posted.push(route.request().postData());
    return route.fulfill({
      json: { pr_url: "https://github.com/acme/widget/pull/42", branch: "issue-agent/fix-parser" },
    });
  });

  await page.goto("/");
  await historyCard(page, 1).click();
  await page.getByRole("button", { name: "查看完整报告" }).click();

  // 面板由 pr-panel.js 通过 MutationObserver 注入到补丁章节之后
  const panel = page.locator(".pr-panel");
  await expect(panel).toBeVisible({ timeout: 10_000 });

  // 未确认前不得有任何写请求
  expect(posted).toHaveLength(0);

  await panel.getByRole("button", { name: "创建 PR" }).click();
  // 人工确认必须看得到「到底要写什么」：分支、文件与内容预览
  await expect(panel).toContainText("确认创建分支与 PR？");
  await expect(panel).toContainText("issue-agent/fix-parser");
  await expect(panel).toContainText("src/a #1.py");
  await expect(panel).toContainText("return encode(raw)");
  expect(posted).toHaveLength(0);

  await panel.getByRole("button", { name: "确认创建" }).click();
  await expect(panel).toContainText("已创建 PR #42");
  await expect(panel.locator('a[href="https://github.com/acme/widget/pull/42"]')).toHaveCount(1);
  expect(posted).toEqual(['{"confirm":true}']);
});

test("keeps the ticking progress timer out of the screen-reader live region", async ({ page }) => {
  await mockCompletedSessions(page);
  await page.goto("/");

  // 可见进度区不得再声明 live：它每秒都会用「阶段 · 已用时」重写自己，
  // 挂在 live region 上会让屏幕阅读器每秒播报一次（实测确认过）。
  const progress = page.locator("#progress");
  await expect(progress).not.toHaveAttribute("aria-live", "polite");
  await expect(progress).not.toHaveAttribute("role", "status");

  // 播报改由视觉隐藏的 live region 承担
  const live = page.locator("#progress-live");
  await expect(live).toHaveAttribute("aria-live", "polite");
  await expect(live).toHaveAttribute("aria-atomic", "true");
  const srOnly = await live.evaluate((el) => {
    const style = getComputedStyle(el);
    return { position: style.position, width: style.width, height: style.height };
  });
  expect(srOnly).toEqual({ position: "absolute", width: "1px", height: "1px" });

  // 重复写入同一内容不得触碰 DOM —— 每秒的计时刷新正是被这条挡住的
  const result = await page.evaluate(async () => {
    const region = document.getElementById("progress-live");
    let mutations = 0;
    const observer = new MutationObserver(() => { mutations += 1; });
    observer.observe(region, { childList: true, characterData: true, subtree: true });
    const IA = window.IssueAgent;
    IA.announceProgress("正在调查代码");
    IA.announceProgress("正在调查代码");
    IA.announceProgress("正在调查代码");
    await new Promise((resolve) => setTimeout(resolve, 50));
    observer.disconnect();
    return { mutations, text: region.textContent };
  });
  expect(result.mutations).toBe(1);
  expect(result.text).toBe("正在调查代码");
});

test("marks the transcript busy while a streamed reply is rendering", async ({ page }) => {
  await mockCompletedSessions(page);
  // 流式回答：先给一段增量，再给 done —— 期间 #messages 必须处于 aria-busy
  await page.route("**/chat/stream", (route) =>
    route.fulfill({
      status: 200,
      contentType: "text/event-stream",
      body: [
        'data: {"type": "delta", "content": "第一段"}',
        "",
        'data: {"type": "done", "reply": "第一段", "tools_used": []}',
        "",
        "",
      ].join("\n"),
    }),
  );
  await page.goto("/");
  await historyCard(page, 1).click();
  await expect(page.locator("#input-bar")).toBeVisible();

  const busyDuringStream = page.evaluate(() => {
    const transcript = document.getElementById("messages");
    return new Promise((resolve) => {
      const seen = transcript.getAttribute("aria-busy");
      const observer = new MutationObserver(() => {
        if (transcript.getAttribute("aria-busy") === "true") {
          observer.disconnect();
          resolve(true);
        }
      });
      observer.observe(transcript, { attributes: true, attributeFilter: ["aria-busy"] });
      if (seen === "true") {
        observer.disconnect();
        resolve(true);
      }
      setTimeout(() => { observer.disconnect(); resolve(false); }, 5000);
    });
  });

  await page.fill("#chatInput", "根因是什么？");
  await page.locator("#chat-send-btn").click();
  expect(await busyDuringStream).toBe(true);
  await expect(page.locator("#messages")).not.toHaveAttribute("aria-busy", "true", { timeout: 8000 });
});

test("activates report drill-down targets from the keyboard", async ({ page }) => {
  await mockCompletedSessions(page);
  await page.goto("/");
  await historyCard(page, 1).click();
  await page.getByRole("button", { name: "查看完整报告" }).click();

  // 指标卡是 role="link" + tabindex="0"：必须能用键盘激活，而不只是「可聚焦但按不动」
  const card = page.locator(".report-metric-card.clickable").first();
  await card.scrollIntoViewIfNeeded();
  await card.focus();
  await expect(card).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page.locator("#report-evidence")).toHaveClass(/section-highlight/, { timeout: 5000 });

  // 修复方案条目：聚焦主按钮后回车同样应下钻并高亮目标章节
  const change = page.locator(".change-item .report-item-primary").first();
  await change.scrollIntoViewIfNeeded();
  await change.focus();
  await expect(change).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page.locator("#report-patch")).toHaveClass(/section-highlight/, { timeout: 5000 });

  // 空格键同样应生效（原生 button 语义）
  await page.locator("#report-patch").scrollIntoViewIfNeeded();
  const changeAgain = page.locator(".change-item .report-item-primary").first();
  await changeAgain.focus();
  await page.keyboard.press(" ");
  await expect(changeAgain).toBeFocused();
});

test("passes an automated accessibility audit on the main views", async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem("ds-theme", "light"));
  // 以「减少动效」运行审计：命令面板等组件的入场动画会短暂处于 opacity:0，
  // 那会让 axe 把按钮文字判定为不可见（button-name 误报）。
  await page.emulateMedia({ reducedMotion: "reduce" });
  await mockCompletedSessions(page);
  await page.goto("/");

  async function audit(label) {
    await page.addScriptTag({ path: AXE_PATH });
    const violations = await page.evaluate(async () => {
      const result = await window.axe.run(document, { resultTypes: ["violations"] });
      return result.violations.map((item) => ({
        id: item.id,
        impact: item.impact,
        help: item.help,
        targets: item.nodes.slice(0, 3).map((node) => node.target.join(" ")),
      }));
    });
    expect(violations, `${label} 存在可及性违规：${JSON.stringify(violations, null, 2)}`).toEqual([]);
  }

  // 首页（浅色）
  await audit("首页");

  // 设置抽屉（地标/标题/表单标签都在这里）
  await page.click("#settings-btn");
  await page.waitForTimeout(400);
  await audit("设置面板");
  await page.keyboard.press("Escape");
  await page.waitForTimeout(300);

  // 命令面板（Ctrl/⌘+K）与帮助浮层：都是真实的对话框表面
  await page.keyboard.press("Control+k");
  await page.waitForTimeout(400);
  await audit("命令面板");
  await page.keyboard.press("Escape");
  await page.waitForTimeout(300);
  await page.keyboard.press("?");
  await page.waitForTimeout(400);
  await audit("帮助浮层");
  await page.keyboard.press("Escape");
  await page.waitForTimeout(300);

  // 报告面板（含图表、补丁、时间线）
  await historyCard(page, 1).click();
  await page.getByRole("button", { name: "查看完整报告" }).click();
  await expect(page.getByRole("complementary", { name: "分析报告" })).toBeVisible();
  await audit("报告面板（浅色）");

  // 深色主题下同一份报告
  await page.click("#theme-toggle-btn");
  await page.waitForTimeout(500);
  await audit("报告面板（深色）");

  // 导出的独立 HTML 报告：用户会单独打开这个文件，也必须通过同一套审计
  const exportHtml = await page.evaluate((data) => {
    const session = {
      session_id: "s1", owner: "acme", repo: "widget", issue_number: 1,
      metrics: {}, messages: [], events: [], report: data,
    };
    return window.IssueAgent.Export.selfContainedHtml(data, session);
  }, report);
  const exportPage = await page.context().newPage();
  await exportPage.route("https://cdn.jsdelivr.net/**", (route) => route.abort());  // 离线审计，不依赖 CDN
  await exportPage.setContent(exportHtml);
  await exportPage.addScriptTag({ path: AXE_PATH });
  const exportViolations = await exportPage.evaluate(async () => {
    const result = await window.axe.run(document, { resultTypes: ["violations"] });
    return result.violations.map((item) => ({
      id: item.id,
      impact: item.impact,
      help: item.help,
      targets: item.nodes.slice(0, 3).map((node) => node.target.join(" ")),
    }));
  });
  expect(exportViolations, `导出的 HTML 报告存在可及性违规：${JSON.stringify(exportViolations, null, 2)}`).toEqual([]);
  await exportPage.close();
});
