"""网页地图编辑（v1.0.2，11-18）：去噪 / 画笔 / 裁剪在网页里做，保存时只把「改了哪些格」发过来。

网页发来的是相对原图的修改：runs = [[起始下标, 长度, 新值], …]，下标按 OccupancyGrid 平铺（行 0 在 origin 一侧，
和网页收到的地图帧一样），新值 0 = 空地、100 = 障碍、-1 = 未知。网页还带上它编辑时那张图的 CRC32
（未知记 255，和地图帧的字节一样），这里用硬盘上的原图算一遍对上了才改——网页看到的图和文件不是同一张就拒绝。

只改动到的像素，其余字节原样保留；yaml 照抄原图（只换 image 一行），所以分辨率、原点、阈值、negate 都不变，
站点坐标和路线在新图上照样有效。写完再按 map_server 的规则读回来逐格核对，不一致就删掉新文件报错。
编辑后的图没有 slam_toolbox 的 .posegraph / .data，不能当续建基准（mapmeta.preflight 会说明）。
"""
import os
import time
import zlib

import numpy as np

import mapfile
import mapmeta as MM

TOOL = 'web-edit'
MAX_RUNS = 400_000
VALUES = (0, 100, -1)


def grid_crc(grid):
    """和网页同一种算法：OccupancyGrid 栅格（未知 → 255）按字节算 CRC32。"""
    g = np.asarray(grid, dtype=np.int16).copy()
    g[g < 0] = 255
    return zlib.crc32(g.astype(np.uint8).tobytes())


def apply_runs(grid, runs):
    """返回 (新栅格, 实际变化的格数)。格式不对抛 ValueError。"""
    flat = np.asarray(grid, dtype=np.int8).reshape(-1).copy()
    n = flat.size
    if not isinstance(runs, list) or not runs:
        raise ValueError('没有修改')
    if len(runs) > MAX_RUNS:
        raise ValueError(f'修改记录太多（{len(runs)} 段，最多 {MAX_RUNS}）')
    for r in runs:
        if not isinstance(r, (list, tuple)) or len(r) != 3 or any(isinstance(v, bool) or not isinstance(v, int) for v in r):
            raise ValueError('修改记录格式不对')
        start, length, value = r
        if value not in VALUES or length <= 0 or start < 0 or start + length > n:
            raise ValueError(f'修改记录越界或取值不对：{r}')
        flat[start:start + length] = value
    new = flat.reshape(np.shape(grid))
    return new, int(np.count_nonzero(new != np.asarray(grid, dtype=np.int8)))


def classify(px, maxv, negate, free_t, occ_t):
    """和 mapfile.load / map_server trinary 一样把像素分成 0 / 100 / -1。"""
    v = np.asarray(px, dtype=np.float32) / maxv
    p = v if negate else 1.0 - v
    g = np.full(np.shape(px), -1, dtype=np.int8)
    g[p > occ_t] = 100
    g[p < free_t] = 0
    return g


def class_pixels(maxv, negate, free_t, occ_t):
    """三类各用哪个像素值。尽量和 map_saver_cli 一样（254 / 0 / 205），并按这张图的阈值核对能读回原类别。"""
    mid = (free_t + occ_t) / 2
    mid_px = int(round((mid if negate else 1.0 - mid) * maxv))
    cand = {0: [maxv - 1, maxv] if not negate else [1, 0],
            100: [0, 1] if not negate else [maxv, maxv - 1],
            -1: [round(205 * maxv / 255) if not negate else round(50 * maxv / 255), mid_px]}
    out = {}
    for cls, options in cand.items():
        for v in options:
            if 0 <= v <= maxv and int(classify(np.array([v]), maxv, negate, free_t, occ_t)[0]) == cls:
                out[cls] = v
                break
        else:
            raise ValueError(f'这张图的阈值（free {free_t}, occupied {occ_t}）下找不到能表示 {cls} 的像素值')
    return out


def _clean_summary(summary):
    """网页报的操作摘要只留已知字段和小整数，写进 meta.json。"""
    out = {}
    if not isinstance(summary, dict):
        return out
    for key in ('free', 'occ', 'unknown', 'crop', 'denoise_blobs', 'denoise_cells', 'denoise_max'):
        v = summary.get(key)
        if isinstance(v, int) and not isinstance(v, bool) and 0 <= v < 10_000_000:
            out[key] = v
    return out


