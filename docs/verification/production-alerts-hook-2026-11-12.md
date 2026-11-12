# 2026-11-12 三分支合入、应用告警上线与真实配置验收

## 授权、范围与发布

用户授权：“把今天所有feature分支合入main，然后发布到线上，在线上用测试账号、拷贝真实项目配置来测试”。验收沿用不上传 FBBM、不写飞书表的约束；制作使用正式原片、AI/缓存和既有 OSS 流程。未修改其他制作人的配置或关闭生产全局功能。

当天分支 `fix/hook-validation-auto-retry`（`76a077e`）、`fix/alert-parent-project-link`（`77ac0d9`）、`feat/application-alerts`（`d446586`）及查询故障隔离、公开文档路径修补，已通过集成候选 `c37af22` 快进合入并正常推送 main。未合入较早的备份或未完成草稿分支。

- 应用固定提交：`c37af22dd11f37a5df85ddf35b8b2121de22ddee`。
- release：`us-east-20261112-alerts-c37af22`。
- 运行镜像：`sha256:104e6c0ec8ff683020b8c56206eb9943b56c8a6f81a6342caa8cace1d93922d0`。
- 北京时间 23:34 完成切换，发布管理器返回成功，耗时 65.46 秒。容器 StartedAt 为 `2026-11-12T15:34:33.853360319Z`。
- 前版保留为 `us-east-20261111-recovery-7383715`，镜像 `sha256:de5f35fc5ebb3486fc99ea2868aaf54cfe26f3c144addc055bf17a0c2345b672`。
- 切换前 active_tasks、phase_blockers 均为空，check-switch allowed=true。通过正式 Linux release manager 的维护、排空、停服、切换、健康与失败恢复流程发布，没有覆盖运行源码。

## 发布门禁

| 环境 | 结果 | 耗时 |
| --- | --- | --- |
| 本机 Python 3.12 完整 unit + integration + UI | 3041 passed / 10 skipped | 853.43 秒 |
| Linux 发布管理器完整验证 | 2600 passed / 451 skipped | 829.98 秒 |
| Linux Chromium UI 补验 | 428 passed / 8 skipped / 2615 deselected | 186.41 秒 |
| Linux 真实浏览器 HTTP 集成 | 7 passed | 19.46 秒 |

各批次有重叠，不能相加；所有批次退出码为 0。标准 Linux 验证镜像没有 Chromium，因此另用断网浏览器容器挂载固定候选补验。两套全量各有十条既有多线程 fork DeprecationWarning；没有把跳过和警告隐去。构建总耗时 873.64 秒。

封存运行镜像和上线容器均核验没有 tests 或 pytest，告警、格式器、阶段执行、素材 worker、错误提示及 webserver 文件哈希与候选一致。私有验收脚本、会话和测试数据未进入镜像。

## 告警切换与真实发送

配置仅作本次告警所需改动：

- 将 deploy 私有监控文件中的现有 webhook 安全接入应用 `production.env`，没有展示或提交真实值；其他凭据行不变。
- runtime 开启 alerts，五秒处理、1800 秒冷却；其他 runtime 配置不变。
- 更新外部监控代码，设置 scan_logs=false；其他监控参数不变，deploy 五分钟探活/资源 cron 保留。
- 在旧服务停止、新服务启动之间，通过原 root feed 补最后增量，再持原监控锁排空待发和冷却累计；结果 old_outbox=0、pending_events=0。配置变更封装在发布管理器 Docker adapter 的 apply 钩子内，失败恢复旧版本时同步恢复备份配置和监控代码。
- 生产发布后的应用告警测试，向共享队列写入两条明确标记“上线验收测试：应用内告警链路检查，非业务故障”的诊断事件；写入期间持发送锁，确保两条先聚合再交给真实 Web 后台发送器。
- 入队样例对应测试账号及测试项目 `5904c7005aa645cc9bf8e89950162a8b`，包含父项目链接、素材编号，pending=2。真实发送后 pending=0、对应 outbox=0，样例已消费、sent_at 已记录。根据实际发送代码，只有 HTTP 200 且飞书 code=0 后才删除 outbox；这验证发送回执成功，不冒充人工群界面阅读确认。
- 回执验收后将 `/etc/cron.d/exactcut-log-feed` 移到受限备份目录，停用 root 抄日志；脚本、feed 和状态保留。部署参数和 nginx 文件哈希不变。

## 测试账号与真实项目配置

