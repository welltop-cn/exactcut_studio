# 同项目多素材并发实施记录

规范：`intra-project-concurrency-plan.md`；基线 `3509a4f`，数据 schema 1。

## 环境与隔离

2026-10-10，macOS 15.8.1 arm64，Python 3.12.12（conda-forge）。全部 Python 使用 `/Users/matao/miniconda3/envs/py3.12/bin/python3.12`。

在 `source/` 执行 `/Users/matao/miniconda3/envs/py3.12/bin/python3.12 /tmp/exactcut-review-tests.py`：包装器为每次运行建立独立 `TemporaryDirectory` 数据根，设置 `EXACTCUT_ENVIRONMENT=development`，清除 EXACTCUT/OPENAI/ALIYUN/FEISHU/CF_ADS/GEMINI/GOOGLE_API 凭据变量；调用 `python -m pytest -o addopts= -q`。外部客户端均模拟，无真实上传。基线 **676 passed，97.59s**。

按组执行 RED → 最小实现 → GREEN → 审查，最后完整 pytest 与已提交版本干净 clone 验证。保留用户原有两个文档和三个前端文件的未提交改动；只按路径暂存自己文件。不部署、不触碰生产。

## 组 1：同剧锁收紧（测试先行）

- `test_intra_project_locks.py`：先确认 **7 failed，4.37s**，覆盖共享读取时缓存命中、忙锁立即等待、同选段不同 Hook 并行、采样锁外、私有副本抵抗缓存清理。
- 最小实现：缓存先判断就绪；缺失才尝试非阻塞独占锁；锁忙抛 `ResourceDeferred`。基底检查/生成/发布及复制到请求私有工作文件在锁内，采样和 Hook 在锁外。
- 规范审查指出无锁快路径遇到清理时的 `is_file/stat` 竞态。先补确定性删除测试，确认 **1 failed**（FileNotFoundError），再将消失视为未命中并在锁内重新检查。
- GREEN：锁/正片缓存/Hook/磁盘/纯剪/强调回归 **74 passed，9.88s**；规范复核 **8 passed**；独立质量审查 **38 passed，6.46s**，通过。
- 预取流程的等待异常传播随阶段集成组补齐，避免转换成永久失败。

## 后续分组

2. 每素材结果存储、旧整批兼容、执行器/进度隔离；素材输入、产物证据和磁盘预算隔离。
3. request 围栏、fetch 扇出、render 单项目容量、恢复/核实/确认隔离；生产模板 render=4、per_project=2。
4. 完整集成与兼容回退验证、干净 clone、Linux 验收说明。

## 回退

设置 `concurrency.per_project=1` 降低单项目 render 并发；设置 `concurrency.enabled=false` 全局保守串行。无需还原数据，新代码合并读取旧 `production/results.json` 与每素材文件。配置降低时已驻留（含暂停）进程仍计入容量，不再准入超额工作；应先排空再观察串行回退。若回退到不识别阶段/分片的旧代码，必须遵守已有 release 管理器的阶段排空守卫，不直接切换。

## 组 2：素材结果与执行状态（测试先行）

- 先写 `test_intra_project_results.py`，初始 **15 failed**：缺分片存储、素材执行器互相覆盖、状态无法汇总、render 不接受素材 ID。再实现 `result_store` 的逐素材 flock/原子文件替换、旧结果合并和分片优先。
- 单素材执行只解析/校验自己的时间线、来源、结果、产物证据和磁盘预算；通过 `execute_batch(request_ids=[rid])` 渲染。新素材执行不写旧整批 results 文件；旧整批 CLI 仍兼容，并同步已存在的分片。
- 额外 RED：旧执行器验证全部素材（旧代码并发测试失败）；证据来源错误包含兄弟 EP2；成功素材继承兄弟失败返回 2；预取吞等待异常；历史 render/fetch 结束时间遮蔽活 upload/poll 心跳；最后一个素材写 production_complete 覆盖兄弟；空列表选择 IndexError；partial 下活/延迟排队兄弟允许再次提交整批；素材开始与旧 fetch 结束时间混配。均在最小修复后转绿。
- 每条素材结果保存自己的进度/心跳/执行起止；批次 runtime 只读汇总任意素材执行器、素材状态和 production_complete。`publications.json` 结构及交付保险不变。新 optional `request_parallel` 标记使新批次派生状态；旧批次仍保留原 runtime 兼容。
- 正片锁忙时预取持久化 waiting_resource/not_before 并传播等待，CLI 经 run_child 返回 75，不转永久失败。
- 相关结果/预取/工作流回归 **64 passed，16.99s**；加入进度兼容边界后定向 **34 passed，2.32s**。独立规范审查通过；质量审查与最终全量结果见下方最终验证。

