# Hook 与上传修复：父级独立审查

状态：两项修复通过父级源码审查、组合代码的全量 unit 与定向跨层验收，可合入本地主线。未发布生产。

## 审查范围与基线

基线 `868f570c9e759478b6305cfd4e17594a7f905e54`。两个实现会话使用独立 Git 工作树，父级单独审查产品 diff、相关测试和验收报告，并在 `fix/preview-publish-reviewed` 整合。未使用生产数据、凭据或外部交付服务。

大型仓库采用针对性审查：全部新增产品逻辑、变更测试、相邻事务/文件身份/前端异步边界。不是全仓安全审计。上传逻辑视为高风险，因为改变外部交付任务的接收条件。

## 上传 lane（498de9e，含 ef9d8a2 前置修改）

结论：本轮未发现阻断项，待与 Hook lane 组合验证。

- `_mutation` 仅内部 `delivery` 操作允许落后 revision；必须是真正整数且在 `[1, current_revision]` 范围，其他五类 mutation 沿用原严格 CAS。权限校验仍在 HTTP 入口。
- `queue_delivery` 保留项目/素材 gate、锁外文件 SHA 检查；锁内再核验当前资产和版本、record SHA、master 路径和 dev/ino/size/mtime/ctime，并用锁内版本生成交付和 outbox。
- 暂停、归档、删除、进行中/待核实交付、已完成重复和文件名绑定保护保留。hash 前记录文件身份，hash 后替换会拒绝；事务后的非协作文件写入仍由 worker 独立复制及 SHA 校验停止，不能把 stat 比对等同任意文件系统攻击防护。
- 同页提交按项目等待上一条回执；排队不等待上传完成。目标身份在点击时冻结，发送时再次检查导航、dirty、版本和交付状态。未知回执不自动 POST，只保存原 payload/token 供用户显式重试。
- 新 sessionStorage 完整请求保存仅限 managed publish；其他 API 沿用原幂等存储形式。普通轮询 busy 保护仍在，冲突同步需等待且确认应用，不能假称已同步。
- 父级静态审查了默认响应清理、5xx/解析失败、导航、dirty、其他在途写请求、同素材并发和幂等重放；没有发现放宽草稿 CAS、自动改目标版本或重复外部交付的新增路径。

调用范围：`_mutation` 6 类内部调用、`queue_delivery` 1 个 HTTP 路由；共享 `api` 37 个静态调用点、`refreshSelectedBatch` 3 处、提交队列 1 个点击入口（实现报告的静态计数，经对应调用路径审查，非动态覆盖率）。

父级独立运行（2026-11-10，upload 工作树 source 目录）：

```sh
/Users/matao/miniconda3/envs/py3.12/bin/python3.12 -m pytest -o addopts='--strict-markers' -m '' -q tests/test_delivery_revision.py tests/test_publish_conflict_resync.py tests/test_publish_session_ui.py tests/test_project_switch.py tests/test_managed_editor_review.py tests/test_material_versions.py tests/test_managed_completion.py tests/test_workflow.py tests/test_webserver.py
```

结果：**216 passed，3 warnings，16.36 秒**。警告为已有多线程进程中 fork 的弃用提示。包含公开持久事务、隔离 HTTP 权限与接收、Node 真函数和 Chromium 测试；不含真实 OSS/FBBM 交付。新同素材并发用例是线程测试，不能称为跨进程交付验收。

## Hook lane 首轮（651a6a1）

明确退回项：`bind_confirmed_previews()` 在确认入口全局 mutation/naming 锁范围内新增 `source_fingerprints()` 完整视频读取。多个素材虽共用同次 hash 缓存，首次大文件读取仍可能阻塞共享写操作；重复确认也会执行无用校验。不能仅以小视频回归通过接受此性能退化。

已要求：确认采用轻量有效性复核或锁外计算+锁内身份复核，renderer 的最终内容 SHA/输入/原片/镜头索引校验不变；补确定性无全量视频读取测试、确认后原片变化拒绝复用测试、重复确认不做昂贵工作测试。可选报告/cache损坏应跳过回退，不让派生数据损坏阻断制作。

前端仅移除派生凭证置脏、保留真实编辑上传保护；素材语义只排除 nested preview_key，不排除 seq/request_id；新的快照绑定仅发生于首次确认。以上方向正确，但等待返修结果与独立回归。

