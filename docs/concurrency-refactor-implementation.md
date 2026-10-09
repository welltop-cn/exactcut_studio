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

本轮实测开发环境为 macOS 15.8.1 arm64，conda-forge Python 3.12.12；未更换解释器或使用生产数据根。

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

保留直接 `stage_results.verified()` 的强 SHA 校验；worker 使用快速证据路径，核对文件路径、dev/ino/size/mtime_ns/ctime_ns 与子进程持久化摘要。阶段结果新增可选文件身份与 JSON 证据摘要，检测同大小替换、恢复 mtime 的修改和结果元数据损坏。旧 schema 1 结果没有这些字段时，由有上限的后台强校验处理，不把缺字段视为不兼容。后台摘要最多两项同时进行；第 2 轮 P1-5 改用 spawn 独立读取进程及父/子两侧截止时间，无无界排队；相关 job 延迟，其他用户/其他 lane 继续准入。

已退出进程的校验等待不占 lane、resident 或磁盘预约；同 batch 保留逻辑 fence 到证据判定结束。活进程先核验身份并认领，再进行摘要等待判断，避免重新引入 A 组修复的恢复超额窗口。fetch 的有效摘要可直接供后继 render 使用。锁外计算前后核对文件身份，变化时拒绝接受过时证据。

completed fetch/upload 有 attempt、没有后继且结果缺失或不可信时，任务显式失败并写“阶段结果缺失或校验失败”原因；交付意图不明仍进入待核实，不自动重放。已存在确定性后继的历史任务无需重复核验和阻塞当前 batch。

render 空间估算按物理组成拆分：输出峰值（最终成片及较小预览）、缓存基底、TMPDIR 中的切段/合并中间文件，各自只算一次，再按 st_dev 归并；第 1 轮曾按一份 render_budget 分摊；第 2 轮 P1-1 修正为完整输出峰值 + 独立临时峰值，同盘合计两份 render_budget。子进程 require_space 与准入使用同一拆分，避免预约后重复索要三倍空间。upload/poll 按正式成片大小预约，包括旧 batch publish action 的 lane 映射。

启动恢复也使用 scheduler 校验，先启动 HTTP/worker，再收割后台结果，不让旧大素材延迟健康检查。旧 workbook 编译与剪到整集末尾的时长探测同样移到有界后台准备。磁盘峰值是保守的每设备空间估算，预览在渲染临时目录清理后生成；CRF 编码不提供硬字节上限。

TDD：首轮六个行为失败；新摘要/身份接口两个失败；死 owned 等待占位一个失败；摘要元数据损坏与父 fetch 复用两个失败；同 batch 校验互斥与旧 publish 预约两个失败；旧 workbook 锁内准备一个失败；活认领顺序与历史父阶段两个失败；启动非阻塞恢复一个失败。后补的输入变更和旧结果栅栏覆盖直接 GREEN，未冒充 RED。专项最终 113 passed；独立规范专项 65 passed、启动新增单测通过；质量专项 65 passed、最终新增证据组 21 passed。

≥1 GiB 回归使用独立临时根下的稀疏文件与受控慢摘要，验证另一用户在 1 秒内入队和其他 lane 准入，同时验证哈希线程不持两把全局锁；这证明调度隔离，不代替真实磁盘吞吐或生产 RSS 测量。HTTP 人工确认的强 SHA 在本组尚未扩大修改，后续交付入口组继续处理。

本组最终 macOS 完整测试 **517 passed，61.98 秒，退出码 0**；使用上文隔离命令，无跳过。`git diff --check` 通过，规范及质量最终复核通过；前端 diff SHA 保持不变。

### C：受控核实、一次重登与关闭旧发布旁路（#4、#5，补齐 #2 HTTP 确认）

新增仅管理员可调用的 `POST /api/batches/<batch_id>/verify-delivery`。请求体只接受 `request_id`、原正式成片 `sha256`、可选 `submission_token`，不接受客户端指定远端 ID。服务器从已保存 publication、intent receipt 与历史回执核对唯一的飞书记录、material、OSS URL/key 和批准 SHA；缺失或冲突拒绝。新 job 强制为 poll，即使关闭并发开关也不能回到上传入口。

核实模式只 GET 原飞书记录和原 CF material，不调用 OSS 上传、ingest、resync 或飞书创建/修改。原 intent 保存在可选 `verification_history`，只读身份绑定跨 `not_before`、崩溃与再次核实保留；普通 queue_publish、upload、旧 workflow.publish 不能绕过。远端 ADS 素材字段为空不能算核实成功，继续待核实并要求人工调查，本轮不自动修复外部记录。

第 1 轮守护模式曾对所有请求重登重试；第 2 轮 P1-2 限定为已知 GET 状态/额度查询，写请求不重登重放，保留请求自定义 headers；未知/网络错误不循环重放，不 sleep。登录失败或第二次失败经阶段保险进入待核实。CLI `execute --publish` 在配置、编译及执行之前非零拒绝，提示使用审片确认后的阶段上传；同时带 `--publish-dry-run` 也拒绝，单独干跑保留。

HTTP publish/核实确认分成两次独占 mutation flock 与 LOCK 准入，中间完整 SHA 在两把锁外计算；重新准入检查账号权限、维护、暂停/归档、批次快照及文件身份（含 ctime），拒绝过时确认。≥1 GiB 稀疏成片受控慢摘要回归中，另一用户启动在 1 秒内返回 202。

TDD：首轮 12 RED→12 GREEN；HTTP 首轮 5 failed/15 passed→GREEN；只读失败后上传绕过、历史回执身份替换、远端 ADS 为空、丢失只读标识分别先 RED 再修复。规范复核发现只读 poll 本地启动失败会落为 publish_failed、堵住再次核实；新增完整入队→本地失败→新 token 核实→原回执就绪用例先 RED，再保留 unverified，所有外部写调用增量为 0。≥1 GiB 回归强化为真实 Alice/Bob 独立账户、会话和 batch owner。追加延期、崩溃、授权与维护/状态变更、幂等审计、干跑边界首次 GREEN，作为补充覆盖而非红灯证据。新增独立交付测试 42 例，专项 **199 passed，20.21 秒**；独立规范复审 **42 passed，12.56 秒**，质量复核 **42 passed，12.52 秒**，均批准。

