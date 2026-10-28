# Discord GPT 娘 · 2.0 核心版

![GPT 娘横幅](artwork/gpt-girl-banner-680x240.png)

白发龙娘前辈：会认真接话，也允许安静。支持 Codex 登录接入、受控群聊观察、明确保存的记忆和定时提醒。完整设计、借鉴来源及分阶段路线见 [UPDATE_PLAN.md](UPDATE_PLAN.md)。

## 已实现

- `MODEL_BACKEND=codex` 为默认：通过原生 CLI 的 `codex exec` 调用，使用部署机器上已有的 Codex 登录；不需要为此模式填写 OpenAI API key。
- `mention`、`observe`、`social` 三种参与模式。默认 mention；所有模式都要求非空频道白名单。
- 每人每频道最近 6 轮成功交流，30分钟后失效；最近群聊仅存在内存，20条、每条500字符、30分钟窗口。
- `记住` 明确保存长期自述，最多10条；`不记录我` 停止把该用户在此频道的新消息送给模型。
- 明确命令创建提醒：持久保存、取消、安静时段延后、过期丢弃、重启不重复尝试发送。
- 全局每日模型调用上限，主动判断有独立子上限；串行模型调用，忙时不堆积无界队列。
- 原 API 模式仍可显式选择，继续使用旧的每日0.10 CAD估算账本，无自动付费回退。

尚未实现：自然语言自动提取长期记忆/事件、跨频道关系画像、自动生成日记或长期目标、图片语音理解、QQ适配。此版本不会假装这些能力已经存在。

## 首次运行或从旧版升级

需要 Python 3.10+、Discord Bot 和原生 Codex CLI。只运行一个机器人进程。

1. 停止旧进程。保留本机 `.env` 和 `daily-budget.sqlite3`；升级时不要覆盖凭证或清空预算。
2. 更新代码后创建环境、安装依赖。首次运行才复制模板：

   ```powershell
   python -m venv .venv
   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
   # 仅在没有 .env 时执行：
   Copy-Item .env.example .env
   ```

3. 在部署机器、运行机器人的同一系统账号中执行 `codex login`，再用 `codex login status` 核对。
4. 合并 `.env.example` 的新字段到本机 `.env`。最少需要：

   ```dotenv
   DISCORD_BOT_TOKEN=你的机器人Token
   ALLOWED_CHANNEL_IDS=测试频道数字ID
   MODEL_BACKEND=codex
   BOT_MODE=mention
   CODEX_EXECUTABLE=codex
   ```

   Windows 若 PATH 找到的是 `.cmd` 或 `.ps1` 包装器，改填原生 `codex.exe` 完整路径。
   `CODEX_MODEL` 留空使用 CLI 默认模型；显式填写前确认部署账号能使用该模型。
   白名单多个ID用英文逗号分隔；空白现在会拒绝启动。线程使用线程自己的ID。

5. 可选运行一次真实连接自检（消耗一次 Codex 调用，只发送合成测试句子）：

   ```powershell
   .\.venv\Scripts\python.exe check_codex.py
   ```

6. 启动并在测试频道明确 @：

   ```powershell
   .\.venv\Scripts\python.exe bot.py
   ```

旧版 `.env` 没有 `MODEL_BACKEND` 时，这次升级会选 Codex。希望维持旧 API 路径时明确填写 `MODEL_BACKEND=openai`、`OPENAI_API_KEY`、`OPENAI_MODEL=gpt-6-luna`。不删除旧账本；切回 API 时继续累计当天已有用量。

`start.ps1` 仍可用于安装依赖和启动。本机关闭、休眠、网络断开时无法准时发送提醒。持续在线需要常开主机或服务进程。

## Discord 与参与模式

