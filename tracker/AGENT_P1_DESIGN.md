# P1 设计方案：事件驱动 + 工作流 + 任务状态机

> 历史设计文档。当前实现、已修复问题与尚未验收能力以 [实现状态](AGENT_IMPLEMENTATION_STATUS.md) 为准；下列旧“已实现/实测”描述不能作为本轮验收结果。

状态：**已确认并实现（2026-10-19）**——事件扫描 纽约 09:00/16:20（DST 感知，≈北京时间 21-22 点 / 次日 04-05 点）· 审批拒绝后修正重跑限 1 次 · 模型路由框架先行（模型名后填）
前置：P0 已完成（单 Agent + 10 工具 + 审批 + RAG v0 + 埋点），代码在 tracker/agent
目标：把 Agent 从"用户问了才干活"升级为"事件来了主动干活"，并把 P0 的监控状态机泛化为通用任务状态机
原则：继续零新增基础设施依赖（不引入消息队列/编排引擎）、复用现有 ECS worker 定时器、每个新能力都带评测埋点

---

## 1. 目标与非目标

**目标（P1 交付）**
1. 事件驱动：行情/日历/复盘三类事件自动触发 Agent 任务（数据库做事件队列，不引入 MQ）
2. 三个可复用工作流：晨报流（已有雏形，正式化）、复盘流、候选研究流——流程定义可配置化
3. 多 Agent 分工路由：监控 Agent（高频轻量）/ 研究 Agent（低频深度）/ 复盘 Agent（周期）
4. 任务状态机：把监控 7 态泛化为通用任务状态机（queued→running→waiting_approval→succeeded/failed）
5. 审批治理增强：任务级审批（pending 关联 task）、拒绝→修正重试（限一次）→归档
6. 任务级评测埋点：耗时/成本/审批结果/有效率，为 P2 指标体系铺路

**非目标（P2+）**
- 多 Agent 并发执行与智能路由（P1 是"分工路由"不是并发）
- 可视化编排拖拽、可配置化平台界面
- 向量检索升级 embedding
- 在线评测看板

---

## 2. 架构总览

```
┌─ 事件源 ──────────────────────────────────────────────┐
│ ① 定时扫描（复用 ECS worker，纽约 09:00 盘前 / 16:20 收盘后）│
│    - 日历到期（review_date / validity_date）           │
│    - 财报/事件临近（catalysts 未来 N 天）               │
│    - 价格条件接近（monitor 状态 approaching/triggered）│
│ ② 聊天页/API 手动触发（候选研究、立即复盘）            │
└──────────────────────┬────────────────────────────────┘
                       ▼
┌─ 事件总线 agent_events（SQLite 表，零依赖）────────────┐
│  事件去重（同 ticker 同类型 24h 不重复）               │
│  状态：new → processed / dropped                       │
└──────────────────────┬────────────────────────────────┘
                       ▼
┌─ 任务编排 runtime ─────────────────────────────────────┐
│  agent_tasks 任务实例（workflow 类型 + 状态机）         │
│  workflow 定义（代码中集中声明，可配置）                │
│  步骤执行：prompt + 工具集 + 三档门校验                 │
└──────────┬────────────────────────┬───────────────────┘
           ▼                        ▼
┌─ Agent 分工（按任务类型路由）────┐  ┌─ 治理 ──────────────┐
│ monitor-agent  小模型 读工具     │  │ 三档门校验         │
│ research-agent 强模型 读写工具   │  │ 待审批队列(任务关联) │
│ review-agent   中模型 读写工具   │  │ 拒绝→修正重试(1次)  │
└──────────┬──────────────────────┘  │ 审计：全链路留痕    │
           ▼                        └────────────────────┘
     产出 → 审批 → 落库/通知/监控更新 → 任务状态机流转
```

---

## 3. 事件层设计（track/agent/events.py）

### 3.1 事件类型

| 事件 | 触发源 | 数据 | 防重规则 | 对应任务 |
|---|---|---|---|---|
| `review_due` | review_date 到期（观察项） | rec_id | 同 rec 30 天内不重 | review 流 |
| `calendar_soon` | catalyst 事件未来 7 天内 | event_id | 同 event 一次 | monitor 提醒（只读分析） |
| `price_approaching` | monitor 状态进入 approaching/triggered | rec_id | 状态变化才发，同态不重发 | monitor 流（判断是否要提醒用户） |
| `manual_research` | 聊天页手动 | ticker/指令 | — | 候选研究流 |
| `manual_review` | 聊天页手动 | — | — | 复盘流（全组合） |

防打扰原则延续 P0：**相同状态没有新证据不重复提醒**（事件生成时做幂等检查）。

### 3.2 为什么用 SQLite 表做事件队列而不是 Kafka/Celery

- 个人系统事件量级：每天个位数；现有 ECS worker 已是定时任务模型。
- 引入消息队列=新基础设施 + 运维负担 + 部署复杂度翻倍。
- **面试点**：这是"成本意识与方案收敛"的体现——MVP 用数据库当队列，等量级到千级/需要实时性再演进。

