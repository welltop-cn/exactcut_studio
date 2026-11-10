# 2026-11-10 Hook 预览与连续上传修复：正式发布

## 授权与版本

用户授权确认修复已合入 main、发布上线，并使用线上测试账号复制已有配置验收；另明确要求测试代码不进入正式运行环境。沿用此前“不真实上传”的约束。

- main 与 origin/main 发布候选：`e53c40b7512463ef5894e5fa87b75985544687b1`，包含已独立审查的 Hook 与上传修复及验收文档；发布前工作区干净，正常快进推送，无强推。
- 前版：`us-east-20261108-evening-aee6fe9`，commit `aee6fe9c80fcea3723ba81d98cc3c30bcf32d2bb`。
- 新版：`us-east-20261110-hook-publish-e53c40b`。
- 正式镜像：`sha256:d61fd94b6516b6da59b4e85712ca8235a499e9907e0d1c41824f0c11ef9e4d04`。
- 北京时间 21:41:51 开始，65.39 秒后发布完成，退出码 0。发布后健康正常、worker 存活、maintenance=false。

## 发布门禁

| 验证 | 通过 | 跳过 | 耗时 |
| --- | ---: | ---: | ---: |
| 本机完整 unit + integration + UI | 2922 | 10 | 839.75 秒 |
| Linux 发布管理器完整回归 | 2502 | 430 | 818.24 秒 |
| Linux Chromium UI 补验 | 407 | 8 | 177.30 秒 |
| Linux 真实浏览器 HTTP 集成 | 7 | 0 | 19.40 秒 |

各批次退出码均为 0，不将重叠批次相加。Linux 标准验证镜像没有 Chromium，跳过不计作通过。本机和 Linux 全量分别有 10 条已有多线程 fork 的 DeprecationWarning。此前本地 32 个专项 Chrome 场景见[本地验收](hook-publish-dev-chrome-2026-11-10.md)。

补验首轮漏设 `EXACTCUT_WORKER_ENABLED=1`，验证镜像默认关闭 worker，导致纳管 save 返回 503，3 个用例随后等不到错误字段焦点。相同条件下线上旧版本 aee6fe9 也有同样 3 个失败。修正隔离容器启动参数后重跑 UI 与 HTTP 补验，均通过；未因此修改产品代码。失败日志保留，未计作成功。

## 正式镜像与配置核验

Dockerfile 使用白名单复制应用、配置、模板和必要集成脚本；回归只在独立验证镜像执行。正式镜像及上线后的容器均实查：没有 source/tests、chrome_matrix.py、sitecustomize.py、本地 accounts.sqlite3，也没有 pytest。Chrome 驱动、模拟回执、故障注入和测试数据未进入正式镜像，未向线上应用添加任何替身。

上线后 app.js、material_versions.py、hook_preview.py 的 SHA256 与已验证候选逐个相同。生产 production.env、runtime.json、deployment.json 和 nginx 配置的前后 SHA256 相同；不输出密钥或认证头。发布前 check-switch 允许切换，活动任务与 phase blockers 均为空；管理器执行维护、正常停服、镜像切换和健康检查，未强杀业务任务。

## 线上业务验收

使用已有 producer 账号 `media_accept_a_1011`（素材版本验收A）。通过应用现有 accounts.login 生成该账号会话，未改密码、角色或其他用户权限；本机 Chrome 使用 HttpOnly/Secure cookie。复制原项目的配置，不复制原任务、成片版本、预览凭证或交付历史。

| 剧 ID | 原项目 | 新测试项目 | 素材数 |
| --- | --- | --- | ---: |
| 101689 | 3c4dde256f084cd2b1a836c090b726a9 | 64b5906d5df84358b21527c167f5ac78 | 2 |
| 101708 | dfcaf0baec744a63897b1fe6a1b22cd0 | 747fdc24de7d46158444e54be0fe91eb | 2 |

新项目首次保存需要建立项目帧索引，等待后校验通过；复用现有原片和基础剪辑缓存。两个项目的真实 Hook 预览完成后草稿不变、dirty=false，刷新后恢复预览也不置脏，均已通过 Chrome 确认制作。

最终业务检查通过：两个项目均 review，各 2 条成片；4 条均复用基础剪辑缓存，两个已预览素材的确认 manifest 均绑定 preview_key。两个项目首条视频在真实 Chrome 中通过直接预览流播放超过 0.5 秒，无媒体错误；时长分别为 190.055 秒、171.27 秒，540×960。勾选确认后两条上传按钮均可用，随后取消勾选，没有点击上传。服务端交付记录各为 0，原项目 media-state.json 前后 SHA256 均相同。

**约束偏差及处置：** 浏览器拦截没有覆盖制作后端的自动 OSS 上传。4 条制作完成时自动产生了 4 个预览与 4 个正式成片 OSS 对象，因此不能将本次描述为“完全没有真实上传”。发现后告知用户，并仅删除这两个新测试项目命名空间下的 8 个对象；删除前核对账号归属、当前版本、无交付记录、精确 key、SHA256、字节数以及 OSS HEAD 元数据，并使用既有对象资源锁。逐个删除后 object_exists=false，最终 remaining_objects=0。保留项目、本地成片和历史回执元数据，不改生产全局配置、不清空已有业务数据。原项目状态未变。本次业务检查通过，但曾偏离不上传约束，记录中不抹去这一点。

线上验收没有发起 publish 或 verify-delivery 请求。本机浏览器拦截 preview-url，按现有非 OSS 响应契约改用已鉴权的直接 preview 流，避免**浏览器调用** OSS 准备接口；该措施未阻止上述制作后端自动上传。这仅验证直接视频流播放，不宣称真实 FBBM/飞书交付通过。连续上传事务与故障重试已在发布前隔离环境测试；线上只验证上传按钮就绪及草稿状态。当前 Chrome 保留直接预览流，不代表以后重新进入正常线上页面时仍不会准备 OSS 文件。

## 回退与证据

前版封存目录和镜像保留，previous 指向 `us-east-20261108-evening-aee6fe9`。需要回退时先重新检查活动任务、协议及后来配置变更，再用候选 linux_ops.py 执行 `--root /srv/exactcut rollback us-east-20261108-evening-aee6fe9`；保留最新业务数据，不恢复旧数据库、不撤销远端素材。

私有证据目录：本机 `release-hook-publish-e53c40b`，服务器 `/srv/exactcut/preparation/hook-publish-e53c40b/`；包含全量日志、JUnit、Linux 构建/浏览器补验、前后状态与配置哈希、publish-result、运行镜像审计及 online-acceptance。会话仅存在私有文件/浏览器，不入 Git。

本记录属于发布后文档补充；后续仅文档提交不改变已发布的应用提交 e53c40b。
