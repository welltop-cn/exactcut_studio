# 服务器独立测试实例：部署与交接

最后核验：2026-10-13。面向其他 agent / session。用户约定：**先部署测试实例、验收，再单独发布正式服务；测试成功不代表获得正式发布授权。**

## 固定入口与边界

| 项目 | 测试实例 | 正式实例（本流程禁止修改） |
|---|---|---|
| 公网入口 | `http://8.221.122.75/test/` | `https://autocut.creativefitting.ai/home/` |
| 容器 | `exactcut-test` | `exactcut-production-web-1` |
| 宿主机监听 | `127.0.0.1:18899` | `127.0.0.1:18792` |
| 容器端口 | 8792 | 8792 |
| 数据根 | `/srv/exactcut-test/data` | `/srv/exactcut/data` |
| 源码挂载 | `/srv/exactcut-test/code` → `/app/source:ro` | 不操作 |
| 环境 | production（独立测试用途） | production（正式用途） |
| Nginx | `/etc/nginx/conf.d/exactcut-test.conf`，精确匹配 IP Host | `sites-enabled/exactcut`、`snippets/exactcut-production.conf` |

测试限额：6 CPU、6 GiB 内存、memory-swap同为6 GiB、256 PID、CPU shares256；不自动重启。日志轮转10MiB×3。同机仍共享网络、磁盘、CPU和Nginx，用户已接受有限资源竞争；不能声称绝对零影响。

测试实例独立账号库，用户名admin。密码位于服务器 `/srv/exactcut-test/data/.web-state/password`，不要写入Git、报告或公开日志，不复用生产密码。

**不要使用旧的 `/home/` IP入口或 `https://8.221.122.75/`。** 浏览器曾缓存/升级旧路径到HTTPS，而IP没有对应TLS入口。新 `/test/` 已完成真实Chrome登录验收。无需开放公网18899，也不要改正式HTTPS兜底规则。

## 当前能力与限制

- 测试部署统一以已审核并提交的 `main` 为源。2026-10-13 已部署提交为 `a7ad9af`（含Linux音频修复），已剥离可视化选帧；镜像提供固定依赖，代码通过只读挂载更新。正式发布仍须走正式发布管理器。
- `EXACTCUT_WORKER_ENABLED=1`，`material_lifecycle.enabled=true`，制作参数及并发配置对齐正式实例（容器仍6核/6GiB）。可测试表单及已有测试原片的制作、质检、本地成片预览。新增原片下载尚无测试配置，开关暂为0；AI已按用户授权复用正式服务，开关为1，重点词分析真实调用已通过；新增原片下载尚未验证。
- 手工分:秒:帧及文本识别入口保留。最新候选已剥离可视化选帧组件至 `feat/visual-frame-picker`，不提供选帧图片或原片播放接口；后台帧索引自动准备。制作完成后的OSS预览仍使用测试专用路径。已运行的旧测试容器需另行部署候选才体现这项变化。
- 测试项目 `95f99b49174e4cbb80dbbe381abde05c`，剧101619，EP1/3/4/5/7为独立副本；帧索引已在Linux重建，项目errors为空。不要覆盖用户后续编辑。
- 不导入生产账号、任务、发布回执、运行状态或凭据。添加真实样本只复制经用户授权的配置/视频副本，不能硬链接或挂载生产目录。

## 连接与日常启停

本地已配置SSH密钥，不在命令中放密码：

```sh
ssh -o BatchMode=yes -i /Users/matao/.ssh/exactcut_8_221_122_75 root@8.221.122.75
```

以下在服务器执行，只操作测试实例：

```sh
/srv/exactcut-test/control status
/srv/exactcut-test/control start
/srv/exactcut-test/control logs
/srv/exactcut-test/control stop
```

停止后保留代码、账号、数据和缓存，测试入口返回上游不可用，正式实例不随之停止。不要运行 `docker compose down`、停止Docker服务或批量删除容器。

