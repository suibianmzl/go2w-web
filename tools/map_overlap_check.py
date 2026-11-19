#!/usr/bin/env python3
"""续建自检：确认「起点位姿」给对了，再让机器人开走。

## 为什么要这个工具

slam_toolbox 用 `START_AT_GIVEN_POSE` 加载旧 posegraph 续建时，起点位姿给错**不会报错**：
旧图的格子在它自己的坐标上原样保留，只有新扫进来的墙会整体偏一点。表现出来就是收工后
发现墙是双线、路线跑起来贴墙。人在场时看不出，所以要自动量。

## 为什么不是「新图 vs 基准图」

一开始想的是比两张栅格的重叠区，但这条路是错的：反序列化后基准图的格子原样存在，
实时图里绝大部分占据点本来就来自基准图，它们必然「一致」，把真正出问题的**新增格子**稀释掉。
实测拿 1116 自比一致率 100%、偏移却报 0.05 m，而真错 0.30 m 时一致率仍有 95.6%——分不出好歹。

真正该量的是：**机器人现在扫到的墙，和基准图在该位姿下预测的墙，差多少。** 这就是
`loc_check.py` 用的贴合率，本工具把它做成离线可调用，并进一步反解出偏差量。

## 用法

    python3 map_overlap_check.py <基准图.yaml> <scan.npz> [--json]

`scan.npz` 由网页后端写：`pts` 为 N×2（float32），**已用当前 map→base_footprint 变换转到
地图坐标系**。所以这个检查同时覆盖两件事：TF 算得对不对、起点位姿给得对不对。
字段名沿用 `globloc.py` 的 npz 约定，便于复用现成代码。

输出：

    correction  [dx, dy, dyaw]  在这些点上把位姿再加这个修正量，贴合率最高
    fit         修正前贴合率     ← 起点位姿给对时应该已经很高
    fit_best    修正后贴合率
    ok          见下面的三个分支

判据（改这里要同步 docs/地图续建.md）：

    MIN_POINTS  有效点 < 120          → 样本不足，不算通过
    MIN_FIT     fit_best < 0.85       → 环境对不上（用错地图 / 不在图上）
    MAX_DX      修正量 > 0.40 m       → 偏差超出 slam_toolbox 的扫描匹配窗口，它自己纠不回来
    MAX_DYAW    修正量 > 8°           → 同上

三个分支，顺序不能换：

    1. fit_best < MIN_FIT          → 环境和这张图对不上
    2. 偏差超出窗口                → 起点位姿错了，报 correction
    3. 偏差在窗口内                → 通过；即使当前 fit 低也放行（slam_toolbox 会自己吸附）

顺序颠倒会把「用错地图」误报成「位姿偏了 0.6 m」，让人白折腾一轮。

## 这个检查的分辨率下限（实测，2026-11-17，lab_20261116）

在真实的走廊位置（墙在 1.5–3 m 内）用射线模型造 scan，故意给错位姿，量 fit（tol=0.10）：

    位姿误差      0.05 m   0.10 m   0.20 m   0.30 m   0.5°   1°    2°
    fit           1.00     1.00     0.99     0.55     0.91   0.91  0.83

结论有两条，都影响用法：

1. **横向平移几乎测不出来**。走廊里 scan 平行于墙，沿着墙挪 0.2 m，每条射线的长度基本不变。
   所以这个检查**不能**当「毫米级对准」用，它分辨不了 0.2 m 以内的平移。门限定在 0.25 m
   是因为再往下就没有分辨力，定了也是假精度。

2. **这恰好够用，因为更小的误差 slam_toolbox 会自己纠回来**。加载 posegraph 后，第一帧
   scan 要和图里已有的节点做匹配，搜索窗就是 `correlation_search_space_dimension: 0.5` m、
   `coarse_search_angle_offset: 0.349`（20°）。也就是说起点位姿偏 0.2 m / 几度时，
   扫描匹配会自己吸附到位；真正会建出双线墙的是**超出这个窗口**的错误——那正是本工具要拦的。

所以分工是：本工具负责「别站在错的地方、别用错地图、别偏出半米」；窗口内的精细对齐交给
slam_toolbox。不要为了让本工具「更准」去调紧门限，那只会误报。
"""
# 部署副本：容器只挂 /opt/go2w/web，scripts/calib 不在挂载里，所以这里放一份。
# 源文件是 scripts/calib/map_overlap_check.py，改那边之后同步过来（web/dev/test_tools_sync.py 会查出来）。
import argparse
import ast
import json
import math
import os
import sys

