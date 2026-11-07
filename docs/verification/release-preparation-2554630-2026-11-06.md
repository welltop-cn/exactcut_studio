# 2026-11-06 main 发布准备：2554630

状态：本候选准备与双平台回归已结束，**发布门禁失败，未执行 publish，生产未切换**。产品和测试源码未修改。

## 候选与整合核对

- 固定候选：`255463018008a2650aedf6e8b083080283fa50e1`。
- 本机 main 与远端 `origin/main`（本轮 `git ls-remote` 实读）一致；启动准备时工作区干净。
- 上线范围：`8216c27..2554630`，共 145 条提交（含合并、测试和文档）。
- 用户参考的[人工清单](manual-checklist-8216c27-to-489c5dd.md)截至 `489c5dd`。候选额外包含 `3507c44`（时间纠正后清除旧错误）、`0031322`（片段勾选、时间与区域形状/效果）及 `2554630` 文档。
- 已逐项确认 `8216c27`、`96c1aa5`、`68713f8`、`f34aabd`、`126d840`、`489c5dd`、`3507c44`、`0031322` 都是候选祖先。本轮早期并发补丁和 60/5 配置已合入。
- 只剩一个登记工作树。两个普通功能分支已合入；未合入分支仅 `backup/m2-wip-live-mosaic`、`backup/m2-wip-main`、`backup/m2-wip-takeover-backoff`。仍有 7 个历史 stash，其中最新一份明确标注本地登录临时补丁。这些不自动纳入发布或删除，不能声称所有历史草稿均合并。

## 线上实时核对

通过既有 root SSH 与 `/srv/exactcut/bin/linux-ops --root /srv/exactcut status` 只读核对：

- 当前 release：`us-east-20261031-bottom-subtitles-8216c27`。
- 应用 commit：`8216c277f27fd88040944faae77a9e82a15be765`。
- 镜像：`sha256:84a0b6cfe26128411555cbc6241bc4ef5f7a5e6c6ffae11ccc039637243c18dd`。
- 本轮核对时 `active_tasks=[]`、`phase_blockers=[]`、`maintenance=false`；健康正常、worker 存活。最终切换前仍必须重新检查。
- 生产资源：14 CPU、24g 内存；磁盘约 670 GiB 可用。
- 本轮只在独立 preparation/releases 目录准备镜像，没有重启、切换或写入业务任务。

## 配置保留

不覆盖服务器 runtime、production.env、deployment.json，不变更已经单独上线的监控脚本和 cron。

现有 runtime 与候选默认配置按当前加载规则合并后的关键值：

| 项目 | 值 |
| --- | --- |
| 普通 fetch/render/upload/poll | 2/3/2/4 |
| AI 预算 | 60 RPM、burst 5 |
| 字幕 | 2 执行、30 等待 |
| 最终打码 | 1 |
| 交互 preview/scan/preprocess | 2/2/2 |
| 共享媒体/最终媒体 | 5/1 |
| frame/transfer | 2/2 |
| 普通/交互编码线程 | 4/2 |

这份核对不等于 Linux 混合负载容量证明。凭据不复制到源码或文档；配置哈希及 release 基线留在私有 preparation 目录，用于切换前后比对。

## 验证与当前阻断

本地使用指定 Python 3.12，在固定提交的独立干净 clone 中运行：

```sh
python3.12 -m pytest -o addopts='' -q --tb=short
```

显式清除继承的业务环境，数据根独立，AI/外部写入/素材获取/发布关闭。没有使用默认的 unit-only 层过滤。

本地完整套件已结束：**2543 passed、6 failed、10 skipped、10 warnings，781.80 秒，退出码 1**。不是 unit-only，也未把失败或跳过记为通过。以下 6 项同时已定点复现；本轮未更改产品、测试或放宽断言：

