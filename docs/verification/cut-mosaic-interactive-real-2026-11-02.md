# 剪切打码交互并发真实验收（2026-11-02）

实现位于 `feat/cut-mosaic-interactive-resources` 隔离树，基于 `1a8d7c0`。下述真实验收期间用户18883未更新。随后父独立review通过，按空闲屏障正常重启完成本地交付；见 [父review与用户升级记录](cut-mosaic-interactive-parent-review-2026-11-02.md)，原URL与数据不变。

**部署目标为无GPU的CPU服务器。** 主线资源隔离提交 `504359c` 与设备无关；Mac MPS兼容修复单独提交 `10e4782`，不作为CPU服务器交付前置。下述完整A/B是在本地Mac/MPS实证；另已用明确device=cpu作有界认证并发冒烟。尚无Linux服务器等价的完整A/B、最大工作量或容量认证，不能将M2Max显存/驱动预算套到服务器CPU。

## 结果与证据

机器为 Apple M2 Max、12 核、64 GiB 统一内存；SAM 2.1 tiny / MPS / 1024，使用原已验证的本地权重与依赖。没有模型替身、禁用 SAM、切 fixed、暂停 A 或占锁/sleep 假装最终生成。

公开机器可读证据：[cut-mosaic-interactive-real-2026-11-02.json](cut-mosaic-interactive-real-2026-11-02.json)。私有完整证据在 `source/.dev-state/interactive-real-20261102/2a90805a/evidence.json`。私有材料、凭据、模型请求/日志与浏览器数据不提交。

| 实证 | 结果 |
|---|---|
| 同账号 alpha：A 真实最终处理，B 真实 Gemini 整片和区域识别、SAM 预览、修改效果、再次播放、最终入队 | B 在 A 完成前 **329.308 秒**进入 queued；A 当时真实 SAM 仍在推进 |
| B 排队后同账号打开 C | 成功；B 冻结，C 可打开；A 不计入 alpha 交互预算 |
| 两个认证账号 beta/gamma 的 Gemini 整片请求 | 真实请求窗口交集 **4.217477 秒** |
| 两个认证账号 beta/gamma 的 Gemini 区域请求 | 真实请求窗口交集 **3.132402 秒** |
| 两个认证账号的 SAM | 实际 mask 写出窗口交集至少 **1.111557 秒**；在双方至少写出两帧后主动取消 beta，gamma 继续完成 60/60，beta 重提后完成 60/60 |
| 预览开关、取消、重提、效果变更 | 实际浏览器与 SAM/FFmpeg 执行、播放验证通过；未取消 A 或其他账号 |
| 旧 single-track | 真实合成 60 帧有音频预览；复用同区域原始 mask，新增 SAM 次数为 0 |
| 旧 cuts 首次准备与扫描 | 独立准备锁后成功；真实 Gemini 扫描完成时 A 仍为 running；报告 actor=beta |
| 最终队列 | A/B 最终任务均完成；整个采样窗口全局最终 active 最大为 **1** |
| 浏览器 | JavaScript 错误 0；独立认证 context，不使用用户原浏览器 tab/localStorage |

本轮共 **7 次真实 Gemini 请求**，保留同一数据根的持久 `ai-rate.json`，集中 `6 RPM / burst 2`，各次重试/实验没有重置桶或换限流域。SDK 方法包装仅记录起止时间并原样调用真实 SDK，不替换输入/输出。旧 cuts worker 的 SDK 诊断没有继承 actor 标签，该次鉴权以真实 HTTP 账号与持久 report.actor=beta 核对；其余双账号重叠证据有明确 SDK actor 标签。

A 的 SAM 300/300 非空 mask，模型 141.714 秒；A 从 running 至完整输出 349.075 秒。B 首次 6 帧 SAM 为 5.016 秒；gamma 60 帧 47.534 秒；beta 取消后的 60 帧重提为 44.760 秒。这些是本机本轮耗时，不是响应时间或真人遮挡质量承诺。

## 输出完整性

- A：原始 6187 帧、1080×1920、30fps、音频保留；完整输出 metadata 与原片一致。
- B：原始 12 帧、6fps，删除最后 2 帧，完整输出 **10 帧**且有音频；解码音频 73728 个 44.1kHz 单声道采样，RMS 2041.05，时长误差在 50ms 内。
- B 首帧目标内部 RGB 为 `[0,0,0]`，外部样点为 `[33,48,70]`；实际视频可解码、播放。
- 真实源片与独立副本的 SHA 均未改变；源素材使用文件复制，独立 inode，未硬链接用户材料。没有确认替换用户素材或发布/上传。
- 另以真实 FFmpeg 回归验证 fixed 20 秒/600 帧完整预览，以及两个 tracked 区域位于 10–20、500–510 帧时完整 500 帧包络；逐帧计数图案、音频、源 SHA 核对，不新增 fixed/包络十秒限制，不截断。

## 真实实证中发现并修复的两个问题

