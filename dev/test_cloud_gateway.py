#!/usr/bin/env python3
"""Regression tests for the cloud gateway and robot edge gateway."""
import asyncio
import json
import struct
import sys
import tempfile
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiohttp import WSMsgType, web
from aiohttp.client import ClientSession


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'server'))
sys.path.insert(0, str(ROOT / 'tools'))

from cloud_gateway import CloudGateway, T_CAM, T_MAP  # noqa: E402
from archive import ArchiveSettings, ArchiveSpool  # noqa: E402
from rtc_token import PRIV_PUBLISH_STREAM, PRIV_SUBSCRIBE_STREAM, parse as parse_rtc_token  # noqa: E402
import go2w_edge_gateway as edge  # noqa: E402
from ssh_bindings import SSHBinding, row_to_binding  # noqa: E402


def pack_frame(kind, metadata, payload=b''):
    packed = json.dumps(metadata, separators=(',', ':')).encode()
    return bytes([kind]) + struct.pack('<I', len(packed)) + packed + payload


def unpack_frame(data):
    metadata_length = struct.unpack_from('<I', data, 1)[0]
    metadata = json.loads(data[5:5 + metadata_length])
    return data[0], metadata, data[5 + metadata_length:]


class Reader:
    def __init__(self, ws):
        self.ws = ws
        self.queue = asyncio.Queue()
        self.items = []
        self.task = asyncio.create_task(self._read())

    async def _read(self):
        try:
            async for message in self.ws:
                if message.type == WSMsgType.TEXT:
                    self.queue.put_nowait(('text', json.loads(message.data)))
                elif message.type == WSMsgType.BINARY:
                    self.queue.put_nowait(('binary', bytes(message.data)))
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    async def get(self, predicate, timeout=3):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            for index, item in enumerate(self.items):
                if predicate(item):
                    return self.items.pop(index)
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError(f'message not received; seen={self.items!r}')
            self.items.append(await asyncio.wait_for(self.queue.get(), remaining))

    async def text(self, predicate=lambda _m: True, timeout=3):
        return (await self.get(lambda x: x[0] == 'text' and predicate(x[1]), timeout))[1]

    async def binary(self, kind, timeout=3):
        return (await self.get(lambda x: x[0] == 'binary' and x[1] and x[1][0] == kind, timeout))[1]

    async def close(self):
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)


class FakeSSHStore:
    def __init__(self):
        self.items = {}

    async def close(self):
        pass

    async def list(self):
        return sorted(self.items.values(), key=lambda item: item.port)

    async def get_by_port(self, port):
        return next((item for item in self.items.values() if item.port == port), None)

    async def get_by_device(self, device_id):
        return self.items.get(device_id)

    async def upsert(self, binding):
        self.items[binding.device_id] = binding
        return binding

    async def set_online(self, device_id, online, seen_at=None):
        binding = self.items.get(device_id)
        if binding is None:
            return None
        seen_at = seen_at or datetime.now(timezone.utc)
        binding = replace(binding, online=online, last_seen_at=seen_at)
        self.items[device_id] = binding
        return binding

    async def unbind(self, device_id):
        binding = self.items.get(device_id)
        if binding is None:
            return None, None
        old_port = binding.port
        binding = replace(binding, port=None, status='unbound', online=False)
        self.items[device_id] = binding
        return binding, old_port


class FakeBroker:
    def __init__(self):
        self.allocation = None
        self.released = []

    async def close(self):
        pass

    async def public_target(self):
        return {'public_host': 'ssh-portbroker.example.com', 'public_ssh_port': 22}

    async def get_dog_port(self, dog_id):
        return self.allocation

    async def allocate(self, dog_id, note='register'):
        self.allocation = {'dog_id': dog_id, 'port': 22100, 'state': 'occupied'}
        return {
            'dog_id': dog_id,
            'port': 22100,
            'reused': False,
            'public': {'public_host': 'ssh-portbroker.example.com', 'public_ssh_port': 22},
        }

    async def release(self, dog_id, port, reason='destroy'):
        self.released.append((dog_id, port, reason))
        return {'released': True, 'port': port, 'message': 'released'}


