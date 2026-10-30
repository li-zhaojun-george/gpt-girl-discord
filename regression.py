"""Fixed synthetic conversations, replayed through the real bot without Discord networking."""
import argparse
import asyncio
import copy
import hashlib
import json
import os
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace as NS
from zoneinfo import ZoneInfo

import discord
from bot import GirlBot
from providers import CodexProvider
from settings import Settings
from social_memory import validate
from state import State
from topic_state import source_row

ROOT=Path(__file__).resolve().parent


def load_suite(path=ROOT/'evals/group_chat.json'):
    suite=json.loads(Path(path).read_text(encoding='utf-8-sig'))
    if suite.get('version')!=1 or not suite.get('cases'):
        raise ValueError('Unsupported or empty regression suite')
    ids=[c['id'] for c in suite['cases']]
    if len(ids)!=len(set(ids)):
        raise ValueError('Duplicate case IDs')
    for case in suite['cases']:
        if case['transcript'] not in suite['transcripts'] or type(case['expect']['reply']) is not bool:
            raise ValueError('Invalid case')
    return suite


class ReplayProvider:
    """Offline oracle checks plumbing only; live mode delegates all decisions to Codex."""
    web_search='disabled'
    def __init__(self,case,live=None):
        self.case,self.live=case,live
        self.calls=[];self.answers=[]

    async def structured(self,rules,payload,schema,**kwargs):
        if kwargs.get('require_search'):
            raise ValueError('Regression must not search the web')
        keys=set(schema['properties'])
        if 'topic_start_id' in keys: phase='context'
        elif 'queries' in keys: phase='query'
        elif 'memory_ids' in keys: phase='memory_'+payload['phase']
        elif 'addressee' in keys: phase='turn'
        else: raise ValueError('Unexpected model phase')
        if self.live:
            provider=copy.copy(self.live);provider.web_search='disabled'
            raw=await provider.structured(rules,payload,schema)
        elif phase=='context':
            chosen=list(map(str,self.case.get('oracle_context',[])))
            action='ready' if chosen else 'unclear' if self.case.get('oracle_unclear') else 'standalone'
            raw=dict(action=action,topic_start_id=chosen[0] if chosen else '',relevant_ids=chosen,focus_ids=chosen[-1:])
        elif phase=='query':
            raw=dict(queries=['键盘 声音 实测 饮品 红茶 咖啡'],person_ids=['1'],start_date='',end_date='',time_mode='none')
        elif phase.startswith('memory_'):
            # Map expected SOURCE IDs to actual DB IDs rather than assuming insertion order.
            selected=[]
            for row in payload['candidates']:
                if phase=='memory_verify':
                    proofs={e['message_id'] for e in row['live_evidence']}
                else:
                    proofs=self.source_ids.get(row['id'],set())
                if proofs & set(map(str,self.case.get('oracle_memory_sources',[]))):
                    selected.append(row['id'])
            raw={'memory_ids':selected}
        else:
            target=self.case.get('oracle_gate','bot')
            raw=dict(addressee=target,action='reply' if target in {'bot','group'} else 'silence')
        self.calls.append({'phase':phase,'result':copy.deepcopy(raw)})
        return raw

    async def complete(self,rules,payload):
        self.answers.append(copy.deepcopy(payload))
        if self.live:
            provider=copy.copy(self.live);provider.web_search='disabled'
            return await provider.complete(rules,payload)
        return 'reply',self.case.get('oracle_answer','合成回答')


def assess(case,result):
    expected=case['expect'];errors=[]
    def check(ok,reason):
        if not ok: errors.append(reason)
    check(bool(result['replies'])==expected['reply'],'reply/silence mismatch')
    for key in ('final_calls','model_calls','topic_count','remaining_topics'):
        if key in expected: check(result[key]==expected[key],key+' mismatch')
    for key in ('memory_owners','memory_sources','stored_sources'):
        if key in expected: check(set(result[key])==set(expected[key]),key+' mismatch')
    ids=set(result['context_ids'])
    check(set(map(str,expected.get('context_contains',[])))<=ids,'missing expected context')
    check(not set(map(str,expected.get('context_excludes',[])))&ids,'unrelated context selected')
    check(set(map(str,expected.get('source_fetches',[])))<=set(result['source_fetches']),'missing source revalidation')
    if expected.get('pending_empty'): check(not any(result['pending']),'finished topic still pending')
    answer='\n'.join(result['replies']).casefold()
    if expected.get('answer_any'): check(any(w.casefold() in answer for w in expected['answer_any']),'answer lacks expected subject')
    for word in expected.get('answer_absent',[]): check(word.casefold() not in answer,'answer includes forbidden subject: '+word)
    return errors


