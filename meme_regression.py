"""Real Codex replay with synthetic static memes; no Discord network/messages."""
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
from test_vision import Scene

ROOT = Path(__file__).resolve().parent
CASES = [
    dict(id='same_image_win', history=[(1,'终于赢了！刚才那局配合太好了。'),(99,'这次配合打出来了。')],
         text='大白龙', functions=['celebration'], tones=['sincere','playful'], kind='meme'),
    dict(id='same_image_loss', history=[(1,'连输五局，分掉光了，今晚这战绩真是没眼看。'),(99,'这几局确实不顺。')],
         text='大白龙', functions=['self_mockery','complaint'], tones=['ironic','mixed','playful'], kind='meme'),
    dict(id='no_context', history=[],text='大白龙，这张图一般怎么用？',kind='meme', low=True),
    dict(id='explicit_correction', history=[(1,'这回总算成功了。'),(99,'你这是在阴阳怪气吗？')],
         text='大白龙，我是在真心庆祝成功，不是阴阳怪气。', functions=['celebration'],tones=['sincere','playful'],kind='meme'),
    dict(id='multiplayer_not_targeting_other',history=[(1,'我又输掉一局，最后还是我自己失误了。'),(2,'我今晚吃面。')],
         text='大白龙',functions=['self_mockery','complaint'],kind='meme',not_person=True),
    dict(id='ordinary_screenshot',history=[(1,'今晚看看吃哪款面。')],text='大白龙，哪款面便宜，多少钱？',
         kind='ordinary',words=['18','猪肚']),
]


def render(font_path, ordinary=False):
    im=Image.new('RGB',(800,520),'white');d=ImageDraw.Draw(im)
    font=ImageFont.truetype(font_path,52)
    if ordinary:
        d.text((30,50),'今日菜单',font=font,fill='black')
        d.text((30,160),'猪肚鸡面 18元',font=font,fill='black')
        d.text((30,270),'番茄牛肉面 22元',font=font,fill='black')
    else:
        d.ellipse((220,50,580,380),fill='#ffdc52',outline='black',width=5)
        d.ellipse((310,135,330,165),fill='black');d.ellipse((470,135,490,165),fill='black')
        d.arc((290,160,510,320),0,180,fill='black',width=12)
        d.text((340,410),'好耶',font=font,fill='black')
    out=io.BytesIO();im.save(out,format='PNG');return out.getvalue()


def assess(case, replies, context):
    errors=[]
    if not replies:errors.append('missing reply')
    if context.get('kind')!=case['kind']:errors.append('image kind')
    if case.get('functions') and context.get('function') not in case['functions']:errors.append('conversational function')
    if case.get('tones') and context.get('tone') not in case['tones']:errors.append('tone')
    if case.get('low') and context.get('confidence')=='high':errors.append('overconfident without context')
    if case.get('not_person') and context.get('target')=='person':errors.append('blamed unrelated person')
    answer='\n'.join(replies)
    if any(w not in answer for w in case.get('words',[])):errors.append('missing visual fact')
    if not case.get('low') and any(w in answer for w in ('置信度','情绪标签','语气：','含义：')):errors.append('analysis report instead of chat')
    if len(answer)>220:errors.append('overlong reply')
    return errors


async def run(args):
    cases=[c for c in CASES if not args.case or c['id'] in args.case]
    if not cases or (args.case and set(args.case)-{c['id'] for c in cases}):raise ValueError('Unknown case')
    output=Path(args.output);output.parent.mkdir(parents=True,exist_ok=True)
    report={'mode':'real-codex-synthetic-discord','model':args.model,'cases':[],
        'code_sha256':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in ROOT.glob('*.py')}}
    for case in cases:
        with tempfile.TemporaryDirectory(prefix='meme-eval-') as folder:
            provider=CodexProvider(Path(folder)/'runtime',args.executable,args.model,web_search='disabled')
            scene=Scene(folder,provider)
            scene.bot.persona=(ROOT/'persona.txt').read_text(encoding='utf-8')
            try:
                for index,(user,text) in enumerate(case['history'],1):scene.make(index,text,user=user)
                with patch('vision.download',AsyncMock(return_value=render(args.font,case['kind']=='ordinary'))):
                    await scene.send(scene.make(100,case['text'],image=True))
                payload=scene.provider.answers[-1] if scene.provider.answers else {}
                interpretation=payload.get('meme_context',{})
                errors=assess(case,scene.replies,interpretation)
                row={'id':case['id'],'replies':scene.replies,'interpretation':interpretation,'errors':errors,
                     'passed':not errors,'trace':scene.provider.calls}
                report['cases'].append(row)
                report['passed']=all(c['passed'] for c in report['cases'])
                report['completed']=len(report['cases'])==len(cases)
                output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
                print(json.dumps({k:row[k] for k in ('id','replies','errors','passed')},ensure_ascii=True),flush=True)
            finally:await scene.bot.close()
    return 0 if report['passed'] else 1


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--executable',default='codex')
    parser.add_argument('--model',default='')
    parser.add_argument('--font',default='C:/Windows/Fonts/msyh.ttc',help='Path to a Chinese-capable font')
    parser.add_argument('--case',action='append')
    parser.add_argument('--output',default='eval-reports/meme-live.json')
    raise SystemExit(asyncio.run(run(parser.parse_args())))

if __name__=='__main__':main()
