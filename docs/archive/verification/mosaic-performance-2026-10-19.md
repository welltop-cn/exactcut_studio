# 同片 CPU 打码性能对照

> 历史归档（2026-11-04）。正文保留当时的方案或执行状态，不代表当前版本；日常操作见[文档入口](../../README.md)。

用户授权在独立线上测试实例优化并使用同一素材对照。项目4142628fb23746149326953e8b8dd49c，原片SHA256 875d6094f766b22b72cf47ebbd372a4449464ebb3424d09fa3bf335a80caa7cc，4500帧/30fps、1080×1920。
冻结任务f7c75921d8ab74c72a6b9511c8d1a08e：删除[135,510)，跟踪[2055,2325)与[2550,2610)，保持框、参考帧和效果不变。

候选d472a16：复用cutting.render_video的精确PCM裁音及校验，通过内部video_graph/video_inputs将动态遮罩合成与删除合并为一次视频编码。音轨仍需重新AAC编码（删除场景），无删除时原音轨复制不变。render engine 5→6隔离旧成片缓存。

SAM官方固定源码的build_sam2_video_predictor支持hydra_overrides_extra；其sam2_base以image_size构建prompt编码器，misc加载器将输入统一缩放到该尺寸。新增集中tracking.inference_size可选512/768/1024，缺省1024；配置进入已有跟踪identity。不是仅缩小JPEG来宣称推理加速。未更换权重或采样帧数。

对照环境：same exactcut-test:mosaic-75efc57镜像，独立禁网容器6CPU/6GiB/256PID；副本source与原始遮罩只读，输出独立目录，串行对照并持有测试compute.lock。不挂载正式data、不改用户草稿/生成记录。

本地完整回归1553 passed（337.62秒），额外真实起止帧/音频场景3项通过。完整回归启动后增加的两项边界测试另行覆盖。

已测：原1024跟踪两区517.53+124.89秒；512冷推理（含worker启动）118.26秒，帧仍为270+60；第一目标空mask127→57，第二0→0。空mask不是实际漏遮率，必须视觉检查。

同原始遮罩编码旧364.49秒，新243.45秒（-33.2%），同4125帧/30fps/1080×1920/音频。旧版重跑输出SHA256 6f051b1ca8fe4067f1ddd22fb5b32737d7d8c7daed07c75d5407658fad9800b1，与原线上结果完全一致。

服务器证据：/srv/exactcut-test/acceptance/perf-d472a16；最终结论见下。

## 最终验收

代码 ca2a1e7（含 d472a16 与 ccd873c）。完整本地套件1555 passed（339.83秒，10个既有fork警告）；同镜像Linux定向52 passed（26.69秒，1个只读pytest缓存警告）。编码前后解码音频MD5均c37d3f48419d0459d768776519c3a723；输出4125帧、1080×1920、30fps一致。副本源SHA一致，用户处理记录与原片未改。

- 512：跟踪118.26秒，第二目标末尾视觉范围扩大到脸部，拒绝默认启用。
- 768：跟踪314.69秒（两区域247.39/64.58秒），空mask与1024相同127/0，逐帧mask平均IoU为0.9598/0.8311；代表帧检查比512接近原版，但并不证明其他素材无退化，仍留作实验档。
- 默认仍1024，不改runtime跟踪字段，避免旧缓存失效。只启用合并编码，合成364.49→243.45秒（减少33.2%）；这是合成阶段，不是整条首次处理耗时。

旧重跑输出SHA为6f051b1ca8fe4067f1ddd22fb5b32737d7d8c7daed07c75d5407658fad9800b1；新输出b0b99a08f5e6985f69cffa339cd595b79f979fe411b65c28c897bc7aff6055a2。单次编码减少重复有损压缩，因此不要求视频字节相同。

独立可播放对照位于本机source/.dev-state/mosaic-perf-comparison/index.html，不在Git中。服务器证据保留在acceptance/perf-d472a16。测试部署记录与回滚代码目录为deployments/perf-ca2a1e7及code-perf-ca2a1e7.previous；回滚仅代码/配置，不还原用户数据。

发布完成：线上测试健康返回test-mosaic-perf-ca2a1e7，worker_alive=true；保留原镜像和依赖。生产容器启动时间未变。已部署三份后端文件与候选逐字节一致。
