# 2026-11-11 后续审查：下载响应体与验证环境

## 范围与结论

审查 main `ab00eee`，运行代码与生产 `2fb979a` 相同。本轮新增确认两个下载边界缺陷，并发现本机验证依赖偏差；没有修改应用、生产配置或业务数据，也没有重新发布。

使用真实本地 HTTP、默认 requests 适配器、原 `_send_original`、Chrome fetch 和原生 OPFS 文件写入。探针保存在仓库外。另启动同生产镜像 ID 的**独立临时容器**，`--network none`，不挂生产数据，通过容器内回环 HTTP 复核依赖差异；未向正在服务的容器写入或注入测试代码。

## F5 / P2：自动解压后仍转发压缩前长度

位置：`source/exactcut/originals.py:262`、`:274`；`source/exactcut/webserver.py:1590`。

触发条件：上游返回 `Content-Encoding: gzip`。默认 requests 可以接受压缩响应，`iter_content` 自动解压，但 `_send_original` 仍照搬原 `Content-Length`，没有保证响应头、正文和后续 Range 使用相同的字节表示。

实测：上游压缩体 154 字节，解压后 40,000 字节。应用发出的头仍为 `Content-Length: 154`，实际写入 40,000 字节。该结果在本机和精确生产镜像（requests 2.34.2 / urllib3 2.7.0）均复现。客户端可据错误长度截断；若中途续传，已转发的解压后偏移也不应直接当作上游编码表示的偏移。

边界：这是受控压缩响应的确定性复现，未证明真实生产 OSS 原片曾返回 gzip，也未证明线上发生过该原因的损坏。不是上次续传头校验已修复的同一问题。

建议：明确原片代理要求的字节表示；请求 identity 并检查上游实际编码，不能只改请求头或只删 Content-Length 而继续混用 Range 偏移。异常编码应在向客户端发送正文前处理。

## F6 / P2：浏览器未校验最终字节数就提交文件

位置：`source/exactcut/web/app.js:323`（`writeOriginalFile`）。

触发条件：目录提供预期大小，上游/代理没有 Content-Length，正文按 EOF 正常结束但比预期短。浏览器 fetch 此时不会自动报协议错误。

真实 Chrome + 原生 OPFS 实测：调用原函数，预期大小 16，HTTP 正文只有 `first` 五字节。函数返回 `saved`，文件已提交为 5 字节，没有错误。目录预期大小目前用于进度显示和同大小文件跳过，却未用于结束时的完整性校验。

建议：在 `writable.close()` 前校验实际累计字节与可用的预期长度；不一致时 abort，明确提示不完整或源片大小变化。不能只依赖 fetch 抛错，也不能在已经提交文件后才报错。真实短读来源是否为中途断链、上游对象变化或目录过期，需要另行区分。

## 验证环境偏差：本机普通短读不可外推生产

| 环境 | requests | urllib3 | 声明 16、实际 5 字节的普通响应 |
| --- | --- | --- | --- |
| 本机指定 Python 环境 | 2.32.3 | 1.26.20 | 默认适配器静默结束 |
| requirements.lock / 精确生产镜像 | 2.34.2 | 2.7.0 | 抛 ChunkedEncodingError |

因此，本机普通短读探针失败**不列为当前生产缺陷**。生产依赖的底层长度校验有效，但本机全量结果不能替代锁定依赖下的验证；上次发布确实另外执行了独立 Linux 全量门禁，不能把此次环境差异误写成“上次没有验证生产依赖”。后续应使用独立且对齐锁定文件的本机测试环境，不原地升级共享 Python 环境。

作为对照，上游声明 5 字节却发送更多时，两个环境的 HTTP parser 都只交付 5 字节；没有证据支持“普通 Content-Length 多读会直接穿透代理”。

## 仍存在的已知边界及未确认项

- OSS 对象人工删除、回执仍有效时，正常签名/准备状态不会自动 HEAD 或恢复对象。这在上一轮已确认，本轮没有把它重复算作新增缺陷。上次恢复了精确的 8 个测试对象，但恢复数据没有改变自愈逻辑。后续可设计异步核验和显式重建，不应在共享锁内或每次播放请求中加入阻塞网络检查。
- 没有初始强 ETag 时，同大小对象替换仍无法仅靠长度证明内容身份；属于已披露限制。
- 任务恢复/交付幂等已有丢失回执、孤儿进程接管和 outbox 报告恢复用例。本轮只复核相关覆盖入口，未做新的全窗口故障矩阵，没有新增失败证据，不能列成已确认 bug 或宣称已穷尽。

## 复现与证据

本机执行（故意保持失败，未修产品代码）：

```sh
/Users/matao/miniconda3/envs/py3.12/bin/python3.12 -m pytest -o addopts= -q --tb=short /Users/matao/exactcut-test/followup-20261111/test_download_boundaries.py
```

两次结果均为 **3 failed / 1 passed**：其中两项是 F5/F6，一项来自本机旧依赖；多读对照通过。最后一次 1.84 秒。

同生产镜像独立容器：短读抛 ChunkedEncodingError；gzip 声明 154 / 写出 40,000；多读声明 5 / 写出 5。

私有证据目录：`/Users/matao/exactcut-test/followup-20261111/`，含 `test_download_boundaries.py`、`locked_image_probe.py`、`local-probes.log`、`local-probe-result.json`、`locked-image-probe.log`、`locked-image-results.json`。
