"""网页示教流程的ROS边界。

采集流程：选图准备→定位自检→用户明确开始→同一测量时刻的map位姿落盘→校验发布。
使用流程：文件编辑只更新地图YAML；普通导航接受目标时由导航包读取该文件固定版本。
这里不实现路径平滑或碰撞算法，也不在准备、自检或恢复时自动站立、解锁、发速度。
"""

import asyncio
from copy import deepcopy
import json
import math
import os
from pathlib import Path
import struct
import threading
import time
import zlib

from aiohttp import web
import numpy as np

from teaching import ACTIVE, CaptureManager, TeachingError, TeachingRepository, _name


def _yaw(q):
    return math.atan2(2 * (q.w * q.z + q.x * q.y), 1 - 2 * (q.y * q.y + q.z * q.z))


def _stamp(stamp):
    return stamp.sec + stamp.nanosec * 1e-9


class TaughtMeasurements:
    """只给示教使用的测量快照，与展示用的latest TF、平滑匹配率分开。

    定位修正会改变map→odom，所以不能拿现在的TF给上一个odom或scan补坐标。
    精确时刻的变换缺失时返回无效，由采样任务中断草稿，不退回odom坐标伪装成功。
    """

    def init_taught_measurements(self):
        self._taught_lock = threading.Lock()
        self._taught_odom = None
        self._taught_map_id = None
        self._taught_scan = None
        self._taught_map = None
        self._taught_pose_stamp = 0.0

    def observe_taught_map(self, message, near=None):
        try:
            from go2w_taught_route.model import map_fingerprint

            info = message.info
            origin = info.origin
            grid = np.asarray(message.data, dtype=np.int8).reshape(info.height, info.width).copy()
            pose = [origin.position.x, origin.position.y, _yaw(origin.orientation)]
            # 收到的分辨率/yaw已经经历ROS序列化与四元数还原，不能再做离线文件的往返规范化。
            identity = map_fingerprint(
                grid, info.resolution, pose, message.header.frame_id or 'map'
            )
            if near is None:
                from scipy.ndimage import binary_dilation

                near = binary_dilation(grid >= 65, iterations=max(1, round(0.10 / info.resolution)))
            with self._taught_lock:
                if self._taught_map_id != identity:
                    self._taught_scan = None
                self._taught_map_id = identity
                self._taught_map = (near, info.resolution, pose, identity)
        except (ImportError, ValueError):
            with self._taught_lock:
                self._taught_map_id = None
                self._taught_map = None

    def observe_taught_odom(self, message):
        with self._taught_lock:
            self._taught_odom = (message, time.monotonic())

    def observe_taught_scan(self, message):
        from rclpy.time import Time

        with self._taught_lock:
            asset = self._taught_map
        if asset is None:
            return
        occupied, res, origin, identity = asset
        try:
            tr = self.tf.lookup_transform(
                'map', message.header.frame_id, Time.from_msg(message.header.stamp)
            ).transform
            angle = _yaw(tr.rotation)
            ranges = np.asarray(message.ranges, dtype=np.float64)
            angles = message.angle_min + np.arange(len(ranges)) * message.angle_increment
            good = (
                np.isfinite(ranges) & (ranges >= message.range_min) & (ranges <= message.range_max)
            )
            ranges, angles = ranges[good], angles[good] + angle
            x = tr.translation.x + ranges * np.cos(angles) - origin[0]
            y = tr.translation.y + ranges * np.sin(angles) - origin[1]
            c, s = math.cos(origin[2]), math.sin(origin[2])
            gx = np.floor((c * x + s * y) / res).astype(int)
            gy = np.floor((-s * x + c * y) / res).astype(int)
            h, w = occupied.shape
            inside = (gx >= 0) & (gy >= 0) & (gx < w) & (gy < h)
            if inside.sum() < 20:
                return
            # 复用控制台0.10m、四邻域膨胀的障碍邻域，但TF必须属于这帧scan的测量时刻。
            match = 100.0 * occupied[gy[inside], gx[inside]].mean()
            result = {
                'match': float(match),
                'stamp': _stamp(message.header.stamp),
                'received': time.monotonic(),
                'map_id': identity,
            }
            with self._taught_lock:
                if self._taught_map_id == identity:
                    self._taught_scan = result
        except Exception:
            # 保留上一帧的原时刻，过期检查会否决；不把失败帧刷新成“新鲜”。
            return

    def taught_measurement(self):
        from rclpy.time import Time

        with self._taught_lock:
            odom = self._taught_odom
            identity = self._taught_map_id
        if odom is None or identity is None:
            raise TeachingError('还没有同一地图的里程计测量')
        raw, received = odom
        now = time.monotonic()
        stamp = _stamp(raw.header.stamp)
        ros_now = self.get_clock().now().nanoseconds * 1e-9
        if now - received > 0.2 or not -0.02 <= ros_now - stamp <= 0.2:
            raise TeachingError('里程计测量或接收已过期（需要0.2秒内）')
        if stamp < self._taught_pose_stamp:
            raise TeachingError('正在等待定位修正后的新里程计测量')
        if self.healthy is not True or now - self.healthy_t > 0.75:
            raise TeachingError('里程计健康状态未知、过期或不健康')
        if raw.header.frame_id != 'odom' or raw.child_frame_id != 'base_footprint':
            raise TeachingError('里程计坐标系必须是odom→base_footprint')
        try:
            transform = self.tf.lookup_transform(
                'map', 'base_footprint', Time.from_msg(raw.header.stamp)
            ).transform
        except Exception as exc:
            raise TeachingError('该测量时刻的map→base_footprint变换不可用') from exc
        p = raw.pose.pose
        t = raw.twist.twist
        return {
            'stamp': stamp,
            'received_stamp': ros_now,
            'pose_map': [
                transform.translation.x,
                transform.translation.y,
                _yaw(transform.rotation),
            ],
            'pose_odom': [p.position.x, p.position.y, _yaw(p.orientation)],
            'tf_stamp': stamp,
            'twist': [t.linear.x, t.linear.y, t.angular.z],
            'map_id': identity,
            'quality': True,
        }


