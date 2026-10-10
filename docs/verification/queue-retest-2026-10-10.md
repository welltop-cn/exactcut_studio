# 四项目队列实测与素材计时验收（2026-10-10）

## 四项目实测

沿用剧101494及上轮四项目各2条素材的实际配置（时间线、Hook和选项保持一致），仅使用新的请求ID和测试账号。测试版本 us-east-20261010-queue-progress，代码 ea4b5e7。

- 51/51检查通过；8/8成片质检与全音视频解码通过，均为1080×1920、30fps、H.264/AAC，时长分别120/140秒。
- 整批691.7秒（约11分32秒），全局最高3路、单项目最高2路；两用户均获得执行机会。
- 普通用户只看到本人任务，管理员看到完整队列；全局计数一致。实际页面验证运行与完成状态，结束后队列清空。
- 四项目执行历史包含创建至制作结束；预览Range请求全部206。
- 采样接口响应最大0.1秒、中位0.008秒。
- 本轮8条均复用剪辑缓存和既有原片，不构成冷缓存下载性能测试；不能把与此前21分5秒的差异全部归因于去掉项目限制。

项目ID：
- 6f41187deffc40fc9f9e1d1a5f39a7b2
- 328a283358104d3eb4e8c60cf6ce327f
- 683606a5a550445f8a14a0df185866ca
- b5e1ddb5ac3d4170ba4c78d9d99130bc

服务器证据保留在 `/srv/exactcut/preparation/drama101494-queue-retest-20261010/` 的 report.json 与 media-verification.json。账号、项目和视频均保留。运行截图：[队列](queue-progress-live-2026-10-10.png)。

## 新增计时

代码27ce058：每条素材展示制作排队时间，以及提交准备任务至render worker完成（含生成预览）的总墙钟时间。计时基于持久化job.created、started、finished及parent_id关联，不使用页面打开时间；完成后固定，上传/poll不影响。暂停时间包含在墙钟总时长中。无历史时间记录时不伪造数字。

新API字段：素材 timing={queue_seconds?,total_seconds?,complete}；活动队列任务新增queue_seconds。针对排队、运行、完成固定与缺少历史记录验证，并检查完成卡片文字。

发布：2026-10-10 21:04 上海，版本 us-east-20261010-asset-timing，提交27ce058a0aa3bc5ce6123814d5d361c214802446。本地全量通过；Linux 834 passed / 2 skipped（169.72秒），健康检查ok、maintenance=false、worker_alive=true。

线上对8条素材重复读取，计时全部固定且total>=queue>=0；浏览器实际展示R683606a5b：制作排队3分29秒，准备至预览总用时7分43秒。截图：[计时卡片](asset-timing-live-2026-10-10.png)。
