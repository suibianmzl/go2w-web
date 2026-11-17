"""起点位姿三级降级的回归测试。

为什么值得单测：这一层的错误形态是「不报错，只是墙偏了」。三级降级要保证
① 顺序固定（停车点 → 自动定位 → 手动），② 每一级不可信时能说清为什么，
③ 手动级一定带告警（它是唯一没有自动校验的一级），④ 图外起步一律拒。

运行：python3 dev/test_mapcontinue.py
"""
import math
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
import mapcontinue as C


def loc(x, y, th=0.0, score=0.88, ratio=0.61):
    return {'x': x, 'y': y, 'th': th, 'score': score, 'ratio': ratio,
            'second': 0.5, 'second_pose': None, 'ok': score >= 0.75 and ratio <= 0.80}


class ParkingTests(unittest.TestCase):
    """停车点是「假设」而不是「量出来的」：续建启动时 map 系是新的，
    map→base_footprint 恒在原点附近，站没站在框内量不出来。所以有停车点就用，
    靠自检兜底。这里锁住这个语义，免得哪天改回「比对位姿」——那会让第 1 级永远用不上。"""

    PARK = {'pose': [1.0, 2.0, 0.5]}

    def test_有停车点就直接用(self):
        pose, why = C.choose_parking(self.PARK)
        self.assertEqual(pose, [1.0, 2.0, 0.5])
        self.assertEqual(why, '')

    def test_没有定位也能用停车点(self):
        # 关键：不能用 robot_pose 当必要条件
        pose, why = C.choose_parking(self.PARK, None)
        self.assertEqual(pose, [1.0, 2.0, 0.5])
        self.assertEqual(why, '')

    def test_定位在别处也仍然采用但会提示(self):
        pose, _ = C.choose_parking(self.PARK, (9.0, 9.0, 0.0))
        self.assertEqual(pose, [1.0, 2.0, 0.5])
        note = C.parking_note(self.PARK, (9.0, 9.0, 0.0))
        self.assertIn('离停车点', note)
        self.assertIn('改用自动定位', note)

    def test_说明里写清是假设(self):
        note = C.parking_note(self.PARK)
        self.assertIn('假设', note)
        self.assertIn('量不出来', note)

    def test_没有停车点(self):
        pose, why = C.choose_parking(None)
        self.assertIsNone(pose)
        self.assertIn('没有配置续建停车点', why)
        self.assertEqual(C.parking_note(None), '')


class GloblocTests(unittest.TestCase):
    def test_门限和globloc一致(self):
        self.assertEqual(C.GLOBLOC_MIN_SCORE, 0.75)
        self.assertEqual(C.GLOBLOC_MAX_RATIO, 0.80)

    def test_分数够且没歧义可用(self):
        pose, why = C.accept_globloc(loc(3.0, -1.0, 1.2))
        self.assertEqual(why, '')
        self.assertAlmostEqual(pose[2], 1.2)

    def test_分数不够(self):
        pose, why = C.accept_globloc(loc(0, 0, 0, score=0.62))
        self.assertIsNone(pose)
        self.assertIn('分数 0.62', why)

    def test_次优太接近不行(self):
        pose, why = C.accept_globloc(loc(0, 0, 0, ratio=0.9))
        self.assertIsNone(pose)
        self.assertIn('歧义', why)

    def test_报错和字段不全(self):
        self.assertIsNone(C.accept_globloc({'error': '点太少'})[0])
        self.assertIsNone(C.accept_globloc({'x': 1})[0])
        self.assertIsNone(C.accept_globloc(None)[0])

    def test_航向归一到pi以内(self):
        pose, _ = C.accept_globloc(loc(0, 0, 4 * math.pi))
        self.assertAlmostEqual(pose[2], 0.0, places=6)