class FakeArchiveStore:
    configured = True

    def __init__(self):
        self.sessions = {}
        self.commands = {}
        self.objects = []
        self.incidents = {}
        self.incident_snapshots = []

    async def close(self):
        pass

    async def session_started(self, robot_id, session_id, connected_at,
                              remote_info=None, edge_version=None):
        self.sessions[session_id] = {
            'robot_id': robot_id,
            'status': 'active',
            'edge_version': edge_version,
        }

    async def session_ended(self, session_id, disconnected_at=None, status='closed'):
        self.sessions[session_id]['status'] = status

    async def command_issued(self, record):
        self.commands[record['command_id']] = {
            **record,
            'status': 'issued',
        }

    async def command_sent(self, command_id, sent_at=None):
        self.commands[command_id]['status'] = 'sent'

    async def command_ack(self, command_id, ok, message='', response=None,
                          ack_at=None):
        self.commands[command_id].update({
            'status': 'acked' if ok else 'failed',
            'ok': ok,
            'message': message,
            'response': response,
        })

    async def command_failed(self, command_id, status='failed', message=''):
        self.commands[command_id].update({
            'status': status,
            'message': message,
        })

    async def add_object(self, meta):
        self.objects.append(meta)

    async def upsert_incident(self, robot_id, item):
        self.incidents[(robot_id, item['id'])] = dict(item)

    async def list_incidents(self, robot_id, limit=50, before_ms=None):
        items = [
            item for (stored_robot, _), item in self.incidents.items()
            if stored_robot == robot_id
            and (before_ms is None or int(item['ts']) < before_ms)
        ]
        return sorted(items, key=lambda item: int(item['ts']), reverse=True)[:limit]

    async def set_incident_snapshot(self, robot_id, incident_id, bucket, key):
        self.incident_snapshots.append((robot_id, incident_id, bucket, key))


async def start_app(app):
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, port