import numpy as np

from map_to_png import read_pgm

# ---- 判据 ----
MIN_POINTS = 120
MIN_FIT = 0.85
# A scan can see furniture and space that was never mapped. Require both
# agreement on the known part and enough old-wall evidence in the full scan.
MIN_KNOWN_FIT = 0.65
MIN_WALL_POINTS = 60
MIN_WALL_COVERAGE = 0.35
VERIFIED_SHIFT = 0.15
VERIFIED_YAW = 4.0
# MAX_DX 不是「我们希望多准」，而是「slam_toolbox 自己能不能纠回来」：
# 加载 posegraph 后第一帧 scan 要和图里已有节点做匹配，搜索窗由
# correlation_search_space_dimension: 0.5（m）决定。窗口内的偏差会自己吸附，
# 超出窗口才会把新扫的墙贴歪、建出双线。所以门限取窗口内留一点余量。
MAX_DX = 0.40        # m
MAX_DYAW = 8.0       # °，粗角搜索 coarse_search_angle_offset: 0.349 rad = 20°，取一半
MATCH_TOL = 0.10     # m，比网页 live 匹配率的 0.15 紧一档
WIDE_SEARCH_XY = 1.50  # m，粗搜范围。要比容差大一档，才能分清「偏多了」和「图用错了」
SEARCH_STEP = 0.05    # m
# 必须大于 MAX_DYAW：搜索上限够不到门限的话，角度偏差再大也报不成「起点位姿错了」，
# 只会因为贴合率掉下去被误报成「环境和图对不上」（原来 ±5° 粗搜 + ±2° 细搜最多 7°，8° 门限永远触发不了）。
# 12° 让 10° 量级的偏差能被量出来并给出 correction，再大才顶到上限。
MAX_SEARCH_YAW = 12.0  # °
YAW_STEP = 0.5        # °

GRID_VALUE_OCCUPIED = 100
GRID_VALUE_UNKNOWN = -1


def _read_yaml(path):
    meta = {}
    with open(path, encoding='utf-8') as f:
        for line in f:
            line = line.split('#', 1)[0]
            if ':' in line:
                key, value = line.split(':', 1)
                meta[key.strip()] = value.strip()
    return meta


class BaseMap:
    """基准图的占据栅格。第 0 行在 origin 一侧（和 OccupancyGrid 一致）。"""

    def __init__(self, grid, res, ox, oy, oyaw=0.0):
        self.grid = grid
        self.res = float(res)
        self.ox, self.oy, self.oyaw = float(ox), float(oy), float(oyaw)
        self._near = None

    @property
    def occupied(self):
        return self.grid == GRID_VALUE_OCCUPIED

    def near(self, tol):
        """占据格膨胀 tol 米。用分离式的两次方向膨胀（方形核可分离），不用 np.roll：
        roll 会把对边绕过来，地图边缘的墙线会和对面的墙「连上」，边界附近出现假匹配。"""
        cells = max(1, int(round(tol / self.res)))
        return _dilate(_dilate(self.occupied, cells, 0), cells, 1)

    def inside(self, x, y):
        r, c = self.to_index(x, y)
        r, c = np.rint(r).astype(np.int32), np.rint(c).astype(np.int32)
        h, w = self.grid.shape
        return (r >= 0) & (r < h) & (c >= 0) & (c < w)

    def to_index(self, x, y):
        """世界坐标 → 栅格浮点索引 (行, 列)。"""
        dx, dy = np.asarray(x, np.float64) - self.ox, np.asarray(y, np.float64) - self.oy
        if abs(self.oyaw) > 1e-12:
            ca, sa = math.cos(self.oyaw), math.sin(self.oyaw)
            dx, dy = ca * dx + sa * dy, -sa * dx + ca * dy
        return dy / self.res - 0.5, dx / self.res - 0.5

    def fit(self, x, y, near, *, dx=0.0, dy=0.0, dyaw=0.0, pivot=(0.0, 0.0)):
        """点位经修正 (dx, dy, dyaw) 后落在墙线 tol 内的比例。

        旋转绕 pivot（机器人起点位置）转，不是绕地图原点：修正量的含义是「起点位姿该改多少」，
        机器人转 dyaw 时转的是它自己。绕原点转的话，离原点 11.7 m 的机器人转 5° 等于被平移约 1 m，
        真实 5°、零平移的偏差会被报成「偏了 0.85 m」（11-17 在 lab_20261116 上实测踩到）。
        """
        hits, known, _inside = self.evidence(x, y, near, dx=dx, dy=dy, dyaw=dyaw, pivot=pivot)
        return hits / known if known else 0.0, known

    def evidence(self, x, y, near, *, dx=0.0, dy=0.0, dyaw=0.0, pivot=(0.0, 0.0)):
        if dyaw:
            ca, sa = math.cos(math.radians(dyaw)), math.sin(math.radians(dyaw))
            px, py = pivot
            x, y = x - px, y - py
            x, y = ca * x - sa * y + px, sa * x + ca * y + py
        x, y = x + dx, y + dy
        keep = self.inside(x, y)
        if not keep.any():
            return 0, 0, 0
        r, c = self.to_index(x[keep], y[keep])
        r, c = np.rint(r).astype(np.int32), np.rint(c).astype(np.int32)
        matched = near[r, c]
        known = matched | (self.grid[r, c] != GRID_VALUE_UNKNOWN)
        return int(matched.sum()), int(known.sum()), int(keep.sum())


