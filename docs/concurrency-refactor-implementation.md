# 并发改造实施记录

实施规范：`docs/concurrency-refactor-plan.md`。数据 schema 保持 1；新增任务字段可选。

## 工作边界与取舍

- 仓库开发规则实际位于 `source/AGENTS.md`，根目录没有该文件。
- 运行配置在 `source/config/default.json`、`source/config/production.json`，沿用 `util.load_config` 的合并机制。
- `config/deployment.json` 是服务器部署根内的机器配置，不在本仓库。此次不访问服务器、不修改该文件；生产 Compose 的默认额度需与部署记录中的 14 CPU / 24 GiB 一致。
- 保留任务对外的 `start` / `publish` 动作，内部以可选 `phase` / `lane` 分阶段。旧任务兼容；独立 CLI 的真实 `execute --publish` 在第 1 轮 C 组拒绝，改用受保护阶段上传。
- 默认各 lane 容量为 1，总执行器上限也为 1；在提高总量上限后，不同 lane 可重叠，这符合“等待不占执行位”。完全回退到新任务的旧入口使用兼容开关。仅将各 lane 设为 1、同时保留较大总量上限，不能描述成全局只有一个进程。
- 共享工作注册表、协作式暂停、媒体探测缓存等按计划后移。此次复用现有正片锁、基底锁和进程身份核验机制。

## 隔离验证方法

在 `source/` 下使用 `/Users/matao/miniconda3/envs/py3.12/bin/python3.12` 运行完整 pytest。启动 pytest 前清除外部凭据及继承的 `EXACTCUT_*` 环境，使用独立临时 `EXACTCUT_DATA_ROOT`，环境为 development。

不要给整个测试进程设置固定 `EXACTCUT_SOURCE_ROOT`：已有 fixture 会切换各自的数据根，固定源根会让正片缓存跨用例污染。没有独立数据根时，production 模拟可能碰到工作区 development 标记；不要删除本地标记绕过隔离。

基线完整测试在上述隔离方式下退出码 0。新增行为用 fixtures、临时数据根和模拟外部客户端验证，不调用真实 OSS、飞书或 FBBM。

可复现的 macOS 完整测试命令（在仓库 `source/` 下执行）：

```sh
/Users/matao/miniconda3/envs/py3.12/bin/python3.12 - <<'PY'
import os, subprocess, tempfile
with tempfile.TemporaryDirectory(prefix='exactcut-concurrency-tests-') as root:
    prefixes = ('EXACTCUT_', 'OPENAI_', 'ALIYUN_', 'FEISHU_',
                'CF_ADS_', 'GEMINI_', 'GOOGLE_API_')
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(prefixes)}
    env.update(EXACTCUT_DATA_ROOT=root, EXACTCUT_ENVIRONMENT='development')
    raise SystemExit(subprocess.call([
        '/Users/matao/miniconda3/envs/py3.12/bin/python3.12',
        '-m', 'pytest', '-o', 'addopts=', '-q'
    ], env=env))
PY
```

初始隔离基线为 364 passed，退出码 0。未隔离时，开发数据根中的环境标记会影响 production 模拟；另一次给整个测试进程固定 `EXACTCUT_SOURCE_ROOT` 后，正片缓存跨 fixture 污染。分别采用临时数据根、清理继承环境以及取消固定源根后通过，未删除用户数据、未放宽环境保护，也未隐藏或跳过失败。排障证据同步保存在工作区外的 `troubleshooting-runbooks/runbooks/exactcut/linux-portability.md`。

## 提交与测试证据

实施期间按三个步骤独立提交。

测试先行的历史证据：先执行新行为回归观察预期失败，再做最小实现和回归；并非每项补充兼容性用例都曾失败，以下仅列实际观察的 RED，不将直接 GREEN 的边界覆盖冒充 RED。

