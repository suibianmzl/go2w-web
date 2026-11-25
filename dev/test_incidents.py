"""异常记录（incidents.py）+ 自动存包调度（Server.incident / incident_bag / incident_save）。

    ~/.cache/go2w_web_venv/bin/python web/dev/test_incidents.py
"""
import asyncio
import json
import os
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(HERE, 'fake_ros'), os.path.join(HERE, '..', 'server')]
import go2w_web as W  # noqa: E402
import incidents as INC  # noqa: E402

fails = 0


def check(name, ok):
    global fails
    fails += not ok
    print(('PASS ' if ok else 'FAIL ') + name)


def log_basics(tmp):
    path = os.path.join(tmp, 'incidents.json')
    pushed = []
    L = INC.IncidentLog(path, on_change=pushed.append)
    a = L.open('nav_fail', '第 2 点失败', 'go2w-01', ts=1000, map_name='lab', task={'routeId': 'r1', 'name': '巡逻', 'point': 2, 'pointName': 'B'})
    check('瞬时异常：end = ts、dur = 0、不持续', a['end'] == 1000 and a['dur'] == 0.0 and a['ongoing'] is False)
    check('带中文名和级别', a['label'] == '导航失败' and a['level'] == 'error')
    b = L.open('stuck', '卡住', 'go2w-01', ts=2000, ongoing=True)
    check('持续异常：end / dur 为空', b['end'] is None and b['dur'] is None and b['ongoing'])
    L.close(b['id'], end=14600)
    check('结束后算持续时长', b['dur'] == 12.6 and b['end'] == 14600 and not b['ongoing'])
    L.close(b['id'], end=99999)
    check('重复结束不改', b['end'] == 14600)
    L.set_bag(a['id'], 'saved', name='go2w-01_1118_142536')
    check('每次变化都推送', len(pushed) == 4 and pushed[-1]['bag']['name'] == 'go2w-01_1118_142536')
    check('recent 新的在前', [r['id'] for r in L.recent()] == [b['id'], a['id']])
    check('recent before 翻页', [r['id'] for r in L.recent(before=2000)] == [a['id']])
    check('recent limit', len(L.recent(limit=1)) == 1)
    c = L.open('estop', '急停', 'go2w-01', ongoing=True)
    L.set_bag(c['id'], 'saving')
    # 重启：持续中的没有结束时间，没存完的包算失败
    L2 = INC.IncidentLog(path)
    r = L2.get(c['id'])
    check('重启后读回全部', len(L2.items) == 3 and L2.get(a['id'])['bag']['state'] == 'saved')
    check('重启时持续中的标成已结束、时长未知', r['ongoing'] is False and r['dur'] is None and '后端重启' in r['msg'])
    check('重启时没存完的包标成失败', r['bag']['state'] == 'failed')
    L3 = INC.IncidentLog(os.path.join(tmp, 'cap.json'), keep=3)
    for i in range(5):
        L3.open('nav_fail', str(i), 'x')
    check('只保留最近 keep 条', [r['msg'] for r in L3.items] == ['2', '3', '4'])
    with open(os.path.join(tmp, 'bad.json'), 'w') as f:
        f.write('{坏')
    check('文件坏了从空开始并报错', INC.IncidentLog(os.path.join(tmp, 'bad.json')).error is not None)


def fake_server(tmp):
    events, saves = [], []
    node = types.SimpleNamespace(event=lambda lv, m: events.append((lv, m)), fixed='map',
                                 lookup=lambda frame: (1.0, 2.0, 0.5), get_logger=lambda: types.SimpleNamespace(
                                     error=print, warn=print))
    s = types.SimpleNamespace(node=node, procs_status={'rollback': True}, rollback_saving=False, pending=set(),
                              inc_save=None, inc_bag_t={}, inc_open={}, rollback_last=None, route=None, clients={})
    s.incidents = INC.IncidentLog(os.path.join(tmp, 'srv.json'))
    for name in ('incident', 'incident_bag', 'incident_save', '_incident_save', 'incident_close', 'incident_task', 'background'):
        setattr(s, name, getattr(W.Server, name).__get__(s))
    s.shown_map = lambda: 'lab'
    s.fail_save = False
    s.save_types = []

    async def rollback_save(who, seconds, *, incident_types=()):
        saves.append((who, round(seconds, 1)))
        s.save_types.append(incident_types)
        if s.fail_save:
            return False, '回溯保存超时'
        name = f'go2w-01_bag{len(saves)}'
        os.makedirs(os.path.join(W.DATA, 'bags', name))
        s.rollback_last = name
        return True, f'已保存回溯 {name}'
    s.rollback_save = rollback_save
    return s, events, saves