## 更新前检查

1. 阅读待部署仓库AGENTS.md，核实工作树及已有修改，不提交或合并他人的工作。
2. 确认没有其他session在更新测试实例、没有用户正在编辑/跑测试。出现冲突先协调，不抢占。
3. 保存待部署提交号、未提交diff说明和包SHA256。先完成对应本地验证。Python使用 `/Users/matao/miniconda3/envs/py3.12/bin/python3.12`。
4. 在服务器记录正式服务健康及启动时间，之后对比：

```sh
curl -fsS http://127.0.0.1:18792/home/healthz
docker inspect -f '{{.State.StartedAt}}' exactcut-production-web-1
docker stats --no-stream
```

5. 检查测试进程、磁盘和挂载，不输出整个环境变量列表以免泄露秘密：

```sh
/srv/exactcut-test/control status
docker top exactcut-test -eo pid,ppid,etime,comm
docker inspect -f '{{range .Mounts}}{{.Source}} -> {{.Destination}} {{end}}' exactcut-test
df -h /srv/exactcut-test
```

## 打包与传输（不包含数据）

从**实际待部署工作树**打包，不默认使用共享主工作区。以下是示例，替换路径和唯一批次名。白名单仅包含应用代码、配置和模板；不要打包 `.env`、账号库、视频、整个source目录或生产数据。

```sh
# 本地执行；仅使用干净、已审核的main。先 git status --porcelain，非空则停止。
/Users/matao/miniconda3/envs/py3.12/bin/python3.12 - <<'PY'
import tarfile
from pathlib import Path
code = Path('/Users/matao/code/exact-cut-fbbm-handoff/source')
archive = Path('/tmp/exactcut-test-code-UNIQUE.tar.gz')  # 换成唯一批次名
with tarfile.open(archive, 'x:gz') as bundle:
    for folder in ('exactcut', 'config', 'templates'):
        for item in (code / folder).rglob('*'):
            if item.is_symlink():
                raise RuntimeError(f'拒绝符号链接: {item}')
            if item.is_file() and '__pycache__' not in item.parts and item.suffix != '.pyc':
                bundle.add(item, arcname=str(item.relative_to(code)))
print(archive)
PY
shasum -a 256 /tmp/exactcut-test-code-UNIQUE.tar.gz
scp -i /Users/matao/.ssh/exactcut_8_221_122_75 /tmp/exactcut-test-code-UNIQUE.tar.gz root@8.221.122.75:/srv/exactcut-test/
```

依赖有变化时不能直接沿用旧镜像。先构建专用测试镜像并验证依赖，再更新测试专用 `base-image.txt`；不要从正式容器提取环境凭据，也不要改正式镜像/tag。集成脚本变化不在上述白名单内，须另行核对测试镜像内 `/app/integrations` 是否匹配。

## 在独立目录准备配置

以下在服务器执行，替换UNIQUE为相同批次名；只处理受信任、已核对SHA256的自建包：

```sh
set -eu
cd /srv/exactcut-test
sha256sum exactcut-test-code-UNIQUE.tar.gz
mkdir code-UNIQUE.next
tar --no-same-owner -xzf exactcut-test-code-UNIQUE.tar.gz -C code-UNIQUE.next
python3 - <<'PY'
import json
from pathlib import Path
p = Path('/srv/exactcut-test/code-UNIQUE.next/config/default.json')
doc = json.loads(p.read_text())
doc.setdefault('web', {})['base_path'] = '/test'
p.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + '\n')
PY
chown -R root:root code-UNIQUE.next
```

`web.base_path`由集中配置读取。**当前代码不消费 `EXACTCUT_WEB_BASE_PATH`；仅设置该环境变量无效。** 固定覆盖配置 `/srv/exactcut-test/runtime.json` 挂载为 `/test-config.json`，通过 `EXACTCUT_CONFIG_FILE` 加载并设置 `/test`；该文件位于代码目录之外，更新代码时保留。上面设置候选默认配置是额外保护，最终以此覆盖配置为准。服务器测试环境文件为 `/srv/exactcut-test/production.env`，挂载为 `/production.env`。本地dev仍用development。不要修改环境标记来绕过目录归属检查。

