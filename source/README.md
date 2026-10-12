# Exact Cut FBBM

> 2026-10-10：本文网页入口示例统一为新默认 `/home`；历史验收记录不代表该路径已经上线。现有生产 runtime 与 nginx 的迁移由部署方处理，本次仅修改开发代码和文档。Cookie Path 改为 `/home/` 后，已登录用户需重新登录一次。

当前开发、发布、备份及回滚以 [开发发布与回滚](docs/开发发布与回滚.md) 为准。生产使用固定发布目录，源码修改必须在独立开发库完成。

轻量化、表格驱动的短剧精确剪辑交付工程：输入填写表和完整物料，按 Excel 时间线精确剪辑，完成格式与时长检查、标准命名、OSS上传、飞书排队以及CF-Ads/Facebook BM同步。

## 完成判定

只有以下条件同时成立，任务才是 `fbbm_ready`：

1. 按 Excel 指定顺序和时间范围渲染完成；
2. 成片格式、时长和首帧检查通过；
3. OSS 上传及飞书排队完成；
4. CF-Ads 返回 `facebook_bm_sync_status=ready`，且 Facebook 视频 ID 已回填。

本工程为纯剪辑流程，不调用 Gemini/GPT 审查，不自动删除或改选镜头，也不依赖镜头索引。网页流程仍需先预览确认，再上传素材库。

## 安装

```bash
cd /Users/admin/Desktop/codex/exact-cut-fbbm
./bin/setup
cp .env.example .env
```

填写 `.env`，并确认系统已有 `ffmpeg` 和 `ffprobe`。

首次连接新的飞书多维表格时，先检查字段；确认计划后再正式补齐：

```bash
./bin/exactcut --env-file .env ensure-feishu --dry-run
./bin/exactcut --env-file .env ensure-feishu
```

## 项目物料结构

每部剧统一存放在工程根目录的 `projects/<剧ID>/` 下：

```text
projects/
└── 101460/
    ├── project.json                 # 剧ID、剧名、授权与排除剧集
    ├── source/videos/              # 正片：EP1.mp4、EP2.mp4……
    ├── requests/                   # Excel 与编译后的任务清单
    │   ├── request.xlsx
    │   └── YYYYMMDD.manifest.json
    └── outputs/YYYYMMDD/           # 成片、失败隔离、审计与结果
```

本地已建立 `projects/101460/` 示例目录，复制了示例配置和 Excel；正片、镜头索引和输出目录待填充。新增其他剧时，以真实剧 ID 命名目录，并同步填写 `project.json` 与 Excel 中的剧 ID、剧名等信息。运行命令时，`--project` 指向这一部剧的目录。

正片齐全后即可直接执行剪辑。`prepare` 命令仅作为可选的镜头索引工具保留，不参与正常制作流程。

## 表格与命名

新表头仅填写剧ID与出表人，日期在制作时按北京时间当天自动确定，剧名不再必填。已锁定的制作清单重跑沿用原日期。新填写推荐使用 `templates/标准剪辑需求_模板_v2.xlsx`：一份文件对应一部剧、一个批次，每行一条成片，每格一个片段。文件包含空白「剪辑填写」与「填写规则与示例」两页，预留20条需求、每条8个片段，附基础必填及重复编号检查。基础检查不代替编译和物料校验。

标准片段统一使用 `EP1 00:05-01:49`、`EP2 全集`、`EP5 01:22-片尾`、`字卡|One hour earlier|1.5s` 四种写法。视频时间为 `MM:SS`（需要毫秒时为 `MM:SS.mmm`），同集多段分别填格，不使用加号或双逗号。旧编导表的个性化写法先按 `docs/编导Excel转换规则.md` 解读，再转换成标准输入。

模板位于 `templates/精确剪辑_FBBM_填写模板.xlsx`。

“出表人”就是FBBM命名中的 Creator 1。例如出表人为张三：

```text
20260902_typezxraw_张三_AGENT_1_#101460#.mp4
```

片段填写示例：