| 用例 | 实际失败 | 初步归类（待收敛） |
| --- | --- | --- |
| `test_editor_draft_restore::test_render_draft_and_pending_confirmation` | Node `ReferenceError: validShape is not defined` | 用例截取源码片段遗漏新依赖；需同时检查旧草稿预期是否同步 |
| `test_editor_polling::test_text_timeline_opens_as_editable_clip_fields_without_implicit_draft_changes` | `#sourcePrefetch` 预期隐藏，实际可见 | 用例同时触发异步预取；需核对模拟响应、展示约定与实际行为，尚未判为产品故障 |
| `test_editor_timeline::test_short_blocks_keep_tooltips_and_clickable_width[ignored-忽略-1]` | 查找“忽略”checkbox 超时 | 候选已移除入口，旧用例尚未同步；仍须保留短片段及旧数据覆盖 |
| 同上，45 帧参数 | 同上 | 同上 |
| `test_mosaic_tracking_capability_ui::test_legacy_tracked_draft_opens_as_fixed_with_notice_without_tracker[False/True]`（2 项） | 旧 soft_blur 草稿预期 blur，UI 实际 pixel | 新手动片段保存自己的效果，用例只修改全局效果；需核实真实旧草稿数据与效果优先级，不能直接删断言 |

人工清单未勾选，不自动记为验收通过。`0031322` 的已有报告明确真实登录页视觉验收未完成；自动 Playwright 证据不能冒充用户实际项目手工确认。

## Linux 构建

使用固定提交的候选 `source/deploy/linux_ops.py build`，不调用 publish。正式镜像与独立测试镜像分离，测试容器不挂载生产配置或数据。Linux 未安装 Playwright 时的跳过必须如实记录，不能算 UI 通过。

- 候选 release ID：`us-east-20261106-main-2554630`。
- 服务端准备目录：`/srv/exactcut/preparation/release-2554630/`。
- 构建目录：`/srv/exactcut/releases/us-east-20261106-main-2554630/`。
- Linux 完整测试调用已结束：**2142 passed、4 failed、413 skipped、10 warnings，705.73 秒**；build 退出码 1。缺少浏览器等平台能力的跳过不计为通过。
- 失败包括同一草稿测试 `validShape` 未定义，以及 `test_segment_reencode::test_mosaic_encodes_only_masked_gops_and_matches_full_path` 的三个 ranges 参数。
- 三个媒体失败的实际事件均为 `used=False`、`reason='PSNR frame count differs'`：分段优化的画质验证未通过，进入已有回退路径。此结果证明优化路径在目标 Linux 工具链未通过门禁，**不据此声称最终输出已经损坏**；具体根因尚未诊断。
- 构建出的运行镜像为 `sha256:5ca75d203fd21f17e94eb581b85e90f05affbac886e83d6339a01fbe6ddf021d`，revision 标签匹配候选。但测试失败，管理器未生成 `release.json`，不可发布。
- 测试容器已退出并自动移除；保留失败构建和日志，不执行镜像清理。
- 收尾重新读取线上：release 仍为 `8216c27`，health 正常、worker 存活、maintenance=false；runtime.json、production.env、deployment.json 的 SHA256 与操作前完全一致。
- 固定 bundle SHA256：`2390771cddd62396f7858b5c21c11bf415dc82cb5c5115a6b56ba0b3bf5b2002`。

## 后续门禁与回退

1. 收敛全部失败，区分产品回归与未同步测试；保持原业务保护和覆盖，不删用例凑通过。
2. 若代码或测试变化，固定新提交重新构建和验收，旧候选记录保留为失败证据。
3. 补齐清单相应实际入口验收，记录真实/替身及未覆盖项。
4. 完成候选镜像封存和 `check-switch`，使用同一候选管理器进入维护、排空任务并切换，不能强杀用户任务。
5. 切换后核对真实镜像、静态资源、登录/项目/播放及健康；保留配置哈希、监控、账号与业务数据。
6. 健康失败按管理器恢复前一封存镜像；普通回退不恢复旧数据库。新持久队列出现后必须重新核对排空与协议兼容性，不能以同为 schema 1 推断任意回退安全。

## 私有证据

本机：`/Users/matao/exactcut-test/release-prep-2554630/`，含 `integration-audit.json`、固定源码 clone/bundle、`full-tests.log`、`focused-failures.log`、`focused-tracking.log`、`test-result.json`、下载的 `linux-tests.log`。

服务端：上述 preparation 目录，含 `before.json`（版本及配置哈希，无凭据明文）、`effective-config-readonly.json`、`build-runner.log`、`build-result.json` 和 `after.json`（收尾版本、配置未变与健康）。
