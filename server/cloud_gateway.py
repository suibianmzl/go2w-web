#!/usr/bin/env python3
"""Cloud WebSocket gateway for a remotely connected Go2W robot.

This process has no ROS dependency. It serves the console and relays the
existing browser protocol between browser sessions and multiple robot gateways.
"""
import argparse
import asyncio
import hmac
import json
import logging
import os
import re
import secrets
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from aiohttp import WSMsgType, web
from pymysql.err import IntegrityError

from archive import ArchiveSpool, ArchiveStore, iso_utc as archive_iso_utc, utc_now as archive_utc_now
from rtc_token import AccessToken, PRIV_PUBLISH_STREAM, PRIV_SUBSCRIBE_STREAM
from ssh_bindings import SSHBinding, SSHBindingStore, parse_time
from ssh_portbroker import PortBrokerClient
from ssh_registration import SSHRegistrationError, SSHRegistrationService


T_MAP, T_SCAN, T_GCOST, T_LCOST, T_CLOUD, T_PART, T_CAM = range(1, 8)
LAYER_KINDS = {T_GCOST: 'gcost', T_LCOST: 'lcost', T_CLOUD: 'cloud', T_PART: 'particles'}
BINARY_NAMES = {T_MAP: 'map', T_SCAN: 'scan', **LAYER_KINDS}
CACHE_TEXT = {'state', 'pose', 'task', 'routes', 'map_views', 'plan', 'inspection_exceptions'}
TEXT_QUEUE_MAX = 500
ROBOT_QUEUE_MAX = 500
LOG = logging.getLogger('go2w_cloud')


@dataclass(eq=False)
class Browser:
    ws: web.WebSocketResponse
    number: int
    robot_id: str
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex)
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
    channel: str = 'control'
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(ROBOT_QUEUE_MAX))
    tx: asyncio.Task | None = None
    last_seen: float = 0.0
    text_cache: dict = field(default_factory=dict)
    binary_cache: dict = field(default_factory=dict)
    ack_routes: dict = field(default_factory=dict)
    active_topics: set = field(default_factory=set)
    owner: Browser | None = None
    connected_at: datetime = field(default_factory=archive_utc_now)
    remote_info: str | None = None
    edge_version: str | None = None
    message_seq: int = 0