```text
EP1 15s-2m4s
EP2 00:55-01:19
字卡|just a few minutes ago|1.5s
```

片段列严格从左到右执行；重复填写同一区间表示有意回溯。

## 校验与执行

```bash
./bin/exactcut compile \
  --project projects/101460 \
  --input projects/101460/requests/request.xlsx

./bin/exactcut validate \
  --project projects/101460 \
  --input projects/101460/requests/request.xlsx
```

只生产并检查成片：

```bash
./bin/exactcut --env-file .env execute \
  --project projects/101460 \
  --input projects/101460/requests/request.xlsx
```

生产、上传并等待FBBM真正就绪：

```bash
./bin/exactcut --env-file .env execute \
  --project projects/101460 \
  --input projects/101460/requests/request.xlsx \
  --publish --wait-ready
```

生产环境写入前可先验证OSS/飞书计划：

```bash
./bin/exactcut --env-file .env execute \
  --project projects/101460 \
  --input projects/101460/requests/request.xlsx \
  --publish-dry-run
```

单条重跑：

```bash
./bin/exactcut --env-file .env execute \
  --project projects/101460 \
  --manifest projects/101460/requests/20260902.manifest.json \
  --request-id 20260902-01 --force
```

## 输出

```text
projects/101460/outputs/20260902/
├── videos/
├── failed/
├── audit/<request_id>/
│   ├── clip_plan.json
│   └── render_qc.json
└── results.json
```

真实密钥只放 `.env`，不要提交到Git或写进表格。

## 静态片尾

每条素材的剪辑时间线下方有「结尾设置」：默认不添加，可选「素材最后一帧」或「上传海报或图片」。片尾在剧情结束后追加，默认 3 秒，可设为 1～15 秒，音轨静音。最后一帧取剪辑后剧情的实际末帧；上传图片支持 PNG、JPEG、WebP，最大 4 MB、3200 万像素，完整显示，比例不同补黑边。

引导文案选填，支持手动换行，最多 200 字；以白字黑描边放在画面下方，自动适配字号，已编辑好的海报可以不另加文字。预计总时长和成片质检包含片尾时长。保存并确认制作后，在现有成片预览中查看最终效果。

已保存的旧素材默认不开启片尾，包括旧版只保存图片的配置。确认制作时，图片按内容校验并保存独立快照；后来替换图片不影响已确认版本。修改片尾可复用剧情剪辑缓存。

制作清单保持 `schema_version: 1`，请求新增可选 `ending`：`mode` 为 `none` / `last_frame` / `image`，`duration_ms` 默认 `3000`，`guide` 为引导文案，`image` 为图片文件名。网页确认时将图片快照保存至项目 `assets/endings/`，制作清单引用其 SHA-256 文件名。

## Hook 视觉样式

网页「标准化内容」中，每条素材的 **标题字幕（Hook）** 下方的「字幕设置」中先选择风格，再展开「自定义字体与颜色」调整内置字体、文字颜色、强调颜色和字号。字号按 1080 宽画面设置（32～96），留空时自动适配；「清除自定义，恢复风格默认」可清除自定义设置。按 Enter 换行，最多三行；指定字号放不下时会提示调整，不截断文案。点击面板底部的「保存并预览字幕」查看效果，正式制作复用同一套设置。

Hook 文案仍从 Excel 的「标题EN/Hook」列读取，不自动生成或改写。可在单元格内换行指定上下两行，用 `**重点词**` 标记强调短语；星号不会出现在成片中。默认最多三行排版，沿用各样式字体和配色；按实际字体宽度自适应字号，超长文案报错，不截断文案。描边随字号缩细，减弱光晕和阴影，避免字母空隙被填满。

默认 `render.hook_style=auto`：根据输入文案的关键词选择风格，并根据成片首帧顶部的冷暖色、亮度和对比度调整配色与衬底。没有匹配信号时使用 `default`。普通 Hook 默认显示 4.5 秒，最后 1 秒淡出；短视频将显示时长限制到实际片长。字体随应用打包，缺失时报错，不使用宿主字体回退。

