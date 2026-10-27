# P0 设计方案：单 Agent + Tool Calling

> 历史设计文档。当前实现、已修复问题与尚未验收能力以 [实现状态](AGENT_IMPLEMENTATION_STATUS.md) 为准；下列旧“已实现/实测”描述不能作为本轮验收结果。

状态：**已确认并实现（2026-10-19）**——路线：DeepSeek（OpenAI 兼容协议）· 待确认队列审批 · 全量写操作审批 · 晨报定时+手动
范围：把 LLM 接进 tracker 系统，实现最小 Agent 闭环（工具层 / 上下文注入 / RAG v0 / 写操作审批 / 晨报 / 评测埋点）
原则：零新增依赖、复用现有表结构、写操作必人审、评测埋点从第一天就有

---

## 1. 目标与非目标

**目标（P0 交付）**
1. 一个跑在系统内的单 Agent（原生 function calling，OpenAI 兼容协议，零新增依赖）
2. 10 个标准化工具（7 读 + 3 写），读写权限分级，写操作走审批
3. 结构化上下文注入（记忆 v0）：持仓/监控/通知摘要 + 仓位风控规则
4. RAG v0：四大框架 + 三档决策门规则入库，FTS5 检索
5. `/agent` 聊天页（含工具调用时间线 + 审批卡片）+ 每日晨报
6. 评测埋点：agent_runs / agent_tool_calls 记录 token、耗时、成本、状态

**非目标（P1+）**
- 多 Agent、事件总线、工作流编排、embedding 检索、模型路由、流式输出、跨会话长期记忆

---

## 2. 架构总览

```
┌─────────────── Web UI：/agent 聊天页 ───────────────┐
│  聊天消息流 + 工具调用时间线 + 审批卡片 + 晨报按钮   │
└───────────────┬──────────────────────────┬─────────┘
        POST /api/agent/chat      POST /api/agent/pending/<id>/resolve
                │                          │
                ▼                          ▼
┌───────────────────────── Agent Runtime ──────────────────┐
│  单 Agent 循环（≤8 轮）：LLM ↔ 工具执行                    │
│  · 系统提示 = 身份 + 三档门规则摘要 + 上下文注入(记忆v0)  │
│  · 读工具直接执行；写工具 → 拦截 → pending_actions        │
│  · 每轮记录 agent_tool_calls；每次运行记录 agent_runs      │
│  · 护栏：迭代上限 / 每日 token 预算 / 429、5xx 重试       │
└──────┬──────────────────────────┬────────────────────────┘
       ▼                          ▼
┌─ Tool 层（10 个）─┐      ┌─ RAG v0 ─────────────┐
│ 读×7（自由调用）   │      │ 5 份框架/规则文档     │
│  query_portfolio  │      │ → 剥frontmatter      │
│  query_recommend… │      │ → 按##切chunk        │
│  query_catalysts  │      │ → knowledge_chunks   │
│  get_quotes       │      │ + FTS5 检索 top-k    │
│  get_sector_sig…  │      └──────────────────────┘
│  search_knowledge │
│  get_market_cale… │      晨报任务：只挂读工具（场景级权限）
│ 写×3（必审批）    │      定时：复用 ECS worker 05:20 数据刷新后
│  propose_recomm…  │      产出：写通知中心 + agent 页展示
│  update_monitor   │
│  send_notification│
└───────────────────┘
```

---

## 3. Tool 层设计（46 路由 → 10 工具）

每个工具定义：JSON Schema（参数）、权限级别（read/write）、超时、降级行为、结果摘要函数（供时间线与埋点展示）。

### 3.1 读工具（7 个，直接执行）

| 工具 | 参数 | 数据源 | 说明 |
|---|---|---|---|
| `query_portfolio` | 无 | broker_positions + recommendations + 最新快照 | 每票成本/数量/最新价/盈亏% + 账户现金与市值概况 |
| `query_recommendations` | status, entry_rec, theme, limit | recommendations | 推荐与观察清单，含 wait_price/validity_date/监控状态 |
| `query_catalysts` | ticker?, days | catalysts + catalyst_tickers | 未来催化剂事件 |
| `get_quotes` | symbols[] | Longbridge API | 实时行情；未配置凭据→降级返回 DB 最新价并注明"非实时" |
| `get_sector_signals` | 无 | ai_infra_monitor + 主题数据 | AI 基建信号（score/5）+ 板块轮动概览 |
| `search_knowledge` | query, k=5 | knowledge_chunks(FTS5) | 检索框架文档/决策门规则/知识文章 |
| `get_market_calendar` | days | catalysts + macro_events (+Longbridge calendar) | 未来 N 天财报/宏观日历 |

### 3.2 写工具（3 个，一律先入审批队列）

