# 2026-11-07 线上报错与用户反馈问题：修复核对及真实数据复验

状态：只读取证与复验已完成。线上实际运行 `us-east-20261107-first-load-f40e59f`（commit `f40e59f`，比我 12:09 发布的 `824cfb5` 更新，15:49 由首屏优化候选发布）。本轮**未改代码、未改配置、未重启服务、未制作或替换任何成片、未执行上传**。

## 一、最近线上报错（生产日志实读）

查询方式：`docker exec exactcut-production-web-1 python -m exactcut.diagnostics --hours 72 --errors --limit 400`，共 253 条（21 error / 232 warning）。按类型归类：

| 时间（UTC） | 事件 | 数量 | 实际内容 | 性质 |
| --- | --- | ---: | --- | --- |
| 11-04 08:59 – 11-06 10:30 | `http.rejected` publish 409 | 48 | “项目已在其他页面更新，请刷新后重试”，涉及 producer01–07 多个真实账号；`98e3905d` 在 10:38–10:44 连续 10 次 | 真实用户操作撞 CAS 守卫 |
| 11-06 03:13 – 07:56 | `http.failed` `/originals/EP25/file` | 11 | `ChunkedEncodingError` / `IncompleteRead(33717782 bytes read, 79363511 more expected)`——OSS 上游流传中断，未捕获 | **真实缺陷（未修）** |
| 11-06 05:41 等 | `source.fetch_aborted` | 5 | `EP2.mp4.part 正片尚不完整或无法读取`，cause=ValueError | 尾帧误判，已由 `489c5dd` 修复 |
| 11-04 09:00 | `python.log` error | 1 | `Runner tick failed` → `ValueError: 此项目已纳管，必须使用素材版本操作` | 内部 tick 抛错（未修，见第四节） |
| 11-06 05:55–09:20 | `/healthz` 503 | 6 | 均在整 5 分钟（监控 cron），说明 worker 心跳 >30s 或连续 tick 失败 ≥3 | 健康抖动，未定位根因 |
| 11-04 10:22 / 11-06 06:26 | `hook_preview.compute.failed` | 2 | `ValidationError: 这条素材没有 Hook，无需生成 Hook 预览`（耗时 2ms） | 校验拒绝被记成 error 级 |
| 11-06 04:33–08:43 | `render.failed` | 1 | `AI 未生成合规重点，请重试或改用手工选词／不强调；未修改原文` | 业务校验，有明确用户提示 |
| 11-07 06:23–06:38 | mosaics 409 | 4 | “素材已提交剪切打码…”“当前成片已变化，请刷新后重新编辑 | 验收测试期间的守卫 |
| 11-07 06:48–07:31 | `preview_prepare_failed` 503 + `media_error` | 4 | 项目 E `cda2f8a9` 预览 OSS 校验失败 | **验收时人为注入的失败**，非真实故障 |
| 11-07 07:53 | 404 `batches/000…0` | 1 | 32 个 0 的项目 ID 请求 | 来源未明，发布后 4 分钟，1 次 |
| 11-07 全天 | `browser.event network_error` TypeError/AbortError | 90+ | 集中在 `batches.detail`/`batches`/`workspace`/`batches.sources`，多为 20s 超时中止 | 与首屏慢同源，见第三节 |

`media_error`（7 次，`network_state=3`、`media_error=4`）出现在项目 A/E，与 E 的注入失败及验收中的替换同批，未独立复现为播放缺陷。

## 二、用户反馈问题与修复记录

| 用户反馈 | 修复提交 | 记录 |
| --- | --- | --- |
| 原片下载后报“正片尚不完整”，实际可播放（EP2 音频尾部比视频长） | `489c5dd` | [原片尾帧校验修复验收](source-tail-validation-2026-11-06.md) |
| 打码时间范围纠正后仍残留旧校验错误 | `3507c44` | [打码区域形状与效果](mosaic-region-shapes-and-effects.md) |
| 首次打开/刷新项目要 21–27 秒 | `a58a672`（线上 `f40e59f`） | [首屏排查](first-load-latency-2026-11-07.md)、[本地优化](first-load-optimization-2026-11-07.md)、[上线验证](first-load-production-2026-11-07.md) |
| 项目页剪切/打码进行中仍显示“成片已就绪” | `b5812ab` | 人工清单第 12 节 |
| 重复 session cookie 导致登录循环、原片预取进度不可见 | `a8a6bff` | 人工清单第 13/10 节 |
| 忽略/取消选择、旧草稿效果继承与时间轴交互 | `0031322`、`b50b3cc` | [阻塞修复记录](release-blocker-fixes-824cfb5-2026-11-07.md) |
| 多个真实账号发布时反复 409 | 未见修复提交 | 本轮仅取证，见第四节 |

