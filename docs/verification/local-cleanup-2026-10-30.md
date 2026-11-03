# 2026-10-30 本地历史文件整理与 worktree 清理

本次为用户授权的本地整理；不发布线上、不修改业务代码、不停止 dev 服务。已封存的应用候选仍为 `ea3db64920ae4857f9c0cd7dda0dae054f4bf981` / `rc/20261030-ea3db64`。本次 main 后续提交仅包含文档和配图，不需要重建该应用候选。

## 文档归档

- `docs/design/2026-10-16-mosaic-tracking-research.md`：保留当时的技术调查，增加历史状态和当前实现边界，避免把早期接入建议当作当前架构。
- `docs/verification/production-smoke-2026-10-19.md`：保留一次合成样本生产冒烟记录，明确日期、验收范围及私有证据位置。
- `docs/素材工作台操作说明_2026-10-28.md` 和 `docs/images/manual-2026-10-28/`：保存历史界面操作手册及五张配图，核对本地链接和图片完整性。截图中的制作人姓名、文件名等已做遮挡；没有重新修改图片。
- 手册明确区分 10 月 28 日晚间已上线的原片可用性提示，以及 10 月 29–30 日仍未发布的校验定位、资源忙恢复、中文字幕预览。

## 两组旧代码快照

| 原 worktree | 归档分支 | 快照提交 | 处理 |
|---|---|---|---|
| `mosaic-sam2` | `archive/mosaic-sam2-scratch-20261030` | `00a8730ae58d96daa41e552931a9ec96cab95fa5` | 保存 10 月 16 日早期 SAM2 草稿、配置、接口及测试；不作为当前功能合入 main。 |
| `restore-stash-1465454` | `archive/restored-cutting-20261012` | `236d1999d44718da60f810049f4fe9311b2d87fd` | 从 detached HEAD 建立分支，保存已暂存的 10 月 12 日剪切、扫描和 review demo 恢复内容。 |

各归档根目录含 `ARCHIVE_STATUS.md`，说明原基线、用途及恢复方式。源码按发现时状态保存，没有为归档改写旧实现，也没有宣称旧实验通过当前测试或可以上线。原有功能分支和六条 stash 均保留。

## 备份与移除条件

私有恢复目录：`~/.config/superpowers/cleanup-archives/exactcut-20261030-190609/`，权限 0700。包含：

- 每个待清理 worktree 的原 HEAD、索引补丁、工作区补丁、未跟踪源码原件；
- 非缓存忽略文件的 `private-data.tar.gz`，主要为历史日志、预留状态和锁文件；
- 每个备份文件的 SHA256 及 tar 内容校验结果；
- 含 main 基线与两个完整归档分支的 `recovery.bundle`；
- 失效 diagnostics 登记的 Git 元数据、索引补丁及独立恢复 bundle；
- `manifest.json`，记录原路径、提交、占用检查、备份内容及实际移除结果。

检查所有进程的打开路径，并在每个目录移除前再次执行 `lsof +D`。确认无进程占用、无新增未提交源码、无新增非缓存数据、备份哈希一致后，使用普通 `git worktree remove` 移除；未使用强制删除。`__pycache__`、pytest/build 等可再生缓存不作唯一数据备份。

已移除 12 个无占用的历史目录：

- `/private/tmp/exactcut-editor-oss-0a3825b`
- `/private/tmp/exactcut-originals-7960491`
- `admin-switch`、`feishu-read-retry`、`mosaic-sam2`
- `oss-delivery`、`preview-project-fix`、`project-init`
- `release-all-20261012`、`render-acceleration`
- `restore-stash-1465454`、`review-all-20261013`

全局工作树前缀为 `~/.config/superpowers/worktrees/exact-cut-fbbm-handoff/`。目录移除不删除分支或提交。

另清理一条已丢失目录的 diagnostics acceptance 登记：原 HEAD `76c8759bde364550eba67de47cf00a7bf9b41cbe` 已固定到 `archive/diagnostics-acceptance-20261012`。其索引没有额外暂存差异；先保存 Git 管理元数据并验证独立 bundle，再执行 worktree prune。未声称恢复本来已经不存在的目录数据。

## 保留的八个 worktree

| worktree | 原因 |
|---|---|
| 主目录 main | 日常开发与现有服务使用。 |
| `mosaic-gemini` | 8801 使用。 |
| `preview-subtitle-demo` | 8810 使用。 |
| `preview-subtitles` | 8821 使用。 |
| `release-20261030` | 8822 集成复测使用，保留封存候选基线。 |
| `source-frame-picker`、`visual-frame-picker` | 明确暂缓开发的独立工作区，保留后续续作入口。 |
| Paseo 的 `fix-editor-busy-retry` | 检查时仍有进程使用，保留应用管理的目录。 |

8793、8800、8801、8810、8820、8821、8822 的进程和数据均保留。本轮不改发布控制器、运行配置、密钥或服务器版本。

## 恢复方式

源码可以在原仓库通过相应分支重新创建 worktree，例如：

```bash
git worktree add /path/to/restored-sam2 archive/mosaic-sam2-scratch-20261030
git worktree add /path/to/restored-cutting archive/restored-cutting-20261012
```

其他已清理目录可按 manifest 中的原分支或提交重新检出。若需要历史日志/状态，先检查对应的私有 tar 内容，再解到独立恢复目录；不要覆盖正在运行的 dev 或生产数据。Git 仓库本身丢失时，可从私有 `recovery.bundle` 克隆源码，并按需导入独立 diagnostics bundle。

验证仅针对本次整理所需的源码可恢复性、文档链接、图片完整性、Git 状态和运行服务。应用源码树与候选 ea3db64 保持一致，沿用已封存候选的本地/Linux 测试结果；不为文档整理重复执行整套业务测试。
