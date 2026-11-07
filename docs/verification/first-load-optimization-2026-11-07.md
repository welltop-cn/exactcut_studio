# 首屏优化与 differential-review（2026-11-07）

## 结论

已在本地完成首屏关键依赖优化，相关 86 项测试通过。未发布生产，不能把本地模拟结果当作生产提速验收。实际生产基线见 [首次打开及刷新排查](first-load-latency-2026-11-07.md)：导航至详情返回 21.05 / 26.62 秒。

审查结果：本次范围内未发现可确认的安全漏洞；严重/高/中/低级别安全发现均为 0。改动涉及授权后的只读恢复路径，审查风险按 HIGH 处理，完成后认为局部残余风险 MEDIUM（主要为并发与生产性能验收边界）。建议接受本地补丁，正式上线仍需既有完整测试和发布流程。

## 修改范围与行为

基线 `dbe98bb`，在现有工作树修改，未触碰其他会话的 `production-chrome-acceptance-2026-11-07.md`。

- `source/exactcut/web/app.js:608`：有效 32 位项目 hash 直接请求详情；无 hash 时恢复服务器保存的选择。侧栏加载不再阻塞详情展示。仍先读取账号身份。
- 同文件 `openBatch`：详情请求成功且仍为当前导航后，才后台保存 workspace，避免保存请求先于详情争用共享锁；过时或失败的导航不保存选择。
- 无效格式 hash 回到保存选择；合法但不可访问的 hash 只在详情返回 404 时回退；503 等失败保留错误，不静默切换项目。
- 侧栏失败提示错误，已打开项目保持显示。
- `source/exactcut/webserver.py:1201`：提取只读 `restored_workspace`，读取原子替换的账号状态，并重新检查项目存在、非内部项目及账号权限。
- `/api/workspace` 空参数且已有合法选择时在全局锁外返回；初始化、默认项目创建、保存选择和无效选择回退仍使用原全局锁。响应 socket 写入移至锁外。
- 未改 sidebar / detail / worker 的锁与调度恢复语义，未引入持久缓存或改变数据协议。

## 红绿验证与结果

先写并在原实现运行：

1. `tests/test_workspace_startup.py::test_linked_project_opens_before_workspace_and_sidebar_finish`：FAIL，workspace 未完成时 opened=[]，项目没有开始打开。
2. `tests/test_sidebar_performance.py::test_initialized_workspace_restores_while_scheduler_holds_global_lock`：FAIL，持有全局锁期间只读 workspace 超过 1 秒仍未返回。

修改后两项通过。进一步覆盖保存选择、不带 hash 恢复、失效链接回退、503 不回退、侧栏失败、账号权限变化、并发首次访问只创建一个默认项目，以及项目切换的过时响应、失败导航和保存顺序。

最终运行（在 source 目录，使用指定 Python 3.12）：

```sh
/Users/matao/miniconda3/envs/py3.12/bin/python3.12 -m pytest \
  tests/test_first_load.py tests/test_workspace_startup.py \
  tests/test_project_switch.py tests/test_sidebar_performance.py \
  tests/test_editor_polling.py tests/test_web_base_path.py \
  tests/test_sidebar_scroll.py tests/test_managed_completion.py \
  tests/test_scheduler_read_once.py tests/test_round2_auth.py \
  tests/test_preview_subtitle_auth.py tests/test_concurrency_recovery.py \
  tests/test_project_initialization.py -q
```

结果：86 passed。`node --check` 和 `git diff --check` 通过。未运行完整发布套件，未启动、重启或更改现有开发服务。

## 模拟依赖链对照

Node VM 执行基线和修改后的真实 startup / restoreWorkspace 代码；API 与 openBatch 使用固定延迟替身：me=4ms、workspace=87ms、sidebar=45ms、详情=69ms。每版 5 次，测至模拟详情完成：

| 版本 | 样本 ms（排序） | 中位 ms |
| --- | --- | ---: |
| 基线 | 205,205,206,208,208 | 206 |
| 优化 | 75,75,76,77,77 | 76 |

中位减少约 63%。仅证明启动依赖被缩短，不模拟全局锁、网络排队、真实渲染和生产吞吐；不能线性放大为生产承诺。生产详情接口自身约 6 秒等待尚未定位，侧栏与详情仍可争用后端共享锁。

## 权限与并发审查

策略：大仓库 SURGICAL，仅审查两个产品代码文件和四个变动测试文件，并沿账号鉴权、项目权限、原子状态写入检查一层依赖。

- 入口信任边界：`handle_post` 仍先检查登录、`X-Exactcut-Request`、维护屏障、请求大小与 JSON 对象，再进入 workspace 分支。新快路径没有提前到这些检查之前。
- 账号状态路径仍使用已认证的 `self.user['username']`，请求体不能指定其他账号。
- `restored_workspace` 有两个产品调用点：workspace 原锁内恢复与 HTTP 空参数快路径；workspace 仍只有一个路由调用点。`restoreWorkspace` 有启动和重试两个调用点。openBatch 为多处 UI 共用入口，已用现有导航、缓存、轮询测试覆盖。
- hash 不作为授权证明；只允许现有 API 项目 ID 格式，最终详情仍调用 `can_access`，不可访问时 404。
- 具体越权场景：Alice 保存自己项目后，项目归属改为 Bob；Alice 再读保存选择应返回 null，详情 404，显式保存 Bob 项目 400。新增 HTTP 回归已验证。
- 未登录恢复 401；缺请求来源头 403；首次并发初始化仍串行且只创建一份默认项目，现有 HTTP 回归通过。
- 全局锁外只读的基础为 `write_json_atomic` 通过 fsync 后 `os.replace` 替换文件。读者取得一个完整旧/新快照；不会通过新路径写状态或创建项目。项目移除/权限变化时由 `can_access` 重新判定；详情仍独立校验。
- 首次恢复、保存选择等写入仍在全局锁内，`workspace` 内仍复核有效选择，避免慢路径竞争创建默认项目。

## 历史核对

`git blame` 显示初始化与 workspace 锁来自 `8ad5c565`；显式保存校验来自 `698f4752`；已初始化选择的轻量恢复来自 `6a5de9ad`。本次保留保存校验及首次访问串行化，只将已有只读快路径移到取得全局锁之前。

前端先等 sidebar 再开详情来自 `b05fcf94` / `8ad5c565`；本次用详情的既有后端鉴权代替侧栏成员存在性作为深链判断，未移除 API 的权限检查。不存在重新引入已知安全修复删除逻辑的证据。

## 剩余验收与限制

- 上线前按仓库发布流程跑完整套件、独立验收及正式打包；当前只有本地相关回归。
- 上线后用同一真实浏览器测首次与刷新，多次记录导航至详情、接口 TTFB 和分段锁等待。
- 生产日志仍受 SSH 权限限制；未确定本轮生产 6 秒详情等待的具体内部来源，也没有为此放宽共享锁或改变后台调度。
- 本报告是局部 differential-review，不是全项目安全审计；对本地行为和已测权限结论置信度高，对生产提速幅度置信度有限。