async def scheduling(tmp):
    W.DATA = tmp
    W.STUCK_BEFORE, W.STUCK_AFTER, W.STUCK_COOLDOWN, W.INC_MERGE = 15.0, 0.4, 1.5, 0.1
    s, events, saves = fake_server(tmp)
    L = s.incidents
    s.route = {'status': 'active', 'index': 1, 'record': {'id': 'r1', 'name': '巡逻', 'points': [{'name': 'A'}, {'name': 'B'}]}}
    a = s.incident('stuck', '卡住', ongoing=True, ago=8)
    r = L.get(a)
    check('记下位置、地图、任务', r['pose'] == {'x': 1.0, 'y': 2.0, 'th': 0.5, 'frame': 'map'} and r['map'] == 'lab'
          and r['task'] == {'routeId': 'r1', 'name': '巡逻', 'point': 2, 'pointName': 'B'})
    check('开始时间往前推 ago 秒', abs(INC.now_ms() - r['ts'] - 8000) < 500)
    check('先是等着存', r['bag']['state'] == 'pending' and any('秒后自动保存' in m for _, m in events))
    await asyncio.sleep(0.1)
    b = s.incident('nav_fail', '第 2 点失败')
    check('计划存包前的异常并进同一个包', L.get(b)['bag']['msg'] == '和前面的异常存进同一个包')
    await asyncio.sleep(0.5)
    check(f'只存了一个包，长度 = 前 15 + 后 0.4 {saves}', len(saves) == 1 and saves[0][1] == 15.4)
    check('包名写回两条异常', L.get(a)['bag'] == L.get(b)['bag'] == {'state': 'saved', 'name': 'go2w-01_bag1', 'msg': ''})
    check('合并异常的两种原因传入命名', set(s.save_types[0]) == {'stuck', 'nav_fail'})
    check('存包说明里列出两种异常', '长时受阻' in saves[0][0] and '导航失败' in saves[0][0])
    with open(os.path.join(tmp, 'bags', 'go2w-01_bag1', 'incidents.json'), encoding='utf-8') as f:
        check('包目录里有异常说明', [x['id'] for x in json.load(f)] == [a, b])
    c = s.incident('stuck', '又卡住')
    check('冷却期内同类异常不存包', L.get(c)['bag']['state'] == 'skipped' and '同类' in L.get(c)['bag']['msg'])
    d = s.incident('estop', '急停')
    check('冷却期内别的类型照样存', L.get(d)['bag']['state'] == 'pending')
    await asyncio.sleep(0.1)
    s.fail_save = True
    await asyncio.sleep(0.5)
    check('存包失败如实记录', L.get(d)['bag']['state'] == 'failed' and L.get(d)['bag']['msg'] == '回溯保存超时')
    s.procs_status = {}
    e = s.incident('motor_hot', '过热')
    check('回溯没在跑：不存并说明', L.get(e)['bag']['state'] == 'skipped' and '回溯录制没在运行' in L.get(e)['bag']['msg'])
    s.inc_open['estop'] = d
    s.incident_close('estop')
    check('incident_close 结束持续中的异常', 'estop' not in s.inc_open)
    s.incident_close('estop')   # 没有开着的：什么也不做
    # 记录本身出错不能抛（停车路径上调用）
    s.incidents = None
    check('出错时返回 None 不抛', s.incident('estop', 'x') is None)


with tempfile.TemporaryDirectory() as tmp:
    log_basics(tmp)
    asyncio.run(scheduling(tmp))
print('全部通过' if not fails else f'{fails} 项失败')
sys.exit(1 if fails else 0)
