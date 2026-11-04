# 2026-10-13 成片无法预览：只读线上排查

> 历史归档（2026-11-04）。正文保留当时的方案或执行状态，不代表当前版本；日常操作见[文档入口](../../README.md)。

线上版本：us-east-20261012-timing-open-4e70327。app.js SHA-256 前20位：67a217b0c5670d0e41c3。时间以下均为北京时间。

## 已确认

- 图2项目为 d1db07054a054419b7df215e7f9cc5f6；其当前素材为 Rd1db0705、R48452f3588fd439b8a34b9e3f1098f6c。
- 图1显示的 R1cde1c27、Rbc0e40a763b944a8b21886e5a2cc7bdb 属于另一项目 1cde1c2787f64d149c97c8fd948e4e6a。该项目的预览地址接口在12:01、12:02及15:18返回过200。
- 15:21:51，producer05 向 d1db0705… 项目的 preview-url 接口提交图1的两条素材ID，两次均400，原因“当前素材不存在或尚未制作完成”。trace_id 分别 f063640a52354bb5b33a7779a4778ed2、7a8c1516277c4b45b6fd41bafd986854。此前15:21还有其他项目素材被拼到同一项目URL的400记录。
- 15:29:44新页面实例4ce36f12…上报page_ready，frontend_build为67a217b0c5670d0e41c3。15:29:59第一条正确素材、15:30:57第二条正确素材上报media_playing，ready_state=4、media_error=0；之后有拖动后播放记录。
- 线上app.js仍存在直接访问r.plan.required.length的路径：download_complete为假、download_prefetch为真且download_current_episode存在时会执行。它位于renderRuntime内、renderAssets之前。openBatch的缓存分支在try/finally之前调用displayBatch；displayBatch更新current后调用renderRuntime。若此处抛错，会跳过后续获取最新详情及openingBatchId清理。loadPreview则以current.id拼接现存视频元素的data-preview-request。该结构允许异常后旧素材DOM与新项目ID组合。

## 推断与边界

截图中的required异常、顶部排队状态、其他项目素材DOM，与上述异常路径一致。请求混用已证实；当时内存中的runtime及异常堆栈未留存，尚未用完整浏览器回归证明全部触发步骤。

失败时间段没有足够浏览器事件识别其frontend_build；不能将后续新页面的版本倒推为失败页面版本，也不能断言用户执行了刷新。新页面正常播放说明重新进入可以恢复本次项目，但不证明当前代码没有缺陷。

本次仅查日志与只读文件，未重启、重新制作、修改生产配置或发布代码。建议保留未保存内容后刷新作为临时恢复；后续修复下载计划空值处理及项目切换异常清理，并做缓存切项目回归。
