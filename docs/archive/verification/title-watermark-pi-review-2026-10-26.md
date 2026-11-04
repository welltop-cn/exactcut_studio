# Pi + DeepSeek V4.1 Flash review

> 历史归档（2026-11-04）。正文保留当时的方案或执行状态，不代表当前版本；日常操作见[文档入口](../../README.md)。

Reviewer: pi/deepseek/deepseek-flash, high. Agent: f015d6cd-7e86-46fb-85be-1b1e150a74d1.
Scope: current working tree; analysis only. Findings below are reviewer recommendations, not completed fixes. Untracked files are expected in this WIP and must be included before release. Do not relax verified API contracts based on speculative response variants. Global-lock findings describe throughput limitations, not deadlock. Automatic-position overlap depends on actual title height and text extent.

审查完成（只读，未改动任何文件；未读取 `.env`/`.dev-state`、未联网、未调用接口、未重启服务）。范围按你给的清单，`git diff HEAD` + 新增未跟踪模块 + 相关测试/文档。`docs/local-dev-services.md`、`source/experiments/`、`docs/archive/design/2026-10-16-mosaic-tracking-research.md` 等按"无关脏文件"忽略。

---

# P1

## P1-1 `position='auto'` 时 Hook 与左上角剧名艺术字重叠，而"新水印"完全不在定位算法视野内

- **文件行号**
  - `source/exactcut/renderer.py:227`：`if hook_floor and options is not None and options.position == 'top':`
  - `source/exactcut/hook_preview.py:55`：`if request.hook_options.position == 'top':`
  - `source/exactcut/brand_overlay.py:203-207`：`raise_top_hook(decision, position, floor)` → `if position != 'top' ... return`
  - 根因：`source/exactcut/hook_presentation.py:118-124`（`select_position`）与 `:132-135`（`placement`），`source/exactcut/hook_visual.py:463`
- **触发条件**：勾选"剧名"（默认 `title_mode` 被 UI 置为 `cover`），且 Hook 位置为默认 `position='auto'`（`hook_options.py:24` 默认就是 auto），最终 placement 解析为 `top`。开发环境必然走 `placement()` 的 `default = {'position':'top', 'y_ratio':POSITIONS['top']}`（`hook_presentation.py:134-135`，`environment() != 'production'` 或 emphasis 非 AI 时都是这个分支）；生产环境 `select_position` 采样的是**原片画面**，水印是后叠加的，所以打分器看不到剧名/logo，选 `top`（与人物重叠最小）反而是高概率结果。
- **影响**：剧名艺术字（1080×1920 下占 x 54–354、y 42–≤222，`brand_overlay.py:184-186`）与 Hook 文本块（`y_ratio=.07` → y≈134，`POSITIONS`，`hook_visual.py:463`）直接重叠；由于两处合成顺序都是"先 Hook 后 brand"（`renderer.py:259-264`、`hook_preview.py:67-72`、`segment_effects.py:136-137`），成片里剧名会盖住 Hook 首行字符。且需求明确"制作直接用、无人工预览确认"，这级缺陷不会被人工拦住就进交付视频。`hook_floor`/`raise_top_hook` 这套保护机制在**默认配置下等于死代码**。
- **具体证据**：`POSITIONS = {'top':.07,...}`（`hook_presentation.py:19`）×1920 = 134px，落在标题带内；`raise_top_hook` 只比较传入的 `position` 字符串，而调用方传的是**用户原始设置** `options.position`，不是解析后的 `decision['placement']['position']`；`test_brand_overlay.py:98-104` 只测了把字面量 `'top'` 传进去，没有覆盖 `auto→top` 情形。
- **建议**：`raise_top_hook` 改为按 `decision['placement']['position']`（缺省回退传入值）判定；或在 `_apply_hook`/`hook_preview` 里统一用解析后的位置。补一条 `position='auto'` 且解析为 top 的回归断言。附带一致性问题：抬高后 `placement['position']` 仍是 `'top'`，与 `y_ratio` 不一致，建议同步记录 `raised_by_brand=True`。

---

# P2

## P2-1 缓存/批次 pin 指向的剧名图片丢失或损坏时永久失败，且提示"请重新提取"但无任何可执行的重新提取入口

