"""Conversation routing based on Discord identities, explicit addressees and recent turns."""
import re
import time
from collections import OrderedDict
from dataclasses import dataclass

@dataclass(frozen=True)
class Route:
    kind: str
    reason: str
    mentioned_users: tuple = ()
    reply_to: int | None = None

    @property
    def interaction(self):
        return self.kind in {"direct", "named", "followup"}

class ConversationRouter:
    def __init__(self, aliases, window=180, clock=time.monotonic):
        self.clock, self.window = clock, window
        self.aliases = re.compile(r"^(?:嘿|喂|欸|诶)?\s*(?:" +
                                  "|".join(re.escape(a) for a in aliases) + r")", re.I)
        self.active = OrderedDict()

    def touch(self, scope, user):
        key = (scope, user)
        self.active[key] = self.clock()
        self.active.move_to_end(key)
        while len(self.active) > 1200:
            self.active.popitem(last=False)

    def clear(self, scope):
        for key in list(self.active):
            if key[0] == scope:
                self.active.pop(key, None)

    def classify(self, message, bot_id, scope, observed, known_bot_reply=False):
        text = message.content.strip()
        targets = tuple(int(v) for v in re.findall(r"<@!?(\d+)>", text))
        reference = getattr(message, "reference", None)
        resolved = getattr(reference, "resolved", None)
        reply_to = getattr(getattr(resolved, "author", None), "id", None)
        if getattr(getattr(resolved, "channel", None), "id", message.channel.id) != message.channel.id:
            reply_to = None
        if bot_id in targets or reply_to == bot_id or known_bot_reply:
            return Route("direct", "mention_or_reply", targets, reply_to)
        if not observed:
            return Route("none", "outside_observed_channel", targets, reply_to)
        named = bool(self.aliases.match(re.sub(r"\s+", "", text)))
        if named:
            return Route("named", "vocative_alias", targets, reply_to)
        if (not targets and reply_to == message.author.id and getattr(resolved, 'attachments', None)
                and getattr(reference, 'channel_id', message.channel.id) == message.channel.id):
            return Route("followup", "own_attachment_question", targets, reply_to)
        if targets or reference is not None:
            self.active.pop((scope, message.author.id), None)
            return Route("other", "addressed_to_someone_else", targets, reply_to)
        if re.fullmatch(r"(?:不用回复了|别回了|先不聊了|结束对话)[。！!\s]*", text):
            self.active.pop((scope, message.author.id), None)
            return Route("closed", "explicit_conversation_end")
        key = (scope, message.author.id)
        updated = self.active.get(key)
        if updated is not None and self.clock()-updated <= self.window:
            return Route("followup", "recent_conversation")
        self.active.pop(key, None)
        return Route("ambient", "no_active_conversation")
