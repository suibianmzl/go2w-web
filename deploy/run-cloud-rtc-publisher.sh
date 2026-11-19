#!/usr/bin/env bash
set -euo pipefail

browser=$(command -v google-chrome-stable || command -v google-chrome)
robot_id=${GO2W_DEFAULT_ROBOT_ID:-go2w-01}
if [[ ! "$robot_id" =~ ^[A-Za-z0-9_-]{1,32}$ ]]; then
  echo "invalid GO2W_DEFAULT_ROBOT_ID: $robot_id" >&2
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
