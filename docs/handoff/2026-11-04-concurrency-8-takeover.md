# 并发验收接手记录（2026-11-04）

基线交接：`test/concurrency-8` / `2919583`。本次使用独立记录分支，未改产品代码、未合并 main、未操作线上服务。

## 两机接管与保护

- 已只读核对 M2 Max、Mac mini 的现有测试服务、进程工作目录、代码和任务状态。
- 两机运行源码均为 `8061f2d`；与交接提交之间只有交接文档差异，未热改正在加载的源码。
- 对实际集中配置做规范化比较：除 SAM checkpoint 的机器路径外一致。render=3、fetch=2、upload=2、poll=4，AI=60/min、burst=5，SAM device=cpu，OSS namespace=test，delivery_enabled=false。
- 实例按既有 production 测试上下文运行，有独立数据根及外部写入守卫；该上下文不代表线上服务。保留 FBBM/飞书/CF-Ads 拦截，没有发布、交付或生产对象写入。
- 接手时 M2 有两个 render 执行根、Mac mini 有一个执行根处于 T 状态。对应父项目都未暂停、未归档。没有重启服务、直接恢复这些进程或改写其任务状态。
- 使用独立主机名和本会话新建的 Chrome 标签，避免覆盖既有验收标签和认证 Cookie。测试目录、凭据、媒体、原始日志和复现脚本保持私有，不随 Git 分发。

## 本轮已执行

### 两机自动化回归

两机分别运行相同命令，均退出 0，共 120 项通过：

```sh
cd source
<python> -m pytest -q \
  tests/test_concurrency.py \
  tests/test_round1_scheduler.py \
  tests/test_step3_resources.py \
  tests/test_round2_storage.py \
  tests/test_preview_oss.py \
  tests/test_no_transfer_slots.py
```

范围包括账号调度公平性、资源延后、AI 持久桶与跨进程锁、磁盘预留、OSS 对象锁和回执复用。部分用例使用替身、受控时钟与模拟容量；不将其算成完整真实 Chrome 故障验收，也没有本轮再跑全量。

### 关闭标签后后台继续：PASS（M2）

1. 真 Chrome 登录独立验收入口，复制已有 101606 项目配置，命名为“接手验收·关页后后台继续”。使用该机器已经从原地址取得的五集正片，没有跨机器搬运视频。
2. 在页面保存、校验并确认制作，确认任务已排队，然后关闭本会话标签。
3. 未重新打开页面期间，fetch 及 render 均完成。render 执行约 174.46 秒，候选晋升为 current。
4. 新开标签，重新选择该项目；页面显示 1/1 制作完成。预览 readyState=4、duration=150.000998 秒；真实点击播放控件后 currentTime 前进至 4.67 秒、paused=false。
5. 服务器有 preview.oss_ready；新任务最终无错误。未点击上传 FBBM 或替换其他项目成片。

可关联的测试身份：parent=`6a78a142d6ee418da571489f8000d9a3`，candidate=`f91f61e5dc6c40a0939c3efa7ceb0f60`，render job=`9c3138908e9c5568b82bd7ab6b0a1850`。

### 同一预览重复上传：PASS（M2，真实 OSS + 应用对象锁）

- 两个独立 Python 进程同时调用应用已有 `_ensure_uploaded`，无旧回执，使用新随机测试对象前缀和同一 6,698,933 字节预览。
- 两进程返回相同回执；内容 SHA-256 与本地视频一致。应用原有 HEAD/长度/SHA/private ACL 校验成功。
- 守卫日志按精确对象 key 统计：`put_object_from_file` attempt=1、ok=1。另一请求复用既有远端对象，没有第二次 PUT。
- 此并发验证经应用函数执行，非两个 Chrome 标签同时点击上传；Chrome 重开播放在上一个用例单独覆盖。
- 新增对象未删除，待最终清理时先列清单：

```text
exactcut-previews/test/480b021240e14047bae404b29e476d36/takeover-preview-dedup/46c5575c6e6286da32b66046a29bc6b62b6329bbf424ece1f108430ac1371a35.mp4
```