本组最终 macOS 完整测试 **559 passed，75.34 秒，退出码 0**；使用上文隔离命令，无跳过。`git diff --check` 通过，前端 diff SHA 保持不变。受控慢摘要只证明锁隔离，不测真实 ≥1 GiB 哈希吞吐；生产仍未执行。

### D：旧协议回退守卫、部署额度与端到端证据（#9、#10）

Linux 发布管理器新增只读 `check-switch VERSION`，按固定目标镜像的 `io.exactcut.stage-protocol=1` 标签识别阶段协议；旧镜像缺标签时，有未完成 phase job 就拒绝。queued/delayed、暂停/归档、unverified、未知状态和坏 JSON 均阻止旧代码；普通 completed/failed/interrupted 历史可保留，关联的 pending/unverified 交付仍阻止。正式切换在维护锁获取后、排空后、停服后均重查，自动健康失败恢复也不能切到不兼容旧执行器。Docker inspect 失败返回脱敏 JSON、非零退出；只读检查不会创建维护标记或变更容器。

核实完成后保留原 job、intent history 和不可变结果，用 `resolved_by`、`resolved_at`、`resolution=verified_delivery` 把已证实的旧 unverified job 收敛为 interrupted 历史；不会把旧 upload 标成 completed 再派发后继。完成核实的新 poll 必须有已验证成功结果、原身份绑定及 ready 回读；原进程 S/T 时不能收敛。原结果文件存在时，必须验证摘要与完整回执，损坏/冲突不回退到较弱证据。原文件缺失时，只读任务用原保存绑定；普通任务须有匹配的准入指纹及同阶段历史 pending intent 中保存的 record/material 回执。不能仅按“同 batch”或“当前 ready”放行；身份证据也丢失时仍拒绝旧代码回退，需人工调查并保留新代码。

生产 Compose 保持 14 CPU / 24g / 512。原管理器已从 deployment.json 显式传 CPU/内存额度并覆盖继承环境，本轮补真实数值回归和缺失/非法额度提前拒绝；不接受审查中把此美东实例默默下调为1/2的建议。dev Compose 约束为1 CPU / 2g / 256。FFmpeg 的4是编码器线程参数，输入解码/滤镜另设1，进程 NLWP 不保证为4；生产操作单分别记录参数上限和 `ps -T` 实测总线程/RSS，不把三路编码器的12作为全部线程实测结论。

新增端到端 fixture 使用实际 workflow、worker_tick、阶段结果和磁盘账本，只替换进程、媒体/外部客户端边界；覆盖完整 fetch→render→人工确认→upload→poll→ready、两 owner/两剧并行且产物隔离、同 batch 串行、全容量1、FBBM等待让位、带部分产物的 render 崩溃恢复、暂停/归档隔离，以及核实后旧回退可用且外部写不重放。

TDD：初始发布守卫/额度用例 **22 failed / 5 existing passed**→GREEN。停服窗口新 phase 及核实后原 job 未收敛两个缺陷 **2 RED→2 GREEN**。独立规范复核发现“回执先于结果”的缺口与 Docker 错误非 JSON，追加 **5 failed / 4 passed→9 passed**；活/暂停原执行器收敛保护 **2 failed / 2 passed→4 passed**。原有完整链11例首先 GREEN（此前 fixture 的媒体时长错误不计业务 RED）；其余覆盖补强亦不伪造红灯。最终六文件专项 **173 passed，16.60 秒**；独立规范复审 **140 passed，16.25 秒**，质量复核 **123 passed，17.36 秒**，均批准。

本组冻结后 macOS 完整测试 **613 passed，76.98 秒，退出码 0**，无跳过。新增发布/集成用例合计54例；`git diff --check` 通过。下方11个 shell 块通过 `bash -n`，内嵌 Python 通过 compile，仅检查语法，未执行生产命令。三个前端原有 diff SHA 保持不变；schema 1 与生产机器额度未更改。生产真实两用户并发、峰值 RSS/线程、供应商额度及真实重启恢复仍未执行，由编排者填写操作单证据。回执/原身份同时丢失或冲突的交付继续人工调查，不能通过删记录回退。

提交后另从本组已提交版本创建临时 `git clone --no-hardlinks`、detach checkout 并确认 `git status --porcelain` 为空。在 clone 的 source/ 下执行同一完整隔离命令，另将 `PYTHONPATH` 指向 clone/source，阻断原工作树导入：**613 passed，77.31 秒，退出码 0**，无跳过。该次验证的 `HEAD:source` 为 `5413b6082796a437e8fc003cebda4499e282550f`；随后仅把本条记录补入同组提交，source tree 不变。原有三个前端 dirty diff 未进入 clone，证明它们没有掩盖本轮回归。

### 本轮审查项与回归入口

| 审查项 | 决定及可复现测试入口（在 source/ 下运行） |
| --- | --- |
| #1 暂停名额 | 修复；`tests/test_round1_scheduler.py` 的停止子进程、恢复越限及活身份认领用例 |
| #2 大片哈希 | 修复 worker/startup 和 HTTP 两条锁路径；`tests/test_round1_evidence.py` 与 `tests/test_round1_delivery.py` 的 1 GiB 慢摘要及身份栅栏用例 |
| #3 completed 无后继 | 修复为显式失败；`test_completed_missing_result_does_not_stick`、`test_completed_invalid_artifact_has_visible_failure` |
| #4 待核实与重登 | 修复；`tests/test_round1_delivery.py` 的只读回执、超时/本地启动失败重试和 100006/100009 用例 |
| #5 旧 CLI 发布 | 早期非零拒绝；`test_legacy_live_execute_rejected_before_manifest_and_external_write` |
| #6 缺省与日志 | 函数 fallback 修复；实际 load_config 已合并安全默认，以代码证据反驳“runtime 缺段必然总量4”；`test_missing_total_is_conservative_without_default_merge`、启动三种配置日志用例 |
| #7 磁盘预约 | 修复；`test_delivery_reserves_approved_final_bytes`、`test_same_disk_render_reserves_single_output_and_temporary_peak` |
| #8 poll、公平、排队位置 | 修复；`tests/test_round1_scheduler.py` 的四 poll + render、持久 owner 游标和只读队列位置用例 |
| #9 回退守卫 | 新管理器按目标镜像阶段协议检查；`tests/test_round1_release.py` 与 `tests/test_round1_integration.py` 的旧镜像拒绝、配置开关、核实收敛与停服竞态用例 |
| #10 配额与线程 | 按用户决定保持生产 14/24，验证显式部署额度并约束 dev；编码线程上限与实际总线程分别记录，不声称 ffmpeg 进程总线程为4 |

