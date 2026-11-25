"""一键上传整包：真实rsync仅复制到临时本地目录，不向服务器上传。"""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'server'),str(ROOT/'dev/fake_ros'),str(ROOT.parent/'go2w/src/navigation/go2w_taught_route')]
import go2w_web as W
from recording import RecordingAPI


class BagUploadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.bag=self.root/'bags/test-bag';self.bag.mkdir(parents=True)
        (self.bag/'metadata.yaml').write_text('duration:\n  nanoseconds: 30000000000\n')
        (self.bag/'test-bag_0.mcap').write_bytes(b'original-robot-data')
        (self.bag/'capture.json').write_text(json.dumps({'request_id':'request-one','name':'test-bag',
            'before_s':20,'after_s':10,'voice':{'state':'pending','requested_s':20}}))
        self.remote=self.root/'remote';self.remote.mkdir()
        self.destination=str(self.remote)
        self.audio=b'\x1aE\xdf\xa3'+b'voice-data'*10
        self.server=W.Server.__new__(W.Server)
        self.server.node=NS(event=lambda *args:None)
        self.server.uploads={'test-bag':{'state':'run','phase':'waiting','pct':0,'msg':'等待语音'}}
        self.server.uploads_file=str(self.root/'bag_uploads.json')
        self.server.recording_api=RecordingAPI(self.root/'bags',ROOT/'static',
            uploads=self.server.uploads,on_change=self.server.bag_content_changed)
        app=web.Application();self.server.recording_api.routes(app)
        self.client=TestClient(TestServer(app));await self.client.start_server()
        self.calls=[];real_spawn=asyncio.create_subprocess_exec
        async def local_rsync(*args,**kwargs):
            self.calls.append(args)
            self.assertEqual(args[0],'rsync');self.assertEqual(args[-1],W.UPLOAD_DEST)
            self.assertIn('--compress',args)
            self.assertIn('--compress-choice=zstd',args)
            self.assertIn('--compress-level=1',args)
            command=list(args[:-1])
            if self.destination.startswith('rsync://'):
                shell=command.index('-e');del command[shell:shell+2]
            return await real_spawn(*command,self.destination,**kwargs)
        self.redirect=patch.object(W.asyncio,'create_subprocess_exec',side_effect=local_rsync)
        self.redirect.start();self.data=patch.object(W,'DATA',str(self.root));self.data.start()

    async def asyncTearDown(self):
        self.redirect.stop();self.data.stop();await self.client.close();self.tmp.cleanup()

    async def upload_voice(self):
        return await self.client.post('/api/recordings/test-bag/audio?duration=20&request_id=request-one',data=self.audio,
                                      headers={'Content-Type':'audio/webm'})

    async def test_one_click_waits_for_voice_then_transfers_data_voice_and_matching_manifest(self):
        job=asyncio.create_task(self.server.bag_upload_run('test-bag',1,'/unused-test-key','local-test'))
        await asyncio.sleep(0)
        self.assertEqual(self.calls,[],'语音尚未完成时不开始传数据')
        self.assertEqual((await self.upload_voice()).status,201)
        await asyncio.wait_for(job,5)
        self.assertEqual(self.server.uploads['test-bag']['state'],'done',self.server.uploads['test-bag'])
        dest=self.remote/'test-bag'
        self.assertEqual((dest/'test-bag_0.mcap').read_bytes(),b'original-robot-data')
        self.assertEqual((dest/'voice-note.audio').read_bytes(),self.audio)
        metadata=json.loads((dest/'voice-note.json').read_text())
        self.assertEqual((metadata['bag_name'],metadata['request_id']),('test-bag','request-one'))
        link=json.loads((dest/'recording-link.json').read_text())
        self.assertEqual(link['bag']['name'],'test-bag')
        self.assertEqual(link['bag']['data_files'],['test-bag_0.mcap'])
        self.assertEqual(link['request_id'],'request-one')
        self.assertEqual(link['audio']['file'],'voice-note.audio')
        self.assertEqual(link['audio']['sha256'],hashlib.sha256(self.audio).hexdigest())
        self.assertTrue((dest/'capture.json').is_file())
        self.assertEqual(self.server.uploads['test-bag']['state'],'done')

    async def test_zstd_wire_transfer_preserves_data_voice_and_manifest(self):
        # Local rsync copies skip wire compression; use a loopback-only receiver.
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        config=self.root/'rsyncd.conf'
        identity='uid = 0\ngid = 0\n' if os.geteuid()==0 else ''
        config.write_text('use chroot = no\n'+identity+
            f'pid file = {self.root}/rsync.pid\nlock file = {self.root}/rsync.lock\n'
            f'log file = {self.root}/rsync.log\n[bags]\npath = {self.remote}\nread only = no\n')
        daemon=subprocess.Popen(['rsync','--daemon','--no-detach','--address=127.0.0.1',
            f'--port={port}',f'--config={config}'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        try:
            for _ in range(100):
                self.assertIsNone(daemon.poll(),'rsync receiver exited')
                try:
                    with socket.create_connection(('127.0.0.1',port),timeout=.1):break
                except OSError:await asyncio.sleep(.02)
            else:self.fail('rsync receiver did not start')
            self.destination=f'rsync://127.0.0.1:{port}/bags'
            payload=(b'point-cloud-sample\x00'*4096)*64
            (self.bag/'test-bag_0.mcap').write_bytes(payload)
            self.assertEqual((await self.upload_voice()).status,201)
            await asyncio.wait_for(self.server.bag_upload_run('test-bag',len(payload)/1024**2,
                '/unused-test-key','loopback-zstd'),10)
            self.assertEqual(self.server.uploads['test-bag']['state'],'done',self.server.uploads['test-bag'])
            self.assertEqual({p.name:p.read_bytes() for p in self.bag.iterdir() if p.is_file()},
                {p.name:p.read_bytes() for p in (self.remote/'test-bag').iterdir() if p.is_file()})
        finally:
            daemon.terminate();daemon.wait(timeout=5)

    async def test_pending_voice_timeout_never_marks_data_only_upload_done(self):
        with patch.object(W,'BAG_VOICE_WAIT',0):
            await self.server.bag_upload_run('test-bag',1,'/unused-test-key','local-test')
        self.assertEqual(self.calls,[])
        self.assertFalse((self.remote/'test-bag').exists())
        self.assertEqual(self.server.uploads['test-bag']['state'],'fail')

    async def test_unavailable_microphone_is_explicit_in_uploaded_manifest(self):
        response=await self.client.post('/api/recordings/test-bag/audio-status',json={'state':'unavailable','reason':'HTTP禁止麦克风','request_id':'request-one'})
        self.assertEqual(response.status,200)
        await self.server.bag_upload_run('test-bag',1,'/unused-test-key','local-test')
        link=json.loads((self.remote/'test-bag/recording-link.json').read_text())
        self.assertEqual(link['audio']['state'],'unavailable')
        self.assertEqual(link['audio']['reason'],'HTTP禁止麦克风')
        response=await self.client.post('/api/recordings/test-bag/audio-status',json={'state':'unavailable','reason':'HTTP禁止麦克风','request_id':'request-one'})
        self.assertEqual(response.status,200)
        self.assertEqual(self.server.uploads['test-bag']['state'],'done','相同状态的回执重试不使已上传记录失效')

    async def test_sending_freezes_voice_and_later_addition_invalidates_old_upload_receipt(self):
        self.server.uploads['test-bag']['phase']='sending'
        self.assertEqual((await self.upload_voice()).status,409)
        self.assertFalse((self.bag/'voice-note.audio').exists())
        self.server.uploads['test-bag']={'state':'done','pct':100,'msg':'earlier'}
        self.assertEqual((await self.upload_voice()).status,201)
        self.assertEqual(self.server.uploads['test-bag']['state'],'stale')
        self.assertEqual(json.loads(Path(self.server.uploads_file).read_text()),{})

    async def test_legacy_bag_without_voice_uploads_with_explicit_no_voice_mapping(self):
        (self.bag/'capture.json').unlink()
        await self.server.bag_upload_run('test-bag',1,'/unused-test-key','local-test')
        link=json.loads((self.remote/'test-bag/recording-link.json').read_text())
        self.assertEqual(link['audio']['state'],'not_recorded')
        self.assertEqual(self.server.uploads['test-bag']['state'],'done')

    async def test_mismatched_voice_never_uploads_as_a_valid_pair(self):
        self.assertEqual((await self.upload_voice()).status,201)
        metadata=json.loads((self.bag/'voice-note.json').read_text())
        metadata['bag_name']='another-bag'
        (self.bag/'voice-note.json').write_text(json.dumps(metadata))
        await self.server.bag_upload_run('test-bag',1,'/unused-test-key','local-test')
        self.assertEqual(self.calls,[])
        self.assertEqual(self.server.uploads['test-bag']['state'],'fail')

    async def test_published_directory_uses_pending_preregistration_until_capture_is_linked(self):
        registration=self.root/'bags/.captures/by-name/test-bag.json'
        registration.parent.mkdir(parents=True)
        (self.bag/'capture.json').replace(registration)
        self.assertIsNone(self.server.recording_api.bundle('test-bag'))
        self.assertEqual((await self.upload_voice()).status,201)
        link=self.server.recording_api.bundle('test-bag')
        self.assertEqual(link['request_id'],'request-one')
        self.assertTrue((self.bag/'capture.json').is_file())

    async def test_incomplete_data_slices_cannot_be_marked_uploaded(self):
        (self.bag/'capture.json').unlink()
        (self.bag/'metadata.yaml').write_text('rosbag2_bagfile_information:\n  relative_file_paths:\n    - test-bag_0.mcap\n    - missing_1.mcap\n')
        await self.server.bag_upload_run('test-bag',1,'/unused-test-key','local-test')
        self.assertEqual(self.calls,[])
        self.assertEqual(self.server.uploads['test-bag']['state'],'fail')

    async def test_existing_data_and_voice_make_legacy_receipt_stale(self):
        self.assertEqual((await self.upload_voice()).status,201)
        Path(self.server.uploads_file).write_text(json.dumps({'test-bag':'11-24 10:00'}))
        self.server.restore_upload_receipts()
        self.assertEqual(self.server.uploads['test-bag']['state'],'stale')

    async def test_new_receipt_remains_done_after_restart_with_same_bundle(self):
        self.assertEqual((await self.upload_voice()).status,201)
        await self.server.bag_upload_run('test-bag',1,'/unused-test-key','local-test')
        self.server.uploads.clear()
        self.server.restore_upload_receipts()
        self.assertEqual(self.server.uploads['test-bag']['state'],'done')

    async def test_wrong_request_cannot_attach_voice_or_mark_missing_audio(self):
        response=await self.client.post('/api/recordings/test-bag/audio?duration=5&request_id=other-request',
            data=self.audio,headers={'Content-Type':'audio/webm'})
        self.assertEqual(response.status,409)
        response=await self.client.post('/api/recordings/test-bag/audio-status',
            json={'state':'unavailable','reason':'old-page','request_id':'other-request'})
        self.assertEqual(response.status,409)
        capture=json.loads((self.bag/'capture.json').read_text())
        self.assertEqual(capture['voice']['state'],'pending')


if __name__=='__main__':unittest.main()
