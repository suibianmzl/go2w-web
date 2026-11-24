"""语音备注HTTP边界：真正落盘、回读，不依赖ROS。"""
from pathlib import Path
import io
import sys
import tempfile
import unittest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'server'))


class RecordingAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from recording import RecordingAPI
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.bag = self.root/'bags/go2w-test'
        self.bag.mkdir(parents=True)
        (self.bag/'metadata.yaml').write_text('duration:\n  nanoseconds: 30000000000\n')
        app = web.Application()
        RecordingAPI(self.root/'bags', self.root).routes(app)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.audio = b'\x1aE\xdf\xa3' + b'webm-test'*10

    async def asyncTearDown(self):
        await self.client.close()
        self.tmp.cleanup()

    async def upload(self, data=None, mime='audio/webm', duration='5', name='go2w-test', **kwargs):
        return await self.client.post(f'/api/recordings/{name}/audio?duration={duration}',
                                      data=io.BytesIO(self.audio if data is None else data),
                                      headers={'Content-Type': mime, **kwargs})

    async def test_voice_is_associated_with_saved_bag_and_survives_reload(self):
        response = await self.upload(mime='audio/webm;codecs=opus')
        self.assertEqual(response.status, 201, await response.text())
        result = await (await self.client.get('/api/recordings')).json()
        item = result['items'][0]
        self.assertEqual((item['name'], item['dur']), ('go2w-test', 30))
        self.assertEqual(item['audio']['duration_s'], 5)
        response = await self.client.get(item['audio']['url'])
        self.assertEqual(response.content_type, 'audio/webm')
        self.assertEqual(await response.read(), self.audio)
        self.assertEqual((await self.upload()).status, 200, '成功回执丢失后相同语音可安全重试')
        self.assertEqual((await self.upload(data=self.audio+b'different')).status, 409)

    async def test_voice_metadata_cannot_follow_existing_symlink(self):
        outside = self.root/'outside.json'
        outside.write_text('original')
        (self.bag/'voice-note.json').symlink_to(outside)
        response = await self.upload()
        self.assertEqual(response.status, 409)
        self.assertEqual(outside.read_text(), 'original')
        self.assertFalse((self.bag/'voice-note.audio').exists())

    async def test_request_id_finds_only_its_completed_bag(self):
        import json
        manifests = self.root/'bags/.captures'
        manifests.mkdir()
        (manifests/'request-one.json').write_text(json.dumps({'name':'go2w-test','request_id':'request-one'}))
        (self.bag/'capture.json').write_text(json.dumps({'name':'go2w-test','request_id':'request-one'}))
        response = await self.client.get('/api/recordings?request_id=request-one')
        self.assertEqual([x['name'] for x in (await response.json())['items']], ['go2w-test'])
        response = await self.client.get('/api/recordings?request_id=request-two')
        self.assertEqual((await response.json())['items'], [])
        (self.bag/'capture.json').write_text(json.dumps({'name':'go2w-test','request_id':'new-request'}))
        response = await self.client.get('/api/recordings?request_id=request-one')
        self.assertEqual((await response.json())['items'], [], '旧索引不能把同名新包当成原包')

    async def test_invalid_uploads_do_not_leave_partial_voice_files(self):
        for data, mime, duration, status in [
            (b'', 'audio/webm', '5', 400),
            (b'not audio', 'audio/webm', '5', 400),
            (self.audio, 'text/html', '5', 415),
            (self.audio, 'audio/webm', '21', 400),
            (self.audio, 'audio/webm', 'nan', 400),
            (self.audio, 'audio/webm', '0', 400),
            (self.audio + bytes(4*1024*1024), 'audio/webm', '5', 413),
        ]:
            response = await self.upload(data, mime, duration)
            self.assertEqual(response.status, status, await response.text())
            self.assertEqual(list(self.bag.iterdir()), [self.bag/'metadata.yaml'])

    async def test_only_completed_local_bags_can_receive_audio(self):
        self.assertEqual((await self.upload(name='missing')).status, 404)
        (self.bag/'metadata.yaml').unlink()
        self.assertEqual((await self.upload()).status, 409)
        outside = self.root/'outside'
        outside.mkdir()
        (outside/'metadata.yaml').touch()
        (self.root/'bags/escape').symlink_to(outside, target_is_directory=True)
        self.assertEqual((await self.upload(name='escape')).status, 404)
        self.assertFalse((outside/'voice-note.audio').exists())

    async def test_cross_origin_write_is_rejected(self):
        self.assertEqual((await self.upload(Origin='http://foreign.test')).status, 403)
        response=await self.client.post('/api/recordings/go2w-test/audio-status',
            json={'state':'unavailable','reason':'HTTP'},headers={'Origin':'http://foreign.test'})
        self.assertEqual(response.status,403)

    async def test_no_voice_status_requires_an_explicit_valid_reason(self):
        for body in [[],{'state':'ready','reason':'not an audio file'},{'state':'unavailable','reason':42}]:
            response=await self.client.post('/api/recordings/go2w-test/audio-status',json=body)
            self.assertEqual(response.status,400)


if __name__ == '__main__':
    unittest.main()
