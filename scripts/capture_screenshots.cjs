/* capture_screenshots.cjs —— 生成 README/docs 用的界面截图
 *
 * 用法（需先起本地服务，默认 127.0.0.1:18766）：
 *   node scripts/capture_screenshots.cjs
 *
 * 说明：使用系统 Edge 通道（channel: "msedge"），数据全部来自路由 mock，
 * 因此不依赖真实 GitHub/LLM 调用，也不需要数据库；配色或布局改动后重跑即可刷新文档图。
 */
/* 重新生成 README 截图：home-dark / report-dark / charts-dark（与当前配色一致） */
const { chromium } = require("D:/XIAOMAO/Projects/issue-agent/node_modules/@playwright/test");
const path = require("node:path");
const fs = require("node:fs");

const OUT = "D:/XIAOMAO/Projects/issue-agent/docs/screenshots";
const BASE = "http://127.0.0.1:18766";

const summary = () => ({
  session_id: "s1", issue_url: "https://github.com/acme/widget/issues/1", owner: "acme", repo: "widget",
  issue_number: 1, title: "路径未编码导致解析失败", status: "completed", phase: "completed",
  error_message: null, archived: false, version: 1, metrics: { duration_seconds: 42, estimated_cost_usd: 0.013 },
  created_at: new Date().toISOString(), updated_at: new Date().toISOString(),
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

(async () => {
  const browser = await chromium.launch({ channel: "msedge" });
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1, reducedMotion: "reduce" });
  const page = await ctx.newPage();
  await page.addInitScript(() => localStorage.setItem("ds-theme", "dark"));
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
        events: [], report: REPORT,
      }),
    });
  });

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

  for (const name of ["home-dark.png", "report-dark.png", "charts-dark.png"]) {
    console.log(name, fs.statSync(path.join(OUT, name)).size, "字节");
  }
  await browser.close();
  process.exit(0);
})().catch((e) => { console.error("SHOT FAILED:", (e && e.stack) || e); process.exit(1); });
