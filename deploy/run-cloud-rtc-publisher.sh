#!/usr/bin/env bash
set -euo pipefail

browser=$(command -v google-chrome-stable || command -v google-chrome)
gateway_url=${GO2W_GATEWAY_URL:-http://127.0.0.1:8080}
robot_id=$(
  python3 -c '
import json
import sys
import urllib.request

with urllib.request.urlopen(sys.argv[1], timeout=5) as response:
    payload = json.load(response)
robots = [
    robot for robot in payload.get("robots", [])
    if robot.get("online") and robot.get("video_upload_enabled") is not False
]
robots.sort(key=lambda robot: (
    -int(robot.get("browsers") or 0),
    str(robot.get("robot_id") or ""),
))
if not robots:
    raise SystemExit("no online robot")
print(robots[0]["robot_id"])
' "${gateway_url%/}/quadruped/health"
)
if [[ ! "$robot_id" =~ ^[A-Za-z0-9_-]{1,32}$ ]]; then
  echo "invalid online robot id: $robot_id" >&2
  exit 1
fi
profile=$(mktemp -d /tmp/go2w-cloud-rtc-chrome.XXXXXX)
trap 'rm -rf "$profile"' EXIT
publisher_url="http://127.0.0.1:8091/rtc_publisher.html?ws=ws%3A%2F%2F127.0.0.1%3A8080%2Fquadruped%2Fcamera%2Fws&cloud=http%3A%2F%2F127.0.0.1%3A8080%2Fquadruped&robot=${robot_id}"

"$browser" \
  --headless \
  --no-sandbox \
  --disable-dev-shm-usage \
  --autoplay-policy=no-user-gesture-required \
  --disable-background-timer-throttling \
  --disable-backgrounding-occluded-windows \
  --disable-renderer-backgrounding \
  --user-data-dir="$profile" \
  "$publisher_url"
