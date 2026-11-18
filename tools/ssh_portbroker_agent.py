#!/usr/bin/env python3
"""Register a Go2W robot and keep its reverse SSH tunnel alive."""
import asyncio
import json
import os
import signal
import sys
from dataclasses import dataclass
from pathlib import Path

import aiohttp


def log(message):
    print(f'[go2w-ssh-agent] {message}', flush=True)


class FleetError(RuntimeError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


@dataclass
class AgentConfig:
    fleet_url: str
    robot_id: str
    robot_name: str
    robot_model: str
    serial_number: str
    firmware_version: str
    broker_username: str
    device_username: str
    local_port: int
    script: Path
    identity: Path
    state_dir: Path
    log_dir: Path
    heartbeat: float
    retry: float
    network_check: float

    @classmethod
    def from_env(cls):
        robot_id = os.environ.get('GO2W_ROBOT_ID', 'go2w-01')
        return cls(
            fleet_url=os.environ.get(
                'GO2W_FLEET_URL', 'https://fleet.evoxrobot.com').rstrip('/'),
            robot_id=robot_id,
            robot_name=os.environ.get('GO2W_ROBOT_NAME', robot_id),
            robot_model=os.environ.get('GO2W_ROBOT_MODEL', 'Unitree Go2'),
            serial_number=os.environ.get('GO2W_ROBOT_SERIAL', ''),
            firmware_version=os.environ.get('GO2W_FIRMWARE_VERSION', ''),
            broker_username=(
                os.environ.get('GO2W_PORTBROKER_USER')
                or os.environ.get('GO2W_SSH_USER')
                or 'root'
            ),
            device_username=os.environ.get('GO2W_DEVICE_SSH_USER', 'unitree'),
            local_port=int(os.environ.get('GO2W_SSH_LOCAL_PORT', '22')),
            script=Path(os.environ.get(
                'GO2W_SSH_AGENT_SCRIPT',
                str(Path(__file__).resolve().parent / 'ssh_portbroker' / 'reverse-register.sh'),
            )),
            identity=Path(os.environ.get(
                'GO2W_SSH_AGENT_KEY',
                '/home/unitree/go2w-ssh-agent/id_portbroker_rsa',
            )),
            state_dir=Path(os.environ.get(
                'GO2W_SSH_AGENT_STATE_DIR',
                '/home/unitree/ssh-portbroker-dog/state',
            )),
            log_dir=Path(os.environ.get(
                'GO2W_SSH_AGENT_LOG_DIR',
                '/home/unitree/ssh-portbroker-dog/log',
            )),
            heartbeat=float(os.environ.get('GO2W_SSH_AGENT_HEARTBEAT', '30')),
            retry=float(os.environ.get('GO2W_SSH_AGENT_RETRY', '5')),
            network_check=float(os.environ.get('GO2W_SSH_AGENT_NETWORK_CHECK', '2')),
        )


class FleetClient:
    def __init__(self, base_url):
        self.base_url = base_url
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=12))

    async def close(self):
        await self.session.close()

    async def _request(self, method, path, payload=None):
        try:
            async with self.session.request(
                method,
                self.base_url + path,
                json=payload,
                headers={'Accept': 'application/json'},
            ) as response:
                try:
                    data = await response.json(content_type=None)
                except Exception:
                    data = {'message': await response.text()}
                if response.status >= 400:
                    raise FleetError(
                        response.status,
                        str(data.get('message') or data.get('detail') or f'HTTP {response.status}'),
                    )
                return data
        except aiohttp.ClientError as error:
            raise FleetError(503, f'fleet unavailable: {error}') from error

    async def register(self, config):
        return await self._request(
            'POST',
            f'/internal/v1/robots/{config.robot_id}/register',
            {
                'deviceId': config.robot_id,
                'deviceName': config.robot_name,
                'deviceModel': config.robot_model,
                'serialNumber': config.serial_number or None,
                'firmwareVersion': config.firmware_version or None,
                'username': config.device_username,
            },
        )

    async def heartbeat(self, robot_id):
        return await self._request(
            'POST', f'/internal/v1/robots/{robot_id}/heartbeat')

    async def offline(self, robot_id):
        return await self._request(
            'POST', f'/internal/v1/robots/{robot_id}/offline')


