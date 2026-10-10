# 基础路径改 /home + 管理员只读磁盘占用页 · 实施规范

日期：2026-10-10。分支 `concurrency-refactor`。数据 schema 保持 1。

## 1. 基础路径：默认改 `/home`，彻底去掉 `/exactcut`

- `source/config/default.json` 的 `web.base_path`：`""` → **`"/home"`**（这是"默认值"）。
- `source/config/production.json` 的 `web.base_path`：`"/exactcut"` → **`"/home"`**。
- 代码逻辑**无需改动**：`web_base_path()` 校验、`public_url()` 前缀、`normalize_path()` 去前缀、Cookie `Path`、登录跳转、`<base href>` 都由 `base_path` 驱动，`/home` 合法（正则 `(?:/[a-zA-Z0-9_-]+)+` 匹配）。
- 清理仓库/测试/文档里对 **`/exactcut`** 的引用，改为 `/home`（或说明由部署方处理）。**不要**再保留 `/exactcut` 的任何兼容/跳转。
- nginx 由编排者在服务器执行，**不在本次代码范围内**；但请核对 `deploy/linux_ops.py` 的 `nginx_config()` 对 `base_path="/home"` 输出正确（应生成 `location = /home {301 /home/}` + `location /home/ {proxy}` 片段）。
- 注意：**Cookie Path 变化会让已登录用户需要重新登录一次**——这是预期，文档里写明。

## 2. 管理员只读"磁盘占用"页面

### 后端
新增 `usage_report(root)`（可放 `storage.py`，沿用 `util`/`os.walk`；结果缓存 30–60s，避免每次全量扫描），返回：

```jsonc
{
  "totals": {"bytes": <数据根合计>, "free_bytes": ..., "total_bytes": ...},
  "projects": [   // 每个 batch 一个
    {"batch_id": "...", "name": "...", "owner": "producer06", "drama_id": "101460",
     "bytes": 123456, "assets": 6, "status": "review"}
  ],
  "users": [      // 按 owner 聚合
    {"username": "producer06", "projects": 2, "bytes": 234567}
  ],
  "shared": [     // 按剧的共享缓存（不归某个用户）
    {"drama_id": "101460", "sources_bytes": ..., "cache_bytes": ..., "episodes_bytes": ...,
     "referenced_by": ["producer06","admin"]}
  ]
}
```

- **归属规则**：`batches/<id>/`（成片/预览/请求/日志）算**项目占用**；项目归属 = `access.json` 的 `owner`（旧项目回退 admin）。`sources/<剧>/`、`projects/<剧>/cache`、`projects/<剧>/data/episodes`、`projects/<剧>/source/videos` 是**共享缓存**，按剧单列，`referenced_by` 列出引用了该剧的 batch 的 owner（用于判断"哪些用户在用它"），**不把共享缓存计入某个用户的私有占用**。
- **端点**：新增**管理员专属、只读** `GET /api/usage`（`is_admin()` 守卫）。保留现有 `/api/storage` 与清理入口不变。
- 大目录扫描要防重入/超时；结果缓存带生成时间。

### 前端
- 管理员导航（现有 `accountsNav`/`storageNav` 旁）新增**"用量"**入口，打开一个**只读页面/对话框**：
  - 顶部：数据根合计、磁盘剩余/总量；
  - **项目占用**表：项目名、用户、剧、素材数、占用；
  - **用户占用**表：用户名、项目数、占用；
  - **共享缓存（按剧）**表：剧、正片、基础缓存、镜头索引、引用用户；
  - **只读**：不放删除/清理按钮（现有"存储"弹窗的清理保持原样）。
- 排序按占用从大到小。

### 测试（TDD）
- 单元：`usage_report` 的聚合与归属正确（两个用户、两个 batch、一个共享剧）；**非管理员访问 `/api/usage` 返回 401/403**；缓存生效（同秒内不重复扫描）。
- 集成：临时数据根造出多用户多项目，断言三张表数字与合计。
- 前端：管理员可见、非管理员不可见（沿用现有 `accountsNav` 的隐藏方式）。

## 3. 约束

- 单机、单数据根、持久 JSON；不引入新依赖。
- **不改** `/api/storage` 的现有语义与清理流程。
- schema 保持 1；**不部署、不碰生产**；测试用临时数据根。
- **只 `git add` 自己的文件**；三个前端文件（本次需要改 `index.html`/`app.js` 以加"用量"页面）——**允许改这三个前端文件**（本任务明确要求 UI），但只做本任务相关改动，不要带其他无关变更。
- 完成后停下等复审。