可以使用 `/srv/exactcut-test/base-image.txt` 指定的固定依赖镜像对候选源码运行隔离检查，不挂载测试data或生产路径。完整Python测试需另外准备tests及测试配置，不能把页面健康检查视为回归测试。

## 切换测试代码

只有候选包检查通过后才进入这一步。不要更新正在运行的Python源码，也不要覆盖data。

```sh
set -eu
cd /srv/exactcut-test
test -d code-UNIQUE.next
test ! -e code-UNIQUE.previous
/srv/exactcut-test/control stop
mv code code-UNIQUE.previous
mv code-UNIQUE.next code
sh /srv/exactcut-test/recreate.sh
```

必须理解：现有容器绑定的目录inode不会随目录重命名自动变更，所以此流程通过 `recreate.sh` **只重建exactcut-test**，保留data和密码。脚本使用 `base-image.txt`、固定端口与限额。不要把正式容器名传入任何命令。

现有脚本中的 `EXACTCUT_RELEASE_ID=frame-picker-test-production-20261013` 是固定展示标签，**不是新代码版本证据**。每次记录包SHA256、来源提交、差异、实际挂载和验收结果到 `/srv/exactcut-test/deployments/`，必要时仅修改测试recreate脚本中的release标签。不要根据health标签推断已经换到目标代码。

日常代码更新不需要修改或reload Nginx。只在修改入口规则时备份测试conf、执行nginx -t，再平滑reload；不能覆盖正式两个配置文件。

## 验收与生产核对

```sh
# 服务器本机
curl -fsS http://127.0.0.1:18899/test/healthz
curl -fsS -H 'Host: 8.221.122.75' http://127.0.0.1/test/healthz
curl -fsS http://127.0.0.1:18792/home/healthz
docker inspect -f '{{.State.StartedAt}}' exactcut-production-web-1
docker stats --no-stream
```

另外从用户网络验证 `http://8.221.122.75/test/` **登录→跳转→项目加载→目标操作**，不能只检查登录页出现。确认协议仍HTTP、路径仍/test/；浏览器不应进入生产域名。更新静态资源后刷新页面。

- 正式版本与StartedAt应与更新前一致，worker健康；出现异常先停测试实例并排查，不能靠重启正式服务修复测试环境。
- 记录公网响应时间。此前图片1–1.5MB，服务器抽帧0.2～0.4秒，公网却需2.8～4.3秒，不能以本机速度替代真实体验。
- 帧索引包含路径、inode、mtime等，不能复用Mac的缓存。新样本在测试实例按当前草稿引用集数调用 `source_frames.prepare` 逐集准备，再检查 `workflow.detail(batch_id)['errors']`；不改用户输入来绕过校验。
- 复制独立项目需包含draft/access/runtime及引用的endings资产；不要复制生产运行任务。现有项目不要每次部署重新初始化。

## 回滚

候选版本失败，只回滚测试代码：

```sh
set -eu
cd /srv/exactcut-test
test -d code-UNIQUE.previous
test ! -e code-UNIQUE.failed
/srv/exactcut-test/control stop
mv code code-UNIQUE.failed
mv code-UNIQUE.previous code
sh /srv/exactcut-test/recreate.sh
```

再次执行健康与页面验收。**不要回滚/删除data、账号库、项目或生产数据库。** 如果新代码写入旧代码不认识的数据，先评估兼容性，不盲目回滚。尤其新帧格式项目不能交给旧毫秒版本写回。

保留失败代码、日志和部署记录供排查；容器重建前先保存需要的docker日志。没有用户清理要求时，不删除旧代码或测试数据。

## 交付摘要模板