## 回退

回退只调整运行配置或代码，保留当前数据根、任务记录及 schema 1 数据，不恢复旧数据库。**代码回退必须先排空队列和活动执行器**，包含 delayed poll、暂停/归档的未完成任务及待核实交付；不能删除 job/intent 或强杀来绕过守卫。旧版本不认识阶段命令，新的发布管理器需先检查目标镜像协议能力。

保守配置在新代码上把 `fetch`、`render`、`upload`、`poll`、`max_active_children`、`hook_previews`、AI `burst` 设为 1；`enabled=false` 只让新提交走旧入口，**不会转换已持久化的 phase job**。已有阶段任务继续单次查询与 not_before 重入，交付 intent 和待核实保护保留，不能把关闭开关当作旧代码已可安全运行的证据。

集中运行配置为 `EXACTCUT_CONFIG_FILE` 指向的 runtime。production.json 是初始化模板，改模板不会覆盖既有 `/srv/exactcut/config/runtime.json`。按下方维护配置事务只替换 concurrency 段，随后通过新 release ID 重建容器挂载，核对容器内配置 hash 和实际生效日志。容器机器额度继续 14 CPU / 24 GiB，保守容量回退不下调它们。

## 生产验收操作单（本会话未执行；由编排者执行）

全部命令在美东实例 `8.221.122.75` 的维护会话中由编排者执行。保持部署根 `/srv/exactcut`、现有账号/数据、14 CPU / 24 GiB 不变；不运行 init、不恢复旧数据库、不在生产数据目录开发。真实素材制作/上传仅使用编排者指定的两名验收用户及两部已授权的不同剧，逐条人工审片后确认。以下命令不含密钥；证据目录需私有，不分享 production.env、密码、Cookie、token 或供应商错误正文。

### 1. 准备已提交的干净版本

本机工作区含用户原有未提交修改，不得直接拿它 build。编排者从最终提交建立干净 clone，或把该提交 git bundle 经现有安全传输渠道送到服务器。不能复制当前 dirty 工作树。

本机（将 COMMIT 填为本轮最终提交的完整 SHA）：

```sh
COMMIT='填写本轮最终完整提交 SHA'
TASK_EXPORT="$(mktemp -d /tmp/exactcut-acceptance.XXXXXX)"
git clone --no-hardlinks /Users/matao/code/exact-cut-fbbm-handoff "$TASK_EXPORT/engineering"
git -C "$TASK_EXPORT/engineering" checkout --detach "$COMMIT"
test -z "$(git -C "$TASK_EXPORT/engineering" status --porcelain)"
git -C "$TASK_EXPORT/engineering" bundle create "$TASK_EXPORT/engineering.bundle" HEAD
# 按现有安全通道传输 engineering.bundle；本会话未传输。
```

服务器工程 clone 放在 `/srv/exactcut/preparation/` 下独立新目录（root 所有，避免 dubious ownership）；已有 engineering 和已归档 release 不覆盖。后续命令在维护终端 `sudo -i` 中执行：

```sh
set -eu
COMMIT='与本机相同的完整提交 SHA'
VERSION="us-east-concurrency-$(date -u +%Y%m%dT%H%M%SZ)"
ENGINEERING="/srv/exactcut/preparation/engineering-$VERSION"
BUNDLE='/填写已收到的/engineering.bundle'
git clone "$BUNDLE" "$ENGINEERING"
git -C "$ENGINEERING" checkout --detach "$COMMIT"
test -z "$(git -C "$ENGINEERING" status --porcelain)"
test "$(git -C "$ENGINEERING" rev-parse HEAD)" = "$COMMIT"
install -m 755 "$ENGINEERING/source/deploy/linux_ops.py" /srv/exactcut/bin/linux-ops
/srv/exactcut/bin/linux-ops --root /srv/exactcut status
/srv/exactcut/bin/linux-ops --root /srv/exactcut build "$VERSION" --repo "$ENGINEERING"
# 发布前先完成下方“容量配置事务”，初次 MODE=safe；然后才 publish。
```

安装新管理器必须先于回退保护验收：旧管理器不知道阶段协议，不具备新守卫。build 在无生产密钥/业务数据挂载的测试容器运行 Linux 全量回归并保存 tests.log；publish 维护、排空、固定镜像切换和 healthz 验证均走管理器。任务不能排空时退出维护发布，不强杀、不绕过维护标记。

### 2. 核对运行额度与证据

先在容量全 1 下上线核对，随后在维护窗口仅更新 runtime 的 concurrency 段为最终 production 模板，不覆盖 web、storage 等现网设置。下面的容量配置事务须在各验收 batch 已核实、排空后执行。runtime 被单文件 bind mount，原子替换后用新的 release ID 发布（同一提交即可），使 Compose 重建容器、重新挂载文件。`recover` 的普通 up 不保证重建，不能用它代替这一步。记录配置变更前后 hash、容器内配置 hash 和实际启动日志；不一致时停止放量。

首次使用 `MODE=safe`、已 build 的 VERSION；放量使用 `MODE=tuned`；保守回退使用 `MODE=legacy`（关闭新任务阶段拆分且各容量 1）。每次都须有独立 VERSION；首次之外先再次 build：

```sh
# 首次保留上一节 VERSION/ENGINEERING；后续创建新 VERSION，再运行 build。
# VERSION="us-east-concurrency-tuned-$(date -u +%Y%m%dT%H%M%SZ)"
# /srv/exactcut/bin/linux-ops --root /srv/exactcut build "$VERSION" --repo "$ENGINEERING"
MODE=safe
export MODE ENGINEERING
python3 - <<'PY'
import importlib.util, os
from importlib.machinery import SourceFileLoader
from pathlib import Path
spec = importlib.util.spec_from_loader('release_ops', SourceFileLoader('release_ops', '/srv/exactcut/bin/linux-ops'))
ops = importlib.util.module_from_spec(spec); spec.loader.exec_module(ops)
root = Path('/srv/exactcut')
mode = os.environ['MODE']
if mode not in {'safe', 'tuned', 'legacy'}: raise SystemExit('未知容量模式')
profile = 'production.json' if mode == 'tuned' else 'default.json'
capacity = ops.read(Path(os.environ['ENGINEERING']) / 'source/config' / profile)['concurrency']
capacity['enabled'] = mode != 'legacy'
with ops.operation_lock(root), ops.write_barrier(root, 900):
    ops.await_idle(root, 900)
    if ops.phase_blockers(root): raise SystemExit('阶段队列未排空或交付待核实；停止修改容量')
    current = ops.read(root / 'config/runtime.json')
    assert current.get('environment') == 'production'
    settings = ops.read(root / 'config/deployment.json')
    assert float(settings['cpus']) == 14 and settings['memory'].lower() == '24g'
    print('runtime before:', ops.sha(root / 'config/runtime.json'))
    current['concurrency'] = capacity
    ops.write(root / 'config/runtime.json', current)
    print('runtime after:', ops.sha(root / 'config/runtime.json'))
PY
/srv/exactcut/bin/linux-ops --root /srv/exactcut publish "$VERSION" --drain-timeout 900
/srv/exactcut/bin/linux-ops --root /srv/exactcut status
```