- **文件行号**：`source/exactcut/title_image/extraction.py:217-221`（离线快路）、`:238-242`（在线身份命中分支）、`:277-279`（pin 命中分支）、`asset_file()` `:117-128`（抛 `ValueError`）。
- **触发条件**：`<data root>/title-assets/<asset>/title.png` 被删除/截断（数据根从部分备份恢复、运维清理、历史 `store_asset` 竞态留下的半写文件），而 `<drama_id>.json` 或批次 `production/title-assets/<identity>.json` 仍然存在且身份匹配。
- **影响**：`asset_file()` 抛错 → `extract()` 不会 fall-through 去重新生成 → `prepare_manifest` 抛错 → 整批 `execute_batch` 在渲染前失败；每次重试都命中同一份坏缓存，**永不恢复**，只能由运维手工删除缓存 JSON 或 pin 文件。错误文案"请重新提取"在 UI 上没有对应操作（`web/app.js` 只暴露两个开关）。
- **证据**：`:240-242` 在身份匹配后直接 `return`，`asset_file` 的异常未被捕获；`prepare_manifest`（`:277-282`）同理，pin 存在就只做校验不复原。`test_title_extraction.py:28-36` 只断言"检测到变化"，未覆盖"检测到后能自愈"。
- **建议**：`asset_file` 失败视为缓存失效（`latest` 擦除后重新生成；pin 场景保留 pin 但重新 `extract(force=...)` 后回写），并在错误信息里给出可执行动作；补一条"缓存文件被删后再次调用会重建"的测试。

## P2-2 打包完整性：logo 二进制资产既未纳入 git，也未纳入 `package-data`

- **文件行号**：`source/pyproject.toml:37`（只新增了 `"title_image/*.mjs"`，没有 `"assets/brand/*"`）；`source/exactcut/brand_overlay.py:14`、`:77-80`（`ASSETS = Path(__file__).parent/'assets'/'brand'`，缺失即 `ValidationError('logo 原图缺失，未生成成片')`）。
- **触发条件 / 影响**：
  1. `git status` 显示 `?? source/exactcut/assets/`、`?? source/exactcut/brand_overlay.py`、`?? source/exactcut/title_image/`、`?? source/exactcut/dev_fetch.py`。当前本机发布路径是 `operations/controller/ops.py:250-256`（`git status --porcelain` 必须为空 → `git archive <commit>`），**未跟踪文件不会进 release**；而 `models.py:9` 在模块导入期就 `from .brand_overlay import BrandOverlay`，一旦漏提交，整个应用起不来（不是只坏一个功能）。好在 dirty 检查和"从 release 目录跑 pytest"（`ops.py:262-268`）会把漏提交/漏资产拦在发布前，因此这不是静默事故，而是**发布阻塞**风险。
  2. 但 `package-data` 缺 `assets/brand/*` 是**静默**的：任何 `pip install`/wheel 路径（`docs/archive/linux-portability.md:46` 记录过 wheel 构建并核对包内资产）装出来的包没有 `assets/brand/*.png`，logo 只在渲染期才炸，且是逐素材失败（`render_request` 在 base cut 之后才调用 `render_brand`，`renderer.py:436-439`）。
- **证据**：`ls source/exactcut/assets/brand` → `loreker.png`、`reelai.png` 两个未跟踪文件；`pyproject.toml:37` 无对应 glob；`brand_overlay.py:79-80` 是唯一的失败点，测试 `test_brand_overlay.py:78-89` 恰好用到真实 logo，所以从 release 目录跑测试能兜住 git-archive 路径。
- **建议**：`git add` 资产并加一条 `exactcut = [..., "assets/brand/*"]`；再加一个"包内资产齐全"的断言（例如 `test_brand_overlay` 里断言 `ASSETS` 下两个文件存在并可被 `importlib.resources` 解析），避免只测工作区。

## P2-3 剧名提取用了一把**全局** flock 覆盖整段网络 I/O（含 240s 级图像 POST），内联预览线程也抢这把锁

