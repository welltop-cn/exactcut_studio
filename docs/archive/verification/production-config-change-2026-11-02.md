# 2026-11-02 生产 AI 限额与 OpenAI 配置变更

> 历史归档（2026-11-04）。正文保留当时的方案或执行状态，不代表当前版本；日常操作见[文档入口](../../README.md)。

本记录于 2026-11-03 根据原会话中的工具执行回执补录。以下时间均为北京时间（Asia/Shanghai）；备份文件名使用 UTC。本次补录未访问或修改生产服务器。

## 授权与范围

原会话中用户授权调整生产 AI 限额，随后授权同步本地默认值，并将本地 `source/.env` 中的 `OPENAI_API_KEY`、`OPENAI_BASE_URL` 同步到生产。实际操作只有下述配置及凭据变更，没有发布其他应用代码。

应用保持：

- commit：`8216c277f27fd88040944faae77a9e82a15be765`
- release：`us-east-20261031-bottom-subtitles-8216c27`
- 上次应用发布：[2026-10-31 正式发布记录](bottom-subtitles-production-2026-10-31.md)

## 17:32：共享 AI 请求额度

目标：服务器 `/srv/exactcut/config/runtime.json`，容器挂载路径 `/run/exactcut/runtime.json`。

| 字段 | 修改前 | 修改后 |
| --- | --- | --- |
| `concurrency.ai.requests_per_minute` | 6 | 60 |
| `concurrency.ai.burst` | 1 | 5 |

执行回执时间：2026-11-02 09:32:22 UTC。回执确认仅上述两项变化，文件 inode 保持不变。配置更新后的 SHA256：`93a82e35f1efb2c5d78ea66e77ac863f68f5dc6e05cbd48f7993903420db0219`。

原配置备份：

```text
/srv/exactcut/config/runtime.json.before-ai-60-5-20261102T093222Z
```

当时限流器每次调用重新加载配置，因此该步骤没有重启。09:32:30 UTC 的容器验证返回有效值 60/5，health 为 `ok`、`worker_alive=true`、`maintenance=false`，release 未变。

## 17:38：OpenAI 配置同步

目标：`/srv/exactcut/config/production.env`。仅替换：

- `OPENAI_API_KEY`
- `OPENAI_BASE_URL`

来源是当时获授权的本地 `source/.env`。不在文档中记录密钥或连接值；未来操作不能假设该本地文件仍与当时相同。

原环境文件备份：

```text
/srv/exactcut/config/production.env.before-openai-20261102T093812Z
```

执行回执记录 `operation=update_openai_environment`。此步骤重启了原生产版本，没有切换应用 release。

09:38:35 UTC 的最终核验：

- 两个字段与本地比对均为 `true`。
- 有效 AI 限额仍为 60/5。
- health `ok`、`worker_alive=true`、`maintenance=false`。
- release 仍为 `us-east-20261031-bottom-subtitles-8216c27`。

## 验证范围

已验证配置读回、凭据一致性和服务健康。此次操作后未使用新 key 发起真实 AI 请求，不能将配置成功解释为真实模型业务已验证。

这是当时的生产执行结果，不是 2026-11-03 重新读取的实时状态。

## 本地同步与尚未收尾的事项

随后本地两处也改为 60/5：

- `source/exactcut/concurrency.py` 的缺省限额。
- `source/config/production.json` 的生产模板。

原会话报告相关 76 项测试通过。2026-11-03 仓库审计确认，这两处仍是 main 工作区的未提交修改，尚无对应提交，不应视为已完成 main 整合，也不能当作来源不明的实验修改删除。

后续 D1/D2 并发候选曾按其余 OpenAI 调用 6/1 验收。最终整合需核对这次已授权的 60/5 与新版本预算适用范围，保留 Gemini、文本／Excel 脱离旧共享预算的独立需求，不能笼统声称候选与生产有效配置完全一致。

## 回退约束

本次未执行回退。若需回退，先读取当前配置与后续变更，再按授权恢复相关字段；不能直接以历史备份覆盖整份当前文件。凭据回退需按既有维护流程重新加载环境。配置回退不涉及业务数据库恢复。

## 原始证据

本机私有会话日志（可能包含敏感操作上下文，不复制进 Git）：

```text
~/.pi/agent/sessions/--<repo>--/2026-11-02T09-15-41-720Z_01a19b8b-0518-70ce-a846-6f518581f1e2.jsonl
```

关键工具回执 UTC 时间：`09:32:22`（限额修改与备份）、`09:32:30`（有效配置与健康）、`09:38:15`（环境更新操作）、`09:38:35`（字段比对及最终健康）。本记录仅提取非敏感事实。
