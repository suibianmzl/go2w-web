#!/usr/bin/env bash
set -euo pipefail

browser=$(command -v google-chrome-stable || command -v google-chrome)
profile=$(mktemp -d /tmp/go2w-cloud-rtc-chrome.XXXXXX)
trap 'rm -rf "$profile"' EXIT

"$browser" \
  --headless \
  --no-sandbox \
  --disable-dev-shm-usage \
  --autoplay-policy=no-user-gesture-required \
  --disable-background-timer-throttling \
  --disable-backgrounding-occluded-windows \
  --disable-renderer-backgrounding \
  --user-data-dir="$profile" \
  'http://127.0.0.1:8091/rtc_publisher.html?ws=ws%3A%2F%2F127.0.0.1%3A8080%2Fquadruped%2Fcamera%2Fws&cloud=http%3A%2F%2F127.0.0.1%3A8080%2Fquadruped&robot=go2w-01'