本轮新建普通制作人 `release_alerts_1112`（告警重点验收1112），随机密码不输出；会话保存在本机私有文件，权限 0600。测试账号访问两个来源项目均返回 404，未扩大原项目访问权限。

| 真实来源项目 | 新测试项目 | 剧号 | 素材数 |
| --- | --- | --- | ---: |
| `3c4dde256f084cd2b1a836c090b726a9` | `5904c7005aa645cc9bf8e89950162a8b` | 101689 | 2 |
| `dfcaf0baec744a63897b1fe6a1b22cd0` | `15a3929fac134237a47c1a9d98645ce3` | 101708 | 2 |

从来源项目当前草稿复制前两条配置，清理旧素材 id、client_id 和预览凭证，设置测试编导，经正式 HTTPS 接口创建并保存为新项目。没有复制原账号、成片、任务或交付回执。全程使用普通账号会话和实际应用接口，禁止 publish / verify-delivery 请求。

首次校验提示所需集数尚未建立帧索引。HTTP 验收驱动最初只等待，没有像页面一样主动准备原片；确认原因后，补走正式 sources 接口：101689 的 EP4/EP5、101708 的 EP1/EP2/EP3 排队准备，校验随后通过。保留该过程，不把测试驱动漏步骤描述成产品故障。

## 线上业务验收

线上 HTTPS 验收完成，结果 passed：

- 两个项目的真实 Hook 预览均完成，预览前后草稿完全一致。
- 两个项目各两条成片全部达到 review、qc_passed；随后只修改 101689 第二条 Hook 的末尾标点，提交单条重制并再次完成。
- 重制前后通过服务器实际文件读取核对：第一条成功素材的版本 id、master/preview SHA-256、字节数与 mtime_ns 完全一致；第二条产生新版本，旧成功版本保留为 history；101708 的两条素材及版本保持不变。
- 最终四条预览分别验证 Range 206 与完整正文 200，下载后 ffprobe 识别为 540×960 视频，时长依次为 190.055011、170.208333、171.270000、157.012993 秒。
- 实际 fetch/render 日志中核验十组阶段上下文，对应两个可见父项目和五个内部执行批次，父子编号正确区分。普通 prefetch 的 batch_id 与 parent_batch_id 相同属于预期行为，未将其误认作内部批次。
- 两个来源项目 media-state.json 的 SHA-256 前后完全一致。测试项目 deliveries 和 delivery/verification outbox 均为 0，关联普通任务全部完成，没有 publish/verify-delivery 或 upload/poll 交付任务。
- 本轮五次制作版本的 hook_validation_retries 均为 0：没有在真实服务商上自然触发重点拒绝后的自动重试。该故障分支由发布前隔离回归验证，不能把正常线上制作冒充真实拒绝重试验收。

本机 Chrome 未开启远程调试，pi-browser-harness 无法连接。本次现场验收使用真实 HTTPS API；发布前 Linux Chromium UI/真实 HTTP 浏览器补验另有上述结果。不能将本次描述为已在现场 Chrome 逐项点击。

## 收尾、证据与回退

临时测试账号已停用，sessions=0，旧会话请求 /api/me 返回 401。测试项目、五个制作版本、本地预览和既有 OSS 回执保留供复查，没有执行 OSS 对象删除或修改原项目。最终 healthz 为 ok、worker_alive=true、maintenance=false；active_tasks 与 phase_blockers 均为空。应用告警 outbox=0、pending_events=0，自发布起未出现 alert_failed；外部监控 outbox=0、self_failures=0，五分钟 cron 保留，root feed cron 不存在。

私有证据根目录名为 `release-alerts-c37af22`，本机保存 full-suite.log/xml、server-evidence/、线上进度、四条预览视频与 ffprobe 结果、制作前后文件身份、原项目 SHA 和账号清理证据。验收脚本和会话不随仓库分发。

服务器准备目录为 `/srv/exactcut/preparation/alerts-c37af22/`，封存 release 在 `/srv/exactcut/releases/us-east-20261112-alerts-c37af22/`。原 runtime、production.env、监控代码/配置和 root cron 备份保存在准备目录内的受限 config-backup 中。

回退前先核对活动任务、阶段协议与最新数据，按发布管理器恢复上一封存版本；保留最新业务数据库。同步恢复原 runtime/凭据、外部监控代码/配置及 root feed cron，处理新告警队列积压，避免关闭新发送器后误以为积压会自动迁移。普通回退不恢复旧业务数据库，不删除测试或原项目。

本记录是发布后文档；后续仅文档提交不改变已经发布的应用提交。
