"""Mention-only Discord character bot. No privileged intents required."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from collections import OrderedDict
from pathlib import Path

import discord
from dotenv import load_dotenv
from openai import AsyncOpenAI
from budget import DailyBudget, EXHAUSTED

ROOT = Path(__file__).resolve().parent
LOG = logging.getLogger("gpt-girl")
MAX_INPUT = 2000
MAX_SESSIONS = 300
HISTORY_TURNS = 6
TTL = 1800
COOLDOWN = 8


def mentioned_text(content: str, bot_id: int) -> str | None:
    pattern = rf"<@!?{bot_id}>"
    if not re.search(pattern, content):
        return None
    return re.sub(pattern, "", content).strip()


class GirlBot(discord.Client):
    def __init__(self, api, model: str, allowed: set[int], persona: str, budget=None):
        intents = discord.Intents.none()
        intents.guilds = True
        intents.guild_messages = True
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self.api, self.model, self.allowed, self.persona = api, model, allowed, persona
        self.sessions = OrderedDict()
        self.cooldowns = OrderedDict()
        self.busy = set()
        self.active = 0
        self.budget = budget

    async def on_ready(self):
        LOG.info("GPT 娘已上线，bot_id=%s；等待正文中的 @。", self.user.id)
        LOG.info("Discord connected; guild_count=%s", len(self.guilds))

    async def respond(self, message, text):
        try:
            await message.reply(text, mention_author=False,
                                allowed_mentions=discord.AllowedMentions.none())
            LOG.info("Reply sent; channel=%s", message.channel.id)
        except discord.HTTPException as exc:
            LOG.warning("Discord 发送失败 (%s)", type(exc).__name__)

    async def on_message(self, message):
        if message.author.bot or message.webhook_id or message.guild is None or self.user is None:
            return
        if self.allowed and message.channel.id not in self.allowed:
            return
        text = mentioned_text(message.content, self.user.id)
        if text is None:
            return
        LOG.info("Direct mention received; channel=%s", message.channel.id)
        if self.budget:
            try:
                if self.budget.exhausted():
                    await self.respond(message, EXHAUSTED)
                    return
            except Exception:
                LOG.error('Budget unavailable; API disabled')
                await self.respond(message, EXHAUSTED)
                return
        key = (message.guild.id, message.channel.id, message.author.id)
        now = time.monotonic()
        user_key = (message.guild.id, message.author.id)
        if key in self.busy or now - self.cooldowns.get(user_key, -COOLDOWN) < COOLDOWN:
            return
        self.cooldowns[user_key] = now
        self.cooldowns.move_to_end(user_key)
        while len(self.cooldowns) > MAX_SESSIONS * 4:
            self.cooldowns.popitem(last=False)
        # Opportunistic expiry; memory stays in RAM only.
        for old_key, (updated, _) in list(self.sessions.items()):
            if now - updated > TTL:
                del self.sessions[old_key]
        if text.lower() in {"清空记忆", "reset"}:
            self.sessions.pop(key, None)
            await self.respond(message, "好，这个频道里我们之前的对话记忆已经清空啦。")
            return
        if not text or text.lower() in {"帮助", "help"}:
            await self.respond(message, "在 @ 后面写想说的话就好～\n`@我 清空记忆` 可清空你在本频道的记忆。\n只记住你与我的最近 6 轮对话，闲置 30 分钟过期，重启清空；被 @ 的文字会发给 OpenAI API。当前只支持文字。")
            return
        if len(text) > MAX_INPUT:
            await self.respond(message, "这段有点长，缩到 2000 字符以内再发给我吧。")
            return
        if self.active >= 3:
            await self.respond(message, "我还在接前面的话，稍等一会儿再 @ 我吧。")
            return
        self.busy.add(key)
        self.active += 1
        try:
            history = self.sessions.get(key, (now, []))[1]
            user_item = {"role": "user", "content": json.dumps({
                "user_id": str(message.author.id),
                "display_name": message.author.display_name,
                "text": text,
            }, ensure_ascii=False)}
            reservation = None
            if self.budget:
                try:
                    reservation = self.budget.reserve(self.persona, [*history, user_item], 600)
                except Exception:
                    LOG.error('Budget reservation failed; API disabled')
                if reservation is None:
                    await self.respond(message, EXHAUSTED)
                    return
            async with message.channel.typing():
                response = await self.api.responses.create(
                    model=self.model, instructions=self.persona,
                    input=[*history, user_item], max_output_tokens=600, store=False,
                    service_tier="default",
                    **({"reasoning": {"effort": "none"}}
                       if self.model == "gpt-6-luna" else {}),
                )
            if self.budget:
                self.budget.settle(reservation, response.usage)
            answer = (response.output_text or "").strip()
            LOG.info("Model request completed; has_text=%s", bool(answer))
            if not answer:
                await self.respond(message, "刚才没组织好语言，再叫我一次试试？")
                return
            if len(answer) > 1900:
                answer = answer[:1850] + "\n（这次先说到这里。）"
            self.sessions[key] = (time.monotonic(), [*history, user_item,
                {"role": "assistant", "content": answer}][-HISTORY_TURNS * 2:])
            self.sessions.move_to_end(key)
            while len(self.sessions) > MAX_SESSIONS:
                self.sessions.popitem(last=False)
            await self.respond(message, answer)
        except Exception as exc:
            # Do not log credentials, prompts, API response bodies, or chat text.
            LOG.warning("请求失败 (%s)", type(exc).__name__)
            await self.respond(message, "连接暂时出了点问题，稍后再试一下吧。管理员可以查看本机日志中的错误类型。")
        finally:
            self.busy.discard(key)
            self.active -= 1


async def main():
    load_dotenv(ROOT / ".env")
    required = ("DISCORD_BOT_TOKEN", "OPENAI_API_KEY", "OPENAI_MODEL")
    missing = [name for name in required if not os.getenv(name, "").strip()]
    if missing:
        raise SystemExit("请在 .env 填写：" + ", ".join(missing))
    try:
        allowed = {int(x.strip()) for x in os.getenv("ALLOWED_CHANNEL_IDS", "").split(",") if x.strip()}
    except ValueError:
        raise SystemExit("ALLOWED_CHANNEL_IDS 必须是逗号分隔的数字频道 ID。")
    if os.environ['OPENAI_MODEL'] != 'gpt-6-luna':
        raise SystemExit('Daily budget pricing is configured only for gpt-6-luna.')
    budget = DailyBudget(ROOT / 'daily-budget.sqlite3')
    async with AsyncOpenAI(timeout=35.0, max_retries=0, base_url='https://api.openai.com/v1') as api:
        async with GirlBot(api, os.environ["OPENAI_MODEL"], allowed,
                           (ROOT / "persona.txt").read_text(encoding="utf-8"), budget) as bot:
            await bot.start(os.environ["DISCORD_BOT_TOKEN"])


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    LOG.setLevel(logging.INFO)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
