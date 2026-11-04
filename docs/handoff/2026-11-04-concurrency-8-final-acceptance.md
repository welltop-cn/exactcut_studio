# 并发 8 分支：两台 Mac 补验与修复收尾（2026-11-04）

接续 [原交接](2026-11-04-concurrency-8.md) 和 [接手记录](2026-11-04-concurrency-8-takeover.md)。本轮已完成两台独立测试实例的接管、三项核心修复及六项补验。不同用例的真实/受控边界见下表；不将模拟条件称为真实厂商故障或物理磁盘耗尽。

**main 未合并、未推送；未开 PR；线上服务未变更。** 最终应用代码 `098ffad`，两台测试服务均已加载该提交。验证使用两机各自已有、从原地址获取的真实源片缓存，没有跨机器传视频。

## 已推送的修复

| 提交 | 分支 | 问题与最终行为 |
| --- | --- | --- |
| `6d538c2` | `fix/pause-resume-executors` | 媒体超时扣除执行器实际 SIGSTOP 时段；普通制作、可取消任务和租约路径均覆盖，真实超时仍终止并回收进程 |
| `58eea96` | `fix/pause-resume-executors` | failed 候选不再被当作需冻结的执行器，可以完成退出并释放名额；候选准入和提交保护仍拒绝失效尝试 |
| `098ffad` | `fix/managed-retry-backoff` | 纳管 render 的 `not_before` 未到时保留原持久等待任务；到期后才创建新执行容器，避免更换容器绕过退避 |

`fix/managed-retry-backoff` 包含前两项修复，并基于原并发集成分支；不是已合并 main 的发布版本。产品改动限于 `batch_control.py`、`processing_cancel.py`、`workflow.py`、`material_workers.py`。控制记录增加可选的执行器暂停计时字段，schema 版本仍为 1；没有修改业务并发参数、媒体效果、超时时限或外部交付路径。

## 两机自动化回归

最终固定应用代码 `098ffad`，两机同一组 **304 项通过，0 失败、0 跳过**：M2 30.213 秒，Mac mini 22.350 秒。此前 `58eea96` 两机各 303 项通过，新增一项为实测发现的退避回归。

```sh
cd source
<python> -m pytest -q -m 'not ui' \
  tests/test_processing_group_cancel.py \
  tests/test_processing_stage_leases.py \
  tests/test_batch_control.py \
  tests/test_concurrency.py \
  tests/test_concurrency_recovery.py \
  tests/test_round1_scheduler.py \
  tests/test_step3_resources.py \
  tests/test_material_versions.py \
  tests/test_round1_integration.py \
  tests/test_round1_evidence.py
```

显式覆盖 integration，避免默认 `not integration` 选择器漏测。Mac mini 第一轮出现三项找不到 Node 的环境失败，补齐既有 Node PATH 后复跑通过，未修改产品代码绕过。此轮未合 main，因此没有另跑完整全量。

## 三项核心故障的红绿证据

### 暂停/归档后错误媒体超时

新增 `test_media_timeout_excludes_batch_pause`：真实执行器和真实媒体子进程，主动工作 0.25 秒、timeout=0.7 秒，调用批次控制暂停 1.1 秒再继续。普通、cancellable、leased 三条路径在修复前均失败；修复后覆盖 pause/archive 两种操作共六种组合。另有三条真实卡死命令仍被超时终止和回收的检查。

真正制作中的 Chrome 补验：

- M2 项目 `9f993c13441145169d8eb70b1c554c14`，确认有 FFmpeg 工作后暂停，约 161.68 秒后继续；同一候选正常成为 current，render completed，未补传或重做。
- Mac mini 项目 `a7daf182f4b74e05b77eedacdc76006c`，确认有 FFmpeg 工作后归档，约 168.10 秒后复原并继续；同一候选成为 current，render completed，未补传或重做。
- 两条均为 150 秒、1080×1920、有音频；主片及预览 SHA 匹配账本、QC passed、OSS test 回执齐全，Chrome 实际播放已通过。