async def test_cloud_gateway():
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, 'index.html').write_text('<h1>Go2W</h1>', encoding='utf-8')
        Path(tmp, 'inspection').mkdir()
        Path(tmp, 'inspection', 'index.html').write_text(
            '<h1>Go2W Inspection</h1>', encoding='utf-8')
        rtc_app_id, rtc_app_key = '123456781234567812345678', 'test-app-key'
        gateway = CloudGateway(tmp, rtc_app_id, rtc_app_key, 'go2w-01', 3600)
        runner, port = await start_app(gateway.app())
        base = f'http://127.0.0.1:{port}'
        try:
            async with ClientSession() as session:
                response = await session.get(base + '/quadruped/')
                assert response.status == 200
                assert 'Go2W' in await response.text()
                inspection = await session.get(base + '/quadruped/inspection/')
                assert inspection.status == 200
                assert 'Go2W Inspection' in await inspection.text()
                inspection_redirect = await session.get(
                    base + '/quadruped/inspection', allow_redirects=False)
                assert inspection_redirect.status in {301, 308}
                assert inspection_redirect.headers['Location'].endswith('/quadruped/inspection/')
                robot_response = await session.get(
                    base + '/quadruped/rtc/config?role=robot&robot_id=go2w-01')
                assert robot_response.headers['Access-Control-Allow-Origin'] == '*'
                robot_config = await robot_response.json()
                assert robot_config['app_id'] == rtc_app_id
                robot_token = parse_rtc_token(robot_config['token'])
                assert robot_token.room_id == 'go2w-01'
                assert robot_token.user_id == 'go2w-01-camera'
                assert PRIV_PUBLISH_STREAM in robot_token.privileges
                assert PRIV_SUBSCRIBE_STREAM in robot_token.privileges
                viewer_config = await (await session.get(
                    base + '/quadruped/rtc/config?role=viewer')).json()
                viewer_token = parse_rtc_token(viewer_config['token'])
                assert PRIV_SUBSCRIBE_STREAM in viewer_token.privileges
                assert PRIV_PUBLISH_STREAM not in viewer_token.privileges

                robot = await session.ws_connect(base + '/quadruped/robot/ws')
                robot_reader = Reader(robot)
                await robot.send_str('{"type":"hello","robot_id":"go2w-01","protocol":1}')

                browser = await session.ws_connect(base + '/quadruped/ws')
                browser_reader = Reader(browser)
                status = await browser_reader.text(lambda m: m.get('t') == 'robot_status')
                assert status == {
                    't': 'robot_status',
                    'online': True,
                    'robot_id': 'go2w-01',
                    'video_upload_enabled': True,
                }

                await robot.send_str('{"t":"state","armed":false,"booting":true}')
                state = await browser_reader.text(lambda m: m.get('t') == 'state')
                assert state['booting'] is True

                exceptions = {
                    't': 'inspection_exceptions',
                    'mode': 'snapshot',
                    'task_id': 'route-1',
                    'items': [{
                        'event_id': 'evt-1',
                        'anomaly_type': 'long_blocked',
                        'title': '长时受阻',
                        'point_name': '人行步道',
                        'status': 'new',
                        'duration_s': 18.4,
                    }],
                }
                await robot.send_str(json.dumps(exceptions, ensure_ascii=False))
                live_exceptions = await browser_reader.text(
                    lambda m: m.get('t') == 'inspection_exceptions')
                assert live_exceptions['items'][0]['event_id'] == 'evt-1'

                incidents = [{
                    'id': 'incident-1',
                    'ts': 1763445936033,
                    'end': None,
                    'dur': None,
                    'ongoing': True,
                    'robot': 'go2w-01',
                    'type': 'stuck',
                    'label': '长时受阻',
                    'level': 'warn',
                    'msg': '导航中原地不动超过 8 秒',
                    'pose': {'x': 3.02, 'y': -2.66, 'th': -0.05, 'frame': 'map'},
                    'map': 'lab_20261021',
                    'task': {'routeId': 'route-1', 'pointName': '人行步道'},
                    'bag': {'state': 'pending', 'name': None, 'msg': ''},
                }]
                await robot.send_str(json.dumps({
                    't': 'incidents', 'items': incidents,
                }, ensure_ascii=False))
                live_incidents = await browser_reader.text(
                    lambda m: m.get('t') == 'incidents')
                assert live_incidents['items'][0]['id'] == 'incident-1'
                incident_api = await session.get(
                    base + '/quadruped/api/incidents?robot_id=go2w-01&limit=50')
                assert incident_api.status == 200
                assert (await incident_api.json())['items'][0]['type'] == 'stuck'

                updated_incident = {
                    **incidents[0],
                    'end': 1763445948633,
                    'dur': 12.6,
                    'ongoing': False,
                    'bag': {'state': 'saved', 'name': 'bag-1', 'msg': ''},
                }
                await robot.send_str(json.dumps({
                    't': 'incident', 'item': updated_incident,
                }, ensure_ascii=False))
                updated_live = await browser_reader.text(
                    lambda m: m.get('t') == 'incident')
                assert updated_live['item']['dur'] == 12.6
                incident_api = await session.get(
                    base + '/quadruped/api/incidents?robot_id=go2w-01')
                items = (await incident_api.json())['items']
                assert len(items) == 1 and items[0]['bag']['name'] == 'bag-1'

                await robot.send_bytes(bytes([T_MAP, 9, 8, 7]))
                assert await browser_reader.binary(T_MAP) == bytes([T_MAP, 9, 8, 7])

                camera = await session.ws_connect(base + '/quadruped/camera/ws')
                camera_reader = Reader(camera)
                await robot_reader.text(lambda m: m.get('t') == 'cam' and m.get('on') is True)
                disabled_response = await session.post(
                    base + '/quadruped/api/robots/go2w-01/camera', json={'enabled': False})
                assert disabled_response.status == 200
                assert (await disabled_response.json())['video_upload_enabled'] is False
                await robot_reader.text(lambda m: m.get('t') == 'cam' and m.get('on') is False)
                notification = await browser_reader.text(lambda m: m.get('t') == 'camera_upload')
                assert notification['enabled'] is False
                await robot.send_bytes(bytes([T_CAM, 8, 9]))
                assert not any(item[0] == 'binary' and item[1][0] == T_CAM
                               for item in camera_reader.items)
                enabled_response = await session.post(
                    base + '/quadruped/api/robots/go2w-01/camera', json={'enabled': True})
                assert (await enabled_response.json())['video_upload_enabled'] is True
                await robot_reader.text(lambda m: m.get('t') == 'cam' and m.get('on') is True)
                await robot.send_bytes(bytes([T_CAM, 5, 6]))
                assert await camera_reader.binary(T_CAM) == bytes([T_CAM, 5, 6])
                await camera.close()
                await robot_reader.text(lambda m: m.get('t') == 'cam' and m.get('on') is False)
                await camera_reader.close()

                await browser.send_str('{"t":"sub","layers":["cloud","particles"]}')
                subscribed = await robot_reader.text(
                    lambda m: m.get('t') == 'sub' and m.get('layers') == ['cloud', 'particles'])
                assert subscribed['layers'] == ['cloud', 'particles']
                assert await browser_reader.binary(T_MAP) == bytes([T_MAP, 9, 8, 7])

                await browser.send_str('{"t":"cmd","id":7,"op":"arm","on":false}')
                command = await robot_reader.text(lambda m: m.get('t') == 'cmd')
                assert command['op'] == 'arm' and command['id'] != 7
                await robot.send_str(json.dumps({
                    't': 'ack', 'id': command['id'], 'ok': True, 'msg': 'locked',
                }))
                ack = await browser_reader.text(lambda m: m.get('t') == 'ack')
                assert ack == {'t': 'ack', 'id': 7, 'ok': True, 'msg': 'locked'}

                await browser.send_str('{"t":"cam","on":true}')
                camera_error = await browser_reader.text(lambda m: m.get('t') == 'cam')
                assert camera_error['state'] == 'error'
                assert not any(x[0] == 'text' and x[1].get('t') == 'cam' and x[1].get('on') is True
                               for x in robot_reader.items)

                await robot.send_bytes(bytes([T_CAM, 1, 2]))
                await robot.send_bytes(bytes([T_MAP, 3, 4]))
                assert await browser_reader.binary(T_MAP) == bytes([T_MAP, 3, 4])
                assert not any(x[0] == 'binary' and x[1][0] == T_CAM for x in browser_reader.items)

                other = await session.ws_connect(base + '/quadruped/ws')
                other_reader = Reader(other)
                await other_reader.text(lambda m: m.get('t') == 'robot_status')
                await other.send_str('{"t":"cmd","id":8,"op":"cancel"}')
                command = await robot_reader.text(lambda m: m.get('t') == 'cmd')
                assert command['op'] == 'cancel'
                await robot.send_str(json.dumps({
                    't': 'ack', 'id': command['id'], 'ok': True, 'msg': 'canceled',
                }))
                other_ack = await other_reader.text(lambda m: m.get('t') == 'ack')
                assert other_ack == {
                    't': 'ack', 'id': 8, 'ok': True, 'msg': 'canceled',
                }

                await browser.close()
                await asyncio.sleep(0.1)
                assert not any(
                    item[0] == 'text'
                    and item[1].get('t') == 'cmd'
                    and item[1].get('op') in {'cancel', 'arm'}
                    for item in robot_reader.items
                )
                await browser_reader.close()

                await other.close()
                cancel = await robot_reader.text(lambda m: m.get('t') == 'cmd' and m.get('op') == 'cancel')
                lock = await robot_reader.text(lambda m: m.get('t') == 'cmd' and m.get('op') == 'arm')
                assert cancel['op'] == 'cancel' and lock['on'] is False

                await other_reader.close()
                browser2 = await session.ws_connect(base + '/quadruped/ws')
                browser2_reader = Reader(browser2)
                status = await browser2_reader.text(lambda m: m.get('t') == 'robot_status')
                assert status['online'] is True
                replayed_exceptions = await browser2_reader.text(
                    lambda m: m.get('t') == 'inspection_exceptions')
                assert replayed_exceptions['items'][0]['title'] == '长时受阻'
                await robot.close()
                status = await browser2_reader.text(lambda m: m.get('t') == 'robot_status')
                assert status['online'] is False
                assert not gateway.robots
                await browser2.close()
                await browser2_reader.close()
                await robot_reader.close()

                browser3 = await session.ws_connect(base + '/quadruped/ws')
                browser3_reader = Reader(browser3)
                status = await browser3_reader.text(lambda m: m.get('t') == 'robot_status')
                assert status['online'] is False
                await asyncio.sleep(0.1)
                assert not any(x[0] == 'text' and x[1].get('t') == 'state'
                               for x in browser3_reader.items)
                await browser3.close()
                await browser3_reader.close()
        finally:
            await runner.cleanup()


