# main 合并前检查与差异审查（2026-11-04）

> 历史归档（2026-11-04）。正文保留当时的方案或执行状态，不代表当前版本；日常操作见[文档入口](../../README.md)。

## 结论

**建议合入：APPROVE。** 用户明确接受非严重测试失败，不再为本轮测试夹具/时序问题重复整轮全量。尚未实际修改或推送 main，也没有生产发布。

基线为远端 main `ddf21bb`；核验时它仍为候选祖先，没有新增远端 main 提交。合并候选为 `fix/concurrency-merge-tests` 的 `63c6d04`，包含原并发集成、三项核心修复与测试修正。产品源码与已实测应用代码 `098ffad`、全量候选 `df39e8e` 一致；63c6d04 只修改三个测试文件。

| 项目 | 结论 |
| --- | --- |
| 严重产品回归/安全阻塞项 | 本次审查未发现 |
| 新发现 CRITICAL/HIGH/MEDIUM/LOW 安全问题 | 0 / 0 / 0 / 0；不是对全部系统的安全认证 |
| 原八项并发改动与三项核心修复 | 纳入本次候选 |
| main 合并 | 具备合入条件；未执行 |
| 生产部署 | 不在本次操作范围，仍按独立发布流程 |

## 全量测试：保留原始结果

两机均在独立、固定 `df39e8e` 工作树中执行完整三层测试，显式覆盖 unit、integration、ui，未使用默认的层过滤：

```sh
<python> -m pytest -m '' -p no:cacheprovider --tb=short --junitxml=<private-output>
```

| 环境 | 通过 | 失败 | 跳过 | 耗时 |
| --- | ---: | ---: | ---: | ---: |
| M2 | 2204 | 1 | 4 | 658.75 秒 |
| Mac mini | 2202 | 3 | 4 | 531.28 秒 |

没有将这轮原始结果改写为全绿。此前两机对应用代码 `098ffad` 的 304 项相关回归仍各自通过；真实业务和受控故障证据见[最终验收记录](2026-11-04-concurrency-8-final-acceptance.md)。

### 三条失败的定性与修正

1. `test_distinct_hook_jobs_use_two_workers_and_leave_third_persistently_queued`：两机均失败。用例隐式依赖旧 preview 默认容量 2，Hook 独立角色默认容量按本轮要求为 3，实际 active=3。修正只在该用例明确配置 `editor_resources.hook_tasks=2`，保留原“两条运行、一条排队、峰值为 2、最终全部完成”的业务断言。
2. `test_old_stop_failure_cannot_overwrite_next_editor_message`：mini 失败。原用例在新编辑器首帧/自动识别仍加载时截取提示，随后正常初始化更新提示也会使断言失败。修正等待新编辑器的自动识别初始化完成，再注入旧 stop 请求失败，原“不得覆盖新提示、编辑保持可用”的断言不变。
3. `test_reopen_while_source_preparing_shares_request`：mini 失败。workspace visible 早于首帧加载完成，原用例在自动 scan 发生前立即断言次数。修正等待 scan 发生后，继续验证 open 请求复用以及恰好一次 scan。

两条 UI 用例及相关产品前端源码在 `ddf21bb` 与候选之间相同。在 mini 的原 main 独立工作树中，第一次定向复核同时复现了上述两条 UI 失败，确认为已有时序问题。修正提交 `63c6d04` 无产品改动、无新增 sleep、无放宽超时、无删除或跳过用例。

修正后，两机分别运行 Hook 容量文件、预览生命周期 UI 文件和 reopen 单项，**各 13 项通过**。根据用户对轻微失败的明确豁免，没有再对修正后的测试提交补跑完整 2209 项。

## 差异范围与策略

采用 differential-review 的差异审查流程，以整个仓库规模选择针对关键路径的审查：读取了全部 22 个产品文件的差异，对外部写入、资源限制移除、信号身份及恢复状态机补充一层调用方检查。

审查范围 `ddf21bb..63c6d04`：57 个文件，新增 1341 行、删除 182 行；其中产品文件 22 个，其余为测试和文档。本文之后的记录提交只增加文档，不改变该产品范围。

