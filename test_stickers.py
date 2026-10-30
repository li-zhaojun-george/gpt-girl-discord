import asyncio
import tempfile
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock, patch

from message_fragments import MessageFragments
from meme_context import validate
from test_meme_context import meaning
from test_vision import Scene, picture
from vision import StickerImage, download, sticker_url


class StickerFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.scene = Scene(self.tmp.name)
        self.seconds = 100.0
        self.scene.bot.fragments = MessageFragments(lambda: self.seconds)
        self.download_patch = patch('vision.download', AsyncMock(return_value=picture()))
        self.download = self.download_patch.start()

    async def asyncTearDown(self):
        await self.scene.bot.close()
        self.download_patch.stop()
        self.tmp.cleanup()

    async def test_discord_sticker_pixels_reach_backend_and_followup_refetches(self):
        await self.scene.send(self.scene.make(1, '<@99> 看这个', sticker=1210429720743645204))
        payload = self.scene.provider.answers[-1]
        self.assertEqual(payload['visual_context']['images'][0]['kind'], 'sticker')
        self.assertTrue(any(c['images'] for c in self.scene.provider.calls))
        self.assertNotIn('untrusted-name', str(self.scene.provider.calls))
        await self.scene.send(self.scene.make(2, '左手拿着什么？'))
        self.assertIn(1, self.scene.fetches)
        self.assertEqual(self.download.await_count, 2)

    async def test_delayed_sticker_supersedes_inflight_text_at_six_and_eleven_seconds(self):
        for delay, text in [(11, '大白龙，终于赢了'), (6, '大白龙，连输五局了')]:
            with self.subTest(delay=delay):
                started, release = asyncio.Event(), asyncio.Event()
                base = self.scene.provider.complete
                async def complete(rules, payload):
                    if not payload['visual_context']['images']:
                        started.set()
                        await release.wait()
                    return await base(rules, payload)
                self.scene.provider.complete = complete
                mid = delay * 10
                before = len(self.scene.replies)
                await self.scene.bot.on_message(self.scene.make(mid, text))
                await asyncio.wait_for(started.wait(), 2)
                self.seconds += delay
                await self.scene.bot.on_message(self.scene.make(mid+1, sticker=1233250239796871179))
                release.set()
                await asyncio.wait_for(asyncio.gather(*self.scene.bot.pending.values()), 5)
                self.assertEqual(len(self.scene.replies), before+1)
                payload = self.scene.provider.answers[-1]
                self.assertIn(text, payload['text'])
                self.assertIn(str(mid), [r['message_id'] for r in payload['linked_messages']])
                self.assertEqual(payload['speaker']['id'], '1')
                self.assertEqual(payload['bot_identity']['id'], '99')
                self.assertTrue(payload['visual_context']['images'])
                self.scene.provider.complete = base
                self.scene.bot.invalidate('10:20')
                self.seconds += 40

    async def test_already_answered_text_is_marked_for_nonrepetitive_followup(self):
        await self.scene.send(self.scene.make(1, '大白龙，终于赢了'))
        self.seconds += 11
        await self.scene.send(self.scene.make(2, sticker=123))
        linked = self.scene.provider.answers[-1]['linked_messages']
        self.assertEqual(linked[0]['message_id'], '1')
        self.assertTrue(linked[0]['already_answered'])

    async def test_incomplete_older_topic_does_not_block_verified_caption_and_pixels(self):
        await self.scene.send(self.scene.make(1, '大白龙，终于赢了'))
        with patch('bot.resolve_reference', AsyncMock(return_value={'status': 'unavailable'})):
            await self.scene.send(self.scene.make(2, sticker=123))
        payload = self.scene.provider.answers[-1]
        self.assertTrue(payload['reference_context']['partial'])
        self.assertEqual(payload['reference_context']['focus_ids'], ['1'])
        self.assertTrue(payload['visual_context']['images'])
        self.assertNotIn('我还没理清', self.scene.replies[-1])

    async def test_own_and_named_other_sticker_references(self):
        own = self.scene.make(1, sticker=123)
        await self.scene.send(self.scene.make(2, '这是什么表情？', reference=self.scene.ref(own)))
        self.assertEqual(self.scene.provider.answers[-1]['visual_context']['images'][0]['message_id'], '1')
        other = self.scene.make(3, sticker=456, user=2)
        before = len(self.scene.provider.answers)
        await self.scene.send(self.scene.make(4, '这是什么？', reference=self.scene.ref(other)))
        self.assertEqual(len(self.scene.provider.answers), before)
        await self.scene.send(self.scene.make(5, '大白龙，这图呢？', reference=self.scene.ref(other)))
        self.assertEqual(self.scene.provider.answers[-1]['visual_context']['images'][0]['user_id'], '2')

    async def test_unsupported_sticker_not_claimed_as_seen_or_named_as_evidence(self):
        for fmt in (0, 5):
            await self.scene.send(self.scene.make(fmt, '<@99> 看这张', sticker=123, sticker_format=fmt))
            payload = self.scene.provider.answers[-1]
            self.assertFalse(payload['capabilities']['images'])
            self.assertTrue(payload['visual_context']['errors'])
        self.download.assert_not_awaited()

    async def test_caption_source_changed_or_deleted_is_not_reused(self):
        for change in ('edit', 'delete'):
            text = self.scene.make(1, '大白龙，终于赢了')
            await self.scene.send(text)
            sticker = self.scene.make(2, sticker=123)
            linked = self.scene.bot.fragments.observe(sticker, 'followup')
            if change == 'edit': text.content = '改过的内容'
            else: del self.scene.messages[1]
            self.assertEqual(await self.scene.bot.fragments.verify(sticker, linked, self.scene.bot), [])
            self.scene.bot.fragments.clear('10:20')

    async def test_removed_sticker_and_privacy_invalidation_clear_followup(self):
        msg = self.scene.make(1, '<@99> 看这个', sticker=123)
        await self.scene.send(msg)
        msg.stickers = []
        await self.scene.send(self.scene.make(2, '它左边呢？'))
        self.assertFalse(self.scene.provider.answers[-1]['visual_context']['images'])
        await self.scene.send(self.scene.make(3, '大白龙，终于赢了'))
        await self.scene.bot.on_raw_message_edit(NS(channel_id=20, guild_id=10, message_id=3, data={'sticker_items': []}))
        self.assertFalse(self.scene.bot.fragments.rows)

    async def test_sticker_and_attachment_with_same_id_remain_distinct(self):
        await self.scene.send(self.scene.make(1, '<@99> 比较', image=True, sticker=10))
        rows = self.scene.provider.answers[-1]['visual_context']['images']
        self.assertEqual({r['kind'] for r in rows}, {'attachment', 'sticker'})
        self.assertEqual(self.download.await_count, 2)

    async def test_fragment_boundaries(self):
        f = self.scene.bot.fragments
        text = self.scene.make(1, '大白龙，终于赢了')
        f.observe(text, 'named')
        self.assertFalse(f.observe(self.scene.make(2, sticker=123, user=2), 'ambient'))
        elsewhere = self.scene.make(3, sticker=123)
        elsewhere.channel = NS(id=21)
        self.assertFalse(f.observe(elsewhere, 'ambient'))
        captioned = self.scene.make(4, '换个话题', sticker=123)
        self.assertFalse(f.observe(captioned, 'followup'))
        for boundary in ('other', 'closed', 'reference', 'expiry'):
            f.observe(text, 'named')
            if boundary == 'expiry': self.seconds += 31
            elif boundary == 'reference':
                f.observe(self.scene.make(5, '看你的', reference=self.scene.ref(text)), 'named')
            else: f.observe(self.scene.make(6, '转给别人'), boundary)
            self.assertFalse(f.observe(self.scene.make(7, sticker=123), 'followup'))

    async def test_new_sticker_during_old_visual_read_does_not_set_failure_backoff(self):
        started, release = asyncio.Event(), asyncio.Event()
        calls = 0
        async def delayed_download(*args):
            nonlocal calls
            calls += 1
            if calls == 1:
                started.set()
                await release.wait()
            return picture()
        self.download.side_effect = delayed_download
        await self.scene.bot.on_message(self.scene.make(1, '大白龙，看图', image=True))
        await asyncio.wait_for(started.wait(), 2)
        await self.scene.bot.on_message(self.scene.make(2, sticker=123))
        release.set()
        await asyncio.wait_for(asyncio.gather(*self.scene.bot.pending.values()), 5)
        self.assertEqual(self.scene.bot.backoff_until, 0)
        self.assertEqual(len(self.scene.replies), 1)
        self.assertTrue(self.scene.provider.answers[-1]['visual_context']['images'])


