# TG Monitor — 多部门 TG 私聊监控模板

为悦达业务审查部开发的 Telegram 私聊监控系统，支持快速复制部署给其他部门。

## 核心功能

- 多账号 Telethon 私聊消息监听（仅私聊，不含群组/频道）
- 消息记录到 Google Sheets（每部门一表格，每账号一分页，3 列横排）
- 关键词预警 → TG 群（到期/续费/暂停/下架/上架/地址/打款/欠费）
- 工作时段内累计 30 分钟未回复预警（非工作时段自动跳过）
- 删除消息检测 + Sheet 标红删除线（60s 巡检）
- 每日零点北京时间日报
- 同一对话同一类预警每天只触发一次（按日期 + peer_id 去重）
- 外事号昵称 / 广告主昵称变更自动同步到 Sheets 表头
- 用户手动改 Sheets 分页名 → 巡检自动改回 TG 名字
- Web 后台登入外事号（`http://<VPS>:<WEB_PORT>`）

## 技术栈

- Python 3.11 + Telethon + aiogram 3.x + gspread + SQLite
- Docker + Docker Compose

## 部署一个新部门（5 分钟）

### 1. 准备资源

每个部门需要：

| 项目 | 说明 |
|---|---|
| 部门英文名 | 例如 `yueda`、`dept2`，用作容器名后缀 |
| 预警群 chat_id | TG 群（通常是负数，可用 [@getidsbot](https://t.me/getidsbot) 取） |
| Bot Token | 找 [@BotFather](https://t.me/BotFather) 创建，把 bot 加进预警群并设为管理员 |
| Google Sheet | 新建空表格，URL 中 `/d/` 后那段就是 SHEET_ID |
| `service-account.json` | Google Cloud 服务账号凭证（所有部门可共用同一个） |
| WEB_PORT | 多部门同 VPS 时各部门要不同端口，默认 5001 |

> 把 service-account.json 里 `client_email` 字段的邮箱加为 Sheet 的「编辑者」。

### 2. 在 VPS 上跑 setup 脚本

```bash
cd /root
git clone https://github.com/PINK1119ZZ/tg-monitor-template.git
cd tg-monitor-template

# 用法: ./setup_company.sh <部门名> <预警群ID> <BOT_TOKEN> <SHEET_ID> [WEB_PORT]
./setup_company.sh dept2 -1003789999999 8778xxx:AAxxxx 1TDNxxxxxxxx 5002
```

会自动：

- 复制模板到 `/root/tg-monitor-dept2`
- 生成对应 `.env`
- 提示放入 `service-account.json`

### 3. 放凭证 + 启动

```bash
cp /path/to/service-account.json /root/tg-monitor-dept2/
cd /root/tg-monitor-dept2
docker compose up -d --build
```

### 4. 浏览器登入外事号

打开 `http://<VPS>:<WEB_PORT>`：

- 默认密码在 `web.py` 的 `LOGIN_PASSWORD` 常量
- 输入手机号 → 收 TG 验证码 → 登录
- 系统会自动建对应的 Sheets 分页

### 5. 测试

让 Bot 在 TG 预警群里成为群管理员，发送任意消息，观察容器日志：

```bash
docker compose logs -f tg-monitor
docker compose logs -f web
```

## 代码改动后重新部署

代码用 `COPY . .` 进镜像，单纯 scp 不生效：

```bash
# 整个重建
docker compose up -d --build

# 临时热替换某个文件
docker cp listener.py tg-monitor-yueda:/app/
docker compose restart tg-monitor
```

## 多部门同 VPS 注意事项

- **WEB_PORT 必须各不相同**（否则端口冲突）
- 每部门自己一个 `.env`、`/root/tg-monitor-<name>/` 目录
- service-account.json 可以共用同一个（每个部门 Sheet 都把它加为编辑者即可）
- 容器名自动加 `<name>` 后缀，互不冲突

## 工作时段（代码默认）

- 周一～周五：11:00-13:00 / 15:00-19:00 / 20:00-23:00
- 周六：11:00-13:00 / 15:00-20:00
- 周日：休息

如需修改，改 `config.py` 的 `WORK_SCHEDULE`。

## 关键词

默认（简体）：`到期,续费,暂停,下架,上架,地址,打款,欠费`

通过 `.env` 的 `KEYWORDS` 改。如果团队是繁中，记得加 `續費,暫停,欠費`。

## 目录结构

```
.
├── main.py              # 主入口
├── bot.py               # aiogram bot 处理器（预警推送 + 命令）
├── listener.py          # Telethon 多账号监听
├── tasks.py             # 定时任务（巡检/未回复/日报/昵称同步）
├── sheets.py            # Google Sheets 写入
├── database.py          # SQLite 操作
├── web.py               # Flask web 后台（登入外事号）
├── config.py            # 配置加载
├── templates.py         # 预警模板
├── docker-compose.yml   # 两个服务: tg-monitor + web
├── Dockerfile
├── requirements.txt
├── setup_company.sh     # 一键部署新部门
└── templates/           # web 后台 HTML
```

## 已知行为（非 bug）

- 列组 0 首次写入会报 addBanding warning — `_create_sheet_tab` 已建好 A-C 斑马纹，不影响数据
- DB peers 数 > 列组数：历史对话但近 N 天无消息的 peer `col_group=-1`，等新消息到来才 lazy 分配（避免空对话占位）