async def terminate(process):
    if process.returncode is not None:
        return
    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), 5)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()


async def run_tunnel(config, target):
    command = [
        str(config.script),
        '--public-host', str(target['publicHost']),
        '--public-ssh-port', str(target['publicSshPort']),
        '--remote-port', str(target['remotePort']),
        '--local-port', str(config.local_port),
        '--user', str(config.broker_username),
        '--identity', str(config.identity),
        '--state-dir', str(config.state_dir),
        '--log-dir', str(config.log_dir),
    ]
    log(
        f"starting tunnel {target['publicHost']}:{target['remotePort']} "
        f"-> 127.0.0.1:{config.local_port}"
    )
    return await asyncio.create_subprocess_exec(*command)


async def heartbeat_loop(client, config):
    failures = 0
    while True:
        await asyncio.sleep(config.heartbeat)
        try:
            await client.heartbeat(config.robot_id)
            failures = 0
        except FleetError as error:
            failures += 1
            log(f'heartbeat failed ({failures}): {error}')
            if error.status == 404 or failures >= 3:
                raise


async def network_fingerprint():
    process = await asyncio.create_subprocess_exec(
        'ip', '-j', 'route', 'get', '1.1.1.1',
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await process.communicate()
    if process.returncode != 0:
        return None
    try:
        route = json.loads(stdout.decode())[0]
    except (IndexError, ValueError):
        return None
    return (
        str(route.get('dev') or ''),
        str(route.get('gateway') or ''),
        str(route.get('prefsrc') or ''),
    )


async def network_watch(config):
    baseline = await network_fingerprint()
    while True:
        await asyncio.sleep(config.network_check)
        current = await network_fingerprint()
        if baseline is None:
            baseline = current
            continue
        if current is None or current != baseline:
            raise RuntimeError(f'network changed: {baseline} -> {current}')


async def run_session(client, config, target):
    process = await run_tunnel(config, target)
    tunnel_wait = asyncio.create_task(process.wait())
    heartbeat = asyncio.create_task(heartbeat_loop(client, config))
    network = asyncio.create_task(network_watch(config))
    try:
        done, _ = await asyncio.wait(
            [tunnel_wait, heartbeat, network], return_when=asyncio.FIRST_COMPLETED)
        if heartbeat in done:
            error = heartbeat.exception()
            if error:
                raise error
        if network in done:
            error = network.exception()
            if error:
                raise error
        return await tunnel_wait
    finally:
        heartbeat.cancel()
        network.cancel()
        await asyncio.gather(heartbeat, network, return_exceptions=True)
        await terminate(process)
        if tunnel_wait.done():
            await asyncio.gather(tunnel_wait, return_exceptions=True)
        else:
            tunnel_wait.cancel()
            await asyncio.gather(tunnel_wait, return_exceptions=True)


async def run_agent():
    config = AgentConfig.from_env()
    if not config.script.is_file():
        raise RuntimeError(f'reverse SSH script not found: {config.script}')
    if not config.identity.is_file():
        raise RuntimeError(f'reverse SSH identity not found: {config.identity}')
    config.state_dir.mkdir(parents=True, exist_ok=True)
    config.log_dir.mkdir(parents=True, exist_ok=True)
    client = FleetClient(config.fleet_url)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    delay = config.retry
    try:
        while not stop.is_set():
            try:
                target = await client.register(config)
                delay = config.retry
                exit_code = await run_session(client, config, target)
                log(f'tunnel exited rc={exit_code}')
            except asyncio.CancelledError:
                raise
            except Exception as error:
                log(f'agent session failed: {error}')
            if stop.is_set():
                break
            try:
                await client.offline(config.robot_id)
            except Exception as error:
                log(f'offline update failed: {error}')
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)
    finally:
        try:
            await client.offline(config.robot_id)
        except Exception as error:
            log(f'final offline update failed: {error}')
        await client.close()


def main():
    try:
        asyncio.run(run_agent())
    except KeyboardInterrupt:
        pass
    except Exception as error:
        print(f'[go2w-ssh-agent] fatal: {error}', file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