class StickerDownloadTests(unittest.IsolatedAsyncioTestCase):
    async def fetch(self, *, status=200, length=None, chunks=(b'pixels',)):
        async def stream(size):
            for chunk in chunks:
                yield chunk
        response = NS(status=status, content_length=length, content=NS(iter_chunked=stream))
        response_cm = MagicMock()
        response_cm.__aenter__ = AsyncMock(return_value=response)
        session = NS(get=MagicMock(return_value=response_cm))
        session_cm = MagicMock()
        session_cm.__aenter__ = AsyncMock(return_value=session)
        with patch('vision.aiohttp.ClientSession', return_value=session_cm):
            result = await download(StickerImage(123, 1), 20)
        session.get.assert_called_once_with('https://cdn.discordapp.com/stickers/123.png', allow_redirects=False)
        return result

    async def test_no_attachment_size_or_length_header_still_reads_bounded_stream(self):
        self.assertEqual(await self.fetch(chunks=(b'one', b'two')), b'onetwo')

    async def test_redirect_and_large_header_or_chunked_body_are_rejected(self):
        with patch('vision.MAX_BYTES', 10):
            for kwargs in ({'status': 302}, {'length': 11}, {'chunks': (b'123456', b'78901')}):
                with self.assertRaises(ValueError):
                    await self.fetch(**kwargs)


class StickerIdentityTests(unittest.TestCase):
    def test_static_cdn_url_is_constructed_from_id_only(self):
        self.assertEqual(sticker_url(StickerImage(123, 1)), 'https://cdn.discordapp.com/stickers/123.png')
        self.assertEqual(sticker_url(StickerImage(123, 2)), 'https://cdn.discordapp.com/stickers/123.png')
        self.assertEqual(sticker_url(StickerImage(123, 3)), 'https://cdn.discordapp.com/stickers/123.json')
        self.assertEqual(sticker_url(StickerImage(123, 4)), 'https://media.discordapp.net/stickers/123.gif')
        for asset in (StickerImage(123, 5), StickerImage(-1, 1), StickerImage(2**64, 1)):
            with self.assertRaises(ValueError): sticker_url(asset)

    def test_self_target_resolves_to_current_human_not_bot(self):
        current = dict(message_id='1', user_id='42', text='大白龙，连输五局了')
        result = validate(meaning(), [], current, {})
        self.assertEqual(result['resolved_target_user_id'], '42')
        self.assertEqual(result['current_speaker_id'], '42')
