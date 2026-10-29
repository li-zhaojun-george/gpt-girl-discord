import asyncio
import copy
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

from bot import GirlBot
from interests import InterestEngine, InterestStore, candidate, load_profile
from media import Media, MediaFetcher, PublicResolver, canonical_url, image_extension, public_url
from settings import Settings
from state import State

PROFILE = Path(__file__).with_name('interests.json')

def decision(kind='video'):
    return {'action':'share','kind':kind,'url':'https://www.youtube.com/watch?v=example',
            'title':'有意思的小实验','comment':'这个点子挺巧的，你们会怎么做？','reason':'有趣的科学演示'}

class InterestTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.state = State(Path(self.folder.name)/'state.sqlite3')
        self.provider = NS(web_search='live',structured=AsyncMock(return_value=decision()),
                           complete=AsyncMock(return_value=('reply','接上了')))
        self.bot = GirlBot(self.provider,Settings({20},observed={20},mode='social',debounce_seconds=0),'',self.state)
        self.bot.settings.interest_channels = {20}
        self.bot._connection.user = NS(id=99)
        self.bot.now = lambda:datetime(2026,10,28,12,tzinfo=ZoneInfo('America/Toronto'))
        self.profile = load_profile(PROFILE)
        self.profile['interests'] = [self.profile['interests'][0]]
        self.fetcher = NS(fetch=AsyncMock(return_value=Media('https://www.youtube.com/watch?v=example','页面真实标题','来源简介')))
        self.engine = InterestEngine(self.bot,self.profile,self.fetcher)
        self.bot.interests = self.engine
        self.permissions = NS(view_channel=True,send_messages=True,attach_files=True)
        self.channel = NS(id=20,guild=NS(id=10,me=NS()),send=AsyncMock(return_value=NS(id=100)),
                          permissions_for=lambda _:self.permissions)
        self.bot.get_channel = lambda _: self.channel
        self.engine.store.ensure('10:20',self.bot.now().timestamp())
        self.engine.store.defer('10:20',0)

    async def asyncTearDown(self):
        await self.bot.close()
        self.folder.cleanup()

    def message(self,text='为什么呢',manager=False):
        return NS(id=300,content=text,author=NS(id=1,bot=False,display_name='测试用户',guild_permissions=NS(manage_guild=manager)),
                  webhook_id=None,guild=self.channel.guild,channel=self.channel,reference=None,reply=AsyncMock(return_value=NS(id=101)))

    async def test_send_persists_and_restart_does_not_repeat_without_response(self):
        await self.engine.tick()
        self.channel.send.assert_awaited_once()
        self.assertEqual(self.engine.store.recent('10:20')[0]['status'],'sent')
        args = self.channel.send.call_args.kwargs
        self.assertTrue(args['silent'])
        self.assertFalse(args['allowed_mentions'].everyone)
        self.assertFalse(args['allowed_mentions'].users)
        self.assertFalse(args['allowed_mentions'].roles)
        self.engine = InterestEngine(self.bot,self.profile,self.fetcher)
        self.bot.interests = self.engine
        self.engine.store.defer('10:20',0)
        await self.engine.tick()
        self.channel.send.assert_awaited_once()
        self.assertTrue(self.engine.store.seen('10:20',self.fetcher.fetch.return_value.url))

    async def test_reply_to_share_becomes_followup_with_actual_source_context(self):
        await self.engine.tick()
        self.provider.complete.reset_mock()
        msg = self.message()
        await self.bot.on_message(msg)
        await asyncio.gather(*list(self.bot.pending.values()))
        self.provider.complete.assert_awaited_once()
        payload = self.provider.complete.call_args.args[1]
        self.assertEqual(payload['route'],'followup')
        self.assertEqual(payload['discoveries'][0]['title'],'页面真实标题')
        self.assertFalse(self.engine.store.status('10:20')['awaiting'])

    async def test_uncertain_send_is_persisted_without_retry(self):
        self.channel.send.side_effect = RuntimeError('transport failure')
        await self.engine.tick()
        self.assertEqual(self.engine.store.recent('10:20')[0]['status'],'uncertain')
        self.bot.backoff_until = 0
        self.engine.store.defer('10:20',0)
        await self.engine.tick()
        self.channel.send.assert_awaited_once()
        self.assertFalse(self.bot.model_lock.locked())

    async def test_quiet_busy_recent_chat_paused_or_permissions_skip_search(self):
        self.bot.settings.quiet_start,self.bot.settings.quiet_end = 0,23
        await self.engine.tick()
        self.bot.settings.quiet_start,self.bot.settings.quiet_end = 23,8
        await self.bot.model_lock.acquire()
        await self.engine.tick()
        self.bot.model_lock.release()
        self.permissions.send_messages = False
        await self.engine.tick()
        self.permissions.send_messages = True
        self.engine.store.pause('10:20',True,self.bot.now().timestamp())
        await self.engine.tick()
        self.engine.store.pause('10:20',False,self.bot.now().timestamp())
        self.engine.store.defer('10:20',0)
        self.engine.human('10:20')
        await self.engine.tick()
        self.provider.structured.assert_not_awaited()

    async def test_human_activity_interrupts_search_and_releases_model_for_conversation(self):
        entered = asyncio.Event()
        async def search(*args,**kwargs):
            entered.set()
            await asyncio.Event().wait()
        self.provider.structured.side_effect = search
        task = asyncio.create_task(self.engine.tick())
        await entered.wait()
        msg = self.message('<@99> 你好')
        await self.bot.on_message(msg)
        await task
        self.channel.send.assert_not_awaited()
        msg.reply.assert_awaited_once()
        self.assertFalse(self.bot.model_lock.locked())
        self.assertGreater(self.engine.store.status('10:20')['next_due'],self.bot.now().timestamp())

    async def test_revision_changed_during_fetch_blocks_stale_share(self):
        async def fetch(*args):
            self.engine.store.human('10:20',self.bot.now().timestamp())
            return Media('https://www.youtube.com/watch?v=example','title','desc')
        self.fetcher.fetch.side_effect = fetch
        await self.engine.tick()
        self.channel.send.assert_not_awaited()
        self.assertEqual(self.engine.store.recent('10:20'),[])

    async def test_silence_or_duplicate_never_downloads_or_posts(self):
        self.provider.structured.return_value = {**decision(),'action':'silence'}
        await self.engine.tick()
        self.fetcher.fetch.assert_not_awaited()
        self.channel.send.assert_not_awaited()
        self.assertGreater(self.engine.store.status('10:20')['next_due'],self.bot.now().timestamp())

    async def test_pause_requires_manager_and_survives_restart(self):
        outsider = self.message('<@99> 暂停分享')
        await self.bot.on_message(outsider)
        self.assertFalse(self.engine.store.status('10:20')['paused'])
        manager = self.message('<@99> 暂停分享',True)
        manager.id = 301
        await self.bot.on_message(manager)
        self.assertTrue(InterestStore(self.state).status('10:20')['paused'])
        self.provider.complete.assert_not_awaited()

    async def test_meme_attaches_bytes_with_source_credit(self):
        self.profile['interests'][0]['formats'] = ['meme']
        self.provider.structured.return_value = {**decision('meme'),'url':'https://tenor.com/view/cat-123'}
        self.fetcher.fetch.return_value = Media('https://tenor.com/view/cat-123','Cat','fun',b'GIF89aDATA','share.gif')
        captured = {}
        async def send(*args,**kwargs):
            captured['bytes'] = kwargs['file'].fp.getvalue()
            return NS(id=100)
        self.channel.send.side_effect = send
        await self.engine.tick()
        self.assertEqual(captured['bytes'],b'GIF89aDATA')
        self.assertIn('https://tenor.com/view/cat-123',self.channel.send.call_args.args[0])

    async def test_attachment_permission_checked_before_claim(self):
        self.fetcher.fetch.return_value.image = b'GIF89aDATA'
        self.permissions.attach_files = False
        await self.engine.tick()
        self.channel.send.assert_not_awaited()
        self.assertEqual(self.engine.store.recent('10:20'),[])

    async def test_unreadable_source_gets_one_alternate_attempt(self):
        media = self.fetcher.fetch.return_value
        self.fetcher.fetch.side_effect = [ValueError('page unavailable'), media]
        self.provider.structured.side_effect = [decision(),{**decision(),'url':'https://www.bilibili.com/video/BV123'}]
        await self.engine.tick()
        self.assertEqual(self.provider.structured.await_count,2)
        payload = self.provider.structured.call_args.args[1]
        self.assertNotIn('youtube.com',payload['allowed_sources'])
        self.assertEqual(payload['previous_unavailable'],[decision()['url']])
        self.channel.send.assert_awaited_once()
        with self.state.db() as db:
            self.assertEqual(db.execute('SELECT total FROM quota').fetchone()[0],3)

    async def test_same_image_at_different_url_is_not_sent_again(self):
        self.fetcher.fetch.return_value.image = b'GIF89aDATA'
        self.fetcher.fetch.return_value.filename = 'share.gif'
        await self.engine.tick()
        self.engine.store.human('10:20',self.bot.now().timestamp()-1000)
        self.bot.last_social.clear()
        self.engine.store.defer('10:20',0)
        self.provider.structured.return_value['url'] = 'https://www.youtube.com/watch?v=second'
        self.fetcher.fetch.return_value.url = 'https://www.youtube.com/watch?v=second'
        await self.engine.tick()
        self.channel.send.assert_awaited_once()

    async def test_caption_uses_verified_metadata_and_rechecks_revision(self):
        async def caption(*args):
            self.assertEqual(args[1]['source_title'],'页面真实标题')
            self.assertNotIn('speaker',args[1])
            self.engine.store.human('10:20',self.bot.now().timestamp())
            return 'reply','新消息到来了'
        self.provider.complete.side_effect = caption
        await self.engine.tick()
        self.channel.send.assert_not_awaited()
        self.assertEqual(self.engine.store.recent('10:20'),[])

    async def test_caption_cannot_add_unverified_links_or_mentions(self):
        self.provider.complete.return_value = ('reply','@everyone https://example.invalid')
        await self.engine.tick()
        self.channel.send.assert_not_awaited()

