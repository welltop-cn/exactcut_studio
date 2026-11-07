# 首屏优化正式发布与线上验证（2026-11-07）

状态：已于北京时间 **2026-11-07 15:49:34** 正式发布，线上验收完成。

## 授权、候选与范围

用户明确要求合入 main、发布上线并在线上测试。优化提交 `a58a672` 已在 main，远端 main 与本地一致；固定发布候选为 `f40e59fe4c91e6743f97f497e537ecc2027b91fe`，后一个提交仅记录既有验收文档。

- 原版本：`us-east-20261107-main-824cfb5`。
- 新版本：`us-east-20261107-first-load-f40e59f`。
- 只发布本轮首屏依赖及工作台只读恢复优化；保留生产配置、凭据、账号、任务和媒体数据。
- 线上验收范围：首次打开、刷新、账号身份、侧栏与当前项目、无 hash 恢复、已有媒体读取。不会为性能测试制作新视频或执行 FBBM/飞书交付。
- 运维使用现有 root SSH 密钥和固定候选 `source/deploy/linux_ops.py`，无变更 SSH/sudo 权限。

## 发布前基线

同一真实 Chrome、同一项目 `bf401b953fb04aeaaef9292c43f4dd52`，取首个项目详情 Resource Timing 的 responseEnd（导航起算）。

| 样本 | 页面 load ms | workspace ms | sidebar ms | 详情 ms | 导航至详情完成 ms |
| --- | ---: | ---: | ---: | ---: | ---: |
| 首次进入 | 1449 | 3175 | 1025 | 2152 | 7585 |
| 刷新 1 | 515 | 3378 | 2119 | 7078 | 13059 |
| 刷新 2 | 685 | 1851 | 1030 | 2728 | 6260 |

三次中位 7585ms，范围 6260–13059ms。较早排查另见 21049/26618ms，不能只挑最慢历史值作为发布对比。测试时 Linux 隔离回归/构建正在运行；生产其他真实用户请求也可能存在，不是受控负载实验。

## 准备与证据

- 本机完整回归：独立 clone、指定 Python 3.12，清空默认 marker 过滤，隔离数据、关闭外部业务写入。
- Linux：发布管理器 build 固定源码、镜像与完整回归；另以相同候选源码执行 Chromium 及依赖浏览器夹具的补验。
- 发布前实读：active_tasks=[]、phase_blockers=[]、maintenance=false。
- 本地私有证据目录：`/Users/matao/exactcut-test/release-prep-first-load-f40e59f/`。
- 服务器准备目录：`/srv/exactcut/preparation/first-load-f40e59f/`。
- 原始浏览器基线：`before-1.json`、`before-2.json`、`before-3.json`。

## 完整回归

| 环境 | 结果 | 用时 |
| --- | --- | --- |
| 本机独立 clone，Python 3.12，清空默认 marker 过滤 | 2559 passed、10 skipped、10 warnings，退出码 0 | 772.25 秒 |
| Linux 发布管理器标准完整回归 | 2152 passed、417 skipped、10 warnings，退出码 0 | 724.49 秒 |
| Linux Chromium 及依赖浏览器夹具的补验 | 668 passed、9 skipped、6 warnings，退出码 0 | 237.01 秒 |

Linux 标准镜像缺浏览器依赖，因此以安装 Playwright 1.57.0 / Chromium 的独立镜像补验相同固定候选源码。9 个浏览器跳过属于既有隐藏/移除功能；不把这些跳过计作通过。补验与完整套件有重叠，不将计数相加当作独立测试总数。测试均隔离数据，关闭业务外部写入。

## 正式切换

使用已验证候选的管理器执行：

```sh
python3 /srv/exactcut/preparation/first-load-f40e59f/repo/source/deploy/linux_ops.py \
  --root /srv/exactcut publish us-east-20261107-first-load-f40e59f
```