class SettleTests(unittest.TestCase):
    PARK = {'pose': [0.0, 0.0, 0.0]}

    def test_顺序一_停车点优先(self):
        r = C.settle(self.PARK, (0.0, 0.0, 0.0), loc(5, 5))
        self.assertEqual(r['source'], 'parking')
        self.assertEqual(r['tried'], [])
        self.assertEqual(r['warn'], '')

    def test_顺序二_没有停车点才自动定位(self):
        # 停车点一旦存在就直接采用（见 ParkingTests 的说明），所以第 2 级是在「没存停车点」时才走的
        r = C.settle(None, (9.0, 9.0, 0.0), loc(5, 5, 0.1))
        self.assertEqual(r['source'], 'globloc')
        self.assertEqual(r['tried'][0][0], 'parking')
        self.assertIn('没有配置续建停车点', r['tried'][0][1])

    def test_顺序三_前两级都没才手动且必告警(self):
        r = C.settle(None, (9.0, 9.0, 0.0), loc(0, 0, score=0.3), manual=[1, 2, 3])
        self.assertEqual(r['source'], 'manual')
        self.assertEqual(r['pose'], [1.0, 2.0, C.normalize_yaw(3.0)])
        self.assertEqual([t[0] for t in r['tried']], ['parking', 'globloc'])
        self.assertIn('手动', r['warn'])

    def test_三级都没结果时不编一个出来(self):
        r = C.settle(None, None, None)
        self.assertIsNone(r['source'])
        self.assertIsNone(r['pose'])
        self.assertIn('没有手动给位姿', r['note'])

    def test_force_manual跳过前两级(self):
        r = C.settle(self.PARK, (0.0, 0.0, 0.0), loc(5, 5), manual=[1, 1, 0], force='manual')
        self.assertEqual(r['source'], 'manual')
        self.assertEqual(r['tried'], [])

    def test_force只能填manual(self):
        with self.assertRaises(ValueError):
            C.settle(self.PARK, (0, 0, 0), loc(0, 0), force='parking')


class CoverageTests(unittest.TestCase):
    # origin (-6.86, -11.8)，32.6 × 15.2 m，0.05 m
    BASE = (-6.86, -11.8, 652, 304, 0.05)

    def test_图内图外(self):
        self.assertTrue(C.in_coverage(self.BASE, (0.0, 0.0)))
        self.assertFalse(C.in_coverage(self.BASE, (30.0, 0.0)))
        self.assertFalse(C.in_coverage(self.BASE, (0.0, -20.0)))

    def test_边界按左下原点算(self):
        self.assertTrue(C.in_coverage(self.BASE, (-6.85, -11.79)))
        self.assertFalse(C.in_coverage(self.BASE, (-6.87, -11.79)))

    def test_check_start_拦图外(self):
        ok, why = C.check_start(self.BASE, [30.0, 0.0, 0.0])
        self.assertFalse(ok)
        self.assertIn('不在基准图覆盖范围内', why)

    def test_check_start_放行图内(self):
        ok, why = C.check_start(self.BASE, [0.0, 0.0, 0.0])
        self.assertTrue(ok, why)

    def test_没有位姿(self):
        self.assertFalse(C.check_start(self.BASE, None)[0])


class StartFixTests(unittest.TestCase):
    """自检在别处量的修正量换算回起点：起点和量的位置在同一个 slam 坐标系里，套同一个刚体变换。"""

    def test_纯平移(self):
        x, y, th = C.start_fix([1.0, 2.0, 0.5], (5.0, 6.0), [0.3, -0.2, 0.0])
        self.assertAlmostEqual(x, 1.3); self.assertAlmostEqual(y, 1.8); self.assertAlmostEqual(th, 0.5)

    def test_绕量的位置转(self):
        # 起点在量的位置正西 2 m，坐标系偏 +90°：起点应落到量的位置正南 2 m，朝向也加 90°
        x, y, th = C.start_fix([3.0, 4.0, 0.0], (5.0, 4.0), [0.0, 0.0, 90.0])
        self.assertAlmostEqual(x, 5.0, places=6); self.assertAlmostEqual(y, 2.0, places=6)
        self.assertAlmostEqual(th, math.pi / 2, places=6)

    def test_量的位置就是起点(self):
        x, y, th = C.start_fix([1.0, 2.0, 0.2], (1.0, 2.0), [0.1, 0.0, -10.0])
        self.assertAlmostEqual(x, 1.1); self.assertAlmostEqual(y, 2.0)
        self.assertAlmostEqual(th, 0.2 - math.radians(10))

    def test_缺数据(self):
        self.assertIsNone(C.start_fix([1.0, 2.0, 0.0], (0.0, 0.0), None))
        self.assertIsNone(C.start_fix([1.0, 2.0, 0.0], (0.0, 0.0), [1, 2]))


if __name__ == '__main__':
    unittest.main(verbosity=2)