async def test_multi_robot_gateway():
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, 'index.html').write_text('<h1>Go2W</h1>', encoding='utf-8')
        gateway = CloudGateway(tmp)
        runner, port = await start_app(gateway.app())
        base = f'http://127.0.0.1:{port}'
        try:
            async with ClientSession() as session:
                robot1 = await session.ws_connect(base + '/quadruped/robot/ws')
                robot2 = await session.ws_connect(base + '/quadruped/robot/ws')
                robot1_reader, robot2_reader = Reader(robot1), Reader(robot2)
                await robot1.send_str('{"type":"hello","robot_id":"go2w-01","protocol":1}')
                await robot2.send_str('{"type":"hello","robot_id":"go2w-02","protocol":1}')

                browser1 = await session.ws_connect(base + '/quadruped/ws?robot_id=go2w-01')
                browser2 = await session.ws_connect(base + '/quadruped/ws?robot_id=go2w-02')
                browser1_reader, browser2_reader = Reader(browser1), Reader(browser2)
                status1 = await browser1_reader.text(lambda m: m.get('t') == 'robot_status')
                status2 = await browser2_reader.text(lambda m: m.get('t') == 'robot_status')
                assert status1['robot_id'] == 'go2w-01' and status1['online'] is True
                assert status2['robot_id'] == 'go2w-02' and status2['online'] is True

                listing = await (await session.get(base + '/quadruped/api/robots')).json()
                assert listing['total'] == 2 and listing['online'] == 2
                assert [robot['robot_id'] for robot in listing['robots']] == ['go2w-01', 'go2w-02']

                await robot1.send_str('{"t":"state","marker":1}')
                state1 = await browser1_reader.text(
                    lambda m: m.get('t') == 'state' and m.get('marker') == 1)
                assert state1['marker'] == 1
                try:
                    await browser2_reader.text(
                        lambda m: m.get('t') == 'state' and m.get('marker') == 1, timeout=0.3)
                    raise AssertionError('go2w-01 state leaked to go2w-02 browser')
                except TimeoutError:
                    pass

                await robot2.send_str('{"t":"state","marker":2}')
                state2 = await browser2_reader.text(
                    lambda m: m.get('t') == 'state' and m.get('marker') == 2)
                assert state2['marker'] == 2

                await browser1.send_str('{"t":"cmd","id":9,"op":"cancel"}')
                command = await robot1_reader.text(lambda m: m.get('t') == 'cmd')
                assert command['robot_id'] == 'go2w-01'
                try:
                    await robot2_reader.text(lambda m: m.get('t') == 'cmd', timeout=0.3)
                    raise AssertionError('go2w-01 command leaked to go2w-02')
                except TimeoutError:
                    pass

                await robot1.close()
                offline = await browser1_reader.text(
                    lambda m: m.get('t') == 'robot_status' and m.get('online') is False)
                assert offline['robot_id'] == 'go2w-01'
                listing = await (await session.get(base + '/quadruped/api/robots')).json()
                assert listing['online'] == 1
                assert next(r for r in listing['robots'] if r['robot_id'] == 'go2w-02')['online'] is True

                await browser1.close()
                await browser2.close()
                await robot2.close()
                for reader in (robot1_reader, robot2_reader, browser1_reader, browser2_reader):
                    await reader.close()
        finally:
            await runner.cleanup()


