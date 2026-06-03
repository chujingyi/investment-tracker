# K4 应募活动监控

自动扫描 Ktown4u 四个站点（GL/CN/KR/JP）的签名会、视频通话、拍照活动，发现新活动后自动启动库存监控，变化记录写入日志，到期后自动发送排序报告。

---

## 架构说明

K4 和 imini、KMS 共用同一个飞书机器人（运行在 `imini/bot_server.py`），**K4 的 `bot_server.py` 不需要单独启动**。所有飞书指令通过 `k4 <指令>` 前缀发送。

```
cron（每小时）
    ↓
orchestrator.py
    ├── k4_scan.py      扫描 Ktown4u 四站点，筛选应募活动
    ├── k4_monitor.py   单活动库存监控（子进程，按活动独立运行）
    └── feishu.py       推送通知

飞书消息
    ↓
imini/bot_server.py（:8080，systemd 管理）
    ├── imini xxx → imini 逻辑
    ├── k4 xxx    → k4 逻辑
    └── kms xxx   → kms 逻辑
```

---

## 文件说明

| 文件 | 用途 |
|------|------|
| `orchestrator.py` | 调度器：扫描新活动、启动/恢复监控、停止过期监控、发送日报和晚报 |
| `k4_monitor.py` | 单活动库存监控进程，记录销量变化，到期自动停止并在 30 分钟后发送最终排序报告 |
| `k4_sort.py` | 日志排序脚本，按变化差值从大到小输出 |
| `k4_scan.py` | 扫描 Ktown4u 四个站点活动，按 productNo 配对生成 campaign 配置 |
| `feishu.py` | 飞书 API 客户端 |
| `config.py` | 路径、飞书配置 |
| `bot_server.py` | **已弃用**，由 `imini/bot_server.py` 统一替代，保留备用 |
| `campaign.sample.json` | 活动配置样例 |

---

## 目录结构

```text
campaigns/<campaign_id>/
  campaign.json
  logs/
    cn_signing.log
    gl_signing.log
    kr_signing.log
    jp_signing.log
  runtime/
    monitor.pid
    monitor.stdout.log
    monitor.stderr.log
  exports/
    20260525_signing_rank.log
```

---

## 活动筛选规则

| 关键词（包含即命中） | 对应活动类型 |
|------|------|
| `sign`, `signing`, `签名`, `签售` | 签名会 |
| `video call`, `video`, `通话` | 视频通话 |
| `photo`, `拍照` | 拍照活动 |
| `fansign`, `hi-touch`, `handshake` | 其他应援活动 |

排除关键词（包含则跳过）：`lucky draw`, `特典`, `gift`, `gwp`, `倉庫`, `warehouse`

---

## 监控恢复与手动停止

- **异常崩溃**：进程意外退出（pid 有值但进程不存在），下次 cron 自动重启并飞书告警。
- **手动停止**（`k4 stop`）：pid 清零，cron 不会重启，需手动 `k4 start` 恢复。

---

## 飞书指令

所有 K4 指令需要加 `k4` 前缀。`campaign_id` 支持**模糊匹配**，输入艺人名或商品号的一部分即可（多个命中时返回日期最新的）。

| 指令 | 说明 |
|------|------|
| `k4 status` | 查看 K4 监控状态（🟢 运行中 / 🔴 已停止） |
| `k4 scan` | 立即扫描 Ktown4u，发现新活动 |
| `k4 start <campaign_id>` | 启动或恢复监控 |
| `k4 stop <campaign_id>` | 停止监控（手动停止，不会被 cron 重启） |
| `k4 set <campaign_id> YYYY-MM-DD HH:MM` | 设置活动截止时间并启动监控（监控自动延长30分钟捞退单） |
| `k4 sort <campaign_id>` | 生成排序报告 |
| `k4 log1 <campaign_id>` | 获取监控日志（所有站点合并） |
| `k4 log2 <campaign_id>` | 获取排序导出文件 |
| `k4 log orchestrator` | 获取调度器日志 |
| `status` | 同时查看所有平台状态 |
| `help` | 查看全部可用指令 |

示例：

```text
k4 sort kangmin
k4 set yerin 2026-05-30 00:00
k4 log1 yerin
k4 log orchestrator
```

---

## status 显示规则

- 运行中：始终显示，附活动截止和剩余时间
- 已停止 < 12 小时：显示（方便确认刚结束的活动）
- 已停止 > 12 小时：自动隐藏（数据和日志文件永久保留）

---

## 自动推送时机

| 时机 | 内容 |
|------|------|
| 发现新活动（有截止时间） | 活动名、campaign_id、截止时间、PID，自动启动监控 |
| 发现新活动（无截止时间） | 提醒用 `k4 set` 补充截止时间 |
| 库存发生变化 | SKU 名称、变化前后库存、差值 |
| 监控进程崩溃恢复 | 告警 + 已重启 |
| 活动到期后 30 分钟 | 最终排序报告（仅自然到期触发，手动 stop 不触发） |
| 每天 19:05 | 当天无新活动时：今日扫描次数和时间点 |
| 每天 22:00 | 所有运行中活动的状态 + 排序报告 |

---

## 使用方法

### 首次初始化

把当前 Ktown4u 已有活动标记为已知，避免重复处理：

```bash
python3 k4_scan.py --init
```

### 手动运行

```bash
python3 orchestrator.py                  # 扫描 + 启动/恢复监控
python3 orchestrator.py --status         # 打印当前状态
python3 orchestrator.py --report         # 发送日报
python3 orchestrator.py --evening-report # 发送 19 点扫描播报
python3 k4_sort.py --config campaigns/<campaign_id>/campaign.json
```

---

## 部署

### 环境变量

写入 `/etc/k4.env`：

```bash
FEISHU_APP_ID=cli_xxx
FEISHU_APP_SECRET=xxx
FEISHU_VERIFY_TOKEN=
```

### feishu_state.json

K4 监控进程推送用，从 imini 复制：

```bash
cp /root/imini/feishu_state.json /root/k4/feishu_state.json
```

### 不需要启动 K4 的 bot server

K4 的飞书指令由 imini 的 `imini-bot.service` 统一处理，无需单独启动。

### Cron

```cron
# 每天 09:00-19:00 每小时扫描 + 恢复监控
0 9-19 * * * cd /root/k4 && set -a && . /etc/k4.env && set +a && /usr/bin/python3 orchestrator.py 2>>/root/k4/orchestrator.log

# 每天 22:00 日报
0 22 * * * cd /root/k4 && set -a && . /etc/k4.env && set +a && /usr/bin/python3 orchestrator.py --report 2>>/root/k4/orchestrator.log

# 每天 19:05 扫描播报
5 19 * * * cd /root/k4 && set -a && . /etc/k4.env && set +a && /usr/bin/python3 orchestrator.py --evening-report 2>>/root/k4/orchestrator.log
```

---

## 日志

```bash
tail -f orchestrator.log
tail -f campaigns/<campaign_id>/logs/cn_signing.log
tail -f campaigns/<campaign_id>/runtime/monitor.stdout.log
```
