"""禁行区（v1.1.8）后端测试：python3 dev/test_keepout.py

不需要 ROS。掩膜按 Nav2 Humble 上游的换算走一遍（src/navigation/navigation2 里核对过）：
  map_io.cpp  MapMode::Raw：像素 0–100 原样当占用值，其他（255）→ -1 未知
  costmap_2d.cpp Costmap2D(OccupancyGrid)：-1 → 255 NO_INFORMATION，其余 round(v × 254 / 100)
  keepout_filter.cpp：掩膜 255 跳过；否则掩膜 > 原值（或原值是未知）就写进去
  theta_star.hpp：代价 < 252 才能走；MPPI consider_footprint：机身碰到 254 算碰撞
"""
import json
import math
import os
import sys
import tempfile

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'server'))
import keepout as K   # noqa: E402

PASS = 0


def ok(cond, what):
    global PASS
    assert cond, what
    PASS += 1
    print('PASS', what)


def costmap_from_raw(px):
    occ = np.where(px <= 100, px.astype(np.int16), -1)
    return np.where(occ < 0, 255, np.rint(occ * 254 / 100)).astype(np.int16)


def apply_filter(master, mask):
    out = master.copy()
    m = mask != 255
    out[m & ((mask > out) | (out == 255))] = mask[m & ((mask > out) | (out == 255))]
    return out


def cell(px_ox, px_oy, x, y):
    return int(math.floor((y - px_oy) / K.RES)), int(math.floor((x - px_ox) / K.RES))


# ---------- 校验 ----------
line = {'kind': 'line', 'pts': [[0, 0], [3, 0]], 'name': '  东侧   路牙 '}
zone = {'kind': 'zone', 'pts': [[5, 5], [7, 5], [7, 7], [5, 7], [5, 5]]}
clean = K.validate([line, zone])
ok(clean[0]['name'] == '东侧 路牙' and len(clean[1]['pts']) == 4, '名字整理空白；区域首尾重复点去掉')
for bad, why in [([{'kind': 'wall', 'pts': [[0, 0], [1, 1]]}], '类型'),
                 ([{'kind': 'line', 'pts': [[0, 0]]}], '线只有 1 个点'),
                 ([{'kind': 'zone', 'pts': [[0, 0], [1, 0]]}], '区域只有 2 个点'),
                 ([{'kind': 'line', 'pts': [[0, 0], [float('nan'), 1]]}], 'NaN'),
                 ([{'kind': 'line', 'pts': [[0, 0], [True, 1]]}], 'bool 坐标'),
                 ([{'kind': 'line', 'pts': [[0, 0], [1e9, 1]]}], '坐标太大'),
                 ([{'kind': 'line', 'pts': [[0, 0], [1, 1]], 'name': '<b>'}], '名字有 <'),
                 ('x', '不是列表'),
                 ([{'kind': 'line', 'pts': [[0, 0], [1, 1]]}] * (K.MAX_SHAPES + 1), '太多')]:
    try:
        K.validate(bad)
        raise AssertionError('没拒绝：' + why)
    except ValueError:
        pass
ok(True, '拒绝坏形状（类型、点数、NaN、bool、超范围、名字、数量）')

# ---------- 渲染 ----------
px, ox, oy = K.render([K.validate([line])[0]])
cm = costmap_from_raw(px)
j, i = cell(ox, oy, 1.5, 0.0)
ok(cm[j, i] == 254, '线上是致命 254')
j2, _ = cell(ox, oy, 1.5, 0.2)
ok(cm[j2, i] == 251, '离线 0.2 m（机身半宽以内）是 251：规划器能过但很贵，低于膨胀层真实障碍的 253')
j3, _ = cell(ox, oy, 1.5, 0.5)
expect = 252 * math.exp(-K.SCALE * (0.5 + 0.025 - K.INSCRIBED))   # 格中心在 0.525 m
ok(abs(cm[j3, i] - expect) <= 3, f'0.5 m 处按膨胀层公式衰减（{cm[j3, i]} ≈ {expect:.0f}）')
j4, _ = cell(ox, oy, 1.5, 0.72)
ok(cm[j4, i] == 255, '超过膨胀半径是未知（KeepoutFilter 跳过，不会把地图未知区改成空地）')
ok((cm[cm != 255] >= 1).all(), '掩膜里没有 0：不会把任何格降成空地')

# 横穿：沿 x=1.5 从 y=-1 走到 1，必须碰到 ≥252 的格（Theta* 过不去）
col = cm[:, i]
known = col[col != 255]
ok(known.max() == 254 and ((known >= 252) & (known <= 254)).sum() >= 3, '致命核心至少 3 格宽（5 cm 分辨率下 15 cm）')

# 斜线也是连续的：45° 线上任意一点，4 邻接的致命格不断
px, ox, oy = K.render(K.validate([{'kind': 'line', 'pts': [[0, 0], [2, 1.3]]}]))
cm = costmap_from_raw(px)
lethal = cm == 254
gaps = 0
for t in np.linspace(0.05, 0.95, 60):
    j, i = cell(ox, oy, 2 * t, 1.3 * t)
    gaps += not lethal[j, i]