async def test_archive_gateway():
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, 'index.html').write_text('<h1>Go2W</h1>', encoding='utf-8')
        settings = ArchiveSettings(
            enabled=True,
            root=Path(tmp, 'archive'),
            segment_seconds=300,
            segment_bytes=32 * 1024 * 1024,
            worker_interval=1,
            retry_max=30,
            completed_retention_days=7,
        )
        spool = ArchiveSpool(settings)
        store = FakeArchiveStore()
        gateway = CloudGateway(tmp, archive_spool=spool, archive_store=store)
        runner, port = await start_app(gateway.app())
        base = f'http://127.0.0.1:{port}'
        try:
            async with ClientSession() as session:
                robot = await session.ws_connect(base + '/quadruped/robot/ws')
                robot_reader = Reader(robot)
                await robot.send_str(json.dumps({
                    'type': 'hello',
                    'robot_id': 'go2w-01',
                    'edge_version': 'test-edge',
                    'protocol': 1,
                }))

                browser = await session.ws_connect(
                    base + '/quadruped/ws?robot_id=go2w-01')
                browser_reader = Reader(browser)
                await browser_reader.text(lambda m: m.get('t') == 'robot_status')

                await robot.send_str('{"t":"state","battery":88}')
                await browser_reader.text(
                    lambda m: m.get('t') == 'state' and m.get('battery') == 88)
                await robot.send_str(json.dumps({
                    't': 'incident',
                    'item': {
                        'id': 'archive-incident',
                        'ts': 1763445936033,
                        'ongoing': True,
                        'type': 'stuck',
                        'label': '长时受阻',
                        'level': 'warn',
                    },
                }, ensure_ascii=False))
                await browser_reader.text(
                    lambda m: m.get('t') == 'incident')
                publisher = await session.post(
                    base + '/quadruped/rtc/publisher-status?robot_id=go2w-01',
                    data='{}',
                )
                requests = (await publisher.json())['snapshot_requests']
                assert requests[0]['incident_id'] == 'archive-incident'
                jpeg = b'\xff\xd8\xff\xe0snapshot\xff\xd9'
                uploaded_snapshot = await session.post(
                    base + '/internal/v1/incidents/go2w-01/archive-incident/snapshot',
                    data=jpeg,
                    headers={'Content-Type': 'image/jpeg'},
                )
                assert uploaded_snapshot.status == 200
                snapshot_payload = await uploaded_snapshot.json()
                snapshot_response = await session.get(
                    base + snapshot_payload['url'])
                assert snapshot_response.status == 200
                assert await snapshot_response.read() == jpeg
                await browser_reader.text(
                    lambda m: m.get('t') == 'incident'
                    and m.get('item', {}).get('snapshot', {}).get('local_url'))
                await robot.send_bytes(bytes([2, 1, 2, 3]))
                await browser_reader.binary(2)

                await browser.send_str(
                    '{"t":"cmd","id":17,"op":"arm","on":false}')
                command = await robot_reader.text(lambda m: m.get('t') == 'cmd')
                await robot.send_str(json.dumps({
                    't': 'ack', 'id': command['id'], 'ok': True, 'msg': 'locked',
                }))
                await browser_reader.text(
                    lambda m: m.get('t') == 'ack' and m.get('id') == 17)

                await robot.close()
                await asyncio.sleep(0.2)
                assert len(store.sessions) == 1
                assert next(iter(store.sessions.values()))['status'] == 'closed'
                assert next(iter(store.sessions.values()))['edge_version'] == 'test-edge'
                assert len(store.commands) == 1
                archived_command = next(iter(store.commands.values()))
                assert archived_command['status'] == 'acked'
                assert archived_command['operation'] == 'arm'
                assert archived_command['response']['msg'] == 'locked'
                assert ('go2w-01', 'archive-incident') in store.incidents
                assert store.incidents[('go2w-01', 'archive-incident')]['snapshot'][
                    'local_url'].endswith('/snapshot')

                pending = list((Path(tmp, 'archive') / 'spool' / 'pending').rglob('*'))
                text_files = [path for path in pending if path.suffix == '.jsonl']
                binary_files = [path for path in pending if path.suffix == '.bin']
                assert len(text_files) == 2, pending
                assert len(binary_files) == 1, pending
                records = [
                    json.loads(line)
                    for path in text_files
                    for line in path.read_text(encoding='utf-8').splitlines()
                ]
                assert {record['message_type'] for record in records} == {
                    'hello', 'state', 'incident', 'ack', 'cmd'}
                assert any(record['command_id'] for record in records)
                assert binary_files[0].read_bytes()[:4] == struct.pack('<I', 4)

                await browser.close()
                await browser_reader.close()
                await robot_reader.close()
        finally:
            await runner.cleanup()


