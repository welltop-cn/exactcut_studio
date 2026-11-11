# 2026-11-11 原片下载完整性修复：本地及服务器测试实例验证

## 结果

按用户要求，先在本地和服务器独立测试实例复现两个问题，再修改，并在两处验证。最终同一组相关回归均为 **67 passed，无失败、无跳过**。本次没有发布生产；生产仍为 `us-east-20261111-followup-2fb979a`。

后端修复提交：`b47d110`。前端修复及本记录在后续独立提交中，两个问题分别提交。

## 修改内容

1. `originals.py`：首次请求和 Range 续传都显式发送 `Accept-Encoding: identity`；`open_original` 在交付流之前检查实际响应编码，非空且非 identity 时关闭响应并返回 502，避免自动解压后的正文与 Content-Length/Range 偏移混用。能够协商 identity 的源站仍正常下载；忽略协商、坚持压缩的响应明确失败。
2. `app.js`：`writeOriginalFile` 在原生文件流 `close()` 之前，核对累计字节与响应长度、目录预期大小（可用时）。任何已知大小不符都进入原有 cancel/abort 路径，保留已有文件，不返回 saved。正常完成、未知长度和同大小已有文件跳过的行为保留。

没有新增配置项，没有改变鉴权、签名目标、ETag/Content-Range 校验、重试上限或素材版本/交付逻辑。

## 实例与隔离

- 本地使用 `/Users/matao/miniconda3/envs/py3.12/bin/python3.12`。实际依赖 requests 2.32.3 / urllib3 1.26.20；没有修改共享 Python 环境。
- 服务器在 `/srv/exactcut/preparation/download-integrity-fix-20261111/` 保留 before/after 两份代码。before 的运行代码为生产 `2fb979a`，与本轮起点 `36d6931` 相同；期间差异仅为文档。
- 服务器使用独立 Chromium 验证容器，挂载测试代码只读，2 CPU / 3 GiB，网络为 none，仅使用容器内回环 HTTP；数据根在容器 `/tmp`，没有生产数据或凭据挂载。实际 requests 2.34.2 / urllib3 2.7.0，与锁定文件一致。
- HTTP 服务和浏览器由测试夹具启动并退出，容器使用 `--rm`。未重启已有本地开发服务或生产容器，未调用真实 OSS/FBBM/飞书接口。

## 先红后绿证据

| 阶段 | 本地 | 服务器测试实例 |
| --- | --- | --- |
| 修改应用前，两个最小回归 | 2 failed，2.90 秒 | 2 failed，2.53 秒 |
| 最终扩展的 12 项新回归作用于保留旧代码 | 8 failed / 4 passed，8.92 秒 | 8 failed / 4 passed，8.33 秒 |
| 修复后：12 项新回归 + 55 项相关回归 | 67 passed，19.43 秒 | 67 passed，19.46 秒 |

这是同一集合的跨环境验证，不累计成 134 个不同测试。首次红灯发生在任何应用修改之前；之后保留旧运行代码、换入最终测试，再次确认新测试对旧缺陷仍会失败。

最初两项失败：

- 实际原片 HTTP 路由对 gzip 上游返回 200，而不是拒绝不一致字节表示。
- Chrome 接到正常 EOF 的 5 字节正文，目录预期为 16 字节，返回 saved 并把原 `old-file` 覆盖为 `first`。

最终新增覆盖：

- 实际 HTTP 下载路由拒绝 gzip，返回明确编码错误，未先发成功响应。
- 内容编码协商后完整返回原始 40,000 字节，长度一致。
- 真实 Range 请求按原始字节偏移下载，并通过原有范围校验。
- 206 续传响应仍声明压缩编码时拒绝。
- 浏览器短读、超出预期、响应头与目录大小冲突、空旧文件失败保护。
- 浏览器正常有/无长度响应、完全未知预期长度、同大小已有文件跳过。

浏览器用例使用真实 fetch、响应 reader 和原生 OPFS 的 staging/abort/close，不用内存替身冒充文件提交。测试调用当前源码中的保存函数，未覆盖操作系统目录选择器界面。

## 相关回归及一致性

```sh
cd source
/Users/matao/miniconda3/envs/py3.12/bin/python3.12 -m pytest -o addopts= -q --tb=short \
  tests/test_original_download_integrity.py \
  tests/test_original_download_integrity_ui.py \
  tests/test_originals_resume.py tests/test_originals.py \
  tests/test_originals_local.py tests/test_originals_lookup_ui.py
```

服务器相同文件集合通过，浏览器用例实际执行，没有因缺 Chromium 而跳过。原有适配器测试更新为同时断言 identity 与 Range，保留断流续传、对象一致性、零字节重试、重试次数和关闭连接的验证。

本地与服务器 after 的两个运行文件及三个测试文件 SHA-256 全部一致：

- originals.py：`d097510921400c30a26deb60a282b0a9992ef2c7ff5a5737a6c9bc1e53ebde23`
- app.js：`a3a92ec3cfe0e4dbb628e6031ef07d7d36e254b4268fa4cb61fdb7ae4919b825`

另外通过 `node --check` 与 `git diff --check`。

## 差异审查

审查范围为两个运行文件及三个测试文件，采用大型仓库的局部关键路径审查，不声称审查整个系统。新增 CRITICAL/HIGH/MEDIUM/LOW 安全发现均为 0；整体变更风险为 MEDIUM，建议通过本轮修复验收，生产发布仍须完整门禁。

调用范围：`open_original` 在应用内有两个调用位置，分别为同一原片 HTTP 接口的首次打开与续传；默认 `_get_stream` 由它选择。`writeOriginalFile` 有一个应用调用者 `startOriginalDownloads`，服务单集、所选及全部下载入口。

历史：`git blame` 显示保存函数源于 `0029b0d9`；原片适配器 Range 扩展来自 `faf29bc8`，明确偏移参数修复为 `96ded7c1`。本次没有删除既有安全检查，没有回退参数修复或范围/ETag 保护。

具体威胁/失败路径：上游对象元数据或服务返回压缩表示时，旧代理混用压缩长度与解压字节；修复后在成功响应前拒绝。目录过期或无长度上游返回短正文时，旧保存流程提交不完整文件；修复后真实文件事务 abort，旧文件保留。普通未授权用户不能因此选择任意上游地址、绕过项目权限或取得额外文件权限；相关控制代码未改。

覆盖限制：未做新的全量 suite，也未做真实生产下载验收；没有独立预期 SHA 时，不保证发现同大小的内容替换。没有任何可用长度时仍允许保存；同大小本地文件跳过也按既有约定保留。拒绝不支持 identity 的源站响应是明确的兼容性取舍，避免把损坏下载当成功。

## 证据位置

- 本机：`/Users/matao/exactcut-test/download-integrity-fix-20261111/`。
- 服务器：`/srv/exactcut/preparation/download-integrity-fix-20261111/`。
- `local-red.log` / `server-red.log`：应用修改前最小失败。
- `local-final-red.log` / `server-final-red.log`：最终测试对旧代码的失败。
- `local-final-green.log` / `server-green.log`：最终 67 项通过。
- `local-final-results.json` / `server-results.json`、`verified-hashes.json`、`production-unchanged.json`：退出码、耗时、哈希和未发布生产的核验。
