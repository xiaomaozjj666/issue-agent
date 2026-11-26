const { test, expect } = require("@playwright/test");
const path = require("node:path");

// axe-core 可及性审计（与 ui.spec.js 同口径）
const AXE_PATH = path.join(path.dirname(require.resolve("axe-core/package.json")), "axe.min.js");

const ISSUE_1 = "https://github.com/acme/widget/issues/1";
const ISSUE_2 = "https://github.com/acme/widget/issues/2";

function task(id, url, status, extra = {}) {
  return {
    task_id: id,
    issue_url: url,
    status,
    error: null,
    has_report: status === "completed",
    ...extra,
  };
}

function batchPayload(status, tasks, progress) {
  return { batch_id: "batch-1", status, progress, tasks };
}

const report = {
  summary: "路径拼接缺少空值校验，遇到空 token 时崩溃。",
  root_cause: "parse_token 未校验 None，抛出了 AttributeError。",
  confidence: "high",
  evidence: [
    { path: "src/parser.py", lines: "L10-L12", reason: "未校验空值", strength: "strong", kind: "code" },
  ],
  proposed_changes: ["在 parse_token 入口增加空值校验。"],
  tests: ["补充空 token 回归用例。"],
  risks: [],
};

async function mockBatchApis(page) {
  const calls = { submit: null, retry: 0, status: 0, details: [] };

  // 注册顺序：通用在前，具体在后（Playwright 后注册的优先匹配）
  await page.route(/\/batch$/, (route) => {
    calls.submit = route.request().postDataJSON();
    return route.fulfill({
      json: batchPayload("pending", [task("t1", ISSUE_1, "pending"), task("t2", ISSUE_2, "pending")], {
        pending: 2,
        running: 0,
        completed: 0,
        failed: 0,
        cancelled: 0,
      }),
    });
  });
  await page.route(/\/batch\/batch-1$/, (route) => {
    const index = calls.status;
    calls.status += 1;
    const steps = [
      batchPayload(
        "running",
        [task("t1", ISSUE_1, "completed"), task("t2", ISSUE_2, "running")],
        { pending: 0, running: 1, completed: 1, failed: 0, cancelled: 0 },
      ),
      batchPayload(
        "partial",
        [
          task("t1", ISSUE_1, "completed"),
          task("t2", ISSUE_2, "failed", { error: "GitHub API returned 404: Not Found" }),
        ],
        { pending: 0, running: 0, completed: 1, failed: 1, cancelled: 0 },
      ),
    ];
    return route.fulfill({ json: steps[index] || steps[steps.length - 1] });
  });
  await page.route(/\/batch\/batch-1\/retry$/, (route) => {
    calls.retry += 1;
    return route.fulfill({
      json: batchPayload("running", [task("t1", ISSUE_1, "completed"), task("t2", ISSUE_2, "pending")], {
        pending: 1,
        running: 0,
        completed: 1,
        failed: 0,
        cancelled: 0,
      }),
    });
  });
  await page.route(/\/batch\/batch-1\/tasks\/(\w+)$/, (route) => {
    const taskId = route.request().url().split("/").pop();
    calls.details.push(taskId);
    return route.fulfill({
      json: { ...task(taskId, ISSUE_1, "completed"), report },
    });
  });
  return calls;
}

