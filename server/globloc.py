#!/usr/bin/env python3
"""2D 全局定位（自动定位）：几帧静止的 360° scan 在整张占据栅格上暴力匹配，不需要初始位姿。

只依赖 numpy（有 scipy 时用它算距离变换，快一些）。
流程：
  1. 地图 → 似然场 field = exp(-d²/2σ²)，d 为到最近障碍的距离
  2. 粗搜：候选位置取地图上离障碍 > 0.2 m 的空闲格（每 COARSE 格取一个），航向每 1.5° 一档；
     粗似然场先做 COARSE×COARSE 滑窗取最大 —— 粗格的分数是其覆盖区域内任何位置分数的上界
     （Cartographer 分支定界的同一个思路，这里只分两层）
  3. 粗搜前 K 个（位置间隔 ≥ 0.5 m）在细似然场上局部细搜
  4. 输出最佳位姿、分数、「次优」（与最佳相距 > 1 m 或 > 30° 的最好结果）的分数 → 判断有没有歧义

11-10 离线评估（lab_20261105，两段实录 bag，真值 = AMCL + odom 外推）：
  单帧静止 scan 122/122 定对，位置误差中位 6 cm；遮挡 180° / 8 个人 + 遮挡 90° 时偶尔定错，
  错的全部是次优/最优 ≥ 0.85；镜像 scan（地图上不存在的地方）分数 0.52–0.75、比值 0.85–1.00。
  所以接受条件：分数 ≥ MIN_SCORE 且 次优/最优 ≤ MAX_RATIO。扩展坞上一次约 5 s。

go2w_web.py 把 scan / 地图存成 npz，用子进程跑本文件（numpy 的大计算会占住 GIL，放在网页后端进程里会卡住心跳 / 安全检查）：
  python3 globloc.py in.npz      → stdout 一行 JSON
npz 字段：grid（h×w int8，OccupancyGrid 原始值，行 0 在 origin 一侧）、res、origin（x, y）、pts（N×2，base_footprint 下的点）
可选：circles（K×3，地图坐标 x, y, 半径 m）= 只在这几个圆里找机器人（11-26「指定大概位置」/ 先在上次位置附近找）；
      min_score = 这次的分数下限（附近找用更严的门槛）。
有 circles 时先把地图裁到「圆的外接框 + 激光最远距离 + 余量」再算：圆内每个位置的分数和整图算出来的完全一样，只是快。
"""
import json
import math
import os
import sys
import time

import numpy as np

try:
    from scipy.ndimage import distance_transform_edt as _edt
except ImportError:  # 容器里没有 scipy：限定最大距离，用平移取最小
    _edt = None

# 最优分数下限（平均似然，0..1）。GO2W_AUTOLOC_MIN_SCORE 只给本地测试用（假 scan 分数总是很高，调高才能测失败流程）
MIN_SCORE = float(os.environ.get('GO2W_AUTOLOC_MIN_SCORE', '0.75'))
MAX_RATIO = 0.80   # 次优 / 最优 上限：越接近 1 越有歧义
# 搜索方式：bbs（默认，多级分支定界，大图也快）/ old（11-10 的两层粗搜 + 细搜，回退用）
IMPL = os.environ.get('GO2W_GLOBLOC_IMPL', 'bbs')
BBS_LEVELS = 7      # 金字塔层数：最粗一层的块是 2^7 = 128 格（0.05 m 时 6.4 m）
BBS_KEEP = 0.70     # 留下上界 ≥ BBS_KEEP × 当前最好分数 的块。比 MAX_RATIO 低一档，次优分数在 0.70 以上时能如实报出来
BBS_CHUNK = 8192    # 每批算多少个块（控制内存：块数 × 点数 × 4 B）
BBS_BATCH = 4096    # 最优先展开时每轮挑多少个上界最高的块一起拆（numpy 批量算，减少 Python 开销）


