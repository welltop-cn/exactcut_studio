# 剪切打码正式发布 · 2026-10-19

> 历史归档（2026-11-04）。正文保留当时的方案或执行状态，不代表当前版本；日常操作见[文档入口](../../README.md)。

用户明确授权将当前测试实例代码合入 main 并发布生产。main 快进合入 feat/mosaic-gemini（ce1ba90，应用代码655e0b4），核对测试实例98个应用/模板文件SHA256与main完全一致。另提交35fe006，为正式发布镜像默认包含固定版本SAM2 CPU依赖，并在生产Compose增加独立models目录只读挂载。

- 生产入口：https://autocut.creativefitting.ai/home/
- 版本：us-east-20261019-mosaic-35fe006
- 提交：35fe00665acd0d18562cc2198e46b56044e8130a
- 固定镜像：sha256:a0d79d11a92a64079def417ed793b278916b3402aa7b34294cd38340c54e45e7
- 前版：us-east-20261015-observer-f147248
- 发布管理器完成时间：2026-10-19T12:13:59 UTC，status=ok。

构建源为服务器独立干净仓库 /srv/exactcut/preparation/release-mosaic-35fe006，通过linux-ops build/publish封存和切换。schema保持1。切换前allowed=true，active_tasks与phase_blockers均为空。

## 验证

- 本地完整回归1574 passed，10 warnings，352.15秒；Linux完整回归1436 passed、138 skipped、10 warnings，329.82秒。Linux跳过项不计入通过数，本地完整执行无跳过。
- 同正式镜像、禁网隔离CPU验收：8帧合成目标均有非空遮罩，跟踪12.08秒，含合成13.58秒；源文件SHA不变，输出帧数/分辨率/帧率一致。该短样本不代表长片速度或真实目标遮挡质量。
- 本机公网healthz及服务器公网鉴权HTTP均通过：production、目标版本、worker_alive=true、maintenance=false。登录、页面、app.js、mosaic.js、api/me和项目列表返回200；mosaic.js哈希与提交源码一致，cutting开关为true。
- 配置/密码/项目草稿快照共352份，除预期runtime.json外其余351份SHA256不变。runtime仅增加cutting.tracking；保留生产/home入口、生产OSS命名空间、交付设置和既有识别配置。
- 模型从已验收权重复制至独立 /srv/exactcut/models，SHA256为7402e0d864fa82708a20fbd15bc84245c2f26dff0eb43a4b5b93452deb34be69。模型只读挂载，device=cpu；不复制测试业务数据。
- 测试容器启动时间前后一致：2026-10-19T11:52:37.653405706Z。没有修改测试实例或Nginx，没有发起真实素材交付。

## 证据与回滚

服务器证据：/srv/exactcut/preparation/mosaic-35fe006/（配置备份、切换检查、publish.log、production-acceptance.json、acceptance/）。Linux测试日志与固定元数据：/srv/exactcut/releases/us-east-20261019-mosaic-35fe006/；tests.log SHA256为84bb05820d0ed5a7d10c7c285d673f090396343cf2ba5d92a740b726bc51bbdf。

回滚通过linux-ops rollback执行，保留当前业务数据；runtime原配置位于上述runtime.previous.json。旧版没有动态跟踪能力，回滚时需相应恢复配置，不能回滚数据库或用户草稿。本地既有未提交运维笔记和研究文档保留，未纳入发布；运维笔记冲突合并保留双方内容，原修改另保留在pre-production-merge stash中。