class MediaTests(unittest.IsolatedAsyncioTestCase):
    def test_urls_deduplicate_tracking_and_video_aliases(self):
        self.assertEqual(canonical_url('https://youtu.be/abc?utm_source=x'),canonical_url('https://www.youtube.com/watch?v=abc&feature=share'))
        self.assertEqual(canonical_url('https://www.youtube.com/shorts/abc'),canonical_url('https://www.youtube.com/watch?v=abc'))
        self.assertEqual(canonical_url('https://www.bilibili.com/video/BV123/?spm_id_from=1'),'https://www.bilibili.com/video/BV123')

    def test_unapproved_destinations_and_mentions_fail_closed(self):
        for url in ('file:///x','https://127.0.0.1','https://tenor.com.evil.test/a','https://user:pass@tenor.com/a','https://tenor.com:8080/a'):
            with self.assertRaises(ValueError):
                public_url(url,['tenor.com'])
        profile = load_profile(PROFILE)
        with self.assertRaises(ValueError):
            candidate({**decision(),'comment':'@everyone 看这里'},profile,profile['interests'][0])
        with self.assertRaises(ValueError):
            candidate({**decision(),'url':'https://www.youtube.com/results?q=cat'},profile,profile['interests'][0])

    async def test_resolver_rejects_private_and_mixed_address_sets(self):
        resolver = PublicResolver()
        resolver.base.resolve = AsyncMock(return_value=[{'host':'1.1.1.1'},{'host':'127.0.0.1'}])
        try:
            with self.assertRaises(ValueError):
                await resolver.resolve('tenor.com')
            resolver.base.resolve.return_value = [{'host':'::1'}]
            with self.assertRaises(ValueError):
                await resolver.resolve('tenor.com')
        finally:
            await resolver.close()

    async def test_source_metadata_drives_meme_download_and_signature(self):
        fetcher = MediaFetcher(['tenor.com'],['media.tenor.com'])
        fetcher._get = AsyncMock(side_effect=[('https://tenor.com/view/cat-1',b'<meta property="og:title" content="Cat"><meta property="og:image" content="https://media.tenor.com/a/cat.gif">'),('https://media.tenor.com/a/cat.gif',b'GIF89a123')])
        media = await fetcher.fetch('https://tenor.com/view/cat-1','meme')
        self.assertEqual(media.filename,'gpt-girl-share.gif')
        self.assertEqual(media.title,'Cat')
        with self.assertRaises(ValueError):
            image_extension(b'<script>not an image</script>')

    def test_interest_config_scope_is_validated(self):
        for kwargs in ({'mode':'mention'},{'backend':'openai'},{'observed':set()}):
            config = dict(allowed={20},observed={20},mode='social',interest_channels={20})
            config.update(kwargs)
            with self.assertRaises(ValueError):
                Settings(**config)