def save(maps_dir, source, name, runs, base_crc, w, h, summary=None, web_version=None):
    """在 source 上应用 runs，另存为 name。返回 (变化格数, meta)。出错抛 ValueError，不留半截文件。"""
    src_yaml = MM.map_path(maps_dir, source)
    if not os.path.isfile(src_yaml):
        raise ValueError(f'data/maps 里没有 {source}')
    for ext in ('.yaml', '.pgm', '.meta.json', '.posegraph', '.data'):
        if os.path.exists(os.path.join(maps_dir, name + ext)):
            raise ValueError(f'已经有叫 {name} 的地图文件（{name}{ext}），换个名字；编辑不会覆盖已有地图')
    meta = mapfile.read_yaml(src_yaml)
    try:
        free_t = float(meta.get('free_thresh', 0.25))
        occ_t = float(meta.get('occupied_thresh', 0.65))
        negate = int(meta.get('negate', 0))
    except ValueError as e:
        raise ValueError(f'{source}.yaml 读不懂：{e}') from e
    if str(meta.get('mode', 'trinary')).strip() not in ('trinary', ''):
        raise ValueError(f'{source} 不是 trinary 模式的地图（mode: {meta.get("mode")}），暂不支持编辑')
    image = meta.get('image') or source + '.pgm'
    px, maxv = mapfile.read_pgm(os.path.join(maps_dir, image))
    hh, ww = px.shape
    if (w, h) != (ww, hh):
        raise ValueError(f'尺寸对不上：网页是 {w}×{h}，文件是 {ww}×{hh}。地图可能已经换了，请重新打开编辑')
    old = classify(px, maxv, negate, free_t, occ_t)[::-1]          # 行 0 在 origin 一侧
    if grid_crc(old) != base_crc:
        raise ValueError(f'网页上编辑的图和硬盘上的 {source} 不一样（可能刚重新保存或续建过），请重新打开编辑')
    new, changed = apply_runs(old, runs)
    if not changed:
        raise ValueError('和原图相比没有任何变化')

    vals = class_pixels(maxv, negate, free_t, occ_t)
    img = np.array(px, dtype=np.uint8, copy=True)                    # read_pgm 只认 8 位
    diff = (new != old)[::-1]                                         # 换回图片行序（行 0 在最上）
    new_img = new[::-1]
    for cls, v in vals.items():
        img[diff & (new_img == cls)] = v

    base = os.path.join(maps_dir, name)
    yaml_lines = []
    with open(src_yaml, encoding='utf-8') as f:
        for line in f:
            yaml_lines.append(f'image: {name}.pgm\n' if line.split('#', 1)[0].strip().startswith('image:') else line)
    if not any(l.startswith('image:') for l in yaml_lines):
        yaml_lines.insert(0, f'image: {name}.pgm\n')
    # 头里不写注释：procs.list_maps 只按空白切头几个字段
    pgm = f'P5\n{ww} {hh}\n{maxv}\n'.encode() + img.tobytes()
    written = []
    try:
        for ext, data in (('.pgm', pgm), ('.yaml', ''.join(yaml_lines).encode('utf-8'))):
            tmp = f'{base}{ext}.tmp'
            with open(tmp, 'wb') as f:
                f.write(data)
            os.replace(tmp, base + ext)
            written.append(base + ext)
        back = mapfile.load(base + '.yaml')['grid']
        if back.shape != new.shape or not np.array_equal(back, new):
            raise ValueError('写出的地图读回来和编辑结果不一致，已删除')
        info = {
            'schema': MM.SCHEMA,
            'name': name,
            'parent': source,
            'edited_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
            'tool': TOOL,
            'changed_cells': changed,
            'edit': _clean_summary(summary),
        }
        if web_version:
            info['web_version'] = str(web_version)[:20]
        MM.write(maps_dir, info)
        written.append(MM.meta_path(maps_dir, name))
    except BaseException:
        for p in written + [f'{base}.pgm.tmp', f'{base}.yaml.tmp']:
            try:
                os.remove(p)
            except OSError:
                pass
        raise
    return changed, info
