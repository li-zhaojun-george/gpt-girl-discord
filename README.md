# Discord GPT 娘 · 第一版

![GPT 娘横幅](artwork/gpt-girl-banner-680x240.png)

Discord 群聊机器人：正文明确 @ 才回复，白发龙娘前辈人设，支持独立短期记忆，以及全群共用的每日 0.10 CAD 本地估算额度。GitHub 保存代码和图片；保持在线仍需要本机或服务器运行程序。

Python 3.10+。正文明确 @ 机器人时才回复，支持中文群聊与短期对话记忆。无需开启 Message Content、Members、Presence 等特权 Intent。

## 1. 创建 Discord Bot

1. 打开 https://discord.com/developers/applications ，New Application，取名 GPT娘。
2. 在 Bot 页面获取或重置 Bot Token，保存到本机 `.env`，不要发到聊天里。
3. 在 Installation 中使用 Guild Install（或 OAuth2 URL Generator），选择 `bot` scope。给予 View Channels、Send Messages、Read Message History；若在帖子/线程内使用，另给 Send Messages in Threads。无需 Administrator。
4. 使用生成的安装链接，添加到你有管理权限的测试服务器。确认频道权限没有覆盖机器人的发言权限。
5. 不用打开 Message Content Intent。此版本只读正文中含明确用户 @ 的消息，不读取普通群聊。

## 2. 本机启动（Windows）

在项目目录打开 PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
notepad .env
```

仅首次复制 `.env`，不要覆盖已经填写的凭证。填写：

```dotenv
DISCORD_BOT_TOKEN=你的机器人Token
OPENAI_API_KEY=你的OpenAI项目API密钥
OPENAI_MODEL=你的项目可调用的模型ID
ALLOWED_CHANNEL_IDS=测试频道的数字ID
```

模型 ID 在 OpenAI 平台确认；这里不绑定某个价格档位或保证账号有某模型权限。需要支持 Responses API 的文本模型。OpenAI API 需要独立配置可用额度，程序不会使用此聊天中的订阅登录态。

Discord 设置 → 高级 → 开发者模式，然后右键频道复制 ID。多个频道用英文逗号隔开；留空允许所有机器人可见的服务器频道。

启动：

```powershell
.\.venv\Scripts\python.exe bot.py
```

看到“GPT 娘已上线”后，在 Discord 输入 `@GPT娘 你好`，从自动补全中选中真实机器人。结束程序按 Ctrl+C；本机休眠或关闭程序后机器人离线。也可以运行 `./start.ps1`，它会创建环境、安装依赖，并在缺少 `.env` 时创建空模板。

## 3. 实际体验

- `@GPT娘 你好`：聊天。
- `@GPT娘 帮助`：查看用法和数据处理说明。
- `@GPT娘 清空记忆` / `@GPT娘 reset`：清空你在当前频道的历史。
- 普通聊天、仅回复机器人但正文没写 @、@everyone、私信、其他机器人消息：忽略。
- 同一用户在同一服务器冷却 8 秒，期间的重复请求静默忽略（包括命令）。最多 3 个并行模型请求，满载时提示稍后再试。
- 每位用户在每个频道独立保存最近 6 轮与机器人的对话；空闲 30 分钟后下次处理消息时清理，重启全部清空。最多保存 300 个会话。
- 输入最多 2000 字符，API 输出上限 600 tokens；过长文本截短，不会触发 @everyone 或主动 ping 用户。

本版只支持文字，不解析图片、附件、语音或网页，也不读取其他群友的普通消息。模型收到的内容包含你的 Discord 用户 ID、显示名、当前文字以及你的相关历史。`store=False` 禁用 Responses 应用层响应存储，不代表供应商无任何日志；上线前可在频道说明中公开这一点。程序自身不记录聊天文本或密钥。

## 4. 人设与来源

编辑 `persona.txt` 后重启即可。采用群主指定的白发黑白服饰龙娘、傲娇高冷前辈版本，加入 Let me check、鲸鱼后辈、泡面与额度梗；具体来源与不确定项见 PERSONA_SOURCES.txt。指令和对话示例是本项目原创，并非统一官方设定。没有下载、复制或附带他人角色插画。

项目图片是根据用户提供的三视图生成的同人衍生图，角色设计来源参见 PERSONA_SOURCES.txt。图片不代表 OpenAI 官方形象，也未赋予他人原角色设计新的授权。

- `artwork/gpt-girl-avatar-closeup.png`：新版大头头像。
- `artwork/gpt-girl-banner-680x240.png`：680×240、17:6 横幅，小于 10MB。

`.env` 中的密钥、预算数据库、日志和本机依赖不应上传。克隆项目后自行复制 `.env.example` 为 `.env` 并填写配置。不要将数据库删除作为日常重启步骤，否则预算记录会丢失。

- [AI 娘化图鉴：GPT 娘白发龙娘](https://genderswapai.com/zh-CN/ai-gijinka)
- [GPT 龙娘梗百科（页面注明非官方二创）](https://aiher.ai/gpt/)
- [OpenAI 官方 API 入门](https://developers.openai.com/api/docs/quickstart)
- [Discord 官方机器人入门](https://docs.discord.com/developers/quick-start/getting-started)
- [Discord Message Content Intent 与提及例外](https://docs.discord.com/developers/events/gateway#message-content-intent)

## 5. 检查与部署

```powershell
.\.venv\Scripts\python.exe -m unittest -v
```

测试使用模拟 Discord 消息和模拟 OpenAI API，不产生费用。覆盖触发条件、用户隔离、清空记忆、冷却、长度、过期及错误恢复。真实登录和模型调用需填写凭证后验证。

常见问题：AuthenticationError 检查 OpenAI 密钥；RateLimitError 检查额度与速率；NotFoundError 检查模型 ID 与项目权限；Discord LoginFailure 检查 Bot Token。日志只打印错误类型，不输出敏感响应体。机器人在线但无回复时，检查真实 @、频道白名单和频道权限，等待 8 秒后重试。

保持在线可将本目录复制到常开主机，在主机配置独立 `.env` 并用进程管理器运行 `python bot.py`。Gateway 采用出站连接，无需公网回调 URL 或开放入站端口。

每日预算：所有用户、频道、服务器共用0.10 加元（CAD）的本地记账额度，多伦多时间零点恢复。`daily-budget.sqlite3` 保存计数，重启不清零。按 GPT-6 Luna Standard 输入 $0.10/百万 tokens、输出 $0.50/百万 tokens 计价，输入加 25% 缓存写入余量，固定按 1.60 CAD/USD 换算（不是实时汇率）；请求前保守预留输入及最大输出费用，返回后按 usage 结算，未知/失败请求保留预留，不自动重试。余额不足以覆盖下一次请求时，当天停止模型调用，@ 后固定回复“今天的额度用完啦，明天再聊～”。这可能提前停止，属于本地估算保护，不是供应商账单硬上限，不含启用前或其他程序的用量、税费及未来调价。模型变更时必须先更新预算计价，程序默认拒绝其他模型。
