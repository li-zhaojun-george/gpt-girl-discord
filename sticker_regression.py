"""Real public Discord sticker pixels + real Codex; synthetic chat, no messages sent."""
import argparse
import asyncio
import hashlib
import json
import tempfile
from pathlib import Path

from providers import CodexProvider
from test_vision import Scene

ROOT = Path(__file__).resolve().parent
CASES = [
    dict(id='delayed_cheering', text='大白龙，终于赢了', delay=11,
         sticker=1210429720743645204, function=['celebration']),
    dict(id='delayed_crying_after_wrong_bot_reply', text='大白龙，连输五局了', delay=6,
         sticker=1233250239796871179, function=['complaint', 'self_mockery', 'reaction']),
]


async def run(args):
    report = {'mode': 'real-codex-real-public-sticker-pixels-synthetic-discord', 'cases': [],
              'code_sha256': {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in ROOT.glob('*.py')}}
    for case in CASES:
        with tempfile.TemporaryDirectory(prefix='sticker-eval-') as folder:
            live = CodexProvider(Path(folder)/'runtime', args.executable, args.model, web_search='disabled')
            scene = Scene(folder, live)
            scene.bot.persona = (ROOT/'persona.txt').read_text(encoding='utf-8')
            tick = [100.0]
            scene.bot.fragments.clock = lambda: tick[0]
            started, release = asyncio.Event(), asyncio.Event()
            base = scene.provider.complete
            async def complete(rules, payload):
                # Hold only the obsolete text answer, deterministically reproducing
                # an in-flight request. All vision, interpretation and new answers
                # use the actual Codex backend; downloads are never mocked.
                if not payload['visual_context']['images']:
                    started.set()
                    await release.wait()
                    return 'reply', '旧的文字回答，不应该发送。'
                return await base(rules, payload)
            scene.provider.complete = complete
            try:
                if 'wrong' in case['id']:
                    scene.make(1, '大白龙，终于赢了')
                    scene.make(2, '我刚赢了一局，前辈的手感回来了。', user=99)
                await scene.bot.on_message(scene.make(10, case['text']))
                await asyncio.wait_for(started.wait(), 150)
                tick[0] += case['delay']
                await scene.bot.on_message(scene.make(11, sticker=case['sticker']))
                release.set()
                await asyncio.wait_for(asyncio.gather(*list(scene.bot.pending.values())), 240)
                payload = scene.provider.answers[-1] if scene.provider.answers else {}
                visual = payload.get('visual_context', {}).get('images', [])
                meaning = payload.get('meme_context', {})
                errors = []
                if len(scene.replies) != 1: errors.append('expected one replacement reply')
                if not visual or visual[0].get('kind') != 'sticker': errors.append('no sticker pixels read')
                if [r['message_id'] for r in payload.get('linked_messages', [])] != ['10']: errors.append('caption association')
                if meaning.get('function') not in case['function']: errors.append('conversational meaning')
                if meaning.get('target') == 'bot': errors.append('misattributed expression')
                answer = '\n'.join(scene.replies)
                if any(s in answer for s in ('我刚赢', '我连输', '我输了', '我的战绩', '我的手感', '我这局', '旧的文字回答')):
                    errors.append('bot claimed human experience or sent superseded answer')
                row = {'id': case['id'], 'replies': scene.replies, 'visual': visual,
                       'meaning': meaning, 'linked_messages': payload.get('linked_messages'),
                       'errors': errors, 'passed': not errors, 'trace': scene.provider.calls}
                report['cases'].append(row)
                report['passed'] = all(r['passed'] for r in report['cases'])
                report['completed'] = len(report['cases']) == len(CASES)
                Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
                print(json.dumps({k: row[k] for k in ('id', 'replies', 'errors', 'passed')}, ensure_ascii=True), flush=True)
            finally:
                release.set()
                await scene.bot.close()
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--executable', default='codex')
    parser.add_argument('--model', default='')
    parser.add_argument('--output', required=True)
    raise SystemExit(asyncio.run(run(parser.parse_args())))
