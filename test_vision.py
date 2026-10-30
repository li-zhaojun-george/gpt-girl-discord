import asyncio
import copy
import io
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

import discord
from PIL import Image
from bot import GirlBot
from providers import CodexProvider
from settings import Settings
from state import State
from vision import MAX_BYTES, image_url, normalize


def picture(color='blue', fmt='PNG'):
    image = Image.new('RGB', (100, 60), color)
    data = io.BytesIO()
    image.save(data, format=fmt)
    return data.getvalue()


class VisualProvider:
    supports_images = True
    web_search = 'live'

    def __init__(self, live=None):
        self.live = live
        self.calls = []
        self.answers = []
        self.select = True

    async def structured(self, rules, payload, schema, **kwargs):
        self.calls.append({'payload': copy.deepcopy(payload), 'images': len(kwargs.get('images', [])),
                           'schema': schema, 'search': self.web_search})
        if self.live:
            provider = copy.copy(self.live)
            provider.web_search = self.web_search
            result = await provider.structured(rules, payload, schema, **kwargs)
            self.calls[-1]['result'] = copy.deepcopy(result)
            return result
        fields = schema['properties']
        if 'observations' in fields:
            return {'observations': [dict(image_id=r['image_id'], description='蓝色图片',
                visible_text='测试编号', question_details='左边是蓝色', uncertainty='') for r in payload['images']]}
        if 'common_usage' in fields:
            return dict(kind='ordinary', tone='sincere', function='none', emotion=[], common_usage='',
                current_meaning='', confidence='low', target='unknown', target_user_id='',
                evidence_ids=[], uncertainty='', reply_hint='answer')
        if 'use_images' in fields:
            return {'use_images': self.select}
        if 'topic_start_id' in fields:
            ids = [payload['explicit_reply_id']] if payload['explicit_reply_id'] else []
            return dict(action='ready' if ids else 'standalone', topic_start_id=ids[0] if ids else '',
                        relevant_ids=ids, focus_ids=ids)
        if 'addressee' in fields:
            return {'addressee': 'bot', 'action': 'reply'}
        raise AssertionError('Unexpected schema')

    async def complete(self, rules, payload):
        self.answers.append(copy.deepcopy(payload))
        if self.live:
            provider = copy.copy(self.live)
            provider.web_search = self.web_search
            return await provider.complete(rules, payload)
        return 'reply', '左边是蓝色。' if payload['visual_context']['images'] else '文字回复。'


class Scene:
    """Synthetic Discord surface: no token, network or actual messages."""
    def __init__(self, folder, live=None):
        self.provider = VisualProvider(live)
        self.messages = {}
        self.replies = []
        self.fetches = []
        self.permissions = NS(view_channel=True, read_message_history=True, send_messages=True)
        self.guild = NS(id=10, me=NS())
        self.channel = NS(id=20, name='次要聊天', guild=self.guild, permissions_for=lambda _: self.permissions)
        self.channel.fetch_message = self.fetch
        self.channel.history = self.history
        cfg = Settings({20}, observed={20}, mode='social', debounce_seconds=0, quiet_start=0, quiet_end=0)
        self.bot = GirlBot(self.provider, cfg, '你是GPT娘，用自然简短中文回复。', State(Path(folder)/'test.sqlite3'))
        self.bot._connection.user = NS(id=99)

    async def fetch(self, mid):
        self.fetches.append(mid)
        if mid not in self.messages:
            raise discord.NotFound(NS(status=404, reason='Missing'), 'Synthetic deletion')
        return self.messages[mid]

    async def history(self, limit, before, oldest_first=False):
        for msg in sorted(self.messages.values(), key=lambda m: m.id, reverse=True):
            if msg.id < before.id:
                yield msg
                limit -= 1
                if not limit:
                    break

    def make(self, mid, text='', *, image=False, user=1, reference=None, sticker=None, sticker_format=1):
        attachments = [NS(id=mid*10, filename='picture.png', content_type='image/png', size=1000,
                          url=f'https://cdn.discordapp.com/attachments/20/{mid*10}/picture.png')] if image else []
        msg = NS(id=mid, content=text, author=NS(id=user, bot=user==99, display_name=f'User{user}'),
                 guild=self.guild, channel=self.channel, attachments=attachments, webhook_id=None,
                 created_at=datetime.now(timezone.utc), reference=reference)
        msg.stickers = [discord.StickerItem(state=None, data={'id': str(sticker), 'name': 'untrusted-name',
                       'format_type': sticker_format})] if sticker else []
        async def reply(text, **kwargs):
            self.replies.append(text)
            return NS(id=100000+mid)
        msg.reply = reply
        self.messages[mid] = msg
        return msg

    def ref(self, msg):
        return NS(message_id=msg.id, channel_id=msg.channel.id, resolved=msg)

    async def send(self, msg):
        await self.bot.on_message(msg)
        if self.bot.pending:
            await asyncio.wait_for(asyncio.gather(*list(self.bot.pending.values())), 220)


