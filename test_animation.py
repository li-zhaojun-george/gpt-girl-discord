import asyncio
import copy
import io
import json
import tempfile
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from PIL import Image, ImageDraw
import animation_frames as frames
from test_vision import Scene, picture


def animated(fmt='GIF', count=12, durations=None, poster=False):
    images = []
    for index in range(count):
        image = Image.new('RGBA', (160, 100), 'white')
        draw = ImageDraw.Draw(image)
        draw.rectangle((index*8, 25, index*8+20, 45), fill='red' if index==7 else 'blue')
        images.append(image)
    output = io.BytesIO()
    options = dict(save_all=True, append_images=images[1:], duration=durations or [100]*count, loop=0)
    if poster:
        cover = Image.new('RGBA', images[0].size, 'yellow')
        options.update(default_image=True, append_images=images)
        cover.save(output, format='PNG', **options)
    else:
        images[0].save(output, format=fmt, **options)
    return output.getvalue()


def vector_animation():
    return {'v':'5.7.4','w':200,'h':100,'fr':10,'ip':0,'op':30,'assets':[],
        'layers':[{'ty':4,'ind':1,'ip':0,'op':30,'st':0,'sr':1,
            'ks':{'o':{'a':0,'k':100},'r':{'a':0,'k':0},'a':{'a':0,'k':[0,0,0]},
                  's':{'a':0,'k':[100,100,100]},
                  'p':{'a':1,'k':[{'t':0,'s':[0,0,0],'e':[140,0,0],
                                    'i':{'x':1,'y':1},'o':{'x':0,'y':0}},
                                   {'t':29,'s':[140,0,0]}]}},
            'shapes':[{'ty':'el','p':{'a':0,'k':[30,50]},'s':{'a':0,'k':[32,32]},'d':1},
                      {'ty':'fl','c':{'a':0,'k':[1,0,0,1]},'o':{'a':0,'k':100},'r':1}]}]}


class FrameTests(unittest.TestCase):
    def test_static_image_remains_single_undecorated_frame(self):
        data, meta = frames.render(picture())
        self.assertFalse(meta['animated'])
        with Image.open(io.BytesIO(data)) as im:
            self.assertEqual(im.size, (100,60))
            self.assertEqual(im.getpixel((50,30)), (0,0,255))

    def test_gif_apng_webp_have_ordered_timed_multiple_frames(self):
        for fmt in ('GIF','PNG','WEBP'):
            with self.subTest(fmt=fmt):
                data, meta = frames.render(animated(fmt))
                samples = meta['sampled_frames']
                self.assertTrue(meta['animated'])
                self.assertEqual(meta['frame_count'], 12)
                self.assertEqual(len(samples), 6)
                self.assertEqual([s['index'] for s in samples], sorted(s['index'] for s in samples))
                self.assertEqual(samples[0]['index'], 0)
                self.assertEqual(samples[-1]['index'], 11)
                self.assertEqual(meta['duration_ms'],1200)
                with Image.open(io.BytesIO(data)) as im:
                    self.assertEqual(im.size,(1536,1080))

    def test_sampling_preserves_brief_strong_change_between_time_samples(self):
        durations = [100]*20
        changes = [0]*20
        changes[7] = 255
        chosen = frames.select_frames(durations, changes)
        self.assertIn(7,chosen)
        self.assertEqual(chosen, [0,5,7,10,15,19])

    def test_unequal_duration_uses_time_not_even_frame_indices(self):
        chosen = frames.select_frames([100]*9+[2000], [0]*10)
        self.assertIn(7,chosen)  # 25% of 2900ms, not 25% of ten frames.
        self.assertIn(9,chosen)

    def test_apng_poster_is_excluded_from_animation(self):
        data, meta = frames.render(animated('PNG', count=3, poster=True))
        self.assertEqual(meta['frame_count'],3)
        self.assertEqual(meta['duration_ms'],300)
        with Image.open(io.BytesIO(data)) as im:
            self.assertNotEqual(im.getpixel((256,280)), (255,255,0))

    def test_gif_disposal_does_not_leave_previous_square_trails(self):
        images=[]
        for x in (5,40,75):
            im=Image.new('RGBA',(100,50),(0,0,0,0))
            ImageDraw.Draw(im).rectangle((x,10,x+10,20),fill='red')
            images.append(im)
        stream=io.BytesIO()
        images[0].save(stream,format='GIF',save_all=True,append_images=images[1:],duration=100,disposal=2)
        data,meta=frames.render(stream.getvalue())
        with Image.open(io.BytesIO(data)) as im:
            # Frames are centered without upscaling inside 512px cells.
            self.assertEqual(im.getpixel((512+206+8,28+231+15)),(255,255,255))
            self.assertEqual(im.getpixel((512+206+45,28+231+15)),(255,0,0))

    def test_duration_frame_and_decode_work_limits_reject_whole_animation(self):
        with self.assertRaises(ValueError): frames.render(animated(durations=[3000]*12))
        with patch.object(frames,'MAX_FRAMES',5):
            with self.assertRaises(ValueError): frames.render(animated())
        with patch.object(frames,'MAX_TOTAL_PIXELS',100):
            with self.assertRaises(ValueError): frames.render(animated())
        with self.assertRaises((ValueError,OSError)): frames.render(b'not a gif')

    def test_lottie_is_actually_rendered_and_moves(self):
        data,meta=frames.render(json.dumps(vector_animation()).encode(),True)
        self.assertEqual(meta['format'],'LOTTIE')
        self.assertGreater(meta['frame_count'],20)
        with Image.open(io.BytesIO(data)) as im:
            # Native sticker size is 200x100, centered in each cell.
            self.assertEqual(im.getpixel((156+30,28+206+50)),(255,0,0))
            self.assertEqual(im.getpixel((2*512+156+170,540+28+206+50)),(255,0,0))

    def test_lottie_external_resources_expressions_and_cycles_are_rejected(self):
        docs=[]
        for entry in ({'p':'secret.png','u':'file:///tmp/'}, {'ty':5}, {'x':'alert(1)'}, {'ty':'rp'}):
            doc=vector_animation();doc['layers'].append(entry);docs.append(doc)
        doc=vector_animation();doc['assets']=[{'id':'a','layers':[{'refId':'a'}]}]
        doc['layers'].append({'refId':'a'});docs.append(doc)
        doc=vector_animation();doc['w']=100000;docs.append(doc)
        doc=vector_animation();doc['op']=100000;docs.append(doc)
        for doc in docs:
            with self.assertRaises(ValueError):frames.lottie_document(json.dumps(doc).encode())


class RendererProcessTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_worker_raster_and_lottie(self):
        for data, is_lottie in ((animated(),False),(json.dumps(vector_animation()).encode(),True)):
            png,meta=await frames.prepare_media(data,is_lottie)
            self.assertTrue(png.startswith(b'\x89PNG'))
            self.assertTrue(meta['animated'])

    async def test_timeout_and_cancellation_kill_worker(self):
        for cancel in (False,True):
            entered=asyncio.Event()
            async def wait(*args):
                entered.set()
                await asyncio.Event().wait()
            process=NS(returncode=None,communicate=wait,wait=AsyncMock())
            process.kill=lambda: setattr(process,'returncode',-9)
            with patch('animation_frames.asyncio.create_subprocess_exec',AsyncMock(return_value=process)), \
                    patch.object(frames,'RENDER_TIMEOUT',.05):
                task=asyncio.create_task(frames.prepare_media(b'data'))
                await entered.wait()
                if cancel:task.cancel()
                with self.assertRaises(asyncio.CancelledError if cancel else asyncio.TimeoutError): await task
                self.assertEqual(process.returncode,-9)
                process.wait.assert_awaited_once()


class AnimationFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder=tempfile.TemporaryDirectory()
        self.scene=Scene(self.folder.name)

    async def asyncTearDown(self):
        await self.scene.bot.close()
        self.folder.cleanup()

    async def test_all_native_animation_sticker_formats_reach_codex_as_frames(self):
        for fmt,data in ((2,animated('PNG')),(3,json.dumps(vector_animation()).encode()),(4,animated('GIF'))):
            with patch('vision.download',AsyncMock(return_value=data)):
                await self.scene.send(self.scene.make(fmt,'<@99> 看这个动画',sticker=123,sticker_format=fmt))
            payload=self.scene.provider.answers[-1]
            self.assertTrue(payload['capabilities']['animation_frames'])
            self.assertFalse(payload['capabilities']['watch_video'])
            self.assertEqual(len(payload['visual_context']['images'][0]['animation']['sampled_frames']),6)

    async def test_caption_then_animation_followup_and_source_change(self):
        await self.scene.send(self.scene.make(1,'大白龙，刚才还高兴，结果又输了'))
        with patch('vision.download',AsyncMock(return_value=animated())) as download:
            await self.scene.send(self.scene.make(2,sticker=123,sticker_format=4))
            self.assertEqual(self.scene.provider.answers[-1]['linked_messages'][0]['message_id'],'1')
            await self.scene.send(self.scene.make(3,'最后怎么了？'))
            self.assertEqual(download.await_count,2)
            self.assertEqual(self.scene.provider.answers[-1]['visual_context']['images'][0]['message_id'],'2')
            download.return_value=animated('PNG')
            await self.scene.send(self.scene.make(4,'再看一次？'))
            self.assertFalse(self.scene.provider.answers[-1]['capabilities']['images'])

    async def test_one_bad_animation_does_not_hide_other_valid_asset(self):
        msg=self.scene.make(1,'<@99> 看这两个',image=True,sticker=123,sticker_format=3)
        with patch('vision.download',AsyncMock(side_effect=[picture(),b'invalid lottie'])):
            await self.scene.send(msg)
        visual=self.scene.provider.answers[-1]['visual_context']
        self.assertEqual(len(visual['images']),1)
        self.assertEqual(len(visual['errors']),1)
        self.assertFalse(self.scene.provider.answers[-1]['capabilities']['animation_frames'])


if __name__=='__main__':unittest.main()
