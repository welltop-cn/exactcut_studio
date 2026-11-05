# 素材版本管理 Grok 设计审查

> 历史归档（2026-11-04）。正文保留当时的方案或执行状态，不代表当前版本；日常操作见[文档入口](../../README.md)。

请求模型 `grok-4.7`，实际返回 `grok-4.7-build`；CLI 会话 `01a126a0-2f23-7773-930c-ef97292e06ec`。仅允许读取不含凭据/业务数据的源码与设计副本，禁止 shell、写入、网络、MCP、子代理；未让 Grok 操作生产或 OSS。原始 JSON 保存在私有 artifacts，不复制内部推理。

## R1

2026-10-11，high，exit 0，stopReason=end_turn，报告完整。读取设计全文、workflow 前 1000 行、result_store、batch_control、util；没有读到完整执行器和发布器，结论需要本地核对。

提出 7 项阻断：草稿与代次提交范围不明确；旧分片/心跳可能绕过 current fencing；调度领取/执行权失效协议不完整；旧锁与新锁交叉；交付单槽覆盖历史；旧二进制与开关回退；预览/master 哈希来源关系表述有歧义。

本地核实及修订：

- 工作副本、代次、幂等和 outbox 纳入同一权威提交。当前成片只读封存版本，旧分片不再决定父项目 current。
- `execution()` 实际是心跳上下文，不是执行互斥租约；新协议必须绑定真实 staged job attempt，不能依赖 executor_running。
- 复用现有 scheduler 的内部候选/交付批次，隔离写入和历史；成功后复制并核验不可变成片再提交，减少另写调度器的风险。
- 新项目锁不嵌套旧 runtime/publications/results/线程锁；不直接采用审查建议的全局锁全序，须在实现审查验证调用图。
- 交付按独立 delivery ID 保留 intent/unknown，旧版本未决时阻止新交付；相同正式目标名不同字节继续拒绝，禁止覆盖。
- 新程序全面替换并排空旧 worker 才纳管；关闭 feature gate 不得降级旧写路径；逻辑删除不调用任何旧物理删除代码。
- 分别验证 master 与 preview 的自身哈希，以及 preview.source_master_sha256 与 master.sha256 的来源关系。
- “所有 ID 改为 32hex”建议不适用已有 Rxxxx 素材 ID；保留历史身份，新版本/执行 ID 才使用 UUID。
- 追加不可变输入、异常迁移、签名授权、真实服务器故障测试和 OSS 既有生命周期只读检查。

修订在 `docs/archive/material-media-lifecycle-design.md` 的 R2 节，已提交同会话复审；未取得修订稿通过前不得称为已审查通过。

## 与此前 OSS 实现审查区分

此前 OSS 审查会话 `01a12682-9b83-7841-a00d-388d1d6453da` 报告 webserver 的 path 未定义；本地读取完整上下文证实在进入 preview-url/download 分支前已经 `path = workflow.folder(batch_id)`。该意见源于仅阅读 diff 缺少上下文，不是已确认缺陷，不能据此盲目修改。其他建议仍需针对实现验证。

真实 Linux 测试另发现同长度快速改写可能不改变 stat 身份：已增加冷上传后的源字节哈希核验，并以固定 stat 身份夹具覆盖；本地和真实 Linux 容器 focused tests 均 12 passed，修复提交 `4ac68a6`。该证据不等于整个新版本已通过或已部署。

## R2 复审

同会话，medium，exit 0，stopReason=end_turn，报告完整。Grok 确认原 7 项阻断在修订设计中闭环，接受保留历史 request_id 和旧锁/新锁不嵌套的方案；允许开始核心实现。接入调度/命名前仍要求先以失败测试固定两项契约：

- B1：父 outbox 与内部 publications 双恢复不能重放 unknown 外部交付；父未知状态不能被内部旧槽恢复抹掉。
- B2：内部 confirmed 只能是父预留名的别名，不能导致父素材改号，也不能让第三个项目抢占同名。

已补充设计最后一节及验收计划。该结论是方案审查，不等于实现通过；最终代码仍需 Grok 审查和真实服务器验证。

## 实现复审闭环

Grok `grok-4.7`（同一审查 session `01a126a0-2f23-7773-930c-ef97292e06ec`）完成代码复审及修订复审；最后一轮 `stopReason=end_turn`、16 轮，检查了与 `ee8267f` 一致的源代码快照。没有交付生产凭据或数据给审查工具。

- 暂停/归档完成窗口和补传中断：转为安全失败并保留旧 current，可显式只补传重试，已关闭。
- 纳管冷哈希占用全局名称锁：移出名称锁，提交时重新检查草稿、相关文件身份和静止条件，已关闭。HTTP 最外层变更屏障仍覆盖迁移，其他写请求可能等待。
- 复制纳管项目丢失结尾图片：按引用复制独立文件并核验内容，已关闭；进度展示候选版本，预览保留旧 current。
- 损坏纳管状态采用 fail-closed，禁止跳过未知名称预留。Grok 接受这一安全选择，但指出影响范围也包括共享调度 tick：损坏的灰度项目可能阻止其他项目调度，直到修复。pending 登记通过显式再次调用 `enroll()` 修复；不能依赖已登记项目的 HTTP enroll 入口自动恢复。

最终结论为**有条件通过**：OSS 开启，`material_lifecycle.enabled=false` 与 `delivery_enabled=false`，只显式纳管一个静止的独立合成生产项目，不为纳管而打开全局开关。真实 FBBM 目标未确认时保持新版交付关闭。该结论不是全量启用新版素材管理的批准，也不代替运行时验收。

Grok 没有执行测试。其返回时还引用了“增量全量测试待完成”的输入状态；实际后续证据已为本机 934 passed、Linux 932 passed/2 skipped、真实 Linux 进程故障五项及隔离 Docker 回退/恢复三项通过，详见发布验收记录。后续浏览器、真实 OSS、生产灰度结果已分别记录在发布验收记录中；真实 FBBM 未执行。
