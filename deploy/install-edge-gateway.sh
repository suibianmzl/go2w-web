#!/usr/bin/env bash
# Run on the robot from a checked-out or copied go2w-web tree.
set -euo pipefail

root=$(cd "$(dirname "$0")/.." && pwd)
target="$HOME/go2w-edge-gateway"
install -d "$target"
install -m 0755 "$root/tools/go2w_edge_gateway.py" "$target/"
if [ ! -x "$target/.venv/bin/python" ]; then
  python3 -m venv "$target/.venv"
fi
"$target/.venv/bin/pip" install -q -r "$root/deploy/requirements-edge.txt"

agent_target="$HOME/go2w-ssh-agent"
install -d "$agent_target/ssh_portbroker"
install -m 0755 "$root/tools/ssh_portbroker_agent.py" "$agent_target/"
install -m 0755 "$root/tools/ssh_portbroker/reverse-register.sh" "$agent_target/"

rtc_target="$HOME/go2w-rtc-publisher"
install -d "$rtc_target/js/vendor"
install -m 0644 "$root/static/rtc_publisher.html" "$rtc_target/"
install -m 0644 "$root/static/js/rtc_publisher.js" "$rtc_target/js/"
install -m 0644 "$root/static/js/vendor/jmuxer.min.js" "$rtc_target/js/vendor/"
install -m 0755 "$root/deploy/run-rtc-publisher.sh" "$rtc_target/"

sudo install -m 0644 "$root/deploy/go2w-edge-gateway.service" /etc/systemd/system/
sudo install -m 0644 "$root/deploy/go2w-ssh-agent.service" /etc/systemd/system/
sudo install -m 0644 "$root/deploy/go2w-rtc-http.service" /etc/systemd/system/
sudo install -m 0644 "$root/deploy/go2w-rtc-publisher.service" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now go2w-edge-gateway.service
if [ -f "$agent_target/id_portbroker_rsa" ]; then
  sudo systemctl enable --now go2w-ssh-agent.service
else
  echo "SSH agent files installed; place id_portbroker_rsa in $agent_target before starting go2w-ssh-agent.service" >&2
fi
sudo systemctl enable --now go2w-rtc-http.service
sudo systemctl enable --now go2w-rtc-publisher.service
systemctl --no-pager --full status go2w-edge-gateway.service go2w-rtc-http.service go2w-rtc-publisher.service go2w-ssh-agent.service
