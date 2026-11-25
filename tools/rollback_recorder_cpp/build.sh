#!/bin/bash
# 在 ROS Humble 环境里编译（容器里跑）：bash build.sh <源码目录> <输出文件>
set -e
SRC=${1:-$(dirname "$0")}
OUT=${2:-$SRC/rollback_recorder_cpp}
. /opt/ros/humble/setup.bash
B=$(mktemp -d)
cmake -S "$SRC" -B "$B" -DCMAKE_BUILD_TYPE=Release >/dev/null
cmake --build "$B" -j4
install -m 0755 "$B/rollback_recorder_cpp" "$OUT"
install -m 0644 "$SRC/../mcap_writer.yaml" "$(dirname "$OUT")/mcap_writer.yaml"
rm -rf "$B"
echo "built $OUT"