此前 M2 项目 `dafc87806f9d4103847e868cb181ffc5` 在上传阶段暂停后发生 `original.oss_failed/RequestError`，首次失败；通过现有 media-upload 补传入口后成为 current 并播放。这条保留为失败后补传证据，**不计作一次通过的制作中暂停验收**。

### failed 候选再次被冻结

接手时 M2 两个、Mac mini 一个执行根处于 T 状态，父项目均未暂停或归档。M2 现场候选实际因 OSS 成片/预览回执校验失败，随后被 `batch_control.read()` 的 failed→stale→paused 投影再次 SIGSTOP。

`test_failed_candidate_executor_exits_and_releases_resident_slot` 通过真实进程复现：候选标记 failed 后，旧代码会把本应退出的执行器停住并导致等待超时；新代码允许其退出，reconcile 不再统计该执行根，同时旧成片未被替换、失效候选无法重新进入执行。

两台测试服务从固定提交正常接管后，原三个执行根已退出，旧任务已收尾；未直接篡改其任务状态或回执。最终所有普通及交互队列为空。

### 退避截止时间被新容器绕过

在 mini 可见 Chrome 中给一个指定验收项目注入一次 `ResourceDeferred(now+15)`：

- 修复前旧 render 持久记录 `not_before=1793764208.9240162`，替代 render 却在 `1793764198.238049` 开始，提前约 10.69 秒。旧任务被标记 interrupted 后，新容器的 fetch 未保留等待约束。
- 新回归 `test_managed_render_retry_keeps_backoff_until_fresh_child_is_due` 先在旧行为上跑红，再验证截止前容器身份、queued 状态及原 not_before 保留；到期后才替换。
- 修复后 mini 项目 `9cc6a19d4c754257b8cf41ebc70dc7c4`：原任务持续 queued，原 child 保持不变；deadline=`1793764679.195934`，新 fetch 创建于 `1793764679.4461498`，开始于 `1793764679.535875`，晚约 0.34 秒，没有提前执行。
- 该项目后续真实制作、主片/预览上传、QC、current 晋升及 Chrome 实际播放全部完成。

## 原交接六项补验

| 用例 | 本轮结论 | 实测边界 |
| --- | --- | --- |
| 多账号轮转 | PASS（所列用例） | M2 真 Chrome、两个独立认证账号、A 四项目/B 两项目；积压时记录到账号交错的补位，六个项目最终均有 current 输出。首次失败与输入修正另记，不声称全部首轮成功 |
| AI 60/min、burst 5 | PASS（预算桶契约） | 两机读取实际配置，用 12 个真实进程和真实持久状态验证总 burst=5、每秒补 1、空闲后封顶 5；使用受控时钟，未发送付费提供方请求，不认证供应商额度或 Gemini 所有入口 |
| 制作失败/资源延后与退避 | PASS（修复后） | mini 真 Chrome + 一次指定 ResourceDeferred，证实等待到期再重建；另实测普通失败后的手动重试、OSS 补传复用。不是任意 RuntimeError 都会自动重试 |
| 磁盘预留等待 | PASS（受控容量拒绝） | mini 仅对指定项目让 storage.reserve 拒绝；约 132 秒内任务保持 queued、无 started、无执行器。放行后 0.48 秒开始 fetch，完成真实制作及 OSS 校验。没有实际填满物理磁盘 |
| 关闭标签后后台继续 | PASS | 接手阶段 M2 真 Chrome 提交 150 秒任务并关闭自有标签；后台 render 约 174.46 秒完成，重新打开后实际播放 |
| 同一预览重复上传 | PASS（应用函数边界） | 两独立进程同时请求同一全新 OSS test 对象，两个相同有效回执，实际 PUT attempt=1/ok=1；使用真实 OSS，不是两标签冷上传按钮用例 |