def distance_field(occ, max_cells):
    """每格到最近障碍格的距离（格），截断在 max_cells。"""
    if _edt is not None:
        return np.minimum(_edt(~occ), max_cells).astype(np.float32)
    h, w = occ.shape
    big = float(max_cells) ** 2
    r = max_cells
    pad = np.pad(occ, r, mode='constant', constant_values=False)
    d2 = np.full((h, w), big, np.float32)
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            dd = dx * dx + dy * dy
            if dd > r * r:
                continue
            sub = pad[r + dy:r + dy + h, r + dx:r + dx + w]
            d2[sub & (d2 > dd)] = dd
    return np.sqrt(d2)


def sliding_max(a, k):
    """out[y, x] = max(a[y:y+k, x:x+k])，越界补 0。"""
    h, w = a.shape
    p = np.pad(a, ((0, k), (0, k)), mode='constant')
    out = np.zeros_like(a)
    for dy in range(k):
        for dx in range(k):
            np.maximum(out, p[dy:dy + h, dx:dx + w], out=out)
    return out


def thin(p, max_pts=180, voxel=0.10):
    """体素去重（避免近处密集点主导分数），再均匀抽样到 max_pts。"""
    if len(p) == 0:
        return p
    key = np.floor(p / voxel).astype(np.int64)
    _, idx = np.unique(key[:, 0] * 1000003 + key[:, 1], return_index=True)
    p = p[np.sort(idx)]
    if len(p) > max_pts:
        p = p[np.linspace(0, len(p) - 1, max_pts).astype(int)]
    return p


