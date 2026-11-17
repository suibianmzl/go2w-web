"""续建自检的回归测试：用射线模型造 scan，检查能否分辨「位姿对了」「位姿偏了」「用错地图」。

为什么用合成 scan 而不是真录包：这里要验的是判据的分辨率边界，需要能精确控制真值。
真录包没有真值，证不了「偏 0.4 m 会不会被拦」。

运行：python3 dev/test_map_overlap_check.py
"""
import math
from pathlib import Path
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))   # 独立仓库：用部署副本（源在主仓库 scripts/calib）
import map_overlap_check as M


def build_map(kind='room'):
    """造一张 20×14 m、0.05 m 的合成地图。房间：四周是墙，中间有一道隔断。"""
    res, w, h = 0.05, 400, 280
    grid = np.zeros((h, w), dtype=np.int8)          # 0 = 空闲
    grid[:2, :] = grid[-2:, :] = 100                 # 上下墙
    grid[:, :2] = grid[:, -2:] = 100                 # 左右墙
    grid[2:-2, 200:202] = 100                        # 中间隔断
    return M.BaseMap(grid, res, -10.0, -7.0)


def scan_at(m, x, y, n=720, rmax=15.0, seed=0):
    """在 (x, y) 真实位置打一圈，返回 base_footprint 系下的点（雷达实际输出的样子）。
    和 dev/fake_ros/go2w_fake_sim.py 的 scan() 同一套射线逻辑。"""
    rng = np.random.default_rng(seed)
    H, W = m.grid.shape
    a = np.linspace(-math.pi, math.pi, n, endpoint=False)
    r = np.arange(0.1, rmax, m.res)
    X = x + np.cos(a)[:, None] * r[None, :]
    Y = y + np.sin(a)[:, None] * r[None, :]
    rr, cc = m.to_index(X, Y)
    gy, gx = np.floor(rr + 0.5).astype(int), np.floor(cc + 0.5).astype(int)
    inside = (gx >= 0) & (gy >= 0) & (gx < W) & (gy < H)
    hit = np.zeros_like(inside)
    hit[inside] = m.grid[gy[inside], gx[inside]] == 100
    dist = np.where(hit.any(1), r[np.maximum(hit.argmax(1), 0)], np.inf)
    dist = dist + rng.normal(0, 0.01, n) * np.isfinite(dist)
    ok = np.isfinite(dist) & (dist > 0.45)
    return np.stack([dist[ok] * np.cos(a[ok]), dist[ok] * np.sin(a[ok])], 1)


def place(pts_base, ex, ey, eth):
    """用（可能错误的）位姿把 base 系的点放到地图坐标系。"""
    c, s = math.cos(eth), math.sin(eth)
    return np.stack([ex + c * pts_base[:, 0] - s * pts_base[:, 1],
                     ey + s * pts_base[:, 0] + c * pts_base[:, 1]], 1)


