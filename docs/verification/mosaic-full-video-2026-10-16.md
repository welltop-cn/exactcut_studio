# 完整打码视频本机验收

2026-10-16，分支 feat/mosaic-gemini。实现 fecf6ae；采用数量文案修正 cc6a68d。8801 development 服务 PID 93125，healthz ok/release development-mosaic-fecf6ae；运行中未重启。其他共享服务未操作。

## 真实浏览器流水线

当前项目 2cf132715d8e47719f5c275f0f6f2d13，mosaic session af76526711a68cef0f51fbabd65cb1ac。保持用户本机已有采用记录，6 个片段共 8 个区域。01:45 的无框片段没有采用，不包含在处理范围。

通过页面“生成完整打码视频”发起，job fdf9bb80bc321e7821336767debe0b7e。08:36:30 至 08:42:52 UTC，约 381.5 秒，包含复用缓存与实际 SAM 2 推理、遮罩合并、整片编码及校验。关闭编辑器再打开能恢复运行进度；完成后页面提供原生视频预览和“下载完整打码视频”链接。

- 输出 148 秒，4440 帧，30/1 fps，1080×1920；音轨 148 秒。
- 文件大小 113646609 bytes；SHA256 222cd447e34ea51fa973cc2f149a0e96971dc26801fd472b40971a06c9293e6e。
- 原片 SHA256 仍为 6f97805d40f3b6fd2aa9c1690f73dbd4d1b6cc96a4a651823c3d746efecdcca7。
- 原片与输出的音轨以 FFmpeg stream copy 导出 ADTS 后 SHA256 相同。
- 私有成片位于本机 data/.web-state/batches/.../mosaics/.../render-fdf9bb80bc321e7821336767debe0b7e/mosaic.mp4，不加入 Git。

## 效果限制

140 帧空遮罩，已整帧像素化：第 1 区域 119 帧、第 4 区域 7 帧、第 6 区域 14 帧。页面明确提示 140 帧需检查。此为生成、恢复、完整性验收，不是逐帧遮挡准确性验收；局部目标漂移仍需人工预览。无框未采用的片段保持原画面。

## 自动回归

30 项 UI 测试、28 项 render/tracking/locate/scan 测试通过。真实 FFmpeg 测试包括重叠遮罩合并、失跟整帧回退、分数帧率不增减帧、原音频字节保留、认证/CSRF、关闭重开和下载入口。node --check 与 git diff --check 通过。

顶部原“8/8”实际上是采用区域数/上限，容易误解为全部片段已处理。现明确写“8 个区域（上限 8 个）”，无框片段写“未采用 · 尚无区域，请先框选”；不修改采用记录。