class GlobalLocalizer:
    def __init__(self, grid, res, origin_xy, sigma=0.10, coarse_sigma=0.20, coarse_cells=4, robot_clearance=0.20, region=None, min_score=None):
        t0 = time.time()
        self.res = float(res)
        self.ox, self.oy = float(origin_xy[0]), float(origin_xy[1])
        self.h, self.w = grid.shape
        occ = grid >= 65
        free = (grid >= 0) & (grid < 20)
        dm = distance_field(occ, int(math.ceil(1.0 / self.res))) * self.res
        # 地图外 / 未知区域的点不加分
        self.fine = np.exp(-dm * dm / (2 * sigma * sigma)).astype(np.float32)
        coarse = np.exp(-dm * dm / (2 * coarse_sigma * coarse_sigma)).astype(np.float32)
        self.k = coarse_cells
        self.coarse = sliding_max(coarse, coarse_cells)
        # 候选位置：空闲且离障碍有一定距离（狗不可能贴着墙 / 在墙里）
        ok = free & (dm > robot_clearance)
        if region is not None:      # 只在指定范围（圆）里找
            ok &= region
        self.walkable = ok
        self.min_score = MIN_SCORE if min_score is None else float(min_score)
        ys, xs = np.nonzero(ok[::coarse_cells, ::coarse_cells])
        self.cand = np.stack([ys * coarse_cells, xs * coarse_cells], 1)
        self.prep_s = time.time() - t0

    def _offsets(self, pts, yaws):
        c, s = np.cos(yaws)[:, None], np.sin(yaws)[:, None]
        x = c * pts[None, :, 0] - s * pts[None, :, 1]
        y = s * pts[None, :, 0] + c * pts[None, :, 1]
        return np.rint(y / self.res).astype(np.int32), np.rint(x / self.res).astype(np.int32)

    def _score(self, field, cy, cx, dy, dx):
        Y = cy[:, None] + dy[None, :]
        X = cx[:, None] + dx[None, :]
        inside = (Y >= 0) & (Y < self.h) & (X >= 0) & (X < self.w)
        v = field[np.clip(Y, 0, self.h - 1), np.clip(X, 0, self.w - 1)]
        return (v * inside).mean(1)

    def to_world(self, cy, cx):
        return self.ox + (cx + 0.5) * self.res, self.oy + (cy + 0.5) * self.res

    def refine(self, pts, cy, cx, yaw, xy_cells, yaw_halfspan, yaw_step):
        yaws = yaw + np.arange(-yaw_halfspan, yaw_halfspan + 1e-9, yaw_step)
        d = np.arange(-xy_cells, xy_cells + 1)
        gy, gx = np.meshgrid(cy + d, cx + d, indexing='ij')
        gy, gx = gy.ravel(), gx.ravel()
        inside = (gy >= 0) & (gy < self.h) & (gx >= 0) & (gx < self.w)
        gy, gx = gy[inside], gx[inside]
        free = self.walkable[gy, gx]
        if not free.any():
            return -1.0, cy, cx, yaw
        gy, gx = gy[free], gx[free]
        DY, DX = self._offsets(pts, yaws)
        best = (-1.0, cy, cx, yaw)
        for i, a in enumerate(yaws):
            s = self._score(self.fine, gy, gx, DY[i], DX[i])
            j = int(np.argmax(s))
            if s[j] > best[0]:
                best = (float(s[j]), int(gy[j]), int(gx[j]), float(a))
        return best

    # ---------- 多级分支定界（BBS，Hess et al. 2016 / Cartographer 的同一思路） ----------
    # P[h][y+O, x+O] = max(fine[y:y+2^h, x:x+2^h])，地图外算 0。四周补 O 格零（O ≥ 激光最远距离 + 2^L），
    # 这样任何激光点的下标都落在数组里，不用逐点判断越界。把激光点按某个朝向放在块的左上角 (y, x)，
    # 用 P[h] 打出的平均分 ≥ 块内任何一个位置用 fine 打出的真实分数，所以上界低于门槛的块整块不用看。
    # h ≥ 1 存 uint8 并向上取整，仍然是上界。下标用一维：idx = (y+O)·Wp + (x+O) + 点偏移（每个朝向预先算好）。
    def _bbs_prepare(self, levels, reach):
        pad = int(reach) + (1 << levels) + 2
        if getattr(self, '_bbs', None) and self._bbs['L'] == levels and self._bbs['pad'] >= pad:
            return self._bbs
        cur = np.pad(self.fine, pad, mode='constant')
        P, W = [cur.ravel()], [self.walkable]
        wk = self.walkable
        for h in range(1, levels + 1):
            s = 1 << (h - 1)
            nxt = cur.copy()
            np.maximum(nxt[:-s, :], cur[s:, :], out=nxt[:-s, :])
            tmp = nxt.copy()
            np.maximum(nxt[:, :-s], tmp[:, s:], out=nxt[:, :-s])
            cur = nxt
            P.append(np.ceil(cur * 255.0).astype(np.uint8).ravel())
            w2 = wk.copy()
            w2[:-s, :] |= wk[s:, :]
            w3 = w2.copy()
            w3[:, :-s] |= w2[:, s:]
            wk = w3
            W.append(wk)
        self._bbs = {'L': levels, 'pad': pad, 'Wp': cur.shape[1], 'P': P, 'W': W}
        return self._bbs

    def _bbs_bound(self, h, ny, nx, ni, OFF):
        """块 (ny, nx) 左上角、朝向 ni 的上界（h = 0 时就是真实分数）。按批算，控制内存。"""
        b = self._bbs
        P, pad, Wp = b['P'][h], b['pad'], b['Wp']
        base = (ny + pad) * Wp + (nx + pad)
        out = np.empty(len(ny), np.float32)
        npts = OFF.shape[1]
        for i in range(0, len(ny), BBS_CHUNK):
            j = slice(i, i + BBS_CHUNK)
            v = np.take(P, base[j, None] + OFF[ni[j]])
            if h == 0:
                out[j] = v.sum(1) / npts
            else:
                out[j] = v.sum(1, dtype=np.int32) / (255.0 * npts)
        return out

    def _bbs_children(self, h, ny, nx, ni):
        """h 层的块拆成 h-1 层的 4 个子块，去掉出界的和里面没有能站的格子的。"""
        s = 1 << (h - 1)
        cy = np.concatenate([ny, ny + s, ny, ny + s])
        cx = np.concatenate([nx, nx, nx + s, nx + s])
        ci = np.concatenate([ni, ni, ni, ni])
        ok = (cy < self.h) & (cx < self.w)
        cy, cx, ci = cy[ok], cx[ok], ci[ok]
        ok = self._bbs['W'][h - 1][cy, cx]
        return cy[ok], cx[ok], ci[ok]

    def localize_bbs(self, pts, yaw_step_deg=1.5, levels=BBS_LEVELS):
        t0 = time.time()
        yaws = np.deg2rad(np.arange(-180, 180, yaw_step_deg))
        DY, DX = self._offsets(pts, yaws)
        reach = int(max(np.abs(DY).max(), np.abs(DX).max()))
        b = self._bbs_prepare(levels, reach)
        OFF = (DY.astype(np.int64) * b['Wp'] + DX).astype(np.int64)
        prep = time.time() - t0
        L, step = levels, 1 << levels
        ys, xs = np.nonzero(b['W'][L][::step, ::step])
        if not len(ys):
            return {'error': '基准图没有可用的空地候选'}
        ny = np.repeat(ys * step, len(yaws)).astype(np.int64)
        nx = np.repeat(xs * step, len(yaws)).astype(np.int64)
        ni = np.tile(np.arange(len(yaws)), len(ys)).astype(np.int64)
        pv = self._bbs_bound(L, ny, nx, ni, OFF)
        ph = np.full(len(pv), L, np.int8)
        py, px, pi = ny, nx, ni
        evals = len(pv)
        # 最优先展开：每次从待查的块里挑上界最高的一批往下拆（Cartographer 的做法，这里用 numpy 数组代替堆）。
        # 很快就落到最好的位置附近拿到真实高分，门槛 BBS_KEEP × 最好 随之抬高，剩下的块大多直接剪掉；
        # 待查的块全都低于门槛时，别处不可能更好，结束。
        best, rounds = -1.0, 0
        lv, ly, lx, li = [], [], [], []
        while len(pv):
            thr = BBS_KEEP * best - 1e-6
            keep = pv >= thr
            if not keep.all():
                pv, ph, py, px, pi = pv[keep], ph[keep], py[keep], px[keep], pi[keep]
            if not len(pv):
                break
            rounds += 1
            if len(pv) > BBS_BATCH:
                sel = np.argpartition(-pv, BBS_BATCH)[:BBS_BATCH]
                rest = np.ones(len(pv), bool)
                rest[sel] = False
            else:
                sel, rest = np.arange(len(pv)), np.zeros(len(pv), bool)
            bv, bh, by, bx, bi = pv[sel], ph[sel], py[sel], px[sel], pi[sel]
            pv, ph, py, px, pi = pv[rest], ph[rest], py[rest], px[rest], pi[rest]
            leaf = bh == 0
            if leaf.any():
                lv.append(bv[leaf]); ly.append(by[leaf]); lx.append(bx[leaf]); li.append(bi[leaf])
                best = max(best, float(bv[leaf].max()))
            add = []
            for h in np.unique(bh[~leaf])[::-1]:
                m = bh == h
                cy, cx, ci = self._bbs_children(int(h), by[m], bx[m], bi[m])
                if not len(cy):
                    continue
                v = self._bbs_bound(int(h) - 1, cy, cx, ci, OFF)
                evals += len(v)
                if h == 1:
                    best = max(best, float(v.max()))
                add.append((v, np.full(len(v), h - 1, np.int8), cy, cx, ci))
            if add:
                pv = np.concatenate([pv] + [a[0] for a in add])
                ph = np.concatenate([ph] + [a[1] for a in add])
                py = np.concatenate([py] + [a[2] for a in add])
                px = np.concatenate([px] + [a[3] for a in add])
                pi = np.concatenate([pi] + [a[4] for a in add])
        dive_best = best
        alive = [rounds]
        B = np.concatenate(lv) if lv else np.zeros(0, np.float32)
        ny = np.concatenate(ly) if ly else np.zeros(0, np.int64)
        nx = np.concatenate(lx) if lx else np.zeros(0, np.int64)
        ni = np.concatenate(li) if li else np.zeros(0, np.int64)
        if not len(B):
            return {'error': '没有找到候选位置'}
        best = max(best, float(B.max()))
        keep = B >= BBS_KEEP * best - 1e-6
        ny, nx, ni, B = ny[keep], nx[keep], ni[keep], B[keep]
        search_s = time.time() - t0 - prep
        # 最好的 + 离它 > 1 m 或 > 30° 的最好的（次优）；都再做一次局部细搜到 0.1°
        k0 = int(np.argmax(B))
        d = np.hypot((ny - ny[k0]) * self.res, (nx - nx[k0]) * self.res)
        dang = np.abs(np.remainder(yaws[ni] - yaws[ni[k0]] + math.pi, 2 * math.pi) - math.pi)
        far = (d > 1.0) | (dang > math.radians(30))
        step_r = math.radians(yaw_step_deg)

        def fine(k):
            r = self.refine(pts, int(ny[k]), int(nx[k]), float(yaws[ni[k]]), 1, step_r, math.radians(0.5))
            return self.refine(pts, r[1], r[2], r[3], 1, math.radians(0.5), math.radians(0.1))
        s0, y0, x0, a0 = fine(k0)
        bx, by = self.to_world(y0, x0)
        if far.any():
            k1 = int(np.flatnonzero(far)[np.argmax(B[far])])
            s1, y1, x1, a1 = fine(k1)
            wx, wy = self.to_world(y1, x1)
            second, second_pose, exact = s1, [round(wx, 3), round(wy, 3), round((a1 + math.pi) % (2 * math.pi) - math.pi, 4)], True
        else:
            # 别处没有上界够得着 0.70 × 最好 的：真实次优比这还低，按 0.70 报（偏保守，判定照样正确）
            second, second_pose, exact = BBS_KEEP * s0, None, False
        ratio = min(second / s0, 1.0) if s0 > 0 else 1.0
        return {'x': round(bx, 4), 'y': round(by, 4), 'th': round((a0 + math.pi) % (2 * math.pi) - math.pi, 4),
                'score': round(s0, 4), 'second': round(second, 4), 'ratio': round(ratio, 4), 'second_pose': second_pose,
                'ratio_exact': exact, 'ok': bool(s0 >= self.min_score and ratio <= MAX_RATIO), 'min_score': self.min_score,
                'n_pts': int(len(pts)), 'impl': 'bbs', 'evals': int(evals), 'dive_best': round(dive_best, 4), 'alive': alive, 'leaves': int(len(B)),
                'prep_s': round(self.prep_s + prep, 2), 'coarse_s': round(search_s, 2),
                'total_s': round(self.prep_s + time.time() - t0, 2)}

    def localize(self, pts, top_k=40, yaw_step_deg=1.5):
        t0 = time.time()
        yaws = np.deg2rad(np.arange(-180, 180, yaw_step_deg))
        cand = self.cand
        if not len(cand):
            return {'error': '基准图没有可用的空地候选'}
        DY, DX = self._offsets(pts, yaws)
        S = np.zeros((len(cand), len(yaws)), np.float32)
        for i in range(len(yaws)):
            S[:, i] = self._score(self.coarse, cand[:, 0], cand[:, 1], DY[i], DX[i])
        coarse_s = time.time() - t0
        bi = S.argmax(1)
        bs = S[np.arange(len(S)), bi]
        # Preserve an opposing heading even if two hypotheses share a location.
        alternate_score = np.full(len(S), -1.0, dtype=np.float32)
        ai = np.zeros_like(bi)
        for i in range(len(yaws)):
            da = np.abs(i - bi)
            better = ((np.minimum(da, len(yaws) - da) * yaw_step_deg > 30)
                      & (S[:, i] > alternate_score))
            alternate_score[better] = S[better, i]
            ai[better] = i
        scores = np.concatenate([bs, alternate_score])
        angles = np.concatenate([bi, ai])
        picks, min_sep = [], (0.5 / self.res) ** 2
        for j in np.argsort(-scores):
            y, x = cand[j % len(cand)]
            a = float(yaws[angles[j]])
            if all((y - py) ** 2 + (x - px) ** 2 >= min_sep
                   or abs(math.remainder(a - pa, 2 * math.pi)) > math.radians(30)
                   for py, px, pa in picks):
                picks.append((int(y), int(x), a))
                if len(picks) >= top_k:
                    break
        step, res = math.radians(yaw_step_deg), []
        for y, x, a in picks:
            c = self.k // 2
            r = self.refine(pts, y + c, x + c, a, self.k, step, math.radians(0.5))
            res.append(self.refine(pts, r[1], r[2], r[3], 1, math.radians(0.5), math.radians(0.1)))
        res.sort(key=lambda r: -r[0])
        s0, y0, x0, a0 = res[0]
        bx, by = self.to_world(y0, x0)
        second, second_pose = 0.0, None
        for s, y, x, a in res[1:]:
            wx, wy = self.to_world(y, x)
            da = abs((a - a0 + math.pi) % (2 * math.pi) - math.pi)
            if math.hypot(wx - bx, wy - by) > 1.0 or da > math.radians(30):
                second, second_pose = s, [round(wx, 3), round(wy, 3), round((a + math.pi) % (2 * math.pi) - math.pi, 4)]
                break
        ratio = second / s0 if s0 > 0 else 1.0
        return {'x': round(bx, 4), 'y': round(by, 4), 'th': round((a0 + math.pi) % (2 * math.pi) - math.pi, 4),
                'score': round(s0, 4), 'second': round(second, 4), 'ratio': round(ratio, 4), 'second_pose': second_pose,
                'ok': bool(s0 >= self.min_score and ratio <= MAX_RATIO),
                'n_pts': int(len(pts)), 'n_cand': int(len(cand)),
                'prep_s': round(self.prep_s, 2), 'coarse_s': round(coarse_s, 2), 'total_s': round(self.prep_s + time.time() - t0, 2)}


