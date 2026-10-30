"""Short-lived, same-speaker captions for separately sent pictures/stickers."""
import asyncio
import time
from collections import OrderedDict

import discord
from vision import image_attachments
from topic_state import source_row
from social_memory import contains_private


class MessageFragments:
    def __init__(self, clock=None):
        self.clock = clock or time.monotonic
        self.rows = OrderedDict()

    def clear(self, scope, user=None):
        for key in list(self.rows):
            if key[0] == scope and (user is None or key[1] == user):
                self.rows.pop(key, None)

    def observe(self, message, route):
        if not isinstance(getattr(message, 'id', None), int):
            return []
        scope = f'{message.guild.id}:{message.channel.id}'
        key, now = (scope, message.author.id), self.clock()
        for old in list(self.rows):
            if now - self.rows[old][-1]['received'] > 30:
                self.rows.pop(old, None)
        if route in {'other', 'closed'} or message.reference is not None:
            self.rows.pop(key, None)
            return []
        if image_attachments(message):
            # A caption on the new picture takes precedence over earlier text.
            if message.content.strip():
                self.rows.pop(key, None)
                return []
            return [dict(r) for r in self.rows.get(key, ()) if now-r['received'] <= 30]
        if message.content.strip():
            rows = self.rows.setdefault(key, [])
            rows.append({'message_id': str(message.id), 'user_id': str(message.author.id),
                         'text': message.content, 'received': now, 'already_answered': False})
            self.rows[key] = [r for r in rows[-4:] if now-r['received'] <= 30]
            self.rows.move_to_end(key)
            while len(self.rows) > 128:
                self.rows.popitem(last=False)
        return []

    def answered(self, message, linked=()):
        if not isinstance(getattr(message, 'id', None), int):
            return
        key = (f'{message.guild.id}:{message.channel.id}', message.author.id)
        ids = {str(message.id), *(r['message_id'] for r in linked)}
        for row in self.rows.get(key, ()):
            if row['message_id'] in ids:
                row['already_answered'] = True

    async def verify(self, message, rows, bot):
        if not rows:
            return []
        scope = f'{message.guild.id}:{message.channel.id}'
        permissions = message.channel.permissions_for(message.guild.me)
        if not permissions.view_channel or not permissions.read_message_history:
            return []
        verified = []
        for row in rows[-4:]:
            try:
                source = await asyncio.wait_for(message.channel.fetch_message(int(row['message_id'])), 5)
                if (source.guild.id != message.guild.id or source.channel.id != message.channel.id
                        or source.author.id != message.author.id or source.author.bot or source.webhook_id
                        or source.content != row['text'] or source.reference is not None
                        or bot.state.muted(scope, source.author.id) or contains_private(source.content)):
                    return []
            except (discord.HTTPException, asyncio.TimeoutError, AttributeError):
                return []
            verified.append({**source_row(source), 'text': source.content,
                             'already_answered': row['already_answered']})
        return verified
