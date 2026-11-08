# 2026-11-08 ExactCut nginx 文本 gzip

## 授权与范围

马涛明确授权在 `8.221.122.75` 的 `autocut.creativefitting.ai` 开启指定文本类型的 nginx gzip，先备份和 `nginx -t`，只 reload，异常恢复备份。本次不修改应用代码、生产 runtime、凭据、部署参数或其他 nginx 配置。

操作使用既有 root SSH，nginx 版本为 1.24.0（Ubuntu）。应用保持 `de8d7e7` / `us-east-20261108-main-de8d7e7`。

## 备份与配置差异

北京时间 2026-11-08 17:24:10 前完成备份：

- 完整 `/etc/nginx`：`/srv/exactcut/preparation/nginx-gzip-20261108/nginx-before.tar.gz`，服务器 root 受限目录，归档权限 0600。
- 目标文件原件：同目录 `exactcut.before`；有效配置快照为 `nginx-before.txt`。
- 原站点文件 SHA256：`31424729ca83a9a6c06a583617bc272b03da9b75c4c684822519baae6370a81c`。
- 修改后 SHA256：`d9ed87034e9a4c68338cc8e4ec9da594cb7f418cbb967621d1815963cf3ed006`。

唯一修改文件 `/etc/nginx/sites-available/exactcut`。在该域名的 HTTPS server 块、`ssl_session_timeout 1d` 后新增以下 6 行（修改后第 44–49 行）及一个空行：

```nginx
gzip on;
gzip_types application/json application/javascript text/css text/plain image/svg+xml;
gzip_min_length 1024;
gzip_comp_level 5;
gzip_vary on;
gzip_proxied any;
```

HTML 使用 nginx 默认的 text/html 类型。按授权包含文本 SVG，不加入 video/*、JPEG、PNG 等二进制媒体类型。共享 http 配置、HTTP 跳转、TLS、反向代理 snippet、其他站点均未修改；对 `/etc/nginx` 常规文件的前后 SHA256 清单核对，变化仅上述站点文件。

## 执行与回滚

- 先运行 `nginx -t`，syntax is ok / test is successful 后才执行 `systemctl reload nginx`。
- reload 于北京时间 17:28:00 完成；响应核验于 17:28:47 完成，执行脚本总用时 47.43 秒，结果 ok。收尾再次 `nginx -t` 通过。
- 未执行 restart。nginx MainPID 前后均为 `2124876`，ActiveEnterTimestamp 保持 2026-11-05 06:15:25 CST。
- 应用容器 ID、镜像及启动时间完全一致，未重启；三份应用配置 SHA256 完全一致。
- 执行脚本包含失败自动恢复目标文件、重新 `nginx -t` 并 reload 的路径；本次核验成功，未触发回滚。备份保留。

需要手动回滚时先核对没有后续配置变更，仅恢复本次站点文件：

```sh
cp -a /srv/exactcut/preparation/nginx-gzip-20261108/exactcut.before /etc/nginx/sites-available/exactcut
nginx -t && systemctl reload nginx
```

完整备份用于灾难恢复，不应直接覆盖其他后续配置。

## 公网响应核验

通过真实 HTTPS 域名、服务器内存中的既有认证信息发起只读 GET。认证头、密码、业务响应正文均未输出或写入 Git。变更前后各比较 `Accept-Encoding: identity` 与 `gzip`；传输大小指响应体字节，不含 HTTP 头与 TLS 开销。

| 请求（Accept-Encoding: gzip） | 变更前传输字节 | 变更后传输字节 | 解压后字节 | 变更后相对未压缩减少 |
| --- | ---: | ---: | ---: | ---: |
| `/home/` | 32,662（原有默认 gzip） | 26,490 | 106,040 | 75.02% |
| `/home/api/batches` | 1,416,338（未压缩） | 178,228 | 1,416,338 | 87.42% |
| `/home/api/batches/sidebar` | 76,157（未压缩） | 12,612 | 76,157 | 83.44% |
| `/home/app.js` | 46,707（应用 gzip） | 46,707 | 153,395 | 69.55% |
| `/home/brand-style.jpg` | 67,612 | 67,612 | 67,612 | 0% |
| `/home/healthz` | 170 | 170 | 170 | 0% |

首页、两个 JSON 和 JS 带 gzip 时均为 200、`Content-Encoding: gzip`、`Vary: Accept-Encoding`。identity 均不压缩。JS 已由应用压缩，传输大小保持，未重复压缩；JS 与 JPEG 解码后的 SHA256 前后一致。JPEG 不压缩。healthz 小于 1024 字节，按阈值不压缩，返回 ok/production/worker_alive=true/maintenance=false，应用 release 正确。

既有 MP4 预览总长 10,074,906 字节，在带 `Accept-Encoding: gzip` 时：

- 普通 GET：200、video/mp4、无 Content-Encoding、Accept-Ranges: bytes，总 Content-Length 保持；只读取前 65,536 字节作为非 Range 样本，前后 SHA256 相同，没有下载完整视频。
- 首段 `Range: bytes=0-1048575`：206、`Content-Range: bytes 0-1048575/10074906`、Content-Length 1,048,576，无 Content-Encoding，响应体 SHA256 前后相同。
- 尾段 `Range: bytes=-1024`：206、`Content-Range: bytes 10073882-10074905/10074906`、Content-Length 1,024，无 Content-Encoding，响应体 SHA256 前后相同。

CSS、text/plain、SVG 的类型配置已核对，没有为验收额外创建静态资源或修改应用。没有浏览器播放验收，也未由本次流量测试推断后端计算耗时改善。

## 证据与提交

私有证据在服务器 `/srv/exactcut/preparation/nginx-gzip-20261108/`，包括 before/after、nginx.diff、nginx-test、reload、result 及配置文件哈希清单。本机响应摘要在 `/Users/matao/exactcut-test/nginx-gzip-20261108/`；无认证秘密。

仓库仅更新本记录及 `docs/production-change-log.md`，中文 commit 后推送 main。无需重建应用镜像；应用代码及线上 commit 保持 de8d7e7。