事务只修改 concurrency；配置写成功但 publish 失败时旧容器可能仍挂着旧 inode。记录这一状态，在维护窗口恢复容量配置或重新发布，不直接覆盖 release 源码、不把旧数据库恢复当回退。

```sh
EVIDENCE="/srv/exactcut/preparation/evidence-$VERSION"
install -d -m 700 "$EVIDENCE"
date -u +%FT%TZ > "$EVIDENCE/started-at.txt"
cp /srv/exactcut/current.json "$EVIDENCE/current.json"
cp /srv/exactcut/config/deployment.json "$EVIDENCE/deployment.json"
sha256sum /srv/exactcut/config/runtime.json > "$EVIDENCE/runtime.sha256"
INSTANCE=$(python3 -c 'import json; print(json.load(open("/srv/exactcut/config/deployment.json"))["instance"])')
CONTAINER=$(docker ps -q --filter "label=com.docker.compose.project=$INSTANCE" --filter label=com.docker.compose.service=web)
test -n "$CONTAINER"
docker inspect --format '{{.Id}} {{.Config.Image}} NanoCpus={{.HostConfig.NanoCpus}} Memory={{.HostConfig.Memory}} PidsLimit={{.HostConfig.PidsLimit}}' "$CONTAINER" > "$EVIDENCE/container-limits.txt"
docker logs --since 10m "$CONTAINER" 2>&1 | sed -n '/Effective concurrency settings:/p' > "$EVIDENCE/effective-concurrency.txt"
docker exec "$CONTAINER" sha256sum /run/exactcut/runtime.json > "$EVIDENCE/container-runtime.sha256"
# 期望 NanoCpus=14000000000、Memory=25769803776、PidsLimit=512。
# 生产有效 lane=fetch2/render3/upload2/poll4，总量8；保守配置 lane及总量均1。
```

发布管理器 `Docker.compose` 从 deployment.json 显式提供 EXACTCUT_CPUS/EXACTCUT_MEMORY，覆盖继承环境；生产 compose 默认仍为批准的14/24g。dev compose 保守1/2g/256。`-threads 4` 约束输出编码器，不代表 ffmpeg 全进程总线程为4：输入解码、滤镜、音频和库线程均可另占线程，不能把“三路总线程12”当验收事实。

三路 render 运行期间连续采样（记录每路线程峰值及 RSS，核对实际参数；总线程数阈值需编排者根据实际输入数先写明，超阈值停止增加并发）：

```sh
# procps 已在运行镜像安装；ps -T/ -eLf 在 Linux 容器内观察线程。
for sample in $(seq 1 60); do
  date -u +%FT%TZ
  docker exec "$CONTAINER" sh -c 'for pid in $(pgrep -x ffmpeg || true); do if ps -p "$pid" -o pid=,ppid=,nlwp=,rss=,pcpu=,args=; then ps -T -p "$pid" -o pid=,spid=,stat=,pcpu=,comm= || echo "pid=$pid exited during thread sample"; fi; done'
  docker stats --no-stream --format '{{.ID}} CPU={{.CPUPerc}} Memory={{.MemUsage}} PIDs={{.PIDs}}' "$CONTAINER"
  uptime
  sleep 1
done > "$EVIDENCE/render-thread-samples.txt"
```

### 3. 场景执行与证据字段

工作台入口 `http://8.221.122.75/exactcut/`，用两个独立浏览器会话登录两个验收账号。每个账号选择不同项目/剧号，各创建至少一个 batch；记录用户名、batch_id、request_id、job_id 及准确提交时间。人工审片后逐条确认，不用旧 execute --publish。

每个观察点保存时间、任务/阶段结果/磁盘预约快照（避免复制密码/账号数据库）：

```sh
SNAPSHOT="$EVIDENCE/$(date -u +%Y%m%dT%H%M%SZ)"
install -d -m 700 "$SNAPSHOT"
cp -a /srv/exactcut/data/.web-state/jobs "$SNAPSHOT/jobs"
cp -a /srv/exactcut/data/.web-state/stage-results "$SNAPSHOT/stage-results"
cp /srv/exactcut/data/.web-state/reservations.json "$SNAPSHOT/reservations.json"
# 仅复制两名验收用户的 batch，不复制整个数据根、accounts.sqlite 或环境文件。
for bid in "$BATCH_A" "$BATCH_B"; do
  case "$bid" in (*[!0-9a-f]*|'') exit 1;; esac
  test "${#bid}" -eq 32
  install -d -m 700 "$SNAPSHOT/$bid"
  for name in runtime.json control.json confirmed.json publications.json; do
    if test -f "/srv/exactcut/data/.web-state/batches/$bid/$name"; then
      cp "/srv/exactcut/data/.web-state/batches/$bid/$name" "$SNAPSHOT/$bid/"
    fi
  done
done
docker exec "$CONTAINER" ps -ww -eo pid,ppid,pgid,stat,nlwp,rss,args > "$SNAPSHOT/processes.txt"
date -u +%FT%TZ > "$SNAPSHOT/at.txt"
```

