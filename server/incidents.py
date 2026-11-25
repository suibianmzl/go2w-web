"""异常记录（11-18，用户：后端能识别的报错都记下来，并且每次都自动录包，给云端「异常归集」用）。

一条异常 = 一个 dict，存在 data/incidents.json（最新的在后面，最多 KEEP 条），推给网页（{t:'incidents'} / {t:'incident'}）。
字段（对外接口，见 docs/巡逻监控接口说明.md，只增不删）：
    id        唯一 ID
    ts        发生时间，Unix 毫秒
    end       结束时间，Unix 毫秒；瞬时异常 = ts；持续中 = None
    dur       持续秒数；持续中 = None
    ongoing   是否还在持续（卡住、急停、过热、运控离线这类有开始有结束的）
    robot     狗编号
    type      类型（TYPES 的键）    label  类型中文名    level  'warn' / 'error'
    msg       具体说明
    pose      {x, y, th, frame} 发生时的位置；拿不到 = None
    map       当时的地图名
    task      {routeId, name, point, pointName} 当时在跑的巡逻任务；没有 = None
    bag       自动录包：{state, name, msg}
              state = pending 等着存 / saving 存包中 / saved 已存（name = 包名）/ failed 失败 / skipped 没存（msg 说原因）
"""
import json
import os
import tempfile
import time
import uuid

# 类型 → (中文名, 级别)
TYPES = {
    'stuck':        ('长时受阻', 'warn'),    # 导航中长时间原地不动（持续）
    'nav_fail':     ('导航失败', 'error'),   # Nav2 放弃目标 / 巡逻任务某站失败
    'safety_stop':  ('安全停车', 'error'),   # 后端安全检查叫停：控制方断线、行走中掉出行走状态、定位丢失等
    'estop':        ('急停', 'error'),       # 网页急停（持续到解除）
    'motor_hot':    ('电机过热', 'error'),   # 电机 ≥ 阈值自动趴下（持续到降温）
    'ctrl_offline': ('运控离线', 'error'),   # 收不到运控状态（持续到恢复）
    'fall':         ('摔倒', 'error'),       # 运控不是网页让的、掉到 Passive 且机身倾角 ≥ 50°（11-19；11-23 起遥控器趴下不再算）
    'odom_wild':    ('里程计异常', 'error'), # 里程计速度 / 跳变离谱：FAST-LIO 发散（11-19）
    'link_lost':    ('网页断线', 'warn'),    # 自动导航中控制方网页断网 / 心跳丢失，任务继续不停车（v1.5.1，11-25）
    'lidar_lost':   ('雷达无数据', 'error'), # 档位在跑但 /scan 没数据（持续到恢复；不存包：包里也没点云）（11-19）
}
KEEP = 1000


def now_ms():
    return int(time.time() * 1000)


class IncidentLog:
    def __init__(self, path, on_change=None, keep=KEEP):
        self.path, self.on_change, self.keep = path, on_change or (lambda rec: None), keep
        self.items = []          # 旧 → 新
        self.error = None
        try:
            with open(path, encoding='utf-8') as f:
                raw = json.load(f)
            self.items = [r for r in raw if isinstance(r, dict) and isinstance(r.get('id'), str)][-keep:]
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError) as e:
            self.error = f'无法读取异常记录 {path}，从空记录开始：{e}'
        # 上次后端退出时还没结束的：结束时间不知道；没存完的包也不会再存了
        dirty = False
        for r in self.items:
            if r.get('ongoing'):
                r.update(ongoing=False, end=None, dur=None, msg=(r.get('msg') or '') + '（后端重启，结束时间未记录）')
                dirty = True
            if (r.get('bag') or {}).get('state') in ('pending', 'saving'):
                r['bag'] = {'state': 'failed', 'name': None, 'msg': '后端重启，包没有存成'}
                dirty = True
        if dirty:
            self.save()

    def get(self, rid):
        return next((r for r in reversed(self.items) if r['id'] == rid), None)

    def open(self, typ, msg, robot, ts=None, pose=None, map_name=None, task=None, ongoing=False):
        label, level = TYPES[typ]
        ts = now_ms() if ts is None else int(ts)
        rec = {'id': uuid.uuid4().hex[:16], 'ts': ts, 'end': None if ongoing else ts, 'dur': None if ongoing else 0.0,
               'ongoing': bool(ongoing), 'robot': robot, 'type': typ, 'label': label, 'level': level, 'msg': msg,
               'pose': pose, 'map': map_name, 'task': task, 'bag': {'state': 'pending', 'name': None, 'msg': ''}}
        self.items.append(rec)
        del self.items[:-self.keep]
        self.changed(rec)
        return rec

    def close(self, rid, end=None):
        rec = self.get(rid)
        if rec is None or not rec['ongoing']:
            return rec
        end = now_ms() if end is None else int(end)
        rec.update(ongoing=False, end=end, dur=round(max(0, end - rec['ts']) / 1000, 1))
        self.changed(rec)
        return rec

    def set_bag(self, rid, state, name=None, msg=''):
        rec = self.get(rid)
        if rec is not None:
            rec['bag'] = {'state': state, 'name': name, 'msg': msg}
            self.changed(rec)
        return rec

    def recent(self, limit=50, before=None):
        """最新的在前；before = 只要 ts 早于它的（翻页）。"""
        out = []
        for r in reversed(self.items):
            if before is not None and r['ts'] >= before:
                continue
            out.append(r)
            if len(out) >= limit:
                break
        return out

    def changed(self, rec):
        self.save()
        try:
            self.on_change(rec)
        except Exception:
            pass

    def save(self):
        try:
            d = os.path.dirname(self.path)
            os.makedirs(d, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=d, prefix='.incidents.')
            try:
                with os.fdopen(fd, 'w', encoding='utf-8') as f:
                    json.dump(self.items, f, ensure_ascii=False)
                os.replace(tmp, self.path)
            except BaseException:
                os.unlink(tmp)
                raise
            self.error = None
        except OSError as e:
            self.error = f'异常记录写不进 {self.path}：{e}'