def load_base(path):
    meta = _read_yaml(path)
    if meta.get('mode', 'trinary') != 'trinary':
        raise ValueError(f'{path}：目前只支持 trinary 栅格地图')
    res = float(meta['resolution'])
    origin = ast.literal_eval(meta['origin'])
    ox, oy = float(origin[0]), float(origin[1])
    oyaw = float(origin[2]) if len(origin) > 2 else 0.0
    image = meta.get('image') or os.path.basename(path)[:-5] + '.pgm'
    image = ast.literal_eval(image) if image[:1] in '"\'' else image
    px = read_pgm(os.path.join(os.path.dirname(path), image))
    value = px.astype(np.float32) / 255.0
    p = value if int(meta.get('negate', 0)) else 1.0 - value   # 占用概率
    grid = np.full(px.shape, GRID_VALUE_UNKNOWN, dtype=np.int8)
    grid[p > float(meta.get('occupied_thresh', 0.65))] = GRID_VALUE_OCCUPIED
    grid[p < float(meta.get('free_thresh', 0.25))] = 0
    return BaseMap(np.ascontiguousarray(grid[::-1]), res, ox, oy, oyaw)   # 图片第 0 行在最上边


def load_scan(path):
    d = np.load(path)
    pts = np.asarray(d['pts'], dtype=np.float64)
    if pts.ndim != 2 or pts.shape[1] != 2:
        raise ValueError(f'{path}：pts 必须是 N×2')
    return pts[np.isfinite(pts).all(1)]


def _dilate(mask, cells, axis):
    """沿一个轴做半径为 cells 的膨胀。补边后逐格 or，不用 np.roll（roll 会绕到对边）。"""
    if cells <= 0:
        return mask
    h, w = mask.shape
    padded = np.pad(mask, ((cells, cells) if axis == 0 else (0, 0),
                           (0, 0) if axis == 0 else (cells, cells)),
                    mode='constant', constant_values=False)
    out = np.zeros_like(mask)
    for k in range(2 * cells + 1):
        if axis == 0:
            out |= padded[k:k + h, :]
        else:
            out |= padded[:, k:k + w]
    return out


def thin(pts, voxel=0.05):
    """按体素抽稀：同一格里只留一个点，避免近处的点把贴合率刷高。"""
    if not len(pts):
        return pts
    key = np.floor(pts / voxel).astype(np.int64)
    _, keep = np.unique(key, axis=0, return_index=True)
    return pts[np.sort(keep)]