| 场景（均未执行） | 直接操作及通过标准 | 必填证据 |
| --- | --- | --- |
| 完整链 | A fetch→render，等人工确认期间无执行器，确认后 upload→poll→ready；等待每次 poll 回到 not_before | 各 phase/job/attempt/result_ref、产物 SHA、审核 SHA、intent 与原 feishu_record_id/material_id、阶段开始/结束时间 |
| 两用户并行 | A/B 两部不同剧同时制作，至少观察两路 render 存活；同 batch 每时刻最多一个根执行器，队列位置可见 | owner/batch/project/lane/PID/token、准入延迟、queue_position、RSS与线程样本 |
| 等待让位 | A 上传后 FBBM 未就绪，两个 poll 查询之间没有 A 活执行器/预约；B render 在此期间开始并推进 | A not_before/状态/空窗、B started、进程表、预约账本；不能仅凭网页文案判断 |
| 暂停/归档隔离 | render容量3时暂停A，确认A的T进程仍占一个render与resident；B在剩余名额继续；归档A不改B | control paused/archived、SIGSTOP/CONT前后进程状态、B结果、resident≤总量；容量1时暂停A不会让同lane B越限 |
| 崩溃恢复 | 在A render与B delayed poll状态下注入一次容器崩溃，再经管理器 recover核对；fetch已完成不重做，render从未完成阶段重排，poll沿原交付ID；不重复上传/ingest | 注入时间/旧新容器ID/health、恢复attempt/源结果SHA、intent/回执、远端创建计数或审计；未知结果应待核实而非重放 |
| 待核实放行 | 只对已有完整回执的指定条目使用管理员受控核实入口；只查询原record/material，任何ID或SHA冲突都拒绝 | 动作/audit/原ID/批准SHA、前后intent与状态、外部ingest/OSS写/飞书create计数增量=0 |
| 保守配置回退 | 维护排空后仅把所有容量/总量/预览/AI burst设1；重挂载runtime，查看实际日志；一次只启动一个，delayed poll仍让位 | 前后runtime hash、容器hash、生效日志、连续进程采样、schema1数据未还原 |
| 旧代码回退守卫 | 新管理器对不支持phase的旧release执行只读检查；有queued/delayed/paused/unverified phase任务时必须拒绝且容器不变 | check-switch返回码/错误、target capability、未完成job清单、检查前后current/containerID |

管理员沿原回执核实的可执行请求（仅已有完整原回执的待核实条目；`curl --user admin` 交互输入密码，命令和证据不含密码）：

```sh
# 先从验收台/当前正式成片确认这三个值；不要填写新的远端 ID。
BATCH_A='填写32位batch_id'
REQUEST_ID='填写原request_id'
APPROVED_SHA='填写原正式成片64位SHA256'
export BATCH_A REQUEST_ID APPROVED_SHA EVIDENCE
python3 - <<'PY'
import json, os, uuid
from pathlib import Path
Path(os.environ['EVIDENCE'], 'verify-request.json').write_text(json.dumps({
    'request_id': os.environ['REQUEST_ID'], 'sha256': os.environ['APPROVED_SHA'],
    'submission_token': uuid.uuid4().hex,
}))
PY
BACKEND_PORT=$(python3 -c 'import json; print(json.load(open("/srv/exactcut/config/deployment.json"))["backend_port"])')
curl --fail-with-body --user admin -H 'X-Exactcut-Request: 1' -H 'Content-Type: application/json' \
  --data-binary "@$EVIDENCE/verify-request.json" \
  "http://127.0.0.1:$BACKEND_PORT/exactcut/api/batches/$BATCH_A/verify-delivery" \
  -o "$EVIDENCE/verify-response.json"
```

同 token 重复提交应返回原 job；核实查询失败后需要新的 token，但 record/material/批准 SHA 不能改变。接口没有新的上传放行选项，回执不足或远端 ADS 映射为空必须人工核对，不能删 intent 再上传。保存前后 publications、job、audit 动作时间与远端审计，确认外部创建增量为 0。

### 4. 受控崩溃和回退

崩溃注入是验收故障操作，不是发布：先确认当前固定 release、容器唯一且两个验收 batch 无其他用户任务，告知维护窗口。保存上述快照后仅杀已核对的 web 容器；restart策略会重启，随后由当前管理器 recover校验登记版本及健康。不得 kill 任意宿主PID，不在外部上传写入中随意注入故障；如测试交付崩溃窗口，必须单独限定验收素材并保存远端审计。

```sh
# 执行前再次核对 CONTAINER/current.json；当前管理器已安装。
docker kill --signal=KILL "$CONTAINER"
/srv/exactcut/bin/linux-ops --root /srv/exactcut recover
/srv/exactcut/bin/linux-ops --root /srv/exactcut status
# 重取容器 ID，记录恢复后的jobs/result/intent/process快照。
```

旧代码回退必须先排空**队列及活动执行器**，包含 not_before 延迟 poll、暂停/归档仍未完成任务及待核实交付。不能靠 enabled=false转换已落盘phase命令；不能删除job/intent绕过守卫。仅在原对象/记录核实完成、队列排空并确认目标可读schema1后执行管理器rollback，保留当前数据。守卫在现用 Linux 发布管理器；历史 Mac launchctl 管理器不在本轮美东验收路径中。

只读守卫干跑先在有未完成 phase job 时取证，预期退出 1、`allowed=false`；故意检查被拒绝不能让上方 `set -e` 提前结束：

```sh
OLD_VERSION='填写已核验的不支持阶段协议的历史release ID'
if /srv/exactcut/bin/linux-ops --root /srv/exactcut check-switch "$OLD_VERSION" > "$EVIDENCE/old-switch-blocked.json"; then
  echo '意外放行，停止验收' >&2
  exit 1
fi
# JSON字段：target/target_stage_protocol/phase_blockers/active_tasks/allowed，失败可含guard_error。
# 核对target_stage_protocol=false，blocked清单确有验收phasejob；current/container ID应无变化。
```

完成全部核实并排空后再执行（目标版本、镜像 ID、commit 和 schema 1 已核验）：

```sh
/srv/exactcut/bin/linux-ops --root /srv/exactcut check-switch "$OLD_VERSION" > "$EVIDENCE/old-switch-drained.json"
/srv/exactcut/bin/linux-ops --root /srv/exactcut rollback "$OLD_VERSION" --drain-timeout 900
/srv/exactcut/bin/linux-ops --root /srv/exactcut status > "$EVIDENCE/after-rollback.json"
```

新镜像的固定 ID 上有 `io.exactcut.stage-protocol=1`，缺标签视为旧协议，不信任可随意编辑的 metadata 布尔值；`data-schema.json` 仍为 1。必须先安装新管理器，旧管理器本身没有这个守卫。`check-switch` 仅为只读快照诊断，正式 transition 会在维护锁内重查，不能用成功干跑绕过正式发布流程。


