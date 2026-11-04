# 动态打码技术可行性调研

> 历史归档（2026-11-04）。正文保留当时的方案或执行状态，不代表当前版本；日常操作见[文档入口](../../README.md)。

> 历史调研归档：2026-10-30 整理。本报告保留 2026-10-16 的调查结论、当时的代码快照和待验证建议。后续主线已形成独立的 `mosaic_*` / 跟踪实现，本文中的旧 `cutting.py` 接入状态和建议不作为当前实现或上线能力说明。历史实验源码保存在 `archive/mosaic-sam2-scratch-20261030`；当前发布边界见[发布准备清单](../verification/release-preparation-2026-10-30.md)。

日期：2026-10-16。范围：技术事实、仓库接入边界和验证计划；不代表跟踪质量验收，不设计 UI、不修改业务。外部依据为官方文档和源码，`main`/`4.x` 为调研时版本，实施前需锁定版本。以下“建议/推论”均不是模型保证。

## 决策结论

**动态移动、缩放的打码可行；任意身体局部随形变准确遮挡，需要独立验证，不能承诺框一次就全程不漏。** 按用户最新要求，优先通过现有 skill 调用托管 SAM 3 视频模型；用托管 SAM 2 二值 mask 序列作为输出契约更清晰的验证基线。CPU 的 CSRT 仅作比较基线。各路线使用同一批短视频和同一漏遮判定。人工关键帧修正应作为基础能力。完成验证后再给 agy 真实能力边界进行 UI/交互设计。

优先级建议：fal genmedia skill + 托管 SAM 3 接口契约验证 → 托管 SAM 2 对照 → 人工关键帧纠偏与导出验证。SAM 3.1 是当前官方后继，但自托管依赖 CUDA、权重申请和不同许可证；托管 SAM 3 服务名称不等于已经使用 3.1，不自行推断后端版本。先不加入“自动发现全部敏感部位”需求，它与“跟踪用户选中的部位”是两个问题。

## 路线比较

| 路线 | 已核实能力 | 对本任务的意义与边界（推论） |
|---|---|---|
| OpenCV CSRT | 用户初始化框；更新矩形位置；源码以 DSST 更新尺度；宽高乘同一尺度因子 | 可跟踪任意外观区域，无人脸类别限制；能移动、等比缩放，不能表达肢体非刚性形变、独立宽高变化或真实轮廓。作为 CPU 基线合适 |
| OpenCV KCF | 用户初始化框；实现更新位置，输出尺寸基于初始化 ROI | 适合作对照；此实现不能当作自动缩放方案，更不提供分割轮廓 |
| 稀疏 LK / 稠密 Farneback 光流 | LK 输出点位、status、err；Farneback 输出逐像素二维位移场 | 可以估计框运动或传播遮罩，但需自己做前景点筛选、几何拟合、误差检查与重初始化；背景混入、遮挡和累积漂移需验证，不是开箱即用对象跟踪 |
| SAM 2.1 视频分割 | 点、框、mask 提示；跨帧传播；多对象；后续帧提示修正并重新传播 | 输出逐帧 mask，能表达大小和轮廓变化；最接近形变打码。用户画的框是提示，不是必须遵守的精确边界；身体局部是否被稳定当作独立目标须测试 |
| SAM 3 / 3.1 | SAM 3 支持文本/视觉提示的检测、分割、跟踪；3.1 增加 Object Multiplex 多对象共享计算 | 可用于后续自动检测多个同类目标，但新增能力不自动解决任意身体局部身份与边界；首版硬件和接入成本较高 |