def check(base, pts, *, tol=MATCH_TOL, pivot=None, require_aligned=False):
    """比对 scan 与基准图，返回可直接 JSON 化的 dict。

    pivot 是起点位姿的 (x, y)，角度修正绕它转。不给时退回点云重心——一圈 scan 的重心
    离机器人通常不远，但墙分布不均时能差出几米，所以调用方知道起点就该传。
    """
    result = {'points': int(len(pts)), 'res': base.res, 'tol': tol, 'ok': False, 'reason': ''}
    if pivot is None and len(pts):
        pivot, result['pivot_source'] = (float(pts[:, 0].mean()), float(pts[:, 1].mean())), 'centroid'
    elif pivot is not None:
        pivot, result['pivot_source'] = (float(pivot[0]), float(pivot[1])), 'given'
    if pivot is not None:
        result['pivot'] = [round(pivot[0], 3), round(pivot[1], 3)]
    if len(pts) < MIN_POINTS:
        result['code'] = 'insufficient_scan'
        result['reason'] = f'样本不足：只有 {len(pts)} 个有效激光点（至少 {MIN_POINTS}）'
        return result

    near = base.near(tol)
    x, y = pts.T
    hits0, used, inside = base.evidence(x, y, near)
    fit0 = hits0 / used if used else 0.0
    result.update(fit=round(fit0, 4), points_used=used, points_inside=inside,
                  wall_points=hits0, wall_coverage=round(hits0 / len(pts), 4))

    # 先粗后细。粗搜的范围要比「容差」大一档：要为「偏得太多」和「图用错了」这两种情况
    # 分开留出判断空间——只搜 ±0.25 m 的话，偏 0.6 m 会因为够不着而报成「图对不上」。
    # Rank by total matched points, with a fixed denominator. A hypothesis
    # cannot win by pushing the poorly matched part of the scan off the map.
    best = (hits0, 0.0, 0.0, 0.0)

    def consider(dx, dy, dyaw):
        """更新最优解。分数打平时取修正量更小的，避免报出无意义的偏移。"""
        nonlocal best
        s, _, _ = base.evidence(x, y, near, dx=dx, dy=dy, dyaw=dyaw, pivot=pivot)
        if (s > best[0] or
                (s == best[0] and
                 math.hypot(dx, dy) + abs(dyaw) * 0.05 <
                 math.hypot(best[1], best[2]) + abs(best[3]) * 0.05)):
            best = (s, dx, dy, dyaw)

    coarse = int(round(WIDE_SEARCH_XY / 0.10))
    for dyaw in np.arange(-MAX_SEARCH_YAW, MAX_SEARCH_YAW + 1e-9, 2.0):
        for iy in range(-coarse, coarse + 1):
            for ix in range(-coarse, coarse + 1):
                consider(ix * 0.10, iy * 0.10, float(dyaw))

    # 在最优点附近细扫：±0.15 m / ±2°
    fine = int(round(0.15 / SEARCH_STEP))
    cd, cx, cy = best[3], best[1], best[2]
    for dyaw in np.arange(cd - 2.0, cd + 2.0 + 1e-9, YAW_STEP):
        for iy in range(-fine, fine + 1):
            for ix in range(-fine, fine + 1):
                consider(cx + ix * SEARCH_STEP, cy + iy * SEARCH_STEP, float(dyaw))

    hits_best, dx, dy, dyaw = best
    _, known_best, _ = base.evidence(x, y, near, dx=dx, dy=dy, dyaw=dyaw, pivot=pivot)
    fit_best = hits_best / known_best if known_best else 0.0
    shift = math.hypot(dx, dy)

    result.update({
        'fit_best': round(fit_best, 4),
        'wall_coverage_best': round(hits_best / len(pts), 4),
        'correction': [round(dx, 3), round(dy, 3), round(dyaw, 2)],
        'correction_norm': round(shift, 3),
    })
    required = MIN_KNOWN_FIT if require_aligned else MIN_FIT
    limit_xy = VERIFIED_SHIFT if require_aligned else MAX_DX
    limit_yaw = VERIFIED_YAW if require_aligned else MAX_DYAW
    enough_walls = max(MIN_WALL_POINTS, MIN_WALL_COVERAGE * len(pts))
    if inside < MIN_WALL_POINTS or known_best < MIN_WALL_POINTS or (
        require_aligned and known_best < enough_walls
    ):
        result['code'] = 'insufficient_overlap'
        result['reason'] = (f'与旧图重叠不足（找到 {hits_best} 个旧墙匹配点，'
                            f'占扫描 {hits_best / len(pts):.0%}），请到能同时看到旧墙和墙角的位置')
    elif fit_best < required:
        # 平移旋转都救不回来：不是「偏了一点」，是环境和这张图对不上。
        # 这一支必须先判，否则「用错地图」会被误报成「位姿偏了 0.6 m」，让人白折腾一轮。
        result['code'] = 'mismatch'
        result['reason'] = (f'旧图贴合率 {fit0:.0%}（局部搜索最好 {fit_best:.0%}，要求 ≥ {required:.0%}）。'
                            '请核对场景变化、基准图和起点')
        if not require_aligned:
            result['reason'] += '，扫描与基准图对不上'
    elif require_aligned and hits_best < enough_walls:
        result['code'] = 'insufficient_overlap'
        result['reason'] = f'旧墙约束不足（匹配 {hits_best} 个点），请到能看到更多旧墙的位置重试'
    elif (shift > limit_xy or abs(dyaw) > limit_yaw
          or (require_aligned and (fit0 < required or hits0 < enough_walls))):
        # 场是对的，但偏差超出了 slam_toolbox 的扫描匹配窗口，它自己纠不回来
        clamp = ''
        if max(abs(dx), abs(dy)) >= WIDE_SEARCH_XY + 0.15 - 1e-6 or abs(dyaw) >= MAX_SEARCH_YAW + 2 - 1e-6:
            clamp = '（修正量已到搜索边界，实际偏差可能更大）'
        result['code'] = 'pose_offset'
        label = 'SLAM' if require_aligned else '起点'
        result['reason'] = (f'{label}位姿偏了 {shift:.2f} m / {dyaw:+.1f}°'
                            f'（容差 {limit_xy:.2f} m / {limit_yaw:.1f}°）{clamp}，'
                            f'修正后贴合率 {fit_best:.0%}。'
                            f'按 correction 改起点位姿再开始，否则会建出双线墙')
    elif require_aligned and any(
        all(base.evidence(x, y, near, dx=dx + sign * ux, dy=dy + sign * uy,
                          dyaw=dyaw, pivot=pivot)[0] >= 0.95 * hits_best for sign in (-1, 1))
        for ux, uy in ((0.35, 0.0), (0.0, 0.35), (0.25, 0.25), (0.25, -0.25))
    ):
        result['code'] = 'ambiguous_overlap'
        result['reason'] = '旧墙能贴合，但沿一个方向移动后仍然相似，请换到能看到墙角或门框的位置'
    elif not require_aligned and fit0 < MIN_FIT:
        # 当前贴合率不高，但偏差在 slam_toolbox 的扫描匹配窗口内，它加载图后会把
        # 第一帧 scan 匹配到位。放行，同时说清楚它自己会纠，省得人以为漏判了。
        result['ok'] = True
        result['reason'] = (f'当前位姿贴合率 {fit0:.0%}，偏 {shift:.2f} m / {dyaw:+.1f}°，'
                            f'在 slam_toolbox 的扫描匹配窗口内（{MAX_DX:.1f} m / {MAX_DYAW:.0f}°），'
                            f'它会自己吸附到位，可以开始；想更稳就先按 correction 改起点')
    else:
        result['ok'] = True
        result['code'] = 'verified'
        result['reason'] = (f'旧图贴合率 {fit0:.0%}，旧墙约束覆盖 {hits0 / len(pts):.0%}，'
                            f'位姿修正 {shift:.2f} m / {dyaw:+.1f}°')
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description='续建自检：确认起点位姿给对了再开走')
    parser.add_argument('base', help='续建基准地图（.yaml，带 .pgm）')
    parser.add_argument('scan', help='网页后端写的 scan.npz（pts 已转到地图坐标系）')
    parser.add_argument('--json', action='store_true', help='只输出一行 JSON')
    parser.add_argument('--tol', type=float, default=MATCH_TOL)
    parser.add_argument('--require-aligned', action='store_true', help='验证 SLAM 已经对齐的位姿')
    parser.add_argument('--pivot', help='起点位姿 x,y（地图坐标系）。角度修正绕它转；'
                                        '负数要写成 --pivot=-1.2,3.4。不给时用点云重心')
    args = parser.parse_args(argv)
    try:
        pivot = None
        if args.pivot:
            px, py = (float(v) for v in args.pivot.split(','))
            pivot = (px, py)
        base = load_base(args.base)
        report = check(base, thin(load_scan(args.scan)), tol=args.tol, pivot=pivot,
                       require_aligned=args.require_aligned)
    except (ValueError, OSError, KeyError, SyntaxError) as error:
        if args.json:
            print(json.dumps({'ok': False, 'reason': str(error)}, ensure_ascii=False))
        else:
            print(f'自检失败：{error}', file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(report, ensure_ascii=False))
    else:
        print(f'{"通过" if report["ok"] else "不通过"}：{report["reason"]}')
        if 'fit' in report:
            print(f'  贴合率 {report["fit"]:.0%}（修正后 {report.get("fit_best", 0):.0%}）'
                  f'  修正量 {report["correction"]}  点数 {report["points_used"]}')
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