| 步骤 | 实施前实际 RED | 修复后专项 GREEN | 完整测试提交 |
| --- | --- | --- | --- |
| 1 | `pytest tests/test_concurrency.py -q`：7 failed / 1 passed；缺 lane、阶段入口与后继、延迟 poll 未生效。缓存源 render、enabled 类型校验、非 batch 恢复及交付失败重试分别补失败复现 | 首轮 25 passed；后续针对性修复 18、36、38 passed | `e142997`，386 passed |
| 2 | 首轮 `test_concurrency_recovery.py`：8 failed；后续 owned/启动窗口、回执持久化、SIGHUP 与飞书实际过滤/回读分别观察 2、1、3、5 个失败 | 对应修复后专项 30、35、66、53、85 passed；最终交付组 88 passed | `fb9efa2`，425 passed |
| 3 | 资源组首轮 12 failed；AI 入口/线程作用域 5 failed；预览预约 2 failed；单令牌重入、Gemini 处理循环、诊断/纠正检查点和缓存兼容性均先复现失败 | 分组 13、18、21、146 passed；最终诊断/缓存相关 86 passed | `00e213f`，471 passed |

以上专项命令均使用指定 Python，在 `source/` 下运行对应测试文件。原有同 batch 串行等已经成立的约束作为兼容性 GREEN 保留。本轮审查新增行为继续逐项先 RED，再最小修复转 GREEN，随后完整测试；各组证据在下文补记。

单元验收映射（每项均用临时数据或 fake process/client，可独立运行）：

| 必须覆盖的单元 | 回归位置 |
| --- | --- |
| lane 容量准入、全容量 1、暂停仍占名额 | `source/tests/test_concurrency.py`、`source/tests/test_round1_scheduler.py` |
| 阶段推进、确定性后继与 `not_before` 重入 | `test_concurrency.py::test_fetch_completion_queues_one_render_and_poll_reschedules`、`test_delayed_poll_survives_recovery_without_interruption` |
| 同 batch / 同旧项目串行 | `test_concurrency.py::test_same_batch_serial_including_stopped_executor`、`test_concurrency_recovery.py::test_legacy_same_project_serializes_with_render_capacity_two` |
| 磁盘同盘归并、预约释放、重启重建 | `source/tests/test_concurrency_recovery.py` 的 reservations、launch failure、device remap 用例 |
| 进程身份认领、owned 共存、断点重排 | `source/tests/test_phase1_recovery.py`、`source/tests/test_concurrency_recovery.py` 的 adoption、dead stage、SIGHUP 用例 |
| AI 共享配额、处理等待、缓存重入、旧 CLI 等待 | `source/tests/test_step3_resources.py` |

第 1 步：新增 lane 准入、owner 公平轮转、同 batch 串行、fetch/render 与 upload/poll 阶段推进、延迟 poll、按 lane 恢复。保留旧 CLI 与函数入口。新增回归覆盖 lane 满不阻塞其他 lane、双 render、跨 tick 公平、暂停进程、共享源缓存衔接、poll 超时和失败后人工重试。

完整测试：386 passed，57.23 秒，退出码 0（独立临时数据根）；`git diff --check` 通过。独立审查发现的重试计时、旧管理任务恢复容量和失败发布状态三处边界均已补回归。

第 2 步：新增 `.web-state/stage-results/<job>-<attempt>.json` 不可变证据；输入指纹排除调度容量与可变运行记录，校验成片、预览或正片 SHA。每 tick 恢复全部任务，活身份认领；中断从未完成阶段重排，落盘结果补推进。阶段结果缺失且没有不明交付意图时可安全重排；显式启动失败与 SIGHUP 分开处理。

磁盘账本为 `.web-state/reservations.json`，由 flock 保护，按当前 `st_dev` 合并峰值估算。admission 保留原始目录预算，重启重建时重新映射设备号；旧活任务补建预算，暂停任务保留预约，失败/结束释放。预算是保守的空间估算，非文件系统硬配额。

