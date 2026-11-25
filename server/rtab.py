"""RTAB-Map 影子库：建图时顺带录一个 RTAB-Map 数据库，日后用来「局部重扫、替换一块」。

## 角色（2026-11-20 定）

- **影子**：RTAB-Map 只读 /scan 和 odom TF，往 data/rtabmap/<库>.db 里录，不发 TF（map 坐标系叫
  rtab_map），不影响 slam_toolbox / AMCL / Nav2。导航仍然只用 slam_toolbox 存的 pgm + AMCL。
- **一张网页地图对应一个库**：建图时存下的每张图（手动保存、自动保存）都记进 data/rtabmap/links.json
  （地图名 → 库文件名）。续建、网页编辑出来的图坐标系不变，沿 meta.json 的 parent 继承基准图的库。
  对齐导出（align_map）的图坐标变了，不继承。
- **局部重扫** = 在定位 / 导航档位下（已定位），用当前位姿当起点在旧库上续录一个新会话：
  先只定位（rtab_localize_params.yaml），对上旧节点再切建图。收工后报告两轮之间的约束数，
  有约束才能做局部替换（P2：替换 / 导出成新地图，原图不动）。

## 坐标系为什么对得上

slam_toolbox 和 RTAB-Map 启动时 map→odom 都是单位阵，FAST-LIO 的 odom 从建图开始时的位置算起。
只要 RTAB-Map 在同一次建图（同一次 FAST-LIO）里启动，两边的地图坐标系就是同一个（之后各自做回环，
差别在厘米级）。所以 RTAB-Map 晚几秒启动没关系，但**续建**（slam_toolbox 把坐标系换成基准图的）
期间录的库和存出来的图坐标系不同，所以续建一开始就停录、也不记关联。

## 进程

P1 由网页后端起 rtabmap 进程（start_new_session，网页重启不会带走它），状态写在
data/rtabmap/session.json，后端重启后按 /proc 里的命令行认回来。以后放进 supervisor 的进程组（P3）时
只换 RtabRunner。停止 = SIGINT，rtabmap 收到后把库写完再退出（几秒到几十秒），期间不能切档位。

本地调试（GO2W_FAKE_PROCS=1）用 dev/fake_ros/go2w_fake_rtab.py 的假进程。
"""
import asyncio
import json
import math
import os
import re
import shutil
import signal
import sqlite3
import struct
import sys
import time

import mapmeta as MM

DB_NAME = re.compile(r'^[A-Za-z0-9_\-]{1,64}\.db$')
MAP_NAME = re.compile(r'^[A-Za-z0-9_\-]{1,48}$')
BIN = os.environ.get('GO2W_RTABMAP_BIN', '/opt/ros/humble/lib/rtabmap_slam/rtabmap')
STOP_TIMEOUT = float(os.environ.get('GO2W_RTAB_STOP_TIMEOUT', '120'))   # SIGINT 后等它存完库
LOCALIZE_TIMEOUT = 60.0      # 局部重扫：和旧库对上的最长时间
START_WAIT = 3.0             # 起进程后看几秒没退出才算起来了
AUTO_WAIT = 120.0            # 建图档启动后最多等多久自动开录（等 slam / scan 起来）
ORPHAN_GRACE = 30.0          # 依赖的档位停了多久还在跑，就自动停（档位被别处停掉时）
KEEP_BACKUPS = 3             # 每个库保留最近几份重扫前的备份
KIND_NAME = {'map': '建图同步录制', 'continue': '局部重扫'}


# ---------- 库文件（sqlite，rtabmap 不在跑时才读写） ----------
def db_summary(path):
    """每个会话（map_id）的节点数 + 跨会话约束数。读不了返回 None。"""
    try:
        db = sqlite3.connect(f'file:{path}?mode=ro', uri=True, timeout=5.0)
    except sqlite3.Error:
        return None
    try:
        sess = dict(db.execute('SELECT map_id, count(*) FROM Node GROUP BY map_id').fetchall())
        mid = dict(db.execute('SELECT id, map_id FROM Node').fetchall())
        cross = sum(1 for a, b in db.execute('SELECT from_id, to_id FROM Link')
                    if a in mid and b in mid and mid[a] != mid[b]) // 2
        return {'sessions': [{'id': int(k), 'nodes': int(v)} for k, v in sorted(sess.items())],
                'cross': int(cross)}
    except sqlite3.Error:
        return None
    finally:
        db.close()


