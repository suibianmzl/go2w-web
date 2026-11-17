#!/usr/bin/env python3
"""Cloud WebSocket gateway for a remotely connected Go2W robot.

This process has no ROS dependency. It serves the console and relays the
existing browser protocol between browser sessions and one robot gateway.
"""
import argparse
import asyncio
import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from aiohttp import WSMsgType, web

from rtc_token import AccessToken, PRIV_PUBLISH_STREAM, PRIV_SUBSCRIBE_STREAM


T_MAP, T_SCAN, T_GCOST, T_LCOST, T_CLOUD, T_PART, T_CAM = range(1, 8)
LAYER_KINDS = {T_GCOST: 'gcost', T_LCOST: 'lcost', T_CLOUD: 'cloud', T_PART: 'particles'}
CACHE_TEXT = {'state', 'pose', 'task', 'routes', 'map_views', 'plan'}
TEXT_QUEUE_MAX = 500
ROBOT_QUEUE_MAX = 500
LOG = logging.getLogger('go2w_cloud')


@dataclass(eq=False)
class Browser:
    ws: web.WebSocketResponse
    number: int
    layers: set = field(default_factory=set)
    topics: set = field(default_factory=set)
    textq: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(TEXT_QUEUE_MAX))
    latest: dict = field(default_factory=dict)
    event: asyncio.Event = field(default_factory=asyncio.Event)
    tx: asyncio.Task | None = None

    def put(self, data, key=None):
        if key is None:
            if self.textq.full():
                return False
            self.textq.put_nowait(data)
        else:
            self.latest[key] = data
        self.event.set()
        return True