upload/poll 先保存 intent，再执行与核对。OSS 回读 SHA 元数据，飞书回读目标 URL；已知记录 ID 和回执在核对前保存。受保护 API 调用单次执行，不隐藏 sleep 或重放。结果不明转 `unverified`，错误提示“待核实”，自动与人工重复提交均拒绝。旧 `workflow.publish` 复用同一保险流程，保留同步等待作为兼容适配器。

完整测试：425 passed，58.71 秒，退出码 0（独立临时数据根）；`git diff --check` 通过。覆盖身份认领与 owned 共存、两个启动窗口、信号中断、结果和输入变化、同盘超卖、释放、旧预约补建、设备号重映射以及外部回执丢失。poll 回读覆盖素材 ID、重试次数、状态和视频 ID，缺字段或读回超时保留回执并阻止再次导入/重同步。

初次第 2 步的兼容边界（历史状态，已由本轮 C 组修正）：独立 CLI `execute --publish` 当时仍是旧同步流程，未接入阶段交付 intent。网页旧项目级发布入口要求逐条预览确认；新批次统一走受保护阶段。

第 3 步初次提交：默认配置的 fetch/render/upload/poll、总执行器上限、交互预览上限和 AI 突发容量均为 1。生产模板当时为 render=3、fetch=2、upload=2、poll=1、总量=8；本轮 A 组将 poll 改为 4。poll 的等待通过 `not_before` 调度。Compose 默认 14 CPU / 24g、`pids_limit=512`，不修改机器部署配置。所有媒体 ffmpeg 调用在每个输入前限制解码线程为 1，输出编码线程为 4，普通/复杂滤镜线程为 1；真实多输入音视频夹具验证参数作用域。

AI 请求采用 `.web-state/ai-rate.json` 与非阻塞 flock 的跨进程令牌桶，覆盖助手、表格解析、Hook/定位、预检及安全检查。配置为 `concurrency.ai.requests_per_minute=6`、`burst=1`，这是保守可配置预算，不是经核验的供应商配额。耗尽时抛 `ResourceDeferred`，保存等待结果、释放执行位和磁盘预约；旧未分阶段任务同样支持。SDK 隐藏重试关闭。逐镜头检查与预览已完成决策持久保存，避免单令牌下重排重复前半段调用。Gemini 文件处理每次只查询一次，保存上传句柄，未就绪或配额不足时延迟重排；令牌只在实际模型推理前扣除，文件生命周期调用不计模型配额。

Hook 第一次回答不合规时，仅保存可信的纠正标记，配额重排后继续纠正轮次，不把模型错误内容写进提示词。多模型连接预检保存已核对的单模型进度，绑定模型、提示、schema、endpoint 与鉴权摘要；单项证据最多保留 300 秒，整轮完成或终止失败时清理，下一次完整诊断重新请求。预检 CLI 资源不足输出结构化等待状态与重试时间，退出码为 75，不回显供应商错误正文或密钥。

可选 Hook 缓存的损坏 JSON 或非对象 JSON 会忽略并重建；清理检查点不能覆盖原始 AI 错误。独立质量审查新增的这些兼容性用例使用模拟模型结果验证。

暂停执行器计入 resident 总量，恢复已有执行器不申请额外进程位置；交互预览共享 render/总量预算，并预约临时与项目文件系统空间，worker 恢复扫描保留活预览预约。助手模型调用移出全局修改锁；预览和表格解析资源忙返回 429 与 `not_before`，助手沿用 `mode=busy` 响应并附可重试时间，不等待令牌。无效并发配置在数据和账号初始化之前拒绝。

第 3 步最终完整测试：471 passed，60.80 秒，退出码 0（独立临时数据根）；`git diff --check` 通过。新增资源测试覆盖真实多输入 FFmpeg、跨进程令牌抢占与重启/时钟回拨、所有 AI 入口配额耗尽、暂停 resident 准入与恢复、交互预览预约保留、旧任务等待资源、Gemini 处理检查点、多模型预检、纠正轮次及损坏缓存重建。规范审查与最终三步集成质量审查均通过。未执行真实生产并发性能/内存峰值标定，供应商实际额度尚未核验。

