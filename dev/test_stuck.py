"""卡住自动保存：StuckWatch 判定 + Server.stuck_tick 的触发条件。

    ~/.cache/go2w_web_venv/bin/python web/dev/test_stuck.py
"""
import asyncio
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(HERE, 'fake_ros'), os.path.join(HERE, '..', 'server')]
import go2w_web as W  # noqa: E402

fails = 0


def check(name, ok):
    global fails
    fails += not ok
    print(('PASS ' if ok else 'FAIL ') + name)


def watch():
    w = W.StuckWatch(seconds=8, move=0.08, turn=0.15)
    check('开始计时不报', w.update(0, (0, 0, 0), True) is None)
    check('7.9 s 不报', w.update(7.9, (0.05, 0, 0.1), True) is None)
    r = w.update(8.0, (0.05, 0, 0.1), True)
    check('8 s 报一次', r is not None and abs(r - 8) < 1e-9)
    check('同一次停住只报一次', w.update(20, (0, 0, 0), True) is None)
    check('动了重新计时', w.update(21, (0.2, 0, 0), True) is None and w.update(28.9, (0.2, 0, 0), True) is None)
    check('再停 8 s 又报', w.update(29.1, (0.2, 0, 0), True) is not None)
    w = W.StuckWatch(seconds=8, move=0.08, turn=0.15)
    w.update(0, (0, 0, 3.1), True)
    check('原地转（跨 ±π）算在动', w.update(5, (0, 0, -2.9), True) is None and w.update(12, (0, 0, -2.9), True) is None)
    check('原地小抖不算动', w.update(13.1, (0.01, 0, -2.95), True) is not None)
    w = W.StuckWatch(seconds=8, move=0.08, turn=0.15)
    w.update(0, (0, 0, 0), True)
    w.update(5, (0, 0, 0), False)
    check('任务结束后清零', w.update(6, (0, 0, 0), True) is None and w.update(13, (0, 0, 0), True) is None)
    check('没有 odom 不报', W.StuckWatch(seconds=0).update(1, None, True) is None)


async def tick():
    """stuck_tick 只负责判定和开 / 关「长时受阻」异常；存包逻辑在 test_incidents.py。"""
    opened, closed = [], []
    t0 = W.time.monotonic()
    node = types.SimpleNamespace(odom_pose=(1.0, 2.0, 0.0, t0), nav_active=lambda: True, is_armed=lambda: True,
                                 event=lambda lv, msg: None)
    s = types.SimpleNamespace(node=node, estop=False, inc_open={}, stuck=W.StuckWatch(seconds=0.2))
    s.incident = lambda typ, msg, ongoing=False, ago=0.0: opened.append((typ, ongoing, ago)) or f'id{len(opened)}'
    s.incident_close = lambda key: closed.append(s.inc_open.pop(key, None))
    tick = lambda: W.Server.stuck_tick(s)
    still = lambda: setattr(node, 'odom_pose', (*node.odom_pose[:3], W.time.monotonic()))   # 原地不动，只刷新时间
    tick(); await asyncio.sleep(0.25); still(); tick()
    check('卡住记一条持续中的异常', len(opened) == 1 and opened[0][0] == 'stuck' and opened[0][1] is True)
    check(f'开始时间往前推已停的秒数 {opened[0][2]:.2f}', opened[0][2] >= 0.2)
    still(); tick()
    check('还卡着不重复记、不结束', len(opened) == 1 and not closed)
    node.odom_pose = (1.5, 2.0, 0.0, W.time.monotonic()); tick()
    check('动起来就结束这条异常', closed == ['id1'] and 'stuck' not in s.inc_open)
    tick(); await asyncio.sleep(0.25); still(); tick()
    check('再卡住记新的一条', len(opened) == 2)
    node.nav_active = lambda: False; tick()
    check('导航结束也结束', closed == ['id1', 'id2'])
    # 没解锁 / 急停 / odom 过期都不算
    for name, setup in [('上锁不算', lambda: setattr(node, 'is_armed', lambda: False)),
                        ('急停不算', lambda: setattr(s, 'estop', True))]:
        node.nav_active, node.is_armed, s.estop, n0 = (lambda: True), (lambda: True), False, len(opened)
        s.stuck = W.StuckWatch(seconds=0.2)
        setup(); still(); tick(); await asyncio.sleep(0.25); still(); tick()
        check(name, len(opened) == n0)
    node.is_armed, s.estop, n0 = (lambda: True), False, len(opened)
    s.stuck = W.StuckWatch(seconds=0.2)
    node.odom_pose = (1.5, 2.0, 0.0, W.time.monotonic() - 10); tick(); await asyncio.sleep(0.25); tick()
    check('odom 过期不算', len(opened) == n0)


watch()
asyncio.run(tick())
print('全部通过' if not fails else f'{fails} 项失败')
sys.exit(1 if fails else 0)
