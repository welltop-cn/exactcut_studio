# 2026-11-07 生产发布：main 824cfb5

## 授权与范围

- 用户明确要求发布上线，并核对代码是否已合入。
- 管理器记录完成时间：2026-11-07 12:09:54 北京时间（04:09:54 UTC）。
- 类型：应用代码发布。使用已验证、已封存候选；不覆盖生产运行配置或凭据，不执行 FBBM 上传、生成或替换用户成片。

## 变更前后

- 原 release：`us-east-20261031-bottom-subtitles-8216c27`。
- 新 release：`us-east-20261107-main-824cfb5`。
- 应用 commit：`824cfb56cc05d5ebb40dcb006c6b67de6d67fa12`。
- 新镜像：`sha256:6d6bddec7234faa4ab4998b057abd5d271a988f5a16a40d47b8c14986a837c84`。
- 经发布管理器重启切换应用容器。runtime.json、production.env、deployment.json 发布前后 SHA256 完全相同，保留 AI 60 RPM / burst 5 和现有凭据。
- 没有操作监控脚本、crontab、账号库或媒体文件。

## 执行与回退

发布前再次只读 `check-switch`，`allowed=true`、活动任务与阶段交付阻塞均为空；随后使用候选自身的管理器执行：

```sh
python3 /srv/exactcut/preparation/release-824cfb5/repo/source/deploy/linux_ops.py \
  --root /srv/exactcut publish us-east-20261107-main-824cfb5
```

管理器退出码 0，耗时 4.94 秒，切换记录 status=ok；完成后维护状态已解除。第一次外围日志脚本因 shell 引号丢失报 SyntaxError，尚未调用管理器、生产未变；改为上传完整脚本后执行成功。失败脚本和日志也保留。

- 私有证据与配置备份：`/srv/exactcut/preparation/release-824cfb5/publish/`。
- 目录权限 0700，三份配置备份权限 0600；不在 Git 保存凭据正文。
- `before-current.json`、`publish.log`、`result.json`、`after.json` 保留切换前后证据。
- 旧镜像和旧封存 release 保留，未执行清理。
- 回退必须用同一候选管理器先检查目标旧版本的协议与活动任务，再执行 `rollback us-east-20261031-bottom-subtitles-8216c27`。如果新任务、编辑数据或交付协议阻止回退，按管理器诊断排空或恢复兼容版本，不能绕过守卫或恢复旧业务数据库。本轮未执行回退。

## 验证与限制

发布前：本地完整回归 2555 passed / 10 skipped；Linux 标准完整 2148 passed / 417 skipped；独立 Linux Chromium 补验 668 passed / 9 skipped。均无失败，详见[修复及完整验收](../../verification/release-blocker-fixes-824cfb5-2026-11-07.md)。

发布后实读：

- 后端与公网 `/home/healthz`：新 release、production、status=ok、worker_alive=true、maintenance=false。
- 公网 `/home/`、认证 `/api/me`、项目侧栏和既有项目详情均 HTTP 200。
- 页面引用的 8 个 JS 资源均 HTTP 200，内容 SHA256 与固定候选源码一致。
- 抽取已有成片执行带认证的 1 KiB Range 请求：HTTP 206、video/mp4、Content-Range 正确。没有生成新结果或触发替换。
- 第一次抽取的历史项目已无 preview，因此改选侧栏中的现有成片；验收脚本曾漏加 `/home` 前缀而 404，按前端实际地址规则修正后通过。这两项属于冒烟脚本问题，没有修改应用或项目数据。

公网证据：`/Users/matao/exactcut-test/release-prep-824cfb5/production-smoke.json`。验证为 HTTP/资源/媒体读取，不冒充浏览器手工全清单或真实新制作验收；没有执行上游 AI、FBBM 交付或完整生产混合负载测试。

## main 同步

发布前已将 main 推送到 `1a7b4be`，其中应用代码与 `824cfb5` 一致，后续提交仅为验收文档。本轮发布记录另行提交推送。代码修复均在 main，工作区不携带未提交源码；历史 WIP 备份分支和 stash 未混入发布。生产配置本轮没有变化，无新增配置同步债务。