## 组 3：素材调度与容量（测试先行）

- 初始 RED：缺 per_project 默认/非法值验证；同批两个 render 加 upload/poll 只准入一个；fetch 只生成无 request_id 的整批 render；恢复只 SIGCONT 一个；6 条素材只启动一条、两个项目合计只有两条；兄弟心跳/交付误触确认 409，目标分片变化未触 409，核实被兄弟任务阻挡。
- 最小实现：素材围栏 `(batch_id, request_id)`；旧无 ID 的任务仍占整批围栏；fetch 为每条素材创建确定性 UUID 后继，恢复补齐半落盘扇出；证据等待只围住本素材，退出/失败/崩溃只清本素材执行器。
- per_project 仅限制 render（暂停进程仍驻留计数），默认 1，非法值拒绝；全局 lane/总量、owner 公平轮转保留。生产模板 render=4、per_project=2；CPU 14、内存 24g、pids512不变。最近已批准的全局缺省规则不变：无 concurrency 段为 1；有段且缺总量则 lane 之和。
- 确认锁外校验仍保留，但只围住目标结果/交付和确认、归属、暂停/归档等有效标记；兄弟的心跳、进程列表和分片写入不使本素材误拒。核实提交仅检查目标素材或无 request_id 的旧整批任务。
- 集成覆盖 6 素材恰好2渲染、双项目4渲染、同项目跨素材 upload/poll 同时运行、一条失败其余完成、死进程按素材重排且活兄弟不动、延迟 poll 与暂停容量、旧任务及禁用并发回退。
- 实现者相关组 **224 passed，29.82s**；独立规范审查 **81 passed，9.83s**，通过。

## 可复现的本机全量命令

在仓库 `source/` 下执行以下脚本；每次只使用新的临时数据根，不继承发布/下载/模型凭据。已有测试逐项使用 fixtures 模拟外部客户端。

```sh
/Users/matao/miniconda3/envs/py3.12/bin/python3.12 - <<'PY'
import os, subprocess, tempfile
prefixes = ('EXACTCUT_', 'OPENAI_', 'ALIYUN_', 'FEISHU_', 'CF_ADS_', 'GEMINI_', 'GOOGLE_API_')
with tempfile.TemporaryDirectory(prefix='exactcut-intra-tests-') as root:
    env = {k: v for k, v in os.environ.items() if not k.startswith(prefixes)}
    env.update(EXACTCUT_DATA_ROOT=root, EXACTCUT_ENVIRONMENT='development')
    raise SystemExit(subprocess.call([
        '/Users/matao/miniconda3/envs/py3.12/bin/python3.12', '-m', 'pytest',
        '-o', 'addopts=', '-q'], env=env))
PY
```

干净 clone 验证：`git clone --no-hardlinks <本开发仓库> <新的临时目录>/repository`，detach 到已提交 HEAD，在 clone 的 source/ 下执行上面命令（额外设置 PYTHONPATH 为 clone/source，核对 exactcut.__file__ 位于该 clone）；运行前后 `git status --porcelain` 都应为空。禁止复制开发工作区三个前端的未提交改动进入 clone。

## Linux 容器：由编排者在 build 阶段执行