def set_start_pose(path, x, y, yaw):
    """RTAB-Map 定位模式启动时假设狗还在上次停下的地方（Admin.opt_last_localization，3×4 float32），
    不理会 initialpose（11-19 .21 因此偏了 6 m）。启动前改成真实起点。返回库里原来记的 (x, y, yaw) 或 None。"""
    c, s = math.cos(yaw), math.sin(yaw)
    db = sqlite3.connect(path, timeout=5.0)
    try:
        row = db.execute('SELECT opt_last_localization FROM Admin').fetchone()
        old = None
        if row and row[0] and len(row[0]) == 48:
            m = struct.unpack('<12f', row[0])
            old = (m[3], m[7], math.atan2(m[4], m[0]))
        db.execute('UPDATE Admin SET opt_last_localization = ?',
                   (struct.pack('<12f', c, -s, 0, x, s, c, 0, y, 0, 0, 1, 0),))
        db.commit()
        return old
    finally:
        db.close()


def backup(path, keep=KEEP_BACKUPS):
    """重扫前备份成 <库>.db.bak-月日_时分秒，只留最近 keep 份。返回备份文件名。"""
    dst = f"{path}.bak-{time.strftime('%m%d_%H%M%S')}"
    shutil.copy2(path, dst)
    d, base = os.path.dirname(path), os.path.basename(path) + '.bak-'
    olds = sorted(f for f in os.listdir(d) if f.startswith(base))
    for f in olds[:-keep]:
        try:
            os.remove(os.path.join(d, f))
        except OSError:
            pass
    return os.path.basename(dst)


def tail(path, n=8):
    try:
        with open(path, 'rb') as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 8192))
            lines = f.read().decode('utf-8', 'replace').splitlines()
        return '\n'.join(lines[-n:])
    except OSError:
        return ''


def _cmdlines():
    for p in os.listdir('/proc'):
        if not p.isdigit():
            continue
        try:
            with open(f'/proc/{p}/cmdline', 'rb') as f:
                yield int(p), f.read().split(b'\0')
        except OSError:
            continue


# ---------- 进程（真机） ----------
class RtabRunner:
    def __init__(self, data_dir, tools_dir):
        self.dir = os.path.join(str(data_dir), 'rtabmap')
        self.tools = os.path.join(str(tools_dir), 'rtab')
        self.proc = None          # 本进程起的 rtabmap（用来收尸）；后端重启后认回来的孤儿是 None

    def available(self):
        """镜像里有 rtabmap（V1.0.0.10 起自带），release 里有参数文件和 helper。"""
        return (os.access(BIN, os.X_OK)
                and all(os.path.isfile(os.path.join(self.tools, f))
                        for f in ('rtab_params.yaml', 'rtab_localize_params.yaml', 'rtab_helper.py')))

    def pids(self):
        """在跑的 rtabmap（命名空间 /rtab 的那个；别人手动起的其他 rtabmap 不算）。"""
        exe = os.path.basename(BIN).encode()
        out = []
        for pid, argv in _cmdlines():
            if argv and os.path.basename(argv[0]) == exe and b'__ns:=/rtab' in argv:
                out.append(pid)
        return out

    async def start(self, db, localize=False):
        os.makedirs(self.dir, exist_ok=True)
        path = os.path.join(self.dir, db)
        argv = [BIN, '--ros-args', '-r', '__ns:=/rtab', '-r', 'scan:=/scan',
                '--params-file', os.path.join(self.tools, 'rtab_params.yaml')]
        if localize:
            argv += ['--params-file', os.path.join(self.tools, 'rtab_localize_params.yaml')]
        argv += ['-p', f'database_path:={path}']
        log_path = os.path.join(self.dir, db[:-3] + '.log')
        with open(log_path, 'ab', buffering=0) as log:
            log.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} {'定位后续录' if localize else '新录'} =====\n".encode())
            self.proc = await asyncio.create_subprocess_exec(
                *argv, stdin=asyncio.subprocess.DEVNULL, stdout=log, stderr=asyncio.subprocess.STDOUT,
                cwd=self.dir, start_new_session=True)
        try:
            await asyncio.wait_for(self.proc.wait(), START_WAIT)
        except asyncio.TimeoutError:
            return self.proc.pid
        self.proc = None
        raise RuntimeError(f'rtabmap 启动后马上退出了：{tail(log_path, 4) or "看 data/rtabmap/" + db[:-3] + ".log"}')

    async def stop(self, timeout=STOP_TIMEOUT):
        """SIGINT 让它存库退出。返回 True = 已退出。绝不 SIGKILL（会丢库）。"""
        pids = self.pids()
        for pid in pids:
            try:
                os.kill(pid, signal.SIGINT)
            except ProcessLookupError:
                pass
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.proc is not None and self.proc.returncode is None:
                try:
                    await asyncio.wait_for(self.proc.wait(), 0.5)
                except asyncio.TimeoutError:
                    pass
            else:
                await asyncio.sleep(0.5)
            if not self.pids():
                self.proc = None
                return True
        return False

    async def helper(self, args, timeout, log_name):
        """跑 tools/rtab/rtab_helper.py（定位 / pause / resume），输出追加到 data/rtabmap/<log_name>。
        返回 (退出码, 最后几行输出)。"""
        log_path = os.path.join(self.dir, log_name)
        with open(log_path, 'ab', buffering=0) as log:
            log.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} {' '.join(args)} =====\n".encode())
            p = await asyncio.create_subprocess_exec(
                sys.executable, os.path.join(self.tools, 'rtab_helper.py'), *map(str, args),
                stdin=asyncio.subprocess.DEVNULL, stdout=log, stderr=asyncio.subprocess.STDOUT,
                start_new_session=True)
            try:
                rc = await asyncio.wait_for(p.wait(), timeout)
            except asyncio.TimeoutError:
                try:
                    os.killpg(p.pid, signal.SIGINT)
                except ProcessLookupError:
                    pass
                await p.wait()
                rc = -1
        return rc, tail(log_path, 3)