| 配置值 | 风格 | 字体与视觉 |
| --- | --- | --- |
| `neon_reversal` | 霓虹反转 | 内置 DejaVu Sans Bold Oblique，紫粉描边及发光 |
| `danger_warning` | 危险警告 | 内置 DejaVu Sans Bold，红/黄重点和厚黑描边 |
| `ceo_luxury` | CEO 奢华 | 内置 DejaVu Serif Bold，奶油白与金色 |
| `emotional_diary` | 情绪日记 | 内置 DejaVu Sans Bold，暖白与粉色 |
| `cold_mystery` | 冷感悬疑 | 内置 DejaVu Sans Bold Oblique，青蓝/紫色重点 |
| `raw_conflict` | 原生冲突 | 内置 DejaVu Sans Bold，黄/红重点和厚黑描边 |
| `default` | 默认清晰 | 白字、橙色重点、黑色描边 |

可新建配置文件，例如 `hook-config.json`，固定本批次风格：

```json
{
  "render": {
    "hook_style": "cold_mystery",
    "hook_duration_ms": 4500,
    "hook_fade_out_ms": 1000
  }
}
```

通过 `./bin/exactcut --config hook-config.json execute ...` 使用。`--config` 位于子命令之前。选择结果、首帧分析、配色和展示时长保存到 `audit/<request_id>/clip_plan.json` 的 `hook_visual` 字段。

## Linux 容器部署

Linux 的环境隔离、生产候选配置、字体与文档转换边界见 [Linux 运行说明](../docs/linux-portability.md)。镜像包含素材下载脚本，不需要 macOS、系统字体、textutil 或 Cloudflare Tunnel。首期 nginx 沿用80端口，提供 IP 的 `/home/` 入口，后端仅监听本地 :18792；开发 :8793。首期仅维护开发、生产两份配置，配置与数据独立，生产启用下载、AI、飞书/FBBM 发布。Linux正式版本已上线，公网地址为 http://124.156.228.202/home/ 。登录与工作台、真实多集下载/制作、AI Hook、QC和缓存复用验收通过；真实业务上传与飞书/FBBM最终发布仍需预览确认。版本与证据见[上线记录](../docs/server-deployment-2026-10-09.md)。

本地开发使用独立配置，不读取旧 `.env`：

```bash
cp .env.development.example .env.development
EXACTCUT_PYTHON=/Users/matao/miniconda3/envs/py3.12/bin/python3.12 ./bin/dev-web
```

实际生产凭据放在服务器 `<部署根>/config/production.env`，默认 `/srv/exactcut/config/production.env`。主机目录、后端端口、实例名和资源限制可指定，换机器不修改应用代码。外部检查与迁移顺序见 [Linux 部署与迁移](../docs/linux-migration.md)。开发和预发布仅使用独立夹具。上文 `.env` 与 Mac 安装命令属于历史流程，不用于 Linux 新部署。

## 历史 macOS 网页控制台部署

访问地址：https://cut.oulagrowth.com 。控制台由本机 `exactcut.webserver` 提供，监听 `127.0.0.1:8790`，通过独立 Cloudflare Tunnel 接入 HTTPS。登录信息保存在本机 `.web-state/登录信息.txt`，不要提交或对外分享。

控制台支持选择本地项目、下载模板、上传 XLSX、校验、编译、生成镜头索引、剪辑检查以及上传并等待 FBBM ready。网页直接调用现有 `exactcut` CLI，制作仅执行精确剪辑与技术 QC。任务串行执行，最多接受 10 个未完成任务；日志和状态持久化到 `.web-state/jobs/`。重启服务后未完成任务标记为 interrupted，不自动重跑以避免重复发布。网页的「操作结束」表示命令返回成功；FBBM 最终状态需以任务日志内的结果为准。

