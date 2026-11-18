"""仅供 dev/fake_ros 的时间值，不连接 DDS 或系统 ROS 时钟。"""
from types import SimpleNamespace as N


class Time:
    """和 rclpy 一样：Time() 的 0 表示最新 TF，非零值可查询假模拟器的测量快照。"""

    def __init__(self, *, seconds=0, nanoseconds=0, clock_type=None):
        self._nanoseconds = int(seconds * 1_000_000_000) + int(nanoseconds)
        self.clock_type = clock_type

    @property
    def nanoseconds(self):
        return self._nanoseconds

    @classmethod
    def from_msg(cls, msg, clock_type=None):
        return cls(seconds=msg.sec, nanoseconds=msg.nanosec, clock_type=clock_type)

    def to_msg(self):
        sec, nanosec = divmod(self._nanoseconds, 1_000_000_000)
        return N(sec=sec, nanosec=nanosec)
