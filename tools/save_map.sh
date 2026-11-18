#!/usr/bin/env bash
# 在已加载 ROS 环境的统一容器内保存当前 slam_toolbox 地图。
# 输出 /go2w/data/maps/<地图名>.{pgm,yaml}（给 map_server/AMCL）和 .posegraph/.data（slam_toolbox 续建用）
set -euo pipefail
if [[ $# -ne 1 || ! "$1" =~ ^[A-Za-z0-9_-]{1,48}$ ]]; then
  echo '用法：save_map.sh <合法地图名>' >&2
  exit 2
fi
name=$1
dir=${GO2W_DATA:-/go2w/data}/maps
mkdir -p "$dir"

# --free 0.196：未知格子存成灰度 205，(255-205)/255≈0.196。用默认 0.25 时 map_server 会把 205 读成空地，
# 地图里就没有「未知」了（11-05 实测 lab_20261105 的 32 万个未知格全被当成空地）
ros2 run nav2_map_server map_saver_cli -f "$dir/$name" --free 0.196 --occ 0.65 --ros-args -p save_map_timeout:=10.0
request=$(python3 -c 'import json, sys; print(json.dumps({"filename": sys.argv[1]}))' "$dir/$name")
ros2 service call /slam_toolbox/serialize_map slam_toolbox/srv/SerializePoseGraph "$request"
# Only the directory and files created by this invocation need host ownership.
if [[ $EUID -eq 0 ]]; then
  chown 1000:1000 "$dir"
  for path in "$dir/$name".{pgm,yaml,posegraph,data}; do
    [[ ! -e "$path" ]] || chown 1000:1000 "$path"
  done
fi
ls -l "$dir/$name".*
