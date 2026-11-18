#!/usr/bin/env python3
"""启动独立假ROS后端，核验真实HTTP/WS及强制结束后的恢复。绝不访问真机。

用开发venv执行：python dev/check_teaching_protocol.py --output /tmp/taught-protocol
地图、原始采样、后端日志与结果全部写到指定测试目录，保留作回读证据。
"""

import argparse
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

import aiohttp

ROOT = Path(__file__).resolve().parents[1]


class Client:
    def __init__(self, session, url):
        self.session = session
        self.url = url
        self.acks = {}
        self.states = []
        self.state = {}
        self.task = {}

    async def open(self):
        self.ws = await self.session.ws_connect(self.url + '/ws')
        self.reader = asyncio.create_task(self.read())
        self.pings = asyncio.create_task(self.ping())
        return self

    async def read(self):
        async for msg in self.ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                data = json.loads(msg.data)
                if data.get('t') == 'ack' and data.get('id') in self.acks:
                    self.acks.pop(data['id']).set_result(data)
                elif data.get('t') == 'taught':
                    self.states.append(data)
                elif data.get('t') == 'state':
                    self.state = data
                elif data.get('t') == 'task':
                    self.task = data.get('task') or {}

    async def ping(self):
        while not self.ws.closed:
            await self.ws.send_json({'t': 'ping', 'ts': time.time()})
            await asyncio.sleep(0.25)

    async def cmd(self, op, **data):
        ident = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.acks[ident] = future
        await self.ws.send_json({'t': 'cmd', 'id': ident, 'op': op, **data})
        return await asyncio.wait_for(future, 12.0)

    async def close(self):
        self.pings.cancel()
        await self.ws.close()
        self.reader.cancel()
        await asyncio.gather(self.pings, self.reader, return_exceptions=True)

    async def drive(self, duration=2.0):
        armed = await self.cmd('arm', on=True)
        assert armed['ok'], armed
        until = time.monotonic() + duration
        while time.monotonic() < until:
            await self.ws.send_json({'t': 'teleop', 'v': 0.25, 'w': 0.0, 'vy': 0.0})
            await asyncio.sleep(0.08)
        await self.ws.send_json({'t': 'teleop', 'v': 0.0, 'w': 0.0, 'vy': 0.0})
        await asyncio.sleep(0.5)