test("submits a batch, follows progress, opens a report, and retries failures", async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem("ds-theme", "light"));
  await page.route("**/sessions?**", (route) => route.fulfill({ json: [] }));
  const calls = await mockBatchApis(page);

  await page.goto("/");
  await page.locator("#batch-open-btn").click();

  const dialog = page.getByRole("dialog", { name: "批量分析" });
  await expect(dialog).toBeVisible();
  await expect(dialog).toContainText("还没有提交批次");

  // 仓库简写应被规范化为仓库链接，Issue 链接原样提交
  await page.fill("#batch-input", `${ISSUE_1}\n${ISSUE_2}\nacme/widget`);
  await page.locator("#batch-submit").click();

  await expect.poll(() => calls.submit).not.toBeNull();
  expect(calls.submit.issue_urls).toEqual([ISSUE_1, ISSUE_2]);
  expect(calls.submit.repository_urls).toEqual(["https://github.com/acme/widget"]);

  // 轮询：状态从"排队中"推进到阶段结果
  await expect(page.locator("#batch-progress-status")).not.toHaveText("排队中");
  await expect(page.locator("#batch-progress-status")).toHaveText("部分失败", { timeout: 10_000 });
  await expect(page.locator("#batch-progress-count")).toHaveText("2/2 已完成");
  await expect(page.locator(".batch-task", { hasText: "acme/widget #1" })).toContainText("已完成");
  const failedRow = page.locator(".batch-task", { hasText: "acme/widget #2" });
  await expect(failedRow).toContainText("失败");
  await expect(failedRow).toContainText("GitHub API returned 404: Not Found");

  // 失败项处理：重试按钮出现并发出请求
  const retry = page.locator("#batch-retry");
  await expect(retry).toBeVisible();
  await retry.click();
  await expect.poll(() => calls.retry).toBe(1);
  // 重试后继续轮询，最终回到 partial（mock 固定返回最后一步）
  await expect(page.locator("#batch-progress-status")).toHaveText("部分失败", { timeout: 10_000 });

  // 查看单个任务报告
  await page.locator(".batch-task", { hasText: "acme/widget #1" }).getByRole("button", { name: "查看报告" }).click();
  await expect.poll(() => calls.details).toContain("t1");
  const taskRow = page.locator(".batch-task", { hasText: "acme/widget #1" });
  await expect(taskRow).toContainText("路径拼接缺少空值校验");
  await expect(taskRow).toContainText("置信度：高");

  // 可及性：弹层本身必须零违规
  await page.addScriptTag({ path: AXE_PATH });
  const violations = await page.evaluate(async () => {
    const result = await window.axe.run(document.getElementById("batch-modal"), { resultTypes: ["violations"] });
    return result.violations.map((item) => ({ id: item.id, help: item.help, targets: item.nodes.map((n) => n.target) }));
  });
  expect(violations, JSON.stringify(violations, null, 2)).toEqual([]);

  // ESC 关闭并把焦点还给入口按钮
  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();
  await expect(page.locator("#batch-open-btn")).toBeFocused();

  // 刷新页面后再次打开：从 localStorage 恢复批次并继续显示进度
  await page.reload();
  await page.locator("#batch-open-btn").click();
  await expect(dialog).toBeVisible();
  await expect(page.locator("#batch-progress-count")).toHaveText("2/2 已完成", { timeout: 10_000 });
});

test("rejects unrecognized input before calling the API", async ({ page }) => {
  await page.route("**/sessions?**", (route) => route.fulfill({ json: [] }));
  let submitted = false;
  await page.route(/\/batch$/, (route) => {
    submitted = true;
    return route.fulfill({ json: batchPayload("pending", [], { pending: 0, running: 0, completed: 0, failed: 0, cancelled: 0 }) });
  });

  await page.goto("/");
  await page.locator("#batch-open-btn").click();
  await page.fill("#batch-input", "https://example.com/not-github");
  await page.locator("#batch-submit").click();

  await expect(page.locator("#batch-form-error")).toContainText("以下输入无法识别");
  expect(submitted).toBe(false);

  // 空输入给出明确指引，而不是静默失败
  await page.fill("#batch-input", "   \n  ");
  await page.locator("#batch-submit").click();
  await expect(page.locator("#batch-form-error")).toContainText("请至少输入一个");
});

test("explains an expired batch and restores the submission form", async ({ page }) => {
  await page.route("**/sessions?**", (route) => route.fulfill({ json: [] }));
  await page.route(/\/batch$/, (route) =>
    route.fulfill({
      json: batchPayload("pending", [task("t1", ISSUE_1, "pending")], {
        pending: 1,
        running: 0,
        completed: 0,
        failed: 0,
        cancelled: 0,
      }),
    }),
  );
  await page.route(/\/batch\/batch-1$/, (route) => route.fulfill({ status: 404, json: { detail: "Batch not found" } }));

  await page.goto("/");
  await page.locator("#batch-open-btn").click();
  await page.fill("#batch-input", ISSUE_1);
  await page.locator("#batch-submit").click();

  await expect(page.locator("#batch-form-error")).toContainText("该批次已不可用", { timeout: 10_000 });
  await expect(page.locator("#batch-form")).toBeVisible();
  // 失效批次不应再被记住
  expect(await page.evaluate(() => localStorage.getItem("iaLastBatchId"))).toBeNull();
});

test("@mobile keeps the batch dialog inside the viewport", async ({ page }) => {
  await page.route("**/sessions?**", (route) => route.fulfill({ json: [] }));
  await page.goto("/");
  await page.locator("#batch-open-btn").click();

  const dialog = page.getByRole("dialog", { name: "批量分析" });
  await expect(dialog).toBeVisible();
  const layout = await page.evaluate(() => ({
    document: document.documentElement.scrollWidth,
    viewport: document.documentElement.clientWidth,
    surface: document.querySelector(".batch-surface").getBoundingClientRect().width,
    close: document.querySelector("#batch-modal .drawer-close").getBoundingClientRect().width,
  }));
  expect(layout.document).toBe(layout.viewport);
  expect(layout.surface).toBeLessThanOrEqual(layout.viewport);
  expect(layout.close).toBeGreaterThanOrEqual(20);
  await expect(page.locator("#batch-input")).toBeVisible();
});
