# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **axe 审计覆盖导出的 HTML 报告**：e2e 用例现在会调用 `IA.Export.selfContainedHtml`
  生成导出文件、离线（阻断 CDN）渲染后跑同一套 axe 断言，导出的报告不再有审计盲区。
- `scripts/capture_screenshots.cjs`：用路由 mock 复现三张文档截图，配色/布局变更后重跑即可。
- `.git-blame-ignore-revs`：把纯格式化提交排除在 `git blame` 之外
  （`git config blame.ignoreRevsFile .git-blame-ignore-revs`）。


- `.github/dependabot.yml` — pip / npm / github-actions 三个生态的每周依赖更新，
  按生态分组并限制并发 PR 数量。
- `CHANGELOG.md` — 本文件。
- CI 产物留存：测试作业上传 `coverage.xml` 与 `report.xml`；Docker 作业在构建后
  真实启动容器并探活。
- 调查并发闸门 MAX_CONCURRENT_INVESTIGATIONS（默认 3）与单次调查成本上限
  MAX_SESSION_ESTIMATED_COST_USD（默认 0 = 不限）：限流只按请求数计，而一次
  /stream 可能跑满 INVESTIGATION_TIMEOUT，需要并发闸门兜住 token 成本与写入压力。
- GET /health 新增 uth_enabled / write_mode，前端与运维脚本据此判断实例安全姿态。
- GET /session/{id}/proposal 新增 write_mode、proposed_bytes 与 preview
  （每个待写入文件前 20 行）：人工确认必须看得到“到底要写什么”，而不是只有行数摘要。
- **「创建 PR」界面（pp/static/js/pr-panel.js）**：后端 /session/{id}/proposal 与
  pply-fix 早已就绪，但前端从未实现，用户只能下载 .patch 手工建分支。现在报告补丁
  章节内会出现创建入口：先展示提案（分支/标题/正文/逐文件内容预览）供人工确认，确认后
  才调用 pply-fix；写模式关闭时按钮置灰并给出可行动说明。面板用 MutationObserver
  观察报告容器注入，不改动 pp.js 的渲染流程；插值全部走 	extContent（无 innerHTML）。


- **axe-core 自动化可及性审计**（`axe-core` 为 devDependency + 一条 e2e 用例）：在 6 个界面
  （首页、设置抽屉、命令面板、帮助浮层、报告浅色、报告深色）上断言**零违规**，审计以
  `prefers-reduced-motion` 运行，避免入场动画的 `opacity:0` 造成 button-name 误报。
  上面 5 类问题全部是这条用例在 CI 上前置发现并修掉的。

- **TypeScript 决策测试记录的是过期快照**：docstring 声称「无 package.json、
  第三方库走 CDN、4644 行 / 3 个文件」，实际是「有 package.json（仅 Playwright
  测试依赖、无构建脚本）、vendor 本地自带、约 8.1k 行 / 11 个文件」。改为用当前
  文件系统事实做可计算断言，并在「构建链已就绪」时失败提醒重新决策。

### Fixed

- **波及范围柱状图的数值标签含义错误**：坐标轴是「改动行数」，但无补丁数据的柱会显示
  报告严重度（如「高」）——既与坐标轴无关，又和柱体颜色重复表达同一件事。现在数值标签
  一律表达该柱自己的量：有数据时「N 行」，无数据时「—」；e2e 断言标签不得出现严重度词。


- **风险矩阵缺图例（颜色含义靠猜）**：标记颜色代表严重度、格底色代表风险等级，两者含义
  不同却没有说明。现在矩阵卡片内加了严重度图例（严重/高/中/低四色点），颜色直接由
  `riskMarkerColor` 生成，与标记永不脱节；e2e 断言图例四项齐全、四色互异，且「高」的色点
  必须与矩阵实际标记色一致。
- **图表坐标轴标签偏小**：横/纵轴标签由 11px 提升到 12px（图上最小的字），响应式不重叠
  用例同步验证。


- **同一严重度在不同图里显示成不同颜色**：`high` 在风险矩阵用 `riskMarkerHigh`（橙）、
  在波及范围/treemap 用 `warning`（黄）；`low` 一处 `success`（绿）一处 `muted`（灰）；
  `medium` 一处 `warning`（黄）一处 `primary`（蓝）—— 用户得为每张图重新学一遍映射。
  现在 `severityColor` 直接委托给 `riskMarkerColor`（单一来源），并新增测试断言锁死这一
  不变量（同时校验映射本身未被改坏）。
