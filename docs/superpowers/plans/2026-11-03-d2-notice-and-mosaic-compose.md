# D2：本地提示撤回与最终打码有效帧优化执行方案

**Goal:** 撤回未经要求新增的本地原片notice；使原失败A在不改变画面、删帧、音频或超时的条件下完成最终合成。
**Architecture:** 沿现有originals本地读取分支撤回notice；在mosaic_render.compose按效果分组计算源帧有效区间，传给mosaic_effects.layered_graph，仅对blur/soft_blur的gblur加timeline enable。其他路径默认保持旧行为。
**Tech Stack:** Python3.12、FFmpeg、现有pytest/浏览器验收。

## 分工与硬边界

用户指定D2实施、父会话写方案并review。D1已取消，不恢复。D2不再委派。父review通过前不更新18883；不改运行树，不重启、不取消用户任务。两项独立提交，无顺手文案、重构、调度/配置改造。真实FBBM上传、发布、远端写入绝对禁止。

基线：/Users/matao/.paseo/worktrees/1kps4qs6/cut-mosaic-18883-verification，固定6ab02457ab67a1b9820bde30a046ef17f01698f5；应用源码05b2faf，测试夹具修正bbfa4f7。新D2工作树从6ab0245分出。先只读检查D1取消时是否留下修改，不覆盖、删除或盲目继承。

Python固定 /Users/matao/miniconda3/envs/py3.12/bin/python3.12。先读source/AGENTS.md、docs/local-dev-services.md和 /Users/matao/code/troubleshooting-runbooks/operations/exactcut-long-render-performance.md。

## 事实及证据入口

- 完整既有验收：基线docs/verification/18883-full-dev-acceptance-2026-11-03.md/.json。2073通过1跳过，原失败测试及test-only修正已记录，勿重写历史。
- 18883当前加载cut-mosaic-18883-source-delivery/source，最后源码05b2faf；PID须实际查询。
- trial根：/Users/matao/.paseo/worktrees/1kps4qs6/cut-mosaic-background-queue/source/.dev-state/cut-mosaic-user-trial-20261031。
- 私有证据：trial/actual-e2e-20261103-123501/compose-command-diagnosis/evidence.json、audio-and-boundaries.json、finals-verified.json；严禁把凭据/视频入Git。
- A报告2a567ce0a14642728cbc4458640b256c：6187输入帧1080x1920/30fps，mask源帧[60,90)，删除[120,150)，预期6157输出帧。实际cut-path默认600秒超时。原报告保持failed，不改写成成功。
- 同源20秒诊断：旧全段gblur67.4327秒、有效区间命令18.4084秒，570解码帧MD5和19秒音轨MD5一致。仅短诊断，不证明全部边界或完整A。
- B无删帧最终曾completed，6399帧；现有cut medium/no-cut fast、CRF18、600/900秒及资源线程均保持。

## Task 1：撤回notice（独立提交）

文件：source/exactcut/originals.py；现有tests/test_originals_local.py、test_originals_lookup_ui.py必要断言。

- [ ] 检查本地load_originals分支及前端notice消费。只删除本地有片时设置“仅显示本地正片，上架状态未查询”的赋值。
- [ ] 保留episodes、bytes、listed:null、本地安全读取/ACL/Range，保留UI null不误标未上架；远端notice/production/fetch1不变，不加替代提示。
- [ ] 验证本地列表有片/空列表、三态及原远端兼容；正常提示仍“有7集原片（EP1–EP7）”。去掉notice后missingDraftEpisodes按真实本地列表检查，不能隐藏真实缺集。
- [ ] 运行：`python -m pytest -o addopts='' -q tests/test_originals_local.py tests/test_originals_lookup_ui.py`（python使用上述绝对路径）。提交仅本项必要代码与测试。

## Task 2：有效源帧模糊（独立提交）

文件：source/exactcut/mosaic_render.py compose；source/exactcut/mosaic_effects.py layered_graph；相应既有effects/render tests（先rg定位，按本地风格扩展，不新建框架）。

