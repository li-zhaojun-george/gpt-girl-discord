"""Private, typed Discord controls over the existing application services."""
import asyncio
import logging
from types import SimpleNamespace

import discord
from discord import app_commands
from channel_context import channel_context

LOG = logging.getLogger('gpt-girl')

class InteractionMessage:
    def __init__(self, interaction):
        self.interaction = interaction
        self.id = f'slash:{interaction.id}'  # An interaction is NOT a public Discord message.
        self.guild, self.channel, self.author = interaction.guild, interaction.channel, interaction.user

    async def reply(self, text, **ignored):
        await self.interaction.edit_original_response(content=text[:1900],allowed_mentions=discord.AllowedMentions.none())
        return SimpleNamespace(id=None)  # Never add ephemeral controls to public conversation history.

class ControlTree(app_commands.CommandTree):
    async def on_error(self, interaction, error):
        LOG.warning('Slash command failed (%s)',type(error).__name__)
        text = '这次操作没完成，请稍后重试。'
        try:
            if interaction.response.is_done():
                await interaction.edit_original_response(content=text,allowed_mentions=discord.AllowedMentions.none())
            else:
                await interaction.response.send_message(text,ephemeral=True,allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            pass

class SlashControls:
    def __init__(self,bot):
        self.bot = bot
        self.tree = ControlTree(bot)
        self.synced = set()
        self.sync_lock = asyncio.Lock()
        self.register()

    async def begin(self,interaction):
        if (interaction.guild is None or interaction.channel is None or
                interaction.channel_id not in self.bot.settings.allowed):
            await interaction.response.send_message('请在GPT娘已启用的服务器频道使用这些指令。',ephemeral=True,
                                                   allowed_mentions=discord.AllowedMentions.none())
            return False
        await interaction.response.defer(ephemeral=True,thinking=True)
        return True

    async def show(self,interaction,text):
        if await self.begin(interaction):
            await InteractionMessage(interaction).reply(text)

    async def invoke(self,interaction,text,feature=None):
        if not await self.begin(interaction):
            return
        proxy = InteractionMessage(interaction)
        if feature=='memory' and not self.bot.memory:
            await proxy.reply('这个服务器尚未启用自动记忆，仍可使用 /记忆 保存。')
            return
        if feature=='sharing' and not self.bot.interests:
            await proxy.reply('目前没有启用主动分享。')
            return
        await self.bot.command(proxy,text)

    def usage(self):
        now = self.bot.now()
        with self.bot.state.db() as db:
            row = db.execute('SELECT total,social FROM quota WHERE day=?',(now.date().isoformat(),)).fetchone()
        total,social = (row['total'],row['social']) if row else (0,0)
        return (f"今日机器人用量 · {now:%Y-%m-%d}（{self.bot.settings.timezone}）\n"
                f"模型调用：{total} 次\n主动判断及兴趣探索：{social} 次\n聊天及记忆整理：{total-social} 次\n"
                '包含沉默、失败和超时尝试；这些 / 管理指令不调用模型。\n'
                '这是机器人本地计数，不是Token账单或Pro会员剩余额度。')

    async def sync_enabled_guilds(self):
        async with self.sync_lock:
            guilds = {c.guild.id for cid in self.bot.settings.allowed
                      if (c:=self.bot.get_channel(cid)) is not None and getattr(c,'guild',None)}
            for guild_id in guilds-self.synced:
                guild = discord.Object(id=guild_id)
                existing = await self.tree.fetch_commands(guild=guild)
                ours = {c.name for c in self.tree.get_commands()}
                if any(c.name not in ours or c.type!=discord.AppCommandType.chat_input for c in existing):
                    LOG.warning('Slash sync skipped: unrelated application commands exist guild=%s',guild_id)
                    continue
                self.tree.copy_global_to(guild=guild)
                result = await self.tree.sync(guild=guild)
                self.synced.add(guild_id)
                LOG.info('Slash commands synced roots=%s guild=%s',len(result),guild_id)

    def register(self):
        tree = self.tree

        @tree.command(name='用量',description='查看机器人今日模型调用次数（本地统计）')
        @app_commands.guild_only()
        async def usage(interaction: discord.Interaction):
            # Guard before reading the ledger.
            if await self.begin(interaction):
                await InteractionMessage(interaction).reply(self.usage())

        @tree.command(name='帮助',description='查看GPT娘的指令与常见用法')
        @app_commands.guild_only()
        async def help_command(interaction: discord.Interaction):
            await self.show(interaction,
                '/用量：今天调用了多少次\n/状态：当前模式、功能和记录设置\n'
                '/记忆 查看、自动、共同、来源：查看记忆\n/记忆 保存、确认、更正、删除、清空：管理记忆\n'
                '/提醒 创建、列表、取消：管理自己的提醒\n/分享 状态、暂停、恢复：主动分享（修改需管理服务器权限）\n'
                '/记录 停止、恢复：退出或恢复记录\n'
                '这些操作结果仅自己可见；到期提醒仍发到当前频道。原来的 @ 文字命令继续可用。')

        @tree.command(name='状态',description='查看当前频道功能和你的记录设置')
        @app_commands.guild_only()
        async def status(interaction: discord.Interaction):
            if not await self.begin(interaction):
                return
            scope = f'{interaction.guild.id}:{interaction.channel_id}'
            cfg = self.bot.settings
            await InteractionMessage(interaction).reply(
                f'后端：{cfg.backend} · 模式：{cfg.mode}\n'
                f'频道方向：{channel_context(interaction.channel)["guidance"]}\n'
                f'自动记忆：{"开启" if interaction.channel_id in cfg.memory_channels else "关闭"}\n'
                f'持续话题：{len(self.bot.topics.store.rows(scope,30)) if self.bot.topics and interaction.channel_id in cfg.memory_channels else 0} 个（30天未更新过期）\n'
                f'兴趣分享：{"已配置（可用 /分享 状态 查看）" if interaction.channel_id in cfg.interest_channels else "关闭"}\n'
                f'你的记录设置：{"已停止" if self.bot.state.muted(scope,interaction.user.id) else "允许记录"}\n'
                f'当前安静时段：{"是" if cfg.quiet(self.bot.now().hour) else "否"}（正常对话仍可进行）')

        memory = app_commands.Group(name='记忆',description='查看、保存和修订你的记忆',guild_only=True)
        tree.add_command(memory)

        @memory.command(name='查看',description='查看你的手动记忆与自动喜好摘要')
        async def memory_view(interaction: discord.Interaction):
            await self.invoke(interaction,'记忆')

        @memory.command(name='自动',description='查看你的自动喜好和待确认候选')
        async def memory_auto(interaction: discord.Interaction):
            await self.invoke(interaction,'自动记忆','memory')

        @memory.command(name='共同',description='查看本频道的共同话题与回忆')
        async def memory_shared(interaction: discord.Interaction):
            await self.invoke(interaction,'共同回忆','memory')

        @memory.command(name='保存',description='明确保存一条自己的记忆')
        @app_commands.describe(内容='1–120字，例如喜欢乌龙茶')
        async def memory_save(interaction: discord.Interaction,内容: app_commands.Range[str,1,120]):
            await self.invoke(interaction,'记住 '+内容)

        @memory.command(name='来源',description='查看某条自动记忆的证据')
        async def memory_source(interaction: discord.Interaction,编号: app_commands.Range[int,1,999999999999]):
            await self.invoke(interaction,f'记忆来源 {编号}','memory')

        @memory.command(name='确认',description='确认自己的喜好或参与的共同回忆')
        async def memory_confirm(interaction: discord.Interaction,编号: app_commands.Range[int,1,999999999999]):
            await self.invoke(interaction,f'确认记忆 {编号}','memory')

        @memory.command(name='更正',description='修订自己的喜好或参与的回忆，以你的说法为准')
        async def memory_correct(interaction: discord.Interaction,编号: app_commands.Range[int,1,999999999999],内容: app_commands.Range[str,1,120]):
            await self.invoke(interaction,f'更正记忆 {编号} {内容}','memory')

        @memory.command(name='删除',description='删除一条自己的喜好或参与的共同回忆')
        async def memory_delete(interaction: discord.Interaction,编号: app_commands.Range[int,1,999999999999]):
            await self.invoke(interaction,f'忘记记忆 {编号}','memory')

        @memory.command(name='清空',description='清除你在本频道的记忆、相关共同回忆和提醒')
        @app_commands.describe(确认='设为True才执行；也会清空共享短期上下文')
        async def memory_clear(interaction: discord.Interaction,确认: bool=False):
            if 确认:
                await self.invoke(interaction,'清空记忆')
            else:
                await self.show(interaction,'这会清除你在本频道的记忆、相关共同回忆和提醒。需要执行时，将「确认」设为True。')

        reminders = app_commands.Group(name='提醒',description='创建和管理自己的提醒',guild_only=True)
        tree.add_command(reminders)

        @reminders.command(name='创建',description='在本频道安排一条提醒，到期后公开发送且不@通知')
        async def reminder_create(interaction: discord.Interaction,分钟: app_commands.Range[int,1,43200],内容: app_commands.Range[str,1,120]):
            await self.invoke(interaction,f'提醒 {分钟} {内容}')

        @reminders.command(name='列表',description='查看你在本频道的提醒及状态')
        async def reminder_list(interaction: discord.Interaction):
            await self.invoke(interaction,'提醒列表')

        @reminders.command(name='取消',description='取消一条属于你的待发送提醒')
        async def reminder_cancel(interaction: discord.Interaction,编号: app_commands.Range[int,1,999999999999]):
            await self.invoke(interaction,f'取消提醒 {编号}')

        sharing = app_commands.Group(name='分享',description='查看或管理本频道主动兴趣分享',guild_only=True)
        tree.add_command(sharing)

        @sharing.command(name='状态',description='查看主动分享是否开启、暂停或等待回应')
        async def sharing_status(interaction: discord.Interaction):
            await self.invoke(interaction,'分享状态','sharing')

        @sharing.command(name='暂停',description='暂停本频道主动分享，需要管理服务器权限')
        async def sharing_pause(interaction: discord.Interaction):
            await self.invoke(interaction,'暂停分享','sharing')

        @sharing.command(name='恢复',description='恢复本频道主动分享，需要管理服务器权限')
        async def sharing_resume(interaction: discord.Interaction):
            await self.invoke(interaction,'恢复分享','sharing')

        recording = app_commands.Group(name='记录',description='管理你在本频道的记录偏好',guild_only=True)
        tree.add_command(recording)

        @recording.command(name='停止',description='清除涉及你的记忆和提醒，并停止记录你的新消息')
        async def recording_stop(interaction: discord.Interaction):
            await self.invoke(interaction,'不记录我')

        @recording.command(name='恢复',description='恢复记录你的新消息，不恢复已删除内容')
        async def recording_resume(interaction: discord.Interaction):
            await self.invoke(interaction,'恢复记录')
