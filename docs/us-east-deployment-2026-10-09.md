# 美东全新生产实例部署记录（2026-10-09）

本次在 `8.221.122.75` 建立独立生产实例。复用东京实例已验证的外部服务配置，不迁移账号、项目、视频、缓存或任务状态；东京实例不因本次部署而停止或升级。

## 实测机器与系统准备

- 阿里云 ECS，元数据 `region-id` 为 `us-east-1`，不是采购方案中的美西。云地域以实际元数据为准，不能由公网 IP 或采购计划推断。
- Ubuntu 24.04.5 LTS，x86_64；16 vCPU，Xeon 6982P-C；约30 GiB内存。
- 1TB NVMe设备，已有ext4根分区，初始可用约942 GiB；使用现有文件系统，不格式化或修改挂载。
- Docker 29.1.3、Compose 2.40.3、nginx 1.24.0，系统软件包安装完成，服务已启动，nginx语法检查通过。
- 日常SSH使用 `deploy` 与专用密钥。账号在sudo组，提权需要密码；本轮系统初始化和发布管理通过root完成，未设置全局免密sudo或赋予Docker组权限。

## 独立生产配置

部署根为 `/srv/exactcut`，唯一密钥文件为 `config/production.env`，root所有、0600；通过SSH传输，未在本机落盘或写入Git。`config/` 与 `data/` 为0700。初始化时确认 `data/` 为空。

`config/deployment.json` 的实际字段为：

```json
{
  "instance": "exactcut-production",
  "backend_port": 18792,
  "public_port": 80,
  "cpus": 14.0,
  "memory": "24g"
}
```

`config/runtime.json` 使用集中生产配置：`web.base_path=/exactcut`；预留50 GiB，正片缓存100 GiB，基础剪辑缓存50 GiB、保留7天，可用空间低于80 GiB触发按需正片回收。缓存策略不是磁盘硬配额，成片仍需保留和归档策略。本地开发配置未修改。

素材OSS位于美西；本机位于美东，保持公网 `oss-us-west-1.aliyuncs.com`。当前发布代码未配置public base时由endpoint拼接成片URL，不能改成内网域名后把生成的内网地址交给FBBM。

运行保持现有CPU编码、`medium`与串行队列。16 vCPU不会自动启用双任务并行；采购方案中的双泳道、耗时和达成率属于尚未验证的计划，不能作为本实例交付结果。

## 构建与验收

使用本地已提交的干净代码 `8ad5c565ecc806f1f250309da20b4a0925fa6a43`。`deploy` 下接收代码；实际构建工程为root所有的 `/srv/exactcut/preparation/engineering`。root读取deploy所有的Git工作区会触发dubious ownership检查，因此另建独立发布工程，不关闭Git安全检查。部署管理器、版本记录、回滚约定沿用[Linux迁移文档](linux-migration.md)。

正式版本 `us-east-20261009-1`，固定镜像 `sha256:3986618e599deb32d6edf6c65ba5a2b10d553e9686444bde15915807d6d889f7`，由发布管理器构建、验收并上线；数据schema保持1。

- 新机Linux完整回归343passed/5skipped，66.19秒，测试容器限制1CPU/2GiB；无生产配置或数据挂载、外部调用开关关闭。缺Docker CLI/Node的五项在本地已覆盖，本地348passed、无跳过。
- 外部检查全部通过：OSS目标桶/来源桶各读取32MiB，12.811/12.764MiB/s；AI小型Responses结构化输出、飞书字段类型及状态选项、FBBM可用性均通过。未上传、创建飞书记录或执行素材发布；抽样吞吐不能作为完整视频下载承诺。
- nginx正式入口为 **http://8.221.122.75/exactcut/**，根路径跳转到工作台路径；后端仅监听 `127.0.0.1:18792`。站点 `/etc/nginx/sites-available/exactcut` include `/etc/nginx/snippets/exactcut-production.conf`，由管理器生成location。原默认欢迎页配置已备份，正式配置语法检查通过后reload。
- 发布后healthz为ok、production、目标版本、worker_alive=true、maintenance=false。Docker实际CPU额度14，内存25769803776字节（24GiB）。
- 从本机通过公网入口验证登录、Cookie Path、工作台、静态脚本、模板、生产缓存策略及下载/AI/发布开关。初始批次数为0，账号库仅新建admin；首次工作台初始化建立一个默认项目，再次进入返回同一项目。没有创建制作或发布任务。

管理员为 `admin`，系统生成独立随机密码，位于服务器 `data/.web-state/password`（0600）。密码不写入文档、命令参数或Git。用户可在终端执行：

```sh
ssh -t deploy@8.221.122.75 'sudo cat /srv/exactcut/data/.web-state/password'
```

首期完整启用下载、剪辑、AI、飞书/FBBM发布，保留逐条预览确认。此轮未在新机执行真实多集业务制作或发布，不能将外部只读检查当作新机全流程写入验收。已有任务文件日志与Docker轮转日志保留；此前要求的新增集中诊断文件日志仍未实现。