@dataclass
class PendingCommand:
    client: Browser | None
    original_id: object
    command_id: str
    browser_session_id: str


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
        archive_spool=None,
        archive_store=None,
        access_token=None,
        auth_session_ttl=12 * 60 * 60,
    ):
        self.static_dir = Path(static_dir).resolve()
        self.access_token = str(access_token or '').strip()
        self.auth_session_ttl = max(60, int(auth_session_ttl))
        self.auth_sessions = {}
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
        self.archive_spool = archive_spool or ArchiveSpool.from_env()
        self.archive_enabled = self.archive_spool.enabled
        if archive_store is not None:
            self.archive_store = archive_store
        elif self.archive_enabled:
            self.archive_store = ArchiveStore.from_env()
        else:
            self.archive_store = ArchiveStore(None)
        self.clients = {}
        self.camera_clients = {}
        self.client_seq = 0
        self.robots = {}
        self.robot_sessions = {}
        self.robot_video_sessions = {}
        self.robot_records = {}
        self.rtc_publisher_states = {}
        self.command_seq = 0
        self.incidents = {}
        self.incident_tasks = set()
        self.snapshot_requests = {}
        self.snapshot_dir = self.archive_spool.root / 'snapshots'
        if self.archive_enabled:
            self.snapshot_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def clean_robot_id(value):
        return re.sub(r'[^A-Za-z0-9_-]', '', str(value or ''))[:32]

    @staticmethod
    def clean_device_id(value):
        return re.sub(r'[^A-Za-z0-9_.-]', '', str(value or ''))[:128]

    @staticmethod
    def safe_auth_next(value):
        target = str(value or '').strip()
        if (
            not target.startswith('/')
            or target.startswith('//')
            or '\\' in target
            or any(ord(char) < 32 for char in target)
        ):
            return '/quadruped/fleet/'
        return target

    @staticmethod
    def normalize_access_token(value):
        token = str(value or '').strip()
        for prefix in ('GO2W_ACCESS_TOKEN=', '云端Web访问Token：', '云端Web访问Token:'):
            if token.startswith(prefix):
                token = token[len(prefix):].strip()
                break
        return token.strip().strip('"\'')

    @staticmethod
    def request_uses_https(req):
        forwarded = req.headers.get('X-Forwarded-Proto', '').split(',', 1)[0].strip().lower()
        return req.secure or forwarded == 'https'

    def auth_enabled(self):
        return bool(self.access_token)

    def purge_auth_sessions(self):
        now = time.time()
        for session_id, expires_at in list(self.auth_sessions.items()):
            if expires_at <= now:
                self.auth_sessions.pop(session_id, None)

    def authenticated(self, req):
        if not self.auth_enabled():
            return True
        session_id = req.cookies.get('go2w_session', '')
        if not session_id:
            return False
        expires_at = self.auth_sessions.get(session_id)
        if expires_at is None:
            return False
        if expires_at <= time.time():
            self.auth_sessions.pop(session_id, None)
            return False
        return True

    @staticmethod
    def is_machine_route(req):
        path = req.path
        if path in {
            '/health',
            '/quadruped/health',
            '/robot/ws',
            '/quadruped/robot/ws',
            '/quadruped/robot/video/ws',
            '/quadruped/camera/ws',
            '/quadruped/rtc/publisher-status',
            '/login',
            '/quadruped/login',
            '/auth/login',
            '/auth/logout',
            '/favicon.ico',
        }:
            return True
        if path.startswith('/internal/'):
            return True
        return path == '/quadruped/rtc/config' and req.query.get('role') == 'robot'

    @staticmethod
    def is_page_request(req):
        if req.method not in {'GET', 'HEAD'}:
            return False
        if req.path in {
            '/',
            '/quadruped',
            '/quadruped/',
            '/quadruped/fleet',
            '/quadruped/fleet/',
            '/quadruped/inspection',
            '/quadruped/inspection/',
        }:
            return True
        return 'text/html' in req.headers.get('Accept', '').lower()

    @web.middleware
    async def auth_middleware(self, req, handler):
        if self.is_machine_route(req) or self.authenticated(req):
            return await handler(req)
        if self.is_page_request(req):
            next_path = quote(req.raw_path or '/', safe='')
            return web.HTTPFound(f'/login?next={next_path}')
        return web.Response(
            status=401,
            text='authentication required',
            headers={'WWW-Authenticate': 'Token'},
        )

    def requested_robot_id(self, req):
        return self.clean_robot_id(req.query.get('robot_id')) or self.default_robot_id

    def clients_for(self, robot_id):
        return [client for client in self.clients.values() if client.robot_id == robot_id]

    def camera_clients_for(self, robot_id):
        return [client for client in self.camera_clients.values() if client.robot_id == robot_id]

    def video_upload_enabled(self, robot_id):
        record = self.robot_records.get(robot_id, {})
        return record.get('video_upload_enabled', True) is not False

    # ---------- archive capture ----------
    def archive_seq(self, session):
        session.message_seq += 1
        return session.message_seq

    @staticmethod
    def archive_task_id(payload):
        if not isinstance(payload, dict):
            return None
        task = payload.get('task')
        if isinstance(task, dict):
            value = task.get('routeId') or task.get('task_id')
            if value:
                return str(value)
        value = payload.get('route_id') or payload.get('task_id')
        return str(value) if value else None

    def archive_up_text(self, session, raw, payload, message_type):
        if not self.archive_enabled or not session.robot_id:
            return
        try:
            self.archive_spool.append_text(
                robot_id=session.robot_id,
                session_id=session.session_id,
                direction='up',
                message_type=message_type,
                message_seq=self.archive_seq(session),
                task_id=self.archive_task_id(payload),
                payload=payload if isinstance(payload, dict) else raw,
            )
        except Exception:
            LOG.exception('archive spool write failed for robot %s', session.robot_id)

    def archive_down_text(self, client, session, payload, message_type,
                          command_id=None):
        if not self.archive_enabled:
            return
        try:
            self.archive_spool.append_text(
                robot_id=client.robot_id,
                session_id=session.session_id,
                direction='down',
                message_type=message_type,
                message_seq=self.archive_seq(session),
                browser_session_id=client.session_id,
                command_id=command_id,
                task_id=self.archive_task_id(payload),
                payload=payload,
            )
        except Exception:
            LOG.exception('archive spool write failed for command %s', command_id)

    def archive_system_text(self, session, payload, message_type, command_id, reason):
        if not self.archive_enabled:
            return
        try:
            self.archive_spool.append_text(
                robot_id=session.robot_id,
                session_id=session.session_id,
                direction='down',
                message_type=message_type,
                message_seq=self.archive_seq(session),
                browser_session_id='system',
                command_id=command_id,
                task_id=self.archive_task_id(payload),
                payload=payload,
            )
        except Exception:
            LOG.exception('archive spool write failed for system command %s', command_id)
        LOG.info('archived system command %s reason=%s', command_id, reason)

    def archive_up_binary(self, session, kind, data):
        if not self.archive_enabled or not session.robot_id:
            return
        try:
            self.archive_spool.append_binary(
                robot_id=session.robot_id,
                session_id=session.session_id,
                kind=BINARY_NAMES.get(kind, f'kind-{kind}'),
                data=data,
                message_seq=self.archive_seq(session),
            )
        except Exception:
            LOG.exception('archive binary spool write failed for robot %s', session.robot_id)

    async def archive_session_started(self, session):
        if not self.archive_enabled or not session.robot_id:
            return
        try:
            await self.archive_store.session_started(
                session.robot_id,
                session.session_id,
                session.connected_at,
                remote_info=session.remote_info,
                edge_version=session.edge_version,
            )
        except Exception:
            LOG.exception('archive session start failed for %s', session.robot_id)

    async def archive_session_ended(self, session, status='closed'):
        if not self.archive_enabled or not session.robot_id:
            return
        self.archive_spool.close_session(session.session_id)
        try:
            await self.archive_store.session_ended(
                session.session_id, archive_utc_now(), status)
        except Exception:
            LOG.exception('archive session end failed for %s', session.robot_id)

    async def archive_command_issued(self, record):
        if not self.archive_enabled:
            return
        try:
            await self.archive_store.command_issued(record)
        except Exception:
            LOG.exception('archive command issue failed: %s', record.get('command_id'))

    async def archive_command_sent(self, command_id):
        if not self.archive_enabled:
            return
        try:
            await self.archive_store.command_sent(command_id, archive_utc_now())
        except Exception:
            LOG.exception('archive command sent failed: %s', command_id)

    async def archive_command_ack(self, command_id, message, response):
        if not self.archive_enabled:
            return
        try:
            await self.archive_store.command_ack(
                command_id,
                bool(message.get('ok')),
                message.get('msg') or '',
                response,
                archive_utc_now(),
            )
        except Exception:
            LOG.exception('archive command ack failed: %s', command_id)

    async def archive_command_failed(self, command_id, status, message):
        if not self.archive_enabled:
            return
        try:
            await self.archive_store.command_failed(command_id, status, message)
        except Exception:
            LOG.exception('archive command failure update failed: %s', command_id)

    def archive_status(self):
        status = self.archive_spool.status()
        status['mysql_configured'] = self.archive_store.configured
        return status

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
        for key in ('state', 'pose', 'task', 'routes', 'map_views', 'plan', 'inspection_exceptions'):
            if key in session.text_cache:
                self.send_browser(client, session.text_cache[key], key=f't{key}')
        for kind in (T_MAP, T_SCAN):
            if kind in session.binary_cache:
                self.send_browser(client, session.binary_cache[kind], key=f'b{kind}')
        for kind, layer in LAYER_KINDS.items():
            if layer in client.layers and kind in session.binary_cache:
                self.send_browser(client, session.binary_cache[kind], key=f'b{kind}')
        incidents = self.incidents.get(client.robot_id)
        if incidents:
            self.send_browser(client, json.dumps({
                't': 'incidents', 'items': incidents[:50],
            }, ensure_ascii=False, separators=(',', ':')), key='incidents')

    def archive_background(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.incident_tasks.add(task)
        task.add_done_callback(self.incident_tasks.discard)

    @staticmethod
    def normalize_incident(item, robot_id):
        if not isinstance(item, dict) or item.get('id') is None or item.get('ts') is None:
            return None
        try:
            started = int(item['ts'])
        except (TypeError, ValueError):
            return None
        normalized = dict(item)
        normalized['id'] = str(item['id'])
        normalized['ts'] = started
        normalized['robot'] = str(item.get('robot') or robot_id)
        normalized['type'] = str(item.get('type') or 'unknown')
        normalized['label'] = str(item.get('label') or normalized['type'])
        normalized['level'] = str(item.get('level') or 'warn')
        return normalized

    def update_incidents(self, robot_id, items, replace=False):
        current = [] if replace else list(self.incidents.get(robot_id, []))
        by_id = {str(item.get('id')): item for item in current if item.get('id')}
        changed = []
        for raw in items:
            item = self.normalize_incident(raw, robot_id)
            if item is None:
                continue
            existing = by_id.get(item['id'])
            if existing and existing.get('snapshot') and not item.get('snapshot'):
                item['snapshot'] = existing['snapshot']
            by_id[item['id']] = item
            changed.append(item)
        merged = sorted(by_id.values(), key=lambda item: int(item['ts']), reverse=True)[:1000]
        self.incidents[robot_id] = merged
        for item in changed:
            if not item.get('snapshot'):
                requests = self.snapshot_requests.setdefault(robot_id, {})
                requests.setdefault(item['id'], archive_iso_utc())
        if changed and self.archive_enabled:
            self.archive_background(self.persist_incidents(robot_id, changed))
        return changed

    async def persist_incidents(self, robot_id, items):
        try:
            for item in items:
                await self.archive_store.upsert_incident(robot_id, item)
        except Exception:
            LOG.exception('persist incidents failed for robot %s', robot_id)

    def robot_status(self, robot_id):
        session = self.robots.get(robot_id)
        return {
            't': 'robot_status',
            'online': session is not None and not session.ws.closed,
            'robot_id': robot_id,
            'video_link': bool(
                self.robot_video_sessions.get(robot_id)
                and not self.robot_video_sessions[robot_id].ws.closed
            ),
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

    def send_robot_video(self, robot_id, data):
        session = self.robot_video_sessions.get(robot_id)
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
            if session.channel == 'control':
                record['online'] = True
                record['last_seen_at'] = time.time()
            else:
                record['video_last_seen_at'] = time.time()
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

    async def register_robot_video(self, session, robot_id):
        session.robot_id = robot_id
        old = self.robot_video_sessions.get(robot_id)
        self.robot_video_sessions[robot_id] = session
        self.touch_robot(session)
        if old is not None and old is not session and not old.ws.closed:
            await old.ws.close(code=4000, message=b'replaced by a new video connection')
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
        for key, pending in list(session.ack_routes.items()):
            await self.archive_command_failed(
                pending.command_id, 'disconnected', '机器狗连接已断开')
            if pending.client is not None:
                self.send_browser(pending.client, json.dumps({
                    't': 'ack', 'id': pending.original_id, 'ok': False,
                    'msg': '机器狗连接已断开',
                }, ensure_ascii=False, separators=(',', ':')))
        session.ack_routes.clear()
        if session.tx:
            session.tx.cancel()
            await asyncio.gather(session.tx, return_exceptions=True)
            session.tx = None
        if robot_id:
            await self.archive_session_ended(session)
            self.broadcast_robot_status(robot_id)

    async def clear_robot_video(self, session):
        self.robot_sessions.pop(session.ws, None)
        robot_id = session.robot_id
        if robot_id and self.robot_video_sessions.get(robot_id) is session:
            del self.robot_video_sessions[robot_id]
            record = self.robot_records.setdefault(robot_id, {'robot_id': robot_id})
            record['video_last_seen_at'] = time.time()
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
        message = json.dumps({
            't': 'cam',
            'on': self.video_upload_enabled(robot_id) and bool(self.camera_clients_for(robot_id)),
        }, separators=(',', ':'))
        video_session = self.robot_video_sessions.get(robot_id)
        if video_session is not None and not video_session.ws.closed:
            self.send_robot_video(robot_id, message)
        else:
            self.send_robot(robot_id, message)

    # ---------- robot messages ----------
    async def on_robot_text(self, session, data):
        self.touch_robot(session)
        try:
            msg = json.loads(data)
        except ValueError:
            if session.robot_id:
                self.archive_up_text(session, data, data, 'raw')
                self.broadcast_text(session.robot_id, data)
            return
        if msg.get('type') == 'hello':
            robot_id = self.clean_robot_id(msg.get('robot_id'))
            if not robot_id:
                LOG.warning('robot hello without a valid robot_id')
                return
            record = self.robot_records.setdefault(robot_id, {'robot_id': robot_id})
            record['name'] = msg.get('name') or record.get('name') or robot_id
            if session.channel == 'video':
                await self.register_robot_video(session, robot_id)
                session.edge_version = (
                    msg.get('edge_version') or msg.get('version') or msg.get('protocol')
                )
                return
            await self.register_robot(session, robot_id)
            session.edge_version = (
                msg.get('edge_version') or msg.get('version') or msg.get('protocol')
            )
            await self.archive_session_started(session)
            self.archive_up_text(session, data, msg, 'hello')
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
        if session.channel == 'video':
            return
        msg_type = msg.get('t') or 'unknown'
        self.archive_up_text(session, data, msg, msg_type)
        if msg_type == 'incidents':
            items = msg.get('items') if isinstance(msg.get('items'), list) else []
            self.update_incidents(robot_id, items, replace=True)
        elif msg_type == 'incident':
            item = msg.get('item')
            self.update_incidents(robot_id, [item] if isinstance(item, dict) else [])
        if msg_type == 'ack':
            key = str(msg.get('id'))
            route = session.ack_routes.pop(key, None)
            if route is not None:
                await self.archive_command_ack(
                    route.command_id, msg, dict(msg))
                if msg.get('id') is not None:
                    msg['id'] = route.original_id
                if route.client is not None:
                    self.send_browser(
                        route.client,
                        json.dumps(msg, ensure_ascii=False, separators=(',', ':')),
                    )
                return
        if msg_type == 'msg':
            topic = msg.get('topic')
            for client in self.clients_for(robot_id):
                if topic in client.topics:
                    self.send_browser(client, data)
            return
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
        if session.channel == 'video':
            if kind != T_CAM or not self.video_upload_enabled(robot_id):
                return
            for client in self.camera_clients_for(robot_id):
                self.send_browser(client, data)
            return
        if kind == T_CAM:
            if not self.video_upload_enabled(robot_id):
                return
            for client in self.camera_clients_for(robot_id):
                self.send_browser(client, data)
            return
        self.archive_up_binary(session, kind, data)
        if kind not in LAYER_KINDS and kind not in (T_MAP, T_SCAN):
            return
        session.binary_cache[kind] = data
        self.broadcast_binary(robot_id, data, kind)

    async def handle_robot(self, req):
        return await self.handle_robot_channel(req, 'control')

    async def handle_robot_video(self, req):
        return await self.handle_robot_channel(req, 'video')

    async def handle_robot_channel(self, req, channel):
        ws = web.WebSocketResponse(max_msg_size=16 * 1024 * 1024, heartbeat=20)
        await ws.prepare(req)
        session = RobotSession(ws, channel=channel)
        session.remote_info = req.remote
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
            if channel == 'video':
                await self.clear_robot_video(session)
            else:
                await self.clear_robot(session)
        return ws

    # ---------- browser messages ----------
    async def send_system_command(self, session, op, extra, reason):
        command_id = uuid.uuid4().hex
        upstream_id = f'cloud-{self.command_seq}'
        self.command_seq += 1
        message = {
            't': 'cmd',
            'id': upstream_id,
            'op': op,
            **extra,
            'robot_id': session.robot_id,
        }
        self.archive_system_text(session, message, 'cmd', command_id, reason)
        issued_at = archive_utc_now()
        await self.archive_command_issued({
            'command_id': command_id,
            'robot_id': session.robot_id,
            'session_id': session.session_id,
            'browser_session': 'system',
            'request_id': None,
            'upstream_id': upstream_id,
            'operation': op,
            'task_id': self.archive_task_id(message),
            'payload': message,
            'issued_at': issued_at,
        })
        session.ack_routes[str(upstream_id)] = PendingCommand(
            None, None, command_id, 'system')
        sent = self.send_robot(
            session.robot_id,
            json.dumps(message, ensure_ascii=False, separators=(',', ':')),
        )
        if sent:
            await self.archive_command_sent(command_id)
        else:
            session.ack_routes.pop(str(upstream_id), None)
            await self.archive_command_failed(
                command_id, 'failed', '机器人指令队列已满或连接已断开')
        return sent

    async def stop_on_last_browser_loss(self, session):
        if session is None or session.ws.closed:
            return
        await self.send_system_command(
            session, 'cancel', {}, '最后一个控制页面断开，自动取消任务')
        await self.send_system_command(
            session, 'arm', {'on': False}, '最后一个控制页面断开，自动锁定底盘')

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
            if msg_type == 'cmd':
                original_id = msg.get('id')
                command_id = uuid.uuid4().hex
                upstream_id = f'cloud-{self.command_seq}'
                self.command_seq += 1
                msg['id'] = upstream_id
                msg['robot_id'] = client.robot_id
                data = json.dumps(msg, ensure_ascii=False, separators=(',', ':'))
                self.archive_down_text(
                    client, session, msg, 'cmd', command_id=command_id)
                await self.archive_command_issued({
                    'command_id': command_id,
                    'robot_id': client.robot_id,
                    'session_id': session.session_id,
                    'browser_session': client.session_id,
                    'request_id': original_id,
                    'upstream_id': upstream_id,
                    'operation': msg.get('op') or 'unknown',
                    'task_id': self.archive_task_id(msg),
                    'payload': msg,
                    'issued_at': archive_utc_now(),
                })
                session.ack_routes[str(upstream_id)] = PendingCommand(
                    client, original_id, command_id, client.session_id)
            elif msg_type == 'teleop':
                self.archive_down_text(
                    client, session, msg, 'teleop')
            sent = self.send_robot(client.robot_id, data)
            if msg_type == 'cmd':
                if sent:
                    await self.archive_command_sent(command_id)
                else:
                    session.ack_routes.pop(str(upstream_id), None)
                    await self.archive_command_failed(
                        command_id, 'failed', '机器人指令队列已满或连接已断开')
                    self.send_browser(client, json.dumps({
                        't': 'ack', 'id': original_id, 'ok': False,
                        'msg': '机器狗未连接',
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
            remaining_clients = self.clients_for(robot_id)
            if session is not None:
                for key, pending in list(session.ack_routes.items()):
                    if pending.client is client:
                        session.ack_routes.pop(key, None)
                        await self.archive_command_failed(
                            pending.command_id, 'abandoned', '控制方网页已断开')
                self.sync_layers(robot_id)
                self.sync_topics(robot_id)
                if not remaining_clients:
                    session.owner = None
                    await self.stop_on_last_browser_loss(session)
                elif session.owner is client:
                    session.owner = remaining_clients[0]
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

    # ---------- authentication ----------
    async def login_page(self, req):
        if self.authenticated(req):
            return web.HTTPFound(self.safe_auth_next(req.query.get('next')))
        return web.FileResponse(self.static_dir / 'login.html')

    async def auth_login(self, req):
        if not self.auth_enabled():
            return web.json_response({'ok': True, 'next': '/quadruped/fleet/'})
        try:
            payload = await req.json()
        except (TypeError, ValueError):
            raise web.HTTPBadRequest(text='invalid JSON')
        token = self.normalize_access_token(
            payload.get('token') if isinstance(payload, dict) else ''
        )
        configured = self.normalize_access_token(self.access_token)
        if (
            not token
            or not hmac.compare_digest(
                token.encode('utf-8'),
                configured.encode('utf-8'),
            )
        ):
            await asyncio.sleep(0.2)
            return web.json_response(
                {'ok': False, 'message': 'Token 无效'},
                status=401,
            )
        self.purge_auth_sessions()
        session_id = secrets.token_urlsafe(32)
        expires_at = time.time() + self.auth_session_ttl
        self.auth_sessions[session_id] = expires_at
        response = web.json_response({
            'ok': True,
            'next': self.safe_auth_next((payload or {}).get('next')),
        })
        response.set_cookie(
            'go2w_session',
            session_id,
            max_age=self.auth_session_ttl,
            path='/',
            httponly=True,
            secure=self.request_uses_https(req),
            samesite='Lax',
        )
        return response

    async def auth_logout(self, req):
        session_id = req.cookies.get('go2w_session', '')
        if session_id:
            self.auth_sessions.pop(session_id, None)
        response = web.json_response({'ok': True})
        response.del_cookie('go2w_session', path='/')
        return response

    # ---------- HTTP ----------
    async def index(self, _req):
        return web.FileResponse(self.static_dir / 'index.html')

    async def fleet(self, _req):
        return web.FileResponse(self.static_dir / 'fleet' / 'index.html')

    async def inspection(self, _req):
        return web.FileResponse(self.static_dir / 'inspection' / 'index.html')

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
        video_session = self.robot_video_sessions.get(robot_id)
        record = self.robot_records.get(robot_id, {})
        online = session is not None and not session.ws.closed
        video_link = video_session is not None and not video_session.ws.closed
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
            'video_link': video_link,
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

    async def incidents_api(self, req):
        robot_id = self.requested_robot_id(req)
        try:
            limit = int(req.query.get('limit', '50'))
            if not 1 <= limit <= 500:
                raise ValueError
        except ValueError:
            raise web.HTTPBadRequest(text='limit must be between 1 and 500')
        before = req.query.get('before')
        if before is not None:
            try:
                before = int(before)
            except ValueError:
                raise web.HTTPBadRequest(text='before must be a millisecond timestamp')

        items = []
        if self.archive_enabled and self.archive_store.configured:
            try:
                items = await self.archive_store.list_incidents(
                    robot_id, limit, before)
            except Exception:
                LOG.exception('query incidents failed for robot %s', robot_id)
        if not items:
            items = [
                item for item in self.incidents.get(robot_id, [])
                if before is None or int(item.get('ts') or 0) < before
            ][:limit]
        return web.json_response({
            'items': items,
        }, headers={'Access-Control-Allow-Origin': '*'})

    def incident_snapshot_path(self, robot_id, incident_id):
        safe_robot = self.clean_robot_id(robot_id)
        safe_incident = re.sub(r'[^A-Za-z0-9_.-]', '-', str(incident_id))[:128]
        return self.snapshot_dir / safe_robot / f'{safe_incident}.jpg'

    async def incident_snapshot_upload(self, req):
        robot_id = self.clean_robot_id(req.match_info.get('robot_id'))
        incident_id = str(req.match_info.get('incident_id'))
        if not robot_id or not incident_id:
            raise web.HTTPBadRequest(text='robot_id and incident_id are required')
        length = req.content_length
        if length is not None and length > 8 * 1024 * 1024:
            raise web.HTTPRequestEntityTooLarge(max_size=8 * 1024 * 1024)
        data = await req.read()
        if len(data) < 4 or not data.startswith(b'\xff\xd8\xff'):
            raise web.HTTPBadRequest(text='JPEG snapshot required')
        path = self.incident_snapshot_path(robot_id, incident_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix('.jpg.tmp')
        temp.write_bytes(data)
        os.replace(temp, path)

        local_url = (
            f'/quadruped/api/incidents/{robot_id}/'
            f'{incident_id}/snapshot'
        )
        snapshot = {
            'state': 'saved',
            'local_url': local_url,
            'content_type': 'image/jpeg',
            'captured_at': archive_iso_utc(),
        }
        item = next(
            (entry for entry in self.incidents.get(robot_id, [])
             if str(entry.get('id')) == incident_id),
            None,
        )
        if item is not None:
            updated = {**item, 'snapshot': snapshot}
            self.incidents[robot_id] = [
                updated if str(entry.get('id')) == incident_id else entry
                for entry in self.incidents[robot_id]
            ]
            self.broadcast_text(robot_id, json.dumps({
                't': 'incident', 'item': updated,
            }, ensure_ascii=False, separators=(',', ':')))
            self.archive_background(self.persist_incidents(robot_id, [updated]))

        session = self.robots.get(robot_id)
        session_id = session.session_id if session is not None else f'snapshot-{incident_id}'
        try:
            self.archive_spool.append_snapshot(
                robot_id=robot_id,
                session_id=session_id,
                incident_id=incident_id,
                data=data,
            )
        except Exception:
            LOG.exception('archive snapshot spool failed: %s/%s', robot_id, incident_id)
        self.snapshot_requests.get(robot_id, {}).pop(incident_id, None)
        return web.json_response({'ok': True, 'url': local_url})

    async def incident_snapshot_get(self, req):
        robot_id = self.clean_robot_id(req.match_info.get('robot_id'))
        incident_id = str(req.match_info.get('incident_id'))
        path = self.incident_snapshot_path(robot_id, incident_id)
        if not path.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(path, headers={'Content-Type': 'image/jpeg'})

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
            'archive': self.archive_status(),
            'robots': robots,
        })

    async def archive_status_api(self, _req):
        return web.json_response(
            self.archive_status(),
            headers={'Access-Control-Allow-Origin': '*'},
        )

    async def rtc_config(self, req):
        if not self.rtc_app_id or not self.rtc_app_key:
            raise web.HTTPServiceUnavailable(text='RTC credentials are not configured')
        role = req.query.get('role', 'viewer')
        robot_id = self.requested_robot_id(req)
        publisher_id = f'{robot_id}-camera'
        user_id = publisher_id if role == 'robot' else f'browser-{uuid.uuid4().hex[:12]}'
        expire_at = int(time.time()) + self.rtc_ttl
        room_id = robot_id
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
        payload = dict(state)
        payload['snapshot_requests'] = [
            {'incident_id': incident_id, 'requested_at': requested_at}
            for incident_id, requested_at in self.snapshot_requests.get(robot_id, {}).items()
        ]
        return web.json_response(payload, headers={'Access-Control-Allow-Origin': '*'})

    async def cleanup(self, _app):
        self.archive_spool.close_all()
        if self.incident_tasks:
            await asyncio.gather(*list(self.incident_tasks), return_exceptions=True)
        await self.archive_store.close()
        await self.ssh_registration.close()

    def app(self):
        @web.middleware
        async def no_cache(req, handler):
            response = await handler(req)
            response.headers['Cache-Control'] = 'no-store'
            return response

        app = web.Application(middlewares=[no_cache, self.auth_middleware])
        app.on_cleanup.append(self.cleanup)
        app.router.add_get('/login', self.login_page)
        app.router.add_get('/quadruped/login', self.login_page)
        app.router.add_post('/auth/login', self.auth_login)
        app.router.add_post('/auth/logout', self.auth_logout)
        app.router.add_get('/health', self.health)
        app.router.add_get('/quadruped/health', self.health)
        app.router.add_get('/internal/v1/archive/status', self.archive_status_api)
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
        app.router.add_get('/quadruped/api/incidents', self.incidents_api)
        app.router.add_get(
            '/quadruped/api/incidents/{robot_id}/{incident_id}/snapshot',
            self.incident_snapshot_get,
        )
        app.router.add_post(
            '/internal/v1/incidents/{robot_id}/{incident_id}/snapshot',
            self.incident_snapshot_upload,
        )
        app.router.add_route(
            '*', '/quadruped/api/robots/{robot_id}/camera', self.robot_camera_api)
        app.router.add_get('/quadruped/rtc/config', self.rtc_config)
        app.router.add_route('*', '/quadruped/rtc/publisher-status', self.rtc_publisher_status)
        app.router.add_get('/robot/ws', self.handle_robot)
        app.router.add_get('/quadruped/robot/ws', self.handle_robot)
        app.router.add_get('/quadruped/robot/video/ws', self.handle_robot_video)
        app.router.add_get('/quadruped/ws', self.browser_ws)
        app.router.add_get('/quadruped/camera/ws', self.camera_source_ws)
        app.router.add_get('/ws', self.browser_ws)
        app.router.add_get('/quadruped', lambda _r: web.HTTPPermanentRedirect('/quadruped/'))
        app.router.add_get('/quadruped/fleet', lambda _r: web.HTTPPermanentRedirect('/quadruped/fleet/'))
        app.router.add_get('/quadruped/fleet/', self.fleet)
        app.router.add_get(
            '/quadruped/inspection',
            lambda _r: web.HTTPPermanentRedirect('/quadruped/inspection/'),
        )
        app.router.add_get('/quadruped/inspection/', self.inspection)
        app.router.add_get('/quadruped/', self.index)
        app.router.add_get('/quadruped/{path:.*}', self.static_file)
        app.router.add_get('/', lambda _r: web.HTTPPermanentRedirect('/quadruped/fleet/'))
        return app


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    here = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--port', type=int, default=8080)
    parser.add_argument('--static', default=str(here.parent / 'static'))
    parser.add_argument('--access-token', default=os.environ.get('GO2W_ACCESS_TOKEN'))
    parser.add_argument(
        '--auth-session-ttl',
        type=int,
        default=int(os.environ.get('GO2W_AUTH_SESSION_TTL', str(12 * 60 * 60))),
    )
    args = parser.parse_args()
    gateway = CloudGateway(
        args.static,
        rtc_app_id=os.environ.get('GO2W_RTC_APP_ID'),
        rtc_app_key=os.environ.get('GO2W_RTC_APP_KEY'),
        rtc_room_id=os.environ.get('GO2W_RTC_ROOM_ID', 'go2w-01'),
        rtc_ttl=int(os.environ.get('GO2W_RTC_TOKEN_TTL', '3600')),
        default_robot_id=os.environ.get('GO2W_DEFAULT_ROBOT_ID'),
        access_token=args.access_token,
        auth_session_ttl=args.auth_session_ttl,
    )
    web.run_app(gateway.app(), host=args.host, port=args.port,
                print=None, handle_signals=True, shutdown_timeout=3.0)


if __name__ == '__main__':
    main()
