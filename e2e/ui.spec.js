const { test, expect } = require("@playwright/test");
const fs = require("node:fs/promises");

const report = {
  summary: "修复特殊路径中的解析错误",
  root_cause: "路径未经过编码。",
  confidence: "high",
  evidence: [{ path: "src/a #1.py", lines: "L10-L12", reason: "这里会触发问题。" }],
  proposed_changes: ["规范化行号并编码路径。"],
  patch: "--- a/src/a #1.py\n+++ b/src/a #1.py\n@@ -10 +10 @@\n-old\n+new",
  tests: ["验证源码链接。"],
  risks: ["无已知风险。"],
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

test("renders real-data report charts without console errors", async ({ page }) => {
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
  await page.getByRole("button", { name: /acme\/widget #1/ }).click();
  await page.getByRole("button", { name: "查看完整报告" }).click();
  await expect(page.getByRole("complementary", { name: "分析报告" })).toBeVisible();

  // 三个图表都是懒加载：滚动进入视口后应渲染出真实 canvas
  for (const id of ["report-evidence-chart", "report-coverage-chart", "report-timeline-chart"]) {
    const el = page.locator(`#${id}`);
    await el.scrollIntoViewIfNeeded();
    await expect(el.locator("canvas").first(), `${id} 应渲染出 canvas`).toBeVisible({ timeout: 10_000 });
  }

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
  await page.getByRole("button", { name: /acme\/widget #1/ }).click();
  await page.getByRole("button", { name: "查看完整报告" }).click();

  await expect(page.getByRole("complementary", { name: "分析报告" })).toBeVisible();
  await expect(page.getByLabel("对话消息")).toBeVisible();
  await expect(page.getByRole("complementary", { name: "分析报告" })).toContainText("可信度高");
  await expect(page.getByRole("complementary", { name: "分析报告" })).toContainText("独立审查 · 已通过");
  await expect(page.getByRole("link", { name: "查看源码" })).toHaveAttribute(
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
  await page.getByRole("button", { name: /acme\/widget #1/ }).click();
  await page.getByRole("button", { name: /acme\/widget #2/ }).click();
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
  await page.getByRole("button", { name: /acme\/widget #1/ }).click();
  const input = page.getByRole("textbox", { name: "继续提问…" });
  await input.fill("请继续解释");
  await page.getByRole("button", { name: "发送" }).click();

  await expect(input).toHaveValue("");
  await expect(page.getByLabel("对话消息")).toContainText("已收到。");
  expect(
    await page.locator(".msg.user").evaluate((element) => getComputedStyle(element, "::before").content),
  ).toBe('"你"');
});

test("@mobile keeps history and report flows inside the viewport", async ({ page }) => {
  await mockCompletedSessions(page);
  await page.goto("/");
  await page.getByRole("button", { name: "会话历史" }).click();
  await expect(page.getByLabel("会话列表")).toBeVisible();
  await page.getByRole("button", { name: /acme\/widget #1/ }).click();
  await page.getByRole("button", { name: "查看完整报告" }).click();
  await expect(page.getByRole("complementary", { name: "分析报告" })).toBeVisible();

  const width = await page.evaluate(() => ({
    document: document.documentElement.scrollWidth,
    viewport: document.documentElement.clientWidth,
    report: document.getElementById("report-panel").getBoundingClientRect().width,
  }));
  expect(width.document).toBe(width.viewport);
  expect(width.report).toBeLessThanOrEqual(width.viewport);
});