在 [Discord Developer Portal](https://discord.com/developers/applications) 创建 Bot，安装到服务器，授予 View Channels、Send Messages、Read Message History；线程另需 Send Messages in Threads。无需 Administrator、Members 或 Presence intent。

| 模式 | 普通消息 | 自动插话 |
| --- | --- | --- |
| mention（默认） | 不观察 | 不会 |
| observe | 只观察 OBSERVED_CHANNEL_IDS | 不会；被直接叫到才答 |
| social | 只观察 OBSERVED_CHANNEL_IDS | 模型选择 reply / silence |

observe/social 必须同时开启 Developer Portal → Bot → **Message Content Intent**，并配置观察频道。观察频道必须是白名单的子集。在频道公开说明数据处理后再启用。例如：

```dotenv
BOT_MODE=observe
ALLOWED_CHANNEL_IDS=123456789
OBSERVED_CHANNEL_IDS=123456789
```

mention 模式不需要 Message Content Intent。正文明确 @ 最可靠；回复机器人时，仅在 Discord 实际提供正文且引用消息已解析时识别，不为分类主动拉取历史。私信、其他机器人及 webhook 消息忽略。

social 每频道等待3秒合并上下文；首条消息作为回复锚点。默认每10分钟最多一次主动判断，每日主动判断最多12次，全部模型调用最多100次。判断沉默、失败和超时也计数。安静时段默认多伦多时间23:00–08:00，不主动插话或发提醒；直接 @ 仍可回复。所有主动判断共享总额度，所以保守设置社交子额度。频道冷却是进程内状态，日计数持久化。

## 命令

以下命令在真实 `@GPT娘` 后输入；它们无需模型调用，在额度耗尽或模型忙碌时仍可执行。

| 命令 | 行为 |
| --- | --- |
| 帮助 | 使用说明 |
| 记住 喜欢下棋 | 保存你的明确自述，1–120字符 |
| 记忆 | 列出你在本频道的长期记忆 |
| 清空记忆 / reset | 删除你的长期记忆、交流和提醒，并清空本频道共享短期及衍生交流上下文 |
| 不记录我 | 清空上述内容，同时停止记录/向模型发送你在本频道的新消息 |
| 恢复记录 | 恢复短期记录；长期记忆仍需明确命令 |
| 提醒 30 复习 | 30分钟后在本频道发送提醒，不触发通知@ |
| 提醒列表 | 查看最近10条提醒及发送状态 |
| 取消提醒 123 | 取消属于你、尚未开始发送的提醒123 |

提醒分钟数1–43200，内容最多120字符；每人每频道最多5条待办，全局1000条。调度器每60秒检查一次，每频道每轮最多一条；延迟超过24小时失效。发送前检查频道白名单和当前权限。数据库先标记发送尝试，再调用 Discord：崩溃或网络结果不明时显示“发送结果不确定”，不自动重试，避免重复刷屏；它不保证必达。已开始发送的消息不能通过取消撤回。

## 记忆、隐私与运行限制

- 频道与用户 ID 用于隔离。模型输入包含当前发言者昵称、ID、相关文字、其显式记忆和成功交流；观察模式还包含该频道最近公开消息。不会自动读取当前 Codex 桌面聊天历史。
- 普通消息不会自动转成永久画像。长期事实是用户自述，模型不得把它当作已核实真相。
- 交流数据保存在 `data/state.sqlite3`，重启仍在；30分钟过期，由每分钟清理任务/读取时清理。群聊观察在内存中，重启消失；明确记忆直到删除，提醒结果最多保留到到期时间后7天。
- 清空会使尚未发送的相关生成失效，防止旧内容写回来；不能撤回已发消息、已发给供应商的请求或备份，也不能阻止别人重新引用你的话。Discord 删除原消息不会自动删除本地记忆，请使用清空命令。
- Codex 每轮使用 `--ephemeral --ignore-user-config --ignore-rules`，临时空目录、只读沙箱、关闭 shell/apps/plugins/浏览器等工具，用应用传入的上下文维持连续性。通过 stdin 传资料，不拼接 shell 命令；子进程环境不传 Discord Token 或 OpenAI API key。
- 这不是完整的操作系统隔离；Codex 认证仍使用部署账号的本机登录。部署到群聊时宜用独立系统账号/专用主机，升级 CLI 后重新跑自检。当前验证版本 `codex-cli 0.165.0-alpha.1.2`；不支持相关参数的版本会失败，不会自动放宽限制。
- 日调用次数是本地阀门，不等于 ChatGPT/Codex 实际剩余额度或费用上限；供应商限额仍适用。没有实时余额查询，不编造数字。
- API 的 `store=False` 与 Codex 的 ephemeral 都不等于供应商完全无日志。程序不把聊天、密钥或模型错误响应体写入应用日志；记录决策、发送是否成功和错误类型。
- 当前只处理文字；不联网检索，不运行群友代码，不执行电脑控制或改服务器指令。

## API 估算预算

只有显式 openai 后端使用根目录原有 `daily-budget.sqlite3`。全体用户共用每日0.10 CAD，多伦多时间零点切换；输入输出计价系数沿用旧版（保守输入200、输出800，单位为十亿分之一CAD/token），这不是实时价格或账单承诺。换模型或供应商调价时应先核对官方计价并修改 `budget.py`。

请求前预留、成功后按 usage 结算；未知结果保留预留，不自动重试。过大的请求被拒绝后，较小请求仍可尝试。不要删除账本来重置额度；该保护不涵盖其他应用调用、税费或未来调价。

## 测试与上线验收

```powershell
.\.venv\Scripts\python.exe -m unittest -v
```

测试使用模拟 Discord/模型与临时 SQLite，不会发群消息或产生模型费用。覆盖隔离、失效回答、退出记录、冷却、额度、成功发送后记忆、提醒权限与幂等、CLI参数/环境过滤/超时、API结算。

真实上线按 [人格验收用例](docs/PERSONA_EVAL.md) 在测试频道观察：先 mention，再 observe，最后小额度 social。不要把模拟测试通过等同于已验证实际群聊体验。

## 人设与素材

仍采用群主指定的白发、黑白服饰、龙角龙尾、爱面子的知识前辈。保留 DeepSeek 蓝鲸后辈、Let me check、泡面与额度梗；具体出处和不确定项见 [PERSONA_SOURCES.txt](PERSONA_SOURCES.txt)。人物设定和示例不等于现实经历或官方形象。

现有头像与横幅保持不变；没有复制参考项目的插画或代码。来源、借鉴边界和后续路线见 [UPDATE_PLAN.md](UPDATE_PLAN.md)。

