# 重新制作流程与计时审查（本地）

> 历史归档（2026-11-04）。正文保留当时的方案或执行状态，不代表当前版本；日常操作见[文档入口](../../README.md)。

## 已确认问题

1. 部分素材重做时，未改素材沿用旧成片，却作为本批完成展示旧计时。真实只读检查项目 b3910f0897064d9d8aa6567c739b14e7：第一条新候选 generation=7 正在运行；第二条没有候选，total_seconds=669、queue_seconds=438，制作日志起于 01:58:08Z。
2. 保存已改配置但尚未确认时，旧 preview 让执行卡继续显示制作完成。
3. 当前成片计时从整个父项目任务挑最新同 request_id render，未按 current version 子项目隔离，可能被 superseded 任务污染。隔离回归复现 current 40 秒和 superseded 800 秒冲突。
4. 准备任务失败且无 render 时，elapsed 使用 now，失败后的耗时继续增长。完成准备但仍等 render 的情况必须继续计时，两者不可混淆。

## 修复边界

- 以最新有效 confirm 的 immutable receipt、revision 和 version_ids 识别制作范围。新增兼容字段 execution_version_ids 记录同版本上传重试及确认时已有活动候选；旧 receipt 使用 version_ids。空确认不抹掉最近有效范围。schema 仍为 1。
- detail 新增 production_scope=run/reused/pending；候选属于 run，当前版本不满足 desired_generation 时为 pending，未参与最近制作范围的已有成片为 reused。
- reused 卡显示沿用已有成片，不展示旧耗时/日志，不计入本轮分母。pending 只提示确认制作，旧片仍可在成片预览访问。
- 当前成片和候选各自只统计自己子项目任务；legacy 当前版本使用父项目任务。失败或中断 fetch 的 elapsed 在 finished 停止；成功 fetch 等待 render 继续计时。

## 检查覆盖

针对回归包含新建/复制既有流程、部分制作与完成晋升、重复确认、上传重试、版本过期回调、暂停恢复、保存未提交、前端计数与旧片预览保留、失败准备计时。新增两素材真实本地生命周期：一条复用、一条生成；完成及再次确认后范围不变。

未修改生产任务、成片或权威数据。当前修改未发布；不以本次检查保证所有功能无缺陷。

隔离完整回归 1026 项通过，git diff --check 通过。
