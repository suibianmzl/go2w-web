#!/usr/bin/env python3
"""Connect the robot-local Go2W WebSocket to the cloud gateway."""
import argparse
import asyncio
import json
import os
import signal
import struct
import sys
from datetime import datetime

import aiohttp


T_CAM = 7


def log(message):
    print(f"{datetime.now().astimezone().isoformat(timespec='seconds')} {message}", flush=True)


def drop_cloud_text(data):
    try:
        return json.loads(data).get('t') == 'cam'
    except (TypeError, ValueError):
        return False


def drop_local_binary(data):
    return bool(data) and data[0] == T_CAM


def add_robot_id_text(data, robot_id):
    try:
        message = json.loads(data)
    except (TypeError, ValueError):
        return data
    if not isinstance(message, dict):
        return data
    existing = message.get('robot_id')
    if existing is not None and existing != robot_id:
        return None
    message['robot_id'] = robot_id
    return json.dumps(message, ensure_ascii=False, separators=(',', ':'))


def add_robot_id_binary(data, robot_id):
    if len(data) < 5:
        return data
    metadata_length = struct.unpack_from('<I', data, 1)[0]
    metadata_end = 5 + metadata_length
    if metadata_end > len(data):
        return data
    try:
        metadata = json.loads(data[5:metadata_end].decode('utf-8'))
    except (UnicodeDecodeError, ValueError):
        return data
    if not isinstance(metadata, dict):
        return data
    existing = metadata.get('robot_id')
    if existing is not None and existing != robot_id:
        return None
    metadata['robot_id'] = robot_id
    packed = json.dumps(metadata, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    return bytes([data[0]]) + struct.pack('<I', len(packed)) + packed + data[metadata_end:]


async def pipe(source, destination, cloud_to_local, robot_id, channel):
    async for message in source:
        if message.type == aiohttp.WSMsgType.TEXT:
            if channel == 'video':
                if not cloud_to_local or not drop_cloud_text(message.data):
                    continue
            elif cloud_to_local and drop_cloud_text(message.data):
                continue
            data = add_robot_id_text(message.data, robot_id)
            if data is None:
                log(f"dropped text with mismatched robot_id={robot_id}")
                continue
            await destination.send_str(data)
        elif message.type == aiohttp.WSMsgType.BINARY:
            is_camera = drop_local_binary(message.data)
            if channel == 'video':
                if cloud_to_local or not is_camera:
                    continue
            elif is_camera:
                continue
            data = add_robot_id_binary(message.data, robot_id)
            if data is None:
                log(f"dropped binary with mismatched robot_id={robot_id}")
                continue
            await destination.send_bytes(data)
        elif message.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED):
            return
        elif message.type == aiohttp.WSMsgType.ERROR:
            raise RuntimeError(f"WebSocket error: {source.exception()!r}")


async def run_once(local_url, cloud_url, robot_id, channel):
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=10)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        log(f"connecting {channel} local {local_url}")
        async with session.ws_connect(local_url, heartbeat=20, max_msg_size=16 * 1024 * 1024) as local:
            log(f"connecting {channel} cloud {cloud_url}")
            async with session.ws_connect(cloud_url, heartbeat=20, max_msg_size=16 * 1024 * 1024) as cloud:
                await cloud.send_str(json.dumps({
                    'type': 'hello', 'robot_id': robot_id, 'protocol': 1, 'channel': channel,
                }, separators=(',', ':')))
                log(f"connected {channel} robot_id={robot_id}")
                tasks = [
                    asyncio.create_task(
                        pipe(local, cloud, False, robot_id, channel),
                        name=f'{channel}-local-to-cloud',
                    ),
                    asyncio.create_task(
                        pipe(cloud, local, True, robot_id, channel),
                        name=f'{channel}-cloud-to-local',
                    ),
                ]
                try:
                    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        error = task.exception()
                        if error:
                            raise error
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)


async def run_channel(name, local_url, cloud_url, robot_id, retry, stop_event):
    while stop_event is None or not stop_event.is_set():
        try:
            await run_once(local_url, cloud_url, robot_id, name)
            log(f"{name} connection closed")
        except asyncio.CancelledError:
            raise
        except Exception as error:
            log(f"{name} connection failed: {error!r}")
        if stop_event is None:
            await asyncio.sleep(retry)
        else:
            try:
                await asyncio.wait_for(stop_event.wait(), retry)
            except asyncio.TimeoutError:
                pass


async def run_gateway(
    local_url,
    cloud_url,
    robot_id,
    retry,
    stop_event=None,
    forward_camera=False,
    cloud_video_url=None,
):
    tasks = [
        asyncio.create_task(
            run_channel('control', local_url, cloud_url, robot_id, retry, stop_event),
            name='control-gateway',
        )
    ]
    if forward_camera:
        if not cloud_video_url:
            raise ValueError('cloud_video_url is required when camera forwarding is enabled')
        tasks.append(asyncio.create_task(
            run_channel('video', local_url, cloud_video_url, robot_id, retry, stop_event),
            name='video-gateway',
        ))
    try:
        if stop_event is None:
            await asyncio.gather(*tasks)
        else:
            await stop_event.wait()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--local', default=os.environ.get('GO2W_EDGE_LOCAL_WS', 'ws://127.0.0.1:8080/ws'))
    parser.add_argument('--cloud', default=os.environ.get(
        'GO2W_CLOUD_WS', 'wss://fleet.evoxrobot.com/quadruped/robot/ws'))
    parser.add_argument('--cloud-video', default=os.environ.get(
        'GO2W_CLOUD_VIDEO_WS', 'wss://fleet.evoxrobot.com/quadruped/robot/video/ws'))
    parser.add_argument('--robot-id', default=os.environ.get('GO2W_ROBOT_ID', 'go2w-01'))
    parser.add_argument('--retry', type=float, default=float(os.environ.get('GO2W_EDGE_RETRY', '3')))
    parser.add_argument('--forward-camera', action='store_true',
                        default=os.environ.get('GO2W_EDGE_FORWARD_CAMERA') == '1')
    args = parser.parse_args()

    async def run():
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except NotImplementedError:
                pass
        await run_gateway(
            args.local,
            args.cloud,
            args.robot_id,
            args.retry,
            stop,
            args.forward_camera,
            args.cloud_video,
        )

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == '__main__':
    sys.exit(main())