正片放在 `projects/<剧ID>/source/videos/`，镜头索引放在 `data/episodes/`；网页仅上传填写表，不上传大视频。新增项目须先在本机创建 `project.json`。主机必须保持开机、联网、唤醒且用户已登录，macOS 登录后 LaunchAgent 自动启动服务。

后台服务：

- `~/Library/LaunchAgents/com.oulagrowth.exactcut.web.plist`
- `~/Library/LaunchAgents/com.oulagrowth.exactcut.tunnel.plist`
- 服务日志：`.web-state/logs/`
- 健康检查：`https://cut.oulagrowth.com/healthz`

发布与回滚使用独立运维入口：

```bash
/Users/admin/Desktop/codex/exact-cut-fbbm-ops/bin/ops status
/Users/admin/Desktop/codex/exact-cut-fbbm-ops/bin/ops publish <已构建版本>
/Users/admin/Desktop/codex/exact-cut-fbbm-ops/bin/ops rollback
```

直接运行 `deploy/install.py` 和手工重启服务的旧部署方式已关闭。先在隔离环境开发、测试并构建版本，再通过运维入口切换。

历史 Mac 服务的登录密码保存在 `.web-state/password`；修改后需重启网页服务。旧 Tunnel 运维入口仅适用于 Mac，不用于 Linux 部署。

### 剧目候选与正片补齐

控制台候选从 Metabase 的 `toc_series`、`supply_tasks` 和订单归因剧名合并，范围为 `101400`～`999999`，只收录有真实剧名的已知 ID，不填造连续号码。输入 ID、`#ID#` 或剧名即可筛选。可点击「更新剧目候选」重新读取数据源；候选缓存为 `.web-state/catalog.json`。

选择尚未落地的剧目后，上传填写表或点击「下载／补齐正片」会创建对应本地项目。下载任务调用已有 Material Hub 的 `sync --restore-missing --only videos`，沿用前 10 集、来源回退与去重逻辑；随后把其本地正片通过硬链接（跨盘时复制）接入本项目。现有 `project.json` 不覆盖，已有正片大小不一致时报错，不覆盖。下载不自动生成镜头索引，之后可执行「生成基础镜头索引」。物料中心 partial/error 原样保留，不把部分同步描述为成功；同步通知由物料中心发送至「🤖物料D」。

## 当前工作台：Excel 到预览审核

页面已升级为批次工作流，入口为上传 Excel，无需单独选择剧 ID。

1. **导入并标准化**：从 XLSX 读取剧 ID、剧名、出表人、日期、Hook、顺序片段等；支持「片段1、片段2…」「时间线」和「集数＋开始时间＋结束时间」常见列。保留原单元格及行号。表格中多个剧 ID 自动拆分为独立批次；不明确的内容在页面报错，由用户修改。不会执行 Excel 中的公式或宏，也不会推测缺失剪辑时间。
2. **编辑确认**：标准化表可修改元信息、素材序号、Hook 和逐行时间线。保存后校验并计算正片计划。点击确认后保存不可变清单与确认记录，后续修改通过「复制为新草稿」。不同确认批次的素材文件名不可重复。
3. **下载与制作**：调用 Material Hub，默认获取前 10 集实际可用正片，只有 8 集时接受 8 集；引用 EP10 后的片段会把上限扩到最大所需集数（最多 EP500）。只扩展当前手动任务，不改变普通同步/Cron 的默认范围。所需正片齐全后直接按 Excel 剪辑，无需生成镜头索引。`deploy/enable_extended_episodes.py` 为唯一 Material Hub 实现安装可选 `--episode-limit` 参数，重复运行不重复安装。
4. **预览与反馈**：成片通过格式与时长检查后才提供带 Range 请求支持的视频预览。页面每 3 秒同步下载、渲染和格式检查阶段，显示真实完成条数。反馈按条保存到 `feedback/`，支持页面分类汇总。
5. **逐条上传 FBBM**：预览勾选并点击上传后，记录被确认成片的 SHA-256。上传前重新验证同一文件，直接上传已检查成片，绝不通过重新渲染替换已预览版本。只有 ready 且有 Facebook 视频 ID 才显示就绪；重试复用已创建的远端记录。

