#!/usr/bin/env python3
"""Ensure the multi-robot RTC publisher supervisor is running."""
import subprocess


SERVICE = 'go2w-cloud-rtc-publisher.service'


def main():
    active = subprocess.run(
        ['/bin/systemctl', 'is-active', '--quiet', SERVICE],
        check=False,
    )
    if active.returncode == 0:
        return
    subprocess.run(
        ['/bin/systemctl', 'start', SERVICE],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


if __name__ == '__main__':
    main()