实际代码关系：compose把tracks按validated appearance分组并为每组encode_mask；layered_graph从原视频split各组并transform，然后maskedmerge；删除在cutting.render_video后续处理。union_mask仅target.start<=frame<target.end有效，failed track不贡献。

- [ ] 先检查各调用点和cutting.render_video滤镜顺序，确认gblur看到的是原视频源帧n。不得按删后帧号或秒近似计算。
- [ ] 从每组有效tracks提取[start,end)并合并重叠/相邻区间；任何过滤只能排除可证明无mask的track。帧内实际空mask可保守包含，不扫描视频增加耗时。
- [ ] layered_graph新增可选逐组区间参数；缺省保持旧行为，空区间明确禁用gblur，非空生成数值表达式。单区间命令形式：`gblur=sigma=48:steps=2:enable='gte(n,60)*lt(n,90)'`；多区间取并集，FFmpeg转义遵循现有参数构造。不要往appearance校验对象塞未定义字段。
- [ ] 仅blur/soft_blur应用enable；pixel/solid、mask生成/羽化/叠层顺序、graph预览路径、强度和编码参数不变。每层仍采样原图，不能改成在上一层结果上模糊。
- [ ] 先添加失败回归证明新实现确实跳过无关帧；不能仅以字符串断言代替实际媒体验证。真实FFmpeg对照原graph与新graph逐帧解码相同，必要性能证据用同源代表片段独立记录，避免机器负载造成单测脆弱时间阈值。
- [ ] 必测：起点/终点exclusive、0帧起/末帧、多区域同组有间隙与重叠、不同组重叠及不同效果、failed/无有效tracks、删帧前后源坐标、30000/1001帧率、soft_blur羽化边缘。实际解码视频相同；删除路径音频解码/时长相同。
- [ ] 核实当前FFmpeg gblur timeline支持。发现语义不一致先报告，不改画质或强度让测试通过。
- [ ] 跑相关测试并提交。源码范围超上述两文件须先向父说明具体必要性。

## Task 3：固定候选验证并交父review

- [ ] 给父两个commit、最小diff、红绿证据、短同源帧/音轨对照与耗时、配置无差异证明。
- [ ] 固定候选完整回归：`python -m pytest -o addopts='' -q`，保存完整日志及退出码；仅在自有隔离数据运行。失败逐项解释，不删断言/靠超时放宽。
- [ ] 父独立审查帧坐标、区间并集、效果/超时不变、测试覆盖；父复跑关键用例。D2等待父明确review结论再切换dev，不重复问用户。

## Task 4：父review后实际18883重验

- [ ] 重新确认进程、实际代码、用户任务及所有queue/children；有活动任务等待自然完成，不取消。备份start/runtime和原用户数据哈希，空闲维护屏障后正常停旧服务切已提交源码；不能修改正在加载的树。
- [ ] 保持worker1/AI1/fetch0/external0/publish0/OSSfalse与全部并发参数，私有凭据路径不变。失败正常回退代码/config，不恢复旧DB。
- [ ] 原列表实际浏览器确认新增notice消失、七集仍可读，未知上架不误标；用户原草稿不变。
- [ ] 用原失败A完整source、原框/遮罩/效果/删除区间经正常任务入口重新提交（新job保留旧失败）；检查业务completed而非queue.done，6157帧、1080x1920、有音频、时长正确、SHA和报告一致、Range/实际播放、无原片替换。记录真实合成时间及原600秒限制未变。
- [ ] 无删路径和其他效果已由媒体回归覆盖，按review需要做最小真实B验证，避免重跑全部20项目/模型。
- [ ] 原用户项目草稿/源片/已有结果SHA一致；真实upload/poll/publication为0；收尾自有任务/进程，无泄漏。
- [ ] 最终报告明确两项结果，完整A失败就如实失败，不用短片速度代替成功。无需生产发布或merge main。