本机未执行 Linux 容器回归。按本轮 Grok 复审安排，由编排者在服务器 build 阶段执行同一套容器完整 pytest，使用生产 Python 3.11，并补录下方证据字段；本会话不连接服务器、不部署。下方保留隔离容器操作单供验收参考。

本机 `/usr/local/bin/docker info --format '{{.ServerVersion}}'` 失败：`unix:///Users/matao/.docker/run/docker.sock` 不存在；未发现 colima/podman/container。未连接任何服务器补测，不能将 macOS 绿灯当成 Linux 证据。待本地 Docker daemon 可用后，以下命令仅在临时 clean clone 执行，容器无网络、无凭据、无生产挂载：

```sh
# 在已提交 clean clone 根目录，确认 git status --porcelain 无输出。
docker build -f source/Dockerfile -t exactcut-intra-test .
# 用临时验收镜像补 Node（JS 契约用例需要），不修改产品 Dockerfile。
docker build -t exactcut-intra-test-tools - <<'DOCKER'
FROM exactcut-intra-test
RUN apt-get update && apt-get install -y --no-install-recommends nodejs && rm -rf /var/lib/apt/lists/*
DOCKER
docker run --rm --network none --entrypoint python \
  --tmpfs /tmp:rw,exec,size=4g \
  -v "$PWD:/workspace:ro" -w /workspace/source \
  -e PYTHONPATH=/workspace/source \
  -e EXACTCUT_DATA_ROOT=/tmp/exactcut-intra-data \
  -e EXACTCUT_ENVIRONMENT=development \
  exactcut-intra-test-tools -m pytest -o addopts= -q
```

证据字段：UTC 时间、Git SHA、镜像 ID、`python --version`、`ffmpeg -version`、完整 pytest 输出/退出码、跳过项理由、clone 前后 git status。基础镜像使用现有 Python 3.11（生产版本兼容性），本机执行仍只使用指定 Python 3.12。本轮不作生产验收；完成后交 Grok 复审。

## 审查修正与最终验证

- 结果组独立质量审查指出两项旧整批兼容边界：旧已退出分片覆盖较新的批次进度；put 永久保留非活分片起止时间。先加两条确定性回归，确认 **2 failed**，再只保留活执行器元数据，并比较批次/素材开始时间选择观察对象。相关组 **58 passed，4.83s**；质量复核 **73 passed**，批准。
- 调度组质量复核 **81 passed + 42 passed**，无未决代码问题；三组均完成先规范、再质量的独立审查。
- 第一轮全量 **740 passed，104.22s**；最终新增边界回归后的全量和干净 clone 结果将补录如下。所有阶段均用上述凭据清理/临时数据根包装器。
- 跨项目 runbook 已更新本次证据支持的缓存消失、私有副本、预取等待传播和观察时间边界；明确只在 macOS fixtures 验证。

最终开发工作区全量：**743 passed，104.71s，退出码 0**（2026-10-10）；`git diff --check` 通过。干净 clone 验证在全部实现提交后执行。

整体独立复核通过，无阻断遗漏；四个本轮新增测试文件 **67 passed，7.88s**。实现分组提交：锁收紧 `562a769`、素材结果/进度 `c67dbf5`、随后素材调度/生产容量组；所有提交仅显式暂存本任务文件。

干净 clone 完整 pytest：**743 passed，105.67s，退出码 0**。验证提交 `fa027742488646b785e51dac5198d28a5a6c7818`，source tree `82eb1fd954f9381a1dcfafa4df8efc044013c946`；实际导入来自 clone/source/exactcut。本地 `git clone --no-hardlinks`、detached HEAD、新临时数据根、清除凭据变量；clone 运行前后 git status 均为空。最后补录此证据为纯文档提交，source tree 与已测版本完全相同。

调度组提交 `fa02774`。开发工作区最终只保留用户原有两个文档及三个前端文件的改动，本轮提交列表不包含这些文件；`git diff --check` 通过。没有部署、没有真实 OSS/飞书/FBBM 请求。

