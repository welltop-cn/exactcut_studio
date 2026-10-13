# 固定服务器测试实例

2026-10-13，用户授权同机隔离测试，接受受限资源竞争；先测试验收，再单独发布正式服务。

## 固定配置

- 主机：8.221.122.75；入口按 Host 分流，测试 `http://8.221.122.75/test/`，正式 `https://autocut.creativefitting.ai/home/`。
- 测试容器 `exactcut-test`，宿主机仅绑定 `127.0.0.1:18899`，容器8792。正式仍为127.0.0.1:18792。
- 测试根 `/srv/exactcut-test`，代码只读挂载code，数据data独立；没有生产数据、配置或凭据挂载。
- development，6 CPU / 6 GiB，禁止swap增长，PID256，CPU shares256；按需启停，不自动重启。
- 外部写入、下载、AI、发布和worker均关闭。这轮用于编辑、选帧和播放交互验收，不能据此宣称制作发布全链路已验证。
- 代码为 `feat/source-frame-picker` 开发快照（包含未提交改动），不是生产发布候选。复用现有镜像的依赖，不修改镜像或正式实例。
- 仅复制本地独立测试项目95f99b49174e4cbb80dbbe381abde05c、剧101619的EP1/3/4/5/7副本；不复制生产账号库。
- 独立admin账号；密码只保存在测试data/.web-state/password和本地私有验收目录，不写Git。
- Nginx独立文件 `/etc/nginx/conf.d/exactcut-test.conf`，精确匹配IP Host；正式域名配置文件不改。新增规则通过nginx -t后平滑reload。

## 后续复用

服务器运行 `/srv/exactcut-test/control start|stop|status|logs`。只操作测试容器，保留数据。停止后IP入口返回上游不可用，正式域名不受该启停操作影响。

代码更新先在独立目录准备并校验，再停止测试实例、切换测试代码并启动，禁止覆盖正在运行的源码。保持端口、账号、数据根、环境标记和资源限额。正式发布仍走已有发布管理器，测试通过不自动发布。

## 已验证及限制

- 服务器本机health和admin登录200；EP4首次索引16.814秒/1560帧。冷抽帧0.185–0.402秒，重复帧0.003秒；这些不含公网网络耗时。
- 当前图片约1–1.5MB，公网拖动体验须实际验收，不能用本机时延推断。
- 严格选区视频与OSS链路尚未实现；当前播放仍由测试实例下发原片并在前端暂停。
- 同机共享磁盘和网络，资源限额不等于绝对零影响。测试时同步检查正式健康、制作进程和资源。
- HTTP IP入口仅使用测试账号，不复用生产凭据。

## 公网验收

- IP:80测试健康200，admin独立登录200；Chrome能打开测试登录页。
- 公网索引缓存0.768秒，5次PNG请求2.771–4.281秒（每张1–1.5MB）。这是当前测试网络结果，说明公网图片传输已成为明显体验瓶颈，不能宣称与本地一样流畅。后续缩略图/图片压缩及准确帧确认策略仍需实现验收。
- 正式HTTPS health返回原版本 `us-east-20261012-timing-open-4e70327`，worker正常。正式容器StartedAt仍为2026-10-12T13:53:04.32042892Z。
- `/etc/nginx/sites-enabled/exactcut`、`/etc/nginx/snippets/exactcut-production.conf` SHA256前后相同；只新增IP测试server块并平滑reload。

2026-10-13：Chrome实测登录后旧/home/路径升级HTTPS失败；带新查询参数可进入HTTP工作台。测试固定入口改为/test/，只修改测试web.base_path配置及IP Host的Nginx规则。后续代码更新须保留测试web.base_path=/test，不依赖未被配置加载器消费的环境变量。

## 后续运行模式（取代上文development初始配置）

2026-10-13按用户要求切为production模式；独立production.env、runtime.json覆盖配置，OSS显式关闭；worker及外部操作开关保持0。测试/正式health均ok，项目errors为空，正式StartedAt仍为2026-10-12T13:53:04.32042892Z。入口/test/、账号、数据不变。详见[现行部署手册](../server-test-deployment.md)。