ok(gaps == 0, '斜线上每个采样点都是致命格')
# 4 连通：任意一个致命格往线方向前进，不会被斜着漏过去 —— 用 Bresenham 横穿检查
crossings = 0
for x0 in np.linspace(0.3, 1.7, 15):
    y_on = 0.65 * x0
    hit = False
    for yy in np.arange(y_on - 1, y_on + 1, 0.01):
        j, i = cell(ox, oy, x0 + 0.3 * (yy - y_on), yy)
        if 0 <= j < cm.shape[0] and 0 <= i < cm.shape[1] and 252 <= cm[j, i] <= 254:
            hit = True
            break
    crossings += hit
ok(crossings == 15, '斜着横穿斜线也一定碰到 ≥252 的格')

# 区域内部致命，外面衰减
px, ox, oy = K.render(clean[1:])
cm = costmap_from_raw(px)
ok(cm[cell(ox, oy, 6, 6)] == 254 and cm[cell(ox, oy, 6, 7.4)] < 252 and cm[cell(ox, oy, 6, 7.4)] != 255, '区域内部 254，外边 0.4 m 处可通行')

# 叠到原图上：原图未知格、空地格都被抬高；原本更高的（真实墙）不降
master = np.full(cm.shape, 0, dtype=np.int16)
master[:, :3] = 255
master[cell(ox, oy, 6, 6)] = 254
out = apply_filter(master, costmap_from_raw(px))
mk = costmap_from_raw(px)
known = master != 255
ok((out[known] >= master[known]).all() and (out[(master == 255) & (mk != 255)] == mk[(master == 255) & (mk != 255)]).all()
   and out[cell(ox, oy, 6, 6)] == 254, 'KeepoutFilter 叠加：已知格只抬高不降低，原图未知格换成掩膜值')

# 空掩膜
px, ox, oy = K.render([])
ok(px.shape == (1, 1) and px[0, 0] == 255, '没有形状 → 1×1 全未知的空掩膜（导航照样能加载）')

try:
    K.render(K.validate([{'kind': 'line', 'pts': [[0, 0], [500, 500]]}]))
    raise AssertionError('太大没拒绝')
except ValueError:
    ok(True, '铺得太开（500 m）拒绝，不会写出几百 MB 的掩膜')

# ---------- 文件 ----------
with tempfile.TemporaryDirectory() as d:
    shapes, path = K.save(d, 'lab', [line, zone], 'test')
    ok(os.path.exists(K.json_path(d, 'lab')) and path == K.mask_path(d, 'lab'), '保存写 json 和掩膜')
    y = open(path).read()
    ok('mode: raw' in y and 'image: lab.pgm' in y and 'resolution: 0.05' in y, '掩膜 yaml：raw 模式、相对图片名')
    raw = open(path[:-5] + '.pgm', 'rb').read()
    hdr = raw.split(b'\n', 3)
    w, h = map(int, hdr[1].split())
    ok(hdr[0] == b'P5' and len(hdr[3]) == w * h, 'PGM 头和像素数一致')
    # PGM 第一行是地图最上面：和 OccupancyGrid 上下翻转
    img = np.frombuffer(hdr[3], np.uint8).reshape(h, w)[::-1]
    o = [float(v) for v in y.split('origin: [')[1].split(']')[0].split(',')]
    ok(costmap_from_raw(img)[cell(o[0], o[1], 1.5, 0)] == 254, '读回的掩膜（按 map_server 方向翻转）线上还是 254')
    ok(K.load(d, 'lab') == shapes and K.load(d, 'nope') == [], 'load 读回；没有文件 → []')
    with open(K.json_path(d, 'bad'), 'w') as f:
        f.write('{坏')
    ok(K.load(d, 'bad') == [], '坏 json → []，不挡导航')
    os.utime(path, (1, 1))
    K.ensure_mask(d, 'lab')
    ok(os.path.getmtime(path) > 1, 'json 比掩膜新 → ensure_mask 重新渲染')
    p2 = K.ensure_mask(d, 'other')
    ok(os.path.exists(p2) and 'mode: raw' in open(p2).read(), '没有禁行区的图也能拿到空掩膜')
    K.save(d, 'lab', [], 'test')
    ok(not os.path.exists(K.json_path(d, 'lab')) and os.path.exists(path), '清空 → 删 json，掩膜变空的（导航热加载后禁行区消失）')
    K.remove_mask(d, 'lab')
    ok(not os.path.exists(path), 'remove_mask 删掩膜')
    ok(not [f for f in os.listdir(os.path.join(d, 'keepout')) if f.startswith('.')], '没留下临时文件')

# ---------- 站点检查 ----------
ok(K.hit(clean, 1.0, 0.2) == 0 and K.hit(clean, 1.0, 0.4) is None, '离线 0.2 m 算挡住，0.4 m 不算')
ok(K.hit(clean, 6, 6) == 1 and K.hit(clean, 8, 8) is None, '区域内部挡住，外面不挡')

print(f'\n全部通过（{PASS} 项）')