- **暗色风险矩阵的格底色糊成一片**：网格线用的 `#161b22` 与深色格底几乎同色，四类深色块
  连成一体。改用官方 `gray/1`（`#2a2b2d`）后分隔清晰，标记也略放大（18→20，描边 2.5→3）。
- 顺带记录了「格底色最优解」的实测结论：官方 0-2 档位已是可测指标上的最优（四类两两
  ΔE 16.9 / 20.9）；改用「官方色相 + 低透明度叠底」会让 ΔE 掉到 8.0 / 5.7，反而更难分辨，
  因此维持官方档位。


- **配色回归 GitHub 官方色板（同时保持色盲可区分）**：此前为破解红绿色盲下的色相坍缩，
  「高」等级标记改用了紫色（`#bc8cff` / `#8250df`），偏离了本项目的 GitHub 配色基调。
  现在改为**全部取值来自 `@primer/primitives` 11.10.0 的官方语义色与官方色阶**
  （2026-09-16 取包核对）：
  · critical 用官方 `fgColor/danger`（暗 `#f85149` / 浅 `#d1242f`）
  · low 用官方 `fgColor/success`（暗 `#3fb950` / 浅 `#1a7f37`）
  · high 用官方色阶 `orange/5`（暗 `#c46212`）/ `orange/6`（浅 `#a24610`）
  · medium 用官方色阶 `yellow/9`（暗 `#f0ca6a`）/ `yellow/6`（浅 `#805900`）
  · unknown 用官方灰阶（暗 `#92a1b5` / 浅 `#647182`）
  · 四类风险格底色改用官方低档色阶（暗 `green/0`、`yellow/2`、`orange/0`、`red/0`；
    浅 `green/0`、`yellow/0`、`orange/1`、`red/0`），hover 取相邻档
  在这个「只能用官方取值」的约束下，色盲最差 ΔE 仍从 3.6 / 4.5 提升到 **13.7 / 12.8**，
  且标记与所在格底色、页面背景的对比度均 ≥ 3:1，四个风险等级格底色两两 ΔE ≥ 16.9 / 20.9。
  同时把 UI 文本色对齐官方：`--muted` → `fgColor/muted`（暗 `#9198a1` / 浅 `#59636e`）、
  浅色 `--faint` → 官方 `gray/5`（`#647182`）。
  测试同步升级：`tests/test_chart_palette_accessibility.py` 新增
  「调色板取值必须全部来自官方色板」的断言（逐条登记 token 名），并把原先的「明度阶梯」
  断言换成「格底色两两 ΔE 下限」—— 实测 Primer 同一色系 0-2 档明度几乎相同
  （暗 L*≈10-11、浅 L*≈92-93），在只用官方取值的前提下无法构成单调阶梯。


- **导出的独立 HTML 报告缺少地标（axe landmark-one-main / region）**：导出页的标题、
  元信息与报告主体都是 `<body>` 直接子元素，屏幕阅读器无法按地标跳转。现用 `<main>`
  包裹，导出文件在 axe 下同样达到零违规。
- 文档截图与当前配色不一致（此前为 2026-08-21 手工截取）：已重新生成
  `docs/screenshots/{home,report,charts}-dark.png`，并给出可复现脚本。


- **Docker 镜像内数据目录不可写（严重）**：`/app/data` 此前不存在且归 root 所有，
  非 root 的 `appuser` 首次落库会 `PermissionError`（`SESSION_DB_PATH` 默认
  `data/sessions.db`）。现在在 `USER appuser` 之前 `mkdir -p /app/data` 并 chown。
- **CI 从不真正运行镜像**：docker 作业过去只 `docker build`，镜像"能构建但开机即坏"
  无法被发现。现在构建后启动容器、轮询 `/health` 直到 `status=ok`（超时打印
  `docker logs` 并失败），并以容器内 `appuser` 身份写入 `/app/data` 验证目录可写。
