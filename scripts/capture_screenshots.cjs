/* capture_screenshots.cjs —— 生成 README/docs 用的界面截图
 *
 * 用法（需先起本地服务，默认 127.0.0.1:18766，可用 ISSUE_AGENT_BASE_URL 覆盖）：
 *   node scripts/capture_screenshots.cjs
 *
 * 说明：优先使用系统 Edge 通道（channel: "msedge"），失败时回退 Chromium；
 * 数据来自完整结构的路由 mock（包含真实调查轨迹 events、5/5 报告能力、多模块证据链与批量队列），
 * 不依赖真实 GitHub/LLM 调用或数据库。
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
  issue_url: "https://github.com/acme/http-kit/issues/142",
  owner: "acme",
  repo: "http-kit",
  issue_number: 142,
  title: "含空格 URL 路径未百分号编码导致请求解析失败",
  status: "completed",
  phase: "completed",
  error_message: null,
  archived: false,
  version: 1,
  metrics: {
    duration_ms: 42300,
    duration_seconds: 42.3,
    model_calls: 5,
    tool_calls: 7,
    review_calls: 1,
    files_read: 4,
    total_tokens: 18420,
    estimated_cost_usd: 0.013,
  },
  created_at: new Date().toISOString(),
  updated_at: new Date().toISOString(),
});

const EVENTS = [
  { type: "phase", data: { phase: "fetching_issue", label: "读取 Issue 与关联讨论" } },
  { type: "tool_call", data: { name: "fetch_issue" } },
  { type: "phase", data: { phase: "investigating", label: "只读探索仓库源码" } },
  { type: "tool_call", data: { name: "search_code" } },
  { type: "tool_call", data: { name: "search_code" } },
  { type: "tool_call", data: { name: "read_file" } },
  { type: "tool_call", data: { name: "read_file" } },
  { type: "tool_call", data: { name: "read_file" } },
  { type: "phase", data: { phase: "verifying", label: "确定性证据交叉核验" } },
  { type: "tool_call", data: { name: "verify_evidence" } },
  { type: "review", data: { status: "approved" } },
];

const REPORT = {
  summary: "URL 路径拼接未做百分号编码，含空格或保留字符的路径在请求行被截断，导致 400 解析失败。",
  root_cause: "parse_path() 直接拼接原始 path 字段，跳过 quote(path, safe='/') 规范化；下游 PreparedRequest 复用同一未编码路径。",
  confidence: "high",
  evidence: [
    {
      path: "src/url/parser.py",
      lines: "L42-L58",
      reason: "parse_path() 直接拼接原始路径字符串，未执行百分号编码。",
      strength: "strong",
      kind: "code",
      claim: "根因",
    },
    {
      path: "src/http/client.py",
      lines: "L108-L124",
      reason: "send() 直接将未编码路径写入 HTTP/1.1 请求行。",
      strength: "strong",
      kind: "code",
      claim: "影响面",
    },
    {
      path: "src/http/request.py",
      lines: "L64-L79",
      reason: "PreparedRequest.prepare_url() 仅校验 scheme/host，未校验 path 空白字符。",
      strength: "moderate",
      kind: "code",
      claim: "影响面",
    },
    {
      path: "tests/test_url_encoding.py",
      lines: "L15-L34",
      reason: "现有用例仅覆盖 query 参数编码，缺失 path 含空格的回归覆盖。",
      strength: "strong",
      kind: "test",
      claim: "修复方案",
    },
  ],
  files_examined: [
    "src/url/parser.py",
    "src/http/client.py",
    "src/http/request.py",
    "tests/test_url_encoding.py",
  ],
  reproduction: {
    steps: [
      "构造含空格路径的请求：client.get('https://api.example.com/files/Q3 report.csv')",
      "观察发出的 HTTP 请求行在空格处被提前截断",
    ],
    observed: "服务端返回 400 Bad Request（请求行非法）",
    expected: "路径自动规范化为 /files/Q3%20report.csv 并正常返回 200",
  },
  proposed_changes: [
    "在 src/url/parser.py::parse_path() 入口对未编码段执行幂等百分号编码（避免对 %20 二次编码）。",
    "补充含空格、中文及已编码路径的 3 条回归用例。",
  ],
  patch:
    "--- a/src/url/parser.py\n+++ b/src/url/parser.py\n@@ -45,3 +45,4 @@\n-    return f'{base}{raw_path}'\n+    safe_path = quote(unquote(raw_path), safe='/:@!$&()*+,;=')\n+    return f'{base}{safe_path}'\n",
  tests: ["tests/test_url_encoding.py::test_space_in_path_is_percent_encoded"],
  risks: ["若调用方有意传递已编码保留字（如 %2F），unquote+quote 需按分段处理以避免误还原斜杠"],
  impact: {
    severity: "high",
    likelihood: "medium",
    blast_radius: [
      "src/url/parser.py",
      "src/http/client.py",
      "src/http/request.py",
      "tests/test_url_encoding.py",
    ],
  },
  review_audit: {
    status: "approved",
    summary: "证据行号经源码核验全部命中，根因与幂等编码修复方案闭环一致。",
    findings: [],
  },
};

const BATCH_DETAIL = {
  batch_id: "batch-demo-01",
  status: "partial",
  progress: { pending: 0, running: 1, completed: 2, failed: 1, cancelled: 0 },
  tasks: [
    {
      task_id: "t1",
      issue_url: "https://github.com/acme/http-kit/issues/142",
      status: "completed",
      has_report: true,
      error: null,
      session_id: "s1",
    },
    {
      task_id: "t2",
      issue_url: "https://github.com/acme/http-kit/issues/156",
      status: "completed",
      has_report: true,
      error: null,
      session_id: "s2",
    },
    {
      task_id: "t3",
      issue_url: "https://github.com/acme/http-kit/issues/168",
      status: "running",
      error: null,
      session_id: null,
    },
    {
      task_id: "t4",
      issue_url: "https://github.com/acme/http-kit/issues/171",
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
          {
            role: "user",
            content: "https://github.com/acme/http-kit/issues/142",
            created_at: new Date().toISOString(),
          },
          {
            role: "assistant",
            content: "已定位根因：**parse_path() 未做百分号编码**，含空格的路径在 HTTP 请求行被截断；已核验 4 条源码证据并生成补丁。",
            created_at: new Date().toISOString(),
          },
        ],
        events: EVENTS,
        report: REPORT,
      }),
    });
  });
  await page.route(/\/batch\/batch-demo-01\/tasks\/t1$/, (r) =>
    r.fulfill({
      json: {
        task_id: "t1",
        issue_url: "https://github.com/acme/http-kit/issues/142",
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
  await page.waitForTimeout(1600);
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
