# K4 Ubuntu 部署说明

以下按 Ubuntu 服务器部署，默认目录是 `/root/k4`。

## 1. 准备系统依赖

```bash
apt update
apt install -y python3 python3-pip python3-venv cron
```

上传代码后进入目录：

```bash
cd /root/k4
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

## 2. 配置飞书和端口

建议把环境变量放到 `/etc/k4.env`：

```bash
cat >/etc/k4.env <<'EOF'
FEISHU_APP_ID=cli_xxx
FEISHU_APP_SECRET=xxx
FEISHU_VERIFY_TOKEN=xxx
BOT_PORT=8080
EOF
chmod 600 /etc/k4.env
```

如果这台服务器上还有 imini，确认 K4 的 `BOT_PORT`、安全组端口、飞书 webhook 路径不会和 imini 冲突。

## 3. 初始化扫描状态

首次部署时先把当前已有活动标记为已知，避免把旧活动都当成新活动启动：

```bash
cd /root/k4
. .venv/bin/activate
python k4_scan.py --init
```

也可以先查看当前能识别到什么活动：

```bash
python k4_scan.py --all
```

## 4. 配置 systemd 服务

创建 `/etc/systemd/system/k4-bot.service`：

```ini
[Unit]
Description=K4 Feishu Bot Server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/root/k4
EnvironmentFile=/etc/k4.env
ExecStart=/root/k4/.venv/bin/python /root/k4/bot_server.py
Restart=always
RestartSec=5
StandardOutput=append:/root/k4/bot_server.log
StandardError=append:/root/k4/bot_server.log

[Install]
WantedBy=multi-user.target
```

启动并设置开机自启：

```bash
systemctl daemon-reload
systemctl enable --now k4-bot
systemctl status k4-bot --no-pager
```

查看日志：

```bash
journalctl -u k4-bot -f
```

## 5. 配置定时任务

编辑 root 的 crontab：

```bash
crontab -e
```

加入：

```cron
# 每小时扫描新活动，并启动/恢复活动监控
0 * * * * cd /root/k4 && set -a && . /etc/k4.env && set +a && /root/k4/.venv/bin/python orchestrator.py >> orchestrator.log 2>&1

# 每天 22:00 发送日报
0 22 * * * cd /root/k4 && set -a && . /etc/k4.env && set +a && /root/k4/.venv/bin/python orchestrator.py --report >> orchestrator.log 2>&1
```

Ubuntu 默认 cron 使用服务器本地时区。先确认时区：

```bash
timedatectl
```

如果需要改成中国时间：

```bash
timedatectl set-timezone Asia/Shanghai
```

## 6. 常用检查命令

查看 K4 状态：

```bash
cd /root/k4
. .venv/bin/activate
python orchestrator.py --status
```

手动跑一次扫描/恢复：

```bash
python orchestrator.py
```

看调度日志：

```bash
tail -f /root/k4/orchestrator.log
```

看飞书服务：

```bash
systemctl status k4-bot --no-pager
tail -f /root/k4/bot_server.log
```