## 第 1 轮审查修复（2026-10-10）

本轮规范为 `docs/concurrency-refactor-grok-review.md` 与编排者核对后的十条决定。逐组先写失败回归，再实现、独立规范/质量审查、完整测试和独立提交；不部署生产。生产验收由编排者在本轮修复后的已提交版本执行，以下生产栏仍为“未执行”。

### A：暂停名额、保守缺省与公平可见性（#1、#6、#8）

非僵尸的暂停执行器计入 lane 和 resident 总量；恢复已驻留执行器不重复申请名额，但实际驻留数已超过 lane 或总量时拒绝继续。按 `(pid, process_token)` 去重，兼容 spawn 后 PID 尚未保存的身份查找。恢复和准入共用持久 owner 轮转游标。生产 poll 改为 4，仍受总量 8 约束；默认总量保持 1。

`settings()` 自身缺省总量由 lane 之和改为 1；进程启动在初始化数据/账号之前记录实际 `enabled`、各 lane、resident 总量、预览与 AI 设置。代码证据澄清：`util.load_config()` 会合并 `default.json`，因此实际 runtime 缺 concurrency 或只有 lane 时原本已继承总量 1；审查中“现网必然默认总量 4”的推断不成立。函数内部 fallback 确有 4 的缺陷，本轮修复并独立覆盖两层读取。

只读 `queue_position` 为 queued job 在 owner 公平遍历中的 1-based 优先位置，包含延迟、暂停及 lane 暂满的 job；不是下一次必定启动的承诺。非 queued 为 null，字段不写回任务，不接受客户端设置，响应沿用账号访问过滤。

TDD：首轮新回归 13 个预期失败；补删除 batch 后的只读响应 RED→GREEN。完整回归进一步发现“不同 PID 但相同 token”误去重，新增两个身份回归先 RED，再按真实身份修复。独立质量审查复现“running 已保存但 remember_child 未写入”的恢复窗口，补 running/queued/owned 三种回归，保证活任务先被完整认领，再进行 SIGCONT 容量判断。既有启动失败测试的 fake Popen 需配套 FakeProcessTable，避免系统 ps 消耗其 launcher side effects；生产身份发现逻辑未删减。

冻结后 macOS 完整测试 **496 passed，61.64 秒，退出码 0**；按上文完整隔离命令执行，无跳过。规范复审专项 102 passed，质量复审专项 142 passed；`git diff --check` 通过。三个原有前端文件合并 diff SHA256 保持 `bd5e9ad1508e657942fdff60de8d7974c43cc32ec564d7ce7a962f0aba81ad8d`，没有纳入暂存。

### B：锁外证据准备、显式恢复失败与物理磁盘峰值（#2、#3、#7）

保留直接 `stage_results.verified()` 的强 SHA 校验；worker 使用快速证据路径，核对文件路径、dev/ino/size/mtime_ns/ctime_ns 与子进程持久化摘要。阶段结果新增可选文件身份与 JSON 证据摘要，检测同大小替换、恢复 mtime 的修改和结果元数据损坏。旧 schema 1 结果没有这些字段时，由有上限的后台强校验处理，不把缺字段视为不兼容。后台摘要最多两项同时进行，无无界 executor 排队；相关 job 延迟，其他用户/其他 lane 继续准入。

已退出进程的校验等待不占 lane、resident 或磁盘预约；同 batch 保留逻辑 fence 到证据判定结束。活进程先核验身份并认领，再进行摘要等待判断，避免重新引入 A 组修复的恢复超额窗口。fetch 的有效摘要可直接供后继 render 使用。锁外计算前后核对文件身份，变化时拒绝接受过时证据。

