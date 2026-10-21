# 侧栏与首屏性能修复生产发布（2026-10-21）

用户明确授权发布生产。先发布 6a5de9a 的轻量侧栏与 workspace 恢复优化；浏览器验收发现剩余数秒阻塞，继续定位后台恢复循环持锁重复读任务，补充 f918001 后重新完成两平台全量回归并发布。

- 最终版本：us-east-20261021-sidebar-f918001
- 提交：f9180018bd8c3c521e7b30679daad02787bd21ef
- 镜像：sha256:fcc8eba5ace89d622e6aaf96583562e7809769ecbfa8c6becf2377f046f9ea77
- 入口：https://autocut.creativefitting.ai/home/
- schema 仍为 1。未修改生产配置、Nginx、模型、业务草稿或素材状态。

## 修复与验证

1. sidebar 从权威素材状态生成轻量投影，跳过完整详情、排版校验、编辑权限与历史读取；与详情共用项目状态规则。
2. 已初始化且仍有访问权限的已选项目直接恢复，避免 workspace 再次全量汇总。
3. recover_jobs 的孤儿扫描复用阶段恢复结束后最新的任务快照，保留锁与恢复规则，避免每个执行目录重读全部任务。

新增复现测试分别在旧实现失败、修复后通过。第一轮相关回归118项，第二轮调度/恢复相关109项。最终本地1591 passed、10 warnings、360.14s；Linux1453 passed、138 skipped、10 warnings、331.22s。Linux跳过项不计入通过数，本地无跳过；warnings为已有多线程fork弃用提示。

生产只读验证103个项目的轻量侧栏输出与原详情投影完全一致。299个执行目录/778份任务的恢复状态只读对照，旧路径含剖析器7.939s、新共享快照0.090s，输出一致。该探针未调用有写入副作用的完整恢复函数。

## 真实 Chrome 验收

同一浏览器、同一项目 d7d53bc952bb457eb2e6daaf77898870，使用新建后台标签页，完成后关闭。原用户标签页未刷新或关闭。

| 观测 | 发布前 | 最终版本首次打开 | 最终版本刷新 |
| --- | ---: | ---: | ---: |
| workspace 初始请求 | 7900ms | 287ms | 256ms |
| sidebar | 4152ms | 513ms | 1123ms |
| 当前项目详情返回（导航起算） | 18934ms | 2711ms | 2058ms |

原用户截图对应请求另有浏览器8551ms/服务端8277ms。最终版本额外连续5次sidebar：468、455、1078、423、489ms；均200，均103个项目。对应nginx上游耗时203、204、808、88、204ms。仍存在数百毫秒波动，本次小样本不代表压力测试或全用户延迟上限。

最终页面显示已连接。公网healthz确认production、最终release、worker_alive=true、maintenance=false。两次发布前均allowed=true、active_tasks=[]、phase_blockers=[]。发布前后465份配置/项目草稿/素材状态文件SHA256均不变；exactcut-test启动时间未变。

## 证据与回滚

- 发布源：服务器干净独立仓库 /srv/exactcut/preparation/release-sidebar-f918001，通过linux-ops build/publish。
- 封存测试与镜像记录：/srv/exactcut/releases/us-east-20261021-sidebar-f918001/；tests.log SHA256 f11e8d860a7fb684da9dcd0d8c25a4727b3a72966fd5c719535ad9df6ab815e6。
- 发布/检查/一致性记录：/srv/exactcut/preparation/sidebar-f918001-{publish.log,switch.json,integrity.json,projection-check.json}。
- 本地浏览器测量：/tmp/exactcut-sidebar-browser-before.json、/tmp/exactcut-sidebar-browser-after-open.json、/tmp/exactcut-sidebar-browser-after-refresh.json（临时验收产物）。
- 回滚使用linux-ops rollback，保留业务数据。默认上一版为本次中间版本us-east-20261021-sidebar-6a5de9a；如需回到本次修复前，明确指定us-east-20261019-mosaic-35fe006。
