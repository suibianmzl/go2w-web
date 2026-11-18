"""每张地图的一条示教，以及可恢复的采集草稿。

活动几何只保存在地图YAML的共享包络中；RouteStore是导航可重新生成的运行缓存。
采集每个保留样本先写日志并fsync，再更新计数/界面；断线和重启只恢复到暂停态。
本模块不依赖ROS、不发送速度，采样有效性由桥接层提供，几何校验复用导航包。
"""

from copy import deepcopy
import fcntl
import importlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import threading
import time
from types import SimpleNamespace
import uuid

import numpy as np

NAME = re.compile(r'^[A-Za-z0-9_-]{1,48}$')
SESSION = re.compile(r'^[a-f0-9]{32}$')
ACTIVE = {'recording', 'paused', 'interrupted', 'processing'}
DEFAULT_FOOTPRINT = [[0.385, 0.26], [0.385, -0.26], [-0.385, -0.26], [-0.385, 0.26]]
JOIN_DISTANCE = 0.2


class TeachingError(ValueError):
    def __init__(self, message, code='TAUGHT_INVALID'):
        self.code = code
        super().__init__(message)


def _name(value):
    if not isinstance(value, str) or not NAME.fullmatch(value):
        raise TeachingError('地图名只能包含1–48个字母、数字、下划线或连字符')
    return value


