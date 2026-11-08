# 2026-11-08 main 审查、死锁修复与生产发布

## 授权与版本

用户要求“对比当前 main 和线上的 commit，搞清楚修改了哪些，再检查一下，然后就发布吧”。本次完成差异审查、必要阻塞修复、双平台完整回归、正式发布和只读上线核验；没有发起真实制作或外部交付。

- 变更前 commit：`79b2ca863b36b7b2b4661546d25de678417388c4`，release `us-east-20261107-mosaic-local-frame-79b2ca8`。
- 开始审查时 main：`e31abe76f33c5acaeddd8847c971c684f402d7c3`，与远端一致，工作区干净。
- 最终应用 commit：`de8d7e73fee243cefd08577536ac706112454aac`，已推送 main。
- 新 release：`us-east-20261108-main-de8d7e7`。
- 新镜像：`sha256:f2899beca5d89787f6e05f52c05c37634834f3c359908833130aba380539e652`。
- 操作时间（北京时间）：2026-11-08 17:18:37 开始切换，约 17:19:43 管理器完成，65.51 秒，退出码 0；新容器于 17:19:38 启动。

## 差异与修复

原生产至 main 共 21 个提交、44 个文件。主要为锁/调度阶段诊断、worker 单轮读取复用、已结束项目无动作检查跳过、切项目请求优化、素材预取去重及坏任务隔离。依赖锁文件、部署工具、Compose、持久数据 schema 和生产配置均没有变化。

审查发现混合入口锁顺序反转：纳管 sources 不预持 web.LOCK，而旧入口预持；预取代码先取 PREFETCH_LOCK，再在读取已有任务时取 web.LOCK，可与旧入口互锁。空 jobs 目录不会触发。先写回归并得到明确失败，再将预取统一为 `LOCK -> PREFETCH_LOCK`；RLock 允许旧入口重入，去重临界区保持。相关集合 91 passed。修复为独立提交 de8d7e7。

详细风险、调用点、历史依据、红绿证据及覆盖边界见[差异审查报告](main-differential-review-2026-11-08.md)。

## 最终回归

全部固定在 de8d7e7 的独立候选中执行。原 e31abe7 测试因发现阻塞而主动停止，保留原始日志，不计作通过。

| 范围 | 结果 |
| --- | --- |
| 本机完整 unit + integration + UI | 2811 passed、10 skipped、10 warnings；797.17 秒；退出码 0 |
| Linux 发布管理器完整回归 | 2397 passed、424 skipped、10 warnings；765.09 秒；退出码 0 |
| Linux Chromium UI 补验 | 401 passed、8 skipped、2412 deselected；173.57 秒；退出码 0 |
| Linux 未标 ui 的浏览器媒体集成 | 7 passed；19.51 秒；退出码 0 |

完整命令均清除 pytest 默认 marker 过滤；本机使用约定 Python 3.12。Linux 门禁使用候选发布管理器构建的运行/验证镜像，隔离数据，不加载生产凭据。标准镜像缺少浏览器，所以在已有 `exactcut-browser-validation:first-load-f40e59f` 中只读挂载同一候选、禁用网络和业务写入补验；另覆盖 `test_mosaic_queue_browser_http.py` 与 `test_validation_http_ui.py`。各批次不相加冒充独立用例数，跳过不计通过。

浏览器补验镜像 Python 3.12.15、pytest 9.1.1、Chromium 143.0.7499.4。两套完整回归的 10 条 warning 均是已有多线程 fork 的 DeprecationWarning。

## 切换与核验

使用候选自身 `source/deploy/linux_ops.py` 完成 build、check-switch、publish，没有更新运行中源码或覆盖已安装运维入口。

- 发布前：封存 commit、镜像与通过门禁一致；线上版本及三份配置与准备时一致；`check-switch` 通过，active_tasks 和 phase_blockers 均为空。
- 发布后：内网和公网 `/home/healthz` 均为新 release、status=ok、environment=production、worker_alive=true、maintenance=false。管理器切换中的健康记录 maintenance=true 属于维护屏障内检查，最终状态已解除。
- 页面及 8 个脚本、同一组 3 个项目详情均返回 200。8 个脚本同时核对内网、公网 SHA256，与固定候选源码一致。
- 前后侧栏项目 ID 集合完全一致，共 228 个；数据/配置挂载完全一致。运行容器 image 与封存 image 一致，容器按发布流程重新创建。
- runtime.json、production.env、deployment.json 的 SHA256 前后一致。CPU 14、内存 24g、端口、凭据及功能开关沿用既有值。
- 上线后一分多钟内新 release 没有 error/critical 事件。首个 scheduler.tick_summary 含 18 次 tick，outbox 处理/跳过 240/3468、crash 计算/跳过 1028/17476，说明优化分支已实际运行；这些是窗口计数，不是性能对比或业务完成数。
- 收尾时只剩正式应用容器；本轮标准验证及浏览器补验容器均已退出并自动删除。

## 回滚与边界

previous 已记录为 `us-east-20261107-mosaic-local-frame-79b2ca8`，该封存目录与镜像保留。回滚先检查活动任务、协议兼容和后续配置，再用候选管理器执行：

```sh
python3 /srv/exactcut/preparation/main-de8d7e7/candidate/source/deploy/linux_ops.py \
  --root /srv/exactcut rollback us-east-20261107-mosaic-local-frame-79b2ca8
```

普通回滚保留当前业务数据，不恢复旧数据库。本次未触发回滚。

本次生产验收是健康、版本、静态资源和项目只读检查；真实媒体业务通过隔离自动化测试覆盖，没有在生产触发 AI、下载、生成、结果替换或 OSS/FBBM/飞书写入，没有测量生产峰值容量，也没有对用户网络的切换延迟作提升百分比承诺。

## 证据与本地同步

- 本机最终回归：`/Users/matao/exactcut-test/release-main-de8d7e7/`。
- 服务器：`/srv/exactcut/preparation/main-de8d7e7/`，含 before/after、各测试日志、check-switch、publish-result、post-verification。
- 封存发布：`/srv/exactcut/releases/us-east-20261108-main-de8d7e7/`，含源码、release.json、镜像与门禁摘要。
- 修复红绿证据：`/Users/matao/exactcut-test/release-main-e31abe7/`。
- 应用提交 de8d7e7 已合入并推送 main；发布之后的审查/上线记录为纯文档，不要求重建应用镜像。
