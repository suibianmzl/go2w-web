#!/usr/bin/env python3
"""Restart a stale cloud RTC publisher when its camera WebSocket is missing."""
import json
import subprocess
import urllib.request
from pathlib import Path


HEALTH_URL = 'http://127.0.0.1:8080/quadruped/health'
SERVICE = 'go2w-cloud-rtc-publisher.service'
STATE = Path('/run/go2w-cloud-rtc-watchdog.failures')
FAILURE_LIMIT = 2


def read_json(url):
    with urllib.request.urlopen(url, timeout=5) as response:
        return json.load(response)


def read_failures():
    try:
        return int(STATE.read_text(encoding='ascii').strip())
    except (FileNotFoundError, ValueError):
        return 0


def main():
    try:
        health = read_json(HEALTH_URL)
    except Exception:
        return

    healthy = (
        health.get('robot_online') is True
        and int(health.get('camera_sources') or 0) > 0
    )
    if healthy:
        STATE.write_text('0\n', encoding='ascii')
        return

    failures = read_failures() + 1
    if failures < FAILURE_LIMIT:
        STATE.write_text(f'{failures}\n', encoding='ascii')
        return

    subprocess.run(['/bin/systemctl', 'restart', SERVICE], check=True)
    STATE.write_text('0\n', encoding='ascii')


if __name__ == '__main__':
    main()
