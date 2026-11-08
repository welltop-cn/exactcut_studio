# 晚间候选差异审查（2026-11-08）

结论：完整回归、同环境基线失败集合比较、Linux 发布门禁与 Chromium 补验通过，没有新增失败，允许将已审核候选快进合入 main 并发布。未发现已确认的安全或业务阻塞。

范围：`d452608..aee6fe9`，共 6 个文件、229 行新增、3 行删除。固定候选 `aee6fe9c80fcea3723ba81d98cc3c30bcf32d2bb` 已包含两个修复及其中文 merge commit，d452608 是它的祖先，可以不改写历史地快进 main。

## 应用改动与风险

- `source/exactcut/web/mosaic.js:377`：区域框只有在画面可用、播放线处于所选半开片段范围且有候选框时显示（正在绘制的既有分支保留）。不清除所选片段、框或播放器画面。`test_mosaic_ui.py` 覆盖 previous/next/jump/slider/timeline 五类越界导航、返回片段后重显；`test_mosaic_local_preview_ui.py` 调整夹具范围，让 frame 123 位于测试所选片段内，继续验证帧就绪。
- `source/exactcut/webserver.py:113`：新增 queue_jobs，使用现有 job_records.load(..., isolate=False)，不经过取得 web.LOCK/batch_control.LOCK 的 read_job。解析仍由 job_records 内部读锁保护，坏结构/坏 JSON 跳过，真实权限或 I/O 错误向外暴露；不隔离、不写旁车、不释放预留或发信号。
- `queue_snapshot` 在排序前只取 queued/running 的 fetch/render 项；原实例过滤、父项目映射、暂停/归档显示、accounts.allowed、admin owner 字段及白名单输出均保留。它仍是文件级只读快照，不提供整个目录的事务一致性，也不宣称完全没有任何读锁。

`queue_jobs` 有 1 个生产调用点（queue_snapshot），queue_snapshot 有 1 个 HTTP 入口（/api/queue），影响范围局限于队列展示。基线 blame 显示队列账号过滤和白名单来自 `ea4b5e7f`，本次未删除；内部子项目映射来自 `5e8cefe7`，同样保留。调度器仍调用原 stored_jobs/read_job 隔离入口。

## 测试与审查边界

新增 test_queue_readonly 的 12 项覆盖：另线程同时持两把全局锁时查询仍完成、查询不隔离而后续 worker 仍隔离、stat/open 间文件消失、真实 I/O 错误、原子替换前后各完整文件版本、活跃筛选和旧展示语义、普通用户权限范围。原 queue_view 改为落盘真实任务，不再只 mock stored_jobs。

审查涉及全部 2 个应用文件和 4 个测试文件，读取原实现、调用方、job_records 与 concurrency.lane，核对锁顺序和权限边界。没有发现已确认的鉴权绕过、隔离副作用或外部写入新增；不代表整个仓库安全审计。

候选没有依赖、部署工具、Compose、runtime 模板、数据 schema 或其他配置差异。分支的 test_queue_readonly.py 末尾有一处空白行提示；保留用户指定的原始提交，不为格式改写历史或追加应用改动。工作树本身保持干净。

完整回归分别在独立 candidate / baseline 副本、相同指定 Python 3.12 与隔离数据目录运行，显式清除 pytest 默认 marker 过滤，保存 JUnit 后比较失败/错误的用例 ID 集合。下午已有记录为剩余 61 项与基线相同；不能用它替代本轮 d452608 实测，也不能把本轮通过说成修复了那 61 项。

生产仅允许只读版本、首页、queue 和静态脚本核验；没有真实 AI、FBBM、OSS 上传/交付或生产测试制作。

## 最终门禁

| 范围 | 通过 | 失败/错误 | 跳过 | 用时 |
| --- | ---: | ---: | ---: | --- |
| 本机候选 aee6fe9 全量 | 2823 | 0 | 10 | 814.10 秒 |
| 同环境 origin/main d452608 全量 | 2811 | 0 | 10 | 812.86 秒 |
| Linux 发布管理器全量 | 2409 | 0 | 424 | 774.18 秒 |
| Linux Chromium UI 补验 | 401 | 0 | 8 | 177.55 秒 |

各批退出码均为 0。本机两个全量各有 10 条已有多线程 fork 的 DeprecationWarning；Linux 全量同为 10 条。Chromium 补验另有 2424 项 deselected，未将它们计作跳过或通过。Linux 标准镜像未安装浏览器，其跳过不能改记为通过，也不把重叠批次相加冒充独立用例总数。

JUnit 对比结果：candidate/baseline failure_ids 均为空，new_failures=[]、resolved_failures=[]。本轮没有失败项，因此没有需要归入历史 61 项的本轮失败；历史“61 项与基线相同”原记录仍保留，不宣称由本修复解决。

测试命令清空默认 addopts，覆盖 unit、integration 和 UI；使用用户约定 Python 3.12，候选和基线使用各自干净源码及独立数据目录，关闭 AI/下载/发布/外部写入。Linux Chromium 补验使用已有验证镜像、只读挂载候选、禁用网络。

私有证据：本机 `/Users/matao/exactcut-test/release-evening-aee6fe9/` 中 candidate/baseline 日志、XML 与 comparison.json；服务器 `/srv/exactcut/preparation/evening-aee6fe9/` 和封存发布目录。线上执行及回退见[正式发布记录](production-evening-2026-11-08.md)。