# ---------- 挂到 Server 上 ----------
class RtabWeb:
    """Server 的 RTAB 影子库操作。self.rtab 是当前会话（没有时 kind=None），self.rtab_last 是上一次的结果。"""

    def init_rtab(self, data, tools_dir, simulated=False):
        self.rtab_dir = os.path.join(str(data), 'rtabmap')
        self.rtab_maps_dir = os.path.join(str(data), 'maps')
        if simulated:
            from go2w_fake_rtab import FakeRtabRunner
            self.rtab_runner = FakeRtabRunner(data, self.node)
        else:
            self.rtab_runner = RtabRunner(data, tools_dir)
        self.rtab = self.rtab_idle()
        self.rtab_last = None
        self.rtab_lock = asyncio.Lock()
        self.rtab_pending = None      # 建图档启动的时刻：等建图跑起来后自动开录
        self.rtab_orphan = None       # 依赖的档位不在了的起始时刻
        self.rtab_avail = self.rtab_runner.available()
        settings = self.rtab_read('settings.json') or {}
        self.rtab_auto = bool(settings.get('auto', True))
        self.rtab_restore()

    @staticmethod
    def rtab_idle():
        return {'kind': None, 'db': None, 'map': None, 'phase': None, 'since': None, 'msg': '', 'paused_by': None}

    # ----- 小文件 -----
    def rtab_read(self, name):
        try:
            with open(os.path.join(self.rtab_dir, name), encoding='utf-8') as f:
                v = json.load(f)
            return v if isinstance(v, dict) else None
        except (OSError, ValueError):
            return None

    def rtab_write(self, name, data):
        os.makedirs(self.rtab_dir, exist_ok=True)
        path = os.path.join(self.rtab_dir, name)
        if data is None:
            try:
                os.remove(path)
            except OSError:
                pass
            return
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)

    def rtab_save_session(self):
        r = self.rtab
        self.rtab_write('session.json', None if r['kind'] is None else
                        {k: r[k] for k in ('kind', 'db', 'map', 'phase', 'since', 'paused_by')})

    def rtab_restore(self):
        """后端重启：rtabmap 还在跑就认回来；session.json 说在录但进程没了，说明是被硬杀的（重启狗 / 容器）。"""
        s = self.rtab_read('session.json')
        pids = self.rtab_runner.pids() if self.rtab_avail else []
        n = self.node
        if s and s.get('kind') in KIND_NAME and isinstance(s.get('db'), str) and DB_NAME.match(s['db']):
            if pids:
                self.rtab = {**self.rtab_idle(), **{k: s.get(k) for k in ('kind', 'db', 'map', 'phase', 'since', 'paused_by')}}
                if self.rtab['phase'] in ('starting', 'localizing', 'stopping'):
                    # 网页重启时定位 helper 跟着没了；rtabmap 还停在定位模式，这一轮作废，让用户停掉重来
                    self.rtab['phase'] = 'stale'
                    self.rtab['msg'] = '网页服务重启时正在对位，这一轮无效，请点「停止」后重新开始'
                n.event('INFO', f"RTAB 库 {s['db']} 还在录（{KIND_NAME[s['kind']]}），已接管")
                return
            n.event('WARN', f"RTAB 库 {s['db']} 上次没有正常停止（狗或容器重启了？），库可能缺最后一段")
            self.rtab_last = {'kind': s['kind'], 'db': s['db'], 'map': s.get('map'), 'wall': time.time(),
                              'ok': False, 'msg': '上次没有正常停止，库可能缺最后一段', 'summary': None}
            self.rtab_write('session.json', None)
        elif pids:
            n.event('WARN', f'发现不是网页启动的 rtabmap（pid {pids}），网页不会管它；要用网页录请先停掉它')

    def rtab_links(self):
        v = self.rtab_read('links.json') or {}
        return {k: d for k, d in v.items() if isinstance(k, str) and isinstance(d, str) and DB_NAME.match(d)}

    def rtab_db_for(self, name):
        """这张图对应的库（自己记的，或者沿续建 / 网页编辑的 parent 往上找）。没有返回 None。"""
        links = self.rtab_links()
        cur, seen = name, set()
        while cur and cur not in seen and len(seen) < 16:
            seen.add(cur)
            db = links.get(cur)
            if db:
                return db if os.path.isfile(os.path.join(self.rtab_dir, db)) else None
            meta = MM.read(self.rtab_maps_dir, cur) or {}
            if meta.get('tool') == 'web-edit' or meta.get('continued_at'):
                cur = meta.get('parent')
            else:
                return None
        return None

    def rtab_link(self, map_name):
        """建图时存图成功后调用：这张图和正在录的库对应起来。续建会话不记（坐标系不同）。"""
        r = self.rtab
        if r['kind'] != 'map' or r['phase'] not in ('recording', 'paused') or self.cont:
            return
        links = self.rtab_links()
        links[map_name] = r['db']
        try:
            self.rtab_write('links.json', links)
        except OSError as e:
            self.node.event('WARN', f'记录地图 {map_name} 和 RTAB 库的对应关系失败：{e}')

    def rtab_view(self):
        if not self.rtab_avail:
            return {'avail': False}
        r = self.rtab
        shown = self.shown_map()
        cur = None
        if r['kind']:
            cur = {**{k: r[k] for k in ('kind', 'db', 'map', 'phase', 'msg', 'paused_by')},
                   'elapsed': round(time.time() - r['since'], 1) if r['since'] else None}
        return {'avail': True, 'auto': self.rtab_auto, 'cur': cur, 'last': self.rtab_last,
                'pending': self.rtab_pending is not None,
                'map': shown, 'mapDb': self.rtab_db_for(shown) if shown else None}

    # ----- 操作 -----
    def rtab_set_auto(self, on, who):
        self.rtab_auto = bool(on)
        try:
            self.rtab_write('settings.json', {'auto': self.rtab_auto})
        except OSError as e:
            return False, f'保存设置失败：{e}'
        self.node.event('INFO', f"建图时{'自动' if on else '不再自动'}同时录 RTAB 库（{who}）")
        return True, '建图时自动同时录 RTAB 库' if on else '建图时不再自动录 RTAB 库'

    def rtab_check_free(self):
        if not self.rtab_avail:
            return '这台狗的镜像里没有 RTAB-Map（需要 V1.0.0.10 以上）或网页包缺少 tools/rtab'
        if self.rtab['kind']:
            return f"RTAB 库 {self.rtab['db']} 正在{KIND_NAME[self.rtab['kind']]}，先停止"
        if self.rtab_runner.pids():
            return '有一个不是网页启动的 rtabmap 在跑，先停掉它（sudo bash ~/go2w_temp/rtab.sh stop）'
        return None

    async def rtab_start_map(self, who, auto=False):
        """建图档里新录一个库：rtab_月日_时分秒.db。"""
        async with self.rtab_lock:
            err = self.rtab_check_free()
            if err:
                return False, err
            if self.prof('mapping') != 'run':
                return False, '建图没有在运行'
            if self.cont or self.cont_busy:
                return False, '续建期间不录 RTAB 库（续建的坐标系和这次建图不同）'
            db = f"rtab_{time.strftime('%m%d_%H%M%S')}.db"
            self.rtab = {**self.rtab_idle(), 'kind': 'map', 'db': db, 'phase': 'starting', 'since': time.time()}
            try:
                await self.rtab_runner.start(db)
            except Exception as e:
                self.rtab = self.rtab_idle()
                self.node.event('ERROR', f'RTAB 库没录起来：{e}')
                return False, f'RTAB 库没录起来：{e}'
            self.rtab['phase'] = 'recording'
            self.rtab_save_session()
        self.node.event('INFO', f"开始同步录 RTAB 库 data/rtabmap/{db}（{'建图自动' if auto else who}）")
        return True, f'开始录 RTAB 库 {db}'

    async def rtab_continue(self, map_name, who, force=False):
        """局部重扫：定位 / 导航档位在用这张图且已定位时，从当前位姿起在它的库上续录一轮。"""
        n = self.node
        async with self.rtab_lock:
            err = self.rtab_check_free()
            if err:
                return False, err
            map_name = map_name or self.shown_map()
            if not isinstance(map_name, str) or not MAP_NAME.match(map_name):
                return False, '地图名不对'
            if not any(self.prof(m) == 'run' for m in ('localization', 'navigation')) or n.map_current != map_name:
                return False, f'先用 {map_name} 启动导航（或定位）并定位成功，再开始局部重扫'
            db = self.rtab_db_for(map_name)
            if not db:
                return False, f'{map_name} 没有对应的 RTAB 库（只有开了「同时录 RTAB 库」时建的图才有）'
            pose = self.rtab_pose()
            if pose is None:
                return False, '拿不到机器人在地图上的位置（map→base_footprint），先定位'
            if n.amcl is None:
                return False, 'AMCL 还没有给出定位，先定位（自动定位或给初始位姿）'
            path = os.path.join(self.rtab_dir, db)
            loop = asyncio.get_running_loop()
            try:
                bak = await loop.run_in_executor(None, backup, path)
                old = await loop.run_in_executor(None, set_start_pose, path, *pose)
            except Exception as e:
                return False, f'准备库失败：{e}'
            self.rtab = {**self.rtab_idle(), 'kind': 'continue', 'db': db, 'map': map_name,
                         'phase': 'starting', 'since': time.time(), 'msg': f'已备份 {bak}'}
            try:
                await self.rtab_runner.start(db, localize=True)
            except Exception as e:
                self.rtab = self.rtab_idle()
                n.event('ERROR', f'局部重扫没开始：{e}')
                return False, f'局部重扫没开始：{e}'
            self.rtab['phase'] = 'localizing'
            self.rtab['msg'] = '正在和旧库对位：让狗慢慢走一两米或原地转一圈'
            self.rtab_save_session()
        n.event('INFO', f"局部重扫 {map_name}（库 {db}，{who}）：起点 x={pose[0]:.2f} y={pose[1]:.2f} "
                        f"朝向 {math.degrees(pose[2]):.0f}°" +
                        (f"（库里记的上次位置 {old[0]:.2f}, {old[1]:.2f}）" if old else '') + f'，已备份 {bak}')
        self.background(self.rtab_localize(db, pose, force))
        return True, '正在和旧库对位（最多 60 s）：让狗慢慢走一两米或原地转一圈'

    async def rtab_localize(self, db, pose, force):
        args = ['localize', f'{pose[0]:.3f}', f'{pose[1]:.3f}', f'{pose[2]:.4f}', str(int(LOCALIZE_TIMEOUT))]
        if force:
            args.append('force')
        try:
            rc, out = await self.rtab_runner.helper(args, LOCALIZE_TIMEOUT + 40, db[:-3] + '.helper.log')
        except Exception as e:
            rc, out = -1, repr(e)
        r = self.rtab
        if r['db'] != db or r['phase'] != 'localizing':
            return   # 对位期间被停掉了
        if rc == 0:
            r['phase'], r['msg'] = 'recording', '已和旧库对上，正在录新的一轮：去要更新的区域，从不同位置扫过变化的地方'
            self.rtab_save_session()
            self.node.event('INFO', f'局部重扫已和旧库对上，开始录新的一轮（{db}）')
            return
        why = (out.strip().splitlines() or ['没有输出'])[-1]
        self.node.event('WARN', f'局部重扫没和旧库对上，已停止（库没改动，备份也在）：{why}')
        await self.rtab_stop('没对上旧库', ok=False, msg=f'没和旧库对上：{why}')

    async def rtab_call(self, op, who, by=None):
        """pause / resume。by = 'freeze' 时是建图冻结自动暂停的，继续建图时自动恢复。"""
        r = self.rtab
        if not r['kind'] or r['phase'] not in ('recording', 'paused'):
            return False, 'RTAB 库没有在录'
        if (op == 'pause') == (r['phase'] == 'paused'):
            return True, '已经暂停' if op == 'pause' else '已经在录'
        rc, out = await self.rtab_runner.helper(['call', op], 30.0, r['db'][:-3] + '.helper.log')
        if rc != 0:
            return False, f"调用 rtabmap {op} 失败：{(out.strip().splitlines() or ['没有输出'])[-1]}"
        r['phase'] = 'paused' if op == 'pause' else 'recording'
        r['paused_by'] = by if op == 'pause' else None
        self.rtab_save_session()
        self.node.event('INFO', f"RTAB 库 {r['db']} {'已暂停' if op == 'pause' else '继续录'}（{who}）")
        return True, '已暂停：不再加新数据' if op == 'pause' else '继续录'

    async def rtab_stop(self, reason, ok=True, msg=None):
        """停止并存库，读出会话统计放进 rtab_last。返回 (ok, msg)。"""
        n = self.node
        async with self.rtab_lock:
            r = self.rtab
            if not r['kind']:
                if self.rtab_avail and self.rtab_runner.pids():
                    return False, '在跑的 rtabmap 不是网页启动的，网页不停它'
                return True, 'RTAB 库没有在录'
            if r.get('end'):            # 上次停止超时（stuck）时记下的结论，收尾时沿用（例如「没对上旧库」）
                ok, msg = r['end']
            r['end'] = (ok, msg)
            r['phase'], r['msg'] = 'stopping', '正在存库（可能要几十秒）'
            self.rtab_save_session()
            n.event('INFO', f"停止 RTAB 库 {r['db']}（{reason}），正在存库……")
            stopped = await self.rtab_runner.stop(STOP_TIMEOUT)
            if not stopped:
                r['phase'], r['msg'] = 'stuck', f'{STOP_TIMEOUT:.0f} s 还没存完，rtabmap 还在跑；它退出后自动收尾'
                self.rtab_save_session()
                n.event('ERROR', f"RTAB 库 {r['db']} 停止 {STOP_TIMEOUT:.0f} s 还没退出（还在存库？）")
                return False, r['msg']
            summary = await asyncio.get_running_loop().run_in_executor(
                None, db_summary, os.path.join(self.rtab_dir, r['db']))
            text = self.rtab_summary_text(r['kind'], summary)
            self.rtab_last = {'kind': r['kind'], 'db': r['db'], 'map': r['map'], 'wall': time.time(),
                              'ok': ok, 'msg': msg or text, 'summary': summary}
            self.rtab = self.rtab_idle()
            self.rtab_orphan = None
            self.rtab_save_session()
        n.event('INFO' if ok else 'WARN', f"RTAB 库 {self.rtab_last['db']} 已保存：{text}")
        return ok, msg or f'RTAB 库已保存：{text}'

    @staticmethod
    def rtab_summary_text(kind, s):
        if not s:
            return '读不出库里的会话（库可能没写完）'
        parts = '，'.join(f"第 {i + 1} 轮 {x['nodes']} 个节点" for i, x in enumerate(s['sessions'])) or '没有节点'
        if kind == 'continue' and len(s['sessions']) > 1:
            parts += f"；新一轮和旧库之间 {s['cross']} 条约束" + ('，可以做局部替换' if s['cross'] else '，没连上，不能做局部替换')
        return parts

    def rtab_pose(self):
        import rclpy.time   # 只在真用到时导入，单元测试不用装 ROS
        try:
            t = self.node.tf.lookup_transform('map', 'base_footprint', rclpy.time.Time()).transform
        except Exception:
            return None
        q = t.rotation
        yaw = math.atan2(2 * (q.w * q.z + q.x * q.y), 1 - 2 * (q.y * q.y + q.z * q.z))
        return (t.translation.x, t.translation.y, yaw)

    # ----- 档位联动 -----
    def rtab_on_mapping_start(self):
        """do_mode 启动建图档后调用：建图跑起来后自动开录（rtab_tick 里做）。"""
        self.rtab_pending = time.monotonic() if (self.rtab_avail and self.rtab_auto) else None

    async def rtab_before_mode_stop(self):
        """do_mode 停档位之前：rtabmap 靠这些档位的 /scan 和 TF，先停下存好库。"""
        self.rtab_pending = None
        if self.rtab['kind']:
            return await self.rtab_stop('切换运行档位')
        return True, ''

    async def rtab_on_freeze(self, reason):
        if self.rtab['kind'] == 'map' and self.rtab['phase'] == 'recording':
            await self.rtab_call('pause', f'建图冻结：{reason}', by='freeze')

    async def rtab_on_resume(self, who):
        if self.rtab['kind'] == 'map' and self.rtab['phase'] == 'paused' and self.rtab['paused_by'] == 'freeze':
            await self.rtab_call('resume', who)

    async def rtab_on_cont_start(self):
        """续建开始：slam_toolbox 换成基准图的坐标系，这次建图录的库和续建出来的图对不上了。"""
        self.rtab_pending = None
        if self.rtab['kind'] == 'map':
            await self.rtab_stop('开始续建（续建的坐标系和这次建图不同）')

    def rtab_tick(self):
        """safety 里每秒一次：自动开录、进程意外退出、依赖的档位没了还在跑。"""
        if not self.rtab_avail:
            return
        n, r, now = self.node, self.rtab, time.monotonic()
        if self.rtab_pending is not None:
            if now - self.rtab_pending > AUTO_WAIT or self.prof('mapping') == 'stop':
                self.rtab_pending = None
            elif (self.prof('mapping') == 'run' and not r['kind'] and not self.busy
                  and not self.cont and not self.cont_busy and not self.rtab_lock.locked()
                  and n.scan_ver > 0):
                self.rtab_pending = None
                self.background(self.rtab_start_map('建图自动', auto=True))
        if not r['kind'] or r['phase'] in ('starting', 'stopping') or self.rtab_lock.locked():
            return
        if r['phase'] == 'stuck':   # 停止超时：等它自己存完退出，再收尾
            if not self.rtab_runner.pids():
                self.background(self.rtab_stop('rtabmap 已退出'))
            return
        if not self.rtab_runner.pids():
            n.event('ERROR', f"RTAB 库 {r['db']} 的 rtabmap 意外退出了，日志在 data/rtabmap/{r['db'][:-3]}.log")
            self.rtab_last = {'kind': r['kind'], 'db': r['db'], 'map': r['map'], 'wall': time.time(), 'ok': False,
                              'msg': 'rtabmap 意外退出：' + (tail(os.path.join(self.rtab_dir, r['db'][:-3] + '.log'), 2)
                                                         or '看 data/rtabmap 里的日志'), 'summary': None}
            self.rtab = self.rtab_idle()
            self.rtab_save_session()
            return
        need = ('mapping',) if r['kind'] == 'map' else ('localization', 'navigation')
        if any(self.prof(m) != 'stop' for m in need) or self.busy:
            self.rtab_orphan = None
        elif self.rtab_orphan is None:
            self.rtab_orphan = now
        elif now - self.rtab_orphan > ORPHAN_GRACE:
            self.rtab_orphan = None
            self.background(self.rtab_stop('它依赖的档位已经停了'))