def crop_to_circles(grid, res, origin, circles, reach):
    """裁出圆附近那块地图 + 圆内为 True 的掩码。margin = 激光最远距离 + 1.5 m（似然场截断 1 m + 取整余量），
    这样圆内候选位置打到的每个激光点、以及决定这些格子似然值的障碍，都还在裁出来的图里。"""
    h, w = grid.shape
    ox, oy = float(origin[0]), float(origin[1])
    c = np.asarray(circles, np.float64).reshape(-1, 3)
    m = reach + 1.5
    x0, x1 = (c[:, 0] - c[:, 2]).min() - m, (c[:, 0] + c[:, 2]).max() + m
    y0, y1 = (c[:, 1] - c[:, 2]).min() - m, (c[:, 1] + c[:, 2]).max() + m
    i0, i1 = max(0, int(math.floor((y0 - oy) / res))), min(h, int(math.ceil((y1 - oy) / res)) + 1)
    j0, j1 = max(0, int(math.floor((x0 - ox) / res))), min(w, int(math.ceil((x1 - ox) / res)) + 1)
    if i0 >= i1 or j0 >= j1:
        return None
    sub = grid[i0:i1, j0:j1]
    cy = oy + (np.arange(i0, i1) + 0.5) * res
    cx = ox + (np.arange(j0, j1) + 0.5) * res
    region = np.zeros(sub.shape, bool)
    for x, y, r in c:
        region |= ((cy[:, None] - y) ** 2 + (cx[None, :] - x) ** 2) <= r * r
    return sub, (ox + j0 * res, oy + i0 * res), region


