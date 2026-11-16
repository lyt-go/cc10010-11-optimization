# 数据仓库ETL任务编排平台

面向数据仓库ETL任务开发、作业编排调度、任务依赖解析、源端数据校验、异常任务重跑、血缘分板与运行全景监控的ETL工程管理平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| ETL作业 | `etljob` | 作业定义 | 作业编号、作业名称、源端数据表 |
| 任务编排 | `dag` | 编排图 | 图编号、图名称、拓扑结构 |
| 依赖解析 | `dependency` | 依赖关系 | 关系编号、上游作业、下游作业 |
| 源端校验 | `sourcecheck` | 校验配置 | 配置编号、源端数据表、行数下限 |
| 运行实例 | `runtime` | 运行记录 | 记录编号、作业名称、开始时间 |
| 异常重跑 | `rerun` | 重跑任务 | 任务编号、原始运行编号、重跑区间开始 |
| 基线管理 | `baseline` | 基线定义 | 基线编号、基线名称、承诺完成时间 |
| 分区检测 | `partitioncheck` | 检测规则 | 规则编号、数据表名、期望分区数 |
| 数据比对 | `comparator` | 比对任务 | 任务编号、源表名、目标表名 |
| 元数据存储 | `metastore` | 元数据表 | 表编号、表名、所属数仓层 |
| 资源配置 | `resource` | 资源池 | 池编号、池名称、最大并发数 |
| SLA监控 | `slavalidate` | SLA规则 | 规则编号、适用作业、完成截止时间 |
| SQL模板 | `sqltpl` | 模板定义 | 模板编号、模板名称、模板类型 |
| 数据备份 | `backuprestore` | 备份策略 | 策略编号、数据表名、备份周期 |
| 日志归档 | `logarchive` | 归档策略 | 策略编号、日志类型、保留天数 |
| 通知渠道 | `notifychan` | 渠道配置 | 渠道编号、渠道类型、接收地址 |
| 发布计划 | `publishplan` | 发布单 | 发布编号、发布内容、变更说明 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 阻断结论快照

源端校验的复合规则（行数、空值、新鲜度、分区完整性）不再各自压成一个状态，而是按表归并
成一份不可变的**阻断结论快照**，执行模块只读取这份快照：

- **批次归并**：每张表可分多批提交规则结果（`规则版本` 单调递增）。归并以规则为粒度：
  规则版本高者优先；同版本内阻断级别高者优先，较晚到达的告警不会覆盖较早的阻断结论；
  冲突双方的口径都留在 `冲突说明` / `被替代命中` 里，命中依据不丢。
- **稳定快照**：每次重算生成的 `结论版本` 单调递增；内容未变时复用同一快照。内容相同的
  批次重复提交只返回一次结论（`duplicate: true`），不会多出版本。
- **配置关闭**：关闭或重新启用配置后，下一次读取 / 下一次运行必定重算，不复用旧结论；
  已关闭时不再给出“通过”，命中结果转入 `参考命中规则` 留档。
- **写入失败**：写入失败只记录失败现场，上一条完整快照继续对外提供；相同批次再次提交会
  补算，成功后失败记录标记为已重试。
- **执行模块**：`GET /api/runtime/{id}/blocking` 读快照并把 `快照编号 / 结论版本 / 规则版本 /
  命中原因` 回写运行侧清单；配置页 `GET /api/sourcecheck/config-summary/{table}` 与执行模块
  读的是同一份快照，命中规则和阻断级别一致。
- **空态**：没有配置、没有批次、没有可用规则结果时返回 `missing: true` 并说明原因，
  启动类动作会被拦下，不会把“缺失结论”当成通过。

相关接口：

| 接口 | 说明 |
| --- | --- |
| `GET /api/blocking/snapshots` | 每张表当前生效的阻断结论 |
| `GET /api/blocking/snapshots/{table}` | 单表当前结论（缺失时给原因） |
| `GET /api/blocking/snapshots/{table}/history` | 结论版本历史 |
| `POST /api/blocking/batches` | 按表提交规则结果批次 |
| `POST /api/sourcecheck/batches` | 同上（源端校验入口） |
| `POST /api/sourcecheck/retry` | 校验失败 / 异常中断后按表重算 |
| `GET /api/sourcecheck/config-summary/{table}` | 配置页的阻断口径 |
| `GET /api/runtime/blocking` | 运行侧清单读取到的结论版本 |
| `POST /api/runtime/{id}/actions` | `应用阻断结论` / `重试阻断校验` |

回归测试：`cd backend && .venv/bin/python -m unittest discover -s tests`

领域：数据工程
子方向：数仓与数据湖
