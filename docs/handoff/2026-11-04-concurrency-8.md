# 交接：并发/调度 8 分支测试（2026-11-04）

接手者：马涛本地电脑上的 agent。本文只写仓库内信息，不含本机路径、IP、密钥。

## 1. 规则（马涛定的，必须遵守）
- 一次只做一件事，小步、能验收；不扩散范围，不顺手修非核心问题。
- 功能分支做完可 push 供 review，不开 PR；**main 不推、不合，合前问马涛**。不 force push。
- 平时只跑相关测试；合 main 前跑一次全量（约 2140 个）。`test_round1_delivery` / `test_round1_evidence` 的 `same_size_restored_mtime` 偶发失败可忽略。
- 「制作助手」按钮 `#helpNav` 保持隐藏。
- 线上服务器只读；任何线上变更需马涛明确授权并记 `docs/production-change-log.md`。
- 测试：FBBM 绝不上传/发布；OSS 只写 `exactcut-previews/test/`（`preview.namespace=test`）；Metabase 只读 SELECT；飞书/CF-Ads 拦截。SAM 一律 CPU。
- 多机测试配置必须一致（只允许路径、端口不同）；真实素材由各机器**从原地址下载**，不互相拷贝；都用真 Chrome。
- 仓库将公开：提交里不能有绝对路径、IP、密钥。

## 2. 分支状态（全部已推送，均未合 main，基于 main ddf21bb）
测试集成分支：`test/concurrency-8`（含下面 7 个 + S6 排队修复）。

| 代号 | 分支 | 内容 |
|---|---|---|
| S1+S2 | fix/short-ops-no-slots-v2 | ffprobe、单帧抽取、抽音、mux 等短操作不占名额 |
| S3 | fix/hook-preview-own-slots | Hook 预览、单帧定位有独立名额（hook 3 / locate 2） |
| S4 | fix/no-preclaim-slots | 交互任务不预占后续阶段名额 |
| S5 | fix/no-transfer-slots | 删除全局 transfer 名额（保留 OSS 按对象锁） |
| S6 | fix/final-queue-only（含 4232bf1） | 删 compute.lock，最终生成只受队列+final_media(1)；排队时显示「排队中/等待后台处理」；旧版剪切不再 429 而是排队 |
| S7 | fix/no-wait-timeout | 可取消任务等名额不再 1800s 超时；SAM 超时 60+4×帧数 |
| SR | fix/scheduler-read-once | 制作调度每轮只读一次 jobs，10.1s→3.9s/轮 |

另有已推送待定分支：perf/probe-packets、perf/trim-seek、perf/smart-cut、perf/mosaic-gop（P1–P4 提速，**本轮不测，留到性能专项**）；fix/public-error-surfaces-ui、fix/legacy-pause-during-production、fix/media-path-relocate、fix/mosaic-confirm-race、feat/pause-archive-cancel-cutting、build/split-dev-deps、test/split-layers、fix/hash-deeplink、fix/preview-upload-retry 等。

## 3. 测试结论（test/concurrency-8，真 Chrome + 真实素材）
通过：
- 剪切打码排队显示（S6）、取消排队（任务不执行，选择保留）、剪切中归档被后端拒绝（400，提示清楚）。
- 从零开始全流程：复制项目（同剧 ID 101606、同 5 个时间段）→ 从原地址下载正片 → 制作 46 秒出片 → 新素材手动加框打码 → 结果框内马赛克 → 放弃结果。
- 制作并发：5 个同时制作，render 车道 3，第 4/5 个 `pool_full` 排队约 20 秒后补上，全部完成；制作与剪切打码同时跑互不阻塞；制作中暂停/继续正常。
- 两账号同时提交可见「等待全局制作空位，按用户轮转调度」（M2 上，轮转结果未出完整报告）。
- 制作中重启服务：新进程接管运行中的 render，正常完成。
- 打码排队中重启：排队任务重启后自动开始并完成。
- 全量测试与 API 并发用例无回归（Mac mini / M2）。

未完成（Mac 两台中途停止）：多账号轮转完整结论、AI 速率桶（60/min, burst 5）、制作失败重试退避、磁盘预留等待、关标签页后任务继续、同对象 OSS 预览上传去重。

## 4. 发现的问题（都未修，等马涛定）
新发现（暂停/归档代码，8 个分支未改动，应为 main 旧问题）：
1. **归档/暂停后继续，制作立即失败「媒体处理超时」**：qc.inspect_output 的 ffprobe 超时 5s，暂停时间被算进超时（`media.run` 不扣暂停时间；`material_download` 有扣）。普通暂停/继续可能同样受影响，未单独测。
2. **继续后子进程又被 SIGSTOP 冻结**，job 一直显示 render running 并占 1 个 render 名额（疑似 batch_control 调度循环读到暂停→继续写入后才发 SIGSTOP 的竞态，未验证）。
3. 暂停期间用时继续计时；继续后短暂显示「较久未收到执行心跳」。
4. 制作刚完成立即打开剪切打码，先提示「该素材正在执行其他操作」，重开正常。

之前已记录的 main 旧问题：
5. 无变化的「重新制作」仍显示「内容已确认，等待制作」（material_versions.py 无变化确认仍写事件）。
6. 取消排队后前端显示「已停止生成…」而不是「已取消排队」（mosaic.js 对 cancelled 写死文案）。
7. 停服务时若有打码在跑，要等打码做完才停（`mosaic_dispatcher.stop(timeout=None)`，实测 2–4 分钟；期间新实例因 worker.lock 起不来）。
8. 同一刻提交两个一样的剪切，一个 429（素材锁瞬时撞锁）。
9. 旧版剪切入口排队时状态显示 running。
10. 配置残留：`editor_resources.transfer_tasks`、`concurrency.hook_previews` 已失效；`hook_tasks`/`locate_tasks` 未写进配置（用代码默认 3/2）。
11. 剪切打码识别的 Gemini 调用不走 AI 速率桶，只受 scan 名额(2)。
12. main 上已有 UI 测试失败：`test_old_stop_failure_cannot_overwrite_next_editor_message`。
13. `docs/开发发布与回滚.md` 第 49 行有本机路径，公开前要处理。

## 5. 待马涛决定
- 是否按顺序把 8 个分支合 main（他说过「先别合 main」）。
- 问题 1–2（暂停/归档后继续失败、二次冻结）是否先修——这是本轮发现的唯一真 bug。
- 问题 5–7、10 是否修。
- 之后的性能专项：P1–P4 分支测试、SAM CPU 提速（目标剪切/打码几十秒）。
- 测试完成后清理 OSS `exactcut-previews/test/`（已授权，删前列清单，绝不碰 production/ 和 fb-bm-assets）。

## 6. 本地复现提示
- 测试实例：用 `test/concurrency-8` 起一个独立数据目录和端口的实例，`preview.namespace=test`，`EXACTCUT_ALLOW_PUBLISH=0`，`delivery_enabled=false`，`EXACTCUT_ALLOW_MATERIAL_FETCH=1`，外部写入守卫开启。
- 归档/复原/继续等接口必须带 `revision` 和 `submission_token`（前端 `api()` 自动加），否则 409「缺少有效幂等标识」。
- 并发控制全景见本分支代码：`concurrency.py`（车道）、`editor_resources.py`（编辑器名额）、`mosaic_queue.py` / `mosaic_render.py`（最终生成）、`interactive_queue.py`、`batch_control.py`（暂停/归档）、`ai_rate.py`。
