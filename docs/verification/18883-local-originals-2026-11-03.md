# 18883 本地原片列表与读取修复（2026-11-03）

状态：本地接口补丁验证通过，18883 原项目完整业务交付仍待源片、维护更新及实际副本验收。此记录不以接口测试代替真实101682制作。

## 现场与范围

18883 原项目 `0d6fdddb468348ad92e0cbd8f5de1db5` 的认证详情 GET 返回四条“本集正片尚未就绪”。trial 来源脚本仅复制四条成片/预览、片尾和字幕缓存，未复制草稿引用的101682 EP1–EP7及帧索引。既有本地来源中尚无这七集原剧集；不能用四条成片代替。

原片列表 `/home/api/batches/{id}/originals?drama_id=101682` 返回400“需要 EXACTCUT_ALLOW_MATERIAL_FETCH=1”，trace `4769bdc68e5b4e5987435bd6ce106d70`。它走远端原片列表，和缺少本地源片是两个原因。worker0还会在入队前拒绝 `/sources`，不能混称所有错误均由worker0导致。

父方案为 `docs/design/18883-full-dev-acceptance-plan.md`（父工作区）。用户后续授权使用真实项目配置及下载这七集。父已同意通过既有development只读OSS适配器补源，不开启发布、外部上传或通用外部写入；素材下载记录另存私有数据目录。

## 最小接口契约

只在 `development` 且素材获取开关不为1时，原片列表读取既有 `source_root()/{drama}/EPn.mp4`，再读取当前数据根 `projects/{drama}/source/videos/EPn.mp4`。同集优先正片缓存；只列规范集号的非空普通文件，不读取链接文件或经链接目录的文件。不做视频探测，不调用MetaBase、OSS或下载器。

沿用既有响应字段：`drama_id`、`episodes[].episode`、`episodes[].bytes`、`episodes[].listed`及可选`notice`。本地列表的`listed`扩展为`null`，明确表示未查询上架状态；前端缓存和列表保留true/false/null，仅false显示“未上架”。非空列表提示“仅显示本地正片，上架状态未查询”；完整的本地空列表不返回提供方错误notice，页面正常显示未准备状态。

`/originals/EPn` 和 `/originals/EPn/file` 在上述本地模式沿既有项目访问权限与剧号/集号校验提供本地视频，使用现有Range流及附件名 `EPn.mp4`，不返回远端签名地址。读取期间保留缓存共享租约。不存在返回404；无效标识返回400；未登录401，无项目权限404。

fetch1与production的既有远端列表、上架查询、签名和流式读取行为保持。测试环境原有禁取片语义保持。未改变持久化schema、用户草稿、媒体账本、任务或配置平台。

## 红绿与验证

隔离树 `cut-mosaic-18883-source-delivery`，基于722b84d；没有热改18883正在加载的resource-scheduling源码。

- 既有原片接口/UI基线：18 passed。
- 新本地接口红灯：10 failed / 5 passed，失败均为fetch0在入口返回400而本地列表/读取应可用。
- 新UI红灯：实际缓存把null变为false，断言`[true,false,null]`失败为`[true,false,false]`。
- 最小补丁后：原片本地/远端及UI共34 passed，13.76秒。
- 扩展相关回归：73 passed / 1 skipped，31.95秒；覆盖原片、环境隔离、源缓存、已有成片播放、实际main worker0/1路径；私有 `/tmp/d1-originals-related-green.log`。

新测试使用独立生成的32×32、0.5秒视频及测试账号验证接口，显式禁止外部提供方调用；fetch1兼容用已存在FakeStore双向覆盖development/production。它不属于真实101682业务验收。

尚未宣称原四条错误消除、普通制作可用、真实Gemini/SAM或最终输出通过；这些必须在18883实际服务与原配置副本补齐。
