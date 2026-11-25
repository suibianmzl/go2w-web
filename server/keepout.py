"""禁行区（v1.1.8，11-23）：网页上画的禁行线 / 禁行区域 → Nav2 KeepoutFilter 用的掩膜。

和「地图编辑画墙」的区别：不改地图本身。AMCL / 自动定位还用原图匹配，禁行信息单独存，
导航的全局、局部代价地图都会叠加（局部代价地图没有静态层，画墙 MPPI 看不到，禁行层看得到）。

文件（都在 data/maps 下）：
  <地图名>.keepout.json         网页画的形状，世界坐标（map 坐标系，米）。删地图时随 <名字>.* 进回收站
  keepout/<地图名>.yaml / .pgm  由 json 渲染出的掩膜，导航启动时 navigation.launch.py 按地图名找它，
                                网页改完或切换地图时调 /filter_mask_server/load_map 热加载

掩膜用 map_server 的 raw 模式：像素值 0–100 直接是占用值（costmap 里 ×2.54），255 = 未知 = 不管。
KeepoutFilter 只会把代价往高改，而且会把「未知」格改成掩膜值，所以掩膜外面必须是 255，不能是 0
（0 会把地图上的未知区变成空地，规划器就能穿过没建图的地方）。

KeepoutFilter 在膨胀层之后叠加，自己不会被膨胀，所以这里按 nav2.yaml 的膨胀参数自己画一圈衰减：
  线本身（半宽 LINE_HALF）或区域内部  100 → 254 致命：MPPI 按机身轮廓检查，机身任何部分碰到都算撞
  离线 ≤ INSCRIBED（半个机身宽）     99 → 251：规划器能过但很贵（真实障碍这里是 253）
  INSCRIBED～INFLATION               252·exp(−SCALE·(d − INSCRIBED))，和膨胀层同一个公式，
                                     MPPI 的 ObstaclesCritic 从代价反推距离时也是对的
"""
import json
import math
import os
import tempfile
import time

import numpy as np

RES = 0.05
LINE_HALF = 0.075          # 致命核心半宽：线画成约 15 cm 宽，MPPI 0.05 s 一步不会跨过去
INSCRIBED = 0.26           # footprint 半宽 0.24 + footprint_padding 0.02
INFLATION = 0.70           # nav2.yaml inflation_radius
SCALE = 2.5                # nav2.yaml cost_scaling_factor
MAX_SHAPES, MAX_PTS, MAX_CELLS, MAX_COORD = 100, 400, 4_000_000, 10000.0
NAME_MAX = 24
SUBDIR = 'keepout'


def json_path(maps_dir, name):
    return os.path.join(maps_dir, f'{name}.keepout.json')


def mask_path(maps_dir, name):
    return os.path.join(maps_dir, SUBDIR, f'{name}.yaml')


def validate(shapes):
    """网页发来的形状 → 干净的列表；不对就抛 ValueError（中文，直接给网页看）。"""
    if not isinstance(shapes, list):
        raise ValueError('禁行区格式不对')
    if len(shapes) > MAX_SHAPES:
        raise ValueError(f'禁行线 / 区域最多 {MAX_SHAPES} 个')
    out = []
    for k, s in enumerate(shapes, 1):
        if not isinstance(s, dict) or s.get('kind') not in ('line', 'zone'):
            raise ValueError(f'第 {k} 个形状类型不对')
        pts = s.get('pts')
        if not isinstance(pts, list) or len(pts) > MAX_PTS:
            raise ValueError(f'第 {k} 个形状的点太多（最多 {MAX_PTS} 个）')
        clean = []
        for p in pts:
            if (not isinstance(p, (list, tuple)) or len(p) != 2 or
                    not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
                            and abs(v) <= MAX_COORD for v in p)):
                raise ValueError(f'第 {k} 个形状有坐标不对')
            q = [round(float(p[0]), 3), round(float(p[1]), 3)]
            if not clean or q != clean[-1]:      # 连续重复点（双击）去掉
                clean.append(q)
        if s['kind'] == 'zone' and len(clean) > 2 and clean[0] == clean[-1]:
            clean.pop()
        need = 2 if s['kind'] == 'line' else 3
        if len(clean) < need:
            raise ValueError(f'第 {k} 个{"禁行线至少要 2 个点" if need == 2 else "禁行区域至少要 3 个点"}')
        name = s.get('name') or ''
        if not isinstance(name, str):
            raise ValueError(f'第 {k} 个形状的名字不对')
        name = ' '.join(name.split())[:NAME_MAX]
        if any(ch in name for ch in '<>"\'\\') or any(ord(ch) < 32 for ch in name):
            raise ValueError(f'第 {k} 个形状的名字不能有 < > " \' \\ 和控制字符')
        out.append({'kind': s['kind'], 'pts': clean, **({'name': name} if name else {})})
    return out