- 测试地址与项目链接；用户名，密码仅指向受限位置。
- 源工作树/提交/是否dirty、包SHA256、部署时间、测试release标签。
- 改了什么、验证结果、未完成能力。
- 正式健康/版本/StartedAt对照结果。
- 测试账号/数据保留情况、回滚目录、当前是否运行。
- 明确“仅部署测试实例，未发布正式服务”。

## 2026-10-13 环境模式调整

用户要求服务器测试实例也使用production，本地dev才使用development。已对独立测试实例停机切换并保留账号、项目、视频与索引；旧启动脚本和环境标记备份在 `/srv/exactcut-test/backups/production-mode-*`。这是一次明确授权的环境迁移，不是普通部署步骤，后续不要重复运行迁移脚本。

- `production.env` 包含测试环境声明及本次授权的OSS预览连接配置，权限0600；不包含FBBM/飞书凭据，禁止输出内容。
- `runtime.json`：`environment=production`、`web.base_path=/test`、`preview.oss_enabled=true`、`preview.namespace=test`。
- OSS预览已按用户授权启用，使用独立test前缀；仅预览上传可写OSS，不开放交付。
- 当前worker已开启；OSS测试预览上传允许，正式交付仍禁用。production模式不等于允许写入真实业务。
- 两个容器内部8792不冲突：网络命名空间独立；宿主机18792和18899不同。不能改成host网络后继续共用监听端口。
- 正式服务环境、端口、数据和Nginx规则不变。

## 当前验收范围（用户最终确认）

只测试表单填写及视频制作全过程：准备原片、剪辑、Hook/结尾、质检、成片预览；允许OSS预览上传到测试专用前缀；不测试正式交付、FBBM或飞书写入。`material_lifecycle.delivery_enabled=false`、`EXACTCUT_ALLOW_PUBLISH=0`、`EXACTCUT_ALLOW_EXTERNAL_WRITES=0`、`preview.oss_enabled=true`、`preview.namespace=test` 必须保留。现有5集副本可用于制作，AI已授权复用正式服务；新增下载待配置。已验证worker_alive=true、require_publish拒绝、OSS测试预览可用、正式健康正常；这是配置验证，不是已经完成一条新成片的验收。

## OSS预览例外（2026-10-13最终配置）

用户授权预览上传OSS，但必须写测试专用路径。当前复用OSS连接账号和Bucket，应用对象键限定 `exactcut-previews/test/`；这是应用前缀隔离，不是另建RAM账号或IAM权限隔离。FBBM/飞书凭据未注入，publish/external_writes保持0、delivery_enabled=false。

`preview_oss.object_prefix()` 通过集中配置仅接受production或test，默认production兼容正式实例；测试runtime.json必须显式设置namespace=test、oss_enabled=true。不能把任意OSS前缀配置为交付目录，也不能用旧生产回执复用测试对象。测试代码回退到不支持namespace的版本前必须关闭OSS预览，否则旧版硬编码生产路径。

验证：30项定向测试通过，含8项新增namespace校验；真实1秒合成视频私有上传成功，签名GET Range返回206/1024字节、MP4头正确，require_publish仍拒绝。验收记录与视频在测试data/acceptance/oss-preview，签名URL短期有效、不可写入文档。没有FBBM/飞书操作，没有修改正式服务。

Chrome真实播放验收：OSS签名视频320×568、1秒，readyState=4、ended=true、error=null，视频直接来自OSS测试前缀。

2026-10-13 AI修复：用户明确授权复用正式AI服务和密钥；只将OPENAI_API_KEY/OPENAI_BASE_URL加入测试专属production.env，AI开关设1，不复制FBBM/飞书凭据。第二条素材原文的重点词分析已真实调用成功。切换时用户主动暂停项目，不自动恢复或重试；重启后需验证paused仍为true。

## 复制帧时间项目的验收

