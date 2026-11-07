# 2026-11-07 首次打开及刷新慢：浏览器只读排查

目标项目：`bf401b953fb04aeaaef9292c43f4dd52`。仅打开独立 Chrome 标签页及刷新一次；未修改代码、配置、业务草稿或发布。页面初始化会按既有逻辑 POST workspace 并上报诊断事件。

## 复现与实测

命令入口为 browser_navigate 指向目标 URL，然后 browser_execute_js 读取 Performance Resource Timing；刷新使用 browser_reload。判定：首个项目详情请求 responseEnd > 10000 ms。

已运行的核心判定表达式：

```js
const p = performance.getEntriesByType('resource').find(x =>
  new URL(x.name).pathname === '/home/api/batches/bf401b953fb04aeaaef9292c43f4dd52');
({ verdict: p && p.responseEnd > 10000 ? 'FAIL' : 'PASS',
   project_ready_ms: Math.round(p?.responseEnd || 0) });
```

首次输出 FAIL，project_ready_ms=21049；刷新详情 responseEnd=26618，亦超过 10 秒。

| 指标 | 首次打开 ms | 普通刷新 ms |
| --- | ---: | ---: |
| window load | 1167 | 554 |
| me 请求 | 387 | 443 |
| workspace 请求 | 8672 | 15383 |
| sidebar 请求 | 4518 | 4557 |
| 项目详情请求 | 6912 | 6177 |
| 导航至详情响应完成 | 21049 | 26618 |

首次 workspace/sidebar/详情开始于导航后 934/9608/14137 ms，刷新开始于 490/15875/20441 ms。首次三请求首字节等待约 8671/3985/6141 ms，刷新约 15379/4333/6130 ms。主要观察是 API 首字节等待与串行依赖；没有完整服务器日志，不能把 TTFB 全算作应用执行或锁等待。

## 已确认依赖

实际 fetch 当前标签页 app.js（4203fd483075b8f7b871）文本核对：启动 Promise.all(me, workspace) 完成后调用 restoreWorkspace；restoreWorkspace 先 await refreshBatches/sidebar，再 openBatch。即使 URL 有合法项目 hash，也需先取完整侧栏确认其存在。

本地后端实现 workspace/sidebar/详情使用 webserver.LOCK；worker_tick 也在同锁内执行 recover_jobs 等。当前源码中 compact 已使用 material_versions.sidebar，不能套用 10 月旧问题、声称侧栏仍逐项调用 detail。

## 边界与后续

SSH deploy 可连接，但 docker socket 无权限、Nginx 日志无权限、sudo -n 要求密码。未绕过权限，未取得当前进程分段 profile，不能确定后台调度是这两次等待的唯一来源。

可优化的确定项：带 hash 的详情展示不应串行依赖完整侧栏与工作台恢复，仍须由详情接口鉴权；并行请求只能缩短依赖，不能解决后端共享锁争用。服务端需量化锁等待、锁内工作及调度扫描后再实施针对性优化。本次仅诊断，没有修复或上线。