- **文件行号**：`source/exactcut/title_image/extraction.py:228-232`（`root().mkdir()` 后 `flock(LOCK_EX)`，锁内创建 `httpx.Client` 并完成 KES 登录/列表/封面下载/`/images/edits`）；`source/exactcut/hook_preview.py:50-53`（预览里直接 `extract()`）；`source/exactcut/webserver.py:1641`（`compute(...)` 在 HTTP 请求线程内执行，`ThreadingHTTPServer` 见 `:1904`，准入上限 `concurrency.hook_previews=3`）。
- **触发条件**：两批不同剧（甚至同一剧两个批次）同时开始制作，或一个人在制作中另一个人点 Hook 预览；`allow_live_requests=true` 时每次新剧都要走网络。
- **影响**：所有剧的提取被串行化；持锁时间 = KES 往返 + 图像编辑（`timeout_seconds` 默认 240）≈ 分钟级。批次在 `prepare_manifest`（`orchestrator.py:118`）阶段就阻塞，且此时 worker 已持有 `cutting_access.gate`（`workflow.py:218-222`）与执行心跳。预览侧会让 HTTP 线程在同名锁上阻塞到分钟级，占用 `hook_previews` 名额（3 个即可让所有人预览排队）。锁序是 pin→global，无反序，**不存在死锁**，但存在明显的串行放大。
- **证据**：锁粒度是 `root()/generation.lock` 单文件（无按剧分片）；图像 POST 也在锁内（`:247-252`）；`extract` 里没有 `force` 之外的取消/超时中断点。
- **建议**：按 `drama_id` 分片加锁（`generation-<drama_id>.lock`），把"先查询、再算身份、命中即返回"放在短暂锁内，图像 POST 放到锁外并在落盘时用 `os.replace` + 二次身份校验（`store_asset` 已按内容哈希幂等）；预览路径改为"缓存未命中就明确告知需先制作一次"或异步化，避免占满预览名额。

---

# P3