事实来源：[CSRT 源码](https://github.com/opencv/opencv_contrib/blob/4.x/modules/tracking/src/trackerCSRT.cpp)、[KCF 源码](https://github.com/opencv/opencv_contrib/blob/4.x/modules/tracking/src/trackerKCF.cpp)、[OpenCV 光流 API](https://docs.opencv.org/4.x/dc/d6b/group__video__track.html)、[SAM 2 README](https://github.com/facebookresearch/sam2)、[SAM 2 视频示例](https://github.com/facebookresearch/sam2/blob/main/notebooks/video_predictor_example.ipynb)、[SAM 3 README](https://github.com/facebookresearch/sam3)、[SAM 3.1 发布说明](https://github.com/facebookresearch/sam3/blob/main/RELEASE_SAM3p1.md)。

### 任意身体局部不等于“一个物体”

SAM 2 官方视频示例本身展示了歧义：想选整个人时得到短裤；想选上衣时得到整个人，需要正/负点击修正。因此能接受任意位置框，并不等于能准确持续追踪任意语义部位。局部皮肤缺少纹理、部位旋转、衣物遮挡、两个相似部位交叉，都必须纳入样本。首帧分割不对时应先纠正，不能靠后续传播自动修好。[官方示例](https://github.com/facebookresearch/sam2/blob/main/notebooks/video_predictor_example.ipynb)

建议先比较两种最终遮挡表达：带安全边距的跟踪矩形、膨胀后的分割 mask。矩形可能多遮挡，但更容易人工校正；紧贴轮廓的 mask 边缘误差可能直接漏出目标。安全边距是待标定的产品参数，不是模型置信度。

### 跟丢和“置信度”

- OpenCV `Tracker.update` 文档接口为成功标志和更新框；`false` 表示该帧未定位，不代表对象一定不存在。CSRT/KCF 源码有响应阈值，但公共更新接口不返回校准概率；CSRT 的 `psr_threshold` 名称也不能直接解释为百分比可靠性。[Tracker API](https://docs.opencv.org/4.x/d0/d0a/classcv_1_1Tracker.html)、[CSRT 实现](https://github.com/opencv/opencv_contrib/blob/4.x/modules/tracking/src/trackerCSRT.cpp)
- SAM 2 的 `propagate_in_video` 产出 frame index、object ids、mask logits；内部存在 `object_score_logits`，但不能把 mask logits 或内部对象存在分数转成“打码成功率”。本次未发现可保证不漏遮的校准分数。[视频预测器源码](https://github.com/facebookresearch/sam2/blob/main/sam2/sam2_video_predictor.py)
- LK 的 `status`、`err` 是点跟踪/误差输出，不是身体部位身份置信度。[光流 API](https://docs.opencv.org/4.x/dc/d6b/group__video__track.html)

建议的质量标记应明确称为启发式检查：位置/面积突变、mask 为空、前后向不一致、点跟踪失效等。即使没有触发标记，也不能视为逐帧通过。

遮挡、快运动、运动模糊、相似对象交叉和镜头切换的恢复质量没有本项目证据。建议镜头切换断开轨迹重新初始化；完全遮挡到重现的区间待确认；修正帧重新传播所影响的区间必须失效重算。不要静默隐藏遮罩，也不要把“沿用上一帧位置”当作已遮住的证明。

## 计算环境、依赖与许可

| 路线 | 官方事实 | 本项目判断 |
|---|---|---|
| OpenCV | CSRT/KCF 位于 contrib tracking；Python wheel 有 contrib/headless 选项且共享 `cv2` 命名空间，官方要求只选一种包 | 使用常规 CPU 路径；这两个类未提供可直接选择 CUDA/MPS 的推理后端接口。不应将 OpenCV 有 CUDA 模块等同于 CSRT 已 GPU 加速。不得直接替换共享 Python 环境中的 cv2 |
| SAM 2.1 | Python≥3.10、torch≥2.5.1、torchvision≥0.20.1；可跳过 CUDA 扩展，但失去部分 mask 后处理；官方视频 notebook 有 CPU/MPS/CUDA 分支 | Mac 可以进入候选验证，但官方明确 MPS 支持初步，可能数值不同或效果退化；CPU 可选不代表性能够用。CUDA 是官方主要验证路径 |
| SAM 3/3.1 | README 要求 Python≥3.12、PyTorch≥2.7、CUDA≥12.6；权重需申请访问。SAM3 模型含检测器和跟踪器 | 本次未核实官方 CPU/MPS 支持保证。无 CUDA 机器不能以此作为首版承诺。3.1 多对象效率改进不能外推为本机速度 |

来源：[OpenCV Python 包说明](https://github.com/opencv/opencv-python)、[SAM 2 INSTALL](https://github.com/facebookresearch/sam2/blob/main/INSTALL.md)、[SAM 2 notebook](https://github.com/facebookresearch/sam2/blob/main/notebooks/video_predictor_example.ipynb)、[SAM 3 安装说明](https://github.com/facebookresearch/sam3)。

许可事实：OpenCV Python 包装代码为 MIT，OpenCV 本体按对应版本附带许可（官方包说明区分旧版本 BSD 和新版 Apache 2.0）；CSRT/KCF 不要求另下载神经模型。SAM 2 的模型权重、代码/示例采用 Apache 2.0，部分第三方组件有独立许可。SAM 3 使用自定义 SAM License，覆盖模型/权重/代码，不能继承 SAM 2 的 Apache 假设；分发时须保留该协议，并有使用限制。正式引入时按锁定版本核对全部依赖和权重许可。[OpenCV 包许可说明](https://github.com/opencv/opencv-python#licensing)、[SAM 2 许可](https://github.com/facebookresearch/sam2#license)、[SAM 3 License](https://github.com/facebookresearch/sam3/blob/main/LICENSE)

本机只读检查由主调研执行：macOS 15.8.1 arm64，指定 Python 3.12 环境 cv2=5.0.0，顶层 `TrackerCSRT_create`/`TrackerKCF_create` 不存在，MIL 存在；torch/sam2 未安装。这不等同于所有 namespace 和其他环境均不可用。本次未安装依赖、下载权重或运行跟踪模型，故不提供 FPS、内存或质量结论。服务器文档的 6 CPU/6 GiB 不是当前硬件实测，更不能据此推定有 GPU。

## 仓库接入边界与已做的小验证

已复核 `source/exactcut/cutting.py`：`normalize_mosaics` 为固定矩形，渲染按 crop/pixelize/overlay 合成后再 select 删除帧；HTTP 在 `cutting_http.py:80` 拒绝 mosaics，`cutting_replace.py:85` 拒绝带 mosaics 结果。因此已有底层固定打码不等于开放的端到端功能，也不是动态轨迹接口。

建议在源视频帧/时间坐标中存放轨迹，剪切之后另做时间映射；不要直接把剪后时间用于源视频 mask。动态矩形或任意 mask 可统一生成逐帧灰度遮罩，再将像素化视频与原视频合成，避免每帧堆一个独立固定矩形滤镜。实际合同、持久化字段尚未设计，不在这里猜测 API 字段；需遵循 schema v1 和依赖迁移/回滚约束。

主调研已进行有限 FFmpeg 冒烟验证：本机存在 maskedmerge/pixelize/alphamerge；合成 160×120、10 fps、1 秒 testsrc2，灰度 mask 矩形每帧横移 5 像素，经 pixelize 和 maskedmerge 输出 10 帧到 null，exit 0。这只证明滤镜管线可执行，未做视觉质量、真实目标跟踪或 1080p 性能验收。

## 托管大模型与 Skill 路线（按用户最新要求）

主调研已读取以下官方模型页面/skill 源码，证据与结论汇入本报告；本次未上传真实素材、未发起付费模型调用。

- **首选验证：fal SAM 3 video-rle + genmedia skill**。[模型 API](https://fal.ai/models/fal-ai/sam-3/video-rle/api)列出 video_url、prompt、带 frame_index 的 point_prompts/box_prompts、mask_url，可作为视频提示入口。但页面 Output 的 video File 与 Other types 中 SAM3RLEOutput 的 rle/metadata/scores 表述不一致；必须先用独立样本取得真实响应，确认逐帧原始 mask、帧编号、形状和可用分数含义。不能据名称虚构字段，也不能将 scores 当作不漏遮概率。
- **对照基线：Replicate 官方 Meta SAM 2 video**。[实际 schema](https://replicate.com/meta/sam-2-video/api/schema)列出 click_frames/coordinates/labels/object_ids、binary mask；可选 output_video=false、PNG、output_frame_interval=1，输出 uri 数组。先验证这些图像与源帧逐一对应、是否包含所有帧，才能驱动导出；不假定它与 fal 请求字段相同。
- **可借用 skill 已存在**：[fal-ai-community/skills](https://github.com/fal-ai-community/skills) 的 [genmedia](https://github.com/fal-ai-community/skills/blob/main/skills/genmedia/SKILL.md) 提供 schema/run/status/upload/pricing 工作流，[genmedia-workflow](https://github.com/fal-ai-community/skills/blob/main/skills/genmedia-workflow/SKILL.md) 提供多步编排/QA。它们适合接模型、任务轮询和产物串联，不是已经适配本仓库的打码算法。应采用当前这些 skill，不用旧名称 fal-generate 推断现有能力。本次未安装。
- **语言理解探索备选**：[ByteDance Sa2VA 视频模型](https://replicate.com/bytedance/sa2va-26b-video) 可列入下一轮；本轮未取得有效 schema，不据此承诺逐帧 mask 或任意身体局部准确输出。

托管模式免去本机 CUDA 前提，但费用、输入大小/时长限制、保留策略、纠偏是否需要全段重算均应在实际调用前按目标服务确认。当前没有实测成本或延迟数字。模型负责生成候选 mask；skill 负责调用和编排；本项目仍需负责源帧映射、人工纠偏、结果缓存及最终合成。

## 本地 Skill 与可借鉴代码

检查当前会话 skills 目录与本地 SKILL.md（检索 CSRT/KCF/SAM2/object tracking/动态打码/跟踪打码），未找到专门提供成熟跟踪打码实现的 skill；此结论仅限本地目录。网上已找到上节 fal 模型编排 skills，不等于专用打码实现。HyperFrames 类 skill 处理视频编排，不能代替视觉跟踪模型。research 适合证据整理，product-design 适合技术验证后的交互阶段。

真正可借鉴的是 SAM 2 官方视频 notebook 的框/点提示、后续帧修正、重新传播流程及官方 demo 的前后端代码；这些仍需适配本项目源帧时间、编辑撤销、任务状态与导出，不是直接可上线的打码组件。[SAM 2 demo](https://github.com/facebookresearch/sam2/tree/main/demo)

## 分阶段验证与进入 UI 设计的门槛

1. **先定验证素材和标注**：使用独立、授权的短样本，覆盖固定目标、平移、远近缩放、转身/弯曲、局部遮挡/完全遮挡再现、快运动/模糊、相似目标交叉、出画、切镜头、多目标以及低纹理局部。人工标注需遮挡的区域和关键帧；区分肉眼检查与逐帧标注。
2. **托管模型首轮**：先以 genmedia 获取 fal SAM3 实际 schema，再用独立短样本验证响应；契约不清时先用 Replicate SAM2 的二值 PNG 序列作对照。检查帧映射、提示约束、遮挡/形变效果、人工纠偏后重算范围、请求费用与端到端等待时间；保留原始响应作为后续模型字段依据。
3. **CPU 对照基线（可选）**：固定框、人工关键帧插值、CSRT 使用同素材；KCF 可作位置跟踪对照；记录每段漏遮帧、最长连续漏遮、过度遮挡、修正次数/耗时、处理耗时、峰值内存。先核验选定 OpenCV 包确实有目标 API，再在隔离依赖路径验证；不升级共享环境。
4. **自托管 SAM 2.1 独立验证（可选）**：优先 tiny/small 验证局部提示是否可控、形变跟踪是否减少纠偏；记录设备和 dtype；Mac MPS/CPU 与可用 CUDA 分开报告。记录模型初始化、传播、修正重算、导出耗时，不能仅记录单帧模型速度。
5. **合成导出验证**：真实输出逐帧检查首尾、剪切边界、变帧率时间映射、旋转/缩放、遮罩边距、多目标重叠、取消和失败重试；证明预览与最终导出使用一致轨迹。合成10帧冒烟不能替代此步骤。
6. **再决定产品范围**：根据托管模型在真实目标素材上的结果，确定首版是分割 mask 传播还是保守矩形遮挡；人工修正始终保留。没有逐帧漏遮证据，不承诺“全自动、无需检查”。将实测支持模式、等待时间分布、纠偏方式和失败状态交给 agy 设计。

推荐下一项具体工作是独立小原型/离线评测，而非立即设计依赖未知能力的 UI。数值验收阈值需按用户实际打码目标制定；本报告不虚构通过率或速度承诺。