2026-10-13 修复后，复制项目会复制当前原片有效的帧索引，不复制预览图片或视频。复制后检查新项目详情 `errors` 为空，以及它自己的 `source-picker/*/index.json` 已存在。不能直接搬用本地索引：服务器原片指纹不同。

对于此前已复制失败的测试草稿，可在确认来源和当前原片一致后，用 `source_frames.copy_frame_indexes(source_path, target_path, target_draft)` 补充派生索引，再调用 `workflow.detail` 验证；不改草稿时间、不恢复暂停任务。源文件已变化时应重新建立索引，不能绕过校验。

验收记录见 [帧项目复制验收](verification/source-frame-clone-2026-10-13.md)。

## Linux 回归镜像注意事项

应用运行镜像不包含Node，不适合直接执行完整测试。使用与依赖锁文件匹配的 `exactcut-validation` 镜像，挂载完整的 Git 已提交源码树到 `/app`，工作目录 `/app/source`；测试依赖仓库中的README、deploy和integrations，不要仅传应用白名单用于全套测试。验证容器限额并禁止网络，不挂载测试或正式data。应用部署包仍只包含代码/配置/模板，不包含业务数据。

最新部署及验收：[main-test-deployment-2026-10-13.md](verification/main-test-deployment-2026-10-13.md)。正式服务等待用户测试结果与后续发布指令。

## 2026-10-14 剪切功能测试部署

用户授权重新开启原测试实例。已部署1256f3e（含ae8acd3剪切功能与旧窗口请求修复），启用cutting及scan，保持测试OSS前缀及交付禁用。当前代码与回滚、实际验收详见 [剪切测试部署记录](verification/cutting-test-deployment-2026-10-14.md)。旧账号和项目保留；正式服务未重启或修改。

指定 101606 五集时间段的真实服务器验收及当前项目入口见 [101606 线上剪切验收](verification/cutting-101606-server-2026-10-14.md)。

## 2026-10-15 飞书自动化交付测试更新

当前测试代码 `b00374a`，release `test-observer-b00374a`。用户明确授权使用真实 OSS/飞书服务配置，测试交付已启用；这是对上文凭据隔离限制的本次明确例外。预览仍用 test namespace，未配置 CF_ADS 凭据。用户点击上传会创建真实飞书待处理记录并触发外部自动化。不要代用户提交素材。正式服务未更新，仍运行旧交付实现。详见 [验收记录](verification/feishu-observer-test-release-2026-10-15.md)。

测试实例现已更新至 `test-observer-0b13f2f`（代码 `0b13f2f`），修复真实交付对 test 私有成片路径的错误拒绝；配置未变。详见上述验收记录。

## 2026-10-19 剪切打码容器候选

本轮用户明确要求提交剪切打码功能并更新线上测试实例；候选来自已审查的 `feat/mosaic-gemini`（包含当前 main），不操作正式实例。镜像运行时使用 Python 3.12，默认不安装 SAM；测试镜像构建传入 `--build-arg EXACTCUT_WITH_SAM2=1`，安装 CPU 专用 torch 2.7.1 / torchvision 0.22.1、Hydra、iopath，并固定 SAM 源码 revision `2b90b9f5ceec907a1c18123530e92e794ad901a4`。CUDA 扩展关闭，构建阶段验证导入且 torch 无 CUDA。

模型不进入 Git 或应用数据目录：测试专用 `/srv/exactcut-test/models` 只读挂载 `/models`。SAM 2.1 tiny 权重 SHA256 为 `7402e0d864fa82708a20fbd15bc84245c2f26dff0eb43a4b5b93452deb34be69`。在现有 runtime.json 的 `cutting.tracking` 设置 enabled=true、device=cpu、checkpoint=/models/sam2.1_hiera_tiny.pt、checkpoint_sha256、model_revision、model_config=configs/sam2.1/sam2.1_hiera_t.yaml、dependency_paths=[]；保留其他已有配置和账号。沿用 6 CPU / 6 GiB / 256 PID 限制，不因模型测试提高正式宿主机资源占用上限。

