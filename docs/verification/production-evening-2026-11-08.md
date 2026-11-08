# 2026-11-08 晚间两个修复合入 main 并发布

## 授权、提交与范围

用户明确要求将 GitHub `welltop-cn/exactcut_studio` 的 `merge/1108-evening`（aee6fe9）合入 main 并发布；合 main 前跑全量并与 origin/main 对比失败，有新增失败即停止；不强推、不改写历史，不改变其他线上配置，不做真实 FBBM/OSS 上传或发布测试。

- 基线 main：`d452608fd577aa2119e3c4d37db2c629aa2ac4ee`。
- 最终应用/main 提交：`aee6fe9c80fcea3723ba81d98cc3c30bcf32d2bb`，保留原中文合并提交“合并队列只读扫描免全局锁修复”。
- 包含 `e68c589`：打码播放线离开所选片段后隐藏区域框；`de69bc2`：queue 专用只读扫描，不取得 web.LOCK/batch_control.LOCK，活跃筛选前移至排序前。
- 候选已基于 d452608，测试完成后直接 fast-forward main 到 aee6fe9 并正常 push；没有 squash、rebase、amend 或 force push。
- 新 release：`us-east-20261108-evening-aee6fe9`。
- 新 image：`sha256:c2fcd6a858c07d605c97b6288fbe1e178aaa06dc82eead60de7057e28fa055a9`。
- 前驱/回滚目标：`us-east-20261108-main-de8d7e7`（应用 de8d7e7）。

用户提及的 `docs/开发发布与回滚.md` 当前正文已归档在 `docs/archive/source/docs/开发发布与回滚.md`，其中旧 Mac 命令不用于本次操作。已读取其工程备份、完整测试及保留业务数据的原则；实际操作沿用今日 17:19 的候选 `source/deploy/linux_ops.py` 与当前 `docs/linux-migration.md`。

## 合 main 前的全量与失败对比

固定 candidate aee6fe9 和 baseline d452608 两个干净副本，用同一指定 Python 3.12、各自独立数据目录，关闭 AI/下载/发布/外部写入，显式 `-o addopts=` 覆盖默认 marker 过滤，跑完整 unit + integration + UI，输出 JUnit。两者均在 main 推送之前结束。

| 范围 | 通过 | 失败/错误 | 跳过 | 用时 | 退出码 |
| --- | ---: | ---: | ---: | --- | ---: |
| 本机候选 aee6fe9 全量 | 2823 | 0 | 10 | 814.10 秒 | 0 |
| 同环境 origin/main d452608 全量 | 2811 | 0 | 10 | 812.86 秒 | 0 |
| Linux 发布管理器全量 | 2409 | 0 | 424 | 774.18 秒 | 0 |
| Linux Chromium UI 补验 | 401 | 0 | 8 | 177.55 秒 | 0 |

三次完整回归各 10 条已有多线程 fork 的 DeprecationWarning。UI 补验另有 2424 项 deselected。标准 Linux 验证镜像缺少浏览器，另以已有浏览器验证镜像、只读挂载相同候选、禁用网络补验。没有将 skipped/deselected 计作通过，也没有把重叠批次相加。

本轮 JUnit failure_ids 对比：候选和基线均为空，**新增失败 0、消失失败 0**。下午旧运行记录的剩余 61 项曾与当时基线相同（`merge-1108-run/six-comparison.json`）；本轮同环境未复现，不能说这两个修复解决了原 61 项，也不使用那份历史记录替代本轮基线。

[差异审查](evening-differential-review-2026-11-08.md)覆盖全部 2 个应用文件、4 个测试文件，核对账号过滤、只读/隔离边界、锁与原子替换。没有发现已确认的新发布阻塞。保留用户指定的原始提交，未因测试文件 EOF 空行提示改写或追加应用代码。

## 备份、发布与回滚

