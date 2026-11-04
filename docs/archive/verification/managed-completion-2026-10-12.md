# 纳管项目全部入库状态修复

> 历史归档（2026-11-04）。正文保留当时的方案或执行状态，不代表当前版本；日常操作见[文档入口](../../README.md)。

## 原因与范围

`material_versions.detail()` 以可预览成片为基础返回 review，覆盖交付中、失败、待核实状态，但遗漏 complete。侧栏 `workflow.summaries(compact=True)` 复用该 runtime，造成素材已入库而项目仍待预览。

仅在存在有效素材、当前素材全部为 fbbm_ready、无候选版本且无待确认修改时返回 complete；现有上传中、待核实和失败判断保持优先。完成提示为“全部素材已入库，可在 FBBM 使用”。前端既有 complete 映射和上传完成步骤自动生效。已入库卡片隐藏确认框并清空勾选，保留禁用的“已入库”按钮。

## 验证

隔离临时目录、模拟成片及交付记录，无真实 FBBM 上传或生产数据读写。

- 修复前新用例复现：成功交付仍为 review，删除未交付素材后仍为 review，入库后确认框仍可见。
- 覆盖单条/多条全部交付、部分交付、上传各阶段、失败、待核实、保存修改、候选排队/制作/失败、新版本晋升、删除素材和空项目，并检查详情与侧栏一致。
- Chromium 使用实际页面样式与 renderAssets 验证状态轮询期间确认框隐藏、已入库按钮禁用、回到可预览状态需重新勾选。
- 回归命令：`~/miniconda3/envs/py3.12/bin/python3.12 -m pytest tests/test_managed_completion.py tests/test_material_versions.py tests/test_managed_editor_review.py tests/test_workflow.py -o addopts='' -q`
- 回归结果：128 passed in 10.14s；另将暂存区独立导出，排除工作区其他改动后再次验证，128 passed in 10.87s。
- JavaScript 语法：`node --check exactcut/web/app.js`。

本次为本地修复，未发布线上。工作区已有静态片尾相关改动，提交时仅纳入本次修复。
