"""录包的浏览器语音备注；与ROS录制独立，文件保存在原包目录。"""
import asyncio
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import time
from urllib.parse import quote, urlsplit

from aiohttp import web
import yaml
from procs import list_bags

MAX_AUDIO_BYTES = 4 * 1024 * 1024
MIME_EXT = {'audio/webm': 'webm', 'audio/mp4': 'm4a', 'audio/ogg': 'ogg'}


def audio_type(data):
    if data.startswith(b'\x1aE\xdf\xa3'):
        return 'audio/webm'
    if len(data) >= 12 and data[4:8] == b'ftyp':
        return 'audio/mp4'
    if data.startswith(b'OggS'):
        return 'audio/ogg'
    return None


class RecordingAPI:
    def __init__(self, bags_dir, static_dir, uploads=None, on_change=None):
        self.bags = Path(bags_dir).resolve()
        self.static = Path(static_dir)
        self.uploading = set()
        self.transfers = uploads if uploads is not None else {}
        self.on_change = on_change or (lambda _name: None)

    def routes(self, app):
        app.router.add_get('/record', self.page)
        app.router.add_get('/api/recordings', self.items)
        app.router.add_post('/api/recordings/{name}/audio', self.upload)
        app.router.add_post('/api/recordings/{name}/audio-status', self.audio_status)
        app.router.add_get('/api/recordings/{name}/audio', self.audio)

    async def page(self, _req):
        return web.FileResponse(self.static/'record.html')

    @staticmethod
    def write_json(path, data):
        tmp = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, prefix='.recording-', delete=False) as f:
                tmp = Path(f.name)
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        finally:
            if tmp:
                tmp.unlink(missing_ok=True)

    @staticmethod
    def capture(bag):
        path = bag/'capture.json'
        if not path.exists():
            path = bag.parent/'.captures/by-name'/f'{bag.name}.json'
            if not path.exists():
                return {}
        if path.is_symlink():
            raise web.HTTPConflict(text='录包匹配信息不可使用符号链接')
        try:
            data = json.loads(path.read_text())
            if not isinstance(data, dict):
                raise ValueError
            if data.get('name', bag.name) != bag.name:
                raise ValueError
            return data
        except (OSError, ValueError):
            raise web.HTTPConflict(text='录包匹配信息损坏，未上传')

    def check_request(self, bag, request_id):
        capture = self.capture(bag)
        if capture.get('request_id') != request_id:
            raise web.HTTPConflict(text='记录编号与当前录包不符，请重新核对录包')
        return capture

    def check_mutable(self, name):
        transfer = self.transfers.get(name, {})
        if transfer.get('state') == 'run' and transfer.get('phase') != 'waiting':
            raise web.HTTPConflict(text='整包正在上传，请等上传结束后再保存语音')

    async def audio_status(self, req):
        origin = req.headers.get('Origin')
        if origin and urlsplit(origin).netloc != req.host:
            raise web.HTTPForbidden(text='只接受当前网页更新录音状态')
        bag = self.bag(req.match_info['name'])
        self.check_mutable(bag.name)
        try:
            data = await req.json()
        except (ValueError, UnicodeError):
            raise web.HTTPBadRequest(text='录音状态必须是JSON')
        if not isinstance(data, dict) or data.get('state') != 'unavailable' or not isinstance(data.get('reason'), str):
            raise web.HTTPBadRequest(text='请明确说明本次没有录音的原因')
        capture = self.check_request(bag, data.get('request_id'))
        self.check_mutable(bag.name)
        if bag in self.uploading:
            raise web.HTTPConflict(text='语音正在保存，请稍等')
        if not self.voice(bag):
            voice = {**capture.get('voice', {}), 'state': 'unavailable', 'reason': data['reason'][:500]}
            if capture.get('voice') != voice:
                capture['voice'] = voice
                self.write_json(bag/'capture.json', capture)
                self.on_change(bag.name)
        return web.json_response({'ok': True})

    def bundle(self, name):
        bag = self.bag(name)
        capture = self.capture(bag)
        if bag in self.uploading:
            return None
        voice = self.voice(bag)
        if voice:
            metadata = json.loads((bag/'voice-note.json').read_text())
            if metadata.get('bag_name', name) != name or (metadata.get('request_id') and metadata['request_id'] != capture.get('request_id')):
                raise web.HTTPConflict(text='语音与录包的匹配信息不一致，未上传')
            audio = {'state': 'ready', 'file': 'voice-note.audio', 'metadata_file': 'voice-note.json',
                     'mime': voice['mime'], 'duration_s': voice['duration_s'],
                     'sha256': hashlib.sha256((bag/'voice-note.audio').read_bytes()).hexdigest()}
        else:
            expected = capture.get('voice', {})
            if expected.get('state') == 'pending':
                return None
            audio = {'state': expected.get('state', 'not_recorded'), 'reason': expected.get('reason', '本包没有录音')}
        try:
            metadata = yaml.safe_load((bag/'metadata.yaml').read_text())
            info = metadata.get('rosbag2_bagfile_information', metadata)
            data_files = info.get('relative_file_paths')
            if data_files is None:
                data_files = sorted(p.name for p in bag.iterdir() if p.suffix in ('.mcap', '.db3') and p.is_file())
            if not isinstance(data_files, list) or not data_files:
                raise ValueError
            for name_in_bag in data_files:
                path = Path(name_in_bag)
                if path.is_absolute() or '..' in path.parts or not path.parts:
                    raise ValueError
                for i in range(1, len(path.parts)+1):
                    if (bag/Path(*path.parts[:i])).is_symlink():
                        raise ValueError
                if not (bag/path).is_file():
                    raise ValueError
        except (OSError, ValueError, TypeError, AttributeError, yaml.YAMLError):
            raise web.HTTPConflict(text='录包数据文件缺失或元数据无效，未上传整包')
        if capture and not (bag/'capture.json').exists():
            self.write_json(bag/'capture.json', capture)
        link = {'schema_version': 1, 'bag': {'name': name, 'metadata_file': 'metadata.yaml', 'data_files': data_files},
                'request_id': capture.get('request_id'), 'capture_file': 'capture.json' if capture else None, 'audio': audio}
        self.write_json(bag/'recording-link.json', link)
        return link

    async def wait_bundle(self, name, timeout=45):
        deadline = time.monotonic() + timeout
        while True:
            link = self.bundle(name)
            if link is not None:
                return link
            if time.monotonic() >= deadline:
                raise TimeoutError('语音尚未保存，未上传整包；请重试保存语音，或在手机录制页结束待处理语音')
            await asyncio.sleep(.2)

    def bag(self, name):
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', name):
            raise web.HTTPNotFound(text='包名无效')
        path = (self.bags/name).resolve()
        if path.parent != self.bags or not path.is_dir():
            raise web.HTTPNotFound(text='没有这个录包')
        if not (path/'metadata.yaml').is_file():
            raise web.HTTPConflict(text='录包尚未写完')
        return path

    def voice(self, bag):
        path = bag/'voice-note.audio'
        if path.is_symlink() or not path.is_file():
            return None
        with path.open('rb') as f:
            mime = audio_type(f.read(16))
        if not mime:
            return None
        duration = None
        try:
            duration = json.loads((bag/'voice-note.json').read_text())['duration_s']
        except (OSError, ValueError, KeyError, TypeError):
            pass
        return {'url': f'/api/recordings/{quote(bag.name)}/audio', 'mime': mime, 'duration_s': duration}

    async def items(self, _req):
        request_id = _req.query.get('request_id')
        selected = None
        if request_id is not None:
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', request_id):
                raise web.HTTPBadRequest(text='录制请求ID无效')
            try:
                manifest = self.bags/'.captures'/f'{request_id}.json'
                if manifest.is_symlink():
                    raise ValueError
                selected = json.loads(manifest.read_text())['name']
            except (OSError, ValueError, KeyError, TypeError):
                return web.json_response({'items': []})
        def read():
            result = []
            for item in list_bags(str(self.bags)):
                if selected is not None and item['name'] != selected:
                    continue
                try:
                    bag = self.bag(item['name'])
                    capture = self.capture(bag)
                    if request_id is not None and capture.get('request_id') != request_id:
                        continue
                    result.append({**item, 'audio': self.voice(bag), 'request_id': capture.get('request_id'),
                                   'voice_state': capture.get('voice', {}).get('state'),
                                   'voice_reason': capture.get('voice', {}).get('reason', '')})
                except (web.HTTPException, OSError):
                    continue
            return result
        return web.json_response({'items': await asyncio.to_thread(read)}, headers={'Cache-Control': 'no-store'})

    async def upload(self, req):
        origin = req.headers.get('Origin')
        if origin and urlsplit(origin).netloc != req.host:
            raise web.HTTPForbidden(text='只接受当前网页上传')
        bag = self.bag(req.match_info['name'])
        capture = self.check_request(bag, req.query.get('request_id'))
        self.check_mutable(bag.name)
        try:
            duration = float(req.query['duration'])
            if not math.isfinite(duration) or not 0 < duration <= 20:
                raise ValueError
        except (KeyError, ValueError):
            raise web.HTTPBadRequest(text='录音时长必须大于0且不超过20秒')
        mime = req.content_type
        if mime not in MIME_EXT:
            raise web.HTTPUnsupportedMediaType(text='录音格式仅支持WebM、MP4或Ogg')
        target = bag/'voice-note.audio'
        metadata = bag/'voice-note.json'
        if bag in self.uploading or target.is_symlink() or metadata.is_symlink() or (metadata.exists() and not target.exists()):
            raise web.HTTPConflict(text='这个包已有语音备注')
        self.uploading.add(bag)
        tmp, created_metadata = None, False
        try:
            size, header, digest = 0, b'', hashlib.sha256()
            with tempfile.NamedTemporaryFile(dir=bag, prefix='.voice-', delete=False) as f:
                tmp = Path(f.name)
                async for chunk in req.content.iter_chunked(65536):
                    size += len(chunk)
                    if size > MAX_AUDIO_BYTES:
                        raise web.HTTPRequestEntityTooLarge(max_size=MAX_AUDIO_BYTES, actual_size=size)
                    header = (header + chunk)[:16]
                    digest.update(chunk)
                    f.write(chunk)
            if size < 16 or audio_type(header) != mime:
                raise web.HTTPBadRequest(text='没有收到有效录音')
            if target.exists():
                if hashlib.sha256(target.read_bytes()).digest() == digest.digest():
                    return web.json_response({'audio': self.voice(bag)})
                raise web.HTTPConflict(text='这个包已有不同的语音备注，不能覆盖')
            # 原子发布，重试/并发请求都不能覆盖已保存语音。
            try:
                with metadata.open('x') as f:
                    created_metadata = True
                    json.dump({'duration_s': duration, 'mime': mime, 'bag_name': bag.name,
                               'request_id': capture.get('request_id')}, f)
                os.link(tmp, target)
            except FileExistsError:
                raise web.HTTPConflict(text='这个包已有语音备注')
            self.on_change(bag.name)
            return web.json_response({'audio': self.voice(bag)}, status=201)
        finally:
            if created_metadata and not target.exists():
                metadata.unlink(missing_ok=True)
            if tmp:
                tmp.unlink(missing_ok=True)
            self.uploading.discard(bag)

    async def audio(self, req):
        bag = self.bag(req.match_info['name'])
        voice = self.voice(bag)
        if not voice:
            raise web.HTTPNotFound(text='这个包没有语音备注')
        return web.FileResponse(bag/'voice-note.audio', headers={
            'Content-Type': voice['mime'], 'X-Content-Type-Options': 'nosniff',
            'Content-Disposition': f'inline; filename="voice-note.{MIME_EXT[voice["mime"]]}"',
        })