async def run_case(case,suite,live=None):
    started=time.monotonic();provider=ReplayProvider(case,live)
    with tempfile.TemporaryDirectory(prefix='gpt-girl-regression-') as folder:
        dbpath=Path(folder)/'state.sqlite3'
        now=datetime.now(ZoneInfo('America/Toronto')).replace(hour=12,minute=0,second=0,microsecond=0)
        cfg=Settings({20,21},observed={20,21},memory_channels={20,21},mode='social',debounce_seconds=0,
                     quiet_start=0,quiet_end=0)
        replies=[];fetches=[];sources={}
        guild=NS(id=10,me=NS())
        channel=NS(id=20,name='次要聊天',guild=guild,
                   permissions_for=lambda _:NS(view_channel=True,read_message_history=True,send_messages=True))
        async def send(text,**kwargs):
            replies.append(text)
            return NS(id=2000)
        async def fetch(mid):
            fetches.append(str(mid))
            if mid in case.get('deleted',[]) or mid not in sources:
                raise discord.NotFound(NS(status=404,reason='Not found'),'Synthetic deleted source')
            return sources[mid]
        async def history(limit,before=None,oldest_first=False):
            if case.get('history'):
                for m in sorted(sources.values(),key=lambda m:m.id,reverse=True):
                    if (before is None or m.id<before.id) and m.id not in case.get('deleted',[]):
                        yield m
                        limit-=1
                        if limit<=0: break
        channel.fetch_message=fetch;channel.history=history
        def message(mid,user,text,stamp):
            return NS(id=mid,author=NS(id=user,display_name={1:'Lin',2:'Ryan',99:'GPT娘'}[user],name=f'user{user}',bot=user==99),
                      guild=guild,channel=channel,content=text,created_at=stamp,reference=None,attachments=[],
                      webhook_id=None,reply=send)
        for row in suite['transcripts'][case['transcript']]:
            sources[row['id']]=message(row['id'],row['user'],row['text'],now-timedelta(hours=row['age_hours']))
        def new_bot():
            b=GirlBot(provider,cfg,'你是群聊里的GPT娘，用自然简短的中文交流。',State(dbpath))
            b._connection.user=NS(id=99);b.now=lambda:now
            return b
        bot=new_bot()
        try:
            for mid in case.get('memories',[]):
                m=sources[mid];row=source_row(m)
                item=dict(kind='preference',category='food',owner=str(m.author.id),key='饮品口味',summary=m.content,
                          certainty='explicit',evidence=[dict(message_id=str(mid),quote=m.content)])
                valid=validate({'memories':[item]},{str(mid):row},{str(mid)})
                if len(valid)!=1: raise ValueError('Invalid synthetic memory')
                bot.memory.store.save('10:20',valid,time.time())
                bot.memory.store.observe_person('10:20',m.author.id,[m.author.display_name])
            if case.get('topic'):
                topic=case['topic']
                bot.topics.store.save('10:20',{'topics':[dict(topic_id='',title=topic['title'],
                    background=['Lin在选择键盘，Ryan建议比较声音实测'],progress=topic['progress'],pending=topic['pending'],
                    agreements=[],source_ids=list(map(str,topic['sources'])))]},
                    [source_row(sources[i]) for i in topic['sources']],[])
            if case.get('restart'):
                await bot.close();bot=new_bot()
                if bot.context or bot.router.active: raise AssertionError('Restart must have empty transient state')
            provider.source_ids={r['id']:{e['message_id'] for e in r['evidence']}
                                 for r in bot.memory.store.rows('10:20',time.time(),limit=None)}
            if case.get('active'):
                bot.router.touch('10:20',1)
                bot.state.save_exchange('10:20',1,'键盘怎么选','比较一下两款键盘的实测。')
            current=message(1000,1,case['text'],now)
            if case.get('reply_to'):
                target=sources[case['reply_to']]
                current.reference=NS(message_id=target.id,channel_id=20,resolved=target)
            await bot.on_message(current)
            if bot.pending:
                await asyncio.wait_for(asyncio.gather(*list(bot.pending.values())),timeout=200)
            payload=provider.answers[-1] if provider.answers else {}
            reference=payload.get('reference_context') or {}
            memory=payload.get('social_memory') or {}
            used=[r for group in memory.values() for r in group]
            topics=reference.get('topic_states',[])
            result=dict(id=case['id'],category=case['category'],replies=replies,
                final_calls=len(provider.answers),model_calls=len(provider.calls)+len(provider.answers),
                context_ids=[r['message_id'] for r in reference.get('messages',[])],
                topic_count=len(topics),pending=[t['pending'] for t in topics],
                remaining_topics=len(bot.topics.store.rows('10:20',30)),
                memory_owners=sorted({r['owner'] for r in used}),
                memory_sources=sorted({e['message_id'] for r in used for e in r['live_evidence']}),
                stored_sources=sorted({e['message_id'] for r in bot.memory.store.rows('10:20',time.time(),limit=None) for e in r['evidence']}),
                source_fetches=fetches,trace=provider.calls,seconds=round(time.monotonic()-started,2))
            result['errors']=assess(case,result);result['passed']=not result['errors']
            return result
        finally:
            await bot.close()