class OverlapCheckTests(unittest.TestCase):
    def setUp(self):
        self.map = build_map()
        # 房间中间偏左的空地，两侧墙都在 4–5 m 内
        self.truth = (0.0, 0.0)
        self.base_scan = scan_at(self.map, *self.truth)

    def run_at(self, dx=0.0, dy=0.0, dth=0.0, scan=None):
        pts = self.base_scan if scan is None else scan
        ex, ey = self.truth[0] + dx, self.truth[1] + dy
        moved = place(pts, ex, ey, math.radians(dth))
        # pivot 是「以为的起点」，和网页后端 cont_check 传的一致
        return M.check(self.map, M.thin(moved), pivot=(ex, ey))

    def test_correct_pose_passes(self):
        report = self.run_at()
        self.assertTrue(report['ok'], report['reason'])
        self.assertGreaterEqual(report['fit'], M.MIN_FIT)

    def test_small_shift_tolerated(self):
        # 0.15 m 在 slam_toolbox 的扫描匹配窗口（0.5 m）内，会自己纠回来，不该拦
        report = self.run_at(dx=0.15)
        self.assertTrue(report['ok'], report['reason'])

    def test_large_shift_rejected(self):
        report = self.run_at(dx=0.6)
        self.assertFalse(report['ok'])
        self.assertIn('起点位姿偏了', report['reason'])

    def test_large_rotation_rejected(self):
        report = self.run_at(dth=25.0)
        self.assertFalse(report['ok'])

    def test_mid_rotation_reported_as_pose_not_mismatch(self):
        # 10° 超出 MAX_DYAW（8°）但还在角度搜索范围内：应报「起点位姿偏了」并给出能用的
        # correction，不能因为当前贴合率低被误报成「环境对不上」（搜索上限曾是 ±5°+2°=7°，
        # 8° 门限永远触发不了）
        self.assertGreater(M.MAX_SEARCH_YAW, M.MAX_DYAW)
        report = self.run_at(dth=10.0)
        self.assertFalse(report['ok'])
        self.assertIn('起点位姿偏了', report['reason'])
        self.assertAlmostEqual(report['correction'][2], -10.0, delta=1.0)

    def test_far_from_origin_small_rotation_passes(self):
        # 回归：旋转曾绕地图原点转。机器人离原点远时，纯 5° 的偏差被当成「平移 0.85 m」拦下
        # （11-17 在 lab_20261116 的 (11.67, 0.93) 实测）。这里把起点放到离原点约 7 m 处
        self.truth = (-6.0, 3.5)
        self.base_scan = scan_at(self.map, *self.truth)
        report = self.run_at(dth=5.0)
        self.assertTrue(report['ok'], report['reason'])
        self.assertLess(report['correction_norm'], 0.2)

    def test_far_from_origin_mid_rotation_reports_yaw(self):
        self.truth = (-6.0, 3.5)
        self.base_scan = scan_at(self.map, *self.truth)
        report = self.run_at(dth=10.0)
        self.assertFalse(report['ok'])
        self.assertIn('起点位姿偏了', report['reason'])
        self.assertAlmostEqual(report['correction'][2], -10.0, delta=1.0)
        self.assertLess(report['correction_norm'], 0.2)

    def test_wrong_map_rejected_as_mismatch_not_shift(self):
        # 换一张「墙位置完全不同」的图当 scan：应报「环境对不上」，不能报成「位姿偏了」
        other = build_map()
        other.grid[150:152, 50:350] = 100        # 凭空多一道横墙
        report = self.run_at(scan=scan_at(other, *self.truth))
        self.assertFalse(report['ok'])
        self.assertIn('对不上', report['reason'])

    def test_too_few_points(self):
        report = M.check(self.map, np.zeros((5, 2)))
        self.assertFalse(report['ok'])
        self.assertIn('样本不足', report['reason'])

    def test_off_map_scan(self):
        # 机器人在图外：点全落在基准图范围之外
        far = place(self.base_scan, 500.0, 500.0, 0.0)
        report = M.check(self.map, M.thin(far))
        self.assertFalse(report['ok'])

    def test_near_mask_does_not_wrap_edges(self):
        # 膨胀不能像 np.roll 那样把对边绕过来：只有左下角有墙时，右上角不该被算作墙线附近
        grid = np.zeros((40, 40), dtype=np.int8)
        grid[0:2, 0:2] = 100
        m = M.BaseMap(grid, 0.05, 0.0, 0.0)
        near = m.near(0.10)
        self.assertTrue(near[0, 0])
        self.assertFalse(near[-1, -1])


class LoaderTests(unittest.TestCase):
    def test_round_trip_yaml(self):
        """真地图存盘再读，确认行序、origin、阈值换算都没错。"""
        with tempfile.TemporaryDirectory() as tmp:
            grid = np.zeros((20, 30), dtype=np.uint8)
            grid[0, :] = 0          # 最上一行占据（图片第 0 行 = 地图最上边）
            grid[-1, :] = 254
            grid[10, 10] = 205      # 未知
            (Path(tmp) / 'm.pgm').write_bytes(b'P5\n30 20\n255\n' + grid.tobytes())
            (Path(tmp) / 'm.yaml').write_text(
                'image: m.pgm\nmode: trinary\nresolution: 0.05\norigin: [-1.0, -2.0, 0]\n'
                'negate: 0\noccupied_thresh: 0.65\nfree_thresh: 0.196\n')
            m = M.load_base(str(Path(tmp) / 'm.yaml'))
            self.assertEqual(m.grid.shape, (20, 30))
            self.assertEqual((m.ox, m.oy), (-1.0, -2.0))
            # 图片第 0 行是地图最上边 → 翻转后落在最后一行
            self.assertEqual(m.grid[-1, 0], 100)
            self.assertEqual(m.grid[0, 0], 0)
            self.assertEqual(m.grid[-11, 10], -1)   # 205 → (255-205)/255≈0.196，介于两阈值之间


if __name__ == '__main__':
    unittest.main()
