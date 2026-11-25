#!/usr/bin/env python3
"""Export a rotated navigation map while retaining the source map and pose graph."""
import argparse
import ast
import fcntl
import math
import os
from pathlib import Path
import re
import tempfile

import numpy as np

from map_to_png import read_pgm, write_png

NAME = re.compile(r'^[A-Za-z0-9_-]{1,48}$')


def export_map(directory, source, target, angle):
    directory = Path(directory).resolve()
    if not all(NAME.fullmatch(name) for name in (source, target)) or source == target:
        raise ValueError('源地图和目标地图需使用不同的合法名字')
    if not math.isfinite(angle) or abs(angle) > math.pi / 2 + 1e-9:
        raise ValueError('旋转角必须在 -90° 到 90° 之间')
    source_yaml = directory / (source + '.yaml')
    if source_yaml.resolve().parent != directory:
        raise ValueError('源地图必须在地图目录内')
    metadata = {}
    for line in source_yaml.read_text().splitlines():
        # 地图配置均为顶层标量；块式示教中的image等子键不能覆盖真正底图配置。
        if line[:1].isspace():
            continue
        if ':' in line and not line.lstrip().startswith('#'):
            key, value = line.split(':', 1)
            metadata[key.strip()] = value.strip()
    # 导出改变了栅格和坐标，不能把原图已校验的活动示教移植到新地图。
    metadata.pop('go2w_taught_route', None)
    image_name = metadata['image']
    if image_name.startswith(('"', "'")):
        image_name = ast.literal_eval(image_name)
    image_path = (directory / image_name).resolve()
    if image_path.parent != directory:
        raise ValueError('地图图片必须在地图目录内')
    if metadata.get('mode', 'trinary') != 'trinary':
        raise ValueError('方向对齐目前支持 trinary 栅格地图')
    resolution = float(metadata['resolution'])
    origin = ast.literal_eval(metadata['origin'])
    free = float(metadata.get('free_thresh', '0.25'))
    occupied = float(metadata.get('occupied_thresh', '0.65'))
    negate = int(metadata.get('negate', '0'))
    if (len(origin) != 3 or not all(math.isfinite(v) for v in origin)
            or not math.isfinite(resolution) or resolution <= 0
            or not 0 <= free < occupied <= 1 or negate not in (0, 1)):
        raise ValueError('源地图参数无效')
    image = read_pgm(str(image_path))
    height, width = image.shape
    yaw = origin[2] + angle
    cosine, sine = math.cos(yaw), math.sin(yaw)
    rotation = np.array([[cosine, -sine], [sine, cosine]])
    align_rotation = np.array([[math.cos(angle), -math.sin(angle)],
                               [math.sin(angle), math.cos(angle)]])
    rotated_origin = align_rotation @ np.array(origin[:2])
    corners = np.array([[0, 0], [width, 0], [0, height], [width, height]]) * resolution
    corners = corners @ rotation.T + rotated_origin
    lower, upper = corners.min(axis=0), corners.max(axis=0)
    out_width, out_height = np.ceil((upper - lower) / resolution - 1e-9).astype(int)
    if min(out_width, out_height) < 1 or out_width * out_height > 16_000_000:
        raise ValueError('旋转后地图尺寸超出支持范围（最多 1600 万像素）')
    probability = (free + occupied) / 2
    unknown = round(255 * (probability if negate else 1 - probability))
    output = np.full((out_height, out_width), unknown, dtype=np.uint8)
    # Work in bounded row chunks. PGM row zero is the top of the world-space grid.
    xs = lower[0] + (np.arange(out_width) + 0.5) * resolution
    for start in range(0, out_height, 128):
        end = min(start + 128, out_height)
        ys = lower[1] + (out_height - np.arange(start, end) - 0.5) * resolution
        dx, dy = xs[None, :] - rotated_origin[0], ys[:, None] - rotated_origin[1]
        gx = np.floor((cosine * dx + sine * dy) / resolution).astype(int)
        gy = np.floor((-sine * dx + cosine * dy) / resolution).astype(int)
        valid = (gx >= 0) & (gx < width) & (gy >= 0) & (gy < height)
        rows = output[start:end]
        rows[valid] = image[height - 1 - gy[valid], gx[valid]]

    metadata.update(image=target + '.pgm', mode='trinary',
                    origin=f'[{lower[0]:.12g}, {lower[1]:.12g}, 0.0]')
    # A directory-wide advisory lock also releases automatically if the process dies.
    with (directory / '.alignment.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        destinations = [directory / (target + suffix) for suffix in ('.pgm', '.png', '.yaml')]
        if any(path.exists() for path in destinations):
            raise FileExistsError(f'目标地图 {target} 已存在，请换一个名字')
        published = []
        try:
            with tempfile.TemporaryDirectory(prefix='.align-', dir=directory) as temporary:
                stage = Path(temporary)
                (stage / (target + '.pgm')).write_bytes(
                    f'P5\n{out_width} {out_height}\n255\n'.encode() + output.tobytes())
                write_png(str(stage / (target + '.png')), output)
                (stage / (target + '.yaml')).write_text(
                    ''.join(f'{key}: {value}\n' for key, value in metadata.items()))
                # Publish YAML last. Readers discover only complete map groups.
                for destination in destinations:
                    os.link(stage / destination.name, destination)
                    published.append(destination)
        except BaseException:
            for path in published:
                path.unlink()
            raise
    return int(out_width), int(out_height)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory')
    parser.add_argument('source')
    parser.add_argument('target')
    parser.add_argument('angle', type=float, help='Counterclockwise rotation in radians')
    args = parser.parse_args()
    try:
        size = export_map(args.directory, args.source, args.target, args.angle)
    except (ValueError, OSError, KeyError, SyntaxError) as error:
        parser.exit(1, f'地图对齐失败：{error}\n')
    print(f'{args.target}：{size[0]}×{size[1]}，原始地图 {args.source} 保留用于续建')


if __name__ == '__main__':
    main()
