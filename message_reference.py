"""Resolve explicit chat references from a bounded same-channel Discord read."""
import asyncio
import re
from datetime import datetime,timezone
import discord

POINTER=re.compile(r'⬆|↑|上一[句条]|上条消息|上面|前面|上文')

def needs_reference(message,text):
    return getattr(message,'reference',None) is not None or bool(POINTER.search(text))

async def resolve_reference(bot,message,text):
    if not needs_reference(message,text):
        return None
    missing={'status':'unavailable','reason':'没有找到可读取的同频道原消息；请用户使用Discord回复或引用原文，不能猜成历史分享。'}
    channel=message.channel
    scope=f'{message.guild.id}:{channel.id}'
    def pack(target,previous=()):
        if getattr(getattr(target,'channel',None),'id',None)!=channel.id:
            return missing
        if bot.state.muted(scope,target.author.id):
            return missing
        group=list(reversed([target,*previous]))
        content='\n'.join(m.content[:600] for m in group) if previous else target.content[:2000]
        return {'status':'resolved','message_id':str(target.id),'message_ids':[str(m.id) for m in group],'author_id':str(target.author.id),
                'author_name':target.author.display_name[:80],'text':content,
                'has_attachments':bool(target.attachments),
                'selection':'explicit_reply' if getattr(message,'reference',None) else 'previous_human_message'}
    async def read():
        permissions=channel.permissions_for(message.guild.me)
        if not permissions.view_channel or not permissions.read_message_history:
            return missing
        ref=getattr(message,'reference',None)
        if ref is not None:
            if getattr(ref,'channel_id',channel.id)!=channel.id or not getattr(ref,'message_id',None):
                return missing
            # Fetch afresh: do not use potentially edited/deleted cached reply text.
            return pack(await channel.fetch_message(ref.message_id))
        if not isinstance(getattr(message,'id',None),int):
            return missing
        selected=[]
        async for target in channel.history(limit=20,before=message,oldest_first=False):
            if (datetime.now(timezone.utc)-target.created_at).total_seconds()>86400:
                break
            if selected:
                if (target.author.id!=selected[0].author.id or target.author.bot or target.webhook_id
                        or (selected[0].created_at-target.created_at).total_seconds()>300
                        or (target.author.id==message.author.id and target.content.strip()==message.content.strip())):
                    break
                selected.append(target)
                if len(selected)==3:
                    break
                continue
            if target.author.bot or target.webhook_id:
                continue
            # A repeated request should not point back to an earlier unsuccessful request.
            if target.author.id==message.author.id and target.content.strip()==message.content.strip():
                continue
            selected.append(target)
        return pack(selected[0],selected[1:]) if selected else missing
    try:
        return await asyncio.wait_for(read(),timeout=5)
    except (discord.HTTPException,asyncio.TimeoutError,AttributeError):
        return missing