### 3.3 事件处理

- `scan_events(conn)`：worker 定时扫描数据 → 生成新事件（写 agent_events）
- `process_pending_events(conn)`：取 new 事件 → 按类型建任务 → 标记 processed
- 失败：事件置 retry（≤3 次）→ 超限 dropped + 写日志

---

## 4. 工作流编排（agent/workflow.py）

### 4.1 工作流定义（代码化声明，可配置）

三个工作流，每个由有序步骤组成，每步定义：`{id, agent_type, prompt模板, 挂载工具, 校验规则, 产出映射}`

**晨报流**（formalize P0）：
```
拉组合 → 逐持仓分析 → 扫日历 → 生成 3 行动点 → 发通知/入库
```

**复盘流**（新，本阶段核心）：
```
取到期推荐(含最新价/监控态) → 逐条判断(续/转可入/关闭) → 生成审批提案 → 审批 → 落库
```

**候选研究流**（新，手动触发）：
```
取 ticker+行情+日历 → 过框架检索 → 输出三档判定 → 可入则生成审批提案
```

### 4.2 为什么"代码化定义"而不是做可视化编排

- 三个流程，线性为主；代码化声明（dict 列表）200 行内搞定，可版本化、可测试。
- 配置化的价值在"多业务方复用"——那是平台化的 P2 命题，不提前背负。
- **面试点**：知道什么时候不引编排引擎（Airflow/Prefect 是重量级答案），知道配置化是平台化入口。

### 4.3 run_steps 执行器

- 顺序执行步骤，支持步骤内工具循环（复用 P0 loop）
- 每步产出校验：过三档门/必填字段 → 失败自动回填修正提示（1 次）
- 步骤之间共享上下文（组合快照透传）

---

## 5. Agent 分工与路由（agent/router.py）

### 5.1 为什么按"任务类型"切而不是"同时开三个 Agent"

| Agent | 频率 | 模型 | 挂载工具 | 任务 |
|---|---|---|---|---|
| monitor-agent | 高（事件触发） | 小/快模型（低成本） | 只读 7 个 | 价格/日历事件语义判断：值得提醒吗 |
| research-agent | 低 | 强模型 | 读 + 写（审批） | 候选研究深度分析 |
| review-agent | 周期 | 中模型 | 读 + 写（审批） | 到期观察复盘、每周误报复盘 |

- 路由规则：任务类型 → agent 类型 → 模型 → 工具集（`agent_types` 配置，兼容一个 key 多个模型）。
- **为什么不是并发**：同一时刻只处理一个任务，多 Agent 并发是 P2 的事——先证明"路由正确"，再优化"执行速度"。
- 模型路由的伏笔：配置 `LLM_FAST_MODEL` / `LLM_STRONG_MODEL`，一个 key 可切模型；P2 做成本看板时这个分流数据就是依据。

### 5.2 任务实例（agent_tasks）

```sql
CREATE TABLE IF NOT EXISTS agent_tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  workflow TEXT NOT NULL,            -- morning/review/research/monitor
  agent_type TEXT NOT NULL DEFAULT 'monitor',
  status TEXT NOT NULL DEFAULT 'queued',  -- 任务状态机
  event_id INTEGER,
  payload_json TEXT DEFAULT '{}',
  result_json TEXT DEFAULT '{}',
  prompt_tokens INTEGER DEFAULT 0,
  completion_tokens INTEGER DEFAULT 0,
  latency_ms INTEGER DEFAULT 0,
  attempt INTEGER DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
```

### 5.3 任务状态机（把监控 7 态泛化）

| 任务状态 | 含义 | 与监控 7 态的关系 |
|---|---|---|
| queued | 已入队未执行 | — |
| running | 执行中 | — |
| waiting_approval | 产出待人工确认 | ≈ triggered（等待用户确认） |
| succeeded | 完成（含被批准） | — |
| rejected | 被用户拒绝 | 对应 dismissed |
| retrying | 校验失败后修正重试（限 1 次） | 对应 revalidate（数据/逻辑需复核） |
| failed | 执行异常/超限 | — |
| cancelled | 手动取消/超时 | 对应 archived |

> **面试点：监控状态机 → 任务状态机 是一次"从领域模型到通用模型"的抽象**——同一套"等待人确认/需要复核/静默归档"的逻辑，从价格监控平移到任意 Agent 任务。

---

## 6. 审批治理增强（复用 P0 pending_actions）

1. `pending_actions` 增加 `task_id` 列，审批卡片带任务上下文（哪个流程/哪个 rec）
2. 拒绝流程：审批拒绝 → 任务进入 `rejected`，把拒绝理由回传给工作流 → 允许修正重跑（1 次）→ 再拒绝则归档
3. 待办聚合：页面"待审批"面板按任务分组，支持批量审（复盘流 6 条观察一次批）
4. 审计：任务、事件、审批、工具调用四张表可交叉回溯（p0 已埋工具调用）

---

## 7. 评测埋点（为 P2 铺路）

