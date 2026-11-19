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


def target_robot(health):
    robots = [
        robot for robot in health.get('robots', [])
        if robot.get('online') and robot.get('video_upload_enabled') is not False
    ]
    robots.sort(key=lambda robot: (
        -int(robot.get('browsers') or 0),
        str(robot.get('robot_id') or ''),
    ))
    return robots[0] if robots else None


def main():
    try:
        health = read_json(HEALTH_URL)
    except Exception:
        return

    robot = target_robot(health)
    publisher = robot.get('rtc_publisher', {}) if robot else {}
    healthy = bool(
        robot
        and robot.get('camera_source')
        and int(publisher.get('videoWidth') or 0) > 0
        and int(publisher.get('readyState') or 0) >= 2
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