- 切换前再次 check-switch：allowed=true、target_stage_protocol=true、active_tasks=[]、phase_blockers=[]。
- 管理器退出码 0，记录 status=ok，切换全过程约 65.55 秒；完成时间由 current.json 实际写入时间核对。未绕过管理器或手工删除状态。
- 镜像：`sha256:3047322967eb61dac47313f2f56fda3ae2d2974e3501c258bef2bbc9c6b6038e`。
- runtime.json、production.env、deployment.json 前后 SHA256 全部一致；发布前私有备份权限 0600，目录 0700。
- 发布后实读 status=ok、production、worker_alive=true、maintenance=false；最终 active_tasks=[]、phase_blockers=[]。
- 本次运行过的验证容器均已退出/自动删除。测试镜像、封存候选和旧 release 保留，不清理用户数据。

私有切换证据位于服务器准备目录 `publish/`，包含 check-switch、publish.log、result.json、before-current.json、after.json 和配置备份。旧版本仍为 previous。若需回退，使用同一管理器先 check-switch 核验旧版本兼容及活动任务，再 rollback `us-east-20261107-main-824cfb5`；不得还原旧业务数据库。本轮未执行回退。

## 真实 Chrome 线上后测

使用同一项目、同一真实 Chrome 的独立标签页，首次进入后再刷新四次。已通过 DOM 检查项目可见且为正确项目，以下仍使用可精确比较的首个详情 responseEnd 口径，不冒充像素级首次绘制时间。

| 样本 | 页面 load ms | 详情开始（导航后）ms | 详情请求 ms | 导航至详情完成 ms |
| --- | ---: | ---: | ---: | ---: |
| 首次进入新版 | 1829 | 1861 | 3416 | 5277 |
| 刷新 1 | 875 | 1107 | 3873 | 4980 |
| 刷新 2 | 522 | 757 | 1707 | 2464 |
| 刷新 3 | 908 | 1160 | 3380 | 4540 |
| 刷新 4 | 522 | 1096 | 4241 | 5337 |

本组前测中位 **7585ms**，后测中位 **4980ms**，减少 **34.34%**；后测范围 2464–5337ms。样本量有限、真实并发负载不受控，前测时隔离测试/构建仍在运行，后测时验证容器已退出，不作为压力测试或全体用户时延保证。没有拿较早最慢的 26 秒样本替代近期基线。

五次后测均验证：详情与 sidebar 同时发起，workspace 保存请求在详情返回之后开始。刷新 1/2 中详情先于 sidebar 完成，不再等完整侧栏显示项目。

附加验收：

- 不带 hash 访问 `/home/`，正确恢复目标项目。只读 workspace 请求 **271ms**，详情导航后 6382ms 完成；该样本单独列出，不混入深链五次的中位数。
- 不存在的 32 位项目 ID：详情返回 404，随后恢复保存的目标项目，详情 200，hash 与 current.id 均回到正确项目。
- 身份 `/api/me` 200；项目状态 complete、6 条素材，侧栏共 226 项。
- 浏览器实际获取的 8 个 JS 均 HTTP 200，SHA256 逐项与固定候选源码一致；app.js 版本 `1272ff3492fe3a82179a`。
- 已有轻量成片 Range `bytes=0-1023`：HTTP 206、video/mp4、1024 字节，Content-Range `bytes 0-1023/8157496`。
- 关闭本轮独立 Chrome 标签页；未操作用户原有标签页。未保存业务草稿、触发制作/编辑/替换或 FBBM/飞书交付；页面正常导航产生的 workspace/诊断及现有来源状态请求保留正常行为。

## 边界与后续

首屏等待链已缩短，但后测详情接口自身仍为 1.71–4.24 秒；不带 hash 的附加样本详情为 5.17 秒。全局锁争用与后台扫描仍需后续分段诊断，不能宣称已经秒开。此轮按已审核补丁发布，没有为了压低测量数字扩大移除锁或修改调度语义。

私有原始证据：`after-1.json` 至 `after-5.json`、`after-no-hash.json`、`after-invalid-hash.json`、`after-smoke.json`、`performance-summary.json`，以及本地/服务器完整测试日志、封存 release.json。取回测试日志的首批多文件 SCP 超过工具 40 秒上限而中止，随后单独取回所需文件；不影响发布或线上验收。

优化已在 main，发布使用固定 main `f40e59f`；本报告与线上变更索引以文档提交同步回 main，不改变本次运行镜像。
