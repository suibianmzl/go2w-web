#!/usr/bin/env bash
set -euo pipefail

browser=""
for candidate in chromium-browser chromium google-chrome google-chrome-stable firefox; do
  if command -v "$candidate" >/dev/null 2>&1; then
    browser=$(command -v "$candidate")
    break
  fi
done
if [ -z "$browser" ]; then
  echo "Chromium/Chrome/Firefox is required for the RTC publisher" >&2
  exit 1
fi

profile=$(mktemp -d /tmp/go2w-rtc-browser.XXXXXX)
trap 'rm -rf "$profile"' EXIT

if [ "$(basename "$browser")" = firefox ]; then
  "$browser" \
    --headless \
    --new-instance \
    --no-remote \
    --profile "$profile" \
    http://127.0.0.1:8090/rtc_publisher.html
  exit $?
fi

"$browser" \
  --headless \
  --no-sandbox \
  --disable-dev-shm-usage \
  --autoplay-policy=no-user-gesture-required \
  --disable-background-timer-throttling \
  --disable-backgrounding-occluded-windows \
  --disable-renderer-backgrounding \
  --user-data-dir="$profile" \
  http://127.0.0.1:8090/rtc_publisher.html
