# 原片可用性提示补充发布验证

> 历史归档（2026-11-04）。正文保留当时的方案或执行状态，不代表当前版本；日常操作见[文档入口](../../README.md)。

用户要求将今天遗漏的代码改动一起发布。核对 main、全部分支今天的提交和未提交差异：已有播放/抽帧/识别诊断代码已进入正式33b0d1b，遗漏的是 web/app.js 与 web/index.html 的原片查询提示及下载完成提示。无关未跟踪历史文档、截图不作为应用代码打包。

行为：输入有效剧号后600ms合并查询；显示已有集数、完整列表中缺失的时间线集数；列表可用才启用下载按钮；查询失败支持重试；弹框复用已查询目录，显式刷新重新读取。列表契约来自 originals.assemble/load_originals 及现有 HTTP 测试，没有新增接口字段。

发布前复现并修正两项问题：源桶不可读时的部分目录不能断言其他集不存在；弹框请求返回时必须仍是同项目/剧号，不能将旧目录套到新项目。空目录提示也不再断言必定制作失败，因为已有本地原片不由远端列表决定。

新增6项浏览器测试覆盖可用目录/缓存、空目录与错误区别/重试、剧号迟到响应、部分目录、编辑后缺集提示、跨项目弹框迟到响应。两个问题先复现失败再修正。原片HTTP、查询UI、文本编辑相关合计29 passed（11.14秒）；Node语法及git diff检查通过。完整回归和正式发布结果另追加。

## 正式发布结果

- 2026-10-28 21:43（Asia/Shanghai），通过 linux-ops 发布 `us-east-20261028-originals-7960491`；提交 `79604912a71d5224a42fa6717c8fb8ae0b0a6ffd`。上一版本 `us-east-20261028-editor-oss-33b0d1b`。
- 干净工作树完整回归：1729 passed、1 skipped、10 warnings，398.06秒；Linux隔离回归1531 passed、199 skipped、10 warnings，344.13秒。Linux无浏览器能力的跳过项不计通过，本地覆盖。
- Git bundle SHA256：206c307329db5dbe2807ece55be19d02e8bd1e083f039c1b65ee67e3ee64c60d。
- 镜像：sha256:3019f65c2fe54d39ded9d83c9cb78430431ced08041ce31eefaed9d4af7e14cd。Linux测试日志SHA256：838f464a473e7f3e6f753bb487c7d311c2acef48a5284580a5b8b630e9a6f8d6。
- check-switch allowed=true、active_tasks=[]、phase_blockers=[]；管理器维护屏障内切换成功。公网health为新release、status=ok、worker_alive=true、maintenance=false。
- runtime.json、deployment.json、production.env前后SHA256一致，OSS编辑播放开关保留。测试容器始终exited，没有重启。
- 真实Chrome已登录工作台：剧101494自动显示“有25集原片（EP1–EP25）”，下载按钮启用；打开弹框有25集，点击刷新后仍ready/25集。app.js版本前缀d766367e2241b3c78d3f与候选文件SHA256一致。未下载文件、未修改草稿或触发制作/交付；自建标签页已关闭。
- 封存 `/srv/exactcut/releases/us-east-20261028-originals-7960491/`；构建/发布/门禁/配置哈希记录 `/srv/exactcut/preparation/release-originals-7960491/`。
- 需要代码回滚时使用 `/srv/exactcut/bin/linux-ops --root /srv/exactcut rollback us-east-20261028-editor-oss-33b0d1b`，仍需任务排空和兼容性门禁，不回滚数据。