## 第 2 轮修复（2026-10-10；未部署）

### R2-A / P1-1：分盘输出预算

TDD：先新增 `test_round2_storage.py`，红灯 2 failed（输出盘只有 budget/4 仍放行、同盘预算只有一份）；再修 `render_allocations`。输出盘完整预约 `render_budget()`；临时峰值另预约同样大小，project 基底缓存占临时预算 1/4、TMPDIR 切段/合并占 3/4。保守假设输出与临时可并存，同盘相加两份，scheduler 与 child 共用同一函数。旧同盘测试同步调整其预期，不缩减输出以满足旧断言。

macOS 15.8.1 arm64 / conda Python 3.12.12，命令（source 下）：`/Users/matao/miniconda3/envs/py3.12/bin/python3.12 /tmp/exactcut-review-tests.py`。脚本清理 EXACTCUT/OPENAI/ALIYUN/FEISHU/CF_ADS/GEMINI/GOOGLE_API 继承变量，以独立 TemporaryDirectory 数据根、development 环境执行 `python -m pytest -o addopts= -q`；无真实外部调用。完整 615 passed / 77.21s；`git diff --check` 通过。三设备输出不足拒绝、同盘第一份启动第二份拒绝、释放后再启动均可复现。


### R2-B / P1-2：守护写请求不重放

TDD：新 `test_round2_auth.py` 先见 9 failed / 2 passed：ingest、resync、PUT、DELETE 在两种过期码下均双发；“服务端已落账但报 100006”也创建两次。实现仅将已知 GET app_usage / material sync_status 判为可重试读；guarded 写只发一次，未知写结果由原 intent 保险进入 unverified，禁止第二次 ingest。读查询仍一次 login 后成功，原 headers 保留；普通非 guarded 历史重试策略未改。

聚焦 53 passed / 13.11s；同 R2-A 隔离命令完整 626 passed / 77.21s，`git diff --check` 通过。fixtures 中真实 CfAdsClient + workflow poll + immutable result + finish_job 验证外部创建次数=1、job unverified、intent pending；无真实请求。


### R2-C / P1-3：核实入队交接事务

TDD：`test_round2_verification.py` 红灯 4 failed / 1 passed（保存前故障 2、启动 orphan 2；保存后故障原本可认领是兼容基线）。核实 publication 的同一次原子 JSON 写中新增可选 `verification_queue`，记录预分配 job ID 与原 unverified/pending 快照；这是一份持久“待执行核实”事务，只有匹配的只读 poll job 落盘才提交。HTTP 异常补偿、每 tick/启动扫描共用恢复函数：缺 job 回原状态，已落盘 job 提交并可认领。旧无标记但 history/verification intent 已写且无核实 job 的交接也恢复为 unverified/pending，保留原回执与只读标识；禁止转成普通上传。

聚焦 69 passed / 17.55s；同 R2-A 的 macOS 临时数据根隔离命令，完整 631 passed / 80.14s，`git diff --check` 通过。publication 后/detail 或 save 前异常、保存后响应丢失、标记/旧 orphan 重启恢复均覆盖；同 token 重试 202（已落盘则200），只读 poll 原对象，ingest/OSS/飞书创建增量0、普通上传拒绝。


### R2-D / P1-4：发布预检与失败恢复

TDD：`test_round2_release.py` 先红灯 6 failed / 2 passed：支持阶段协议的新镜像曾放行待核实和损坏 publication，健康失败后原容器未拉起。修复要求 check-switch 与所有正向/回退 transition 的 phase_blockers 为空（包括停服前后重查），待核实在停服前拒绝。健康失败只允许紧急恢复本次刚停掉的、已 verify 的原 release；中途出现阻断则保留维护锁、记录 `maintenance_retained` 并给出绝对路径 recover 命令，不允许守卫把新旧两侧都停掉。此例外不放宽常规旧代码回退守卫。

P1-4(a) 的“缺文件阻断”未在当前代码复现：`linux_ops.read(path, default=None)` 对不存在文件原本返回 `{}`，phase_blockers 对该值取空 entry。新增 completed fetch/render 带 batch_id/request_id 且无 publications 测试均在修复前即通过，因此保留代码；坏 JSON / 非对象仍失败关闭，新增回归覆盖正向发布。

发布相关 56 passed / 0.80s；完整（本组提交候选全部测试）639 passed / 79.95s，`git diff --check` 通过。其间提前加入下一组的三个 RED 草案时曾得到 3 failed / 639 passed；失败均是 P1-5 预期红灯，草案暂移出本组候选后完整重跑全绿，不作为基线失败掩盖。

生产验收仍由编排者执行，**本会话未执行**。现有操作单的发布管理器命令、两用户并发、等待让位、重启和回退步骤保留；本轮新增门禁：正向发布同样先 `check-switch NEW_VERSION`，有 unverified/pending 不得停服。健康失败取证保存 current.json、history 中 restored / recovery_error_type / maintenance_retained、docker ps/inspect、健康响应、维护标记；若维护保留，先沿原回执显式核实并排空，禁止删除 job/intent 绕过，之后执行 `/srv/exactcut/bin/linux-ops --root /srv/exactcut recover`。常规失败且无阻断应恢复原 release 并撤维护。

第 10 项决定不变：生产14 CPU /24g /pids512。`-threads 4` **仅约束编码器**，验收必须保存上方 `ps -T`/NLWP 每路 ffmpeg 的实际线程样本，记录 release/commit/UTC时间/batch/ffmpeg PID/命令行/NLWP峰值/RSS/三路并发数及宿主load；不能把4或编码器合计12写为实测全进程线程数。该采样本会话未执行。


### R2-E / P1-5：证据瞬断与可回收超时

TDD：`test_round2_evidence.py` 三条主回归先 3 failed：失败 future 永久缓存、瞬断导致 completed fetch failed、两阻塞槽不释放。最小修复后另两条边界先 2 failed（取消计时回调与关闭句柄竞态、输入 stat 瞬断），再修复；父计时器丢失的孤儿读取回归先 1 failed，再补子进程自身截止时间。新增读错误参数分别覆盖 ValueError / OSError。

