const { test, expect } = require("@playwright/test");
const fs = require("node:fs/promises");

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
  await expect(page.getByRole("heading", { level: 1 })).toHaveAccessibleName("Issue 溯源・自动生成修复补丁");
  const heroStep = page.locator(".hero-step").first();
  await heroStep.hover();
  await expect(heroStep).not.toHaveCSS("transform", "none");
  await expect(heroStep).toHaveAttribute("data-spotlight-active", "true");
  const heroSpotlight = await heroStep.evaluate((card) => getComputedStyle(card).getPropertyValue("--spot-o").trim());
  expect(Number(heroSpotlight)).toBeGreaterThanOrEqual(0.18);
  const heroExample = page.locator(".hero-example").first();
  await heroExample.hover();
  await expect(heroExample).toHaveAttribute("data-spotlight-active", "true");
  await expect(heroExample).not.toHaveCSS("transform", "none");
  const heroCta = page.getByRole("button", { name: "开始排查", exact: true });
  const heroCtaBox = await heroCta.boundingBox();
  await heroCta.hover({ position: { x: heroCtaBox.width - 4, y: heroCtaBox.height / 2 } });
  await expect.poll(() => heroCta.evaluate((button) => button.style.transform)).not.toBe("");
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
    riskMarker: "#bc6b00",
    riskLow: "#dafbe1",
    riskHigh: "#ffebc8",
    riskCritical: "#ffebe9",
    riskSeriesType: "heatmap",
    riskSeriesCount: 2,
    riskCriticalHover: "#ffcecb",
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
  const riskLocate = riskItem.getByRole("button", { name: "在风险矩阵中定位" });
  await expect(riskLocate).toContainText("定位");
  await expect(riskItem.getByRole("button", { name: "复制风险提示" })).toContainText("复制");
  await riskItem.locator(".report-item-primary").click();
  await expect(page.locator("#report-risk-matrix-section")).toHaveClass(/section-highlight/);
  const changeItem = page.locator(".change-item").first();
  const changeLocate = changeItem.getByRole("button", { name: "定位关联依据" });
  await expect(changeLocate).toContainText("定位");
  await expect(changeItem.getByRole("button", { name: "复制修复方案" })).toContainText("复制");
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
  const spotlight = await page.locator("#report-risk-matrix-section").evaluate((card) => ({
    active: card.dataset.spotlightActive,
    opacity: getComputedStyle(card).getPropertyValue("--spot-o").trim(),
    borderLayer: getComputedStyle(card, "::after").backgroundImage,
    borderLayerZIndex: getComputedStyle(card, "::after").zIndex,
  }));
  expect(spotlight.active).toBe("true");
  expect(Number(spotlight.opacity)).toBeGreaterThan(0.2);
  expect(Number(spotlight.opacity)).toBeLessThan(0.35);
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
  expect(hoveredCell).toEqual({ seriesCount: 2, hoverColor: "#ffcecb" });
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
    evidenceRoot: "#58a6ff",
    riskMarker: "#f0883e",
    riskLow: "#1f4d32",
    riskCritical: "#642b37",
    riskCriticalHover: "#873e4b",
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
  const evidenceModalRoam = await page.evaluate(() => {
    const target = document.getElementById("chart-modal-canvas");
    return window.echarts.getInstanceByDom(target).getOption().series[0].roam;
  });
  expect(evidenceModalRoam).toBe(true);
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
});