class TaughtWeb:
    """Server的示教操作。所有算法、读盘和同步日志在工作线程，心跳任务持续独立运行。"""

    def init_taught(self, data):
        self.taught_repo = TeachingRepository(Path(data) / 'maps', Path(data) / 'routes')
        self.captures = CaptureManager(self.taught_repo)
        self._taught_capture = self.captures.status()
        self.taught_owner = None
        self.taught_checks = {}
        self._taught_synced = None
        self._taught_epoch = time.monotonic()
        self._taught_stood_seq = self.stood_seq
        self._taught_job = None
        self._taught_mutation = asyncio.Lock()
        self._taught_finish_busy = False
        self._taught_missing_since = None

    def taught_open(self):
        return bool(self._taught_capture and self._taught_capture['state'] in ACTIVE)

    def taught_recording(self):
        return bool(self._taught_capture and self._taught_capture['state'] == 'recording')

    def fresh_taught_scan(self, map_id):
        """接收时间只能说明网络刚送达；还需测量本身新鲜，且发生在本次定位修正之后。"""
        n = self.node
        scan = n._taught_scan
        if not scan or scan['map_id'] != map_id:
            return False
        ros_now = n.get_clock().now().nanoseconds * 1e-9
        return bool(
            time.monotonic() - scan['received'] <= 0.5
            and -0.02 <= ros_now - scan['stamp'] <= 0.5
            and scan['stamp'] >= n._taught_pose_stamp
        )

    def reserve_taught_owner(self, ws, name):
        if ws.closed or ws not in self.clients:
            raise TeachingError('网页已断开，未开始采集')
        if self.taught_open() and self.taught_owner is not None and self.taught_owner is not ws:
            raise TeachingError('另一个页面正在操作这次采集')
        self.owner = self.taught_owner = ws
        self.clients[ws]['taught_map'] = name

    async def confirm_taught_owner(self, ws):
        """落盘可能慢于断线事件；异步返回后再次确认，不能把已消失的页面重新设成所有者。"""
        client = self.clients.get(ws)
        live = bool(
            client
            and not ws.closed
            and self.taught_owner is ws
            and self.owner is ws
            and time.monotonic() - client['last_ping'] <= self.heartbeat
        )
        if live:
            return
        await self.taught_interrupt('开始或恢复期间网页断开，草稿已保留')
        if self.taught_owner is ws:
            self.taught_owner = None
        if self.owner is ws:
            self.owner = None
        raise TeachingError('网页已断开，采集已中断，重连后需明确恢复')

    def taught_invalidate(self, reason):
        self._taught_synced = None
        self._taught_epoch = time.monotonic()
        self._taught_stood_seq = self.stood_seq
        for check in self.taught_checks.values():
            # 准备本就会主动换图/启栈，不能把这次预期切换提前广播成“准备已失败”。
            if self._taught_job and check.get('state') in ('preparing', 'loading', 'localizing'):
                check.update(ok=False)
            else:
                check.update(state='failed', ok=False, message=reason)

    async def taught_document(self, name):
        try:
            doc = await asyncio.to_thread(self.taught_repo.document, name)
        except Exception as exc:
            doc = {
                'map': name,
                'map_id': None,
                'map_token': None,
                'revision': 0,
                'points': [],
                'length': 0.0,
                'executable': False,
                'report': {},
                'state': 'invalid',
                'message': str(exc),
                'available': self.taught_repo.available,
            }
        doc['check'] = deepcopy(
            self.taught_checks.get(
                name, {'state': 'idle', 'ok': False, 'message': '尚未准备采集', 'checks': []}
            )
        )
        capture = self._taught_capture
        doc['capture'] = deepcopy(capture) if capture and capture['map'] == name else None
        if self.simulated:
            doc['simulated'] = True
        return doc

    async def taught_push(self):
        names = {c.get('taught_map') or self.shown_map() for c in self.clients.values()}
        for name in names - {None}:
            doc = await self.taught_document(name)
            data = json.dumps({'t': 'taught', **doc}, ensure_ascii=False, allow_nan=False)
            for ws, c in list(self.clients.items()):
                if (c.get('taught_map') or self.shown_map()) == name:
                    self.tx_put(ws, data, 'taught')

    async def sync_taught_map(self, name, *, required=False):
        """LoadMap和示教节点必须指向同一个文件，避免换图后导航读取另一张地图的参考。"""
        from rcl_interfaces.msg import Parameter, ParameterType, ParameterValue
        from rcl_interfaces.srv import SetParameters

        n = self.node
        if not n.taught_param_cli.service_is_ready():
            self._taught_synced = None
            if required:
                raise TeachingError('示教导航节点未运行，请重新准备地图')
            return False
        request = SetParameters.Request()
        parameter = Parameter()
        parameter.name = 'map_yaml'
        parameter.value = ParameterValue(
            type=ParameterType.PARAMETER_STRING, string_value=str(self.taught_repo.path(name))
        )
        request.parameters = [parameter]
        response = await self.aw(n.taught_param_cli.call_async(request), 3.0)
        if not response.results or not all(r.successful for r in response.results):
            raise TeachingError(
                '示教节点拒绝地图文件：' + str([r.reason for r in response.results])
            )
        self._taught_synced = name
        return True

    async def check_ordinary_taught_binding(self, name):
        """普通导航沿用当前地图文件。旧栈若尚无代理，不允许默默忽略已经保存的示教。"""
        if not name or not self.taught_repo.available:
            return True, ''
        if 'taught_route' in self.node.get_node_names():
            if self._taught_synced != name:
                await self.sync_taught_map(name, required=True)
            return True, ''
        doc = await asyncio.to_thread(self.taught_repo.document, name)
        if doc['state'] != 'empty':
            return False, '该地图已保存示教，请通过出发流程重新启动导航以启用示教服务'
        return True, ''

    async def taught_self_check(self, name):
        n = self.node
        doc = await asyncio.to_thread(self.taught_repo.document, name)
        now = time.monotonic()
        nodes = set(n.get_node_names())
        checks = []
        measurement = None

        def row(key, label, ok, message):
            checks.append({'key': key, 'label': label, 'ok': bool(ok), 'message': message})

        row(
            'stack',
            '导航与定位服务',
            self.prof('navigation') == 'run'
            and {'amcl', 'bt_navigator', 'go2w_cmd_bridge', 'taught_route'} <= nodes,
            '需运行定位、导航、速度桥和示教节点',
        )
        row(
            'map',
            '底图一致',
            n.map_file is None
            and n._taught_map_id == doc['map_id']
            and self._taught_synced == name,
            '实际/map、所选地图文件和示教节点必须一致',
        )
        try:
            measurement = n.taught_measurement()
            why = '测量与TF按同一时刻，均为新鲜数据'
        except Exception as exc:
            why = str(exc)
        row('measurement', '测量与坐标变换', measurement is not None, why)
        scan = n._taught_scan
        amcl = n.amcl
        scan_ok = bool(self.fresh_taught_scan(doc['map_id']) and scan['received'] > self.pose_set_t)
        quality_ok = bool(
            amcl
            and amcl[2] > self._taught_epoch
            and math.isfinite(amcl[0])
            and amcl[0] <= 0.3
            and scan_ok
            and scan['match'] >= 60.0
        )
        quality = f'位置σ {amcl[0]:.3f}m' if amcl else '位置σ未知'
        if amcl and amcl[2] <= self._taught_epoch:
            quality += '（尚未收到本次换图/定位后的新协方差）'
        quality += f"，测量时刻匹配率 {scan['match']:.1f}%" if scan_ok else '，新鲜匹配率未知'
        row('localization_quality', '定位质量', quality_ok, quality + '；需要≤0.3m、≥60%')
        confirmed = (
            self.pose_set_t > self._taught_epoch and self.stood_seq == self._taught_stood_seq
        )
        row(
            'localized',
            '定位来源已确认',
            confirmed,
            '换图、重启或站起后需重新自动定位或手动修正初始位置',
        )
        still = bool(
            n.odom is not None and math.hypot(n.odom[0], n.odom[1]) < 0.03 and abs(n.odom[2]) < 0.05
        )
        row('still', '机器人静止', still, '开始和继续采集前保持静止')
        row(
            'exclusive',
            '任务状态',
            not (
                self.estop
                or self.busy
                or self.map_loading
                or n.nav_active()
                or self.route_running()
                or self.prof('patrol') != 'stop'
            ),
            '急停解除，且没有其他导航任务或地图切换',
        )
        ok = all(c['ok'] for c in checks)
        check = {
            'state': 'ready' if ok else 'failed',
            'ok': ok,
            'checks': checks,
            'message': (
                '自检通过，可明确开始采集并进入遥控'
                if ok
                else '；'.join(c['label'] + '未通过' for c in checks if not c['ok'])
            ),
        }
        self.taught_checks[name] = check
        return check, measurement

    async def taught_prepare_job(self, name):
        n = self.node
        try:
            doc = await asyncio.to_thread(self.taught_repo.document, name)
            if self.estop or n.nav_active() or self.route_running() or self.teleop is not None:
                raise TeachingError('先停止当前运动或导航任务，再准备采集')
            # 运行中已启用同功能时只换图；旧栈则重启导航组，使普通目标代理真正生效。
            self.taught_checks[name].update(
                state='loading', message='正在启动导航服务并加载选定地图'
            )
            if self.prof('navigation') != 'run' or 'taught_route' not in n.get_node_names():
                result = self.set_mode('navigation', name, force=True, taught_route=True)
                if not result[0]:
                    raise TeachingError(result[1])
                deadline = time.monotonic() + 95.0
                while self.busy or self.prof('navigation') == 'starting':
                    if time.monotonic() > deadline:
                        raise TeachingError('导航服务启动超时，请查看运行日志')
                    await asyncio.sleep(0.2)
                if self.prof('navigation') != 'run':
                    raise TeachingError('导航服务未运行，请查看运行日志')
            ok, text = await self.load_map(name)
            if not ok:
                raise TeachingError(text)
            await self.sync_taught_map(name, required=True)
            deadline = time.monotonic() + 10.0
            while n._taught_map_id != doc['map_id']:
                if time.monotonic() > deadline:
                    raise TeachingError('选定地图尚未从/map到达，不能开始采集')
                await asyncio.sleep(0.1)
            self._taught_stood_seq = self.stood_seq
            self.taught_checks[name].update(
                state='localizing', message='保持静止，正在定位；失败后可手动修正位置并重做自检'
            )
            if self.autoloc_busy:
                while self.autoloc_busy:
                    await asyncio.sleep(0.2)
            if self.pose_set_t <= self._taught_epoch:
                await self.auto_localize('示教采集准备', on_start=True)
            # 新initialpose之后至少等一帧新的scan，不将定位前缓存立即当作通过。
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                scan = n._taught_scan
                amcl = n.amcl
                if (
                    scan
                    and scan['received'] > self.pose_set_t
                    and amcl
                    and amcl[2] > self._taught_epoch
                ):
                    break
                await asyncio.sleep(0.1)
            await self.taught_self_check(name)
        except Exception as exc:
            self.taught_checks[name] = {
                'state': 'failed',
                'ok': False,
                'message': str(exc),
                'checks': [],
            }
            n.event('WARN', '示教准备未完成：' + str(exc))
        finally:
            self._taught_job = None
            await self.taught_push()

    async def taught_owner_lost(self, owner, reason):
        if owner is not self.taught_owner:
            return
        self.taught_owner = None
        await self.taught_interrupt(reason)

    async def taught_interrupt(self, reason):
        if self.taught_recording():
            try:
                self._taught_capture = await asyncio.to_thread(
                    self.captures.pause,
                    self._taught_capture['session_id'],
                    reason,
                    interrupted=True,
                )
            except Exception as exc:
                # 存储故障必须报告，但不能跳过取消导航/底盘上锁，也不能让采样循环继续运行。
                self._taught_capture.update(
                    state='interrupted',
                    can_resume=False,
                    message='采样已停止，检查点写入失败：' + str(exc),
                )
                self.node.event('ERROR', self._taught_capture['message'])

    async def taught_finish_job(self, sid):
        try:
            async with self._taught_mutation:
                await asyncio.to_thread(self.captures.finish, sid)
            self.node.event('INFO', '示教校验保存完成，后续普通导航自动读取地图内参考')
        except Exception as exc:
            self.node.event('WARN', '示教保存失败，草稿已保留：' + str(exc))
        finally:
            self._taught_capture = await asyncio.to_thread(self.captures.status)
            self._taught_finish_busy = False
            self.map_busy = False
            await self.taught_push()

    async def handle_taught(self, ws, data):
        op = data['op']
        name = data.get('map') or (self._taught_capture or {}).get('map') or self.shown_map()
        if name is not None:
            _name(name)
        if op in ('taught_get', 'taught_status'):
            if name is None:
                raise TeachingError('先选择一张已保存地图')
            self.clients[ws]['taught_map'] = name
            return True, '', await self.taught_document(name)
        if self._taught_job or self._taught_finish_busy:
            raise TeachingError('示教正在准备或校验，请稍候')
        if op == 'taught_save':
            if self.map_busy:
                raise TeachingError('地图正在保存，请稍候')
            self.map_busy = True
            try:
                async with self._taught_mutation:
                    doc = await asyncio.to_thread(
                        self.taught_repo.save,
                        name,
                        data.get('points'),
                        expected_revision=data.get('expected_revision'),
                        map_token=data.get('map_token'),
                    )
            finally:
                self.map_busy = False
            await self.taught_push()
            return True, '示教已校验并随地图保存', await self.taught_document(name)
        if op == 'taught_prepare':
            if self.taught_open() and self._taught_capture['map'] != name:
                raise TeachingError('请先结束另一张地图的采集')
            if self.taught_recording():
                raise TeachingError('请先暂停采集')
            if self.busy or self.map_busy:
                raise TeachingError('运行服务或地图正在切换，请稍候')
            await asyncio.to_thread(self.taught_repo.document, name)
            self.clients[ws]['taught_map'] = name
            self.taught_checks[name] = {
                'state': 'preparing',
                'ok': False,
                'message': '准备加载地图并定位',
                'checks': [],
            }
            self._taught_job = asyncio.create_task(self.taught_prepare_job(name))
            self.pending.add(self._taught_job)
            self._taught_job.add_done_callback(self.pending.discard)
            return True, '正在准备，保持机器人静止', await self.taught_document(name)
        if op == 'taught_check':
            if self._taught_synced != name:
                await self.sync_taught_map(name, required=True)
            check, _ = await self.taught_self_check(name)
            return check['ok'], check['message'], await self.taught_document(name)
        if op == 'taught_start':
            if self.map_busy:
                raise TeachingError('地图正在保存，请稍候')
            check, measurement = await self.taught_self_check(name)
            if not check['ok']:
                return False, check['message'], await self.taught_document(name)
            async with self._taught_mutation:
                if self.taught_open():
                    raise TeachingError('已有未结束采集，请继续或放弃原草稿')
                self.reserve_taught_owner(ws, name)
                self._taught_capture = await asyncio.to_thread(
                    self.captures.start,
                    name,
                    mode=data.get('mode'),
                    expected_revision=data.get('expected_revision'),
                    map_token=data.get('map_token'),
                    measurement=measurement,
                    start_index=data.get('start_index'),
                    end_index=data.get('end_index'),
                )
                await self.confirm_taught_owner(ws)
            return True, '开始采集，请明确进入遥控', await self.taught_document(name)
        sid = data.get('session_id')
        capture = self._taught_capture
        if not capture or sid != capture['session_id']:
            raise TeachingError('采集编号已变化，请刷新后操作', 'SESSION_MISMATCH')
        name = capture['map']
        if self.taught_owner is not None and self.taught_owner is not ws:
            raise TeachingError('另一个页面正在操作这次采集，请在原页面暂停或断开后接管')
        if op == 'taught_resume':
            if self._taught_synced != name:
                await self.sync_taught_map(name, required=True)
            check, measurement = await self.taught_self_check(name)
            if not check['ok']:
                return False, check['message'], await self.taught_document(name)
            async with self._taught_mutation:
                self.reserve_taught_owner(ws, name)
                self._taught_capture = await asyncio.to_thread(
                    self.captures.resume, sid, measurement
                )
                await self.confirm_taught_owner(ws)
            return True, '已从断点恢复，按需进入遥控', await self.taught_document(name)
        if op in ('taught_pause', 'taught_finish', 'taught_abandon'):
            self.teleop = None
            self.node.publish_cmd(0.0, 0.0, 0.0)
            if op == 'taught_pause':
                self._taught_capture = await asyncio.to_thread(self.captures.pause, sid)
            elif op == 'taught_abandon':
                self._taught_capture = await asyncio.to_thread(self.captures.abandon, sid)
            else:
                if self.map_busy:
                    raise TeachingError('地图正在保存，请稍候')
                if self.taught_recording():
                    self._taught_capture = await asyncio.to_thread(
                        self.captures.pause, sid, '正在校验保存'
                    )
                self._taught_capture.update(state='processing', message='正在校验完整足迹并保存')
                self._taught_finish_busy = True
                self.map_busy = True
                self.background(self.taught_finish_job(sid))
            await self.disarm_and_cancel('示教采集停止遥控')
            return (
                True,
                '正在校验保存' if op == 'taught_finish' else self._taught_capture['message'],
                await self.taught_document(name),
            )
        raise TeachingError('未知示教操作')

    async def taught_loop(self):
        """20Hz读取测量；保留点由RouteRecorder的距离/角度门槛决定，落盘不在ROS回调。"""
        k = 0
        while True:
            await asyncio.sleep(0.05)
            k += 1
            try:
                if self.taught_recording():
                    try:
                        measurement = self.node.taught_measurement()
                        # 行走时不要求静止，也不要求每个AMCL协方差消息都更新；新鲜scan仍须可信。
                        scan = self.node._taught_scan
                        amcl = self.node.amcl
                        if not self.fresh_taught_scan(measurement['map_id']):
                            raise TeachingError('同一测量时刻的scan匹配率已过期')
                        if not amcl or amcl[0] > 0.3 or scan['match'] < 60.0:
                            raise TeachingError('定位质量下降，采集已暂停')
                        if self.estop or self.node.nav_active():
                            raise TeachingError('急停或其他导航任务介入，示教已暂停')
                        self._taught_missing_since = None
                        await asyncio.to_thread(self.captures.sample, measurement)
                        self._taught_capture = await asyncio.to_thread(self.captures.status)
                        if not self.taught_recording():
                            await self.disarm_and_cancel('示教测量出现断口')
                    except Exception as exc:
                        now = time.monotonic()
                        if self._taught_missing_since is None:
                            self._taught_missing_since = now
                        if now - self._taught_missing_since > 0.3:
                            await self.taught_interrupt(str(exc))
                            await self.disarm_and_cancel('示教测量或定位中断')
                if k % 10 == 0:
                    if self.stood_seq != self._taught_stood_seq:
                        await self.taught_interrupt('站立状态已变化，请重新定位后继续')
                        self.taught_invalidate('站起后请重新定位并自检')
                    if not self._taught_finish_busy:
                        self._taught_capture = await asyncio.to_thread(self.captures.status)
                        await self.taught_push()
            except Exception as exc:
                self.node.event('WARN', '示教状态更新失败：' + str(exc))

    async def taught_http(self, request):
        """离线编辑器只访问文件API；这里不调用LoadMap、initialpose、arm或速度发布。"""
        try:
            name = request.match_info.get('name')
            if name is None:
                return web.json_response(
                    {
                        'maps': [
                            {'name': p.stem}
                            for p in sorted(self.taught_repo.maps_dir.glob('*.yaml'))
                            if not p.is_symlink() and len(p.stem) <= 48
                        ]
                    }
                )
            _name(name)
            if request.path.endswith('/grid'):
                _, asset, _ = await asyncio.to_thread(self.taught_repo.grid, name)
                grid = asset['grid'].astype(np.int16)
                grid[grid < 0] = 255
                h, w = grid.shape
                origin = asset['origin']
                meta = json.dumps(
                    {
                        'w': w,
                        'h': h,
                        'res': asset['res'],
                        'ox': origin[0],
                        'oy': origin[1],
                        'oyaw': origin[2],
                    }
                ).encode()
                return web.Response(
                    body=struct.pack('<BI', 1, len(meta))
                    + meta
                    + zlib.compress(grid.astype(np.uint8).tobytes()),
                    content_type='application/octet-stream',
                )
            if request.method == 'POST':
                data = await request.json()
                if self.map_busy:
                    raise TeachingError('地图正在保存，请稍候')
                self.map_busy = True
                try:
                    async with self._taught_mutation:
                        await asyncio.to_thread(
                            self.taught_repo.save,
                            name,
                            data.get('points'),
                            expected_revision=data.get('expected_revision'),
                            map_token=data.get('map_token'),
                        )
                finally:
                    self.map_busy = False
                await self.taught_push()
                return web.json_response(
                    {
                        'ok': True,
                        'msg': '已校验并保存到地图文件',
                        'data': await self.taught_document(name),
                    }
                )
            doc = await self.taught_document(name)
            sid = request.query.get('capture')
            if sid:
                from teaching import SESSION

                if not SESSION.fullmatch(sid):
                    raise TeachingError('无效草稿编号')
                path = (
                    self.taught_repo.maps_dir
                    / (name + '.taught')
                    / 'captures'
                    / sid
                    / 'samples.jsonl'
                )

                def read_draft():
                    result = []
                    with path.open() as stream:
                        for line in stream:
                            if not line.endswith('\n'):
                                break
                            row = json.loads(line)
                            p = row['pose_map']
                            result.append(
                                {
                                    'x': p[0],
                                    'y': p[1],
                                    'yaw': p[2],
                                    'continuous_id': row['continuous_id'],
                                }
                            )
                    return result

                doc['draft_points'] = await asyncio.to_thread(read_draft)
            return web.json_response(doc)
        except Exception as exc:
            code = getattr(exc, 'code', 'TAUGHT_INVALID')
            status = (
                409 if code in ('VERSION_CONFLICT', 'MAP_MISMATCH', 'REVISION_CONFLICT') else 400
            )
            return web.json_response({'ok': False, 'msg': str(exc), 'code': code}, status=status)

    def taught_routes(self, app):
        app.router.add_get(
            '/taught-editor',
            lambda _: web.FileResponse(os.path.join(self.static_dir, 'taught-editor.html')),
        )
        app.router.add_get('/api/taught/maps', self.taught_http)
        app.router.add_get('/api/taught/maps/{name}/grid', self.taught_http)
        app.router.add_get('/api/taught/maps/{name}', self.taught_http)
        app.router.add_post('/api/taught/maps/{name}', self.taught_http)