class CloudGateway:
    def __init__(self, static_dir, rtc_app_id=None, rtc_app_key=None, rtc_room_id=None, rtc_ttl=3600):
        self.static_dir = Path(static_dir).resolve()
        self.rtc_app_id = rtc_app_id
        self.rtc_app_key = rtc_app_key
        self.rtc_room_id = rtc_room_id or 'go2w-01'
        self.rtc_ttl = int(rtc_ttl)
        self.clients = {}
        self.camera_clients = {}
        self.client_seq = 0
        self.owner = None
        self.robot_ws = None
        self.robot_id = None
        self.robot_queue = asyncio.Queue(ROBOT_QUEUE_MAX)
        self.robot_tx = None
        self.robot_generation = 0
        self.last_robot_seen = 0.0
        self.rtc_publisher_state = {'state': 'unknown'}
        self.text_cache = {}
        self.binary_cache = {}
        self.ack_routes = {}
        self.command_seq = 0
        self.active_topics = set()

    # ---------- browser output ----------
    def send_browser(self, client, data, key=None):
        if client.ws.closed:
            return
        client.put(data, key)

    async def browser_tx(self, client):
        try:
            while True:
                await client.event.wait()
                client.event.clear()
                while not client.textq.empty() or client.latest:
                    if not client.textq.empty():
                        data = client.textq.get_nowait()
                    else:
                        key = next(iter(client.latest))
                        data = client.latest.pop(key)
                    if isinstance(data, bytes):
                        await client.ws.send_bytes(data)
                    else:
                        await client.ws.send_str(data)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOG.exception('browser sender failed')
            if not client.ws.closed:
                await client.ws.close()

    def broadcast_text(self, data):
        for client in list(self.clients.values()):
            self.send_browser(client, data)

    def broadcast_binary(self, data, kind):
        layer = LAYER_KINDS.get(kind)
        for client in list(self.clients.values()):
            if layer is None or layer in client.layers:
                self.send_browser(client, data, key=f'b{kind}')

    def replay(self, client):
        for key in ('state', 'pose', 'task', 'routes', 'map_views', 'plan'):
            if key in self.text_cache:
                self.send_browser(client, self.text_cache[key], key=f't{key}')
        for kind in (T_MAP, T_SCAN):
            if kind in self.binary_cache:
                self.send_browser(client, self.binary_cache[kind], key=f'b{kind}')
        for kind, layer in LAYER_KINDS.items():
            if layer in client.layers and kind in self.binary_cache:
                self.send_browser(client, self.binary_cache[kind], key=f'b{kind}')

    def robot_status(self):
        return {
            't': 'robot_status',
            'online': self.robot_ws is not None and not self.robot_ws.closed,
            'robot_id': self.robot_id,
        }

    def broadcast_robot_status(self):
        data = json.dumps(self.robot_status(), ensure_ascii=False, separators=(',', ':'))
        self.broadcast_text(data)

    # ---------- robot output ----------
    def send_robot(self, data):
        if self.robot_ws is None or self.robot_ws.closed:
            return False
        try:
            self.robot_queue.put_nowait(data)
            return True
        except asyncio.QueueFull:
            return False

    async def robot_tx_loop(self, generation):
        try:
            while generation == self.robot_generation:
                data = await self.robot_queue.get()
                if self.robot_ws is None or self.robot_ws.closed:
                    continue
                if isinstance(data, bytes):
                    await self.robot_ws.send_bytes(data)
                else:
                    await self.robot_ws.send_str(data)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOG.exception('robot sender failed')
            if self.robot_ws is not None and not self.robot_ws.closed:
                await self.robot_ws.close()

    async def replace_robot(self, ws):
        old = self.robot_ws
        self.robot_generation += 1
        generation = self.robot_generation
        if self.robot_tx:
            self.robot_tx.cancel()
            await asyncio.gather(self.robot_tx, return_exceptions=True)
        while not self.robot_queue.empty():
            self.robot_queue.get_nowait()
        self.robot_ws, self.robot_id = ws, None
        self.robot_tx = asyncio.create_task(self.robot_tx_loop(generation))
        if old is not None and old is not ws and not old.closed:
            await old.close(code=4000, message=b'replaced by a new robot connection')
        return generation

    async def clear_robot(self, ws):
        if self.robot_ws is not ws:
            return
        self.robot_ws = None
        self.robot_id = None
        self.text_cache.clear()
        self.binary_cache.clear()
        self.active_topics.clear()
        for key, (client, original_id) in list(self.ack_routes.items()):
            self.send_browser(client, json.dumps({
                't': 'ack', 'id': original_id, 'ok': False, 'msg': '机器狗连接已断开',
            }, ensure_ascii=False, separators=(',', ':')))
        self.ack_routes.clear()
        self.robot_generation += 1
        if self.robot_tx:
            self.robot_tx.cancel()
            await asyncio.gather(self.robot_tx, return_exceptions=True)
            self.robot_tx = None
        self.broadcast_robot_status()

    # ---------- subscriptions ----------
    def sync_layers(self):
        if self.robot_ws is None:
            return
        layers = sorted({layer for client in self.clients.values() for layer in client.layers})
        self.send_robot(json.dumps({'t': 'sub', 'layers': layers}, separators=(',', ':')))

    def sync_topics(self):
        wanted = {topic for client in self.clients.values() for topic in client.topics}
        for topic in sorted(wanted - self.active_topics):
            self.send_robot(json.dumps({'t': 'inspect', 'topic': topic, 'on': True}, separators=(',', ':')))
        for topic in sorted(self.active_topics - wanted):
            self.send_robot(json.dumps({'t': 'inspect', 'topic': topic, 'on': False}, separators=(',', ':')))
        self.active_topics = wanted

    def sync_camera(self):
        self.send_robot(json.dumps({
            't': 'cam', 'on': bool(self.camera_clients),
        }, separators=(',', ':')))

    # ---------- robot messages ----------
    async def on_robot_text(self, data):
        self.last_robot_seen = time.monotonic()
        try:
            msg = json.loads(data)
        except ValueError:
            self.broadcast_text(data)
            return
        if msg.get('type') == 'hello':
            self.robot_id = msg.get('robot_id')
            self.active_topics.clear()
            self.sync_layers()
            self.sync_topics()
            self.sync_camera()
            self.broadcast_robot_status()
            return
        if msg.get('t') == 'pong':
            return
        if msg.get('t') == 'ack':
            key = str(msg.get('id'))
            route = self.ack_routes.pop(key, None)
            if route is not None:
                client, original_id = route
                if msg.get('id') is not None:
                    msg['id'] = original_id
                self.send_browser(client, json.dumps(msg, ensure_ascii=False, separators=(',', ':')))
                return
        if msg.get('t') == 'msg':
            topic = msg.get('topic')
            for client in list(self.clients.values()):
                if topic in client.topics:
                    self.send_browser(client, data)
            return
        msg_type = msg.get('t')
        if msg_type in CACHE_TEXT:
            self.text_cache[msg_type] = data
        if msg_type != 'cam':
            self.broadcast_text(data)

    def on_robot_binary(self, data):
        self.last_robot_seen = time.monotonic()
        if not data:
            return
        kind = data[0]
        if kind == T_CAM:
            for client in list(self.camera_clients.values()):
                self.send_browser(client, data)
            return
        if kind not in LAYER_KINDS and kind not in (T_MAP, T_SCAN):
            return
        self.binary_cache[kind] = data
        self.broadcast_binary(data, kind)

    async def handle_robot(self, req):
        ws = web.WebSocketResponse(max_msg_size=16 * 1024 * 1024, heartbeat=20)
        await ws.prepare(req)
        await self.replace_robot(ws)
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    await self.on_robot_text(msg.data)
                elif msg.type == WSMsgType.BINARY:
                    self.on_robot_binary(msg.data)
                elif msg.type == WSMsgType.ERROR:
                    break
        except Exception:
            LOG.exception('robot handler failed')
        finally:
            await self.clear_robot(ws)
        return ws

    # ---------- browser messages ----------
    def ensure_owner(self, client):
        if self.owner is None or self.owner not in self.clients:
            self.owner = client
        return self.owner is client

    async def stop_on_owner_loss(self):
        if self.robot_ws is None:
            return
        self.send_robot(json.dumps({'t': 'cmd', 'id': f'cloud-stop-{self.command_seq}', 'op': 'cancel'}))
        self.command_seq += 1
        self.send_robot(json.dumps({'t': 'cmd', 'id': f'cloud-lock-{self.command_seq}', 'op': 'arm', 'on': False}))
        self.command_seq += 1

    async def on_browser_text(self, client, data):
        try:
            msg = json.loads(data)
        except ValueError:
            return
        msg_type = msg.get('t')
        if msg_type == 'ping':
            self.send_browser(client, json.dumps({'t': 'pong', 'ts': msg.get('ts')}, separators=(',', ':')))
            if client is self.owner:
                self.send_robot(data)
            return
        if msg_type == 'cam':
            self.send_browser(client, json.dumps({
                't': 'cam', 'state': 'error', 'msg': '视频已迁移到 RTC 链路'
            }, ensure_ascii=False, separators=(',', ':')))
            return
        if msg_type == 'sub':
            client.layers = {x for x in msg.get('layers', []) if x in LAYER_KINDS.values()}
            self.sync_layers()
            self.replay(client)
            return
        if msg_type == 'inspect' and isinstance(msg.get('topic'), str):
            topic = msg['topic']
            if msg.get('on'):
                client.topics.add(topic)
            else:
                client.topics.discard(topic)
            self.sync_topics()
            return
        if msg_type in ('cmd', 'teleop'):
            if not self.ensure_owner(client):
                if msg_type == 'cmd':
                    self.send_browser(client, json.dumps({
                        't': 'ack', 'id': msg.get('id'), 'ok': False, 'msg': '另一个页面正在控制'
                    }, ensure_ascii=False, separators=(',', ':')))
                return
            if msg_type == 'cmd':
                original_id = msg.get('id')
                upstream_id = f'cloud-{self.command_seq}'
                self.command_seq += 1
                self.ack_routes[str(upstream_id)] = (client, original_id)
                msg['id'] = upstream_id
                data = json.dumps(msg, ensure_ascii=False, separators=(',', ':'))
            if not self.send_robot(data) and msg_type == 'cmd':
                self.ack_routes.pop(str(upstream_id), None)
                self.send_browser(client, json.dumps({
                    't': 'ack', 'id': original_id, 'ok': False, 'msg': '机器狗未连接'
                }, ensure_ascii=False, separators=(',', ':')))
            return
        if client is self.owner:
            self.send_robot(data)

    async def browser_ws(self, req):
        ws = web.WebSocketResponse(max_msg_size=1 << 20, heartbeat=20)
        await ws.prepare(req)
        self.client_seq += 1
        client = Browser(ws, self.client_seq)
        self.clients[ws] = client
        client.tx = asyncio.create_task(self.browser_tx(client))
        if self.owner is None:
            self.owner = client
        self.send_browser(client, json.dumps(
            self.robot_status(), ensure_ascii=False, separators=(',', ':')), key='robot_status')
        if self.robot_ws is not None and not self.robot_ws.closed:
            self.replay(client)
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    await self.on_browser_text(client, msg.data)
                elif msg.type == WSMsgType.ERROR:
                    break
        except Exception:
            LOG.exception('browser handler failed')
        finally:
            was_owner = self.owner is client
            self.clients.pop(ws, None)
            for key, (target, _) in list(self.ack_routes.items()):
                if target is client:
                    self.ack_routes.pop(key, None)
            self.sync_layers()
            self.sync_topics()
            if was_owner:
                self.owner = None
                await self.stop_on_owner_loss()
            if client.tx:
                client.tx.cancel()
                await asyncio.gather(client.tx, return_exceptions=True)
        return ws

    async def camera_source_ws(self, req):
        ws = web.WebSocketResponse(max_msg_size=1 << 20, heartbeat=20)
        await ws.prepare(req)
        self.client_seq += 1
        client = Browser(ws, self.client_seq)
        self.camera_clients[ws] = client
        client.tx = asyncio.create_task(self.browser_tx(client))
        if len(self.camera_clients) == 1:
            self.sync_camera()
        try:
            async for msg in ws:
                if msg.type == WSMsgType.ERROR:
                    break
        finally:
            self.camera_clients.pop(ws, None)
            if not self.camera_clients:
                self.sync_camera()
            if client.tx:
                client.tx.cancel()
                await asyncio.gather(client.tx, return_exceptions=True)
        return ws

    # ---------- HTTP ----------
    async def index(self, _req):
        return web.FileResponse(self.static_dir / 'index.html')

    async def static_file(self, req):
        rel = req.match_info.get('path', '')
        path = (self.static_dir / rel).resolve()
        if path != self.static_dir and self.static_dir not in path.parents:
            raise web.HTTPNotFound()
        if path.is_file():
            return web.FileResponse(path)
        raise web.HTTPNotFound()

    async def health(self, _req):
        return web.json_response({
            'ok': True,
            'robot_online': self.robot_ws is not None and not self.robot_ws.closed,
            'robot_id': self.robot_id,
            'last_seen_seconds': round(time.monotonic() - self.last_robot_seen, 1) if self.last_robot_seen else None,
            'browsers': len(self.clients),
            'camera_sources': len(self.camera_clients),
        })

    async def rtc_config(self, req):
        if not self.rtc_app_id or not self.rtc_app_key:
            raise web.HTTPServiceUnavailable(text='RTC credentials are not configured')
        role = req.query.get('role', 'viewer')
        robot_id = re.sub(r'[^A-Za-z0-9_-]', '', req.query.get('robot_id', 'go2w-01'))[:32] or 'go2w-01'
        publisher_id = f'{robot_id}-camera'
        user_id = publisher_id if role == 'robot' else f'browser-{uuid.uuid4().hex[:12]}'
        expire_at = int(time.time()) + self.rtc_ttl
        token = AccessToken(self.rtc_app_id, self.rtc_app_key, self.rtc_room_id, user_id)
        token.expire_time(expire_at)
        if role == 'robot':
            token.add_privilege(PRIV_PUBLISH_STREAM, expire_at)
        token.add_privilege(PRIV_SUBSCRIBE_STREAM, expire_at)
        return web.json_response({
            'app_id': self.rtc_app_id,
            'room_id': self.rtc_room_id,
            'user_id': user_id,
            'token': token.serialize(),
            'publisher_id': publisher_id,
            'role': role,
            'expires_at': expire_at,
        }, headers={'Access-Control-Allow-Origin': '*'})

    async def rtc_publisher_status(self, req):
        if req.method == 'POST':
            try:
                data = json.loads(await req.text())
            except (TypeError, ValueError):
                raise web.HTTPBadRequest(text='invalid publisher status')
            if isinstance(data, dict):
                self.rtc_publisher_state = {
                    key: value for key, value in data.items()
                    if isinstance(key, str) and len(str(value)) < 2000
                }
                self.rtc_publisher_state['updated_at'] = int(time.time())
        return web.json_response(self.rtc_publisher_state,
                                 headers={'Access-Control-Allow-Origin': '*'})

    def app(self):
        @web.middleware
        async def no_cache(req, handler):
            response = await handler(req)
            response.headers['Cache-Control'] = 'no-store'
            return response

        app = web.Application(middlewares=[no_cache])
        app.router.add_get('/health', self.health)
        app.router.add_get('/quadruped/health', self.health)
        app.router.add_get('/quadruped/rtc/config', self.rtc_config)
        app.router.add_route('*', '/quadruped/rtc/publisher-status', self.rtc_publisher_status)
        app.router.add_get('/robot/ws', self.handle_robot)
        app.router.add_get('/quadruped/robot/ws', self.handle_robot)
        app.router.add_get('/quadruped/ws', self.browser_ws)
        app.router.add_get('/quadruped/camera/ws', self.camera_source_ws)
        app.router.add_get('/ws', self.browser_ws)
        app.router.add_get('/quadruped', lambda _r: web.HTTPPermanentRedirect('/quadruped/'))
        app.router.add_get('/quadruped/', self.index)
        app.router.add_get('/quadruped/{path:.*}', self.static_file)
        app.router.add_get('/', lambda _r: web.HTTPPermanentRedirect('/quadruped/'))
        return app


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    here = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--port', type=int, default=8080)
    parser.add_argument('--static', default=str(here.parent / 'static'))
    args = parser.parse_args()
    gateway = CloudGateway(
        args.static,
        rtc_app_id=os.environ.get('GO2W_RTC_APP_ID'),
        rtc_app_key=os.environ.get('GO2W_RTC_APP_KEY'),
        rtc_room_id=os.environ.get('GO2W_RTC_ROOM_ID', 'go2w-01'),
        rtc_ttl=int(os.environ.get('GO2W_RTC_TOKEN_TTL', '3600')),
    )
    web.run_app(gateway.app(), host=args.host, port=args.port,
                print=None, handle_signals=True, shutdown_timeout=3.0)


if __name__ == '__main__':
    main()