| 文件/分组 | 风险重点 | 审查结果 |
| --- | --- | --- |
| `preview_oss.py` | 移除 transfer 车道后的对象并发写入 | 对象锁、不可覆盖写、源身份/哈希、HEAD/大小/private ACL 核验均保留 |
| `cutting.py`、`mosaic_render.py` | 移除 compute 锁后的最终任务互斥、取消与资源归还 | final_media 车道、worker/batch lease、取消检查与 finally 清理保留；旧剪切仍在 final_group 内执行 |
| `mosaic_session.py` | 复制与帧准备不再占 transfer 车道 | 逐键准备锁、复制 SHA、源版本和编辑准入核验保留 |
| `interactive_queue.py`、`editor_resources.py` | 独立 hook/locate、取消预占、无固定等待超时 | 角色槽位、permit、路径限制、worker/batch lease、维护检查及取消作用域保留 |
| `batch_control.py`、`processing_cancel.py`、`workflow.py` | 暂停计时、执行根信号及进程清理 | instance/PID/PGID/process_token 校验仍在；真实超时和取消清理有真实进程回归 |
| `material_workers.py` | 读一次 jobs 的快照与 fresh child 重试 | 父状态锁、代际/child/attempt 准入保护保留，not_before 到期约束已补回归及实测 |
| `media.py`、`cutting_scan.py`、`mosaic_scan.py` | 短操作分类及扫描阶段资源 | 只改变资源使用路径；原媒体错误、扫描配置/凭据和源校验保留 |
| `mosaic_tracking.py`、`mosaic_inference.py`、`mosaic_sam2.py` | SAM 按帧超时与短 mux | 推理仍在受限媒体阶段，等待检查取消，有限总时限按帧计算 |
| `ending.py`、`hook_presentation.py`、`hook_splice.py`、`material_download.py`、`qc.py`、`renderer.py` | 单帧/抽音/mux 等 light 标记 | 对输出与校验的命令及断言未放宽；改变的是共享槽位归属 |

## 保护边界与具体场景

- **普通账号尝试跨项目处理或角色切换绕过权限**：交互 descriptor 的相对路径限制和 permit 校验仍在 `interactive_queue.py:46`、`:118`；最终处理在 `mosaic_render.py:244` 复查启用账号、项目权限，再取得素材 gate。新增角色分配没有替代这些校验。HTTP 鉴权、accounts、cutting_access、正式 publish 模块本轮没有差异。
- **同一 OSS key 的两个并发上传，或上传期间替换本地源文件**：`preview_oss.py:214` 的快照和身份检查、`:228` 的对象锁、不可覆盖请求及上传后的身份/内容/远端 ACL 核验均保留。检查了替换成片及历史补传调用方；它们仍在提交当前指针前核验对应媒体身份。
- **PID 重用/另一个实例被误发信号**：`batch_control.py:148` 仍通过 child_state 验证实例、PGID、token 后才 killpg。新计时函数不能独立绕过身份校验。
- **取消、失效或旧代际任务借等待/恢复继续提交**：交互取消作用域、最终任务取消检查、candidate stage_enter/commit 的 _eligible 保护仍在；failed 能退出并不使其重新获得准入资格。

这些是对具体变更路径的检查，不是对所有输入、外部系统、磁盘故障或所有时序排列的穷尽证明。

## 调用影响范围

对生产 Python AST 的同名调用节点静态计数，不含测试；不声称覆盖动态调用或完整传递调用图：

| 函数 | 生产调用位置 |
| --- | ---: |
| `media.run_checked` | 23 |
| `preview_oss._ensure_uploaded` | 7 |
| `batch_control.signal_children` | 3 |
| `editor_resources.media_stage` | 3 |
| `editor_resources.final_group` | 2 |
| `material_workers.reconcile_candidates` | 1 |
| `interactive_queue.initial_resource` | 1 |

共享媒体函数影响较广，因此既核对其调用点的 light 使用，也结合完整 suite 和此前的真实媒体检查。没有生成或声称精确的行覆盖率数据。已修改的关键资源边界有对应回归；真实 OSS 仅验证 test 命名空间，未进行真实 FBBM 提交。

## 历史依据

- 被移除的 legacy compute 锁来自 `b400bfc`（资源冲突有界重试修复）；核对后确认它承担共享资源互斥，不是权限保护。最终 final_group/final_media 和 batch/worker 租约仍是当前实现的执行约束。
- OSS transfer 层来自 `f34aabd`；对象锁与快照/内容/ACL 校验来自 `fa42015` 等历史提交。本轮删除前者，保留后者，不以移除限流为由移除内容与覆盖保护。
- 交互恢复中“未确认云端请求不盲目重放”的历史处理（`abd089f`、`5a64c35`）未在本轮移除。

## 剩余限制与建议

没有发现本轮需要阻止合并的严重产品问题。接受本次合并不代表生产发布已获授权，也不代表完成 Linux 峰值容量、真实提供方额度或物理满盘认证。

IO 并发因移除全局 transfer 限制而增加，后续部署继续使用既有容量评估与发布流程。交接中的配置残留、提示语、性能专项和 Gemini 扫描预算等仍为单独事项。外部 OSS RequestError 的首次失败及补传历史继续保留，未宣称网络故障不会再发生。

原始两机全量、main 基线复现及定向结果保存在私有测试目录，公开记录不包含绝对路径、IP、口令、密钥或签名 URL。
