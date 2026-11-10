# 原片断流续传参数修复（2026-11-10）

针对复盘 F1。本地修复，尚未发布生产。

## 原因与改动

`open_original(..., start=100)` 把 `{'Range': 'bytes=100-'}` 作为第二个位置参数传给 `_get_stream(url, start=0)`。默认适配器再次组装请求头，最终生成非法的 `bytes={'Range': 'bytes=100-'}-`。

产品代码仅一行：改为 `get(url, start=start)`。没有偏移时保留原来的单参数调用。鉴权、签名、下载流处理、最多三次续传、OSS 上传、交付及部署配置都未改。

## 验证

测试保留 `open_original`、默认 `_get_stream`、`_Remote` 和 requests.Response，只在签名返回及网络出口使用独立替身。

- 默认适配器测试先红：**1 failed、1 passed / 0.30s**，失败精确显示非法 Range；普通下载正常。修复后通过。
- 最初测试夹具未给 requests.Response 配 raw，close 抛 AttributeError；修正夹具后才得到上述有效红灯，不把夹具错误算作缺陷复现。
- 真实响应流转换路径：先转发 262144 字节，再由 urllib3.ProtocolError 触发 requests.ChunkedEncodingError；续传必须请求该偏移，最终输出与 768000 字节已知数据完全一致，Content-Length 只发一次。
- 检查断流连接在重连前关闭、新连接读取期间未提前关闭，最终连接均释放。测试夹具明确区分 close 与完整读完后的 release_conn，未要求 requests 对已消费连接再次 raw.close。
- 续传收到 200 而不是 206 时仍拒绝，并释放响应；重试次数上限回归通过。
- 修正原成功测试的假数据：声明长度 15 却拼出 16 字节、Content-Range 不完整；现在长度和合法范围与实际数据一致。

从 source 执行指定 Python，清除默认 marker 过滤：

```sh
/Users/matao/miniconda3/envs/py3.12/bin/python3.12 -m pytest -q -o addopts= \
  tests/test_originals_resume.py tests/test_originals.py tests/test_originals_local.py
# 33 passed / 9.77s
/Users/matao/miniconda3/envs/py3.12/bin/python3.12 -m pytest -q -o addopts= \
  tests/test_originals_lookup_ui.py tests/test_phase1_download.py
# 17 passed / 7.15s
```

两批文件不重叠，共 **50 passed**。复盘原始 `probe_original_resume.py` 也重新通过：实际 Range=bytes=100-、outcome=accepted、external_requests=0。

本轮未调用真实 OSS、未验证历史网络断链原因，也未扩展 Content-Range/ETag 一致性策略。未执行完整发布回归；上线仍需正常发布门禁。