- **`SESSION_STALE_AFTER_SECONDS` 三方冲突**：`.env.example` 与
  `wiki/Configuration.md` 写 `1800`，而代码默认 `300`（`app/config.py`，`app/main.py`
  的注释也明确按 300 设计）。由于 `start-issue-agent.ps1` 会把 `.env.example` 复制成
  `.env`，一键启动跑 1800、Docker/CI 跑 300，行为不一致。现统一为 `300`，并在注释中
  写明它必须大于 `INVESTIGATION_TIMEOUT`（默认 600），否则正在执行的调查会被
  `recover_stale_sessions()` 误判为孤儿。
- **一键启动把正常路径报成失败**：首次运行（无 `.env`）时 `start-issue-agent.ps1`
  会复制示例、打开记事本并 `exit 2`，而 `打开 Issue Agent.cmd` 用 `if errorlevel 1`
  判定，导致用户第一次双击看到"启动失败"。现在 `exit 2` 单独提示"已创建 .env，
  请填入 `OPENAI_API_KEY` 后再次双击启动"。
- **`scripts/backfill_reports.py`**：不再硬编码 `data/sessions.db`，改为通过
  `app.config.get_settings().session_db_path` 解析（尊重 `SESSION_DB_PATH` 与 `.env`）；
  数据库不存在时返回非零退出码并说明原因（此前静默 `return 0` 空转）；备份前先
  `PRAGMA wal_checkpoint(TRUNCATE)`，避免 WAL 模式下未 checkpoint 的数据不在主库
  文件里而被漏备。
- **同一会话并发调查会互相抹掉事件史（严重）**：新增原子「会话调查租约」
  （SessionManager.try_claim_running，SQLite 走条件 UPDATE、内存实现走状态检查），
  只有把会话从非 running 翻成 running 的请求能继续，续跑前的 clear_events() 不再
  可能删掉另一个正在跑的调查的事件；并发请求现在收到 409 / SSE error 事件。
- **长调查被 stale recovery 误判为孤儿**（终态丢失、用户拿到 409）：/chat 与
  /chat/stream 现在都有活跃心跳（periodic_touch，每 15s），与 /stream 的 SSE 心跳对齐。
- **熔断器把请求级 4xx 计入失败**：只有可重试故障（408/409/425/429/5xx、连接/超时）
  才计数，五次写错的 model 名不再能让 provider 全局熔断 30 秒；新增
  
eport_stream_outcome，流式调用在**消费完成后**补报真实结果（此前 call() 在拿到
  响应对象时就记了成功，中途断流/超时对熔断器完全不可见）。
- **请求体 model 缺少校验**：现在受字符集与长度约束
  （[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}）。