def load(maps_dir, name):
    """地图的禁行形状；没有 / 坏了都返回 []（坏文件不该挡住导航，日志由调用方记）。"""
    try:
        with open(json_path(maps_dir, name), encoding='utf-8') as f:
            return validate(json.load(f).get('shapes'))
    except (OSError, ValueError, AttributeError):
        return []


def load_all(maps_dir, names):
    out = {}
    for n in names:
        s = load(maps_dir, n)
        if s:
            out[n] = s
    return out


def _segments(shapes):
    for s in shapes:
        p = s['pts']
        for i in range(len(p) - 1):
            yield p[i], p[i + 1]
        if s['kind'] == 'zone':
            yield p[-1], p[0]


def _inside(poly, X, Y):
    """偶奇规则，X / Y 是格中心坐标数组。"""
    inside = np.zeros(X.shape, dtype=bool)
    n = len(poly)
    for i in range(n):
        (x1, y1), (x2, y2) = poly[i], poly[(i + 1) % n]
        if y1 == y2:
            continue
        cond = (Y >= min(y1, y2)) & (Y < max(y1, y2))
        xc = x1 + (Y - y1) * (x2 - x1) / (y2 - y1)
        inside ^= cond & (X < xc)
    return inside


def distance_field(shapes, ox, oy, w, h):
    """每格中心到最近禁行线的距离（区域内部为 0），超出 INFLATION 的保持 inf。"""
    dist = np.full((h, w), np.inf, dtype=np.float32)
    pad = INFLATION + RES
    for (ax, ay), (bx, by) in _segments(shapes):
        i0 = max(0, int((min(ax, bx) - pad - ox) / RES)); i1 = min(w, int((max(ax, bx) + pad - ox) / RES) + 1)
        j0 = max(0, int((min(ay, by) - pad - oy) / RES)); j1 = min(h, int((max(ay, by) + pad - oy) / RES) + 1)
        if i0 >= i1 or j0 >= j1:
            continue
        X = ox + (np.arange(i0, i1) + .5) * RES
        Y = oy + (np.arange(j0, j1) + .5) * RES
        X, Y = np.meshgrid(X, Y)
        dx, dy = bx - ax, by - ay
        L2 = dx * dx + dy * dy
        t = np.clip(((X - ax) * dx + (Y - ay) * dy) / L2, 0, 1) if L2 > 0 else 0
        d = np.hypot(X - (ax + t * dx), Y - (ay + t * dy)).astype(np.float32)
        np.minimum(dist[j0:j1, i0:i1], d, out=dist[j0:j1, i0:i1])
    for s in shapes:
        if s['kind'] != 'zone':
            continue
        xs, ys = [p[0] for p in s['pts']], [p[1] for p in s['pts']]
        i0 = max(0, int((min(xs) - ox) / RES)); i1 = min(w, int((max(xs) - ox) / RES) + 1)
        j0 = max(0, int((min(ys) - oy) / RES)); j1 = min(h, int((max(ys) - oy) / RES) + 1)
        if i0 >= i1 or j0 >= j1:
            continue
        X, Y = np.meshgrid(ox + (np.arange(i0, i1) + .5) * RES, oy + (np.arange(j0, j1) + .5) * RES)
        sub = dist[j0:j1, i0:i1]
        sub[_inside(s['pts'], X, Y)] = 0
    return dist


def values(dist):
    """距离 → raw 掩膜像素（0–100，255 = 不管）。"""
    v = np.full(dist.shape, 255, dtype=np.uint8)
    grad = dist <= INFLATION
    cost = 252.0 * np.exp(-SCALE * (dist[grad] - INSCRIBED))
    v[grad] = np.clip(np.rint(cost * 100 / 254), 1, 99).astype(np.uint8)
    v[dist <= INSCRIBED] = 99
    v[dist <= LINE_HALF] = 100
    return v


