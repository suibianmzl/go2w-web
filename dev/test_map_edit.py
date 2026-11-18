"""地图编辑（server/mapedit.py）的回归测试。

重点：
  1. 只改发来的格子，其余像素字节原样保留；yaml 除了 image 一行照抄
  2. 读回来和编辑结果逐格一致（按 map_server trinary 规则）
  3. 网页编辑的底图和硬盘上的不一样（CRC 对不上）、尺寸不对、同名、越界 → 拒绝，且不留文件
  4. negate: 1 的图也能写对
  5. 编辑后的图不能当续建基准，续建原图时提醒编辑会丢

运行：python3 dev/test_map_edit.py [真实地图.yaml]（给了就再用真实地图做一遍往返）
"""
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
import mapedit as E
import mapfile
import mapmeta as M

REAL = sys.argv.pop(1) if len(sys.argv) > 1 and sys.argv[1].endswith('.yaml') else None


def write_map(d, name, px, *, negate=0, extra=''):
    h, w = px.shape
    Path(d, name + '.pgm').write_bytes(f'P5\n{w} {h}\n255\n'.encode() + px.astype(np.uint8).tobytes())
    Path(d, name + '.yaml').write_text(
        f'image: {name}.pgm\nmode: trinary\nresolution: 0.05\norigin: [-1.5, -2.0, 0]\n'
        f'negate: {negate}\noccupied_thresh: 0.65\nfree_thresh: 0.196\n{extra}')


def runs_from(old, new):
    """和网页一样：变化的格子按 (起点, 长度, 新值) 连成段。"""
    a, b = old.reshape(-1), new.reshape(-1)
    out, i, n = [], 0, a.size
    while i < n:
        if a[i] == b[i]:
            i += 1
            continue
        j = i + 1
        while j < n and a[j] != b[j] and b[j] == b[i]:
            j += 1
        out.append([i, j - i, int(b[i])])
        i = j
    return out


class MapEditTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        px = np.full((40, 60), 205, np.uint8)
        px[5:35, 5:55] = 254
        px[5, 5:55] = px[34, 5:55] = 0
        px[20, 30] = 0           # 一个噪点
        px[10, 10] = 140         # 非标准灰度（未知）：没改到的格子要原样保留
        write_map(self.d, 'base', px, extra='# 注释保留\n')
        self.px = px
        self.grid = mapfile.load(os.path.join(self.d, 'base.yaml'))['grid']

    def tearDown(self):
        shutil.rmtree(self.d)

    def edit(self, new, name='base_e1', **kw):
        h, w = self.grid.shape
        args = dict(runs=runs_from(self.grid, new), base_crc=E.grid_crc(self.grid), w=w, h=h)
        args.update(kw)
        return E.save(self.d, 'base', name, **args)

    def test_denoise_roundtrip(self):
        new = self.grid.copy()
        new[40 - 1 - 20, 30] = 0                      # 噪点（图片第 20 行）→ 空地
        changed, info = self.edit(new, summary={'denoise_blobs': 1, 'denoise_cells': 1, 'evil': 'x'})
        self.assertEqual(changed, 1)
        back = mapfile.load(os.path.join(self.d, 'base_e1.yaml'))['grid']
        self.assertTrue(np.array_equal(back, new))
        px2, _ =mapfile.read_pgm(os.path.join(self.d, 'base_e1.pgm'))
        diff = np.argwhere(px2 != self.px)
        self.assertEqual(diff.tolist(), [[20, 30]])   # 只动了这一个像素
        self.assertEqual(int(px2[20, 30]), 254)       # 和 map_saver 一样用 254 表示空地
        self.assertEqual(int(px2[10, 10]), 140)       # 没改的非标准灰度原样保留
        y = Path(self.d, 'base_e1.yaml').read_text()
        self.assertIn('image: base_e1.pgm', y)
        self.assertIn('origin: [-1.5, -2.0, 0]', y)
        self.assertIn('# 注释保留', y)
        meta = M.read(self.d, 'base_e1')
        self.assertEqual((meta['parent'], meta['tool'], meta['changed_cells']), ('base', 'web-edit', 1))
        self.assertEqual(meta['edit'], {'denoise_blobs': 1, 'denoise_cells': 1})
        self.assertEqual(M.lineage(self.d, 'base_e1'), ['base_e1', 'base'])

    def test_all_three_values(self):
        new = self.grid.copy()
        new[0:3, 0:4] = 0      # 未知 → 空地
        new[12, 12:20] = 100   # 空地 → 墙
        new[20:25, 20:40] = -1  # 空地 → 未知
        self.edit(new)
        back = mapfile.load(os.path.join(self.d, 'base_e1.yaml'))['grid']
        self.assertTrue(np.array_equal(back, new))
        px2, _ = mapfile.read_pgm(os.path.join(self.d, 'base_e1.pgm'))
        self.assertEqual(set(np.unique(px2[px2 != self.px]).tolist()), {254, 0, 205})

    def test_rejects_stale_base(self):
        new = self.grid.copy(); new[0, 0] = 0
        with self.assertRaisesRegex(ValueError, '不一样'):
            self.edit(new, base_crc=E.grid_crc(self.grid) ^ 1)
        with self.assertRaisesRegex(ValueError, '尺寸对不上'):
            self.edit(new, w=61)
        self.assertFalse(any(f.startswith('base_e1') for f in os.listdir(self.d)))

    def test_rejects_bad_runs_and_no_change(self):
        h, w = self.grid.shape
        crc = E.grid_crc(self.grid)
        for runs in ([], [[0, 1, 50]], [[w * h - 1, 2, 0]], [[-1, 1, 0]], [[0, 1]], [[0, True, 0]], 'x'):
            with self.assertRaises(ValueError, msg=runs):
                E.save(self.d, 'base', 'base_e1', runs, crc, w, h)
        with self.assertRaisesRegex(ValueError, '没有任何变化'):
            E.save(self.d, 'base', 'base_e1', [[0, 1, -1]], crc, w, h)   # (0,0) 本来就是未知
        self.assertFalse(any(f.startswith('base_e1') for f in os.listdir(self.d)))

    def test_never_overwrites(self):
        new = self.grid.copy(); new[0, 0] = 0
        self.edit(new)
        with self.assertRaisesRegex(ValueError, '已经有'):
            self.edit(new)
        with self.assertRaisesRegex(ValueError, '已经有'):
            self.edit(new, name='base')
        Path(self.d, 'other.posegraph').write_text('x')
        with self.assertRaisesRegex(ValueError, '已经有'):
            self.edit(new, name='other')

    def test_negate_map(self):
        px = np.full((10, 12), 50, np.uint8)   # negate: p = v → 50/255 = 0.196 不小于 free → 未知
        px[2:8, 2:10] = 0                      # 空地
        px[2, 2:10] = 255                      # 墙
        write_map(self.d, 'neg', px, negate=1)
        g = mapfile.load(os.path.join(self.d, 'neg.yaml'))['grid']
        self.assertEqual(set(np.unique(g).tolist()), {-1, 0, 100})
        new = g.copy(); new[4, 4] = 100; new[5, 5] = -1; new[0, 0] = 0
        E.save(self.d, 'neg', 'neg_e1', runs_from(g, new), E.grid_crc(g), 12, 10)
        self.assertTrue(np.array_equal(mapfile.load(os.path.join(self.d, 'neg_e1.yaml'))['grid'], new))

    def test_continuation_messages(self):
        new = self.grid.copy(); new[0, 0] = 0
        for ext in ('.posegraph', '.data'):
            Path(self.d, 'base' + ext).write_text('x')
        self.edit(new)
        ok, reasons, _ = M.preflight(self.d, 'base_e1')
        self.assertFalse(ok)
        self.assertTrue(any('编辑过' in r and 'base' in r for r in reasons), reasons)
        ok, reasons, warns = M.preflight(self.d, 'base')
        self.assertTrue(ok, reasons)
        self.assertTrue(any('base_e1' in w and '编辑' in w for w in warns), warns)

    def test_derived_map_drops_embedded_teaching_but_keeps_source(self):
        source=Path(self.d,'base.yaml')
        source.write_text(source.read_text()+'go2w_taught_route:\n  schema: 1\n  reference:\n    image: not_a_map.pgm\n')
        original=source.read_bytes()
        new=self.grid.copy();new[0,0]=0
        self.edit(new)
        self.assertNotIn('go2w_taught_route',Path(self.d,'base_e1.yaml').read_text())
        self.assertEqual(source.read_bytes(),original)
        np.testing.assert_array_equal(mapfile.load(Path(self.d,'base_e1.yaml'))['grid'],new)

    @unittest.skipUnless(REAL, '没给真实地图')
    def test_real_map(self):
        name = Path(REAL).stem
        for f in Path(REAL).parent.glob(name + '.*'):
            if f.suffix in ('.yaml', '.pgm'):
                shutil.copy(f, self.d)
        g = mapfile.load(os.path.join(self.d, name + '.yaml'))['grid']
        new = g.copy()
        occ = np.argwhere(new == 100)[:50]
        new[occ[:, 0], occ[:, 1]] = 0
        new[:20, :] = -1
        runs = runs_from(g, new)
        changed, _ = E.save(self.d, name, name + '_e1', runs, E.grid_crc(g), g.shape[1], g.shape[0])
        self.assertEqual(changed, int(np.count_nonzero(new != g)))
        self.assertTrue(np.array_equal(mapfile.load(os.path.join(self.d, name + '_e1.yaml'))['grid'], new))
        print(f'\n  真实地图 {name}：{g.shape[1]}×{g.shape[0]}，{len(runs)} 段，改了 {changed} 格，读回一致')


if __name__ == '__main__':
    unittest.main(verbosity=1)