completed fetch/upload 有 attempt、没有后继且结果缺失或不可信时，任务显式失败并写“阶段结果缺失或校验失败”原因；交付意图不明仍进入待核实，不自动重放。已存在确定性后继的历史任务无需重复核验和阻塞当前 batch。

render 空间估算按物理组成拆分：输出峰值（最终成片及较小预览）、缓存基底、TMPDIR 中的切段/合并中间文件，各自只算一次，再按 st_dev 归并；同盘总量为一份完整 render_budget，而非向三个目录各加同一预算。子进程 require_space 与准入使用同一拆分，避免预约后重复索要三倍空间。upload/poll 按正式成片大小预约，包括旧 batch publish action 的 lane 映射。

启动恢复也使用 scheduler 校验，先启动 HTTP/worker，再收割后台结果，不让旧大素材延迟健康检查。旧 workbook 编译与剪到整集末尾的时长探测同样移到有界后台准备。磁盘峰值是保守的每设备空间估算，预览在渲染临时目录清理后生成；CRF 编码不提供硬字节上限。

TDD：首轮六个行为失败；新摘要/身份接口两个失败；死 owned 等待占位一个失败；摘要元数据损坏与父 fetch 复用两个失败；同 batch 校验互斥与旧 publish 预约两个失败；旧 workbook 锁内准备一个失败；活认领顺序与历史父阶段两个失败；启动非阻塞恢复一个失败。后补的输入变更和旧结果栅栏覆盖直接 GREEN，未冒充 RED。专项最终 113 passed；独立规范专项 65 passed、启动新增单测通过；质量专项 65 passed、最终新增证据组 21 passed。

≥1 GiB 回归使用独立临时根下的稀疏文件与受控慢摘要，验证另一用户在 1 秒内入队和其他 lane 准入，同时验证哈希线程不持两把全局锁；这证明调度隔离，不代替真实磁盘吞吐或生产 RSS 测量。HTTP 人工确认的强 SHA 在本组尚未扩大修改，后续交付入口组继续处理。

本组最终 macOS 完整测试 **517 passed，61.98 秒，退出码 0**；使用上文隔离命令，无跳过。`git diff --check` 通过，规范及质量最终复核通过；前端 diff SHA 保持不变。

### C：受控核实、一次重登与关闭旧发布旁路（#4、#5，补齐 #2 HTTP 确认）

新增仅管理员可调用的 `POST /api/batches/<batch_id>/verify-delivery`。请求体只接受 `request_id`、原正式成片 `sha256`、可选 `submission_token`，不接受客户端指定远端 ID。服务器从已保存 publication、intent receipt 与历史回执核对唯一的飞书记录、material、OSS URL/key 和批准 SHA；缺失或冲突拒绝。新 job 强制为 poll，即使关闭并发开关也不能回到上传入口。

核实模式只 GET 原飞书记录和原 CF material，不调用 OSS 上传、ingest、resync 或飞书创建/修改。原 intent 保存在可选 `verification_history`，只读身份绑定跨 `not_before`、崩溃与再次核实保留；普通 queue_publish、upload、旧 workflow.publish 不能绕过。远端 ADS 素材字段为空不能算核实成功，继续待核实并要求人工调查，本轮不自动修复外部记录。

守护模式遇到 100006/100009 重新登录一次并重试一次，保留请求自定义 headers；未知/网络错误不循环重放，不 sleep。登录失败或第二次失败经阶段保险进入待核实。CLI `execute --publish` 在配置、编译及执行之前非零拒绝，提示使用审片确认后的阶段上传；同时带 `--publish-dry-run` 也拒绝，单独干跑保留。

HTTP publish/核实确认分成两次独占 mutation flock 与 LOCK 准入，中间完整 SHA 在两把锁外计算；重新准入检查账号权限、维护、暂停/归档、批次快照及文件身份（含 ctime），拒绝过时确认。≥1 GiB 稀疏成片受控慢摘要回归中，另一用户启动在 1 秒内返回 202。

