# 多项目制作期间字幕预览被拒绝：调查与复现

## 已确认事实

- 2026-10-12 只读检查美东生产实例 `8.221.122.75`，当前版本 `us-east-20261012-combined-d544eae`，提交 `d544eae88b903cab28369b5615212b3bd571a5c6`。未改生产配置、数据或重启服务。
- 日志 `/srv/exactcut/data/logs/events-20261012-1791763210567641264.jsonl` 中，producer03 在北京时间 17:23:04 至 17:45:46 有 10 次 `/api/batches/.../hook-preview` 被拒绝，HTTP 429，原因均为“制作或预览资源繁忙，请稍后重试”，涉及三个项目。
- 首次失败 trace `10c73f1b4ccd4dd592839af436ec4e58`；最后一次失败 trace `7bc0ddb3740b41688b8237e098036009`。
- 相邻调度快照 17:22:51、17:23:51、17:24:52、17:45:11 显示 render=3/3。17:49:14 降至 2/3，17:50:11 同一此前被拒绝的项目预览返回 200，耗时 2437 ms，trace `630fd434acac481389d04eac14bd4243`。
- 线上配置为 fetch=2、render=3、upload=2、poll=4、hook_previews=1、per_project=1，解析后的总子进程上限为 11。
- 已直接读取生产容器 `webserver.py` 的 `admit_hook_preview()`，与修复前本地相同：字幕预览使用 render 总池；hook_previews=1 是预览子限制，不是额外预留的独立名额。render 满时在进入 compute 前立即抛 ResourceDeferred，HTTP 返回 429。
- 前端 api() 对非成功响应直接抛错，预览按钮调用链没有消费 not_before、自动排队或重试。error_guidance 的默认提示又加入“制作失败可重试，需修改内容时请创建新草稿”，使预览暂时限流看起来像制作失败。

## 本地隔离复现

初始复现脚本已转为正式回归 [test_hook_preview_capacity.py](../../source/tests/test_hook_preview_capacity.py)。复用项目现有 HTTP 测试 fixture 和 FakeProcessTable；使用独立临时数据、真实请求路由/鉴权/资源计数/准入/错误包装，模拟其他项目的存活制作子进程，替换预览 compute，未调用外部 AI 或进行真实多路视频编码。

结果：4 passed in 2.52s。

| 其他项目占用 render 数 | HTTP | compute 调用 |
| --- | --- | --- |
| 0 | 200 | 1 |
| 1 | 200 | 1 |
| 2 | 200 | 1 |
| 3 | 429，截图同款错误及 error_info | 0 |

3 个任务场景中移除一个存活进程，再发相同项目、相同 revision 的预览请求，返回 200。说明触发条件是共享名额耗尽，并非只要有一个项目运行就必然无法预览。

## 本地修复（尚未发布）

后台长任务能够占满字幕交互所需的全部资源名额，预览缺少预留容量/排队机制，前端将暂时资源不足直接展示为失败。生产日志与隔离接口复现相互吻合。

用户明确选择字幕预览不占制作名额，预览自身全站并发上限为3。实现：

- HTTP预览从后台 render、max_active_children 和串行制作准入中分离；后台启动与暂停恢复也不再把预览计入制作资源。
- 沿用集中配置 concurrency.hook_previews 管理独立预览上限；生产配置模板改为3。未配置时仍沿用原保守默认1。
- 保留预览临时磁盘预约、预约恢复和 finally 释放；AI服务限流保持原机制。第4个同时生成请求返回429，释放后可重试。
- 生产实际使用 `/srv/exactcut/config/runtime.json`，当前显式值仍为1。发布时需通过生产运维流程将该值改为3；仅修改仓库模板不能覆盖运行实例配置。此次未部署或改线上配置。

回归先在旧代码验证6项失败（满额429、预览被计入render、后台任务被阻塞），修改后通过。另补充真实多HTTP线程同时进入3个预览、拒绝第4个、释放后重新成功，以及预览不阻止后台暂停进程恢复的检查。

验证命令（source目录）：

```sh
~/miniconda3/envs/py3.12/bin/python3.12 -m pytest tests/test_hook_preview_capacity.py tests/test_step3_resources.py tests/test_round3_preview.py tests/test_phase2_preview_api.py tests/test_concurrency.py tests/test_round1_scheduler.py tests/test_intra_project_scheduler.py -o addopts='' -q --tb=short
```

最终结果：166 passed in 16.37s；git diff --check 通过。

此为准入与资源回收回归，尚不构成3路真实抽帧/AI请求下的吞吐或内存性能验收。预览上限独立后，满载时可同时运行3个制作和3个预览，需要在上线后观察资源峰值；临时磁盘预约不等于操作系统内存硬限制。