只安装依赖或启动健康成功不代表动态跟踪验收：切换前须在同镜像、同限额的隔离目录完成实际 CPU 跟踪和 FFmpeg 合成，再检查输出遮罩/帧数。本地 MPS 的结果不能替代 Linux CPU 证据。CPU 长片推理速度需单独评估。

已部署 `test-mosaic-75efc57`，CPU真实短段跟踪、Linux37项回归和公网浏览器验收通过；正式服务未变。最新证据、测试项目及回滚位置见 [剪切打码测试部署](verification/mosaic-test-deployment-2026-10-19.md)。

## 2026-10-19 同片性能优化
测试实例加载test-mosaic-perf-ca2a1e7，沿用mosaic-75efc57镜像；代码ca2a1e7，一次编码完成删除与打码。SAM默认1024及跟踪缓存配置保持不变。1555项本地全量、52项Linux定向通过；同素材合成364.49→243.45秒。部署及回滚记录：/srv/exactcut-test/deployments/perf-ca2a1e7/release.json。正式生产未改动。详见verification/mosaic-performance-2026-10-19.md。

## 2026-10-19 跟踪缓存复用修复
测试实例test-cache-655e0b4，代码655e0b4：原始SAM缓存独立于效果视频版本，兼容历史5/6遮罩，保留当前强度。1574项本地全量、37项Linux通过；项目4142628fb23746149326953e8b8dd49c只读验证270+60帧复用且SAM调用0。公网health正常、worker_alive=true；生产容器启动时间未变。回滚记录/srv/exactcut-test/deployments/cache-655e0b4/release.json。未改用户数据。

## 2026-10-27 播放复现专用运行状态

用户授权重启已停止的测试实例，复制正式项目配置和视频用于播放复现。保留 test-cache-655e0b4 代码，未部署日志补丁；当前容器由 `recreate-playback-repro.sh` 启动，worker/AI/publish/external_writes=0，runtime 中 scan、delivery、OSS preview 关闭。历史 `recreate.sh` 未改，不应直接运行它恢复后台能力。备份在 `/srv/exactcut-test/backups/playback-repro-20261027/`。新项目及复现结果见 [播放复现记录](verification/playback-reproduction-2026-10-27.md)。正式服务未改动。

## 2026-10-27 播放修复测试验收

当前测试版本test-playback-62c42b3，包含播放诊断及取消竞态修复。1672项本地全量通过，Pi deepseek-flash high复审通过；真实Chrome复测快速拖动、恢复、关闭重开，诊断已落盘。仅测试部署，正式实例未更新。

本轮保留播放复现隔离：worker/AI/material-fetch/publish/external-writes=0，scan/delivery/OSS预览关闭。**不得用历史recreate.sh无意恢复后台或交付能力**；最终启动脚本位于/srv/exactcut-test/deployments/playback-62c42b3/recreate.sh。测试账号、数据、复制项目均保留，详见[验收记录](verification/playback-test-acceptance-2026-10-27.md)。

## 2026-10-28 OSS 正式原片播放测试

当前测试版本 `test-editor-oss-0a3825b`，应用提交 `0a3825b`。干净提交完整回归 1723 passed、1 skipped；公网 Chrome 验证 OSS 主播放/定位、错误后服务器回退、处理前同源、处理后与下载仍为独立结果。测试实例保持运行，入口仍为 HTTP `/test/`。

本次只重新开启 test namespace OSS 预览及 `cutting.oss_playback_enabled`；worker/AI/material-fetch/publish/external-writes=0，scan/delivery 关闭。安全启动脚本 `/srv/exactcut-test/deployments/editor-oss-0a3825b/recreate.sh`，不要使用历史根目录 recreate.sh。正式实例未修改。详细记录见 [本轮验收](verification/editor-oss-test-deployment-2026-10-28.md)。