## 核心故障：已复现，尚未修复

### 1. 暂停时间计入媒体超时（两机均复现）

私有最小复现沿真实 `media.run_checked -> processing_cancel.run -> subprocess.run` 调用链运行：媒体子命令主动工作 0.25 秒，timeout=0.7 秒；等媒体启动后暂停其调用者整个进程组 1.1 秒，再继续。两机均确定性失败，异常链为 `subprocess.TimeoutExpired -> RuntimeError: 媒体处理超时，已停止，可查看日志后重试`。

结论：暂停的墙钟时间被 subprocess 的超时计算包含。不是素材必须处理超过正常时限。复现只暂停新创建的进程组，不操作既有服务或制作任务；结束回收自有进程。

本轮直接覆盖无 cancellation scope 的普通制作路径；带 cancellation scope/lease 的另一路也使用单调时钟差值，但该路径尚需独立红绿验证，不能算已完成修复覆盖。

### 2. failed 候选在退出前被调度器再次 SIGSTOP（已确定性重放）

交接称其疑似“暂停/继续读写竞态”。本轮获得更具体的另一条因果链：

1. M2 现场两个冻结候选实际都因“正式成片与预览均需完成 OSS 校验”标成 failed；日志在 OSS RequestError 和 pipeline checkpoint 之后出现调度器 SIGSTOP。不能把这两个现场都归因于暂停超时。
2. `batch_control.read()` 对内部 candidate 把 version.status=failed 视为 stale，并合入有效 paused，尽管父 control.paused=false。
3. `reconcile()` 看到有效 paused 后发送 SIGSTOP。failed 不是 `cancel_invalid_children()` 当前主动回收的失效条件，进程因此可能在结束前停住并持续占 render 名额。
4. 将现场父/子轻量元数据复制进临时目录，使用 `FakeProcessTable` 表示仍活着的已验证执行根、只替换 `os.killpg` 系统调用，真实调用 `read/reconcile`：输出 parent paused=false、effective child paused=true、scheduler signal=SIGSTOP。期望“不因业务终态冻结尚未退出的执行器”的断言失败。

该重放不向现有或任意真实 PID 发信号，也没有依赖并发概率。它证明 failed 分支可以独立导致冻结；不据此声称排除了所有暂停/继续竞态。

## 剩余验收矩阵

| 交接待测项 | 本轮状态 | 边界 |
| --- | --- | --- |
| 多账号轮转完整结论 | 自动化相关项通过；两机完整 Chrome 重跑待做 | 冻结执行根仍占 render 名额，先处理核心故障 |
| AI 60/min、burst 5 | 两机配置一致，相关桶回归通过；真实完整限流验收待做 | 没有额外发送批量付费模型请求 |
| 制作失败/资源延后重试退避 | 相关延后回归通过；真实 Chrome 故障注入待做 | 不把资源等待与任意制作异常自动重试混为一谈 |
| 磁盘预留等待 | 相关模拟容量回归通过；真实 Chrome 等待/恢复待做 | 没有填满机器磁盘或修改既有服务配置 |
| 关标签页后任务继续 | PASS | M2 新任务，真实后台完成、重新打开及播放 |
| 同一预览重复上传 | PASS（真实应用函数并发 + OSS） | 两进程、同一新对象、一次 PUT；非双标签冷上传 UI 验收 |

## 下一步修复边界（待确认测试层）

建议分两条最小修复：

- 复用 `media.run_checked/processing_cancel.run` 调用边界，以真实进程组暂停/继续验证：暂停不消耗运行超时、真正超时仍回收进程，兼顾取消和 lease 路径。
- 复用现有 `batch_control.reconcile` 调度边界，覆盖 managed candidate failed 后执行器能够退出、释放名额；再用两机真 Chrome 补暂停、归档、恢复和后续任务补位。

未写正式新回归、未改产品代码。按 tdd 技能的显式要求，修复前需与用户确认测试边界。完成两条核心故障修复后，再继续四项未完成的完整验收；非核心文案、性能专项、main 合并和线上变更维持原交接决策边界。