def main(path):
    d = np.load(path)
    pts = thin(np.asarray(d['pts'], np.float64))
    if len(pts) < 30:
        print(json.dumps({'error': f'有效激光点太少（{len(pts)}）'}))
        return
    min_score = float(d['min_score']) if 'min_score' in d.files else None
    grid, res, origin, region = d['grid'], float(d['res']), d['origin'], None
    if 'circles' in d.files and len(d['circles']):
        reach = float(np.hypot(pts[:, 0], pts[:, 1]).max())
        c = crop_to_circles(grid, res, origin, d['circles'], reach)
        if c is None:
            print(json.dumps({'error': '指定的范围在地图外面'}))
            return
        grid, origin, region = c
        if not region.any():
            print(json.dumps({'error': '指定的范围在地图外面'}))
            return
    L = GlobalLocalizer(grid, res, origin, region=region, min_score=min_score)
    if region is not None and not L.walkable.any():
        print(json.dumps({'error': '指定范围里没有能站的空地'}))
        return
    r = L.localize_bbs(pts) if IMPL == 'bbs' or region is not None else L.localize(pts)
    if region is not None and 'error' not in r:
        r['region'] = True
        r['crop'] = list(grid.shape)
    print(json.dumps(r))


if __name__ == '__main__':
    main(sys.argv[1])
