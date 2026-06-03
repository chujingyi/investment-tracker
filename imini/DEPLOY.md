# 部署指南（阿里云服务器）

## 一、上传文件到服务器

在本机执行（把整个 imini 目录传上去）：

```bash
scp -r /Users/chujingyi/Downloads/gaoji/imini/ root@112.124.10.177:/root/imini/
```

## 二、服务器环境准备

SSH 进服务器后执行：

```bash
# 安装 Python 依赖
cd /root/imini
pip3 install -r requirements.txt

# 确认 Python 版本 >= 3.10（需要 str | None 语法）
python3 --version
```

如果版本低于 3.10，把代码里所有 `Path | None` 和 `str | None` 改成 `Optional[...]`，或升级 Python：

```bash
# CentOS/AlmaLinux 升级方式
dnf install python3.11 -y
alias python3=python3.11
```

## 三、阿里云安全组放行端口

登录阿里云控制台 → ECS → 安全组 → 入方向，添加规则：

| 端口 | 协议 | 来源 |
|------|------|------|
| 8080 | TCP  | 0.0.0.0/0 |

## 四、飞书开发者后台配置

### 4.1 开通权限
登录 https://open.feishu.cn → 你的应用 → 权限管理，开启：
- `im:message`（发送消息）
- `im:message:send_as_bot`（以机器人身份发送）

### 4.2 配置事件订阅
应用 → 事件订阅：
1. 填写请求地址：`http://112.124.10.177:8080/feishu/webhook`
2. 添加事件：搜索并勾选 `接收消息（im.message.receive_v1）`
3. 复制页面上的 **Verification Token**，填入服务器的 `config.py`：

```python
FEISHU_VERIFY_TOKEN = "这里填你复制的 token"
```

### 4.3 发布应用
应用 → 版本管理与发布 → 创建版本 → 申请发布（企业内部应用通常秒过）

### 4.4 将机器人添加为联系人
在飞书 App 里搜索你的机器人名称，发起会话，**发任意消息**（比如"hello"）完成绑定。
绑定后服务器会保存你的 open_id，后续所有通知都发到这里。

## 五、启动 bot_server（常驻）

```bash
cd /root/imini
nohup python3 bot_server.py > bot_server.log 2>&1 &
echo $! > bot_server.pid
echo "Bot server 已启动，PID: $(cat bot_server.pid)"
```

验证是否正常运行：

```bash
curl -s http://127.0.0.1:8080/feishu/webhook \
  -H "Content-Type: application/json" \
  -d '{"type":"url_verification","challenge":"test123"}'
# 应返回：{"challenge":"test123"}
```

## 六、初始化已知活动列表

```bash
cd /root/imini
python3 imini_scan.py --init
# 输出：已将 XX 个 ID 标记为已知
```

## 七、配置 cron 定时扫描（每天 09:00–19:00 每小时运行）

```bash
crontab -e
```

添加两行：

```
# 每天 09:00-19:00 每小时扫描一次新活动
0 9-19 * * * cd /root/imini && /usr/bin/python3 orchestrator.py >> orchestrator.log 2>&1

# 每天 22:00 发送晚报（活动列表 + 剩余天数 + 排序文件）
0 22 * * * cd /root/imini && /usr/bin/python3 orchestrator.py --report >> orchestrator.log 2>&1
```

保存退出，验证：

```bash
crontab -l
```

## 八、验证完整流程

手动跑一次 orchestrator：

```bash
cd /root/imini
python3 orchestrator.py
```

如果有新活动，飞书会收到通知；如果没有，看 `orchestrator.log` 确认正常输出。

---

## 日常使用

### 飞书指令

| 指令 | 说明 |
|------|------|
| `status` | 查看所有监控进程状态 |
| `set <pro_id> 2026-06-01 00:00` | 补充截止时间并启动监控 |
| `sort <pro_id>` | 生成排序报告发给你 |
| `help` | 显示帮助 |

### 查看日志

```bash
# 扫描日志
tail -f /root/imini/scan.log

# 调度日志
tail -f /root/imini/orchestrator.log

# 某个活动的监控日志
tail -f /root/imini/monitors/<活动文件夹>/<文件夹名>.log

# bot server 日志
tail -f /root/imini/bot_server.log
```

### 重启 bot_server（服务器重启后需要手动执行）

```bash
cd /root/imini
kill $(cat bot_server.pid) 2>/dev/null
nohup python3 bot_server.py > bot_server.log 2>&1 &
echo $! > bot_server.pid
```

如果希望服务器重启后自动拉起，可以用 systemd（告诉我，我帮你写 service 文件）。

---

## ⚠️ 安全提醒

你的 `App Secret` 已在对话中出现过，建议部署完成后立即在飞书开发者后台重置：
应用 → 凭证与基础信息 → App Secret → 重置，然后更新 `config.py`。
