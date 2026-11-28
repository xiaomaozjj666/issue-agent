/* capture_screenshots.cjs —— 生成 README/docs 用的界面截图
 *
 * 用法（需先起本地服务，默认 127.0.0.1:18766，可用 ISSUE_AGENT_BASE_URL 覆盖）：
 *   node scripts/capture_screenshots.cjs
 *
 * 说明：优先使用系统 Edge 通道（channel: "msedge"），失败时回退 Chromium；
 * 数据全部来自路由 mock，不依赖真实 GitHub/LLM 调用或数据库。
 * 配色或布局改动后重跑即可刷新文档图：
 *   home-dark.png / report-dark.png / charts-dark.png / batch-dark.png
 */
const path = require("node:path");
const fs = require("node:fs");
const { chromium } = require("@playwright/test");

const REPO_ROOT = path.resolve(__dirname, "..");
const OUT = path.join(REPO_ROOT, "docs", "screenshots");
const BASE = (process.env.ISSUE_AGENT_BASE_URL || "http://127.0.0.1:18766").replace(/\/+$/, "");

const summary = () => ({
  session_id: "s1",
  issue_url: "https://github.com/acme/widget/issues/1",
  owner: "acme",
  repo: "widget",
  issue_number: 1,
  title: "路径未编码导致解析失败",
  status: "completed",
  phase: "completed",
  error_message: null,
  archived: false,
  version: 1,
  metrics: { duration_seconds: 42, estimated_cost_usd: 0.013 },
  created_at: new Date().toISOString(),
  updated_at: new Date().toISOString(),
});

const REPORT = {
  summary: "URL 路径未做百分号编码，含空格的路径被截断，导致解析失败。",
  root_cause: "parse_path() 直接拼接原始路径，未调用 encodeURIComponent 等价逻辑。",
  confidence: "high",
  evidence: [
    { path: "src/Lib/Widget.cpp", lines: "L10-L12", reason: "入口直接拼接原始路径。", strength: "strong", kind: "code", claim: "根因" },
    { path: "src/library/json.cpp", lines: "L88", reason: "透传未校验。", strength: "moderate", kind: "code", claim: "影响面" },
    { path: "tests/test_path.cpp", lines: "L5-L18", reason: "回归用例覆盖含空格路径。", strength: "strong", kind: "test", claim: "修复方案" },
  ],
  files_examined: ["src/Lib/Widget.cpp", "src/library/json.cpp"],
  proposed_changes: ["在入口统一做百分号编码，并补一条含空格的回归用例。"],
  patch: "--- a/src/Lib/Widget.cpp\n+++ b/src/Lib/Widget.cpp\n@@ -10,3 +10,3 @@\n-  auto p = base + raw;\n+  auto p = base + encode(raw);\n",
  tests: ["tests/test_path.cpp::space_in_path"],
  risks: ["旧调用方若已编码会出现双重编码"],
  impact: { severity: "high", likelihood: "medium", blast_radius: ["src/Lib/Widget.cpp", "src/library/json.cpp"] },
  review_audit: { status: "approved", summary: "根因与补丁一致", findings: [] },
};

const BATCH_DETAIL = {
  batch_id: "batch-demo-01",
  status: "partial",
  progress: { pending: 0, running: 1, completed: 2, failed: 1, cancelled: 0 },
  tasks: [
    {
      task_id: "t1",
      issue_url: "https://github.com/acme/widget/issues/1",
      status: "completed",
      has_report: true,
      error: null,
      session_id: "s1",
    },
    {
      task_id: "t2",
      issue_url: "https://github.com/acme/widget/issues/14",
      status: "completed",
      has_report: true,
      error: null,
      session_id: "s2",
    },
    {
      task_id: "t3",
      issue_url: "https://github.com/acme/widget/issues/27",
      status: "running",
      error: null,
      session_id: null,
    },
    {
      task_id: "t4",
      issue_url: "https://github.com/acme/widget/issues/31",
      status: "failed",
      error: "GitHub API 限流（403 rate limit exceeded），可稍后点击重试",
      session_id: null,
    },
  ],
};

async function launchBrowser() {
  const preferred = process.env.IA_BROWSER_CHANNEL || "msedge";
  try {
    return await chromium.launch({ channel: preferred });
  } catch {
    return await chromium.launch();
  }
}

(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  const browser = await launchBrowser();
  const ctx = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    deviceScaleFactor: 1,
    reducedMotion: "reduce",
  });
  const page = await ctx.newPage();
  await page.addInitScript(() => {
    localStorage.setItem("ds-theme", "dark");
    localStorage.removeItem("iaLastBatchId");
  });
  await page.route("**/health", (r) => r.fulfill({ json: { status: "ok", app: "issue-agent", build_id: "shot" } }));
  await page.route(/\/sessions\?.*/, (r) => r.fulfill({ json: [summary()] }));
  await page.route(/\/session\/[^/?]+(\/(report|export|proposal))?$/, (route) => {
    const kind = (new URL(route.request().url()).pathname.match(/\/(report|export|proposal)$/) || [])[1];
    if (kind === "report") return route.fulfill({ json: REPORT });
    return route.fulfill({
      json: Object.assign({}, summary(), {
        messages: [
          { role: "user", content: "https://github.com/acme/widget/issues/1", created_at: new Date().toISOString() },
          { role: "assistant", content: "已定位根因：**parse_path() 未做百分号编码**，含空格的路径被截断。", created_at: new Date().toISOString() },
        ],
        events: [],
        report: REPORT,
      }),
    });
  });
  await page.route(/\/batch\/batch-demo-01\/tasks\/t1$/, (r) =>
    r.fulfill({
      json: {
        task_id: "t1",
        issue_url: "https://github.com/acme/widget/issues/1",
        status: "completed",
        error: null,
        report: REPORT,
      },
    })
  );
  await page.route(/\/batch\/batch-demo-01$/, (r) => r.fulfill({ json: BATCH_DETAIL }));

  await page.goto(BASE + "/");
  await page.waitForTimeout(900);
  await page.screenshot({ path: path.join(OUT, "home-dark.png") });
  console.log("home-dark.png 完成");

  await page.locator(".session-card").first().click();
  await page.waitForTimeout(700);
  await page.click("#report-toggle");
  await page.waitForTimeout(1500);
  await page.screenshot({ path: path.join(OUT, "report-dark.png") });
  console.log("report-dark.png 完成");

  const charts = page.locator("#report-risk-matrix-section").locator("xpath=..");
  await charts.scrollIntoViewIfNeeded();
  await page.waitForTimeout(1300);
  await charts.screenshot({ path: path.join(OUT, "charts-dark.png") });
  console.log("charts-dark.png 完成");

  // 批量分析弹窗（含进度、完成/运行/失败任务与重试操作）
  await page.evaluate(() => localStorage.setItem("iaLastBatchId", "batch-demo-01"));
  await page.click("#batch-open-btn");
  await page.waitForTimeout(900);
  await page.locator('[data-task-id="t1"] .batch-task-report-btn').click();
  await page.waitForTimeout(700);
  await page.screenshot({ path: path.join(OUT, "batch-dark.png") });
  console.log("batch-dark.png 完成");

  for (const name of ["home-dark.png", "report-dark.png", "charts-dark.png", "batch-dark.png"]) {
    console.log(name, fs.statSync(path.join(OUT, name)).size, "字节");
  }
  await browser.close();
  process.exit(0);
})().catch((e) => {
  console.error("SHOT FAILED:", (e && e.stack) || e);
  process.exit(1);
});