1. **`drama_id` 未归一化就写进 `title_drama_id`**：`intake.py:553` 做了 `.strip().strip('#')`，但 `brand_overlay.py:40,65` 用原始值。触发：用户输入 `" 101607"` 或 `"#101607"`（`web/app.js` 不裁剪）。影响：制作路径正常（`prepare_manifest` 用 `manifest.drama_id` 再回填，`extraction.py:283`），但 **Hook 预览**走 `request.brand_overlay.title_drama_id` → `check_drama` 报"剧 ID 必须为六位数字"（`extraction.py:54-57`），提示指向错误；若草稿里已有 `title_asset`，`parse` 的 `asset_file(asset, 原始ID)`（`brand_overlay.py:44-49`）会报"与当前剧 ID 不符"从而**拒绝保存草稿**。建议 `parse()` 用归一化后的 drama_id。
2. **cover 模式仍强制要求 `drama_title`，且 `brand.title` 参与了指纹与 pin 身份**：`brand_overlay.py:36,50-51`；`orchestrator.py:62-66`；`extraction.py:271`。cover 模式的文字其实来自 KES 的 `row['name']`（`extraction.py:235`，`prompt(title)`），`brand.title` 不参与渲染，却会让"改剧名"触发全量重渲染（指纹变化）并生成一份多余的 pin（`title-assets/<new identity>.json`）。建议 cover 模式下指纹只取 `title_asset`/`title_drama_id`（或对 `title` 做归一化忽略）。
3. **凭据读取有两处越界回退**：`extraction.py:50-51` `values.get(key, os.getenv(key,''))`，指定的 `credentials_file` 缺项时会静默采用**进程环境**里的 `OPENAI_API_KEY`/`KES_*`，与文档"credentials are read locally, not injected into the process environment"不完全一致（`docs/archive/verification/title-extraction-2026-10-26.md`）。同理 `util.py:70` 用 `setdefault`，`dev_fetch.py:19` 载入的专用 dev env 文件不会覆盖环境里已有的 `ALIYUN_*`（`bin/dev-web:4-9` 只清理了 `bin/dev-web` 自身的环境，手工直跑 `python -m exactcut.dev_fetch` 则没有这层保护）。建议缺项即失败，或显式 `os.environ[key]=value`。
4. **dev 只读下载适配器复制了 hub 的键/集号推导**：`dev_fetch.py:24` 硬编码 `f'assets/{drama}/video/{episode}.mp4'`，而 hub 用 `target_key()`+`episode_label()`（`integrations/drama-material-hub/scripts/material_hub.py:2394-2413`）。`episode_label` 会把 `ep01`/`1` 规范成 `EP1`，`check_episode`（`source_cache.py:40-43`）却只接受大写 `EP1`——一旦上游把集号写成小写，dev 下载会报"无效集号"或键不存在。另：`hub_script()` 指向仓库根 `integrations/...`（`source_cache.py:155-156`），脱离源码树（wheel/纯包安装）即不可用；`dev_fetch.main` 对 `sys.argv[1:]` 长度不符抛 `ValueError`、对未设 `EXACTCUT_DEV_FETCH_ENV` 抛 `KeyError`（`:16,19`），无友好提示。建议复用 hub 的 `episode_label`/`target_key`，并对参数/环境变量做显式校验。
5. **`dev-web` 参数顺序敏感**：`bin/dev-web:16-35` 顺序解析，`--fetch-env` 必须写在 `--title-images`/`--cutting` 之后；反序只得到 usage 报错。另外 `:48` 在 `:54` 定义 `DEVELOPMENT_PYTHON` 之前就用了同一表达式（当前值一致，属重复而非错误）。建议用 `while [[ $# -gt 0 ]]` + `case` 解析。
6. **遗留 Gemini 剧名实现被一并打包/测试**：`exactcut/title_image/__init__.py`（`generate_gemini_image.mjs`、`GEMINI_API_KEY`、`node` 子进程、`.env` 读取）与 `config/default.json` 里的 `title_image` 段，产线代码路径完全不使用（新路径只用 `title_extraction` + `extraction.py`），却拥有一个与 `title_extraction` 极易混淆的配置命名空间，并由 `tests/test_title_image.py` 固化。建议删除或在文档中明确标为实验遗留，避免有人改错配置段。
7. **KES 登录信封与响应断言偏脆**：`extraction.py:149-151` 用 `content-type: application/x-www-form-urlencoded` 头却发 JSON body（能通过仅因服务端不校验）；`:166` `data.get('has_more') is not False` 对 `0`/缺字段极其敏感。二者都标注"真实响应已验证"，因此不算缺陷，但建议改成 `in (False, 0)` 并在注释里记录真实响应样例。
8. **无 Hook 且无字幕特效时多了一次全片重编码**：`renderer.py:182-196`（`_burn_brand`）与 `:206-208`。改前该分支是 `shutil.copy2` 直接拷贝，现在为了压 logo 重新编码一遍（同 crf/preset 的二次损失 + 时间成本）。建议评估用 `-c:v copy` 无法叠层时是否可接受"仅在 base cache 阶段就把 brand 合进 base"，或至少加 `-fps_mode passthrough` 与时长断言（`_burn_brand` 目前没有像 `apply_effects` 那样的帧数校验，`:140-142`）。
9. **`raise_top_hook` 的视觉副作用未覆盖**：`hook_visual.py:463-474` 的 scrim 从 `y_start - 0.7*size` 开始，抬高到 floor 后仍可能压到标题最后一行（1080 宽、两行文字模式时 floor≈0.086 → scrim 顶≈130px，标题底≈142px），夜间/深色画面上表现为标题底部被压暗。建议把 scrim 上界也纳入 floor 计算。
10. **`store_asset` 的临时文件名不唯一**：`extraction.py:108-111` 用固定 `title.tmp`。当前只靠 `generation.lock` 串行才安全；一旦按 P2-3 建议把图像 POST 移出全局锁，这里需要改成 `tempfile.mkstemp(dir=folder)`。另 `asset_file` 不重校验 alpha，全透明 PNG 会在 `brand_overlay.py:180` 的 `getbbox()` 上抛 `TypeError`（未包装成 `ValidationError`）。

---

# 你点名的三项专项检查

1. **未跟踪模块遗漏打包**：见 P2-2。要点：`brand_overlay.py`/`title_image/`/`dev_fetch.py`/`assets/brand/*.png` 全部是 `??`；`git-archive` 发布路径 + `models.py:9` 的顶层导入意味着漏提交即全站导入失败（会被 `ops.py:250` 的 dirty 检查挡住，属"发布阻塞"而非静默）；`package-data` 只补了 `title_image/*.mjs`，**没补 `assets/brand/*`**，这是唯一静默的漏洞（面向 wheel/pip 安装）。
2. **重试资产引用**：设计成立且有测试保障——pin 落在 `production/title-assets/<identity>.json`（`extraction.py:271-282`），重试时 `_produce` 从 `confirmed.json` 重建 manifest（`workflow.py:1098`，不含 `title_asset`），`prepare_manifest` 命中 pin 即复用，`test_title_extraction.py:59-78` 断言两次 `prepare_manifest` 只调一次 `extract`。风险只在 P2-1（pin 指向的资产文件本身丢失/损坏时不可自愈）和 P3-2（改名导致 pin 身份漂移，会多生成一个 pin，但内容一致、不重复调用 API）。
3. **结尾最后帧污染**：**未发现污染**。brand 只作用于 base（plot）：有结尾时 `hook_output = work/'hooked.mp4'`（`renderer.py:435`），结尾卡从 `request_base`（未叠 brand）取最后一帧（`renderer.py:455`），再 `_concat_units([hook_output, unit])`（`:469`）；无结尾时输出本身就是 plot。`apply_effects` 用 `add_overlay(brand, 0, plan['total_frames'])` + `enable='lt(n,start+count)'`（`segment_effects.py:136-137`、`:108-109`），`_apply_hook`/`_burn_brand` 都用 `-t total_s` 且带 brand 时**跳过了 `try_hook_splice`**（`renderer.py:271`），所以"未改动的尾部被拼接复用"这条路已被正确关闭。唯一可讨论点是结尾卡背景 = plot 最后一帧**去掉**水印，等于水印在切点处瞬移消失（视觉跳变），但这正是"结尾无标题与 logo"的需求，不当缺陷计。