| 指标 | 来源 | P1 就产出 |
|---|---|---|
| 任务完成率 | agent_tasks.status | ✅ |
| 任务平均耗时 | agent_tasks.latency_ms | ✅ |
| 任务 token 成本 | agent_tasks.prompt/completion | ✅ |
| 审批通过率 | pending_actions.status 按 task 聚合 | ✅ |
| 事件有效率 | agent_events → task → 最终提醒（用户在通知点击率暂无） | ✅ 部分 |
| 误报率 | 事件处理后用户是否忽略（后续可加"忽略"按钮） | 部分 |

数据模型与 P0 一致：埋点即产品——**P2 只是把 P1 的数据出报表与基线，不需要回补埋点**。

---

## 7. 与现有系统的接缝

| 现有组件 | P1 改动 |
|---|---|
| ai_monitor_worker（ECS 纽约 09:00/16:20 定时） | 增加 scan_events + process_events 调用（16:20 收盘后数据刷新并入队晨报、09:00 盘前事件扫描分发），失败不影响主流程 |
| monitor 7 态状态机 | 保持现状不动；任务状态机独立新增（互不干扰），监控 7 态→事件映射写在 events 层 |
| 通知中心 | 工作流产出提醒沿用 add_notification；新增"事件驱动提醒"来源标记 |
| /agent 页面 | 新增"任务流"面板：任务列表 + 事件列表 + 审批卡片（已有）；右上角任务运行状态 |
| Longbridge | 行情/日历数据复用现有适配层（P0 的 get_quotes 已带降级） |

---

## 8. 新增/改动文件

```
tracker/agent/events.py      # 事件扫描/去重/处理（~200 行）
tracker/agent/workflow.py    # 三个工作流定义 + run_steps（~300 行）
tracker/agent/tasks.py       # 任务状态机 + 任务 CRUD + 路由（~150 行）
tracker/agent/router.py      # agent_types 路由配置（~60 行，可并入 tasks）
tracker/templates/agent.html # 待办/任务/事件面板（~150 行）
tracker/app.py               # +4 路由：/api/agent/events /api/agent/tasks /api/agent/workflows/<wf>/run /api/agent/events/scan
tracker/ai_monitor_worker.py # 增加事件扫描调用（~20 行）
tracker/tests/test_agent_p1.py # ~30 用例
```

预计新增 ~900 行（含测试），零新增依赖。改造成本小于 P0。

---

## 9. 验收标准（demo checklist）

1. 造一条 `review_date` 明天的推荐 → 手动 run scan → 自动生成复盘任务 → Agent 输出"转可入/继续/关闭" → 审批卡片 → 批准后落库，任务状态 completed
2. 拒掉一次审批 → 任务 rejected，理由回流给 Agent → 修正重跑限 1 次 → 再拒绝归档
3. 造一条 approaching 的 monitor → 事件触发 monitor-agent（只读）→ 页面通知"值得关注：xxx"且无幻觉数据
4. 晨报走 workflow 步骤化，手动触发一次
5. 页面待办面板：事件列表 / 任务状态机 / 审批分组
6. agent_tasks 表能看到耗时/token/审批结果；P2 指标数据源成立
7. 全部测试通过（含原 63 条回归）

---

## 10. 风险与应对

| 风险 | 应对 |
|---|---|
| 事件风暴（价格波动产生大量 approaching） | 事件去重 + 24h 冷却 + 同态不重复（防打扰原则） |
| 自动任务调用 LLM 成本超预算 | 复用 P0 预算护栏 + 事件扫描上限（每轮最多 N 个任务） |
| 任务与监控状态冲突（Agent 与规则同时动作） | P1 明确分工：规则引擎只负责"触发信号"，Agent 负责"判断与建议"，写库仍走审批 |
| 工作流步骤校验不过死循环 | 修正重试上限 1 次 → 超限标记 failed，人工可见 |
| 并发 worker 冲突（单实例保证） | 沿用 P0 单实例调度器（worker 同一进程内串行） |

---

## 11. 实现验证（2026-10-19）

- **自动化测试 81 条全绿**（原 63 条 + 新增 18 条：事件扫描与幂等、任务状态机全生命周期、审批拒绝→修正重跑→归档、工作流路由与 prompt、无 LLM key 时任务保持 queued、新路由）
- **真实库端到端**：扫描真实数据自动发现 6 只过期观察项（GLW/HUBB/NVT/LEU/MOD/POWL，validity 2026-10-16）→ 生成 review_due 事件 → 派发 review 任务排队，等 LLM key 配置后自动复盘
- **页面验证**：/agent 任务流面板正常渲染任务状态（排队/执行/待审批/完成徽章），扫描与手动复盘按钮可用
- **行为说明**：无 LLM key 时事件照常扫描派发，任务保持 queued 不丢失；配置 key 后 run_pending_tasks 自动消化

---

*本方案与 P0 的面试叙事线一致：P1 把"监控状态机泛化为任务状态机"、把"工具权限分级"扩展到"场景级路由"，全部是平台化叙事的具体验证。*

> AI生成