async def run_suite(args):
    suite=load_suite(args.suite)
    selected=[c for c in suite['cases'] if not args.case or c['id'] in args.case]
    if not selected or (args.case and set(args.case)-{c['id'] for c in selected}):
        raise ValueError('Unknown or empty case selection')
    if not 1<=args.repeat<=10:
        raise ValueError('Repeat must be 1..10')
    selected=[(run,case) for run in range(1,args.repeat+1) for case in selected]
    report=dict(version=1,mode='live' if args.live else 'offline',model=args.model if args.live else 'scripted-oracle',
                started=datetime.now().astimezone().isoformat(),repeat=args.repeat,
                suite_sha256=hashlib.sha256(Path(args.suite).read_bytes()).hexdigest(),
                code_sha256={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(ROOT.glob('*.py'))},cases=[])
    output=Path(args.output);output.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='gpt-girl-eval-model-') as runtime:
        live=CodexProvider(runtime,args.executable,args.model,web_search='disabled') if args.live else None
        for run,case in selected:
            try:
                result=await asyncio.wait_for(run_case(case,suite,live),timeout=240)
            except Exception as exc:
                result=dict(id=case['id'],category=case['category'],passed=False,errors=[type(exc).__name__+': '+str(exc)])
            result['run']=run
            report['cases'].append(result)
            report['passed']=all(c['passed'] for c in report['cases'])
            report['completed']=len(report['cases'])==len(selected)
            report['summary']={category:{'passed':sum(r['passed'] for r in report['cases'] if r['category']==category),
                'total':sum(r['category']==category for r in report['cases'])} for category in sorted({r['category'] for r in report['cases']})}
            output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
            print(json.dumps({k:result[k] for k in ('id','passed','errors')},ensure_ascii=True),flush=True)
    return 0 if report['passed'] and report['completed'] else 1


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--suite',default=str(ROOT/'evals/group_chat.json'))
    parser.add_argument('--live',action='store_true',help='Use actual Codex login; never Discord or paid API fallback')
    parser.add_argument('--executable',default=os.getenv('CODEX_EXECUTABLE','codex'))
    parser.add_argument('--model',default=os.getenv('CODEX_MODEL',''))
    parser.add_argument('--case',action='append')
    parser.add_argument('--repeat',type=int,default=1,help='Repeat each case independently, retaining every outcome')
    parser.add_argument('--output',default=str(ROOT/'eval-reports/latest.json'))
    raise SystemExit(asyncio.run(run_suite(parser.parse_args())))


if __name__=='__main__': main()