TDD：首轮 12 RED→12 GREEN；HTTP 首轮 5 failed/15 passed→GREEN；只读失败后上传绕过、历史回执身份替换、远端 ADS 为空、丢失只读标识分别先 RED 再修复。规范复核发现只读 poll 本地启动失败会落为 publish_failed、堵住再次核实；新增完整入队→本地失败→新 token 核实→原回执就绪用例先 RED，再保留 unverified，所有外部写调用增量为 0。≥1 GiB 回归强化为真实 Alice/Bob 独立账户、会话和 batch owner。追加延期、崩溃、授权与维护/状态变更、幂等审计、干跑边界首次 GREEN，作为补充覆盖而非红灯证据。新增独立交付测试 42 例，专项 **199 passed，20.21 秒**；独立规范复审 **42 passed，12.56 秒**，质量复核 **42 passed，12.52 秒**，均批准。

本组最终 macOS 完整测试 **559 passed，75.34 秒，退出码 0**；使用上文隔离命令，无跳过。`git diff --check` 通过，前端 diff SHA 保持不变。受控慢摘要只证明锁隔离，不测真实 ≥1 GiB 哈希吞吐；生产仍未执行。

## 回退

回退只调整运行配置或代码，保留当前数据根、任务记录及 schema 1 数据，不恢复旧数据库。切换代码前应按发布管理器现有规则等待活动执行器结束；本任务不执行发布。

使用 `EXACTCUT_CONFIG_FILE` 指向的集中运行配置，将 `concurrency.enabled` 设为 `false`，并把 `render`、`fetch`、`upload`、`poll`、`max_active_children` 均设为 1，即可恢复全局串行准入与新任务的旧入口。已经提交的阶段任务仍能继续，交付 intent 与“待核实”保护保留。生产的 `source/config/production.json` 是初始化模板，修改模板不会自动覆盖既有 `/srv/exactcut/config/runtime.json`；后续发布/回退应通过现有配置流程确认实际生效值，不能只看源码模板。容器机器额度仍保持 14 CPU / 24 GiB，容量回退不需下调它们。

## 生产验收模板（未执行）

本次仅在本地 fixtures 与临时数据根验证，不发布、不访问生产、不执行真实外部上传。生产容量是用户指定的初始配置，尚无本轮真实业务吞吐、内存峰值或并行编码测量。

后续发布由现有发布管理器处理，记录干净提交、固定镜像、release ID、健康检查和回退版本；保留 `deployment.json` 中 14 CPU / 24 GiB。使用独立验收账号及授权素材，按下表记录证据；外部写验收需另行授权，不能把本地模拟结果填入生产栏。

| 场景 | 操作与观察 | 必须记录的证据 | 当前状态 |
| --- | --- | --- | --- |
| 两用户并行 | 不同 batch 同时提交，确认两个 render 可存活且同 batch 始终单执行器 | job/owner/batch/lane、PID、启动/结束时间、CPU/内存/线程峰值 | 未执行 |
| 等待让位 | A 等 FBBM，B 继续制作；A poll 查询结束至 `not_before` 前没有活进程 | 阶段结果、队列时间、进程清单 | 未执行 |
| 公平与暂停 | A 连续提交，B 获得执行机会；暂停/归档 A 后 B 继续，resident 总量受限 | 准入顺序、暂停控制及磁盘预约账本 | 未执行 |
| 重启恢复 | 各阶段执行中和结束后重启，核对活身份认领、断点重排及不重复交付 | attempt/result_ref、产物 SHA、intent、远端回执与外部调用次数 | 未执行 |
| 结果不明 | 受控模拟外部写后回读失败，确认“待核实”且不自动重放 | intent/回执、错误、后续调用次数 | 未执行 |
| 保守回退 | 维护窗口将容量恢复 1 或关闭并发开关；继续读取 schema 1 旧任务 | 生效配置、队列推进、数据保留及健康检查 | 未执行 |