故障注入仅存在于 mini 私有测试启动适配器：复用原外部写入守卫，对指定验收项目施加一次容量拒绝/一次资源延后，没有改集中业务配置或产品源文件。**全部用例完成后已退出该适配器，两台均恢复普通测试启动方式，应用代码同为 098ffad。**

## 多账号项目的首次失败与最终输出

提交时间顺序为 A1/A2/A3/A4，再 B1/B2。初轮 render 的账号序列为 A、A、B、A、B、A；排队的后四个任务等待约 85–89 秒。单次有限用例证明所列调度与完成情况，不将其扩展为无限负载公平性证明。

| 项目 | 首轮/修正 | 最终 |
| --- | --- | --- |
| A1 `8ecadbe56af94efc85143548a831f928` | 正常完成 | current，150 秒 |
| A2 `fa04e075024847c9be4fcf9b53958393` | 正常完成 | current，150 秒 |
| A3 `4b17d3a722ed432785cf95a9bf186649` | 正式成片 OSS RequestError，现有补传重试 | current，150 秒 |
| A4 `f114b67048c64e63b3a5f64792335fa8` | 正式成片 OSS RequestError，现有补传重试 | current，150 秒，最终 Chrome 播放通过 |
| B1 `ada5f25b33344ee0a4bc32b880332062` | 测试准备误复制了 AI Hook，服务未配置；经 UI 清空 Hook 后重新确认 | current，约 30.03 秒 |
| B2 `0fe08db7715a47319b800a52869efca4` | 同 B1，旧失败候选保留为 superseded | current，约 30.03 秒 |

没有删除、伪造或弱化失败记录，也没有关闭 OSS 校验来获得通过。最终 B2 也已实际播放，B 账号读取 A 项目详情返回 404。两机最终审计所列十条输出（M2 七条、mini 三条）均 current、QC passed、1080×1920、有音频，主片/预览真实文件 SHA 与账本一致，两个 OSS 回执均属于 test 命名空间。两机编码文件 SHA 不要求互相一致；核对的是各自输出与各自批准/预览/QC 账本。

## 最终服务与保护状态

- 两台 health 均为 status=ok、worker_alive=true、maintenance=false，release=`development-backoff-098ffad`。
- 普通 active/queued/waiting_resource=0，交互 pending/active=0，最终打码 queued/running=0，交付 upload/poll job=0；两机各 8 个现有 execution-lock 文件均可获取，无遗留执行锁占用。
- 既有配置保持一致，仅机器路径和端口不同；render3/fetch2/upload2/poll4、AI60/burst5、SAM CPU、preview.namespace=test、delivery_enabled=false。
- FBBM/飞书/CF-Ads 守卫继续开启，未上传或发布 FBBM，未变更生产。制作助手 `#helpNav` 在最终实际页面仍隐藏。
- OSS 日志中去重 key 分别为 M2 31 个、mini 30 个，全在 `exactcut-previews/test/`；包含交接前记录，不是本轮新建数量或完整云端对象清单。
- 保留媒体与测试对象供页面验收，未执行 OSS 删除；后续清理仍须先列精确清单，保留在用回执的可播放性。
- Chrome 测试窗口保留供查看。各机器本机入口分别为 `takeover-m2.localhost:18793/home/`、`takeover-mini.localhost:18792/home/`，从侧栏选择相应验收项目；两个 localhost 入口不能跨机器混用。

原始 JSON、JUnit、事件、对象写入账本、故障适配器及截图留在各机私有测试目录。公开记录不包含绝对路径、IP、账号口令、签名 URL 或密钥。

## 本轮未扩展的事项

main 合并仍待用户明确决定；P1–P4 性能专项、SAM CPU 提速、暂停时 UI 用时统计、剪切入口短暂 busy、取消排队文案、停服排空、配置残留及 Gemini 扫描预算等仍按原交接范围单列。本轮没有修改这些行为，也没有将全部存量问题标成已解决。