- **写路径缺少服务端不变量**：
alidate_pr_proposal 拒绝受保护路径
  （.github/**、.git/**、Dockerfile、.env*、锁文件、*.pem/*.key、CODEOWNERS）
  与受保护分支（main/master/develop/release/hotfix/rc/gh-pages），并新增 apply-fix 审计日志
  （仓库/分支/文件数/提案内容哈希）。
- **POST /session/import 可注入 pending_pr**：导入不再恢复写意图，否则任何持
  API_KEY 的调用方都能「导入一个提案 → 调用 apply-fix」用仓库 token 推任意文件并开 PR。
- **WRITE_MODE=true 且未设 API_KEY 时拒绝启动**（写模式能推代码，绝不能免鉴权暴露）；
  未设 API_KEY 时启动打印显著警告。
- **grep_content 的 ReDoS 风险**：嵌套量词模式直接拒绝，扫描改到工作线程执行
  （最坏只占用一个线程，事件循环与 SSE 心跳不受影响），并限制扫描字符总量。
- **MemoryStore 与 SqliteStore 语义不一致**：get() 返回副本、save() 回写并检测版本
  冲突，:memory:/dev 模式不再靠对象别名掩盖并发写入问题。
- **会话导入逐条入库**：事件改为单事务批量插入（5MB 上限此前最多 5000 次 commit）。
- **_migrate_report_enrichment_once 的进程级 flag**：改为按数据库路径记账，同进程内的
  第二个数据库不再静默跳过回填。
- /stream 的取消分支显式关闭心跳包装器（不再等 GC 回收），被取消的调查立刻停止，
  不会再多跑完当前网络步骤并继续计费。
- **进度条填充不可见**：.progress-bar-fill 引用从未定义的 --blue，实测背景为
  
gba(0,0,0,0)，进度条只剩灰槽。改用 
ar(--accent)。
- **浅色主题下「续跑提示卡」深底灰字**：--surface-2 同样未定义且兜底是深色，对比度约
  2.5:1；改用主题感知的 --canvas-subtle / --border，实测 4.93:1（浅）/ 5.62:1（深）。
- **进度条每次阶段变化被整块重建**（刚出现就被抹掉、	ool_call 后还会倒缩）：改为常驻
  DOM + 单调递增钳制，阶段更新只改文本与宽度。
- **取消分析后按钮不再恢复**：30s 轮询耗尽时按钮停在 display:none，用户既不能再次取消
  也无法发起新分析；现在恢复按钮并说明服务端仍在收尾。
- **文件追踪面板每次工具调用强制合上**：重建时保留用户展开状态。
- **长任务没有时间预期**：唯一的「通常 30–120 秒」提示 25 秒后自行消失；现在按阶段持续
  显示（获取信息 10–30s / 调查代码 2–5 分钟 / 生成报告 1–2 分钟），工具执行中改显示
  「正在执行 <工具>」，看门狗阈值在工具执行期间放宽（30s/90s → 180s/300s），不再劝退
  正常的慢搜索。
- **分析失败没有重试出口**：流中断此前只给一行错误；现在与会话续跑（有会话）或重试
  （无会话）按钮打通。
- **空输入回车无反馈**：改为输入框下方行内提示，而不是静默 
eturn。
- **归档当前会话静默跳回首页 / 删除会话无反馈**：补 toast（归档文案说明去哪儿恢复）。
- **「重新生成」先删掉上一条回答**（失败即永久丢失）：改为保留旧回答并标记「已被取代」。
- **中文界面出现英文工具名**（
ead_file {"path": ...}）：工具名与参数摘要改为本地化短语
  （「读取文件 src/app.py」「搜索代码 "parse_path"」「内容检索 pattern=TODO app/」）。
- **密钥引导**：401/403 都触发全局引导（此前只有 401），并修正「设置面板在右上角」的
  错误方位（实际是左上角齿轮按钮）。
- **「复制」语义不明 / 反馈过短**：报告 JSON 复制按钮改为「复制报告 JSON」，toast 由
  1.6s 延长到 2.5s，下载类 toast 带上真实文件名。
- **导出 HTML 的数据岛转义错误**：`<script type="application/json">` 是 raw text、实体不解码，
  原用 `escapeHtml` 会把 `< > &` 变成字面量污染导出页数据；改用 `\u003c` 转义。
- **图表调色板在色盲下不可区分（可及性）**：风险矩阵的五个严重度标记
  （critical/high/medium/low/unknown）会同时出现在一张图里，实测在红色盲
  （protanopia）模拟下 `danger/riskMarkerHigh` 的 Lab 色差只有 **3.6**（暗色）/
  **4.5**（浅色）——「严重」与「高」对红绿色盲用户几乎同色。调整后在同等约束
  （页面背景与所在格底色对比度均 ≥ 3:1）下最差色差提升到 **15.6 / 27.1**：
  暗色 `danger #f85149→#ff7b72`、`riskMarkerHigh #f0883e→#bc8cff`（改紫色以跳出
  红-橙-黄坍缩）、`warning #d29922→#bf8700`、`success #3fb950→#7ee787`、
  `muted #8b949e→#6e7681`；浅色 `riskMarkerHigh #bc6b00→#8250df`、
  `warning #9a6700→#7d4e00`、`success #1a7f37→#0f5323`。新增
  `tests/test_chart_palette_accessibility.py`，用 Viénot/Brettel 二色觉模拟把
  「ΔE ≥ 14 且对比度 ≥ 3:1」固化为可回归的约束。
- **触屏按钮小于最小可点区域**：手机视口下图标按钮实测 34×34，低于 iOS HIG /
  Android 建议的 44×44。触屏媒体查询下提升到 ≥44×44，并在移动端 e2e 用例中断言
  「可见图标按钮不得小于 44×44」。
- **进度区每秒播报（屏幕阅读器不可用）**：`#progress` 既是可见进度区，又挂着
  `role="status" aria-live="polite"`，而它每秒都会被「阶段 · 已用时」重写——辅助技术
  用户会听到每秒一次的播报。现在可见区域不再声明 live，播报改由视觉隐藏的
  `#progress-live` 承担，且只在内容真正变化时写入（阶段变化才播报，秒数刷新被去重挡掉）。
- **流式回答逐字播报**：`#messages` 是 polite live region，而回答以最高 12fps 重渲染，
  辅助技术会被高频增量淹没。现在流式期间给它加 `aria-busy="true"`、回答结束后移除，
  由辅助技术在完成后一次性播报。

- e2e 新增两条可及性回归：进度区不得声明 live（并用 MutationObserver 断言重复写入不触碰
  DOM）、流式回复期间 `#messages` 必须处于 `aria-busy` 且结束后清除。
- `playwright.config.js` 支持 `IA_BROWSER_CHANNEL` 环境变量：本机装不上 Playwright 自带
  chromium 时，可用系统 Edge/Chrome 跑同一套件（README 已注明）。

- **风险矩阵格底色的色阶在色盲/灰度下无法分辨**：四行严重度底色此前亮度几乎相同
  （只有色相差异，L* 都在 30 附近），红绿色盲用户与黑白打印时读不出等级。现在改为
  **明度阶梯**：暗色逐级变亮（L* 19.5 → 26.0 → 30.0 → 33.7）、浅色逐级加深
  （95.7 → 93.3 → 87.0 → 84.2），相邻严重度在色盲模拟下的 ΔE 达到暗色 9.7 / 浅色 4.9。
  `tests/test_chart_palette_accessibility.py` 新增「明度必须单调且步长 ≥ 2」与
  「相邻等级色盲 ΔE 下限」两条断言，改回等亮度会直接失败。
- **浅色主题 `--faint` 对比度不足**：`#8c959f` 在白底上只有 2.9:1（低于 AA 4.5:1），
  而它用于时间戳、占位符、说明等小字。改为 `#6e7781`（4.6:1）。
- **导出 HTML 硬编码语言与英文主题标签**：导出页此前固定 `lang="zh-CN"`、主题按钮写死
  `Light`/`Dark`（且初始标签与当前主题不一致）。现在语言跟随界面、标签走新增的
  `theme_light`/`theme_dark` 键，初始文本与当前主题一致。
- **历史空态文案未转义**：与相邻分支的 `escapeHtml` 处理保持一致。

- **命令面板的「操作」条目渲染成空白行（axe critical）**：渲染代码读 `it.label`，而操作
  条目的标签存在 `it.data.label` 上 —— 7 个操作全是只有图标的空按钮，用户看到的是空白
  行（会话条目不受影响）。同时该面板把 `role="dialog"` 放在内层 surface 上，导致内容被
  判定为不在地标内；分组标题/副标题/页脚用 `opacity` 降级，把对比度一并压到 AA 以下。
  现已：读取正确的标签、把对话框语义移到外层容器、用 `--muted` 取代 `opacity`。
- **4 个对话框表面没有可访问名（axe aria-dialog-name, serious）**：设置抽屉、命令面板、
  帮助浮层、对比浮层都带 `role="dialog"` 却没有 name/labelledby —— 屏幕阅读器只会播报
  「对话框」。现均补上 `aria-label`（沿用各自的标题文案）。
- **侧栏与时间线小字对比度不足（axe color-contrast, serious）**：暗色 `--faint` 在侧栏底色上
  只有 3.7:1；选中/悬停的会话行带 accent 叠色，其中时间戳掉到 4.22:1；浅色主题 `--muted`
  比 `--faint` 还深（层级颠倒），且 `--faint` 在卡片底色上只有 4.30:1。现：暗色 `--faint`
  → `#7d8590`（4.6:1）、选中行时间戳改用 `--textDim`（13.3:1）、浅色 `--muted` → `#57606a`
  （6.6:1）与 `--faint` → `#68707b`（卡片上 4.7:1），层级与对比度同时达标。
- **标题层级与地标（axe heading-order / landmark / page-has-heading-one）**：报告章节从
  `h4` 改为 `h3`（不再从面板的 h2 跳级）；hero 标语由 `h1` 降为 `h2`，每屏唯一的 `h1`
  交给会话标题（详情视图里 hero 会被移除，原先那里没有任何可见 h1）；`#main` 补
  `role="main"`、`#sidebar` 补 `role="complementary"` + 可访问名；装饰性 `<header>`
  降级为 `<div>`（避免 banner 地标嵌套）。顺带修掉两处遗留的 `</header>` 闭合标签。

### Changed

- **类型检查与格式检查覆盖到测试与脚本**：mypy app/ tests/ scripts/ evals/ 现在全清
  （此前只检查 app/；测试侧 31 处类型问题按真实类型修掉，而不是用 ignore 掩盖——
  自造请求替身改用真实的 ApplyFixRequest、消息字面量补 list[dict] 标注、Optional
  先断言再索引、工厂 kwargs 用 Any、SimpleNamespace 替身显式 cast；仅「刻意替换方法
  模拟故障」的 7 处保留带原因说明的 type: ignore）。
- ruff format 统一了 22 个文件，CI 同时固化 ruff check、ruff format --check 与 mypy 三项，
  范围扩到 app/ tests/ scripts/ evals/。
- 排查记录：ruff check --fix 的 B010 会把 setattr(obj, "m", v) 自动改回直接赋值——
  改造中「改完又变回去」正是这个原因，改用显式类型忽略注释后稳定。


- Docker 构建改为按 `uv.lock` 精确安装依赖（`uv export --frozen` 导出固定版本后交给
  pip 安装，项目本体 `--no-deps`）：锁文件此前提交却无人消费，镜像里装到的版本会随上游
  漂移，现在 CI 的 pip-audit 与镜像看到同一套版本。
- `/health` 轮询在后台标签页跳过、回到前台立即补一次，不再为不可见页面持续发请求。
- 本地 `.env` 补齐 24 项此前隐含的可调项（全部以注释形式给出默认值，行为不变）。
- e2e 新增键盘下钻用例：指标卡（`role="link"`）Enter 激活、修复方案条目 Enter 下钻并
  高亮目标章节，确保「可聚焦」同时「可激活」。

- README 的「测试」命令与 CI 对齐（`ruff check app/ tests/`、`npm ci` 而非
  `npm install`、补 `pip-audit --skip-editable`），本地照抄即可复现 CI 结论。
- Dockerfile 改为非 editable 安装（`pip install .`），镜像内不再保留指向构建阶段
  源码树的 `.pth` 链接。
- `.dockerignore` 排除 `wiki/`、`docs/`、`evals/`、`REVIEW-REPORT.md`、`CHANGELOG.md`，
  减小构建上下文；构建必需的 `pyproject.toml`、`app/`、`README.md` 保留。
- CI 测试矩阵新增 Python `3.14`（本地 `.venv` 就是 3.14.7），保留 3.11 / 3.12 / 3.13。
- CI 新增 `concurrency` 分组，同一 ref 的旧运行会被取消。
- CI 中所有 action 按 commit SHA 固定版本（行尾注释版本号）。
- `playwright.config.js`：`fullyParallel` 由 `true` 改为 `false`，与注释中已说明的
  `workers: 1` 约束（共享 webServer + `:memory:` SQLite）保持一致。

- 本机 .env 的 SESSION_STALE_AFTER_SECONDS 同步为 300，与代码默认值、
  .env.example、wiki 三方一致（原为 1800，会让横死会话多挂 25 分钟）。
## [0.6.0] - 2026-09

### Added

- 成本可观测：`INPUT_TOKEN_PRICE_PER_MILLION` / `OUTPUT_TOKEN_PRICE_PER_MILLION`
  与报告中的 `estimated_cost_usd`。
- 证据内容对齐与批量落盘；`evals/` 基准套件。
- 默认模型切换为 `deepseek-flash`（DeepSeek V4.1 Flash）。

### Changed

- 前端按职责块拆分（`app.js` 拆出独立前端模块）。
- `main.py` 路由模块化：新增 `routes/`、`deps.py`、`sse.py`、`rate_limit.py`。

### Fixed

- 进度条透明等配色缺陷与三处交互卡死。
- 取消分析时未停止计时器；真机走查发现的六处交互体验问题。
- 启动器在依赖探测失败时透出真实原因，而不是笼统提示。
- 限流清理测试在 CI 上的环境依赖型 flaky。

[Unreleased]: https://github.com/xiaomaozjj666/issue-agent/compare/v0.6.0...HEAD
[0.6.0]: https://github.com/xiaomaozjj666/issue-agent/releases/tag/v0.6.0