读取池替换为最多两项的 Python 标准库 spawn 进程，无 fork 继承服务锁、无新增中间件。成功证据按完整 stat/metadata 身份缓存，失败永不缓存；异常与 `None` 分开，瞬断抛 EvidencePending、下一 tick 重算，缺文件/身份/摘要/结果元数据不符才判失败。legacy 强校验启用异常透传，直接 verified 的旧兼容行为保留。单次读取默认120秒，可选 `concurrency.evidence_timeout_seconds` 通过 util.load_config 读取并验证有限正数；父计时器和子 SIGALRM 都能终止独立读取进程组（含其 ffprobe），即使服务崩溃也不留下永久读取。退出/失败/超时回收句柄并让槽位接受新任务，其他 batch 的非证据 job 不受阻。

旧线程 monkeypatch 在 spawn 子进程不继承，因此将原慢摘要/编译/变异回归改成真实独立进程 + 文件事件；保留 ≥1GiB 正片与另一用户 0.8s 内入队、同 batch fence、退出执行器释放 lane、无 backlog、身份变化拒绝等断言。新超时测试在不释放 gate 的情况下核对原 PID 已不存在并成功启动新读取，避免仅从字典删除“假回收”。计时回调与 close 通过独立生命周期锁串行化。

聚焦 28 passed / 7.34s；macOS 完整 646 passed / 86.92s，退出0，隔离命令同上；`git diff --check` 通过。前一次完整645 passed后增补孤儿超时测试再完整重跑，未以较早结果替代最后验证。生产实际磁盘吞吐、RSS、线程和供应商响应仍未执行，等待最终复审及编排者验收。


### R2-C 补充 / P1-3：已被旧扫描改成失败的孤儿核实

最终自查按历史恢复代码补第二个启动窗口：第 1 轮 recover_jobs 可能已将 history/verification intent 孤儿从 fbbm_queued 改为 publish_failed。TDD 扩展启动回归先 1 failed / 3 passed，再将 legacy 修复条件兼容这个状态；仍要求保存的 verification_only 和 verification phase，不能把普通上传失败放行。原 intent/pending、只读原对象、普通上传拒绝约束保留。

聚焦7 passed /4.08s；相同 macOS 独立临时数据根完整648 passed /88.46s，`git diff --check` 通过。此补充单独提交，不混入证据读取改造。


### 本轮最终验证与干净版本

本机 macOS 15.8.1 arm64 / Python3.12.12：完整648 passed /88.46s，退出0。独立 `git clone --no-hardlinks` 已提交版本 `63491d48c58f4b3b5c16123b7b2cf2cdcc371b38`：完整648 passed /87.93s，退出0；clone测试前后 `git status --porcelain` 均空，显式核对导入来自clone/source，清除继承凭据与配置，使用另一临时数据根。clone检查器首次路径断言遇到macOS `/var` 与 `/private/var` 别名，在运行pytest前退出；改为 resolve() 后执行上面的成功验证，没有隐藏测试失败。

本段证据是最后提交的纯文档 amend，source tree保持 `8a44a3dd8571cbd148617db5761af4348bb7494b`，与已验证clone完全相同。`git diff --check` 通过；原三前端合并 diff SHA256仍为 `bd5e9ad1508e657942fdff60de8d7974c43cc32ec564d7ce7a962f0aba81ad8d`。其他原未提交文档和原未追踪计划/审查文件均未暂存；主工作区保留这些用户改动，发布应从提交版本的独立干净工程执行。

六个聚焦提交：R2-A `89fa99a`、R2-B `16d4922`、R2-C `aa98342`、R2-D `97eff55`、R2-E `c605f1b`、R2-C兼容补充（最后提交）。每组包含对应TDD及完整测试记录。未决事项是第三轮最终复审与编排者的生产真实环境验收；本会话未部署、未真实上传。保守回退仍先排空阶段任务/活动子进程/待核实意图，再用发布管理器切版本，保留现有业务数据；当前代码上容量全1可保守运行，不能用 enabled=false 绕过旧版本协议守卫。

干净clone复现命令（从开发仓库根执行；只操作本地临时目录）：

```sh
/Users/matao/miniconda3/envs/py3.12/bin/python3.12 - <<'PY'
import os, subprocess, tempfile
from pathlib import Path
python = '/Users/matao/miniconda3/envs/py3.12/bin/python3.12'
repository = Path.cwd()
commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
with tempfile.TemporaryDirectory(prefix='exactcut-round2-clean-') as folder:
    clone = Path(folder) / 'repository'
    subprocess.run(['git', 'clone', '--no-hardlinks', str(repository), str(clone)], check=True)
    subprocess.run(['git', 'checkout', '--detach', commit], cwd=clone, check=True)
    assert not subprocess.check_output(['git', 'status', '--porcelain'], cwd=clone)
    prefixes = ('EXACTCUT_', 'OPENAI_', 'ALIYUN_', 'FEISHU_', 'CF_ADS_', 'GEMINI_', 'GOOGLE_API_')
    env = {k: v for k, v in os.environ.items() if not k.startswith(prefixes)}
    data = Path(folder) / 'data'; data.mkdir()
    source = clone / 'source'
    env.update(EXACTCUT_DATA_ROOT=str(data), EXACTCUT_ENVIRONMENT='development', PYTHONPATH=str(source))
    imported = subprocess.check_output([python, '-c', 'import exactcut; print(exactcut.__file__)'], cwd=source, env=env, text=True).strip()
    assert Path(imported).resolve().is_relative_to(source.resolve())
    code = subprocess.call([python, '-m', 'pytest', '-o', 'addopts=', '-q'], cwd=source, env=env)
    assert not subprocess.check_output(['git', 'status', '--porcelain'], cwd=clone)
    raise SystemExit(code)
PY
```

## 第 3 轮最终收尾（2026-10-10，本会话不部署）

### R3-A / P1-A：持久核实等待不能当孤儿

测试先行：新增 `tests/test_round3_verification.py`，先观察 waiting_resource / 未知等待状态的 publication 被回滚，以及75等待后重入失败；queued/running 的重复请求原本返回已有job（200），测试保留这一幂等合同，不要求创建新job。修复把“匹配job已存在”作为交接提交证据，不以状态白名单判断落盘；只有真正缺job才补偿。持久 waiting_resource 恢复为queued并保留not_before；重复核实对其他未终结状态拒绝。退出75的阶段结果与持久job状态是两层：当前worker通常把前者转成queued，本回归同时覆盖持久waiting_resource的兼容窗口。