未决项：Linux 容器完整回归因本机 daemon 不可用仍**未执行**；实际生产线程数/性能与业务验收仍**未执行**，待 Grok 复审和编排者后续验收。本轮无已知未解决代码审查阻断项。现停止实施，等待复审。

## 本机并发完成汇总修复（2026-10-10，测试先行）

用户在本机 8792 发现批次 A `47fea566799947d9a84de77e72aaa449` 的 6 份结果全部 `qc_passed`，但持久文案为“制作结束，5 条可预览，0 条失败”。本轮只读核对 `source/.web-state` 确认上述不一致；B/C 当前均为 6 份通过、文案 6。没有修改业务数据，没有请求运行中应用或重启/部署服务。

根因：每个 `_produce` 用自己的返回结果快照写整批计数；只读 runtime 的派生规则又会在只有部分素材完成时给出 review/partial，且不会修正持久文案。最后结束者持有过期快照，最终状态不会自动纠正。

TDD 证据：

- 先新增汇总回归，初始 **11 failed，0.91s**。用两个真实 `workflow.render` 调用、线程池和 Event 固定交错顺序：第一个写入分片并保留仅含自己的结果，第二个完成后再释放第一个；两种顺序均确定复现最终“1 条”而非“2 条”。不依赖 sleep、真实时间推进或外部客户端。
- 再补持久 render job 与 6 素材计数回归，确认 **2 failed**；另补“旧批次残留 render phase + 活 upload”回归，确认失败后才修复原始/派生 executor 标记混用。新增文件最终共 **14 条**回归。
- 最小实现：批次 `runtime.lock` 内重读全部确认素材分片（保留旧整批 results 兼容），统一 `production_summary` 供写入和读取使用。局部返回结果仅决定本 render 退出码；素材执行器退出及调度收割时重新持久化汇总。新增可选 `execution_phase` 区分 render/upload/poll，保持 schema 1。
- 缺失、processing、waiting_resource 分片以及仍驻留/排队（含 not_before）的 render 都阻止完成；全部素材终态才置 `production_complete=True`，根据成功/失败与交付状态派生 review/partial/publishing/complete。失败素材也是制作终态。显式 failed/interrupted 且无待执行 render 时保留恢复错误，避免遮蔽故障。upload/poll 不阻挡已完成制作；未确认的残留分片不计数。
- 按本轮用户要求，修正原计划第 4/8 节的中间态：仍有兄弟素材未完成时统一 producing/False，全部制作终态后才 review/partial。更新两处旧期望，并保留失败隔离、交付保险和恢复回归。

定向验证（凭据清理、独立临时数据根，命令在 `source/`）：

```sh
/Users/matao/miniconda3/envs/py3.12/bin/python3.12 /tmp/exactcut-review-tests.py \
  tests/test_batch_production_summary.py tests/test_intra_project_results.py \
  tests/test_intra_project_scheduler.py tests/test_round1_integration.py \
  tests/test_workflow.py tests/test_phase4_observations.py
```

结果：**128 passed，14.21s，退出码 0**。完整本机与干净 clone 证据在下方补录；可复现隔离脚本见本文“可复现的本机全量命令”。环境仍为 macOS 15.8.1 arm64 / Python 3.12.12。三个前端文件及用户其他未提交改动保持原样，不进入本轮提交。

本机全量：在 `source/` 执行 `/Users/matao/miniconda3/envs/py3.12/bin/python3.12 /tmp/exactcut-review-tests.py`，**757 passed，106.66s，退出码 0**；每轮新临时数据根、清除凭据变量，外部客户端模拟。`git diff --check` 通过。

干净 clone 全量：执行 `/Users/matao/miniconda3/envs/py3.12/bin/python3.12 /tmp/exactcut-clean-review.py`，**757 passed，104.33s，退出码 0**。验证提交 `c244be64f4d4b4864639f285b937ac508232ac91`，source tree `a891fd9c1d0f68675802f3477af88dfb1ccca4d7`；本地 `git clone --no-hardlinks` 后 detach 到该提交，核对导入来自 clone/source，运行前后 git status 均为空。命令内部使用本文全量命令所述的固定 Python、凭据清理、新临时数据根及 `-m pytest -o addopts= -q`。本次补录为纯文档提交，source tree 保持与已测版本一致。