## Hook 返修审查（47c3070）

首轮阻断项已解决。父级重新阅读全部产品差异及新增边界测试：

- binder 不再调用 `source_fingerprints` 读取整片视频。确认阶段核对本批/本素材 report、请求/完整配置指纹、source/index stat 身份、缓存 JSON 摘要、输入 identity 与镜头索引。缓存里的 source SHA 在此用作已发布结果的一部分，**不是重新验证过的视频内容证明**；renderer 原有实际 source hash 比对负责最终判定复用。
- 确认后的来源替换不会修改冻结指针，renderer 会因内容 SHA 不符拒绝复用。新增真实渲染测试验证这一边界。
- 纳管 pending 筛选与 `_confirm.apply` 的两条跳过条件对应：queued/processing candidate 或已满足 desired_generation 的 current 不重新绑定。failed candidate 的上传恢复仍保留原 manifest；其他真正新建版本路径仍会绑定。并发快照变化受原 `_mutation` revision CAS 拒绝。
- legacy 只在没有 confirmed.json 时绑定；既有快照重试不重绑。schema 维持 1，不迁移历史视频/项目数据。
- 不合法可选 report/result/completed_at/cache identity/decision 被跳过；`approved_preview` 对非对象 cache/decision 增加保护。不能因可选派生文件格式损坏而阻断正常重新分析。
- no-video-read 测试用 Path.open 拦截真实视频路径，并在真实 mutation/naming 锁范围调用两类公开确认入口；不是耗时阈值测试。确认仍有原有 ffprobe/时长与 JSON/索引 I/O，不能声称确认无 I/O。
- 前端真实 dirty 上传保护没有移除，Hook 的展示更新不再污染草稿；只读/pending 控件遵循统一 controls。与上传 lane 的共享 app.js/material_versions.py 变更已自动合并并人工核对：两项职责均保留，无冲突标记。

本阶段没有剩余阻断项。旧草稿仅含 preview_key 而无本批有效报告时，新确认会重新分析；旧已确认快照仍按原凭证复用校验。这是有意兼容边界，不是数据迁移。

## 组合验收

整合分支 `fix/preview-publish-reviewed`，被测产品提交 `2578502`（随后提交本审查记录，不再改变产品代码）。已通过 `git diff --check`、`node --check`。

父级在整合工作树 `source/` 目录实际执行：

```sh
/Users/matao/miniconda3/envs/py3.12/bin/python3.12 -m pytest -o addopts=--strict-markers -m 'not ui and not integration' -q
# 1611 passed, 1 skipped, 1320 deselected in 153.44s

/Users/matao/miniconda3/envs/py3.12/bin/python3.12 -m pytest -o addopts=--strict-markers -m '' -q tests/test_delivery_revision.py tests/test_publish_conflict_resync.py tests/test_publish_session_ui.py tests/test_project_switch.py tests/test_managed_editor_review.py tests/test_material_versions.py tests/test_managed_completion.py tests/test_workflow.py tests/test_webserver.py tests/test_hook_preview_snapshot.py tests/test_hook_preview_confirmation.py tests/test_hook_preview_source_identity.py tests/test_managed_editor.py tests/test_phase2_hook_workflow.py tests/test_phase3_hook_sampling.py tests/test_hook_preview_draft_ui.py tests/test_editor_polling.py tests/test_source_frame_ui.py
# 308 passed, 3 warnings in 64.86s
```

两条命令退出码均为 0。组合验收覆盖源视频/真实 FFmpeg、预览生成及确认冻结、晚到结果、原片变化、可选坏缓存、真实浏览器异步结果、公开保存/制作/交付事务、HTTP 权限及并发幂等。3 条警告仍为原有 multiprocessing 多线程进程 fork 弃用提示。两组测试有重叠，不合计为不同用例总数。

父级最终结论：**本次范围通过审查，可合入本地；无需修改现有生产项目数据或重做已生成视频。** 没有留下已知阻断项。两条根因来自早期生命周期设计与后续异步预览/前端同步修改的组合，并非所有逻辑都是最近一周加入；具体提交见两个实现报告。

未做生产验收、未调用真实 OSS/FBBM；全量 unit 已运行，但未运行全仓所有 integration/UI，故不将本报告描述为生产发布验收。上线仍须走现有发布管理与发布门禁。