| 工具 | 审批后动作 | 三档门校验 |
|---|---|---|
| `propose_recommendation` | 落 recommendations 表 | "可入"必填六要素（ticker/entry_price/止损或目标价/wait_price/监控/操作说明）；"观察"必填触发条件+validity_date；schema 不过→把错误返回给 LLM 让它补全 |
| `update_monitor` | rec_id + action(start/pause/snooze/close) | 校验记录存在与状态合法迁移 |
| `send_notification` | 写 notifications 表 | 标题/正文长度限制 |

> 关键治理设计：**工具权限随场景分级**——聊天会话挂全量工具（写走审批）；晨报等定时任务只挂读工具。LLM 永远接触不到 Longbridge 凭据，所有工具服务端执行。

> 好消息：recommendations 表已内置三档门字段（entry_rec / wait_price / wait_condition / validity_date / conviction / stop_loss_price / target1/2），**无需表结构变更**。

---

## 4. Agent Runtime（tracker/agent/runtime.py）

- **协议**：OpenAI 兼容 `POST {LLM_BASE_URL}/chat/completions`，原生 `tools` 参数；用现有 `requests` 实现，**零新增依赖**
- **循环**：LLM 响应含 tool_calls → 逐个执行（P0 串行）→ 结果回传 → 下一轮；无 tool_calls → 输出最终答案；上限 8 轮
- **护栏**：`AGENT_DAILY_TOKEN_BUDGET`（超预算拒绝运行）、429/5xx 指数退避重试 2 次、单工具超时 30s
- **埋点**：`agent_runs`（model/prompt_tokens/completion_tokens/latency_ms/status/error）+ `agent_tool_calls`（tool_name/args/status/duration_ms/result_summary）——P2 评测的数据从第一天就有
- **降级**：LLM 未配置 → /agent 页面与 API 返回"未配置 LLM"提示，其余系统不受影响（沿用 LongbridgeNotConfigured 模式）

---

## 5. 上下文注入：记忆 v0（tracker/agent/context.py）

`build_context()` 每次运行时构建 Markdown 上下文块（预算约 1500 token）：

| 块 | 内容 | 来源 |
|---|---|---|
| 账户概况 | 现金、总市值、持仓数、模拟/实盘说明 | broker 数据 |
| 持仓清单 | ticker/成本/最新价/盈亏%/conviction（超 N 行截断） | DB |
| 监控中项 | triggered/approaching 优先，含 wait_price | recommendations |
| 近期通知 | 近 7 天通知标题摘要 | notifications |
| 风控规则 | 单票≤20%、conviction→仓位映射、三档门摘要 | 静态规则文本（源自闭环规则文档） |

系统提示 = Agent 身份定义 + 风控/决策规则摘要 + build_context() 结果 + 工具使用指引。会话内历史消息作为短期记忆一并提交（按条数截断）。

---

## 6. RAG v0（tracker/agent/knowledge.py + ingest CLI）

- **摄取**：`python -m tracker.agent.cli ingest` 解析 5 份文档（四大框架 + 推荐录入与执行闭环规则.md）→ 剥离 AIGC frontmatter → 按 `##` 标题切块 → 写入 `knowledge_chunks`（title/source/section/content），同时把 `knowledge_articles` 现有文章并入 FTS 索引
- **检索**：SQLite FTS5 `MATCH` + bm25 排序取 top-k，返回 title + 截断 content
- **升级路径**：`retrieve(query, k)` 是唯一检索入口函数，P1/P2 可整体替换为 embedding 检索，上层不动

---

## 7. 写操作审批流（pending_actions）

```
Agent 调用写工具
   → Runtime 拦截 → 写 pending_actions(status=pending, args, 摘要)
   → 返回给 LLM："已创建待审批操作 #N：<摘要>，等待用户确认"
   → LLM 告知用户 → /agent 页显示审批卡片（批准/拒绝）
   → 用户批准 → 执行工具 → 结果写 result_json + 通知中心发一条确认
   → 用户拒绝 → status=rejected，Agent 下轮对话可见结果
```

- 审批卡片在聊天流内联展示 + 待办数量角标；手机端可用（沿用现有移动端适配）
- 拒绝信息回流给 Agent（下轮上下文），形成"人否决 → Agent 学习"的闭环叙事

---

## 8. 晨报（tracker/agent/morning.py）

- 固定任务提示：隔夜关键变化 → 持仓今日关注点 → 监控接近触发项 → 今日日历 → **3 个必须关注的行动点**（带数据引用）
- **只挂读工具**（治理分级）；生成结果写 notifications（标题前缀 [晨报]）+ agent 页"最新晨报"区
- 触发：① `/api/agent/morning_report`（X-API-Key，供 curl/外部调用）② /agent 页手动按钮 ③ ECS worker：05:20 数据刷新成功后自动调用（`AGENT_MORNING_ENABLED=1` 且 LLM 已配置且未超日预算）

---

## 9. Web UI（/agent）

- 新模板 `agent.html`（沿用 base.html 导航与现有样式风格）：
  - 聊天流：用户消息 / Agent 回答 / **工具调用时间线**（🔧 工具名 → 参数摘要 → 结果摘要 → 耗时）
  - 审批卡片：内联批准/拒绝按钮
  - 顶部操作：生成晨报、新建会话