async def test_internal_ssh_bindings():
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, 'index.html').write_text('<h1>Go2W</h1>', encoding='utf-8')
        store = FakeSSHStore()
        gateway = CloudGateway(tmp, ssh_store=store)
        runner, port = await start_app(gateway.app())
        base = f'http://127.0.0.1:{port}'
        try:
            async with ClientSession() as session:
                listing = await (await session.get(
                    base + '/internal/v1/ssh-bindings')).json()
                assert listing == {'items': []}

                now = datetime.now(timezone.utc)
                binding = SSHBinding(
                    device_id='demo-device-1',
                    device_name='演示机器狗',
                    device_model='Unitree Go2',
                    device_enabled=True,
                    online=True,
                    host='ssh-gateway.example.com',
                    port=22001,
                    username='robot',
                    status='bound',
                    binding_version='7',
                    last_seen_at=now,
                    updated_at=now,
                )
                store.items[binding.device_id] = binding
                by_port = await session.get(
                    base + '/internal/v1/ssh-bindings/by-port/22001')
                by_port_payload = await by_port.json()
                assert by_port.status == 200
                assert by_port_payload['deviceId'] == 'demo-device-1'
                assert by_port_payload['port'] == 22001
                assert by_port_payload['lastSeenAt'].endswith('Z')

                upsert = await session.put(
                    base + '/internal/v1/ssh-bindings/demo-device-2',
                    json={
                        'deviceId': 'demo-device-2',
                        'deviceName': '测试机器狗 2',
                        'deviceModel': 'Unitree Go2',
                        'deviceEnabled': True,
                        'online': False,
                        'host': 'ssh-gateway.example.com',
                        'port': 22002,
                        'username': 'robot',
                        'status': 'bound',
                        'bindingVersion': '1',
                        'lastSeenAt': '2026-11-16T03:50:00.000Z',
                    },
                )
                assert upsert.status == 200
                assert (await upsert.json())['deviceId'] == 'demo-device-2'
                listing = await (await session.get(
                    base + '/internal/v1/ssh-bindings')).json()
                assert [item['port'] for item in listing['items']] == [22001, 22002]
                assert 'lastSeenAt' not in listing['items'][0]

                invalid = await session.get(
                    base + '/internal/v1/ssh-bindings/by-port/nope')
                assert invalid.status == 400
        finally:
            await runner.cleanup()


