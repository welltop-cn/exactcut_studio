# OSS 预览、正式成片下载与内部复制交付

## 行为与数据边界

- 生产通过现有 OSS 凭据连接现有桶；预览访问由 preview.oss_enabled 控制，不依赖 FBBM 发布开关；固定写入 `exactcut-previews/production/<batch>/<request>/<sha256>.mp4`。配置沿用 `config/default.json` / `EXACTCUT_CONFIG_FILE` 的 `preview.oss_enabled`，默认启用，仅 production 生效；开发与测试使用隔离夹具和本地视频，不连接生产 OSS。
- 新增已鉴权 GET `/api/batches/<batch>/preview-url/<request>`，确认项目权限和素材可预览后返回 `{url, expires_in}`。生产返回 900 秒签名 GET 地址，响应 no-store。详情接口不上传、不携带签名。正式成片下载使用已鉴权 `/api/batches/<batch>/download/<request>`，返回 302 到附带 attachment 文件名参数的签名地址，no-store；不传输视频字节。FBBM 仍需用户确认，优先从已验证的私有正式成片对象执行 OSS CopyObject 到原交付键，验证源 SHA/大小与 ETag 条件并禁止覆盖，不重传视频；无私有回执的历史素材保留原直接上传能力。私有源对象 ACL 不变。
- 新预览生成后立即上传，私有对象校验成功后把回执保存在素材结果中；复用成片及手动补生成预览的流程同样确保上传。前端只需取签名；通过绑定桶、端点、完整对象键和本地文件身份（dev/ino/size/mtime/ctime）的回执校验后直接签名，无需重新哈希或 OSS 查询。正式成片也在制作完成后自动上传。历史视频首次访问或此前上传失败时按需补传（无轻量预览则用成片），之后同一内容复用。冷路径使用独立本地临时副本进行哈希和上传，防止两次读取不同版本；清理的只是本地副本，不是 OSS 对象。上传、散列和网络操作不持有项目全局锁；每个进程并发准备限 2。上传失败返回可重试 503，不自动回退应用视频流。
- 对象显式 private；存在时核对 SHA256 元数据、长度和 ACL，不符则失败，不修改对象。上传使用 `x-oss-forbid-overwrite: true`；详见 [OSS PutObject 官方契约](https://www.alibabacloud.com/help/en/oss/developer-reference/putobject)。预览/下载准备不调用任何 OSS 删除、桶 ACL、CORS、生命周期操作；正式交付仍按既有用户确认流程设置交付对象 public-read。
- 切项目先释放播放器，默认执行进度（草稿为编辑）。只有预览标签可见时才获取地址、设置 src 和 preload=auto。切标签、切项目、离开工作区及 pagehide 均 abort 在途地址请求，并 pause/remove src/load；迟到响应不得恢复旧播放器。过期地址播放或 seek 时重取，保留位置和暂停状态；其他失败显示重试。
- 独立前缀避免其他应用键冲突，但共用桶容量、带宽与计费。没有自动删除规则，历史预览版本保留。

## 历史补传

`python -m exactcut.media_backfill` 仅统计已有、质检通过且未在执行的素材；`--apply` 逐条补传预览与正式成片，`--batch ID` 可限制范围。成功回执通过素材锁重新校验当前版本后合并保存，不覆盖其他记录字段；失败可重复执行，同内容复用，不渲染、不删除。

## 回归范围

私有上传与相同内容并发去重；不同内容保留旧对象；已有对象 hash/size/ACL 不符拒绝；非生产禁用；HTTP 鉴权、无权项目拒绝、no-store、失败信息脱敏；OSS 请求被阻塞时详情仍响应；前端进度页不加载、进入预览加载、切走取消与迟到响应保护；快路径不哈希不查询 OSS、稳定副本上传、生成完成自动上传与回执持久化、历史补传 dry-run/重复执行保留原记录；正式下载重定向与附件文件名、FBBM 内部复制且不执行二次 PUT；完整主机及 Linux 发布套件。

## 线上验收

待本次发布后补充版本、测试和实际浏览器结果。