class ImageFormatTests(unittest.TestCase):
    def test_static_formats_and_size(self):
        for fmt in ('PNG', 'JPEG', 'WEBP', 'GIF'):
            output = normalize(picture(fmt=fmt))
            with Image.open(io.BytesIO(output)) as img:
                self.assertEqual(img.format, 'PNG')
                self.assertEqual(img.size, (100, 60))
                self.assertFalse(img.info)

    def test_animation_and_corrupt_data_are_not_first_frame_success(self):
        output = io.BytesIO()
        Image.new('RGB', (10, 10), 'red').save(output, format='GIF', save_all=True,
                    append_images=[Image.new('RGB', (10, 10), 'blue')], duration=50)
        for data in (output.getvalue(), b'<svg/>', b'fake.png', b'x'*(MAX_BYTES+1)):
            with self.assertRaises((ValueError, OSError)):
                normalize(data)

    def test_pixel_limit_and_orientation(self):
        output = io.BytesIO()
        Image.new('1', (5000, 4100)).save(output, format='PNG')
        with self.assertRaisesRegex(ValueError, 'pixel limit'):
            normalize(output.getvalue())
        im = Image.new('RGB', (30, 70), 'red')
        exif = im.getexif(); exif[274] = 6
        output = io.BytesIO();im.save(output, format='JPEG', exif=exif)
        with Image.open(io.BytesIO(normalize(output.getvalue()))) as turned:
            self.assertEqual(turned.size, (70, 30))

    def test_only_matching_discord_attachment_urls(self):
        good='https://cdn.discordapp.com/attachments/20/30/p.png?ex=123&hm=456'
        self.assertEqual(image_url(good,20,30),good)
        for url in ('http://cdn.discordapp.com/attachments/20/30/p.png',
                    'https://cdn.discordapp.com.evil.test/attachments/20/30/p.png',
                    'https://127.0.0.1/attachments/20/30/p.png',
                    'https://cdn.discordapp.com/attachments/21/30/p.png',
                    'https://user@cdn.discordapp.com/attachments/20/30/p.png'):
            with self.assertRaises(ValueError):image_url(url,20,30)


class VisualBotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder=tempfile.TemporaryDirectory()
        self.scene=Scene(self.folder.name)
        self.download=AsyncMock(return_value=picture())
        self.patcher=patch('vision.download',self.download);self.patcher.start()

    async def asyncTearDown(self):
        await self.scene.bot.close()
        self.patcher.stop();self.folder.cleanup()

    def last_images(self):return self.scene.provider.answers[-1]['visual_context']['images']

    async def test_image_only_enters_real_pipeline_and_gate_sees_observations(self):
        await self.scene.send(self.scene.make(1,image=True))
        self.assertEqual(self.last_images()[0]['message_id'],'1')
        gate=next(r for r in self.scene.provider.calls if 'addressee' in r['schema']['properties'])
        self.assertTrue(gate['payload']['visual_context']['images'])
        self.assertTrue(self.scene.provider.answers[-1]['capabilities']['images'])
        self.assertFalse(self.scene.provider.answers[-1]['capabilities']['web_search'])
        self.assertEqual(len(self.scene.replies),1)

    async def test_mention_only_with_attachment_does_not_open_help(self):
        await self.scene.send(self.scene.make(1,'<@99>',image=True))
        self.assertTrue(self.last_images())

    async def test_reply_to_own_image_and_other_person_image_when_named(self):
        image=self.scene.make(1,image=True)
        await self.scene.send(self.scene.make(2,'这张图左边是什么？',reference=self.scene.ref(image)))
        self.assertEqual(self.last_images()[0]['message_id'],'1')
        other=self.scene.make(3,image=True,user=2)
        await self.scene.send(self.scene.make(4,'大白龙，图里有什么？',reference=self.scene.ref(other)))
        self.assertEqual(self.last_images()[0]['user_id'],'2')
        self.assertIn(3,self.scene.fetches)

    async def test_other_person_reply_without_address_does_not_interrupt(self):
        other=self.scene.make(1,image=True,user=2)
        await self.scene.send(self.scene.make(2,'这个是什么？',reference=self.scene.ref(other)))
        self.assertFalse(self.scene.provider.calls)
        self.download.assert_not_awaited()

    async def test_followup_reloads_source_and_pixels(self):
        await self.scene.send(self.scene.make(1,'<@99> 看看这张',image=True))
        await self.scene.send(self.scene.make(2,'那右边呢？'))
        await self.scene.send(self.scene.make(3,'上面那行字呢？'))
        self.assertEqual(self.download.await_count,3)
        self.assertEqual(self.last_images()[0]['message_id'],'1')
        self.assertGreaterEqual(self.scene.fetches.count(1),2)

    async def test_topic_change_removes_old_picture(self):
        await self.scene.send(self.scene.make(1,'<@99> 看图',image=True))
        self.scene.provider.select=False
        await self.scene.send(self.scene.make(2,'换个话题，什么是质数？'))
        self.assertFalse(self.last_images())
        self.assertFalse(self.scene.bot.vision.active)
        self.assertEqual(self.download.await_count,1)

    async def test_picture_and_question_in_same_burst(self):
        self.scene.bot.router.touch('10:20',1)
        await self.scene.bot.on_message(self.scene.make(1,image=True))
        await self.scene.bot.on_message(self.scene.make(2,'左边是什么？'))
        await asyncio.gather(*list(self.scene.bot.pending.values()))
        self.assertEqual(self.last_images()[0]['message_id'],'1')

    async def test_cooldown_picture_can_be_requested_later_without_reply_function(self):
        import time
        self.scene.bot.last_social['10:20']=time.monotonic()
        await self.scene.send(self.scene.make(1,image=True))
        self.download.assert_not_awaited()
        self.assertTrue(self.scene.bot.vision.active)
        await self.scene.send(self.scene.make(2,'<@99> 刚才那张图是什么？'))
        self.assertEqual(self.last_images()[0]['message_id'],'1')

    async def test_explicit_old_image_overrides_latest_image(self):
        first=self.scene.make(1,'<@99> 第一张',image=True)
        await self.scene.send(first)
        await self.scene.send(self.scene.make(2,'<@99> 第二张',image=True))
        await self.scene.send(self.scene.make(3,'<@99> 这张的左边呢？',reference=self.scene.ref(first)))
        self.assertEqual([r['message_id'] for r in self.last_images()],['1'])

    async def test_reply_to_bot_answer_follows_original_image_reference(self):
        first=self.scene.make(1,image=True)
        answer=self.scene.make(2,'蓝色图形',user=99,reference=self.scene.ref(first))
        await self.scene.send(self.scene.make(3,'右边呢？',reference=self.scene.ref(answer)))
        self.assertEqual(self.last_images()[0]['message_id'],'1')

    async def test_cross_channel_reference_never_downloads(self):
        first=self.scene.make(1,image=True)
        ref=self.scene.ref(first);ref.channel_id=21
        await self.scene.send(self.scene.make(2,'<@99> 这张是什么',reference=ref))
        self.download.assert_not_awaited()

    async def test_observation_does_not_change_mention_only_mode(self):
        self.scene.bot.settings.mode='mention'
        self.scene.bot.settings.observed=set()
        await self.scene.send(self.scene.make(1,image=True))
        self.assertFalse(self.scene.provider.calls)
        self.assertFalse(self.scene.bot.vision.active)

    async def test_deleted_source_not_replaced_by_previous_answer(self):
        await self.scene.send(self.scene.make(1,'<@99> 看图',image=True))
        del self.scene.messages[1]
        await self.scene.send(self.scene.make(2,'<@99> 右边呢？'))
        self.assertFalse(self.last_images())
        self.assertTrue(self.scene.provider.answers[-1]['visual_context']['errors'])
        self.assertEqual(self.download.await_count,1)

    async def test_changed_source_text_and_pixels_are_rejected(self):
        for changed in ('text','pixels'):
            with self.subTest(changed=changed):
                self.scene.bot.vision.active.clear()
                start=10 if changed=='text' else 20
                msg=self.scene.make(start,'<@99> 看图',image=True)
                await self.scene.send(msg)
                if changed=='text':msg.content='Edited'
                else:self.download.return_value=picture('red')
                await self.scene.send(self.scene.make(start+1,'<@99> 右边呢？'))
                self.assertFalse(self.last_images())

    async def test_source_permission_and_optout(self):
        image=self.scene.make(1,image=True,user=2)
        self.scene.bot.state.set_muted('10:20',2,True)
        await self.scene.send(self.scene.make(2,'<@99> 看图',reference=self.scene.ref(image)))
        self.download.assert_not_awaited()
        self.scene.permissions.read_message_history=False
        await self.scene.send(self.scene.make(3,'<@99> 看图',reference=self.scene.ref(image)))
        self.download.assert_not_awaited()

    async def test_image_context_is_per_person_and_non_memory_channel_edit_clears_it(self):
        await self.scene.send(self.scene.make(1,'<@99> 看图',image=True))
        await self.scene.send(self.scene.make(2,'<@99> 你好',user=2))
        self.assertFalse(self.last_images())
        self.assertTrue(self.scene.bot.vision.active)
        await self.scene.bot.on_raw_message_edit(NS(channel_id=20,guild_id=10,message_id=1,data={'attachments':[]}))
        self.assertFalse(self.scene.bot.vision.active)

    async def test_failure_is_explicit_and_does_not_claim_vision(self):
        self.download.return_value=b'not an image'
        await self.scene.send(self.scene.make(1,'<@99> 看图',image=True))
        self.assertFalse(self.last_images())
        self.assertTrue(self.scene.provider.answers[-1]['visual_context']['errors'])
        self.assertFalse(self.scene.provider.answers[-1]['capabilities']['images'])

    async def test_privacy_change_during_download_prevents_model_image_call(self):
        async def invalidate(*args):
            self.scene.bot.invalidate('10:20')
            return picture()
        self.download.side_effect=invalidate
        await self.scene.send(self.scene.make(1,'<@99> 看图',image=True))
        self.assertFalse(self.scene.provider.calls)
        self.assertFalse(self.scene.replies)


class ImageProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_image_files_attached_in_order_and_cleaned_on_success_and_failure(self):
        for failure in (False,True):
            with tempfile.TemporaryDirectory() as folder:
                with patch('providers.shutil.which',return_value='C:/codex.exe'):
                    provider=CodexProvider(folder)
                seen=[]
                async def spawn(*args,**kwargs):
                    paths=[Path(args[i+1]) for i,arg in enumerate(args) if arg=='--image']
                    self.assertEqual([p.read_bytes() for p in paths],[picture(),picture('red')])
                    seen.extend(paths)
                    self.assertEqual(args[-1],'-')
                    self.assertEqual(args[args.index('view_image')-1],'--disable')
                    output=Path(args[args.index('--output-last-message')+1])
                    async def communicate(data):output.write_text('{}',encoding='utf-8')
                    return NS(returncode=1 if failure else 0,communicate=communicate)
                with patch('providers.asyncio.create_subprocess_exec',side_effect=spawn):
                    if failure:
                        with self.assertRaises(RuntimeError):
                            await provider.structured('',{}, {},images=[picture(),picture('red')])
                    else:await provider.structured('',{}, {},images=[picture(),picture('red')])
                self.assertTrue(seen)
                self.assertFalse(list(Path(folder).iterdir()))
