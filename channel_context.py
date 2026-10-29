"""Channel names are context, never executable instructions or permissions."""
import copy

def channel_context(channel):
    name = str(getattr(channel,'name',''))[:100]
    category = str(getattr(getattr(channel,'category',None),'name',''))[:100]
    topic = str(getattr(channel,'topic',None) or '')[:300]
    theme, guidance = 'general','日常闲聊、有趣动物、食物与轻松冷知识，接续当前话题。'
    if any(w in name for w in ('桌游','棋牌')):
        theme,guidance = 'tabletop','桌游规则、策略、合作与聚会玩法；不把电子游戏混作桌游。'
    elif any(w in name for w in ('游戏','电竞','apex','Apex')):
        theme,guidance = 'games','游戏机制、团队配合、创意玩法与游戏趣事。'
    elif any(w in name for w in ('学习','知识','技术')):
        theme,guidance = 'learning','知识解释、编程、科学与学习方法，具体且有来源。'
    elif '欣赏' in name:
        theme,guidance = 'art','全年龄的绘画、摄影、音乐、动画与创作欣赏。'
    elif '吃瓜' in name:
        theme,guidance = 'discussion','公开游戏或科技趣事；核对来源，不传播私人八卦或未经证实的指控。'
    elif any(w in name for w in ('表情','整活')):
        theme,guidance = 'humor','轻松表情与无伤大雅的趣事，不针对个人羞辱。'
    formats = ['meme'] if any(w in name for w in ('图片','表情包')) else ['video'] if '视频' in name else ['meme','video','article']
    proactive = not any(w in name for w in ('黄色','色情','成人','公告','规则','人员变动','日志','待机'))
    return dict(name=name,category=category,topic=topic,theme=theme,guidance=guidance,
                formats=formats,proactive=proactive)

def channel_interests(profile,channel):
    context = channel_context(channel)
    if not context['proactive']:
        return []
    themes = {
        'games':('频道游戏兴趣',['游戏巧思','Apex团队配合','独立游戏创意','游戏幽默表情']),
        'learning':('频道学习兴趣',['科学小实验','编程知识','学习方法']),
        'tabletop':('频道桌游兴趣',['合作桌游玩法','桌游规则讲解','桌游策略','桌游幽默表情']),
        'art':('频道创作欣赏',['全年龄绘画创作','动画艺术','风景摄影','音乐创作']),
        'discussion':('频道公开趣事',['游戏开发有趣故事','科学科技趣事','轻松反应表情']),
        'humor':('频道轻松幽默',['猫的搞笑表情','游戏趣味GIF','轻松反应表情']),
    }
    if context['theme'] in themes:
        name,topics=themes[context['theme']]
        return [dict(id='channel_'+context['theme'],name=name,topics=topics,weight=3,formats=context['formats'])]
    choices=copy.deepcopy(profile['interests'])
    for item in choices:
        item['formats']=[f for f in item['formats'] if f in context['formats']]
    return [i for i in choices if i['formats']]
