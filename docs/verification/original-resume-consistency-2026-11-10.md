# 原片续传响应一致性修复（2026-11-10）

后续排查确认：此前只验证续传 HTTP 206，未核对 Content-Range 和原对象身份。受控上游返回错误起点、不同总长、缺失范围、错误剩余长度、变化/丢失的 ETag 时，正文仍被拼接。六种失败回归均在旧行为上报 DID NOT RAISE（6 failed / 0.39s）。这是本地缺陷复现，不是线上错片发生记录。

## 修复

`originals.validate_resume` 在 `_send_original` 接到新响应后、读取正文前检查：

- 已发送字节数与 Content-Range 起点一致，范围不倒序；已知总长下必须返回对象的剩余完整范围，且总长与初次响应一致。
- 续传 Content-Length 存在时，与范围长度一致。
- 首响应有 ETag 时，续传必须保留相同强 ETag；弱 ETag 不能证明字节相同，拒绝用其继续拼接。
- 不一致则关闭新响应、停止续传，沿用原下载异常路径；已经发送的旧字节无法撤回，但不再追加不匹配正文。
- 首字节发送前断流允许匹配的 HTTP 200 从头重试，仍核对原长度和 ETag。补验发现最初仅接受 206 会拒绝这一合法场景，已加回归并修正。
- 原始长度未知时允许合法未知总长的范围；首响应没有 ETag 时保留旧兼容能力，但不能据此保证同长度对象替换可被检测。

不改变签名、权限、OSS 上传、外部交付、配置或持久 schema；已发送过正文时不从头重放。仍最多三次续传，测试从“<=3”收紧为确实尝试三次合法续传后退出。

## 验证

九文件组合：Hook 预览、切项目、发布同步、原片适配器/HTTP/本地文件、原片查询 UI、下载校验，**108 passed / 20.22s**。

随后补充首字节前对象变化、弱 ETag、未知初始长度等边界，续传专题最终 **21 passed / 0.27s**。两批重叠，不累加。

命令（source，指定 Python，清除默认 marker 过滤）：

```sh
/Users/matao/miniconda3/envs/py3.12/bin/python3.12 -m pytest -q -o addopts= tests/test_hook_preview_draft_ui.py tests/test_project_switch.py tests/test_publish_session_ui.py tests/test_publish_conflict_resync.py tests/test_originals_resume.py tests/test_originals.py tests/test_originals_local.py tests/test_originals_lookup_ui.py tests/test_phase1_download.py
/Users/matao/miniconda3/envs/py3.12/bin/python3.12 -m pytest -q -o addopts= tests/test_originals_resume.py
```

测试使用独立数据和受控网络出口，无真实 OSS/FBBM 请求。尚未发布生产；未执行完整发布回归。该校验不能代替原对象的 SHA 校验，也不证明已消除所有网络故障或中途损坏。
