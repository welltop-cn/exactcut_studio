# 多会话整合发布验收（2026-10-12）

> 历史归档（2026-11-04）。正文保留当时的方案或执行状态，不代表当前版本；日常操作见[文档入口](../../README.md)。

## 范围与冻结

用户授权统一整合、最终 Grok 整体审查后提交发布。相关会话均确认停止共享工作区写入/提交/发布/dev重启。线上基准为 us-east-20261012-combined-d544eae。

候选在独立 release/all-sessions-20261012 工作区，以91a8517为基线，包含：

- 30250a9：当前版本全部交付后项目complete，隐藏已入库确认框。
- 76c8759：字幕预览独立于后台制作名额。
- 91a8517：浏览器操作、截图、HTTP和后端诊断关联及限流。
- 冻结主工作区18个修改/新增文件：静态片尾none/last_frame/image、图像快照、时长QC、编辑器分区/折叠提示及验收记录。
- fix/feishu-read-retry至5fc8f14：临时GET失败有限重试，原回执只读自动恢复和准确提示。
- OSS HEAD校验、短Hook加速、encoder_threads默认4和项目初始化修复均已在基线中，未重复引入。
- perf/short-hook末笔b2edd68仅是两条长样本通过的记录，主分支已有更完整等价证据，保留主分支文档。
- 诊断验收临时worktree六个修改文件逐字节与主工作区一致，无独立遗漏改动。

不包含dev账号/业务数据、视频、私有配置、日志或凭据。schema仍为1。

## 整合验证

- 本机指定Python完整回归：1145 passed in 196.22s。涵盖真实FFmpeg静态片尾帧/静音/边界/缓存，以及调度、权限、并发、诊断和交付恢复测试。
- JavaScript app.js/diagnostics.js语法及git diff --check通过。
- 整合候选dev：127.0.0.1:8796，隔离数据/tmp/exactcut-all-dev-20261012；关闭真实外部写入/发布/AI/素材获取，仅模拟远端HTTP故障。
- 实际业务代码及网页：1255001连续故障显示自动重查，7轮、19次模拟GET、外部写入0；恢复后fbbm_ready，项目“全部已入库”，旧错误隐藏，预览确认框隐藏。成功后查询计数不再增长。
- 暂停状态、缺/冲突回执、永久错误和超时仍保留既定处理，相关回归包含于全量。

## 发布配置与回退计划

只将实际生产runtime.json的concurrency.hook_previews从1改3；render保持3、media.encoder_threads保持4，其他值保留。

一次性发布适配器在现有linux_ops.transition操作锁与维护屏障内、旧服务停止后、候选apply前保存runtime配置并更新这一字段。原版本发生漂移/配置值发生变化则拒绝。健康失败时，由同一发布管理器恢复旧镜像及原运行配置；不恢复业务数据库。适配器使用隔离假引擎验证成功切换和健康失败回退，2项通过。

最终发布记录见下文。

## 本机真实制作补充验收

整合代码使用独立 dev 8797，复制既有本地开发素材，通过真实页面确认、任务队列、FFmpeg 渲染和质检。测试项目 c59c0a15026c42d3903b779252c77287：定格片尾 11 秒、图片片尾 12 秒，均 qc_passed、1080×1920。抽取第 9 秒画面确认中英文文案完整可见；解码片尾音频非空且所有 PCM 样本为零。远端发布与写入关闭。最初环境缺少 .env 导致启动失败，补齐隔离开发配置后正常完成，未因此修改业务实现。

## 全分支及 worktree 对账

用户要求全部有效修改合入 main 并发布。2026-10-12 再次逐一核对：

| 分支/工作区 | 核对结果 |
|---|---|
| concurrency-refactor / 主工作区 | HEAD 91a8517；冻结 18 个文件 SHA256 全部未变，均纳入整合 |
| fix/admin-switch-performance | b30387f 已是基线祖先，全部包含 |
| fix/oss-delivery-verification | d544eae 已是基线祖先，全部包含 |
| linux-editor | 174ce5e 已是基线祖先，全部包含 |
| fix/managed-project-init | 18771e7 的补丁经 git cherry 验证已等价纳入，不重复覆盖 |
| perf/short-hook | 实现已包含；末笔 b2edd68 仅文档措辞，保留主线更完整长片验收及已发布记录 |
| fix/feishu-read-retry | 至 5fc8f14 的完整差异纳入，包含实现、回归、dev 证据 |
| detached diagnostics acceptance | 6 个未提交文件与整合候选逐字节一致，无遗漏 |
| release/all-sessions-20261012 | 汇总上述代码及 Grok 唯一问题修复、审查记录 |

私有环境目录、账号、视频、日志、凭据均不是发布代码，不纳入 Git 或镜像。所有旧分支和工作区保留供核对。最终合并还需验证所有分支 HEAD 都是 main 的祖先，并核对构建归档与 main 的树一致。

## 最终发布结果

- 所有本地分支 HEAD 均通过 `git merge-base --is-ancestor <branch> main`；主工作区切换到 main，代码提交干净。被冻结的未提交内容在纳入提交后，另保留私有 stash 与文件哈希备份。
- 发布代码：`14734fb5da56617e4fcba7dbf12db8e8bc151764`；本记录是发布后的文档补充，不修改发布代码。
- Grok 整体审查及补充复审均正常 end_turn 并通过；唯一已证实问题为片尾进度步骤缺失，已修复，回归先复现失败后通过。完整结论见 all-sessions-grok-review-2026-10-12.md。
- 最终本机全量：1145 passed in 183.87s；相关进度/编辑器回归14通过，发布配置适配器成功与回退2通过。
- Linux 隔离全量：1131 passed, 14 skipped in 225.44s；镜像通过发布管理器封存。
- 发布：`us-east-20261012-all-main-14734fb`，2026-10-12 20:05（Asia/Shanghai），由发布管理器切换成功。
- 镜像：`sha256:c331eac68d2d06b50bfa08e5af97fb58feea44746182b3c67a59fa2b30e04284`。
- 核对封存归档210个源码文件SHA256与main全部相同；运行容器镜像ID与发布记录相同。
- 运行配置与发布前备份逐字段比较，唯一差异 hook_previews:1→3。render仍为3；encoder_threads未显式配置，保持已验证代码默认4。
- 公网 `/home/healthz`：status=ok，release为新版本，maintenance=false，worker_alive=true。管理器status无活动任务、无阶段阻塞。
- 原任务2b49b3f61ee64c88bcaa889110bfdd12：两条素材最新交付均fbbm_ready；历史publish_failed记录保留。本次没有重传素材或改写业务记录。
