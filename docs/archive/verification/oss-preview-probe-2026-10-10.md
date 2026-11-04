# OSS 预览隔离可行性验证（2026-10-10）

> 历史归档（2026-11-04）。正文保留当时的方案或执行状态，不代表当前版本；日常操作见[文档入口](../../README.md)。

用户授权在现有 OSS 中新增独立预览目录，并明确禁止删除 OSS 数据、影响其他应用。本次未删除或覆盖任何已有对象，未修改桶级 ACL、CORS、生命周期或其他应用配置。应用尚未接入 OSS 预览。

## 目录实测

桶 `aigc-us02-content`，地域端点 `oss-us-west-1.aliyuncs.com`。根目录 delimiter 列举完整返回、未截断，无根级文件：

```text
OULA_AGENT/
_debug/
assets/
bgm/
exactcut-previews/
fb-assets-thumbnail/
fb-bm-assets/
novel-assets/
pipeline/
relay/
v15-ab-tests/
```

新增前缀 `exactcut-previews/production/`，仅包含本次上传的一个私有预览对象：

```text
452471dced424bec8bd5b668e641402d/R452471dc/b53d40e0d1d66050ce9b50798942f49e75ba384438372f7045afdc7f9959b417.mp4
```

正式交付前缀 `fb-bm-assets/` 下按数字子目录组织；仅查看前 20 个子目录，该层结果截断，未递归扫描其他应用文件。

## 访问验证

- 复用生产 `OssPublisher.connect()` 的配置与权限；未调用会公开对象的正式交付上传流程。
- 上传现有轻量预览 6,443,372 字节，上传前检查目标不存在；对象 ACL 显式为 private。桶本身为 public-read，不能继承其默认读权限。
- HEAD 大小及 SHA256 元数据匹配；匿名 GET 返回 403。
- 15 分钟签名 URL 的 Range GET 返回 206，Content-Range 为 `bytes 0-1023/6443372`，前 1024 字节与源文件一致。
- 1 秒签名 URL 等待 3 秒后访问返回 403。
- 现有 Chrome admin 页临时原生 video 直接加载 OSS：4,308 ms 获得元数据，120 秒、540×960。同期 home、详情、队列均 200，完成耗时 812/815/814 ms。临时 video 已释放移除。
- 桶无 CORS 配置；上述不带 crossorigin 的原生 video 验证成功，不代表跨域 fetch 可用。

该结果证明现有权限与私有签名视频访问可行；单次后台标签元数据测试不等于完整视频并发下载、Command+R 或 Mac mini 前台验收。未配置任何自动删除规则，预览对象保留。

独立前缀避免覆盖其他应用键，但不是独立桶或权限边界；未来接入只允许写入此前缀，仍共用桶容量与计费。