回退：优先将 `per_project=1`，并排空已启动 render；不恢复旧数据。旧错误文案可由新代码读取及后续正常状态写入纠正。本轮不直接修写现有 `.web-state`，不部署；实现及测试交复审。

## Grok 复审 P1：超过项目上限的暂停 render 恢复（2026-10-10，测试先行）

核对代码确认：`batch_control.reconcile` 原本将所有驻留 render（包括 T）计入项目恢复名额，使用 `> per_project` 拒绝且恢复成功不递增；旧无 request_id 的 render 又绕过此条件。降低 per_project 或残留暂停任务超额时，会全部停住。

先在 `test_intra_project_scheduler.py` 补 6 个确定性用例，以 FakeProcessTable 和模拟 killpg 驱动真实 reconcile/持久 control 文件，不发真实信号、不依赖 sleep/时间推进。RED 命令：

```sh
# cwd: source/
/Users/matao/miniconda3/envs/py3.12/bin/python3.12 /tmp/exactcut-review-tests.py \
  tests/test_intra_project_scheduler.py -k reconcile
```

RED：**5 failed，2 passed，30 deselected，0.52s**。上限2/3个T实际拉起0（期望2）；上限1/2个T实际拉起0（期望1）。已有“2 render + 1 upload 全部继续”通过；旧整批优先的一个兼容用例也通过。其余失败覆盖已有S占位、素材/旧整批FIFO恢复及SIGCONT前退出。

最小实现只改恢复计数：项目 render 名额仅统计非T且身份有效的非僵尸进程；恢复前 `>= per_project` 跳过；SIGCONT成功后才递增。无 request_id 的旧 render 使用同一上限，并保留整批围栏。全局 lane/resident 仍包括T，容量与身份校验、暂停/归档、owner轮转保持既有行为；无 schema 变更。

GREEN 定向命令：

```sh
/Users/matao/miniconda3/envs/py3.12/bin/python3.12 /tmp/exactcut-review-tests.py \
  tests/test_intra_project_scheduler.py tests/test_batch_control.py tests/test_concurrency.py \
  tests/test_round1_integration.py tests/test_step3_resources.py
```

结果：**133 passed，13.43s，退出码0**。覆盖一次恰好恢复2/1、满额重复tick不继续、一个退出后恢复最后一个、活render占位、SIGCONT失败不占位、旧无ID与素材任务共享上限/围栏；原2render+1upload回归保持通过。

环境及隔离沿用本文全量操作单：指定 Python 3.12.12 / macOS 15.8.1 arm64，每轮独立临时数据根，清除凭据，模拟外部操作。不部署，不改本机业务记录，三个前端和其他用户改动不暂存。完整本机及已提交版本的干净clone结果随后补录。

本机完整 pytest：在 `source/` 执行 `/Users/matao/miniconda3/envs/py3.12/bin/python3.12 /tmp/exactcut-review-tests.py`，**763 passed，104.45s，退出码0**；`git diff --check` 通过。

干净 clone 完整 pytest：执行 `/Users/matao/miniconda3/envs/py3.12/bin/python3.12 /tmp/exactcut-clean-review.py`，**763 passed，106.17s，退出码0**。验证提交 `62a7d1495a4c7ba92382010093cfda6cf318ef75`，source tree `f51ae8c79a6bb62df9f1598ee57b4f9d04c96ec9`；本地 `--no-hardlinks` clone、detached HEAD、独立临时数据根、凭据清理及 clone/source 导入检查均通过，运行前后 clone 的 git status 为空。最后补录为纯文档提交，source tree 与已测版本一致。

本轮无已知未解决代码问题；Linux 容器全量/生产 Python 3.11 **由编排者在 build 阶段执行，本会话未执行**。只显式暂存本轮3个文件，不含三个前端文件或用户其他改动。未部署；完成后停止，等待复审/终验。
