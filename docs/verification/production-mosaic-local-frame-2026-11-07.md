# 2026-11-07 打码时间轴本地预览发布

## 授权与范围

用户明确要求“合入main分支，发布”。发布打码编辑器的播放器本地预览改动；本次不调整剪切编辑器。

main 合并提交 `c631b03bd955936b080217708ace95c2bc1adb0b`；应用文件与 D3 审核的 `4f30305` 一致。发布回归发现 Node 时间轴测试夹具缺少新增的 `seekFrameTimer` 声明，补齐后提交 `79b2ca863b36b7b2b4661546d25de678417388c4` 并推送 main；应用代码未再变化。

## 行为

拖动时播放器就绪便直接显示其画面，不再为每次拖动请求服务端帧。打开时保留首次后台帧读取；视频跳转未完成时最多等待 1.5 秒，失败或超时后保留服务端兜底。识别状态读取随画面就绪触发。

## 回归证据

- `c631b03` 首轮本机全量：2572 passed、1 failed、10 skipped；Linux 全量：2157 passed、1 failed、424 skipped。唯一失败均为 `test_timeline_and_seek_handlers_with_delayed_callbacks` 中 `ReferenceError: seekFrameTimer is not defined`。
- 补齐夹具后，时间轴与本地预览定向测试 22 passed。
- 最终提交本机全量：2572 passed、10 skipped、10 warnings，768.85 秒，退出码 0。
- 最终提交 Linux 发布管理器全量：2158 passed、424 skipped、10 warnings，719.95 秒，退出码 0。Linux 验证镜像未安装浏览器，不能把跳过项称为已通过。

## 私有证据位置

- 本机：`/tmp/exactcut-mosaic-79b2ca8/`。
- Linux：`/srv/exactcut/preparation/mosaic-local-frame-79b2ca8/`。
- 候选：`/srv/exactcut/releases/us-east-20261107-mosaic-local-frame-79b2ca8/`。

## 验收边界

本次不触发生产 AI、生成、保存或交付。UI 自动化的本地 seek 就绪路径采用受控视频状态；未测量用户网络下真实原片拖动延迟，也未验收 iOS Safari。服务健康和资源哈希核验不能替代用户手感验证。

## 正式发布与核验

北京时间 2026-11-07 23:32 发布成功，管理器切换用时 64.87 秒，退出码 0。

- release：`us-east-20261107-mosaic-local-frame-79b2ca8`。
- commit：`79b2ca863b36b7b2b4661546d25de678417388c4`。
- image：`sha256:de75bcd59ae882621a97e587e4671231d39ff7f74ac589ec8fda84137bf3959f`。
- 前驱：`us-east-20261107-errors-4a5bc20`。回滚前重新核对活动任务，通过候选管理器的 rollback 回到该封存版本，保留最新业务数据。
- 发布前 `check-switch`：allowed=true，active_tasks=[]，phase_blockers=[]。
- 发布后公网 health：status=ok、production、maintenance=false、worker_alive=true，新 release 正确。
- 公网页面、侧栏、用户提供的项目 `171f25e152e845bbbf3759661819fdfd` 均返回 200。页面引用的 8 个本地 JS 均返回 200，SHA256 与固定候选源码一致。
- runtime.json、production.env、deployment.json 的 SHA256 与准备前一致。
- 仅文档的后续提交不需重建应用镜像；线上应用固定在上述 commit。