def render(shapes):
    """→ (像素 h×w uint8，行 0 在 origin 一侧, ox, oy)。没有形状时是 1×1 的空掩膜。"""
    if not shapes:
        return np.full((1, 1), 255, dtype=np.uint8), 0.0, 0.0
    xs = [p[0] for s in shapes for p in s['pts']]
    ys = [p[1] for s in shapes for p in s['pts']]
    pad = INFLATION + 2 * RES
    ox = math.floor((min(xs) - pad) / RES) * RES
    oy = math.floor((min(ys) - pad) / RES) * RES
    w = int(math.ceil((max(xs) + pad - ox) / RES))
    h = int(math.ceil((max(ys) + pad - oy) / RES))
    if w * h > MAX_CELLS:
        raise ValueError(f'禁行区铺得太开了（{w * RES:.0f}×{h * RES:.0f} m），最多约 100×100 m')
    return values(distance_field(shapes, ox, oy, w, h)), round(ox, 3), round(oy, 3)


def _atomic(path, data):
    d = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=d, prefix='.' + os.path.basename(path) + '.')
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def write_mask(maps_dir, name, shapes):
    """渲染并写 keepout/<name>.pgm + .yaml（yaml 最后写：读到 yaml 时 pgm 一定是新的）。返回 yaml 路径。"""
    px, ox, oy = render(shapes)
    os.makedirs(os.path.join(maps_dir, SUBDIR), exist_ok=True)
    base = os.path.join(maps_dir, SUBDIR, name)
    h, w = px.shape
    _atomic(base + '.pgm', f'P5\n{w} {h}\n255\n'.encode() + px[::-1].tobytes())
    _atomic(base + '.yaml', (f'image: {name}.pgm\nmode: raw\nresolution: {RES}\norigin: [{ox}, {oy}, 0.0]\n'
                             f'negate: 0\noccupied_thresh: 0.65\nfree_thresh: 0.196\n').encode())
    return base + '.yaml'


def save(maps_dir, name, shapes, who=''):
    """校验 → 写掩膜 → 写 json（没有形状就删 json，掩膜写成空的）。返回 (干净的形状, 掩膜 yaml 路径)。"""
    shapes = validate(shapes)
    path = write_mask(maps_dir, name, shapes)
    jp = json_path(maps_dir, name)
    if shapes:
        doc = {'version': 1, 'map': name, 'updated': time.strftime('%Y-%m-%d %H:%M:%S'), 'by': who,
               'shapes': shapes}
        _atomic(jp, json.dumps(doc, ensure_ascii=False, indent=1).encode())
    elif os.path.exists(jp):
        os.unlink(jp)
    return shapes, path


def ensure_mask(maps_dir, name):
    """导航要加载的掩膜：没有或比 json 旧就重新渲染（比如 json 是拷过来的）。返回 yaml 路径。"""
    path, jp = mask_path(maps_dir, name), json_path(maps_dir, name)
    try:
        stale = not os.path.exists(path) or (os.path.exists(jp) and os.path.getmtime(jp) > os.path.getmtime(path))
    except OSError:
        stale = True
    if stale:
        write_mask(maps_dir, name, load(maps_dir, name))
    return path


def remove_mask(maps_dir, name):
    for ext in ('.yaml', '.pgm'):
        try:
            os.unlink(os.path.join(maps_dir, SUBDIR, name + ext))
        except FileNotFoundError:
            pass


def hit(shapes, x, y, r=INSCRIBED):
    """点 (x, y) 在哪个形状的禁行范围里（离线 ≤ r 或在区域内）→ 形状序号，不在 → None。"""
    for k, s in enumerate(shapes):
        p = s['pts']
        if s['kind'] == 'zone' and _inside(p, np.array([x]), np.array([y]))[0]:
            return k
        segs = list(zip(p, p[1:])) + ([(p[-1], p[0])] if s['kind'] == 'zone' else [])
        for (ax, ay), (bx, by) in segs:
            dx, dy = bx - ax, by - ay
            L2 = dx * dx + dy * dy
            t = min(1, max(0, ((x - ax) * dx + (y - ay) * dy) / L2)) if L2 > 0 else 0
            if math.hypot(x - ax - t * dx, y - ay - t * dy) <= r:
                return k
    return None
