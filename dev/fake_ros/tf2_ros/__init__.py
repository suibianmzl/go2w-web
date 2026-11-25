import math
from types import SimpleNamespace as N
from rclpy.time import Time

BODY_Z = 0.53   # 假 FAST-LIO 的 body 坐标系：在 base_footprint 正上方（真机上还有约 16° 俯仰）


def _tf(x=0.0, y=0.0, z=0.0, q=None, *, fixed='', frame='', stamp=0):
    q = q or N(x=0.0, y=0.0, z=0.0, w=1.0)
    return N(header=N(frame_id=fixed, stamp=Time(nanoseconds=stamp).to_msg()), child_frame_id=frame,
             transform=N(translation=N(x=x, y=y, z=z), rotation=q))


class Buffer:
    """假 TF：map 和 odom 重合（假里程计没有漂移），机器人位姿取自假模拟器。"""

    def __init__(self):
        self.node = None

    def lookup_transform(self, fixed, frame, when, **_kw):
        sim = getattr(self.node, 'fake_sim', None)
        if sim is None or not sim.any_ready():   # 导航栈没跑时没有 TF
            raise LookupError('no tf')
        stamp = when.nanoseconds
        sample = sim.tf_at(stamp) if stamp else None
        if not stamp:
            stamp = sim.ros_time_ns()
        def transform(x=0.0, y=0.0, z=0.0, q=None):
            return _tf(x, y, z, q, fixed=fixed, frame=frame, stamp=stamp)
        if fixed in ('map', 'odom') and frame in ('map', 'odom'):
            return transform()
        if fixed == 'base_footprint' and frame == 'base_link':
            # planar_odom 发的机身姿态：加减速时轻微俯仰，转弯时轻微横滚
            roll, pitch = sample[3] if sample else sim.tilt()
            cr, sr, cp, sp = math.cos(roll/2), math.sin(roll/2), math.cos(pitch/2), math.sin(pitch/2)
            return transform(q=N(x=sr*cp, y=cr*sp, z=-sr*sp, w=cr*cp))
        if fixed == 'base_footprint' and frame == 'body':
            return transform(z=BODY_Z)
        if fixed in ('map', 'odom') and frame == 'base_footprint':
            if sample:
                x, y, th = sample[1] if fixed == 'map' else sample[2]
            else:
                x, y, th = sim.map_pose() if fixed == 'map' else (sim.x, sim.y, sim.th)
            return transform(x, y, 0.0, N(x=0.0, y=0.0, z=math.sin(th / 2), w=math.cos(th / 2)))
        raise LookupError(f'{fixed} -> {frame} not available')


class TransformListener:
    def __init__(self, buf, node):
        buf.node = node
