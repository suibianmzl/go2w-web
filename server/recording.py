"""录包的浏览器语音备注；与ROS录制独立，文件保存在原包目录。"""
import asyncio
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import quote, urlsplit

from aiohttp import web
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
    def __init__(self, bags_dir, static_dir):
        self.bags = Path(bags_dir).resolve()
        self.static = Path(static_dir)
        self.uploading = set()

    def routes(self, app):
        app.router.add_get('/record', self.page)
        app.router.add_get('/api/recordings', self.items)
        app.router.add_post('/api/recordings/{name}/audio', self.upload)
        app.router.add_get('/api/recordings/{name}/audio', self.audio)

    async def page(self, _req):
        return web.FileResponse(self.static/'record.html')

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
                    result.append({**item, 'audio': self.voice(bag)})
                except (web.HTTPException, OSError):
                    continue
            return result
        return web.json_response({'items': await asyncio.to_thread(read)}, headers={'Cache-Control': 'no-store'})

    async def upload(self, req):
        origin = req.headers.get('Origin')
        if origin and urlsplit(origin).netloc != req.host:
            raise web.HTTPForbidden(text='只接受当前网页上传')
        bag = self.bag(req.match_info['name'])
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
                    json.dump({'duration_s': duration, 'mime': mime}, f)
                os.link(tmp, target)
            except FileExistsError:
                raise web.HTTPConflict(text='这个包已有语音备注')
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
