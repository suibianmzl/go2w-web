"""本地调试用的假 rtabmap（server/rtab.py 的 RtabRunner 接口）。

不跑真的 RTAB-Map：start 只记下「在跑」，stop 时往 data/rtabmap/<库>.db 里写一轮会话
（每秒 1 个节点；局部重扫对上旧库时再加 3 条跨会话约束），表结构只有 rtab.py 用到的那几列。

环境变量 / 开关文件（测试用）：
  GO2W_FAKE_RTAB=0             当成镜像里没有 rtabmap
  GO2W_FAKE_RTAB_LOCFAIL=1     局部重扫对不上旧库
  GO2W_FAKE_RTAB_LOC_S         对位耗时（默认 1.5 s）
  GO2W_FAKE_RTAB_STOP_S        存库耗时（默认 0.5 s；超过 GO2W_RTAB_STOP_TIMEOUT 就是「停不下来」）
  /tmp/go2w_fake_rtab_crash    文件存在时 rtabmap「意外退出」（测完删掉）
"""
import asyncio
import os
import sqlite3
import struct
import time

CRASH = os.environ.get('GO2W_FAKE_RTAB_CRASH', '/tmp/go2w_fake_rtab_crash')
PID = 424242


class FakeRtabRunner:
    def __init__(self, data_dir, node):
        self.dir = os.path.join(str(data_dir), 'rtabmap')
        self.node = node
        self.run = None        # {'db', 'localize', 't0', 'localized', 'exit_at'}

    def available(self):
        return os.environ.get('GO2W_FAKE_RTAB', '1') != '0'

    def pids(self):
        r = self.run
        if r is None:
            return []
        if os.path.exists(CRASH):
            self.run = None
            return []
        if r.get('exit_at') and time.monotonic() >= r['exit_at']:
            self.finish()
            return []
        return [PID]

    async def start(self, db, localize=False):
        os.makedirs(self.dir, exist_ok=True)
        path = os.path.join(self.dir, db)
        if localize and not os.path.isfile(path):
            raise RuntimeError(f'没有 {db}')
        with open(os.path.join(self.dir, db[:-3] + '.log'), 'a', encoding='utf-8') as f:
            f.write(f"假 rtabmap {'定位后续录' if localize else '新录'} {time.strftime('%H:%M:%S')}\n")
        await asyncio.sleep(0.2)
        self.run = {'db': db, 'localize': localize, 't0': time.monotonic(), 'localized': False, 'exit_at': None}
        return PID

    def finish(self):
        """「存库」：追加一轮会话。"""
        r, self.run = self.run, None
        if r is None or (r['localize'] and not r['localized']):
            return     # 定位模式没切建图：库不变
        path = os.path.join(self.dir, r['db'])
        db = sqlite3.connect(path)
        try:
            db.execute('CREATE TABLE IF NOT EXISTS Node (id INTEGER PRIMARY KEY, map_id INTEGER)')
            db.execute('CREATE TABLE IF NOT EXISTS Link (from_id INTEGER, to_id INTEGER, type INTEGER)')
            db.execute('CREATE TABLE IF NOT EXISTS Admin (opt_last_localization BLOB)')
            if db.execute('SELECT count(*) FROM Admin').fetchone()[0] == 0:
                db.execute('INSERT INTO Admin VALUES (?)', (struct.pack('<12f', 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0),))
            mid = db.execute('SELECT coalesce(max(map_id), -1) FROM Node').fetchone()[0] + 1
            first = db.execute('SELECT coalesce(max(id), 0) FROM Node').fetchone()[0] + 1
            k = max(2, int(time.monotonic() - r['t0']))
            ids = list(range(first, first + k))
            db.executemany('INSERT INTO Node VALUES (?, ?)', [(i, mid) for i in ids])
            db.executemany('INSERT INTO Link VALUES (?, ?, 1)', [(a, a + 1) for a in ids[:-1]] +
                           [(a + 1, a) for a in ids[:-1]])
            if r['localized'] and first > 1:
                db.executemany('INSERT INTO Link VALUES (?, ?, 2)', [(ids[j], 1 + j) for j in range(3)] +
                               [(1 + j, ids[j]) for j in range(3)])
            db.commit()
        finally:
            db.close()

    async def stop(self, timeout=120.0):
        r = self.run
        if r is None:
            return True
        delay = float(os.environ.get('GO2W_FAKE_RTAB_STOP_S', '0.5'))
        r['exit_at'] = time.monotonic() + delay
        await asyncio.sleep(min(delay, timeout) + 0.05)
        return not self.pids()

    async def helper(self, args, timeout, log_name):
        r = self.run
        if args[0] == 'localize':
            await asyncio.sleep(float(os.environ.get('GO2W_FAKE_RTAB_LOC_S', '1.5')))
            if os.environ.get('GO2W_FAKE_RTAB_LOCFAIL') == '1' and not (len(args) > 5 and args[5] == 'force'):
                return 1, '失败： 60 s 内没和旧图对上。确认起点位姿，或把狗挪动 0.5 m / 转一下再试'
            if r is None:
                return 1, '失败： rtabmap 没在跑'
            r['localized'] = True
            return 0, '已切到建图模式，开始录新会话'
        if args[0] == 'call':
            return (0, f'已调用 /rtab/{args[1]}') if r else (1, '失败： rtabmap 没在跑')
        return 1, f'假 helper 不支持 {args[0]}'