- 交互为非流式（POST 一次返回完整 trace，前端渲染时间线）——工具调用过程全程透明，演示效果好；流式留 P1
- 会话持久化：`agent_chats` + `agent_messages`，支持多会话

---

## 10. 数据库变更（init_db 内追加，全部 IF NOT EXISTS）

```sql
CREATE TABLE IF NOT EXISTS agent_chats (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id INTEGER NOT NULL,
  role TEXT NOT NULL,          -- user/assistant/tool
  content TEXT DEFAULT '',
  tool_calls_json TEXT DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id INTEGER,
  trigger TEXT NOT NULL,       -- chat/morning
  model TEXT, prompt_tokens INTEGER, completion_tokens INTEGER,
  latency_ms INTEGER, status TEXT, error TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_tool_calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER NOT NULL,
  tool_name TEXT NOT NULL,
  args_json TEXT DEFAULT '',
  status TEXT, duration_ms INTEGER, result_summary TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pending_actions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id INTEGER, run_id INTEGER,
  tool_name TEXT NOT NULL,
  args_json TEXT NOT NULL,
  summary TEXT DEFAULT '',
  status TEXT DEFAULT 'pending',   -- pending/approved/rejected
  result_json TEXT DEFAULT '',
  created_at TEXT NOT NULL,
  resolved_at TEXT
);
CREATE TABLE IF NOT EXISTS knowledge_chunks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL, source TEXT NOT NULL,
  section TEXT DEFAULT '', content TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
  title, content, content='knowledge_chunks', content_rowid='id'
);
```

> libSQL/turso 兼容性：FTS5 在部署环境若不可用，降级为 `LIKE` 关键词检索（检索入口函数统一封装，实现时验证）。

---

## 11. 配置项（.env 追加，均带默认值）

```
LLM_BASE_URL=https://api.deepseek.com/v1   # OpenAI 兼容端点
LLM_API_KEY=                               # 未配置=优雅禁用
LLM_MODEL=deepseek-chat
LLM_TEMPERATURE=0.3
AGENT_MAX_ITERATIONS=8
AGENT_DAILY_TOKEN_BUDGET=200000
AGENT_MORNING_ENABLED=1
```

---

## 12. 文件布局与改动清单

**新增（~1300 行）**
```
tracker/agent/__init__.py
tracker/agent/config.py        # 环境变量读取与校验
tracker/agent/context.py       # 记忆 v0
tracker/agent/knowledge.py     # FTS 检索 + 文档摄取
tracker/agent/tools.py         # 10 工具注册表（schema/权限/执行/摘要）
tracker/agent/runtime.py       # 单 Agent 循环 + 审批拦截 + 埋点
tracker/agent/morning.py       # 晨报任务
tracker/agent/cli.py           # ingest / 冒烟测试命令
tracker/templates/agent.html
tracker/tests/test_agent.py    # 工具schema/上下文/审批流/循环(mock LLM)/FTS
```

**修改（小改）**
- `app.py`：init_db 追加 6 表 + 6 个路由（/agent 页、chat、pending 列表/resolve、morning_report、chats）
- `ai_monitor_worker.py`：05:20 刷新成功后调晨报（开关控制）
- `requirements.txt`：不变（零新增依赖）

---

## 13. 验收标准（demo checklist）

1. /agent 问"我的组合今天有什么变化" → Agent 调 query_portfolio + get_quotes，回答带真实数据，时间线可见工具调用
2. 问"MU 还值得买吗" → search_knowledge 命中供应链框架，回答引用框架逻辑而非泛泛而谈
3. 让 Agent"把 VECO 录入为观察，触发价 $78" → 生成审批卡片 → 批准后落库，推荐页可见
4. 点"生成晨报" → 含 3 个行动点、有数据引用，落入通知中心；ECS 定时链路跑通
5. agent_runs 表可见每次 token/耗时/状态；单次晨报成本 ≈ 几分钱量级
6. 未配置 LLM_KEY 时页面优雅提示，其余功能不受影响
7. pytest 全绿（工具 schema 校验、审批流生命周期、mock LLM 的循环上限、FTS 命中与 frontmatter 剥离）

---

## 14. 风险与应对

| 风险 | 应对 |
|---|---|
| LLM 幻觉给出错误投资结论 | 工具返回真实数据 + 系统提示强制"结论必须引用工具数据"；写操作必审批 |
| FTS5 在 libSQL 不可用 | 检索入口统一封装，降级 LIKE 检索 |
| token 成本失控 | 日预算护栏 + runs 埋点 + 晨报用默认便宜模型 |
| 写操作参数不合法 | 三档门 schema 校验失败→错误回传 LLM 自动补全重试 |
| 部署环境（Vercel）超时 | 聊天循环有 8 轮上限；Vercel 场景晨报走外部 cron 调 API（已留 X-API-Key 端点） |

> AI生成