---

# 已核对通过、不计缺陷的项

- 两个开关默认关闭：`BrandOverlay` 字段默认 `False`（`brand_overlay.py:18-19`）、UI 未勾选（`web/app.js:85`）、`title_mode` 默认 `'text'`；两个开关独立（`test_brand_overlay.py:44-61`）。
- 关闭时不改旧指纹：`orchestrator.py:62-66` 先 pop 再按需加回，`test_brand_overlay.py:27-41` 用"删掉 brand_overlay 的旧 dump"验证等价。
- 既有 text 配置保留、不在艺术字上叠白字：`brand_overlay.py:176-197` 是 `cover` / `text` 互斥分支，`parse` 里 `title_on` 为假时强制 `title_mode='text'`（`:63`）；cover 模式**不会**静默回退成白字（`test_title_extraction.py:52-56`，`decode_response`/`validate_image` 强制真实 alpha）。
- 位置与尺寸：1080 下 (54,42)、等比限制 300×180、logo 底部居中距底 110（`brand_overlay.py:166-186`，`test_brand_overlay.py:78-95`）。
- 缓存判据：封面 hash + model + prompt_version + prompt_sha + quality（`extraction.py:236-242`），`test_title_extraction.py:87-123` 覆盖"换 quality 才重跑、改标题才重跑"。
- TLS 局部覆盖：只改 KES 主机的连接地址，保留 `Host` 与 `sni_hostname`（`extraction.py:186-198`）；已核对 httpcore 1.0.9 的 `_sync/connection.py:107,151` 用 `sni_hostname` 作为 `server_hostname` 传给 TLS，**证书校验对象仍是原域名**。
- 重试范围：`fetch_cover_with_retry` 只包住查询+下载（最多 3 次，`extraction.py:201-208`），`/images/edits` 的 POST 在包装之外，不重试（`:247-252`），与需求一致。
- 前端 `hidden` 生效（`index.html:3` 有全局 `[hidden]{display:none!important}`，所以 `.brand-logos[hidden]` 不会被 `display:flex` 覆盖）；`controls()` 已把 `[data-brand]` 纳入只读禁用（`web/app.js:35`）。
- 进度阶段两处同步：`progress.py:4`、`web/app.js:8`。
- 面板/日志/资产元数据未写入凭据；`asset.json` 只含 `cover_url`/`prompt`/`title`/哈希，`poster_url` 限制同源 https、禁 query/fragment/`..`（`extraction.py:60-69`）。

**结论**：无"必须立刻回滚"的阻塞级缺陷，但 **P1-1（默认 auto 定位下 Hook 被剧名遮挡，且无人工确认门）在实际交付路径上会出现**，建议在发布前修掉；P2-1（缓存不可自愈）、P2-2（`assets/brand/*` 未进包数据 + 未跟踪资产）、P2-3（全局提取锁）建议同批处理。


## 修复复查结果

2026-10-26：同一 Pi + DeepSeek V4.1 Flash reviewer 复查确认字幕叠层、损坏缓存和pin恢复、每剧生成锁、logo包数据全部解决。补修直接引用恢复后再次确认B已解决、未发现新引入功能性问题。另补Unicode损坏元数据恢复；28项相关测试通过，wheel实际包含两张logo及新增模块。首页参考图内联属于另一路界面修改，未包含于本轮；无用hook_floor参数为非功能清理项。用户现有视频未重跑，生产未发布。
