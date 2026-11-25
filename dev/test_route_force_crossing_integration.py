"""真实HTTP/WebSocket后端 + 假机器人 + 临时ros2替身；不连接硬件。"""
import asyncio
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest

from aiohttp import ClientSession, WSMsgType

ROOT = Path(__file__).resolve().parents[1]


class CrossingIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_and_route_execution_share_parameter_command(self):
        with tempfile.TemporaryDirectory(prefix='go2w-crossing-') as tmp:
            base = Path(tmp)
            maps = base/'data/maps'; maps.mkdir(parents=True)
            (maps/'lab_20261021.yaml').write_text('image: lab.pgm\nresolution: 0.05\norigin: [-5, -5, 0]\n')
            (maps/'lab.pgm').write_bytes(b'P5\n200 200\n255\n'+bytes([254])*40000)
            bindir = base/'bin'; bindir.mkdir()
            command_log = base/'params.jsonl'
            ros2 = bindir/'ros2'
            ros2.write_text(f'#!{sys.executable}\nimport json,sys\nfrom pathlib import Path\n'
                            f'with Path({str(command_log)!r}).open("a") as out: out.write(json.dumps(sys.argv[1:])+"\\n")\n'
                            'print("Successfully set parameter")\n')
            ros2.chmod(0o755)
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
            env = {**os.environ, 'GO2W_FAKE_PROCS':'1', 'GO2W_DATA':str(base/'data'),
                   'GO2W_FAKE_MAP':str(maps/'lab_20261021.yaml'), 'GO2W_FAKE_START':'0,0,0',
                   'GO2W_CAM_ADDR':'127.0.0.1:9', 'PATH':str(bindir)+os.pathsep+os.environ['PATH'],
                   'PYTHONPATH':os.pathsep.join([str(ROOT/'dev/fake_ros'), str(ROOT.parent/'go2w/src/navigation/go2w_taught_route')])}
            log = (base/'server.log').open('w+')
            process = subprocess.Popen([sys.executable, str(ROOT/'server/go2w_web.py'), '--port', str(port),
                                        '--static', str(ROOT/'static')], cwd=ROOT, env=env, stdout=log, stderr=log)
            try:
                async with ClientSession() as session:
                    url = f'http://127.0.0.1:{port}'
                    for _ in range(100):
                        try:
                            async with session.get(url+'/version.json') as response:
                                if response.status == 200: break
                        except OSError:
                            pass
                        if process.poll() is not None:
                            log.seek(0); self.fail(log.read())
                        await asyncio.sleep(.1)
                    else:
                        self.fail('后端启动超时')
                    for value in ('true', 'false'):
                        async with session.post(url+'/api/obstacles-critic/'+value) as response:
                            self.assertEqual(response.status, 200)
                            self.assertEqual((await response.json())['enabled'], value == 'true')
                    command_log.write_text('')
                    async with session.ws_connect(url+'/ws') as ws:
                        state, replies, seq = {}, {}, 0
                        async def read():
                            async for message in ws:
                                if message.type == WSMsgType.TEXT:
                                    data = json.loads(message.data)
                                    if data['t'] == 'ack' and data['id'] in replies:
                                        replies.pop(data['id']).set_result(data)
                                    else:
                                        state[data['t']] = data
                        async def ping():
                            while True:
                                await ws.send_json(dict(t='ping', ts=0))
                                await asyncio.sleep(.3)
                        async def command(op, **values):
                            nonlocal seq
                            seq += 1
                            future = asyncio.get_running_loop().create_future()
                            replies[seq] = future
                            await ws.send_json(dict(t='cmd', id=seq, op=op, **values))
                            result = await asyncio.wait_for(future, 10)
                            self.assertTrue(result['ok'], result)
                            return result.get('data')
                        async def until(check, label, seconds=40):
                            async with asyncio.timeout(seconds):
                                while not check():
                                    await asyncio.sleep(.05)
                        def values():
                            return [json.loads(line)[-1] for line in command_log.read_text().splitlines()]
                        tasks = [asyncio.create_task(read()), asyncio.create_task(ping())]
                        try:
                            await until(lambda: state.get('pose', {}).get('frame') == 'map'
                                        and state.get('state', {}).get('mapCurrent') == 'lab_20261021', '定位')
                            pts = [dict(x=float(i), y=0., th=0., name=str(i), **({'forceCrossing':True} if i in (1, 2) else {}))
                                   for i in range(4)]
                            for through in (False, True):
                                name = '连续' if through else '逐站'
                                record = await command('route_save', map='lab_20261021', name=name, points=pts,
                                                       opts=dict(through=through))
                                await command('initial_pose', x=0., y=0., th=0.)
                                await command('arm', on=True)
                                command_log.write_text('')
                                await command('route_start', route_id=record['id'])
                                await until(lambda: (state.get('task', {}).get('task') or {}).get('name') == name
                                            and state['task']['task']['status'] == 'succeeded', name)
                                self.assertEqual(values(), ['true', 'false'])
                                print(f'PASS 真实WebSocket {name}到点后执行 true → false', flush=True)
                                await asyncio.sleep(.2)
                            await command('initial_pose', x=0., y=0., th=0.)
                            await command('arm', on=True)
                            command_log.write_text('')
                            await command('route_start', route_id=record['id'])
                            await until(lambda: values() == ['true'], '进入跨越区间')
                            async with session.post(url+'/api/obstacles-critic/false') as response:
                                self.assertEqual(response.status, 409, '手动请求不能覆盖路线配置')
                            self.assertEqual(values(), ['true'])
                            await command('cancel')
                            await until(lambda: values() == ['true', 'false'], '取消恢复')
                            print('PASS 真实WebSocket 取消后执行 false', flush=True)
                        finally:
                            for task in tasks: task.cancel()
                            await asyncio.gather(*tasks, return_exceptions=True)
            finally:
                process.terminate()
                try:
                    await asyncio.to_thread(process.wait, timeout=8)
                except subprocess.TimeoutExpired:
                    process.kill(); await asyncio.to_thread(process.wait, timeout=3)
                log.close()


if __name__ == '__main__':
    unittest.main()