原 Excel、草稿、确认清单、确认审计、运行事件、独立输出和上传记录保存在 `.web-state/batches/<batch_id>/`。服务重启会标记未结束任务为中断；制作失败可重试并复用通过检查的产物，上传失败可在预览卡片继续同步。主机必须开机联网。历史命令行执行方式仍可用，但旧网页的批量直接发布入口已关闭，必须先预览确认。

### 当前工作台素材命名规则

制作前必须手动填写「编导姓名」，例如徐明飞。Excel 中的出表人仅作为来源信息展示，不自动代替本批编导；复制新草稿时也需要重新填写。页面实时展示命名预览，未填写姓名无法开始制作。

```text
{业务日期}_{素材类型}_{编导姓名}_agent_{素材序号}_#{剧ID}#.mp4
20260924_typezxraw_徐明飞_agent_1_#101460#.mp4
```

`agent` 固定小写；日期使用标准化表中的业务日期，序号取对应素材行。编导姓名不能含下划线或路径符号。已确认历史批次仍按其锁定清单中的文件名处理，不追溯改名。

「创意脚本 / 剧ID / 标题短文案 / 日期」表格也可直接导入：「标题短文案」保留为 Hook，去掉脚本中的「正文画面——」前缀，并按中文逗号分隔 EP 片段。这类创意脚本按已确认约定将 `1.52s` 解释为 `01:52`；标准片段列仍保留小数秒语义。只有集号表示全集，只有单个时间点表示从该点到片尾；实际终点在对应视频下载后读取。

每条成片的剧情片段区支持「粘贴文本识别」：粘贴集号及完整起止时间，调用现有 AI 配置解析后填入。默认追加，可选择替换；保存前可「撤销本次填入」。保留片段顺序及重复段，空白占位行不会留在已识别片段前。时间使用分:秒或分:秒:毫秒，旧编导写法 `1.52s` 表示 1分52秒。缺少起止时间、全集/片尾未给明确终点、混入多条成片或无法解释的文字会提示核对，不部分填入。一次最多 12000 字、120 行、30 段；剧情组合编辑器最多 12 段。取消识别后，迟到结果不会写入。

文本解析接口：已登录且带 `X-Exactcut-Request: 1` 的 `POST /api/clip-text`，JSON 请求为 `{"text":"第30集 1分20秒到1分47秒"}`。响应为 `{"clips":[{"episode":"30","start_m":"01","start_s":"20","start_ms":"000","end_m":"01","end_s":"47","end_ms":"000"}],"errors":[]}`；识别存在问题时 `clips=[]`，`errors` 包含中文问题说明。接口只解析文本，不保存或启动制作。复用 Excel 理解的模型、API 地址、AI 开关、限流与并发槽位；测试使用模拟响应，不接触生产凭据。

整集写法已支持：`EP4`、`EP4 全集` 或 `EP4 整集` 都表示从该集开头使用到结尾。确认清单保留整集意图，下载完成后用 ffprobe 读取实际时长，生成 `production/resolved.manifest.json` 记录最终起止点。单个时间点（如 `EP5:01:22`）表示从 01:22 到片尾，并保留起点；起点不小于实际片尾时会报错。

编导格式的详细约定见 `docs/编导Excel转换规则.md`：`+` 拆为同集独立选段，重复与回溯不重排；只修正已确认的 `1..44s` 笔误并记录来源。缺失结构、Hook、日期保持空白。多剧原文件副本与含「片段1、片段2…」字段的 `normalized.json` 保存在 `projects/<剧ID>/requests/import-<batch_id>/`，导入归档不会创建授权配置或触发制作。未知转场文字、旧素材文件引用继续标注待确定。

首次进入工作台按账号创建默认项目并直接进入编辑器，随后恢复服务器记录的上次项目；网页已移除Excel导入入口。规则及迁移状态见[首次进入与项目恢复](../docs/workspace-startup.md)。
