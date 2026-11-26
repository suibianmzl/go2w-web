#!/usr/bin/env python3
"""Run one headless RTC publisher per online, video-enabled Go2W robot."""
import asyncio
import json
import logging
import os
import re
import shutil
import signal
import tempfile
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit
from urllib.request import urlopen


LOG = logging.getLogger('go2w_rtc_publisher')
ROBOT_ID = re.compile(r'^[A-Za-z0-9_-]{1,32}$')


@dataclass
class Publisher:
    robot_id: str
    process: asyncio.subprocess.Process
    profile_dir: str
    started_at: float
    log_task: asyncio.Task | None = None
    failures: int = 0


def publisher_url(page_url, gateway_url, robot_id):
    gateway = gateway_url.rstrip('/')
    if gateway.startswith('https://'):
        websocket = 'wss://' + gateway[len('https://'):]
    elif gateway.startswith('http://'):
        websocket = 'ws://' + gateway[len('http://'):]
    else:
        websocket = gateway
    parts = urlsplit(page_url)
    query = dict(parse_qsl(parts.query))
    query.update({
        'ws': websocket + '/quadruped/camera/ws',
        'cloud': gateway + '/quadruped',
        'robot': robot_id,
    })
    return parts._replace(query=urlencode(query)).geturl()


def target_robots(health):
    robots = []
    for robot in health.get('robots', []):
        robot_id = str(robot.get('robot_id') or '')
        if not ROBOT_ID.fullmatch(robot_id):
            continue
        if not robot.get('online') or robot.get('video_upload_enabled') is False:
            continue
        robots.append(robot)
    return sorted(robots, key=lambda robot: robot['robot_id'])


def publisher_healthy(robot):
    publisher = robot.get('rtc_publisher')
    if not isinstance(publisher, dict):
        return False
    return bool(
        robot.get('camera_source')
        and int(publisher.get('videoWidth') or 0) > 0
        and int(publisher.get('readyState') or 0) >= 2
    )


def chrome_binary():
    configured = os.environ.get('GO2W_RTC_CHROME', '').strip()
    if configured:
        return configured
    for name in ('google-chrome-stable', 'google-chrome'):
        found = shutil.which(name)
        if found:
            return found
    raise RuntimeError('Google Chrome executable not found')


def chrome_command(binary, profile_dir, page_url):
    return [
        binary,
        '--headless',
        '--no-sandbox',
        '--disable-dev-shm-usage',
        '--autoplay-policy=no-user-gesture-required',
        '--disable-background-timer-throttling',
        '--disable-backgrounding-occluded-windows',
        '--disable-renderer-backgrounding',
        f'--user-data-dir={profile_dir}',
        page_url,
    ]