独立临时数据根、假进程表与模拟客户端覆盖：75→资源等待、两次tick不回滚、不占执行位、固定时钟到期→原record/material只查询一次；期间新核实拒绝/已有queued请求复用原job。不存在OSS/飞书创建/ingest/resync。聚焦命令（source目录）：`/Users/matao/miniconda3/envs/py3.12/bin/python3.12 /tmp/exactcut-review-tests.py tests/test_round3_verification.py tests/test_round2_verification.py`：13 passed /6.89s。

R3-A完整pytest（同一清凭据、临时数据根隔离脚本，无固定SOURCE_ROOT）：654 passed /91.17s，退出0。macOS15.8.1 arm64、conda-forge Python3.12.12。`git diff --check`通过。

### R3-B / P1-B：首次升级失败后的独立维护核实入口

TDD：`tests/test_round3_release.py`先10 failed（新维护模块/管理器命令不存在），实现后9 passed，另一个失败是测试捕获bound method时漏计self，修正测试。继续注入poll异常、结果落盘前崩溃，观察2条真实红灯；空.env fixture修复后转绿。聚焦42 passed /1.60s（本轮、R2发布、原发布器回归）。新`linux-ops verify-delivery RELEASE BATCH_ID REQUEST_ID SHA256`在已有维护态、无活动队列时停止登记旧容器，用verify过的固定新镜像、原compose配置/数据挂载，以`--entrypoint python`和`EXACTCUT_WORKER_ENABLED=0`执行一次`exactcut.maintenance_verify --once`；无端口、不启动web/worker，不依赖旧应用API。成功或异常均尝试拉起登记旧容器并检查健康，current版本与维护标记不变。宿主只用现有stdlib发布管理器。

维护模块复用原SHA审批、verification_binding、只读poll、不可变stage-result和原待核实job的严格证据关联；仅原record/material身份一致且ready才终结原历史。不造回执，不清除未知intent。暂未ready、身份冲突、读超时、磁盘不足都保留unverified；单次命令失败/未就绪退出非0，下一次显式命令再读。维护job从第一次保存就用unverified而非queued，避免中途崩溃后旧worker自动执行新阶段；本地结果落盘中断可再次只读核实，已封存ready证据只补终结历史，不重复远端查询。全程保留普通上传保险。因缺回执/不符证据不能排空时，拒绝恢复旧协议，不用虚假“核实”绕守卫。

测试证明完整路径：不同固定镜像ID的旧release不支持阶段/核实API→新release健康失败并产生真实带stage-result的unverified→旧release恢复且保持维护→管理器CLI使用新镜像的一次只读核实→phase_blockers为空→recover成功撤维护→再次正向publish成功。另覆盖无维护/旧核实镜像/活动任务停服前拒绝、查询失败恢复旧容器、维护CLI必须显式--once、磁盘拒绝可重试、结果落盘/历史终结中断可恢复。模拟外部客户端均无OSS、飞书创建/更新、ingest/resync。

操作单补充（**本会话未执行**，由编排者在干净已提交工程构建后执行）：首次升级前按现有安装流程先更新宿主发布管理器，镜像与应用仍通过build/publish切换。此命令使用封存新镜像而非旧容器API；新镜像web健康失败不妨碍单次Python核实进程，但若镜像/凭据本身不可用，保留维护和旧容器，先修复原因再重试，不删数据。

```sh
# CLEAN_REPO 是已提交、git status --porcelain为空的独立服务器工程，勿用生产目录开发。
sudo install -m 755 "$CLEAN_REPO/source/deploy/linux_ops.py" /srv/exactcut/bin/linux-ops
sudo /srv/exactcut/bin/linux-ops --root /srv/exactcut build "$NEW_VERSION" --repo "$CLEAN_REPO"
sudo /srv/exactcut/bin/linux-ops --root /srv/exactcut check-switch "$NEW_VERSION"
sudo /srv/exactcut/bin/linux-ops --root /srv/exactcut publish "$NEW_VERSION"
# 仅健康失败且maintenance仍保留时：从status列出的job、其batch publications取原BATCH_ID/REQUEST_ID/APPROVED_SHA256。
sudo /srv/exactcut/bin/linux-ops --root /srv/exactcut status > "$EVIDENCE/failed-upgrade-status.json"
sudo /srv/exactcut/bin/linux-ops --root /srv/exactcut verify-delivery "$NEW_VERSION" "$BATCH_ID" "$REQUEST_ID" "$APPROVED_SHA256" > "$EVIDENCE/maintenance-verification.json"
# 多记录逐一显式核实；未ready/读异常需稍后再次执行上一条，禁止重传。
sudo /srv/exactcut/bin/linux-ops --root /srv/exactcut status > "$EVIDENCE/drained-status.json"
# 必须确认phase_blockers和active_tasks均空。
sudo /srv/exactcut/bin/linux-ops --root /srv/exactcut recover
sudo /srv/exactcut/bin/linux-ops --root /srv/exactcut check-switch "$NEW_VERSION"
sudo /srv/exactcut/bin/linux-ops --root /srv/exactcut publish "$NEW_VERSION"
```

每次记录UTC时间、执行命令/退出码、管理器版本commit、executor_release/固定image/commit、current旧release、维护标记、jobs+stage-results+publications前后快照、history/*-verify-delivery.json、原record/material/SHA对应关系、只读查询次数和平台新增对象计数（必须0）、recover后维护解除、重新publish健康结果。原两用户并发/等待让位/重启/保守回退验收仍执行；14 CPU/24g、pids512不变。`-threads 4`仅编码器约束，继续用`ps -T`及NLWP采样每路ffmpeg实际线程数，不能把参数值当实际总线程证据。

R3-B本机完整pytest：672 passed /91.77s，退出0，同R3-A的macOS/Python和隔离方式。`git diff --check`通过。

R3-B中断窗口补充：维护命令首次保存意图但尚未落job时被终止，下一次命令也必须先执行已有recover_verification_queue。TDD外部临时测试先2 failed /18 passed（原fbbm_queued与旧扫描已改publish_failed两种），回归合入source/tests，再添加启动扫描；只补偿真正缺job，不解除持久等待保险。最终三次全量与干净clone针对含此补齐的source tree重新执行，之前675用例的预备运行不计为最终版本三连跑。

维护中断补齐后的本机全量：677 passed /92.92s，退出0（包含尚未提交的R3-C预览修复）；聚焦本轮维护+预览24 passed /3.14s。R3-B以本次amend纳入上述启动扫描与两条回归，最终干净clone还会核验提交后的完整source tree。