发布前工程备份：`/srv/exactcut/backups/pre-evening-aee6fe9/`，root 私有目录，包含原生产封存源码/发布元数据及运维 bin 的 `engineering.tar.gz`、三份 config、current.json、nginx 站点原件，以及候选和 main 祖先历史的 `history.bundle`；没有复制业务 data、账号数据库、任务、原片或成片。工程归档 SHA256 通过，bundle 已验证完整祖先历史。收尾另补全本地所有分支/远端引用/标签的 `history-all.bundle` 并验证通过；该补充动作发生在发布后，不冒充发布前备份。

候选通过 Linux 管理器 build 封存；发布前再次校验：

- 本机完整比较 allowed=true、new_failures=[]，候选 commit 与封存一致。
- 线上 current、三份应用配置及全部 nginx 常规配置文件 SHA256 与准备前一致。
- `check-switch` allowed=true，active_tasks=[]、phase_blockers=[]；支持既有 schema 1、媒体/编辑/阶段协议。

北京时间 2026-11-08 **21:59:07** 开始 publish，约 **22:00:13** 完成，耗时 **65.52 秒**，退出码 0。仅切换已合 main 的 aee6fe9，不修改已安装运维入口、nginx、runtime 或凭据。管理器在维护屏障内检查健康后解除维护，最终 maintenance=false。

旧封存版本及镜像保留，previous 已更新为 de8d7e7 的 release。需要回滚时先检查任务和后续配置，以同一候选管理器执行：

```sh
python3 /srv/exactcut/preparation/evening-aee6fe9/candidate/source/deploy/linux_ops.py \
  --root /srv/exactcut rollback us-east-20261108-main-de8d7e7
```

普通代码回滚保留最新业务数据，不恢复旧数据库或旧 nginx 配置。本次未触发回滚。

## 发布后只读核验

通过真实 HTTPS 域名、现有认证信息，只发 GET：

- `/home/healthz`：200，status=ok、production、worker_alive=true、maintenance=false，新 release 正确；运行容器 image 与封存 image 一致。
- `/home/`：200，HTML 正常并保持 gzip。
- `/home/api/queue`：200，返回已定义的 pools/tasks/scope 结构，管理员 scope=all；该次 tasks=0，实测约 0.682 秒，单次结果不代表性能基准或有载验收。
- `/home/mosaic.js`、`/home/app.js`：200，解压后 SHA256 与固定候选源码一致，gzip 保持。
- runtime.json、production.env、deployment.json SHA256 不变；nginx 全部配置文件哈希、主进程 PID 不变。数据/配置/模型挂载的路径与读写权限不变。

首次辅助核验脚本把 Docker Mounts 返回列表的顺序当作配置差异而断言失败；实际各目标挂载完全相同。修正为按 destination 比较后完整通过。这是仓库外核验脚本问题，没有修改应用代码或线上挂载来绕过检查。

只读状态核对显示无活动任务、无 phase blocker、maintenance=false；本轮验证容器均已退出并自动删除，仅正式容器运行。没有触发真实 AI、制作、FBBM、OSS 上传/交付，没有新增生产测试项目或修改业务状态，也未进行线上播放/打码编辑行为测试。

## 证据与提交

- 本机：`/Users/matao/exactcut-test/release-evening-aee6fe9/`，含 candidate/baseline 日志、XML、退出码、comparison.json。
- 服务器：`/srv/exactcut/preparation/evening-aee6fe9/`，含前后快照、check-switch、publish-result、各门禁日志；封存发布在 `/srv/exactcut/releases/us-east-20261108-evening-aee6fe9/`。
- 初次 bundle 只含 remote-tracking ref，clone 未取得 HEAD，构建入口尚不存在而退出；改传具名本地分支 ref 后正常 clone/build，原日志单独保留，不算测试失败或通过。
- 应用 aee6fe9 已合 main 并推送。发布后仅补充本记录、差异审查和 production-change-log，用中文提交并推 main；纯文档提交不重建应用镜像。
