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
  await expect(page.getByRole("complementary", { name: "分析报告" })).toContainText("置信度高");
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
  expect(markdown).toContain("## 根因");
  expect(markdown).toContain("**置信度:** 高");

  await page.getByRole("button", { name: "返回上一步" }).click();
  await expect(page.getByRole("complementary", { name: "分析报告" })).toBeHidden();
  await page.getByRole("button", { name: "返回上一步" }).click();
  await expect(page.getByLabel("对话消息")).toContainText("选择一个历史会话");
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
  await page.route("**/chat", (route) =>
    route.fulfill({ json: { reply: "已收到。", tools_used: [], report: null } }),
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
