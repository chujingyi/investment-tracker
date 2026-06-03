# imini.tv 应募活动监控

自动扫描 [imini.tv 星君文化店铺](https://www.imini.tv/wap_xingjunwenhua.html) 的新增应募活动，发现目标活动后自动启动库存监控。监控日志按活动保存，排序报告会把同一活动的 `【大陆地址】` 和 `【非大陆地址】` 合并，并标明每条记录来源。

---

## 架构说明

imini、K4、KMS 三个平台共用同一个飞书机器人（`imini/bot_server.py`，端口 8080，systemd 管理）。所有飞书指令以平台名开头区分。

```
cron（每小时）
    ↓
orchestrator.py
    ├── imini_scan.py    扫描 imini 店铺新增商品
    ├── imini_monitor.py 单活动库存监控（子进程）
    └── feishu.py        推送通知

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
| `orchestrator.py` | 调度器：扫描新活动、启动监控、恢复异常退出的监控、发送每日晚报 |
| `imini_scan.py` | 扫描 imini 店铺新增商品 ID，筛选目标活动并返回新活动列表 |
| `imini_monitor.py` | 单活动库存监控进程，记录库存变化，到截止时间自动停止 |
| `imini_sort.py` | 排序脚本，支持大陆/非大陆地址合并排序 |
| `bot_server.py` | 统一飞书 webhook 服务（同时管理 imini / K4 / KMS） |
| `feishu.py` | 飞书 API 客户端，负责发送消息和保存绑定用户 |
| `config.py` | 路径、飞书应用凭证、端口等配置 |
| `requirements.txt` | Python 依赖 |
| `known_ids.json` | 已处理过的 `pro_id` 列表，自动维护 |
| `orchestrator.log` | 调度器日志 |
| `monitors/` | 每个活动的监控目录、日志、排序结果 |

---

## 活动筛选规则

只保留以下类型：

| 类型 | 匹配规则 |
|------|----------|
| `线下签名会` | 名称含 `签名会` |
| `视频通话签名会` | 名称含 `视频通话` 或 `视频签售` |
| `手渡咖啡` | 名称含 `手渡` |
| `photo_event` | 名称含 `photo event`，不区分大小写 |

---

## 目录结构

```text
monitors/<截止日期>_<活动标题>_<pro_id>/
  info.json                         # 活动信息、PID、启动时间
  <文件夹名>.log                    # 库存监控日志
  <文件夹名>_sorted.log             # 单活动排序结果

monitors/<截止日期>_<活动标题>_合并排序.log  # 合并排序（大陆+非大陆）
```

---

## 监控恢复与手动停止

- **异常崩溃**：进程意外退出（pid 有值但进程不存在），下次 cron 自动重启并飞书告警。
- **手动停止**（`imini stop`）：pid 清零，cron 不会重启，需手动 `imini start` 恢复。

---

## 飞书指令

所有 imini 指令需加 `imini` 前缀。`pro_id` 支持精确匹配。

| 指令 | 说明 |
|------|------|
| `imini status` | 查看 imini 监控状态（含剩余时间） |
| `imini scan` | 立即扫描 imini 店铺，发现新活动 |
| `imini start <pro_id>` | 启动或恢复监控 |
| `imini stop <pro_id>` | 停止监控（手动停止，不会被 cron 重启） |
| `imini set <pro_id> YYYY-MM-DD HH:MM` | 设置活动截止时间并启动监控（监控自动延长30分钟捞退单） |
| `imini sort <pro_id>` | 生成合并排序报告 |
| `imini log1 <pro_id>` | 获取原始监控日志文件 |
| `imini log2 <pro_id>` | 获取合并排序文件 |
| `imini log orchestrator` | 获取调度器日志 |
| `status` | 同时查看所有平台状态 |
| `help` | 查看全部可用指令 |

示例：

```text
imini set 59037453 2026-05-28 00:00
imini sort 59037453
imini log1 59037453
```

---

## status 显示规则

- 运行中：始终显示，附剩余时间
- 已停止 < 12 小时：显示（方便确认刚结束的活动）
- 已停止 > 12 小时：自动隐藏（数据和日志文件永久保留）

---

## 自动推送时机

| 时机 | 内容 |
|------|------|
| 发现新活动（有截止时间） | 活动名、pro_id、价格、截止时间，自动启动监控 |
| 发现新活动（无截止时间） | 提醒用 `imini set` 补充截止时间 |
| 库存发生变化 | SKU 名称、变化前后库存、差值 |
| 监控进程崩溃恢复 | 告警 + 已重启 |
| 每天 22:00 | 所有运行中活动的状态 + 合并排序报告 |

---

## 截止时间处理

- 截止时间来自 imini 详情接口的 `active_module[].popup_info.end_time`。
- 部分活动无此字段，系统保存活动信息并飞书提醒补时间。
- `imini set` 输入**活动截止时间**，监控进程自动延长 30 分钟捞退单。

---

## 合并排序规则

同一活动通常有两个商品（`【大陆地址】` / `【非大陆地址】`），排序时去掉地址标签，按 `截止日期 + 活动标题` 识别为同一组，合并两个日志按库存变化差值排列。

---

## 使用方法

### 1. 安装依赖

```bash
pip3 install -r requirements.txt
```

### 2. 首次初始化

把当前店铺已有商品标记为已知，后续只处理新增活动：

```bash
python3 imini_scan.py --init
```

### 3. 手动运行

```bash
python3 orchestrator.py           # 扫描 + 启动/恢复监控
python3 orchestrator.py --report  # 发送日报
python3 orchestrator.py --status  # 仅打印状态（不发飞书）
```

---

## 部署

### 环境变量

写入 `/etc/imini.env`：

```bash
FEISHU_APP_ID=cli_xxx
FEISHU_APP_SECRET=xxx
FEISHU_VERIFY_TOKEN=xxx
BOT_PORT=8080
```

### systemd（bot_server）

```bash
# /etc/systemd/system/imini-bot.service
[Unit]
Description=Imini + K4 + KMS Unified Feishu Bot
After=network.target

[Service]
EnvironmentFile=/etc/imini.env
WorkingDirectory=/root/imini
ExecStart=/usr/bin/python3 /root/imini/bot_server.py
Restart=always

[Install]
WantedBy=multi-user.target
```

```bash
systemctl enable imini-bot
systemctl start imini-bot
```

### Cron

```cron
# 每天 09:00-19:00 每小时扫描一次
0 9-19 * * * cd /root/imini && set -a && . /etc/imini.env && set +a && /usr/bin/python3 orchestrator.py 2>>/root/imini/orchestrator.log

# 每天 22:00 发送晚报
0 22 * * * cd /root/imini && set -a && . /etc/imini.env && set +a && /usr/bin/python3 orchestrator.py --report 2>>/root/imini/orchestrator.log
```

---

## 日志

```bash
tail -f orchestrator.log
tail -f monitors/<活动文件夹>/<活动文件夹>.log
```