def _atomic_json(path, value):
    """先写完整临时文件并同步，再发布元信息，不能让恢复程序看到半份检查点。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.checkpoint-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _pose(point):
    return [float(point['x']), float(point['y']), float(point.get('yaw', point.get('th', 0.0)))]


class TeachingRepository:
    """地图文件读写与实际导航算法之间的边界，不另建可供用户选择的路线列表。"""

    def __init__(self, maps_dir, routes_dir, *, footprint=None, parameters=None):
        self.maps_dir = Path(maps_dir)
        self.routes_dir = Path(routes_dir)
        self.maps_dir.mkdir(parents=True, exist_ok=True)
        self._core = None
        self._lock = threading.RLock()
        self._cache = {}
        self._footprint = deepcopy(footprint)
        self.parameters = dict(parameters or {})

    @property
    def core(self):
        if self._core is None:
            try:
                bundle = importlib.import_module('go2w_taught_route.map_bundle')
                model = importlib.import_module('go2w_taught_route.model')
                processing = importlib.import_module('go2w_taught_route.processing')
                recorder = importlib.import_module('go2w_taught_route.recorder')
                if not all(
                    callable(getattr(bundle, name, None))
                    for name in (
                        'inspect_bundle',
                        'read_bundle',
                        'load_map_asset',
                        'map_identity',
                        'write_bundle',
                        'prepare_bundle_route',
                    )
                ):
                    raise ImportError('incompatible taught route package')
                self._core = SimpleNamespace(
                    bundle=bundle, model=model, processing=processing, recorder=recorder
                )
            except ImportError as exc:
                raise TeachingError(
                    '当前后端缺少新版示教算法包，请使用配套导航版本后重启网页后端',
                    'TAUGHT_UNAVAILABLE',
                ) from exc
        return self._core

    @property
    def available(self):
        try:
            self.core
            return True
        except TeachingError:
            return False

    @property
    def store(self):
        return self.core.model.RouteStore(self.routes_dir)

    @property
    def footprint(self):
        if self._footprint is None:
            # 运行环境通过ament定位同一配置；源码调试由包文件位置定位，不能依赖当前目录。
            candidates = []
            try:
                from ament_index_python.packages import get_package_share_directory

                candidates.append(
                    Path(get_package_share_directory('go2w_taught_route'))
                    / 'config/taught_route.yaml'
                )
            except (ImportError, LookupError):
                pass
            candidates.append(
                Path(self.core.recorder.__file__).resolve().parents[1] / 'config/taught_route.yaml'
            )
            self._footprint = deepcopy(DEFAULT_FOOTPRINT)
            for path in candidates:
                if not path.is_file():
                    continue
                import yaml

                settings = yaml.safe_load(path.read_text())['taught_route']['ros__parameters']
                raw = settings.get('footprint')
                if raw:
                    self._footprint = [list(pair) for pair in zip(raw[::2], raw[1::2])]
                for key in ('resample_spacing', 'smooth_max_offset'):
                    if key in settings:
                        self.parameters.setdefault(key, settings[key])
                break
        return self._footprint

    def path(self, name):
        path = self.maps_dir / (_name(name) + '.yaml')
        if path.is_symlink() or path.resolve().parent != self.maps_dir.resolve():
            raise TeachingError('不能通过符号链接访问其他目录的地图')
        if not path.is_file():
            raise TeachingError('找不到这张地图', 'MAP_NOT_FOUND')
        return path

    def grid(self, name):
        asset = self.core.bundle.load_map_asset(self.path(name))
        origin = asset.get(
            'origin', [asset.get('ox', 0.0), asset.get('oy', 0.0), asset.get('oyaw', 0.0)]
        )
        resolution = float(np.float32(asset['res']))
        map_id = self.core.bundle.map_identity(asset['grid'], resolution, origin)
        grid = self.core.processing.GridMap(asset['grid'], resolution, origin, map_id=map_id)
        return grid, asset, self.core.bundle.map_source_token(self.path(name))

    def document(self, name):
        with self._lock:
            path = self.path(name)
            # 文件配置、图像和旧默认变化才重读大数组；采样广播不会每帧重新解析整张地图。
            default = self.routes_dir / '.map-defaults.json'
            cached = self._cache.get(name)
            if cached:
                image = cached[2]
                try:
                    key = (
                        path.stat().st_mtime_ns,
                        path.stat().st_size,
                        image.stat().st_mtime_ns,
                        image.stat().st_size,
                        default.stat().st_mtime_ns if default.exists() else 0,
                    )
                except OSError:
                    key = None  # YAML可能改用了新图像，不能因旧图像已移除而卡在过期缓存。
                if cached[0] == key:
                    return deepcopy(cached[1])
            import yaml

            meta = yaml.safe_load(path.read_text())
            image = (path.parent / meta['image']).resolve()
            key = (
                path.stat().st_mtime_ns,
                path.stat().st_size,
                image.stat().st_mtime_ns,
                image.stat().st_size,
                default.stat().st_mtime_ns if default.exists() else 0,
            )
            grid, _, token = self.grid(name)
            bundle = self.core.bundle.inspect_bundle(path)
            if bundle is None:
                # 仅共享核心决定是否兼容迁移旧原始默认；派生缓存不允许重新冒充活动源。
                self.core.bundle.prepare_bundle_route(self.store, path, grid.map_id, activate=True)
                bundle = self.core.bundle.read_bundle(path)
            reference = bundle['reference'] if bundle else {}
            valid = bool(bundle and bundle['map_id'] == grid.map_id and reference.get('executable'))
            invalid_message = '底图已变化，请重新编辑并校验示教'
            if valid:
                try:
                    # 展示可以读取旧几何供修复；只有导航的严格足迹复验也通过，才显示“参考可用”。
                    self.core.bundle.read_bundle(path, grid.map_id)
                except self.core.model.RouteError as exc:
                    valid = False
                    invalid_message = '示教未通过当前底图校验：' + str(exc)
            doc = {
                'map': name,
                'map_id': grid.map_id,
                'map_token': token,
                'revision': int(bundle['revision']) if bundle else 0,
                'points': deepcopy(reference.get('points', [])),
                'length': float(reference.get('length', 0.0)),
                'executable': valid,
                'report': deepcopy(reference.get('report', {})),
                'state': 'ready' if valid else 'invalid' if bundle else 'empty',
                'message': (
                    '已随地图保存，后续导航自动使用'
                    if valid
                    else invalid_message if bundle else '这张地图还没有示教轨迹'
                ),
                'source': bundle.get('source') if bundle else None,
                'capture': None,
                'check': {'state': 'idle', 'ok': False, 'message': '尚未准备采集', 'checks': []},
                'available': True,
            }
            self._cache[name] = (key, deepcopy(doc), image)
            return doc

    def save(
        self,
        name,
        points,
        *,
        expected_revision,
        map_token,
        source='web-edit',
        fixed=True,
        provenance=None,
    ):
        """输入是地图几何而非新的测量；先完整校验，成功才替换YAML中的唯一活动参考。"""
        if not isinstance(points, list) or not 2 <= len(points) <= 20000:
            raise TeachingError('示教轨迹需要2–20000个有效点')
        with self._lock:
            doc = self.document(name)
            if doc['revision'] != expected_revision:
                raise TeachingError('示教已被其他页面修改，请重新加载后再保存', 'VERSION_CONFLICT')
            if doc['map_token'] != map_token:
                raise TeachingError('底图已变化，请重新加载后再保存', 'MAP_MISMATCH')
            grid, _, token = self.grid(name)
            if token != map_token:
                raise TeachingError('校验期间底图已变化，请重新加载后再保存', 'MAP_MISMATCH')
            samples = []
            for i, p in enumerate(points):
                pose = _pose(p)
                if not all(math.isfinite(value) for value in pose):
                    raise TeachingError('轨迹坐标必须是有限数值')
                # 合成时间仅用于处理器排序，不冒充真实传感器时间；原始测量另存samples.jsonl。
                row = {
                    'sample_id': i,
                    'stamp': i * 0.1,
                    'pose_map': pose,
                    'map_id': grid.map_id,
                    'continuous_id': 0,
                    'fixed': bool(p.get('fixed', fixed)),
                    'raw_ids': deepcopy(p.get('raw_ids', [])),
                }
                if 'mark' in p:
                    row['mark'] = deepcopy(p['mark'])
                samples.append(row)
            footprint = self.footprint
            reference = self.core.processing.process_route(
                samples,
                grid,
                footprint,
                map_id=grid.map_id,
                parameters=self.parameters,
                preserve_fixed_points=bool(
                    provenance and provenance.get('mode') in ('append', 'replace_segment')
                ),
            )
            if not reference.get('executable'):
                raise TeachingError(
                    '轨迹校验未通过：'
                    + str(reference.get('report', {}).get('reason', '足迹或连续性不安全'))
                )
            reference['footprint'] = deepcopy(footprint)
            reference['provenance'] = provenance or {'kind': 'edited-reference-not-measurements'}
            self.core.bundle.write_bundle(
                self.path(name),
                reference,
                expected_revision=expected_revision,
                expected_token=map_token,
                source=source,
            )
            # 运行缓存可由导航重新生成；地图包络始终是活动参考的权威来源。
            self.core.bundle.prepare_bundle_route(
                self.store, self.path(name), grid.map_id, activate=True
            )
            self._cache.pop(name, None)
            return self.document(name)


class CaptureManager:
    """持久录制状态机。所有方法可在工作线程调用，单个管理器只允许一个未结束采集。"""

    def __init__(self, repository):
        self.repo = repository
        self.current = None
        self.recorder = None
        self.base = []
        self._lock = threading.RLock()
        self._stream = None
        self._owner_fd = None
        self._closed = False
        self._tail_offset = None
        self._external = False
        self.marker = self.repo.maps_dir / '.taught-current.json'
        self._recover()

    def _take_lock(self):
        if self._owner_fd is not None:
            return
        stream = (self.repo.maps_dir / '.taught-capture.lock').open('a')
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            stream.close()
            raise TeachingError('另一个网页后端正在采集，请先结束原采集')
        self._owner_fd = stream

    def _release(self):
        if self._owner_fd:
            fcntl.flock(self._owner_fd, fcntl.LOCK_UN)
            self._owner_fd.close()
            self._owner_fd = None

    def _directory(self):
        return (
            self.repo.maps_dir
            / (self.current['map'] + '.taught')
            / 'captures'
            / self.current['session_id']
        )

    def _checkpoint(self):
        if self.recorder is not None:
            self.current['sample_count'] = len(self.recorder._samples)
            self.current['events'] = self.recorder.events
            self.current['next_cid'] = self.recorder._cid
        self.current['updated_at'] = time.time()
        _atomic_json(self._directory() / 'session.json', self.current)
        if self.current['state'] in ACTIVE or self.current['state'] == 'failed':
            _atomic_json(
                self.marker, {'map': self.current['map'], 'session_id': self.current['session_id']}
            )

    def _open_stream(self):
        path = self._directory() / 'samples.jsonl'
        if self._tail_offset is not None:
            raw = path.read_bytes()
            # 崩溃中未完成的尾行不属于已确认样本，先保留完整原文件，再重建完整前缀。
            backup = path.with_name('samples-incomplete-' + uuid.uuid4().hex + '.jsonl')
            os.replace(path, backup)
            with path.open('wb') as output:
                output.write(raw[: self._tail_offset])
                output.flush()
                os.fsync(output.fileno())
            self._tail_offset = None
        self._stream = path.open('a', encoding='utf-8')

    def _recover(self):
        if not self.marker.exists():
            return
        try:
            marker = json.loads(self.marker.read_text())
            name = _name(marker['map'])
            sid = marker['session_id']
            if not isinstance(sid, str) or not SESSION.fullmatch(sid):
                raise TeachingError('采集恢复编号损坏')
            path = self.repo.maps_dir / (name + '.taught') / 'captures' / sid
            metadata = json.loads((path / 'session.json').read_text())
            self.current = metadata
            self._take_lock()
            raw = (path / 'samples.jsonl').read_bytes()
            samples = []
            offset = 0
            lines = raw.splitlines(keepends=True)
            for i, line in enumerate(lines):
                if not line.endswith(b'\n'):
                    if i != len(lines) - 1:
                        raise TeachingError('采集日志中间存在未完成记录')
                    self._tail_offset = offset
                    break
                sample = json.loads(line)
                if sample.get('recording_id') != sid or sample.get('sample_id') != len(samples):
                    raise TeachingError('采集日志编号不一致，保留原始文件但不自动续采')
                samples.append(sample)
                offset += len(line)
            if len(samples) < metadata.get('sample_count', 0):
                raise TeachingError('采集日志缺少已确认的样本')
            self.base = json.loads((path / 'base.json').read_text())
            recorder = self.repo.core.recorder.RouteRecorder(sid, metadata['map_id'])
            # RouteRecorder无恢复API：只重建已落盘证据；不复用崩溃前的_last时序假设。
            recorder._samples = deepcopy(samples)
            recorder._events = deepcopy(metadata.get('events', []))
            recorder._cid = max(
                metadata.get('next_cid', 0),
                max((p['continuous_id'] for p in samples), default=-1) + 1,
            )
            recorder._last = None
            recorder.state = 'PAUSED'
            self.recorder = recorder
            bundle = self.repo.core.bundle.read_bundle(self.repo.path(name))
            if bundle and bundle.get('source') == 'capture:' + sid:
                self.current.update(state='finished', message='上次保存已完成，已恢复保存结果')
                self._checkpoint()
                self.marker.unlink(missing_ok=True)
                self._release()
                return
            if metadata['state'] == 'failed':
                # 校验失败的原始草稿仍需可在编辑器打开；它不是可直接恢复采样的活动会话。
                self._checkpoint()
                self._release()
                return
            self.current.update(
                state='interrupted', message=f'已恢复{len(samples)}个落盘点，自检后回到断点附近继续'
            )
            self._checkpoint()
        except BlockingIOError:
            self._external = True
        except Exception as exc:
            if self._owner_fd is None:
                self._external = True
            if self.current is not None:
                self.current.update(
                    state='interrupted' if self._external else 'failed', message=str(exc)
                )
            if not self._external:
                self._release()

    def _require(self, session_id):
        if not self.current or session_id != self.current['session_id']:
            raise TeachingError('采集编号已变化，请刷新后再操作', 'SESSION_MISMATCH')
        if self._external:
            raise TeachingError('另一个后端仍持有采集，不能接管')

    def _join_safe(self, a, b, grid):
        if math.hypot(a[0] - b[0], a[1] - b[1]) > JOIN_DISTANCE + 1e-9:
            raise TeachingError('请先遥控回到断点附近（0.2m内），再继续采集')
        if not grid.swept_safe(a, b, self.repo.footprint):
            raise TeachingError('断点连接的机器人轮廓会碰撞，请重新调整位置')

    def start(
        self,
        name,
        *,
        mode,
        expected_revision,
        map_token,
        measurement,
        start_index=None,
        end_index=None,
    ):
        with self._lock:
            if self.current and self.current['state'] in ACTIVE:
                raise TeachingError('还有未结束采集，请继续、结束或放弃它')
            if mode not in ('replace', 'append', 'replace_segment'):
                raise TeachingError('未知采集方式')
            doc = self.repo.document(name)
            if doc['revision'] != expected_revision or doc['map_token'] != map_token:
                raise TeachingError('地图或示教已变化，请刷新后再开始', 'VERSION_CONFLICT')
            if measurement.get('map_id') != doc['map_id']:
                raise TeachingError('机器人当前地图与采集地图不一致', 'MAP_MISMATCH')
            base = deepcopy(doc['points'])
            if mode != 'replace' and not doc['executable']:
                raise TeachingError('追加或局部替换需要已有有效示教')
            grid, _, _ = self.repo.grid(name)
            if mode == 'append':
                self._join_safe(_pose(base[-1]), measurement['pose_map'], grid)
            if mode == 'replace_segment':
                if (
                    isinstance(start_index, bool)
                    or isinstance(end_index, bool)
                    or not isinstance(start_index, int)
                    or not isinstance(end_index, int)
                    or not 0 <= start_index < end_index < len(base)
                ):
                    raise TeachingError('请选择有效的起止点区间')
                self._join_safe(_pose(base[start_index]), measurement['pose_map'], grid)
            self._take_lock()
            self._closed = False
            self._external = False
            sid = uuid.uuid4().hex
            self.current = {
                'session_id': sid,
                'map': name,
                'map_id': doc['map_id'],
                'map_token': map_token,
                'base_revision': expected_revision,
                'mode': mode,
                'start_index': start_index,
                'end_index': end_index,
                'state': 'recording',
                'sample_count': 0,
                'message': '正在采集，使用遥控工具行走',
                'started_at': time.time(),
                'joins': [],
            }
            self.base = base
            self.recorder = self.repo.core.recorder.RouteRecorder(sid, doc['map_id'])
            self.recorder.start()
            directory = self._directory()
            directory.mkdir(parents=True)
            _atomic_json(directory / 'base.json', base)
            (directory / 'samples.jsonl').touch()
            self._open_stream()
            self._checkpoint()
            self.sample(measurement)
            return self.status()

    def sample(self, measurement):
        with self._lock:
            if not self.current or self.current['state'] != 'recording':
                return False
            if measurement.get('map_id') != self.current['map_id']:
                self.pause(self.current['session_id'], '地图已变化，采集已中断', interrupted=True)
                return False
            samples = self.recorder._samples
            # 轮询可能重复读到未达到降采样门槛的一帧。去重按“已检查帧”而非“已保存点”，
            # 否则第二次检查同一时刻会被录制器正确判为时间倒退，导致静止时误中断。
            last_checked = self.recorder._last
            if last_checked and last_checked['stamp'] == measurement['stamp']:
                return False
            before = len(self.recorder._events)
            saved = self.recorder.add_sample(
                measurement['stamp'],
                measurement['received_stamp'],
                measurement['pose_map'],
                measurement['pose_odom'],
                tf_stamp=measurement.get('tf_stamp', measurement['stamp']),
                measured_twist=measurement.get('twist', (0.0, 0.0, 0.0)),
                quality=measurement.get('quality', True),
                source='web-teleop',
            )
            if len(self.recorder._events) > before:
                self.pause(
                    self.current['session_id'],
                    '测量不连续：' + self.recorder._events[-1]['reason'],
                    interrupted=True,
                )
                return False
            if saved:
                self._stream.write(
                    json.dumps(
                        samples[-1], ensure_ascii=False, allow_nan=False, separators=(',', ':')
                    )
                    + '\n'
                )
                self._stream.flush()
                os.fsync(self._stream.fileno())
                self._checkpoint()
            return saved

    def pause(self, session_id, reason='已暂停，可回到断点附近继续', *, interrupted=False):
        with self._lock:
            self._require(session_id)
            if self.current['state'] == 'recording':
                self.recorder.pause()
            if self.current['state'] not in ('recording', 'paused', 'interrupted'):
                raise TeachingError('这次采集已经结束，不能暂停')
            self.current.update(state='interrupted' if interrupted else 'paused', message=reason)
            if self._stream:
                self._stream.flush()
                os.fsync(self._stream.fileno())
                self._stream.close()
                self._stream = None
            self._checkpoint()
            return self.status()

    def resume(self, session_id, measurement):
        with self._lock:
            self._require(session_id)
            if self.current['state'] not in ('paused', 'interrupted'):
                raise TeachingError('当前不是可继续的采集')
            doc = self.repo.document(self.current['map'])
            if (
                doc['revision'] != self.current['base_revision']
                or doc['map_token'] != self.current['map_token']
            ):
                raise TeachingError(
                    '采集期间地图或参考版本变化，请在编辑器修复草稿', 'VERSION_CONFLICT'
                )
            if measurement.get('map_id') != self.current['map_id']:
                raise TeachingError('请先加载原采集地图并完成自检')
            if self.recorder._samples:
                last = self.recorder._samples[-1]
                grid, _, _ = self.repo.grid(self.current['map'])
                self._join_safe(last['pose_map'], measurement['pose_map'], grid)
                self.current['joins'].append(
                    {
                        'from_cid': last['continuous_id'],
                        'to_cid': self.recorder._cid,
                        'from': last['pose_map'],
                        'to': measurement['pose_map'],
                        'source': 'operator-confirmed-short-connector',
                    }
                )
            self._open_stream()
            self.recorder.resume()
            self.current.update(state='recording', message='已从断点继续采集')
            self._checkpoint()
            self.sample(measurement)
            return self.status()

    def finish(self, session_id):
        with self._lock:
            self._require(session_id)
            if self.current['state'] not in ('recording', 'paused', 'interrupted'):
                raise TeachingError('这次采集不能重复结束')
            if self.current['state'] == 'recording':
                self.pause(session_id, '正在校验保存')
            if len(self.recorder._samples) < 2:
                self.current.update(state='paused', message='有效采样点不足，请继续采集后再结束')
                self._checkpoint()
                raise TeachingError(self.current['message'])
            self.current.update(
                state='processing', message='正在检查轨迹与机器人轮廓，原有效路线保持不变'
            )
            self._checkpoint()
            try:
                raw = self.recorder.samples
                # 原始断口不改写；只有经明确resume并检查的短连接才允许组成一条活动参考。
                allowed = {(j['from_cid'], j['to_cid']) for j in self.current['joins']}
                for a, b in zip(raw, raw[1:]):
                    if (
                        a['continuous_id'] != b['continuous_id']
                        and (a['continuous_id'], b['continuous_id']) not in allowed
                    ):
                        raise TeachingError('采集中存在未确认的断口，不能作为连续示教保存')
                new = [
                    {
                        'x': p['pose_map'][0],
                        'y': p['pose_map'][1],
                        'yaw': p['pose_map'][2],
                        'fixed': False,
                        'raw_ids': [p['sample_id']],
                    }
                    for p in raw
                ]
                fixed = [dict(p, fixed=True) for p in self.base]
                mode = self.current['mode']
                if mode == 'append':
                    points = fixed + new
                elif mode == 'replace_segment':
                    start, end = self.current['start_index'], self.current['end_index']
                    grid, _, _ = self.repo.grid(self.current['map'])
                    self._join_safe(_pose(new[-1]), _pose(fixed[end]), grid)
                    points = fixed[: start + 1] + new + fixed[end:]
                else:
                    points = new
                doc = self.repo.save(
                    self.current['map'],
                    points,
                    expected_revision=self.current['base_revision'],
                    map_token=self.current['map_token'],
                    source='capture:' + session_id,
                    fixed=False,
                    provenance={
                        'kind': 'captured-reference',
                        'capture_session': session_id,
                        'mode': mode,
                        'validated_connectors': deepcopy(self.current['joins']),
                    },
                )
                self.current.update(
                    state='finished', message='示教已校验并随地图保存，后续导航自动使用'
                )
                self._checkpoint()
                self.marker.unlink(missing_ok=True)
                self._release()
                return doc
            except Exception as exc:
                self.current.update(state='failed', message=str(exc))
                self._checkpoint()
                self._release()
                raise

    def abandon(self, session_id):
        with self._lock:
            self._require(session_id)
            if self.current['state'] not in ACTIVE and self.current['state'] != 'failed':
                raise TeachingError('这次采集已经结束，不能再次放弃')
            if self.current['state'] == 'recording':
                self.pause(session_id)
            self.current.update(
                state='abandoned', message='已放弃草稿，原始采集保留，已保存示教不变'
            )
            self._checkpoint()
            self.marker.unlink(missing_ok=True)
            self._release()
            return self.status()

    def status(self, map_name=None):
        with self._lock:
            if not self.current or (map_name and self.current['map'] != map_name):
                return None
            data = deepcopy(self.current)
            samples = self.recorder._samples if self.recorder else []
            data['sample_count'] = len(samples)
            data['points'] = [
                {
                    'x': s['pose_map'][0],
                    'y': s['pose_map'][1],
                    'yaw': s['pose_map'][2],
                    'continuous_id': s['continuous_id'],
                }
                for s in samples
            ]
            data['length'] = sum(
                math.hypot(b['x'] - a['x'], b['y'] - a['y'])
                for a, b in zip(data['points'], data['points'][1:])
                if a['continuous_id'] == b['continuous_id']
            )
            data['can_resume'] = data['state'] in ('paused', 'interrupted') and not self._external
            for key in ('events', 'joins', 'next_cid', 'map_token'):
                data.pop(key, None)
            return data

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if (
                self.current
                and self.current['state'] in ('recording', 'paused', 'interrupted')
                and not self._external
            ):
                self.pause(
                    self.current['session_id'], '网页后端已中断，原始采集已保存', interrupted=True
                )
            if self._stream:
                self._stream.close()
                self._stream = None
            self._release()
