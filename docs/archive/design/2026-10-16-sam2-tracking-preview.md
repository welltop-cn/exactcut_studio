# SAM 2 单区域动态预览

> 历史归档（2026-11-04）。正文保留当时的方案或执行状态，不代表当前版本；日常操作见[文档入口](../../README.md)。

首轮只验证一个已采用区域、最多 10 秒且不超过 300 帧。前端提交保存后的整数像素框与源视频整数帧范围；未保存的调整不能启动跟踪。它不改变原片，也不写入剪切任务。

## 契约

- `POST /api/batches/{bid}/mosaics/{sid}/track`：`{reset_at, target:{start,end,seed_frame,x,y,w,h}}`，结束帧排他。返回 202 与 `{schema_version:1,id,status,source_sha256,reset_at,target,created_at,progress}`。
- `GET .../track/{id}`：同一任务，状态 running/completed/failed；完成结果 `result` 含预览宽高、逐帧 `{frame,box,area}`、`warnings:[{frame,reason}]`、耗时、模型与设备。box 是预览坐标的 `[left,top,right,bottom]`，空遮罩为 null、area=0。
- `GET .../track/{id}/preview`：认证、项目权限及源版本验证后流式返回预览 MP4。
- 源 SHA、reset、目标和配置共同决定缓存键；读取运行状态可恢复中断任务。同一 compute lease 限制重型视频任务并发。

## 模型与合成

官方 SAM 2 源码锁定 `2b90b9f5ceec907a1c18123530e92e794ad901a4`。接口依据 `sam2/sam2_video_predictor.py` 的 `init_state`、`add_new_points_or_box(box=...)`、`propagate_in_video(start_frame_idx=...,reverse=...)` 与 `sam2/build_sam.py`。

用 FFmpeg 按帧 trim 提取 JPEG（宽度至多 640），按比例映射原始提示框。SAM 2.1 tiny 从参考帧双向传播；每个原始帧必须有结果，不能静默丢帧。二值 mask 以 11×11 最大值滤波向外扩展，再用 Pillow 合成像素化预览帧；FFmpeg 编码并映射对应片段音频。SAM 2 的可选 CUDA 扩展后处理关闭，避免 Mac 依赖 CUDA。模型在独立 Python 子进程运行，依赖通过配置的 dependency_paths 加载，不修改共享环境。

空遮罩在预览中整帧像素化并警告；相邻遮罩面积变化超过 2.5 倍提示复核。这是启发式标记，不是漏遮保证；模型可能扩展到整个人而不是所框局部，也可能在切镜、遮挡时漂移。首轮无多关键帧纠偏和整段导出。

## 本地配置

既有配置读取器 `cutting.tracking`，默认关闭：enabled、checkpoint、model_config、device、dependency_paths。验收配置额外固定 checkpoint_sha256 和模型源码 model_revision，纳入缓存身份。依赖：torch 2.7.1、torchvision 0.22.1、hydra-core 1.3.2、iopath 0.1.10、tqdm 4.67.1、Pillow 11.2.1、numpy 2.2.6。以指定 Python 3.12 的 pip --target 安装至忽略目录。首次安装依赖/下载权重不计入模型跟踪耗时。

模型代码及权重遵循官方 Apache 2.0 许可。源码与权重均为本地部署，视频不上传模型服务。

首轮实测限制：同一目标在 MPS 多次运行出现全部非空与大量空遮罩两种结果，尚未定位数值/模型根因。CPU 短段可行，但全段也出现空遮罩，不能视为稳定替代。失跟结果仅保留作为诊断预览，下次发起同一请求会重算；非有限 logits 直接失败。详见验收记录。
