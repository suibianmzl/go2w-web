#!/usr/bin/env python3
"""Regression tests for the cloud gateway and robot edge gateway."""
import asyncio
import json
import sys
import tempfile
from pathlib import Path

from aiohttp import WSMsgType, web
from aiohttp.client import ClientSession


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'server'))
sys.path.insert(0, str(ROOT / 'tools'))

from cloud_gateway import CloudGateway, T_CAM, T_MAP  # noqa: E402
from rtc_token import PRIV_PUBLISH_STREAM, PRIV_SUBSCRIBE_STREAM, parse as parse_rtc_token  # noqa: E402
import go2w_edge_gateway as edge  # noqa: E402


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
        rtc_app_id, rtc_app_key = '123456781234567812345678', 'test-app-key'
        gateway = CloudGateway(tmp, rtc_app_id, rtc_app_key, 'go2w-01', 3600)
        runner, port = await start_app(gateway.app())
        base = f'http://127.0.0.1:{port}'
        try:
            async with ClientSession() as session:
                response = await session.get(base + '/quadruped/')
                assert response.status == 200
                assert 'Go2W' in await response.text()
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
                assert status == {'t': 'robot_status', 'online': True, 'robot_id': 'go2w-01'}

                await robot.send_str('{"t":"state","armed":false,"booting":true}')
                state = await browser_reader.text(lambda m: m.get('t') == 'state')
                assert state['booting'] is True

                await robot.send_bytes(bytes([T_MAP, 9, 8, 7]))
                assert await browser_reader.binary(T_MAP) == bytes([T_MAP, 9, 8, 7])

                camera = await session.ws_connect(base + '/quadruped/camera/ws')
                camera_reader = Reader(camera)
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

                await browser.close()
                cancel = await robot_reader.text(lambda m: m.get('t') == 'cmd' and m.get('op') == 'cancel')
                lock = await robot_reader.text(lambda m: m.get('t') == 'cmd' and m.get('op') == 'arm')
                assert cancel['op'] == 'cancel' and lock['on'] is False

                await browser_reader.close()
                browser2 = await session.ws_connect(base + '/quadruped/ws')
                browser2_reader = Reader(browser2)
                status = await browser2_reader.text(lambda m: m.get('t') == 'robot_status')
                assert status['online'] is True
                await robot.close()
                status = await browser2_reader.text(lambda m: m.get('t') == 'robot_status')
                assert status['online'] is False
                assert not gateway.text_cache and not gateway.binary_cache
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


async def test_edge_gateway(forward_camera=False):
    local_messages, cloud_messages = asyncio.Queue(), asyncio.Queue()
    connected = asyncio.Event()

    async def local_handler(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_bytes(bytes([T_CAM, 1]))
        await ws.send_bytes(bytes([T_MAP, 2, 3]))
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
            camera_frame = await asyncio.wait_for(cloud_messages.get(), 3)
            assert camera_frame == ('binary', bytes([T_CAM, 1]))
        mapped = await asyncio.wait_for(cloud_messages.get(), 3)
        assert mapped == ('binary', bytes([T_MAP, 2, 3]))
        state = await asyncio.wait_for(cloud_messages.get(), 3)
        assert state == ('text', {'t': 'state', 'armed': False})

        if forward_camera:
            camera_on = await asyncio.wait_for(local_messages.get(), 3)
            assert camera_on == ('text', {'t': 'cam', 'on': True})
        command = await asyncio.wait_for(local_messages.get(), 3)
        assert command == ('text', {'t': 'cmd', 'id': 1, 'op': 'cancel'})
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
    await test_edge_gateway()
    print('PASS edge gateway: hello, text/binary relay, camera filtering')
    await test_edge_gateway(forward_camera=True)
    print('PASS edge gateway: camera forwarding for cloud RTC publisher')
    print('\n全部通过')


if __name__ == '__main__':
    asyncio.run(main())
