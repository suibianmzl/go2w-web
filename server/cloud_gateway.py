#!/usr/bin/env python3
"""Cloud WebSocket gateway for a remotely connected Go2W robot.

This process has no ROS dependency. It serves the console and relays the
existing browser protocol between browser sessions and multiple robot gateways.
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
from datetime import datetime
from pathlib import Path

from aiohttp import WSMsgType, web
from pymysql.err import IntegrityError

from rtc_token import AccessToken, PRIV_PUBLISH_STREAM, PRIV_SUBSCRIBE_STREAM
from ssh_bindings import SSHBinding, SSHBindingStore, parse_time
from ssh_portbroker import PortBrokerClient
from ssh_registration import SSHRegistrationError, SSHRegistrationService


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
    robot_id: str
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


@dataclass(eq=False)
class RobotSession:
    ws: web.WebSocketResponse
    robot_id: str | None = None
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(ROBOT_QUEUE_MAX))
    tx: asyncio.Task | None = None
    last_seen: float = 0.0
    text_cache: dict = field(default_factory=dict)
    binary_cache: dict = field(default_factory=dict)
    ack_routes: dict = field(default_factory=dict)
    active_topics: set = field(default_factory=set)
    owner: Browser | None = None


class CloudGateway:
    def __init__(
        self,
        static_dir,
        rtc_app_id=None,
        rtc_app_key=None,
        rtc_room_id=None,
        rtc_ttl=3600,
        default_robot_id=None,
        ssh_store=None,
        ssh_broker=None,
        ssh_registration=None,
    ):
        self.static_dir = Path(static_dir).resolve()
        self.rtc_app_id = rtc_app_id
        self.rtc_app_key = rtc_app_key
        self.rtc_room_id = rtc_room_id or 'go2w-01'
        self.rtc_ttl = int(rtc_ttl)
        self.default_robot_id = self.clean_robot_id(
            default_robot_id or os.environ.get('GO2W_DEFAULT_ROBOT_ID') or 'go2w-01'
        )
        self.ssh_bindings = ssh_store or SSHBindingStore(
            os.environ.get('GO2W_SSH_MYSQL_DSN'),
            os.environ.get('GO2W_SSH_REDIS_URL'),
        )
        self.ssh_registration = ssh_registration or SSHRegistrationService(
            self.ssh_bindings,
            ssh_broker or PortBrokerClient(
                os.environ.get(
                    'GO2W_SSH_PORTBROKER_URL',
                    'https://ssh-portbroker-api.evoxrobot.com',
                )
            ),
        )
        self.clients = {}
        self.camera_clients = {}
        self.client_seq = 0
        self.robots = {}
        self.robot_sessions = {}
        self.robot_records = {}
        self.rtc_publisher_states = {}
        self.command_seq = 0

    @staticmethod
    def clean_robot_id(value):
        return re.sub(r'[^A-Za-z0-9_-]', '', str(value or ''))[:32]

    @staticmethod
    def clean_device_id(value):
        return re.sub(r'[^A-Za-z0-9_.-]', '', str(value or ''))[:128]

    def requested_robot_id(self, req):
        return self.clean_robot_id(req.query.get('robot_id')) or self.default_robot_id

    def clients_for(self, robot_id):
        return [client for client in self.clients.values() if client.robot_id == robot_id]

    def camera_clients_for(self, robot_id):
        return [client for client in self.camera_clients.values() if client.robot_id == robot_id]

    def video_upload_enabled(self, robot_id):
        record = self.robot_records.get(robot_id, {})
        return record.get('video_upload_enabled', True) is not False

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

    def broadcast_text(self, robot_id, data):
        for client in self.clients_for(robot_id):
            self.send_browser(client, data)

    def broadcast_binary(self, robot_id, data, kind):
        layer = LAYER_KINDS.get(kind)
        for client in self.clients_for(robot_id):
            if layer is None or layer in client.layers:
                self.send_browser(client, data, key=f'b{kind}')

    def replay(self, client):
        session = self.robots.get(client.robot_id)
        if session is None:
            return
        for key in ('state', 'pose', 'task', 'routes', 'map_views', 'plan'):
            if key in session.text_cache:
                self.send_browser(client, session.text_cache[key], key=f't{key}')
        for kind in (T_MAP, T_SCAN):
            if kind in session.binary_cache:
                self.send_browser(client, session.binary_cache[kind], key=f'b{kind}')
        for kind, layer in LAYER_KINDS.items():
            if layer in client.layers and kind in session.binary_cache:
                self.send_browser(client, session.binary_cache[kind], key=f'b{kind}')

    def robot_status(self, robot_id):
        session = self.robots.get(robot_id)
        return {
            't': 'robot_status',
            'online': session is not None and not session.ws.closed,
            'robot_id': robot_id,
            'video_upload_enabled': self.video_upload_enabled(robot_id),
        }

    def broadcast_robot_status(self, robot_id):
        data = json.dumps(self.robot_status(robot_id), ensure_ascii=False, separators=(',', ':'))
        self.broadcast_text(robot_id, data)

    # ---------- robot output ----------
    def send_robot(self, robot_id, data):
        session = self.robots.get(robot_id)
        if session is None or session.ws.closed:
            return False
        try:
            session.queue.put_nowait(data)
            return True
        except asyncio.QueueFull:
            return False

    async def robot_tx_loop(self, session):
        try:
            while not session.ws.closed:
                data = await session.queue.get()
                if session.ws.closed:
                    continue
                if isinstance(data, bytes):
                    await session.ws.send_bytes(data)
                else:
                    await session.ws.send_str(data)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOG.exception('robot sender failed')
            if not session.ws.closed:
                await session.ws.close()

    def touch_robot(self, session):
        now = time.monotonic()
        session.last_seen = now
        if session.robot_id:
            record = self.robot_records.setdefault(session.robot_id, {'robot_id': session.robot_id})
            record['online'] = True
            record['last_seen_at'] = time.time()
            record.setdefault('video_upload_enabled', True)

    async def register_robot(self, session, robot_id):
        session.robot_id = robot_id
        old = self.robots.get(robot_id)
        self.robots[robot_id] = session
        self.touch_robot(session)
        if old is not None and old is not session and not old.ws.closed:
            await old.ws.close(code=4000, message=b'replaced by a new robot connection')
        self.sync_layers(robot_id)
        self.sync_topics(robot_id)
        self.sync_camera(robot_id)
        self.broadcast_robot_status(robot_id)

    async def clear_robot(self, session):
        self.robot_sessions.pop(session.ws, None)
        robot_id = session.robot_id
        if robot_id and self.robots.get(robot_id) is session:
            del self.robots[robot_id]
            record = self.robot_records.setdefault(robot_id, {'robot_id': robot_id})
            record['online'] = False
            record['last_seen_at'] = time.time()
        for key, (client, original_id) in list(session.ack_routes.items()):
            self.send_browser(client, json.dumps({
                't': 'ack', 'id': original_id, 'ok': False, 'msg': '机器狗连接已断开',
            }, ensure_ascii=False, separators=(',', ':')))
        session.ack_routes.clear()
        if session.tx:
            session.tx.cancel()
            await asyncio.gather(session.tx, return_exceptions=True)
            session.tx = None
        if robot_id:
            self.broadcast_robot_status(robot_id)

    # ---------- subscriptions ----------
    def sync_layers(self, robot_id):
        if robot_id not in self.robots:
            return
        layers = sorted({layer for client in self.clients_for(robot_id) for layer in client.layers})
        self.send_robot(robot_id, json.dumps({'t': 'sub', 'layers': layers}, separators=(',', ':')))

    def sync_topics(self, robot_id):
        session = self.robots.get(robot_id)
        if session is None:
            return
        wanted = {topic for client in self.clients_for(robot_id) for topic in client.topics}
        for topic in sorted(wanted - session.active_topics):
            self.send_robot(robot_id, json.dumps(
                {'t': 'inspect', 'topic': topic, 'on': True}, separators=(',', ':')))
        for topic in sorted(session.active_topics - wanted):
            self.send_robot(robot_id, json.dumps(
                {'t': 'inspect', 'topic': topic, 'on': False}, separators=(',', ':')))
        session.active_topics = wanted

    def sync_camera(self, robot_id):
        self.send_robot(robot_id, json.dumps({
            't': 'cam',
            'on': self.video_upload_enabled(robot_id) and bool(self.camera_clients_for(robot_id)),
        }, separators=(',', ':')))

    # ---------- robot messages ----------
    async def on_robot_text(self, session, data):
        self.touch_robot(session)
        try:
            msg = json.loads(data)
        except ValueError:
            if session.robot_id:
                self.broadcast_text(session.robot_id, data)
            return
        if msg.get('type') == 'hello':
            robot_id = self.clean_robot_id(msg.get('robot_id'))
            if not robot_id:
                LOG.warning('robot hello without a valid robot_id')
                return
            record = self.robot_records.setdefault(robot_id, {'robot_id': robot_id})
            record['name'] = msg.get('name') or record.get('name') or robot_id
            await self.register_robot(session, robot_id)
            return
        robot_id = session.robot_id
        if not robot_id:
            return
        message_robot_id = self.clean_robot_id(msg.get('robot_id'))
        if message_robot_id and message_robot_id != robot_id:
            LOG.warning('dropped robot message for %s from %s', message_robot_id, robot_id)
            return
        if msg.get('t') == 'pong':
            return
        if msg.get('t') == 'ack':
            key = str(msg.get('id'))
            route = session.ack_routes.pop(key, None)
            if route is not None:
                client, original_id = route
                if msg.get('id') is not None:
                    msg['id'] = original_id
                self.send_browser(client, json.dumps(msg, ensure_ascii=False, separators=(',', ':')))
                return
        if msg.get('t') == 'msg':
            topic = msg.get('topic')
            for client in self.clients_for(robot_id):
                if topic in client.topics:
                    self.send_browser(client, data)
            return
        msg_type = msg.get('t')
        record = self.robot_records.setdefault(robot_id, {'robot_id': robot_id})
        if msg_type == 'task':
            record['task'] = msg.get('task')
        elif msg_type == 'state':
            record['battery'] = msg.get('battery')
        if msg_type in CACHE_TEXT:
            session.text_cache[msg_type] = data
        if msg_type != 'cam':
            self.broadcast_text(robot_id, data)

    def on_robot_binary(self, session, data):
        self.touch_robot(session)
        robot_id = session.robot_id
        if not robot_id:
            return
        if not data:
            return
        kind = data[0]
        if kind == T_CAM:
            if not self.video_upload_enabled(robot_id):
                return
            for client in self.camera_clients_for(robot_id):
                self.send_browser(client, data)
            return
        if kind not in LAYER_KINDS and kind not in (T_MAP, T_SCAN):
            return
        session.binary_cache[kind] = data
        self.broadcast_binary(robot_id, data, kind)

    async def handle_robot(self, req):
        ws = web.WebSocketResponse(max_msg_size=16 * 1024 * 1024, heartbeat=20)
        await ws.prepare(req)
        session = RobotSession(ws)
        self.robot_sessions[ws] = session
        session.tx = asyncio.create_task(self.robot_tx_loop(session))
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    await self.on_robot_text(session, msg.data)
                elif msg.type == WSMsgType.BINARY:
                    self.on_robot_binary(session, msg.data)
                elif msg.type == WSMsgType.ERROR:
                    break
        except Exception:
            LOG.exception('robot handler failed')
        finally:
            await self.clear_robot(session)
        return ws

    # ---------- browser messages ----------
    def ensure_owner(self, session, client):
        if session is None:
            return False
        if session.owner is None or session.owner not in self.clients_for(session.robot_id):
            session.owner = client
        return session.owner is client

    async def stop_on_owner_loss(self, session):
        if session is None or session.ws.closed:
            return
        self.send_robot(session.robot_id, json.dumps({
            't': 'cmd', 'id': f'cloud-stop-{self.command_seq}', 'op': 'cancel',
        }, separators=(',', ':')))
        self.command_seq += 1
        self.send_robot(session.robot_id, json.dumps({
            't': 'cmd', 'id': f'cloud-lock-{self.command_seq}', 'op': 'arm', 'on': False,
        }, separators=(',', ':')))
        self.command_seq += 1

    async def on_browser_text(self, client, data):
        try:
            msg = json.loads(data)
        except ValueError:
            return
        msg_type = msg.get('t')
        if msg_type == 'ping':
            self.send_browser(client, json.dumps({'t': 'pong', 'ts': msg.get('ts')}, separators=(',', ':')))
            session = self.robots.get(client.robot_id)
            if session is not None and session.owner is client:
                self.send_robot(client.robot_id, data)
            return
        if msg_type == 'cam':
            self.send_browser(client, json.dumps({
                't': 'cam', 'state': 'error', 'msg': '视频已迁移到 RTC 链路'
            }, ensure_ascii=False, separators=(',', ':')))
            return
        if msg_type == 'sub':
            client.layers = {x for x in msg.get('layers', []) if x in LAYER_KINDS.values()}
            self.sync_layers(client.robot_id)
            self.replay(client)
            return
        if msg_type == 'inspect' and isinstance(msg.get('topic'), str):
            topic = msg['topic']
            if msg.get('on'):
                client.topics.add(topic)
            else:
                client.topics.discard(topic)
            self.sync_topics(client.robot_id)
            return
        if msg_type in ('cmd', 'teleop'):
            session = self.robots.get(client.robot_id)
            if session is None:
                if msg_type == 'cmd':
                    self.send_browser(client, json.dumps({
                        't': 'ack', 'id': msg.get('id'), 'ok': False, 'msg': '机器狗未连接',
                    }, ensure_ascii=False, separators=(',', ':')))
                return
            if not self.ensure_owner(session, client):
                if msg_type == 'cmd':
                    self.send_browser(client, json.dumps({
                        't': 'ack', 'id': msg.get('id'), 'ok': False, 'msg': '另一个页面正在控制'
                    }, ensure_ascii=False, separators=(',', ':')))
                return
            if msg_type == 'cmd':
                original_id = msg.get('id')
                upstream_id = f'cloud-{self.command_seq}'
                self.command_seq += 1
                session.ack_routes[str(upstream_id)] = (client, original_id)
                msg['id'] = upstream_id
                msg['robot_id'] = client.robot_id
                data = json.dumps(msg, ensure_ascii=False, separators=(',', ':'))
            if not self.send_robot(client.robot_id, data) and msg_type == 'cmd':
                session.ack_routes.pop(str(upstream_id), None)
                self.send_browser(client, json.dumps({
                    't': 'ack', 'id': original_id, 'ok': False, 'msg': '机器狗未连接'
                }, ensure_ascii=False, separators=(',', ':')))
            return
        session = self.robots.get(client.robot_id)
        if session is not None and session.owner is client:
            self.send_robot(client.robot_id, data)

    async def browser_ws(self, req):
        ws = web.WebSocketResponse(max_msg_size=1 << 20, heartbeat=20)
        await ws.prepare(req)
        self.client_seq += 1
        robot_id = self.requested_robot_id(req)
        client = Browser(ws, self.client_seq, robot_id)
        self.clients[ws] = client
        client.tx = asyncio.create_task(self.browser_tx(client))
        session = self.robots.get(robot_id)
        if session is not None and session.owner is None:
            session.owner = client
        self.send_browser(client, json.dumps(
            self.robot_status(robot_id), ensure_ascii=False, separators=(',', ':')), key='robot_status')
        if session is not None and not session.ws.closed:
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
            self.clients.pop(ws, None)
            session = self.robots.get(robot_id)
            if session is not None:
                for key, (target, _) in list(session.ack_routes.items()):
                    if target is client:
                        session.ack_routes.pop(key, None)
                self.sync_layers(robot_id)
                self.sync_topics(robot_id)
                if session.owner is client:
                    session.owner = None
                    await self.stop_on_owner_loss(session)
            if client.tx:
                client.tx.cancel()
                await asyncio.gather(client.tx, return_exceptions=True)
        return ws

    async def camera_source_ws(self, req):
        ws = web.WebSocketResponse(max_msg_size=1 << 20, heartbeat=20)
        await ws.prepare(req)
        self.client_seq += 1
        robot_id = self.requested_robot_id(req)
        client = Browser(ws, self.client_seq, robot_id)
        had_camera = bool(self.camera_clients_for(robot_id))
        self.camera_clients[ws] = client
        client.tx = asyncio.create_task(self.browser_tx(client))
        if not had_camera:
            self.sync_camera(robot_id)
        try:
            async for msg in ws:
                if msg.type == WSMsgType.ERROR:
                    break
        finally:
            self.camera_clients.pop(ws, None)
            if not self.camera_clients_for(robot_id):
                self.sync_camera(robot_id)
            if client.tx:
                client.tx.cancel()
                await asyncio.gather(client.tx, return_exceptions=True)
        return ws

    # ---------- internal SSH bindings ----------
    @staticmethod
    def internal_request_id(req):
        value = req.headers.get('X-Request-Id', '')
        if re.fullmatch(r'[A-Za-z0-9._:-]{1,128}', value):
            return value
        return f'req-{uuid.uuid4()}'

    @staticmethod
    def internal_json(request_id, payload, status=200):
        return web.json_response(payload, status=status, headers={
            'X-Request-Id': request_id,
            'Cache-Control': 'no-store',
        })

    def internal_error(self, request_id, status, code, message):
        return self.internal_json(request_id, {
            'code': code,
            'message': message,
            'requestId': request_id,
        }, status)

    async def internal_ssh_bindings(self, req):
        request_id = self.internal_request_id(req)
        try:
            bindings = await self.ssh_bindings.list()
        except Exception:
            LOG.exception('list SSH bindings failed')
            return self.internal_error(
                request_id, 503, 'SERVICE_UNAVAILABLE', 'SSH binding storage unavailable')
        return self.internal_json(request_id, {
            'items': [binding.to_api(include_last_seen=False) for binding in bindings],
        })

    async def internal_ssh_binding_by_port(self, req):
        request_id = self.internal_request_id(req)
        try:
            port = int(req.match_info['port'])
            if port < 1 or port > 65535:
                raise ValueError
        except ValueError:
            return self.internal_error(
                request_id, 400, 'INVALID_ARGUMENT', 'port must be between 1 and 65535')
        try:
            binding = await self.ssh_bindings.get_by_port(port)
        except Exception:
            LOG.exception('get SSH binding by port failed')
            return self.internal_error(
                request_id, 503, 'SERVICE_UNAVAILABLE', 'SSH binding storage unavailable')
        if binding is None:
            return self.internal_error(request_id, 404, 'NOT_FOUND', 'SSH binding not found')
        return self.internal_json(request_id, binding.to_api())

    async def internal_ssh_binding_upsert(self, req):
        request_id = self.internal_request_id(req)
        try:
            payload = await req.json()
        except Exception:
            return self.internal_error(request_id, 400, 'INVALID_ARGUMENT', 'invalid JSON request')
        if not isinstance(payload, dict):
            return self.internal_error(request_id, 400, 'INVALID_ARGUMENT', 'JSON object required')
        device_id = self.clean_device_id(req.match_info.get('device_id'))
        if not device_id or payload.get('deviceId', device_id) != device_id:
            return self.internal_error(request_id, 400, 'INVALID_ARGUMENT', 'deviceId mismatch')
        status = str(payload.get('status', 'bound'))
        if status not in {'bound', 'unknown', 'unbound'}:
            return self.internal_error(request_id, 400, 'INVALID_ARGUMENT', 'invalid binding status')
        required = ('deviceName', 'deviceModel', 'host', 'username', 'bindingVersion')
        missing = [key for key in required if not isinstance(payload.get(key), str) or not payload[key]]
        if missing:
            return self.internal_error(
                request_id, 400, 'INVALID_ARGUMENT', f'missing fields: {", ".join(missing)}')
        port = payload.get('port')
        if port is not None:
            try:
                port = int(port)
                if port < 1 or port > 65535:
                    raise ValueError
            except (TypeError, ValueError):
                return self.internal_error(request_id, 400, 'INVALID_ARGUMENT', 'invalid port')
        if status == 'bound' and port is None:
            return self.internal_error(request_id, 400, 'INVALID_ARGUMENT', 'bound status requires port')
        try:
            last_seen_at = parse_time(payload.get('lastSeenAt'))
        except ValueError:
            return self.internal_error(request_id, 400, 'INVALID_ARGUMENT', 'invalid lastSeenAt')
        serial_number = payload.get('serialNumber')
        firmware_version = payload.get('firmwareVersion')
        if serial_number is not None and (not isinstance(serial_number, str) or len(serial_number) > 128):
            return self.internal_error(request_id, 400, 'INVALID_ARGUMENT', 'invalid serialNumber')
        if firmware_version is not None and (
            not isinstance(firmware_version, str) or len(firmware_version) > 128
        ):
            return self.internal_error(request_id, 400, 'INVALID_ARGUMENT', 'invalid firmwareVersion')
        binding = SSHBinding(
            device_id=device_id,
            device_name=payload['deviceName'],
            device_model=payload['deviceModel'],
            device_enabled=bool(payload.get('deviceEnabled', True)),
            online=bool(payload.get('online', False)),
            host=payload['host'],
            port=port,
            username=payload['username'],
            status=status,
            binding_version=str(payload['bindingVersion']),
            last_seen_at=last_seen_at,
            updated_at=datetime.now().astimezone(),
            serial_number=serial_number or None,
            firmware_version=firmware_version or None,
            public_ssh_port=22,
        )
        try:
            saved = await self.ssh_bindings.upsert(binding)
        except IntegrityError:
            return self.internal_error(
                request_id, 409, 'CONFLICT', 'SSH port is already bound to another device')
        except Exception:
            LOG.exception('upsert SSH binding failed')
            return self.internal_error(
                request_id, 503, 'SERVICE_UNAVAILABLE', 'SSH binding storage unavailable')
        return self.internal_json(request_id, saved.to_api())

    def registration_error(self, request_id, error):
        LOG.warning('SSH registration failed: %s', error)
        return self.internal_error(
            request_id, error.status, error.code, str(error))

    async def internal_robot_ssh_binding(self, req):
        request_id = self.internal_request_id(req)
        device_id = self.clean_device_id(req.match_info.get('device_id'))
        try:
            binding = await self.ssh_bindings.get_by_device(device_id)
        except Exception:
            LOG.exception('get SSH binding by device failed')
            return self.internal_error(
                request_id, 503, 'SERVICE_UNAVAILABLE', 'SSH binding storage unavailable')
        if binding is None:
            return self.internal_error(request_id, 404, 'NOT_FOUND', 'SSH binding not found')
        return self.internal_json(request_id, binding.to_api())

    async def internal_robot_register(self, req):
        request_id = self.internal_request_id(req)
        device_id = self.clean_device_id(req.match_info.get('device_id'))
        if not device_id:
            return self.internal_error(request_id, 400, 'INVALID_ARGUMENT', 'invalid deviceId')
        try:
            payload = await req.json()
        except Exception:
            return self.internal_error(request_id, 400, 'INVALID_ARGUMENT', 'invalid JSON request')
        if not isinstance(payload, dict):
            return self.internal_error(request_id, 400, 'INVALID_ARGUMENT', 'JSON object required')
        if payload.get('deviceId', device_id) != device_id:
            return self.internal_error(request_id, 400, 'INVALID_ARGUMENT', 'deviceId mismatch')
        required = ('deviceName', 'deviceModel')
        missing = [key for key in required if not isinstance(payload.get(key), str) or not payload[key]]
        if missing:
            return self.internal_error(
                request_id, 400, 'INVALID_ARGUMENT', f'missing fields: {", ".join(missing)}')
        normalized = {
            'device_name': payload['deviceName'],
            'device_model': payload['deviceModel'],
            'serial_number': payload.get('serialNumber') or None,
            'firmware_version': payload.get('firmwareVersion') or None,
            'device_enabled': bool(payload.get('deviceEnabled', True)),
            'username': payload.get('username') or payload.get('sshUser') or 'robot',
        }
        try:
            result = await self.ssh_registration.register(device_id, normalized)
        except SSHRegistrationError as error:
            return self.registration_error(request_id, error)
        except IntegrityError:
            return self.internal_error(
                request_id, 409, 'CONFLICT', 'device or port already belongs to another robot')
        except Exception:
            LOG.exception('register robot SSH failed')
            return self.internal_error(
                request_id, 503, 'SERVICE_UNAVAILABLE', 'SSH registration unavailable')
        return self.internal_json(request_id, result)

    async def internal_robot_heartbeat(self, req):
        request_id = self.internal_request_id(req)
        device_id = self.clean_device_id(req.match_info.get('device_id'))
        try:
            result = await self.ssh_registration.heartbeat(device_id)
        except SSHRegistrationError as error:
            return self.registration_error(request_id, error)
        except Exception:
            LOG.exception('robot SSH heartbeat failed')
            return self.internal_error(
                request_id, 503, 'SERVICE_UNAVAILABLE', 'SSH registration unavailable')
        return self.internal_json(request_id, result)

    async def internal_robot_offline(self, req):
        request_id = self.internal_request_id(req)
        device_id = self.clean_device_id(req.match_info.get('device_id'))
        try:
            result = await self.ssh_registration.offline(device_id)
        except SSHRegistrationError as error:
            return self.registration_error(request_id, error)
        except Exception:
            LOG.exception('robot SSH offline update failed')
            return self.internal_error(
                request_id, 503, 'SERVICE_UNAVAILABLE', 'SSH registration unavailable')
        return self.internal_json(request_id, result)

    async def internal_robot_unbind(self, req):
        request_id = self.internal_request_id(req)
        device_id = self.clean_device_id(req.match_info.get('device_id'))
        try:
            payload = await req.json()
        except Exception:
            return self.internal_error(request_id, 400, 'INVALID_ARGUMENT', 'manual JSON request required')
        if not isinstance(payload, dict) or payload.get('manual') is not True:
            return self.internal_error(
                request_id, 400, 'INVALID_ARGUMENT', 'manual=true is required')
        operator = payload.get('operator')
        if not isinstance(operator, str) or not operator:
            return self.internal_error(request_id, 400, 'INVALID_ARGUMENT', 'operator is required')
        try:
            result = await self.ssh_registration.unbind(device_id, operator)
        except SSHRegistrationError as error:
            return self.registration_error(request_id, error)
        except Exception:
            LOG.exception('manual robot SSH unbind failed')
            return self.internal_error(
                request_id, 503, 'SERVICE_UNAVAILABLE', 'SSH unbind unavailable')
        return self.internal_json(request_id, result)

    # ---------- HTTP ----------
    async def index(self, _req):
        return web.FileResponse(self.static_dir / 'index.html')

    async def fleet(self, _req):
        return web.FileResponse(self.static_dir / 'fleet' / 'index.html')

    async def static_file(self, req):
        rel = req.match_info.get('path', '')
        path = (self.static_dir / rel).resolve()
        if path != self.static_dir and self.static_dir not in path.parents:
            raise web.HTTPNotFound()
        if path.is_file():
            return web.FileResponse(path)
        raise web.HTTPNotFound()

    def robot_summary(self, robot_id):
        session = self.robots.get(robot_id)
        record = self.robot_records.get(robot_id, {})
        online = session is not None and not session.ws.closed
        last_seen_at = record.get('last_seen_at')
        last_seen_seconds = (
            max(0.0, time.time() - last_seen_at)
            if last_seen_at else None
        )
        task = record.get('task')
        return {
            'robot_id': robot_id,
            'name': record.get('name') or robot_id,
            'online': online,
            'last_seen_seconds': round(last_seen_seconds, 1) if last_seen_seconds is not None else None,
            'task': task.get('status') if isinstance(task, dict) else None,
            'task_type': task.get('type') if isinstance(task, dict) else None,
            'battery': record.get('battery'),
            'video_upload_enabled': self.video_upload_enabled(robot_id),
            'browsers': len(self.clients_for(robot_id)),
            'camera_source': bool(self.camera_clients_for(robot_id)),
            'rtc_publisher': self.rtc_publisher_states.get(robot_id, {'state': 'unknown'}),
        }

    def robot_summaries(self):
        robot_ids = sorted(set(self.robot_records) | set(self.robots))
        return [self.robot_summary(robot_id) for robot_id in robot_ids]

    async def robots_api(self, _req):
        robots = self.robot_summaries()
        return web.json_response({
            'robots': robots,
            'total': len(robots),
            'online': sum(1 for robot in robots if robot['online']),
        }, headers={'Access-Control-Allow-Origin': '*'})

    async def robot_camera_api(self, req):
        robot_id = self.clean_robot_id(req.match_info.get('robot_id'))
        if robot_id not in self.robot_records:
            raise web.HTTPNotFound(text='robot not found')
        if req.method == 'POST':
            try:
                data = await req.json()
            except ValueError:
                raise web.HTTPBadRequest(text='invalid JSON')
            if not isinstance(data, dict) or not isinstance(data.get('enabled'), bool):
                raise web.HTTPBadRequest(text='enabled must be boolean')
            record = self.robot_records.setdefault(robot_id, {'robot_id': robot_id})
            record['video_upload_enabled'] = data['enabled']
            self.sync_camera(robot_id)
            self.broadcast_text(robot_id, json.dumps({
                't': 'camera_upload',
                'robot_id': robot_id,
                'enabled': data['enabled'],
            }, ensure_ascii=False, separators=(',', ':')))
        return web.json_response(
            self.robot_summary(robot_id),
            headers={'Access-Control-Allow-Origin': '*'},
        )

    async def health(self, _req):
        robots = self.robot_summaries()
        online_robots = [robot for robot in robots if robot['online']]
        primary = self.robots.get(self.default_robot_id)
        last_seen_seconds = None
        if primary is not None and primary.last_seen:
            last_seen_seconds = round(time.monotonic() - primary.last_seen, 1)
        return web.json_response({
            'ok': True,
            'robot_online': bool(online_robots),
            'robot_id': self.default_robot_id if primary is not None else (
                online_robots[0]['robot_id'] if online_robots else self.default_robot_id
            ),
            'last_seen_seconds': last_seen_seconds,
            'browsers': len(self.clients),
            'camera_sources': len(self.camera_clients),
            'robots': robots,
        })

    async def rtc_config(self, req):
        if not self.rtc_app_id or not self.rtc_app_key:
            raise web.HTTPServiceUnavailable(text='RTC credentials are not configured')
        role = req.query.get('role', 'viewer')
        robot_id = self.requested_robot_id(req)
        publisher_id = f'{robot_id}-camera'
        user_id = publisher_id if role == 'robot' else f'browser-{uuid.uuid4().hex[:12]}'
        expire_at = int(time.time()) + self.rtc_ttl
        room_id = self.rtc_room_id if robot_id == self.default_robot_id else robot_id
        token = AccessToken(self.rtc_app_id, self.rtc_app_key, room_id, user_id)
        token.expire_time(expire_at)
        if role == 'robot':
            token.add_privilege(PRIV_PUBLISH_STREAM, expire_at)
        token.add_privilege(PRIV_SUBSCRIBE_STREAM, expire_at)
        return web.json_response({
            'app_id': self.rtc_app_id,
            'room_id': room_id,
            'robot_id': robot_id,
            'user_id': user_id,
            'token': token.serialize(),
            'publisher_id': publisher_id,
            'role': role,
            'expires_at': expire_at,
        }, headers={'Access-Control-Allow-Origin': '*'})

    async def rtc_publisher_status(self, req):
        robot_id = self.requested_robot_id(req)
        if req.method == 'POST':
            try:
                data = json.loads(await req.text())
            except (TypeError, ValueError):
                raise web.HTTPBadRequest(text='invalid publisher status')
            if isinstance(data, dict):
                state = {
                    key: value for key, value in data.items()
                    if isinstance(key, str) and len(str(value)) < 2000
                }
                state['robot_id'] = robot_id
                state['updated_at'] = int(time.time())
                self.rtc_publisher_states[robot_id] = state
        state = self.rtc_publisher_states.get(robot_id, {'robot_id': robot_id, 'state': 'unknown'})
        return web.json_response(state, headers={'Access-Control-Allow-Origin': '*'})

    async def cleanup(self, _app):
        await self.ssh_registration.close()

    def app(self):
        @web.middleware
        async def no_cache(req, handler):
            response = await handler(req)
            response.headers['Cache-Control'] = 'no-store'
            return response

        app = web.Application(middlewares=[no_cache])
        app.on_cleanup.append(self.cleanup)
        app.router.add_get('/health', self.health)
        app.router.add_get('/quadruped/health', self.health)
        app.router.add_get('/internal/v1/ssh-bindings', self.internal_ssh_bindings)
        app.router.add_get(
            '/internal/v1/ssh-bindings/by-port/{port}', self.internal_ssh_binding_by_port)
        app.router.add_put(
            '/internal/v1/ssh-bindings/{device_id}', self.internal_ssh_binding_upsert)
        app.router.add_get(
            '/internal/v1/robots/{device_id}/ssh-binding', self.internal_robot_ssh_binding)
        app.router.add_post(
            '/internal/v1/robots/{device_id}/register', self.internal_robot_register)
        app.router.add_post(
            '/internal/v1/robots/{device_id}/heartbeat', self.internal_robot_heartbeat)
        app.router.add_post(
            '/internal/v1/robots/{device_id}/offline', self.internal_robot_offline)
        app.router.add_delete(
            '/internal/v1/robots/{device_id}/ssh-binding', self.internal_robot_unbind)
        app.router.add_get('/quadruped/api/robots', self.robots_api)
        app.router.add_route(
            '*', '/quadruped/api/robots/{robot_id}/camera', self.robot_camera_api)
        app.router.add_get('/quadruped/rtc/config', self.rtc_config)
        app.router.add_route('*', '/quadruped/rtc/publisher-status', self.rtc_publisher_status)
        app.router.add_get('/robot/ws', self.handle_robot)
        app.router.add_get('/quadruped/robot/ws', self.handle_robot)
        app.router.add_get('/quadruped/ws', self.browser_ws)
        app.router.add_get('/quadruped/camera/ws', self.camera_source_ws)
        app.router.add_get('/ws', self.browser_ws)
        app.router.add_get('/quadruped', lambda _r: web.HTTPPermanentRedirect('/quadruped/'))
        app.router.add_get('/quadruped/fleet', lambda _r: web.HTTPPermanentRedirect('/quadruped/fleet/'))
        app.router.add_get('/quadruped/fleet/', self.fleet)
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
        default_robot_id=os.environ.get('GO2W_DEFAULT_ROBOT_ID'),
    )
    web.run_app(gateway.app(), host=args.host, port=args.port,
                print=None, handle_signals=True, shutdown_timeout=3.0)


if __name__ == '__main__':
    main()