def test_ssh_binding_online_ttl():
    now = datetime.now(timezone.utc)
    base = {
        'device_id': 'ttl-device',
        'device_name': 'TTL',
        'device_model': 'Unitree Go2',
        'device_enabled': 1,
        'online': 1,
        'host': 'ssh-portbroker.example.com',
        'port': 22001,
        'username': 'unitree',
        'status': 'bound',
        'binding_version': '1',
        'updated_at': now,
        'serial_number': None,
        'firmware_version': None,
    }
    assert row_to_binding({**base, 'last_seen_at': now}).online is True
    assert row_to_binding({
        **base, 'last_seen_at': now - timedelta(seconds=31),
    }).online is False


async def test_robot_ssh_registration():
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, 'index.html').write_text('<h1>Go2W</h1>', encoding='utf-8')
        store, broker = FakeSSHStore(), FakeBroker()
        gateway = CloudGateway(tmp, ssh_store=store, ssh_broker=broker)
        runner, port = await start_app(gateway.app())
        base = f'http://127.0.0.1:{port}'
        try:
            async with ClientSession() as session:
                registered = await session.post(
                    base + '/internal/v1/robots/go2w-02/register',
                    json={
                        'deviceId': 'go2w-02',
                        'deviceName': '机器狗 02',
                        'deviceModel': 'Unitree Go2',
                        'serialNumber': 'SN-02',
                        'firmwareVersion': '1.0.0',
                        'username': 'robot',
                    },
                )
                assert registered.status == 200
                target = await registered.json()
                assert target['remotePort'] == 22100
                assert target['publicHost'] == 'ssh-portbroker.example.com'
                assert target['publicSshPort'] == 22
                assert target['online'] is True

                again = await (await session.post(
                    base + '/internal/v1/robots/go2w-02/register',
                    json={
                        'deviceId': 'go2w-02',
                        'deviceName': '机器狗 02',
                        'deviceModel': 'Unitree Go2',
                    },
                )).json()
                assert again['reused'] is True and again['remotePort'] == 22100

                offline = await (await session.post(
                    base + '/internal/v1/robots/go2w-02/offline')).json()
                assert offline['online'] is False
                assert offline['remotePort'] == 22100
                assert offline['portPreserved'] is True
                assert broker.released == []

                heartbeat = await (await session.post(
                    base + '/internal/v1/robots/go2w-02/heartbeat')).json()
                assert heartbeat['online'] is True and heartbeat['remotePort'] == 22100

                refused = await session.delete(
                    base + '/internal/v1/robots/go2w-02/ssh-binding',
                    json={'operator': 'tester', 'reason': 'destroy'},
                )
                assert refused.status == 400
                assert broker.released == []

                unbound = await session.delete(
                    base + '/internal/v1/robots/go2w-02/ssh-binding',
                    json={'manual': True, 'operator': 'tester', 'reason': 'destroy'},
                )
                assert unbound.status == 200
                payload = await unbound.json()
                assert payload['released'] is True and payload['port'] == 22100
                assert broker.released == [('go2w-02', 22100, 'destroy')]
                assert store.items['go2w-02'].port is None
                assert store.items['go2w-02'].status == 'unbound'
        finally:
            await runner.cleanup()


