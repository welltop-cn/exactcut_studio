# 同片 CPU 打码性能对照

用户授权在独立线上测试实例优化并使用同一素材对照。项目4142628fb23746149326953e8b8dd49c，原片SHA256 875d6094f766b22b72cf47ebbd372a4449464ebb3424d09fa3bf335a80caa7cc，4500帧/30fps、1080×1920。
冻结任务f7c75921d8ab74c72a6b9511c8d1a08e：删除[135,510)，跟踪[2055,2325)与[2550,2610)，保持框、参考帧和效果不变。

候选d472a16：复用cutting.render_video的精确PCM裁音及校验，通过内部video_graph/video_inputs将动态遮罩合成与删除合并为一次视频编码。音轨仍需重新AAC编码（删除场景），无删除时原音轨复制不变。render engine 5→6隔离旧成片缓存。

SAM官方固定源码的build_sam2_video_predictor支持hydra_overrides_extra；其sam2_base以image_size构建prompt编码器，misc加载器将输入统一缩放到该尺寸。新增集中tracking.inference_size可选512/1024，缺省1024；配置进入已有跟踪identity。不是仅缩小JPEG来宣称推理加速。未更换权重或采样帧数。

对照环境：same exactcut-test:mosaic-75efc57镜像，独立禁网容器6CPU/6GiB/256PID；副本source与原始遮罩只读，输出独立目录，串行对照并持有测试compute.lock。不挂载正式data、不改用户草稿/生成记录。

本地完整回归1553 passed（337.62秒），额外真实起止帧/音频场景3项通过。完整回归启动后增加的两项边界测试另行覆盖。

已测：原1024跟踪两区517.53+124.89秒；512冷推理（含worker启动）118.26秒，帧仍为270+60；第一目标空mask127→57，第二0→0。空mask不是实际漏遮率，必须视觉检查。

同原始遮罩编码旧364.49秒，新243.45秒（-33.2%），同4125帧/30fps/1080×1920/音频。旧版重跑输出SHA256 6f051b1ca8fe4067f1ddd22fb5b32737d7d8c7daed07c75d5407658fad9800b1，与原线上结果完全一致。

服务器证据：/srv/exactcut-test/acceptance/perf-d472a16；最终512效果结论与发布待补充。