async def main(output, port):
    output.mkdir(parents=True, exist_ok=True)
    maps = output / 'maps'
    maps.mkdir(exist_ok=True)
    if (maps / 'lab.yaml').exists():
        raise ValueError('--output 必须使用新的测试目录，不能覆盖已有地图和采集证据')
    width, height = 240, 200
    pixels = bytearray([254]) * (width * height)
    for y in range(height):
        for x in range(width):
            if x < 4 or x > width - 5 or y < 4 or y > height - 5:
                pixels[y * width + x] = 0
    (maps / 'lab.pgm').write_bytes(b'P5\n240 200\n255\n' + pixels)
    (maps / 'lab.yaml').write_text(
        'image: lab.pgm\nmode: trinary\nresolution: 0.05\norigin: [-6,-5,0]\n'
        'negate: 0\noccupied_thresh: 0.65\nfree_thresh: 0.196\n'
    )
    env = dict(
        os.environ,
        GO2W_DATA=str(output),
        GO2W_FAKE_MAP=str(maps / 'lab.yaml'),
        GO2W_FAKE_PROCS='1',
        GO2W_TAUGHT_ROUTE='false',
        GO2W_AUTO_LOCALIZE='0',
        GO2W_IDLE_LIE='0',
    )
    env['PYTHONPATH'] = os.pathsep.join(
        (str(ROOT / 'dev/fake_ros'), str(ROOT.parent / 'go2w/src/navigation/go2w_taught_route'))
    )
    url = f'http://127.0.0.1:{port}'
    process = None
    log = None
    clients = []
    evidence = []

    def start():
        nonlocal process, log
        log = (output / 'backend.log').open('a')
        process = subprocess.Popen(
            [sys.executable, str(ROOT / 'server/go2w_web.py'), '--port', str(port)],
            env=env,
            stdout=log,
            stderr=log,
        )

    async def ready(session):
        for _ in range(100):
            if process.poll() is not None:
                raise RuntimeError('假后端退出，见backend.log')
            try:
                async with session.get(url + '/api/taught/maps') as r:
                    if r.status == 200:
                        break
            except aiohttp.ClientError:
                pass
            await asyncio.sleep(0.1)
        else:
            raise RuntimeError('后端未就绪')
        await asyncio.sleep(2.0)

    async def connect(session):
        c = await Client(session, url).open()
        clients.append(c)
        return c

    try:
        start()
        async with aiohttp.ClientSession() as session:
            await ready(session)
            c = await connect(session)
            initial = await c.cmd('taught_get', map='lab')
            assert initial['ok'], initial
            d = initial['data']
            assert d['revision'] == 0 and d['points'] == [], d
            denied = await c.cmd(
                'taught_start',
                map='lab',
                mode='replace',
                expected_revision=0,
                map_token=d['map_token'],
            )
            assert not denied['ok'], denied
            evidence.append('unknown localization blocks capture')
            prepared = await c.cmd('taught_prepare', map='lab')
            assert prepared['ok'], prepared
            for _ in range(400):
                prepared = (await c.cmd('taught_get', map='lab'))['data']
                if prepared['check']['state'] in ('ready', 'failed'):
                    break
                await asyncio.sleep(0.1)
            assert prepared['check']['state'] in ('ready', 'failed'), prepared
            assert any(
                row['key'] == 'map' and row['ok'] for row in prepared['check'].get('checks', [])
            ), prepared
            for _ in range(30):
                if c.state:
                    break
                await asyncio.sleep(0.1)
            assert c.state.get('armed') is False, c.state
            assert c.task.get('status') != 'active', c.task
            evidence.append(
                'map prepare upgraded legacy navigation, loaded map and checked localization without automatic arm or goal'
            )
            assert (await c.cmd('initial_pose', x=0.025, y=0.025, th=0.0))['ok']
            for _ in range(30):
                checked = await c.cmd('taught_check', map='lab')
                if checked['ok']:
                    break
                await asyncio.sleep(0.1)
            assert checked['ok'], checked
            begun = await c.cmd(
                'taught_start',
                map='lab',
                mode='replace',
                expected_revision=0,
                map_token=d['map_token'],
            )
            assert begun['ok'], begun
            sid = begun['data']['capture']['session_id']
            goal = await c.cmd('goal', x=1.0, y=0.0, th=0.0)
            assert not goal['ok'], goal
            observer = await connect(session)
            await observer.close()
            await asyncio.sleep(0.3)
            assert (await c.cmd('taught_get', map='lab'))['data']['capture']['state'] == 'recording'
            evidence.append(
                'observer disconnect does not interrupt owner; navigation blocked during capture'
            )
            await c.drive(2.5)
            status = (await c.cmd('taught_get', map='lab'))['data']['capture']
            assert status['state'] == 'recording' and status['sample_count'] >= 4, status
            last = status['points'][-1]
            count = status['sample_count']
            # SIGKILL没有清理机会：重启必须依据已fsync原始日志恢复成中断态，不能自动续跑。
            process.kill()
            process.wait(timeout=5.0)
            log.close()
            await c.close()
            env['GO2W_FAKE_START'] = f"{last['x']},{last['y']},{last['yaw']}"
            # 真机网页重启不会重启已准备好的导航；假ROS与网页同进程，重建同样的已启用状态。
            env['GO2W_TAUGHT_ROUTE'] = 'true'
            start()
            await ready(session)
            c = await connect(session)
            recovered = (await c.cmd('taught_get', map='lab'))['data']['capture']
            assert (
                recovered['session_id'] == sid
                and recovered['state'] == 'interrupted'
                and recovered['sample_count'] >= count
            ), recovered
            evidence.append(
                f'backend SIGKILL preserved {count} acknowledged raw samples; restart does not auto resume'
            )
            premature = await c.cmd('taught_resume', session_id=sid)
            assert not premature['ok'], premature
            assert (await c.cmd('initial_pose', x=last['x'], y=last['y'], th=last['yaw']))['ok']
            for _ in range(30):
                checked = await c.cmd('taught_check', map='lab')
                if checked['ok']:
                    break
                await asyncio.sleep(0.1)
            assert checked['ok'], checked
            resumed = await c.cmd('taught_resume', session_id=sid)
            assert resumed['ok'], resumed
            await c.drive(1.0)
            paused = await c.cmd('taught_pause', session_id=sid)
            assert paused['ok'], paused
            before = paused['data']['capture']['sample_count']
            await asyncio.sleep(0.6)
            assert (await c.cmd('taught_get', map='lab'))['data']['capture'][
                'sample_count'
            ] == before
            finish = await c.cmd('taught_finish', session_id=sid)
            assert finish['ok'], finish
            for _ in range(100):
                result = (await c.cmd('taught_get', map='lab'))['data']
                if result['capture']['state'] not in ('paused', 'processing'):
                    break
                await asyncio.sleep(0.1)
            assert (
                result['capture']['state'] == 'finished'
                and result['executable']
                and result['revision'] == 1
            ), result
            assert len({p['continuous_id'] for p in result['points']}) == 1, result
            raw = maps / 'lab.taught/captures' / sid / 'samples.jsonl'
            raw_rows = [json.loads(row) for row in raw.read_text().splitlines()]
            assert len({p['continuous_id'] for p in raw_rows}) == 2
            evidence.append(
                'explicit recheck/resume, paused samples frozen, validated connector leaves raw continuous segments intact'
            )
            async with session.post(
                url + '/api/taught/maps/lab',
                json={
                    'expected_revision': 0,
                    'map_token': d['map_token'],
                    'points': [{'x': 0.0, 'y': 0.0}, {'x': 1.0, 'y': 0.0}],
                },
            ) as response:
                assert response.status == 409, await response.text()
            evidence.append('stale HTTP editor save rejected without overwriting revision 1')
            async with session.get(url + '/api/taught/maps/lab/grid') as response:
                assert response.status == 200
            # 正常断开控制方也必须中断并上锁；旁观页断开已在前面验证不影响。
            started = await c.cmd(
                'taught_start',
                map='lab',
                mode='replace',
                expected_revision=result['revision'],
                map_token=result['map_token'],
            )
            assert started['ok'], started
            observer = await connect(session)
            refused = await observer.cmd('arm', on=True)
            assert not refused['ok'], refused
            assert (await c.cmd('arm', on=True))['ok']
            await c.close()
            await asyncio.sleep(0.4)
            disconnected = (await observer.cmd('taught_get', map='lab'))['data']['capture']
            assert disconnected['state'] == 'interrupted', disconnected
            await asyncio.sleep(0.6)
            assert observer.state.get('armed') is False, observer.state
            evidence.append(
                'capture owner disconnect interrupts and disarms; observer cannot steal arm ownership'
            )
            (output / 'result.json').write_text(
                json.dumps(
                    {'passed': True, 'checks': evidence, 'document': result},
                    ensure_ascii=False,
                    indent=2,
                )
            )
            print(
                json.dumps(
                    {
                        'passed': True,
                        'checks': evidence,
                        'samples': len(raw_rows),
                        'length': result['length'],
                    },
                    ensure_ascii=False,
                )
            )
    finally:
        await asyncio.gather(*(c.close() for c in clients), return_exceptions=True)
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5.0)
        if log is not None:
            log.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--port', type=int, default=8840)
    args = parser.parse_args()
    asyncio.run(main(args.output.resolve(), args.port))
