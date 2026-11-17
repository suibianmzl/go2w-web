"""续建元数据与前置检查的回归测试。

重点验三件事，都是「错了要到收工才发现」的那类：
  1. 没有完整 posegraph 的图不能当基准（lab_20261021 就是这种）
  2. 换了运控策略 / 改了切片高度要拦下来，而不是续建完看到墙错层
  3. 续建绝不覆盖同名地图

运行：python3 dev/test_mapmeta.py
"""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
import mapmeta as M


class Workspace:
    """临时 data/maps，造四件套。"""

    def __init__(self):
        self.dir = tempfile.mkdtemp()

    def make(self, name, *, posegraph=True):
        for ext in ('.yaml', '.pgm'):
            Path(self.dir, name + ext).write_text('x')
        if posegraph:
            for ext in ('.posegraph', '.data'):
                Path(self.dir, name + ext).write_text('x')
        return name

    def meta(self, name, **fields):
        M.write(self.dir, M.build(name, **fields))


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.w = Workspace()

    def test_没有基准图(self):
        ok, reasons, _ = M.preflight(self.w.dir, 'nope')
        self.assertFalse(ok)
        self.assertIn('没有基准图', reasons[0])

    def test_没有posegraph的图不能当基准(self):
        # 1116 之前的 lab_20261021 就是这种：只有 pgm/yaml，走不了续建
        self.w.make('old', posegraph=False)
        ok, reasons, _ = M.preflight(self.w.dir, 'old')
        self.assertFalse(ok)
        self.assertIn('posegraph', reasons[0])

    def test_完整四件套可以续建(self):
        self.w.make('base')
        ok, reasons, _ = M.preflight(self.w.dir, 'base')
        self.assertTrue(ok, reasons)

    def test_同名目标直接拒(self):
        self.w.make('base')
        self.w.make('taken')
        ok, reasons, _ = M.preflight(self.w.dir, 'base', target='taken')
        self.assertFalse(ok)
        self.assertTrue(any('同名' in r for r in reasons))

    def test_策略变了要拦(self):
        self.w.make('base')
        self.w.meta('base', parent=None, start_pose=[0, 0, 0], start_source='manual',
                    policy='mjlab_go2w_dgv2_m22_scratch_w02_7999')
        ok, reasons, _ = M.preflight(self.w.dir, 'base', policy='some_other_policy')
        self.assertFalse(ok)
        self.assertIn('运控策略变了', reasons[0])
        # 同一策略就放行
        ok, reasons, _ = M.preflight(self.w.dir, 'base', policy='mjlab_go2w_dgv2_m22_scratch_w02_7999')
        self.assertTrue(ok, reasons)

    def test_切片高度变了要拦(self):
        self.w.make('base')
        self.w.meta('base', parent=None, start_pose=[0, 0, 0], start_source='manual',
                    slice=[0.30, 1.60])
        ok, reasons, _ = M.preflight(self.w.dir, 'base', slice_now=[0.30, 2.00])
        self.assertFalse(ok)
        self.assertIn('切片高度变了', reasons[0])
        # 浮点尾数差异不算变
        ok, reasons, _ = M.preflight(self.w.dir, 'base', slice_now=[0.3001, 1.5999])
        self.assertTrue(ok, reasons)

    def test_没有meta的基准放行但告警(self):
        self.w.make('base')
        ok, reasons, warns = M.preflight(self.w.dir, 'base', policy='p')
        self.assertTrue(ok, reasons)
        self.assertTrue(any('没有 meta.json' in w for w in warns))


class LineageTests(unittest.TestCase):
    def setUp(self):
        self.w = Workspace()

    def test_追溯血缘并告警层数(self):
        for n in ('r', 'c1', 'c2', 'c3'):
            self.w.make(n)
        self.w.meta('c1', parent='r', start_pose=[0, 0, 0], start_source='manual')
        self.w.meta('c2', parent='c1', start_pose=[0, 0, 0], start_source='manual')
        self.w.meta('c3', parent='c2', start_pose=[0, 0, 0], start_source='manual')
        self.assertEqual(M.lineage(self.w.dir, 'c3'), ['c3', 'c2', 'c1', 'r'])
        ok, _, warns = M.preflight(self.w.dir, 'c3')
        self.assertTrue(ok)
        self.assertTrue(any('续建链已经有 4 层' in w for w in warns))

    def test_环形血缘不会死循环(self):
        self.w.make('a')
        self.w.make('b')
        self.w.meta('a', parent='b', start_pose=[0, 0, 0], start_source='manual')
        self.w.meta('b', parent='a', start_pose=[0, 0, 0], start_source='manual')
        self.assertEqual(M.lineage(self.w.dir, 'a'), ['a', 'b'])


