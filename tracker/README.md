# Investment Tracker / 自用投资 Agent

投资记录、持仓与事件追踪，加上可审批的研究助手。实现边界和评估方法见 [AGENT_IMPLEMENTATION_STATUS.md](AGENT_IMPLEMENTATION_STATUS.md)。P0/P1/P2 为演进设计记录。

## 本地运行

建议 Python 3.11/3.12，在 tracker 目录操作：

```sh
python -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
cp .env.example .env
```

在 .env 配置模型、长桥和应用凭证。不要提交 .env 或账户数据库。LLM_API_KEY 未设置时保留业务页面，Agent 不执行。账户信息与检索内容会发送给所配置的模型服务，请只连接你选择并信任的服务。

Web 与 Worker 都从 tracker 目录启动：

```sh
.venv/bin/gunicorn --preload --bind 127.0.0.1:5050 --workers 2 --timeout 120 wsgi:app
```

```sh
.venv/bin/python ai_monitor_worker.py
```

Worker 启动会初始化缺失的表并摄取框架知识，随后处理后台队列。只启动 Web 时请求会排队，页面会提示检查 Worker。正式数据库先备份再启动新版本，新增列为增量迁移。停止正在执行的 Worker 后，该任务租约过期会标记失败，请检查已批准动作后重新创建任务。

## Docker

在 tracker 目录执行 `docker compose up --build -d`。构建上下文为仓库根目录，Dockerfile 为 tracker/Dockerfile；根目录 .dockerignore 排除凭证、数据库和临时资料。默认访问 127.0.0.1:5050。Web 与 Worker 共用持久卷、模型环境配置；Turso 配置后两者共用远端库。

Compose 要求显式配置 SECRET_KEY、APP_USERNAME、APP_PASSWORD。它不会导入你的现有账户数据库，请自行备份并按数据迁移说明处理。`migrate_to_turso.py` 是覆盖式复制工具，不会自动执行，操作前分别备份源与目标。

## 调用约定

- `POST /api/agent/chat`：message + 可选 chat_id → 202 task_id。
- `POST /api/agent/workflows/review/run` / `research/run`：入队，不同步运行。
- `GET /api/agent/tasks/{id}`：状态、步骤、工具摘要与结果。
- `POST /api/agent/tasks/{id}/cancel`：取消任务；已批准动作不会回滚。
- `POST /api/agent/pending/{id}/resolve`：action=approve/reject，可带 reason。
- 晨报和事件扫描接口支持 X-API-Key；浏览器调用仍验证 CSRF 与应用登录。其他修改接口使用应用登录与 CSRF。

本地网页提供聊天、晨报、立即复盘、事件扫描、任务详情与取消；候选研究还可通过工作流接口触发。

## 验证

先运行只读预检：

```sh
.venv/bin/python -m agent.cli doctor
.venv/bin/python -m agent.cli doctor --network
```

默认仅检查配置；`--network` 查询一条公开行情和数据库表结构，不初始化表、不读取持仓、不调用模型。服务令牌过期、缺少新表等会明确报告，未就绪时退出码为1。真实模型测试仍须使用合成数据，不能把配置检查当成链路测试。

2026-10-19 联调：DeepSeek 合成工具调用正常，4个合成输出契约案例通过；Turso连接正常但缺少Agent新表；长桥返回401003（令牌过期）。未修改远端库或凭证。完成令牌更新、数据库备份和新版本初始化后再启动真实账户任务。

```sh
.venv/bin/python -m pytest -q
```

测试使用临时数据库，禁止真实网络请求，不会调用付费模型。可运行历史推荐评估或输出契约评估，详见实现状态文档；它们不是投资建议或收益保证。

Vercel 的 Web 部署不能自行保持常驻 Worker；需要独立 Worker 连接相同的持久数据库。本项目的推荐自用部署为单个 Docker Compose 实例。