应用内“预览有问题”反馈库（`data/feedback/`）当前为空，以上来自会话反馈与验收文档，不是页面反馈条目。

## 三、用当时出错的数据复验

### 1. 原片尾帧误判（EP2）——已修复，红绿对照成立

- 数据：当时报错的原始文件 `EP2-original.mp4`，23,915,857 字节，SHA256 `ace5495…9ab62`，上传到 `/srv/exactcut/preparation/regression-20261107/`（0600）。
- 方法：同一文件只读挂载，分别在**旧线上镜像** `us-east-20261031-…-8216c27` 与**当前线上镜像** `…first-load-f40e59f` 的容器内调用各自 `material_download.validate_downloaded_video`，禁用网络。

| 镜像 | 结果 |
| --- | --- |
| `8216c27`（报错时版本） | FAIL `正片尚不完整或无法读取`（0.162s） |
| `f40e59f`（当前） | PASS（0.559s） |

### 2. 首屏慢——已修复，真实浏览器实测

同一项目 `bf401b953fb04aeaaef9292c43f4dd52`，真实 Chrome、当前构建 `app.js?v=1272ff3492fe3a82179a`，取项目详情 `responseEnd`：

| 样本 | 项目可用 ms | load ms | 关键请求顺序 |
| --- | ---: | ---: | --- |
| 修复前（历史记录） | 21049 / 26618 | 1167 / 554 | workspace → sidebar → 详情 串行 |
| 本次首次进入 | **5463** | 1565 | me 1646 → 详情与 sidebar 同时 1647 → workspace 5464（详情之后） |
| 本次刷新 | **4169** | 686 | me 917 → 详情与 sidebar 同时 928；workspace 未调用 |

详情请求本身仍需 3.2–4.5 秒（后端共享锁等待，本轮未优化），但已不再被侧栏和工作台恢复串行拖住，符合 `a58a672` 的设计边界。

### 3. 预览准备失败（项目 E）——当前已可取到地址

`GET /preview-url/Re502bb…?status=1` 现返回 **200** 与 900 秒签名 URL（原先 503 `preview_prepare_failed`）。该项目是验收时人为让 OSS 校验失败、再显式重试成功的样本，符合“失败不自动重试、显式重试可恢复”的设计，不是回归。

### 4. EP25 原片流传中断——当前未复现，但代码未修

在容器内用当前代码重新 `open_original('101698','EP25')` 并读完整个对象：**113,081,293 字节 / 432 块 / 9.36 秒，完整读完**，与报错时声明的 113,081,293 一致。

因此当时是上游（OSS→服务端）流传瞬断。但 `originals._send_original` 至今只捕获 `BrokenPipeError`/`ConnectionResetError`，`requests` 的 `ChunkedEncodingError` 会变成 HTTP 500 且不重试、不续传。**属于未修复的健壮性缺口**，本轮未改。

### 5. 发布 409 与 worker 健康抖动——未验证为已修复

- 48 次 publish 409 分布在 3 天、7 个真实账号，集中成簇。发出请求的页面 revision 落后于服务端时，这道守卫按设计拒绝；但同一账号 1 分钟内连续 10 次说明页面没有及时取得新 revision，用户体验上等同于失败。未找到对应修复提交，也**没有用真实数据复现该场景**（会写生产状态，需单独授权）。
- 6 次 `/healthz` 503 与 `Runner tick failed` 指向 worker 心跳 >30 秒或连续 tick 失败 ≥3；`workflow.update` 对已纳管项目仍会抛 `ValueError`，本轮未修、未复现。

## 四、结论与边界

- 已用当时出错的数据确认修复：**EP2 尾帧误判**（旧版本失败、当前版本通过）、**首屏慢**（21/27 秒 → 5.5/4.2 秒）、**预览显式重试**（当前 200）。
- 未修复、需排期：EP25 一类上游流传中断不重试、publish 409 反复、worker 心跳导致 `/healthz` 503、`workflow.update` 对纳管项目抛错。
- 本轮未做：真实新制作、成片替换、FBBM/飞书交付、混合负载压测。恢复生产状态类验证需单独授权。

私有证据：`/Users/matao/exactcut-test/release-prep-824cfb5/production-regression.json`（接口时序与预览状态）；服务器 `/srv/exactcut/preparation/regression-20261107/`（EP2 原始文件与 sha256）。生产浏览器测量通过本机 Chrome 只读完成，未修改项目。