async def test_edge_gateway(forward_camera=False):
    local_messages, cloud_messages = asyncio.Queue(), asyncio.Queue()
    connected = asyncio.Event()
    camera_frame = pack_frame(T_CAM, {'key': True, 'dur': 0.07}, b'\x00\x00')
    map_frame = pack_frame(T_MAP, {'name': 'test-map'}, b'\x01\x02')

    async def local_handler(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_bytes(camera_frame)
        await ws.send_bytes(map_frame)
        await ws.send_str('{"t":"state","armed":false}')
        async for message in ws:
            if message.type == WSMsgType.TEXT:
                local_messages.put_nowait(('text', json.loads(message.data)))
            elif message.type == WSMsgType.BINARY:
                local_messages.put_nowait(('binary', bytes(message.data)))
        return ws

    async def cloud_handler(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        connected.set()
        await ws.send_str('{"t":"cam","on":true}')
        await ws.send_str('{"t":"cmd","id":1,"op":"cancel"}')
        async for message in ws:
            if message.type == WSMsgType.TEXT:
                cloud_messages.put_nowait(('text', json.loads(message.data)))
            elif message.type == WSMsgType.BINARY:
                cloud_messages.put_nowait(('binary', bytes(message.data)))
        return ws

    local_app, cloud_app = web.Application(), web.Application()
    local_app.router.add_get('/ws', local_handler)
    cloud_app.router.add_get('/robot/ws', cloud_handler)
    local_runner, local_port = await start_app(local_app)
    cloud_runner, cloud_port = await start_app(cloud_app)
    stop = asyncio.Event()
    task = asyncio.create_task(edge.run_gateway(
        f'ws://127.0.0.1:{local_port}/ws',
        f'ws://127.0.0.1:{cloud_port}/robot/ws',
        'go2w-01', 0.1, stop, forward_camera,
    ))
    try:
        await asyncio.wait_for(connected.wait(), 3)
        hello = await asyncio.wait_for(cloud_messages.get(), 3)
        assert hello == ('text', {'type': 'hello', 'robot_id': 'go2w-01', 'protocol': 1})
        if forward_camera:
            relayed_camera = await asyncio.wait_for(cloud_messages.get(), 3)
            kind, metadata, payload = unpack_frame(relayed_camera[1])
            assert kind == T_CAM and metadata['robot_id'] == 'go2w-01' and payload == b'\x00\x00'
        mapped = await asyncio.wait_for(cloud_messages.get(), 3)
        kind, metadata, payload = unpack_frame(mapped[1])
        assert kind == T_MAP and metadata['robot_id'] == 'go2w-01'
        assert metadata['name'] == 'test-map' and payload == b'\x01\x02'
        state = await asyncio.wait_for(cloud_messages.get(), 3)
        assert state == ('text', {'t': 'state', 'armed': False, 'robot_id': 'go2w-01'})

        if forward_camera:
            camera_on = await asyncio.wait_for(local_messages.get(), 3)
            assert camera_on == ('text', {'t': 'cam', 'on': True, 'robot_id': 'go2w-01'})
        command = await asyncio.wait_for(local_messages.get(), 3)
        assert command == ('text', {
            't': 'cmd', 'id': 1, 'op': 'cancel', 'robot_id': 'go2w-01',
        })
        await asyncio.sleep(0.1)
        if not forward_camera:
            assert not any(item[0] == 'text' and item[1].get('t') == 'cam'
                           for item in list(local_messages._queue))
    finally:
        stop.set()
        await asyncio.gather(task, return_exceptions=True)
        await local_runner.cleanup()
        await cloud_runner.cleanup()


async def main():
    await test_cloud_gateway()
    print('PASS cloud gateway: static, cache, layers, command ack, camera filtering')
    await test_multi_robot_gateway()
    print('PASS cloud gateway: concurrent robots, isolated data, routing, fleet API')
    await test_archive_gateway()
    print('PASS cloud gateway: archive session, telemetry, binary and command ACK capture')
    await test_internal_ssh_bindings()
    print('PASS cloud gateway: SSH binding lookup, list and upsert')
    test_ssh_binding_online_ttl()
    print('PASS cloud gateway: SSH binding online heartbeat TTL')
    await test_robot_ssh_registration()
    print('PASS cloud gateway: register, heartbeat, offline and manual unbind')
    await test_edge_gateway()
    print('PASS edge gateway: hello, robot_id injection, text/binary relay, camera filtering')
    await test_edge_gateway(forward_camera=True)
    print('PASS edge gateway: camera forwarding for cloud RTC publisher')
    print('\n全部通过')


if __name__ == '__main__':
    asyncio.run(main())