class MetaTests(unittest.TestCase):
    def setUp(self):
        self.w = Workspace()

    def test_字段齐全且可往返(self):
        self.w.make('base')
        meta = M.build('base_c1', 'base', [1.23456, -2.5, 0.789], 'globloc',
                       globloc={'score': 0.88, 'ratio': 0.61},
                       check={'fit': 0.96, 'correction': [0.02, -0.01, 0.1]},
                       policy='p', slice=[0.3, 1.6], free_thresh_saved=0.196,
                       git_revision='7768e59', reason='补东侧门洞')
        M.write(self.w.dir, meta)
        back = M.read(self.w.dir, 'base_c1')
        self.assertEqual(back['schema'], 1)
        self.assertEqual(back['parent'], 'base')
        self.assertEqual(back['start_pose'], [1.2346, -2.5, 0.789])   # 四舍五入到 4 位
        self.assertEqual(back['start_source'], 'globloc')
        self.assertEqual(back['check'], {'fit': 0.96, 'correction': [0.02, -0.01, 0.1]})
        self.assertRegex(back['continued_at'], r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d')

    def test_坏字段直接拒(self):
        with self.assertRaises(ValueError):
            M.build('x', 'base', [0, 0], 'manual')          # 位姿少一项
        with self.assertRaises(ValueError):
            M.build('x', 'base', [0, 0, 0], 'guess')        # 不认识的说法

    def test_半截文件当成没有(self):
        Path(self.w.dir, 'broken.meta.json').write_text('{"name": "bro')
        self.assertIsNone(M.read(self.w.dir, 'broken'))


class RouteRetargetTests(unittest.TestCase):
    def test_只改指向旧图的路由(self):
        routes = [
            {'id': 'a', 'map': 'lab_20261116', 'points': [{'x': 1, 'y': 2}]},
            {'id': 'b', 'map': 'lab_20261105', 'points': []},
        ]
        out, n = M.retarget_routes(routes, 'lab_20261116', 'lab_20261116_c1')
        self.assertEqual(n, 1)
        self.assertEqual(out[0]['map'], 'lab_20261116_c1')
        self.assertEqual(out[1]['map'], 'lab_20261105')
        # 站点坐标原样保留：续建不改坐标系，这是选原位续建的理由
        self.assertEqual(out[0]['points'], [{'x': 1, 'y': 2}])
        # 原来的列表没被就地改掉
        self.assertEqual(routes[0]['map'], 'lab_20261116')


class SliceTests(unittest.TestCase):
    def test_读不到配置用默认值(self):
        got, src = M.current_slice(config_dir='/nonexistent', env={})
        self.assertEqual(got, list(M.SLICE_DEFAULT))
        self.assertEqual(src, 'default')

    def test_从配置读出切片(self):
        d = tempfile.mkdtemp()
        Path(d, 'pcl2scan_slam.yaml').write_text(
            'pcl2scan_slam:\n  ros__parameters:\n    min_height: 0.30\n    max_height: 1.60\n')
        got, src = M.current_slice(config_dir=d, env={})
        self.assertEqual(got, [0.30, 1.60])
        self.assertEqual(src, 'config')


class PolicySourceTests(unittest.TestCase):
    """web 进程的环境变量里没有策略名（go2w web start 不转发），要能从 runtime.env 读到。"""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env_file = os.path.join(self.tmp.name, 'runtime.env')

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, text):
        with open(self.env_file, 'w', encoding='utf-8') as f:
            f.write(text)

    def test_env_wins(self):
        self.write('GO2W_CONTROL_POLICY="from_file"\n')
        self.assertEqual(M.current_policy({'GO2W_CONTROL_POLICY': 'from_env'}, [self.env_file]),
                         ('from_env', 'env'))

    def test_reads_quoted_value_from_runtime_env(self):
        # 和扩展坞上 /opt/go2w/config/runtime.env 第 8 行同样的写法
        self.write('GO2W_MAP=lab.yaml\nGO2W_CONTROL_POLICY="mjlab_go2w_dgv2_m22_scratch_w02_7999"\n')
        self.assertEqual(M.current_policy({}, [self.env_file]),
                         ('mjlab_go2w_dgv2_m22_scratch_w02_7999', self.env_file))

    def test_export_single_quote_and_comment(self):
        self.write("# GO2W_CONTROL_POLICY=commented\nexport GO2W_CONTROL_POLICY='p1' # 注释\n")
        self.assertEqual(M.current_policy({}, [self.env_file])[0], 'p1')

    def test_missing_everywhere(self):
        self.assertEqual(M.current_policy({}, [os.path.join(self.tmp.name, 'nope')]), (None, None))

    def test_prefix_key_not_confused(self):
        self.write('GO2W_CONTROL_POLICY_OLD=x\n')
        self.assertEqual(M.current_policy({}, [self.env_file]), (None, None))


if __name__ == '__main__':
    unittest.main(verbosity=2)