class Supervisor:
    def __init__(self):
        self.gateway_url = os.environ.get(
            'GO2W_GATEWAY_URL', 'http://127.0.0.1:8080').rstrip('/')
        self.health_url = self.gateway_url + '/quadruped/health'
        self.page_url = os.environ.get(
            'GO2W_RTC_PUBLISHER_PAGE',
            'http://127.0.0.1:8091/rtc_publisher.html',
        )
        self.poll = max(2.0, float(os.environ.get('GO2W_RTC_PUBLISHER_POLL', '5')))
        self.startup_grace = max(
            10.0, float(os.environ.get('GO2W_RTC_PUBLISHER_GRACE', '45')))
        self.failure_limit = max(
            2, int(os.environ.get('GO2W_RTC_PUBLISHER_FAILURES', '3')))
        self.retry_delay = max(
            1.0, float(os.environ.get('GO2W_RTC_PUBLISHER_RETRY', '3')))
        self.binary = chrome_binary()
        self.publishers = {}
        self.retry_at = {}
        self.last_health_error = 0.0

    def log(self, robot_id, line):
        line = line.rstrip()
        if line:
            LOG.info('[%s] %s', robot_id, line)

    async def read_health(self):
        return await asyncio.to_thread(self._read_health)

    def _read_health(self):
        with urlopen(self.health_url, timeout=8) as response:
            return json.load(response)

    async def pipe_logs(self, robot_id, stream):
        try:
            while True:
                line = await stream.readline()
                if not line:
                    return
                self.log(robot_id, line.decode('utf-8', errors='replace'))
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.log(robot_id, f'log stream failed: {error!r}')

    async def start(self, robot_id):
        profile_dir = tempfile.mkdtemp(prefix=f'go2w-cloud-rtc-{robot_id}-')
        page = publisher_url(self.page_url, self.gateway_url, robot_id)
        process = await asyncio.create_subprocess_exec(
            *chrome_command(self.binary, profile_dir, page),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        publisher = Publisher(robot_id, process, profile_dir, time.monotonic())
        publisher.log_task = asyncio.create_task(
            self.pipe_logs(robot_id, process.stdout),
            name=f'rtc-log-{robot_id}',
        )
        self.publishers[robot_id] = publisher
        self.retry_at.pop(robot_id, None)
        LOG.info('started %s pid=%s', robot_id, process.pid)

    async def stop(self, robot_id, reason):
        publisher = self.publishers.pop(robot_id, None)
        if publisher is None:
            return
        process = publisher.process
        LOG.info('stopping %s pid=%s reason=%s', robot_id, process.pid, reason)
        if process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), 8)
            except asyncio.TimeoutError:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await process.wait()
        if publisher.log_task is not None:
            publisher.log_task.cancel()
            await asyncio.gather(publisher.log_task, return_exceptions=True)
        shutil.rmtree(publisher.profile_dir, ignore_errors=True)
        self.retry_at[robot_id] = time.monotonic() + self.retry_delay

    async def reconcile(self, health):
        now = time.monotonic()
        targets = {robot['robot_id']: robot for robot in target_robots(health)}

        for robot_id in list(self.publishers):
            if robot_id not in targets:
                await self.stop(robot_id, 'robot offline or video disabled')

        for robot_id, robot in targets.items():
            publisher = self.publishers.get(robot_id)
            if publisher is not None and publisher.process.returncode is not None:
                await self.stop(robot_id, f'publisher exited rc={publisher.process.returncode}')
                publisher = None
            if publisher is None:
                if now >= self.retry_at.get(robot_id, 0):
                    await self.start(robot_id)
                continue

            uptime = now - publisher.started_at
            if uptime < self.startup_grace:
                continue
            if publisher_healthy(robot):
                if publisher.failures:
                    LOG.info('%s recovered', robot_id)
                publisher.failures = 0
                continue

            publisher.failures += 1
            LOG.warning(
                '%s unhealthy (%s/%s): camera_source=%s publisher=%s',
                robot_id,
                publisher.failures,
                self.failure_limit,
                robot.get('camera_source'),
                json.dumps(robot.get('rtc_publisher') or {}, ensure_ascii=False)[:800],
            )
            if publisher.failures >= self.failure_limit:
                await self.stop(robot_id, 'publisher unhealthy')

    async def run(self):
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        LOG.info('supervisor started gateway=%s page=%s', self.gateway_url, self.page_url)
        try:
            while not stop.is_set():
                try:
                    health = await self.read_health()
                    await self.reconcile(health)
                except Exception as error:
                    now = time.monotonic()
                    if now - self.last_health_error >= 30:
                        self.last_health_error = now
                        LOG.warning('health/reconcile failed: %r', error)
                try:
                    await asyncio.wait_for(stop.wait(), self.poll)
                except asyncio.TimeoutError:
                    pass
        finally:
            await asyncio.gather(
                *(self.stop(robot_id, 'supervisor shutdown') for robot_id in list(self.publishers)),
                return_exceptions=True,
            )
            LOG.info('supervisor stopped')


def main():
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s %(name)s %(message)s',
    )
    asyncio.run(Supervisor().run())


if __name__ == '__main__':
    main()
