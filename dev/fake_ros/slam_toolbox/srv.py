"""假 slam_toolbox srv：只实现续建用到的 DeserializePoseGraph 和保存用的 SerializePoseGraph。

字段照 Humble 上游（srv/DeserializePoseGraph.srv）：
    string filename
    int8 match_type            # 0 UNSET / 1 START_AT_FIRST_NODE / 2 START_AT_GIVEN_POSE / 3 LOCALIZE_AT_POSE
    geometry_msgs/Pose2D initial_pose
没有 response 字段——上游 srv 的 response 段是空的。这一点真机上也一样：
服务「调用成功」不等于「图真的加载了」，所以续建必须做事后自检。
"""


class DeserializePoseGraph:
    # 和 rosidl 生成的代码一样：常量只在 Request 上，服务类本身没有（写错地方真机会 AttributeError）
    class Request:
        UNSET, START_AT_FIRST_NODE, START_AT_GIVEN_POSE, LOCALIZE_AT_POSE = 0, 1, 2, 3

        def __init__(self, filename='', match_type=0, initial_pose=None):
            self.filename = filename
            self.match_type = match_type
            self.initial_pose = initial_pose if initial_pose is not None else Pose2D()

    class Response:
        pass


class SerializePoseGraph:
    class Request:
        def __init__(self, filename=''):
            self.filename = filename

    class Response:
        RESULT_SUCCESS, RESULT_FAILED_TO_WRITE_FILE = 0, 255

        def __init__(self, result=0):
            self.result = result


class Pause:
    class Request:
        pass

    class Response:
        def __init__(self, status=True):
            self.status = status


class Pose2D:
    def __init__(self, x=0.0, y=0.0, theta=0.0):
        self.x, self.y, self.theta = x, y, theta