1. MPS 非阻塞状态搬运：同一个 60 帧输入，原 MPS 在第二帧产生非有限 logits；CPU 60/60 有效。保持 MPS、原权重、1024、CPU 状态卸载，仅让 SAM 进程内涉及 MPS 的 `Tensor.to(..., non_blocking=True)` 同步完成后，60/60 有效，20.06 秒。另用 SDK 默认 GPU 状态存储作对照也不再复现；没有把它作为正式运行策略。同步搬运与 GPU 状态对照 mask IoU 均值 0.999109，跨 CPU 后端约 0.925，不能把后端一致性当真人标注质量。兼容处理只在单任务 SAM 子进程内生效并 finally 恢复；web 进程不 patch Torch。新 MPS identity 标记 `blocking_v1`，旧 MPS 派生缓存重算；CPU/CUDA 旧缓存继续兼容。非有限输出仍明确 failed，绝不补零 mask 或 skip 后成功。
2. 旧 cuts 首次 open 仍持最终 compute：真实 A/B 流程中旧入口返回资源忙。已用独立 `cut-prepare.lock` 隔离短复制/探测；新增红测后修复，最后真实旧入口在 A running 时准备和扫描均成功。

失败的临时缓存清理曾删除 worker 根因日志；现在退出非零会将有界日志尾部写入脱敏持久诊断，再清理 work。回归验证日志保留且 api_key 测试值被脱敏。

## CPU服务器主线核验与明确限制

- `mosaic_sam2.track` 只在 `device=='mps'` 时进入同步传输scope；CPU使用nullcontext，仍原Tensor.to。没有在web进程安装Torch patch。CPU原始缓存key不增加MPS标记，旧CPU缓存复用控制测试通过。
- 本地Mac明确 `device=cpu` 的真实HTTP有界冒烟通过：一个alpha最终任务的实际CPU模型与beta/gamma两个认证账号的CPU预览同时执行，每模型6帧/1024。模型耗时13.787、13.326、13.452秒；两个预览真实mask生成窗口重叠10.069秒，均完成时A最终媒体仍running。随后只取消该私有冒烟A以收尾，不将其当作完整CPU最终输出验收。
- 采样自有进程树CPU百分比和最高757.3%，RSS和最高5496432KiB；RSS含共享映射，不能当物理内存。无Gemini请求，原持久限流文件SHA未变化。
- 证据：[cut-mosaic-interactive-cpu-smoke-2026-11-02.json](cut-mosaic-interactive-cpu-smoke-2026-11-02.json)。这是Mac CPU、6帧代表性并发证据，不代表Linux服务器的300帧/长片资源或速度承诺。服务器并发profile须基于其CPU/RSS证据另行核定；本次未在生产压测、未改服务，也没有回退全站单交互槽。

## 配置、容量与局限

`editor_resources` 集中配置全局/每账号的 `preview_tasks/preview_per_user`、`scan_tasks/scan_per_user`、`preprocess_tasks/preprocess_per_user`，默认分别 2/1，整数范围 1–32；`encoder_threads` 默认 2（1–8）。全局或账号满额分别提示“本实例”/“当前账号”，拒绝前不落 running 任务；最终队列保持 `mosaic_queue.max_active_tasks=1`。配置变化应在任务排空后实施。

本轮实际验证至少两个认证交互用户，三路实际模型为一个 300 帧最终任务加两个 60 帧预览；OS free 最低 **48%**。另一次纯三路 SAM 诊断最低 25%，两预览约 46–47 秒，均 60/60。不能把这些结果说成无限用户、所有最大工作量或其他机器均可支持；扩容需要容量证据。

修复后三路各 300 帧容量复测已通过：[cut-mosaic-interactive-capacity-sync-2026-11-02.json](cut-mosaic-interactive-capacity-sync-2026-11-02.json)。三个进程均300/300非空，模型耗时319.63/317.70/319.77秒，整体323.12秒，实际mask输出窗口交集308.264秒，OS free最低25%，未触发18%保护。采样MPS driver约9.993GiB/进程，RSS历史峰约4.16–4.17GiB。它是容量诊断，不替代鉴权业务实证；RSS、MPS tensor、MPS driver不能相加推导物理显存，低熵输入不保证任意真人长视频和其他主机负载的峰值。

## 回归

最终代码全量：**1995 passed, 1 skipped, 10 个原有多线程 fork DeprecationWarning，613.96 秒**。命令在隔离树 `source` 中执行：

```bash
~/miniconda3/envs/py3.12/bin/python3.12 -m pytest -o addopts='' -q
```

包括账号/全局及跨进程预算、父进程死亡与真实子进程退出、阶段 FD 关闭复用、同 key 单次生产/不同 key 并行、完整临时缓存恢复/未完成重算、取消/reset/预准入 permit、旧 single-track stop、迟到回执/停止失败/最终排队 UI 隔离、跟踪不可用提示与显式 fixed 修改等。

用户围观使用另启的只读服务；不启动 dispatcher、不提交模型/最终任务、不修改源数据。Paseo 工作区为「剪切打码 · 只读围观」，服务名 viewer。只读入口服务可保留，测试凭据不写入本公开文档。
