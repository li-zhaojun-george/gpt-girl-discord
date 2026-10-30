"""Real Codex temporal-vision replay; synthetic chat and no Discord sends."""
import argparse
import asyncio
import hashlib
import io
import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

from PIL import Image, ImageDraw, ImageFont
from providers import CodexProvider
from test_animation import vector_animation
from test_vision import Scene

ROOT=Path(__file__).resolve().parent


def reversal(font_path):
    font=ImageFont.truetype(font_path,40)
    frames=[]
    for stage in range(3):
        image=Image.new('RGB',(400,360),'white');d=ImageDraw.Draw(image)
        d.ellipse((100,25,300,225),fill='#ffd550',outline='black',width=4)
        d.ellipse((150,90,162,108),fill='black');d.ellipse((238,90,250,108),fill='black')
        if stage==0:d.arc((145,100,255,185),0,180,fill='black',width=6)
        elif stage==1:d.ellipse((185,145,215,190),fill='black')
        else:
            d.arc((145,160,255,210),180,360,fill='black',width=6)
            d.rectangle((150,110,162,180),fill='#0099ff');d.rectangle((238,110,250,180),fill='#0099ff')
        d.text((105,260),['稳了','等等','又输了'][stage],font=font,fill='black')
        frames.append(image)
    out=io.BytesIO()
    frames[0].save(out,format='PNG',save_all=True,append_images=frames[1:],duration=[700,300,1200],loop=0)
    return out.getvalue()


def moving_gif():
    frames=[]
    for x in (40,110,220,350,220,40):
        image=Image.new('RGB',(400,180),'white')
        ImageDraw.Draw(image).ellipse((x-20,70,x+20,110),fill='blue')
        frames.append(image)
    out=io.BytesIO()
    frames[0].save(out,format='GIF',save_all=True,append_images=frames[1:],duration=250,loop=0)
    return out.getvalue()


async def run(args):
    cases=[
        dict(id='reversal_in_context',fmt=2,data=reversal(args.font),caption='大白龙，这把开局还以为稳了，结果又输了。',text='',
             followup='它是先哭还是先笑？最后写的什么？'),
        dict(id='gif_returns_to_start',fmt=4,data=moving_gif(),text='大白龙，这个蓝色圆点是一直往右走吗？'),
        dict(id='lottie_moves_right',fmt=3,data=json.dumps(vector_animation()).encode(),text='大白龙，红色圆点向哪边移动？'),
        dict(id='public_apng',fmt=2,sticker=1228092333061443654,text='大白龙，这个动画里的角色动作有什么变化？'),
        dict(id='public_lottie',fmt=3,sticker=796140620111544330,text='大白龙，这个动画里的角色动作有什么变化？'),
    ]
    report={'mode':'real-codex-temporal-vision-synthetic-chat','cases':[],
        'code_sha256':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in ROOT.glob('*.py')}}
    for case in cases:
        if args.case and case['id'] not in args.case:continue
        with tempfile.TemporaryDirectory(prefix='animation-eval-') as folder:
            scene=Scene(folder,CodexProvider(Path(folder)/'runtime',args.executable,args.model,web_search='disabled'))
            scene.bot.persona=(ROOT/'persona.txt').read_text(encoding='utf-8')
            patcher=patch('vision.download',AsyncMock(return_value=case['data'])) if 'data' in case else None
            if patcher:patcher.start()
            try:
                if case.get('caption'):
                    # Keep the text in the existing debounce inbox so the test
                    # exercises separate message association deterministically.
                    await scene.bot.on_message(scene.make(1,case['caption']))
                await scene.send(scene.make(2,case['text'],sticker=case.get('sticker',123),sticker_format=case['fmt']))
                first=scene.provider.answers[-1] if scene.provider.answers else {}
                if case.get('followup'):await scene.send(scene.make(3,case['followup']))
                last=scene.provider.answers[-1] if scene.provider.answers else {}
                visual=first.get('visual_context',{}).get('images',[])
                errors=[]
                if not scene.replies or not visual:errors.append('no visual answer')
                if visual and not visual[0].get('animation',{}).get('animated'):errors.append('not temporal input')
                text='\n'.join(scene.replies)
                if case['id']=='reversal_in_context':
                    meaning=first.get('meme_context',{})
                    if meaning.get('function') not in {'self_mockery','complaint','reaction'}:errors.append('context function')
                    if meaning.get('target')=='bot':errors.append('human experience assigned to bot')
                    if '又输了' not in text or not any(w in text for w in ('先笑','先是笑','开始是笑','开始笑','先微笑','先高兴')):
                        errors.append('temporal followup or last-frame OCR')
                    if not last.get('visual_context',{}).get('images'):errors.append('followup lost source')
                if case['id']=='gif_returns_to_start' and not any(w in text for w in ('左','来回','折返','返回')):errors.append('missed reversal')
                if case['id']=='lottie_moves_right' and '右' not in text:errors.append('missed rightward movement')
                if any(w in text for w in ('我刚赢','我连输','我的战绩','我这局输了')):errors.append('experience attribution')
                row={'id':case['id'],'replies':scene.replies,'visual':visual,'meaning':first.get('meme_context',{}),
                     'passed':not errors,'errors':errors,'trace':scene.provider.calls}
                report['cases'].append(row)
                report['passed']=all(r['passed'] for r in report['cases'])
                Path(args.output).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
                print(json.dumps({k:row[k] for k in ('id','replies','passed','errors')},ensure_ascii=True),flush=True)
            finally:
                if patcher:patcher.stop()
                await scene.bot.close()
    if not report['cases']:raise ValueError('No selected case')
    return 0 if report['passed'] else 1


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--executable',default='codex')
    parser.add_argument('--model',default='')
    parser.add_argument('--font',default='C:/Windows/Fonts/msyh.ttc')
    parser.add_argument('--output',required=True)
    parser.add_argument('--case',action='append')
    raise SystemExit(asyncio.run(run(parser.parse_args())))
