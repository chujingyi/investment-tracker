# P2 设计方案：评测运营体系

> 历史设计文档。当前实现、已修复问题与尚未验收能力以 [实现状态](AGENT_IMPLEMENTATION_STATUS.md) 为准；下列旧“已实现/实测”描述不能作为本轮验收结果。

状态：**已确认并实现（2026-10-19）**——7/30/60 全档位 · /agent 页评测面板 · DeepSeek 官方价成本核算 · 首次运行自动存基线
前置：P1 已完成（事件驱动 + 任务状态机 + 工作流），埋点表已有数据（11 runs / 60 tool_calls / 11 tasks / 16 events / 2 pending）
目标：让 Agent "好坏可度量、调优可回归"——分两层：离线评测（策略级 ground truth）+ 在线指标（过程级看板）
承诺：P0/P1 埋点已齐，**P2 只做指标与报表，不回补埋点**；零新增依赖

---

## 1. 目标与非目标

**目标**
1. 离线评测：用历史推荐 + 后验行情建评测集，量化"Agent/框架的推荐质量"
2. 在线指标：任务完成率 / 审批通过率 / 工具成功率 / 耗时 / 成本 五项在线看板
3. 回归机制：`python -m agent.cli eval` 跑评测并存基线，调 prompt/规则后可 diff 对比（"Agent 调优有回归测试"）
4. 成本核算：token × 可配置单价 → 每日/每周成本曲线

**非目标（P3+）**
- 在线 A/B 实验（推荐级灰度对比）
- 自动调参（评测驱动自动改 prompt）
- 多租户看板与权限

---

## 2. 数据底子（实测）

| 数据 | 量级 | 用途 |
|---|---|---|
| recommendations | 55 条（38 条有 ≥5 天快照） | 离线评测样本 |
| price_snapshots | 1352 条（2026-05-28 → 2026-08-06） | 后验行情 |
| agent_runs / tool_calls / tasks / events / pending | 11 / 60 / 11 / 16 / 2 | 在线指标 |

> 注意：快照在 2026-08-06 后未更新（线上刷新可能停摆）——离线评测以后验数据为准，同时这也是"监测数据新鲜度"指标的依据。

---

## 3. 离线评测（agent/eval.py）

### 3.1 评测集

每条评测样本 = 一条推荐（entry_rec ∈ actionable/wait/watch）+ 关联快照序列。
判定期：entry 后 T 天（T = 7 / 30 / 60，实际取快照可达的最大天数）。

### 3.2 指标定义（与投资域对齐，面试可量化）

| 指标 | 定义 | 怎么算 |
|---|---|---|
| 方向准确率 | 推荐后 7/30 天收盘 vs entry_price 方向 | pct_change 符号匹配 |
| 止损达成率 | 后验期内先触及 stop_loss_price | 快照 min ≤ stop |
| 目标达成率 | 后验期内先触及 target1_price | 快照 max ≥ target |
| 触发命中率 | wait_price 在后验期内被触及 | 快照 min/max vs wait |
| 平均持有收益 | 后验期末收益均值 | 分档统计 |
| 档位检验 | "可入"档 vs "观察"档 后验表现差异 | 分组均值对比 |

### 3.3 回归机制

- 评测结果写入 `eval_runs` 表（scope, metrics_json, baseline_flag, created_at）
- 首次运行自动存为**基线**；后续运行与基线 diff：方向准确率 ±X%、成本变化等
- CLI：`python -m agent.cli eval [--fresh]` → 打印报告 + 存档
- 面试金句："我的 Agent 调优有回归测试——改 prompt 前跑一次基线，改完再跑一次，指标说话。"

---

## 4. 在线指标（agent/metrics.py）

全部基于已有埋点表聚合，不新增采集。

| 指标 | 来源 | 聚合 |
|---|---|---|
| 任务完成率 | agent_tasks.status | 按 workflow：succeeded / 总数 |
| 审批通过率 | pending_actions.status | approved / (approved+rejected) |
| 工具成功率 | agent_tool_calls.status | ok / 总数（分工具） |
| 任务/工具耗时 | latency_ms | 均值 + P95 |
| 成本 | runs tokens × 单价 | 按日/按 workflow/按模型 |
| 事件有效率 | agent_events | new→processed 率、dropped 数 |
| 运行量 | runs/tasks 每日计数 | 时间趋势 |

成本单价可配置：`LLM_INPUT_PRICE_PER_M` / `LLM_OUTPUT_PRICE_PER_M`（默认 DeepSeek 官方：输入 ¥1/M、输出 ¥2/M，人民币）。

---

## 5. 页面

- `/agent` 页新增"评测"面板（与任务流并列）：
  - 概览卡片：任务完成率 / 审批通过率 / 今日成本 / 工具失败率
  - 按 workflow 的小表：任务数 / 完成率 / 平均耗时 / token / 成本
  - 离线评测摘要：方向准确率 / 目标达成率 / 触发命中率 + 与基线 diff 徽章
- 接口：`GET /api/agent/metrics`（在线聚合 + 最近一次离线结果）

---

## 6. 新增/改动文件

```
tracker/agent/eval.py        # 离线评测集 + 指标 + 回归（~250 行）
tracker/agent/metrics.py     # 在线指标聚合（~200 行）
tracker/agent/cli.py         # + eval 命令
tracker/agent/schema.py      # + eval_runs 表
tracker/app.py               # + /api/agent/metrics
tracker/templates/agent.html # 评测面板
tracker/tests/test_agent_p2.py
```

预计新增 ~700 行（含测试），零新增依赖。

---

## 7. 验收标准（demo checklist）

1. `python -m agent.cli eval`：对 38 条可评测推荐输出报告（方向准确率 7/30 天、止损达成率、目标达成率、触发命中率），并存基线
2. 再跑一次 eval：与基线 diff 明确（变化 ± 显示）
3. `/api/agent/metrics`：返回任务完成率（目前 ~73% = 8/11）、审批通过率、今日成本（≈¥0.2）、平均耗时、按 workflow 分布
4. /agent 页面评测面板渲染真实数据
5. 全部测试通过（原 81 + 新增 ~15）

---

## 8. 风险与应对

| 风险 | 应对 |
|---|---|
| 快照缺失导致样本不足 | 判据天数按实际快照可达值自适应；样本数在报告中标注 |
| 成本估算口径不准确 | 单价可配置；输出"估算值"标注，不伪装精确 |
| 在线指标基数太小（11 任务） | 面板显示数据区间（近 7 天/累计），随使用量自然增长 |
| 评测被操纵（调参过拟合） | 固定评测集版本号，变更需 `--fresh` 显式重建 |

---

## 9. 实现验证（2026-10-19）

- **自动化测试 88 条全绿**（原 81 + 新增 7：窗口指标与风控命中、基线/diff、--fresh 重建、成本核算、空数据稳健、metrics 路由）
- **真实库离线评测**：33 条推荐样本建立基线——7 天命中率 45.5% / 30 天 24.2% / 60 天 21.2%，止损达成 77.8%，目标达成 38.9%，触发命中 100%；分档显示 actionable -8.9% / watch -15.2%（基线已存 eval_runs）
- **在线指标**：任务完成率 81.8%（9/11）、累计成本 ¥0.196、工具调用 60 次、任务均值耗时 7.8s
- **页面**：/agent 评测面板渲染真实数据（完成率/成本/工作流分布/离线命中率）
- **评测体系价值验证**：数据显示 30 天命中率仅 24%、止损达成 77.8%——"方法论本身需要用评测集驱动迭代"成为真实可讲的故事

> AI生成
