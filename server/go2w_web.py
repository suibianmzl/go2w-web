#!/usr/bin/env python3
"""Go2W 网页控制台后端。

一个进程里跑两件事：
  - rclpy 节点 go2w_web（域 43）：订阅地图 / scan / 里程计 / 健康状态 / 导航状态，查 TF，
    并代网页发初始位姿、导航目标、取消、解锁 / 上锁、调参
  - aiohttp：同一端口提供安装的静态页面和 WebSocket（/ws）

安全规则（P1）
  - 急停：取消所有导航目标 + 调 /go2w_cmd_bridge/arm(false)（cmd_bridge 立即发 StopMove 并上锁）。
    急停是锁存的：解除之前拒绝解锁和发目标；急停期间如果发现底盘被别处解锁，会再次上锁。
  - 心跳：网页每 0.5 s 发一次 ping。最近一个发过运动相关指令的网页是「控制方」；
    控制方心跳超过 --heartbeat 秒（默认 5.0）或连接断开，而底盘处于解锁或有导航目标时，
    取消导航并上锁。恢复后保持上锁，必须手动重新解锁。

WebSocket 协议
  浏览器 → 后端（JSON）：
    {"t": "ping", "ts"}
    {"t": "cmd", "id", "op", ...}，op = initial_pose | goal | cancel | arm | estop | release_estop | set_param
  后端 → 浏览器（JSON）：
    {"t": "pong"} {"t": "ack", "id", "ok", "msg"} {"t": "pose"} {"t": "state"} {"t": "task"} {"t": "log"}
  后端 → 浏览器（二进制）：u8 类型 + u32 元数据长度（小端）+ 元数据 JSON + 负载
    1 = 地图：负载是 zlib 压缩的 uint8 栅格（0..100 占用概率，255 未知），行 0 在 origin 一侧
    2 = scan：负载是 float32 [x, y, x, y, ...]，已经变换到 fixed 坐标系

运行（统一运行容器里）：python3 /opt/go2w/console/server/go2w_web.py --port 8080
"""
import argparse
import asyncio
import base64
import hashlib
import json
import math
import os
import re
import shutil
import socket
import struct
import sys
import tempfile
import threading
import time
import uuid
import zlib
from collections import deque

import numpy as np
import rclpy
import rclpy.time
from action_msgs.msg import GoalStatus, GoalStatusArray
from action_msgs.srv import CancelGoal
from aiohttp import WSCloseCode, WSMsgType, web
from geometry_msgs.msg import PoseStamped, PoseWithCovarianceStamped, Twist
from nav2_msgs.action import ComputePathToPose, NavigateThroughPoses, NavigateToPose
from nav2_msgs.msg import BehaviorTreeLog, ParticleCloud
from nav2_msgs.srv import ClearCostmapExceptRegion, LoadMap
from nav_msgs.msg import OccupancyGrid, Odometry, Path
from rcl_interfaces.msg import Log, Parameter, ParameterType, ParameterValue
from rcl_interfaces.srv import GetParameters, SetParameters
from rclpy.action import ActionClient
from rclpy.executors import ExternalShutdownException, SingleThreadedExecutor
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy, qos_profile_sensor_data
from sensor_msgs.msg import LaserScan, PointCloud2
from std_msgs.msg import Bool
from rosidl_runtime_py import message_to_yaml
from rosidl_runtime_py.utilities import get_message
from std_srvs.srv import SetBool
from tf2_ros import Buffer, TransformListener

# 续建用的 slam_toolbox 接口。老镜像里没有这两个 srv 时不能整个后端起不来——建图档之外的功能
# 和续建无关，所以缺失时只把续建这条路标成不可用
try:
    from slam_toolbox.srv import DeserializePoseGraph
except ImportError:  # pragma: no cover - 只在没有 slam_toolbox 的环境走到
    DeserializePoseGraph = None
try:
    from slam_toolbox.srv import SerializePoseGraph
except ImportError:  # pragma: no cover
    SerializePoseGraph = None
from geometry_msgs.msg import Pose2D

import audio as AU
import camera as C
import incidents as INC
import keepout as KO
import mapcontinue as MC
import mapmeta as MM
import mapedit as ME
import mapfile
import procs as P
import robot_ctrl as R
import rtab as RT
from teaching import TeachingError
from teaching_bridge import TaughtMeasurements, TaughtWeb

# 二进制帧类型。代价地图 / 点云 / 粒子较重，只发给打开了对应图层的网页
T_MAP, T_SCAN, T_GCOST, T_LCOST, T_CLOUD, T_PART = 1, 2, 3, 4, 5, 6
T_CAM = 7   # 摄像头一帧 H.264（Annex-B），只发给正在看的那一个网页
HEAVY = {'gcost': T_GCOST, 'lcost': T_LCOST, 'cloud': T_CLOUD, 'particles': T_PART}
# 点云：离地高度范围（和 pcl2scan 一致）、最远距离、俯视体素、每帧最多点数、最高频率
CLOUD_Z, CLOUD_RANGE, CLOUD_VOXEL, CLOUD_MAX, CLOUD_DT = (0.30, 1.60), 12.0, 0.05, 4000, 0.2
PARTICLES_MAX, PARTICLES_DT = 300, 0.5
PLAN_MAX = 500
ROSOUT_PER_S = 30   # /rosout 转发限流（条 / 秒）
# 已挂载的持久数据；离线测试可以显式指定临时目录。
DATA = os.environ.get('GO2W_DATA') or '/go2w/data'
# 部署在扩展坞上时镜像里的 launch/config 读不到（/opt/go2w/releases 是 root-only），
# 所以续建核对切片高度时优先读这几处，找不到再用代码里的默认值并明确标注来源
CONFIG_DIRS = [os.environ.get('GO2W_CONFIG_DIR') or '',
               os.path.join(DATA, 'config'),
               '/opt/go2w/config',
               # V1.0.0.1 运行时镜像里 go2w_bringup 装在这里（11-17 在 go2w_runtime 容器里 find 到的）
               '/opt/go2w/base/go2w_bringup/share/go2w_bringup/config',
               '/go2w/ws/src/go2w_bringup/config']
# 录包上传到常驻服务器 166:~/go2w_bags。密钥在 data/ssh（不随 sync.sh 同步）；166 上这把钥匙被 rrsync 限制成只能往 go2w_bags 写
UPLOAD_DEST = os.environ.get('GO2W_UPLOAD_DEST', 'evoengine@192.168.1.22:')
UPLOAD_LABEL = os.environ.get('GO2W_UPLOAD_LABEL', '166:~/go2w_bags')
# 录包容量上限：data/bags 总量超过 BAG_LIMIT_GB，或磁盘剩余不到 BAG_MIN_FREE_GB，就从最旧的开始删「已上传到 166」的包；
# 没上传的不自动删，只报警并拒绝开始新的录包；正在录的包自己就超过上限（或磁盘快满）时自动停止录包
BAG_LIMIT_GB = float(os.environ.get('GO2W_BAG_LIMIT_GB', '100'))
BAG_MIN_FREE_GB = float(os.environ.get('GO2W_BAG_MIN_FREE_GB', '10'))
ROLLBACK_SECONDS = 20         # 手动「保存刚才 N 秒」的长度
# 包名 = <狗编号>_<月日>_<时分秒>（11-12：原来是 go2w_，改成带狗编号，多狗时能区分）
# 狗编号（11-18，用户：「robot id 写到机器狗自己的环境变量，不写到镜像里」）按这个顺序取：
#   1. 狗硬盘上的 runtime.env（/opt/go2w/config/runtime.env，容器里是 /etc/go2w/runtime.env）的 GO2W_ROBOT_ID
#   2. 进程环境变量 GO2W_ROBOT_ID（本地调试用；runtime.env 优先，镜像里就算写了也盖不过狗自己的）
#   3. ROBOT_ID_DEFAULT
# 每次录包 / 存回溯时现读，改了 runtime.env 不用重启
ROBOT_ID_DEFAULT = 'go2w-01'


def robot_id():
    """(狗编号, 来源)。"""
    env_file = os.environ.get('GO2W_RUNTIME_ENV')
    for path in ((env_file,) if env_file else ()) + MM.RUNTIME_ENV_FILES:
        v = (MM.read_env_file(path, 'GO2W_ROBOT_ID') or '').strip()
        if v:
            if P.SAFE_PREFIX.fullmatch(v):
                return v, path
            # 写了但格式不对：不悄悄换成别处的值，用默认并在来源里说明（启动日志会打出来）
            return ROBOT_ID_DEFAULT, f'默认值；{path} 里的 {v!r} 不合规（字母数字开头，只能有字母数字_-，最多 30 个字符）'
    v = os.environ.get('GO2W_ROBOT_ID', '').strip()
    if v and P.SAFE_PREFIX.fullmatch(v):
        return v, 'env'
    return ROBOT_ID_DEFAULT, 'default'
ROLLBACK_BUFFER = 30          # 回溯进程缓存的长度，要装得下卡住自动保存的 前 + 后
ROLLBACK_RETRY = 30.0         # 回溯进程退出后，隔多久再拉起来
ROLLBACK_SAVE_TIMEOUT = 20.0  # 等存包完成的最长时间
# 卡住自动保存：导航目标没结束、底盘已解锁、没急停，odom 位置在 STUCK_MOVE 内、朝向在 STUCK_TURN 内
# 持续 STUCK_SECONDS 就算卡住；以判定卡住的那一刻为中心，存前 STUCK_BEFORE 秒 + 后 STUCK_AFTER 秒（go2w_ 开头，
# 11-12 起 15 + 15 = 30 秒；原来是 2 秒后存最近 20 秒），两次自动保存至少隔 STUCK_COOLDOWN
STUCK_SECONDS = float(os.environ.get('GO2W_STUCK_SECONDS', '8'))
STUCK_MOVE, STUCK_TURN = 0.08, 0.15
STUCK_BEFORE, STUCK_AFTER, STUCK_COOLDOWN = 15.0, 15.0, 60.0
# 11-18 起所有异常（incidents.py 的 TYPES）都按上面的 前 + 后 自动存回溯包：
#   计划存包前又出了异常，离存包还有 ≥ INC_MERGE 秒就并进同一个包；
#   同一类异常两次自动存包至少隔 STUCK_COOLDOWN（异常照样记，包标成「没存」并说明原因）
INC_MERGE = 5.0
INC_PUSH = 50          # 网页连上时推最近多少条异常
INC_AUTO = object()    # Server.incident 的 task 默认值：取当前任务
# 档位进程起来后，看到这些节点才算「运行中」
MODE_NODES = {'mapping': ('slam_toolbox',), 'localization': ('amcl',), 'navigation': ('bt_navigator', 'go2w_cmd_bridge')}
START_TIMEOUT = 90.0
BRIDGE_RETRY = 10.0   # 遥控速度桥退出后，隔这么久再重启
# 键盘遥控限速（网页按键给的速度再被这里夹一次，cmd_bridge 还会再限一次）
TELEOP_MAX = (1.0, 0.2, 1.2)
TELEOP_TIMEOUT = 0.3
# 网页发送队列：一条消息 TX_TIMEOUT 秒还发不出去（网页不读了 / 网络断了）才断开；FIFO 消息积压超过 TX_QUEUE_MAX 也断开
TX_TIMEOUT = 10.0
TX_QUEUE_MAX = 500
INSPECT_DT = 0.5    # topic 查看器每条 topic 最多 2 Hz 刷新
ROS_LEVEL = {20: 'INFO', 30: 'WARN', 40: 'ERROR', 50: 'ERROR'}
BASE_FRAME = 'base_footprint'
STALE = 3.0  # 秒；超过这么久没收到就当没有
BAT_TOPIC = '/go2w/robot_lowstate'
BAT_PERIOD = 5.0    # 秒；多久读一次 lowstate（电量 + 电机温度）
BAT_STALE = 70.0    # 秒；超过这么久没读到电量，网页显示 --
MOTOR_STALE = 20.0  # 秒；超过这么久没读到电机温度，网页显示 --
# LowState 里 bms_state.soc 的位置：4 字节 CDR 头 + 1039（定长数组、按 CDR 对齐算出；11-16 录包验证 = 73%、29.9 V）
BAT_SOC_OFFSET = 4 + 1039
# motor_state[i].temperature（int8，℃）：motor_state 从 77 开始、每个 48 字节，temperature 在元素内 +31
# （11-17 用 11-13/11-16 录包验证：关节角按 FR/FL/RR/RL × 髋/大腿/小腿 排列，12–15 是轮子，温度 23–81 ℃）
MOTOR_TEMP_OFFSET, MOTOR_STRIDE, MOTOR_N = 4 + 108, 48, 16
# motor_state[i].reserve[0]（uint32）当错误标志（同 app「狗腿信息 → 错误标志」），在元素内 +39 = temperature 后 8 字节。
# 11-17 用 go2w-01_1116_172503 录包核对（14772 帧）：16 个电机全程 0（= 正常）；reserve[1] 一直 507/508，不是错误码
MOTOR_ERR_OFFSET = MOTOR_TEMP_OFFSET + 8
# motor_state[i].tau_est（float32，N·m，估计力矩）：元素内 q +0 / dq +4 / ddq +8 / tau_est +12 → temperature 前 16 字节。
# 11-17 用 go2w-01_1116_172503 核对：站着时小腿约 +7、左右髋符号相反（±4~7），轮子 ±2 以内
MOTOR_TAU_OFFSET = MOTOR_TEMP_OFFSET - 16
# 网页打开「力矩」视图时 lowstate 改成每 MOTOR_FAST_PERIOD 秒读一次（= 状态推送 2 Hz），关掉后回到 BAT_PERIOD
MOTOR_FAST_PERIOD = 0.5
# 建图自动保存（11-17，用户：「报错自动保存地图」「两分钟太长，三十秒」）：建图时每 AUTOSAVE_PERIOD 秒、
# 停止建图前、雷达 / FAST-LIO / 里程计出问题时，把当前地图存成 data/maps/autosave_<建图开始时间>，
# 同一次建图覆盖同一份。只在地图有变化时存；单次存图耗时超过周期的 1/10 就自动拉长间隔，免得拖慢 slam。
# 只留最近 AUTOSAVE_KEEP 次建图的自动保存（被路线引用的不删）
AUTOSAVE_PERIOD = float(os.environ.get('GO2W_AUTOSAVE_SECONDS', '30'))
AUTOSAVE_KEEP = 3
AUTOSAVE_PREFIX = 'autosave_'
AUTOSAVE_PROBLEM_GAP = 5.0    # s：出问题时立刻存，但两次之间至少隔这么久
AUTOSAVE_PREV = '_prev'       # 每次覆盖前把上一份挪成 <名字>_prev：坏图写进来时还能退一步
# 只有网页自己起的名字才算自动保存：autosave_MMDD_HHMMSS 和它的 _prev。11-19 在 .21 上，清理按前缀
# 「autosave_」认，把从自动保存续建出来、用户手动保存的 autosave_…_c1 也当成自动保存直接删掉了（不进回收站）
AUTOSAVE_RE = re.compile(r'autosave_\d{4}_\d{6}(?:_prev)?')


def is_autosave(name):
    return isinstance(name, str) and AUTOSAVE_RE.fullmatch(name) is not None
# 摔倒 / 里程计发散保护（11-19：.30 建图时摔倒，FAST-LIO 发散，「出问题时自动保存」反而把摔倒前的好图
# 覆盖成了 467 × 551 m 的坏图——/odom_healthy 只看 FAST-LIO 还在不在输出，不看输出对不对）。
# 检测到「运控意外掉到 Passive」或「里程计数值离谱」：建图立刻冻结（关扫描闸门、停自动保存），并记异常 + 存回溯包
ODOM_WILD_SPEED = 4.0         # m/s：Go2W 实际最快约 1.2 m/s；11-19 发散时 -8 → -48 m/s
ODOM_WILD_TURN = 8.0          # rad/s
ODOM_WILD_JUMP = 1.0          # m：相邻两条 /odom（10 Hz）位置跳变
FALL_LIE_GRACE = 3.0          # s：网页刚让狗趴下后的这段时间里掉到 Passive 不算意外
# 摔倒 vs 遥控器趴下（11-23：11-20 晚上 4 次「意外趴下」倾角都很小、也没有 API 请求，应该是遥控器 LT+B，却被记成摔倒）：
# 运控两种情况都不留原因。运控的姿态保护是机身倾角 > 1.0 rad（57°）才进 Passive，真摔倒后狗也是侧躺 / 翻着的；
# 遥控器趴下是正常软着陆，机身基本水平。所以看掉到 Passive 前 FALL_TILT_PRE 秒到后 FALL_TILT_POST 秒内的
# 最大倾角：≥ FALL_TILT_DEG 记「摔倒」（存包）；更小 = 遥控器趴下，只写日志。楼梯上正常走俯仰也有 40°，所以门限取 50°。
# 拿不到姿态（planar_odom 没在跑）时分不清，照旧按摔倒记。
FALL_TILT_DEG = 50.0
FALL_TILT_PRE = 2.0           # s
FALL_TILT_POST = 1.0          # s：真摔倒时姿态保护触发后还在倒，等一下再判断
# 路线启动时离第 1 站不到这么远，就当已在第 1 站、直接去第 2 站：这么短的路径 RotationShim 不会先转正
# （forward_sampling_distance 0.5），MPPI 只给出很小的速度，Go2W 不执行，会一直卡住
ROUTE_NEAR_START = 0.5
# 11-24 任务接续：任务没跑完（Nav2 失败、手动取消、急停、断线上锁、切档位、网页服务重启 / 断电）就在
# data/route_resume.json 记断点（到了第几站、第几圈、已完成几站、停在哪里）。网页 / 云端可以从断点接着跑完剩下的站，
# 圈数接着算。断点只在人点「继续」时用，不会自己出发；超过这么久、或者任务的站点改过就作废
ROUTE_RESUME_MAX_AGE = 24 * 3600   # s
# 多点任务：Nav2 报未到达（差几厘米到 goal checker 0.15 m 判不过、收尾蹭不进去）时，
# 机器人离该站不超过这么远就当作到了，继续下一站；更远才终止任务
ROUTE_ARRIVE_TOL = 0.4
# 11-20 到点卡顿：每站单独一个 NavigateToPose、带固定朝向 → 原地转到朝向、完全停下 ~1 s、再慢慢起步，每站多花 4–6 s。
# 现在中间站（后面还有下一站的）不停车：朝向改成面向下一站，离站不到这么远就直接发下一站（Nav2 新目标顶替旧目标，
# 控制器不停）。只有整条任务的最后一站才按自己的朝向停稳
ROUTE_PASS_DIST = 0.6   # 实际取 min(0.6, 这段路长度的一半)
# 11-20 晚：拐角大的站提前换目标会「开过点」——狗还在 0.4 m/s 往前冲，Nav2 换目标要 ~1 s，冲过去后倒车 ~1.5 s、
# 再慢慢原地转，比停下来转还慢。所以只有拐角不超过这个角度（= RotationShim angular_dist_threshold 0.5 rad）
# 的站才不停车经过；拐角大的站照常到点停下，但朝向仍面向下一站（只转一次，不会先转到站点朝向再转回来）
# 11-24：0.5 rad 时 1–2 m 一站、拐角 37–46° 的小圈（lab_20261118_2，11 站）几乎每站都停，每站减速 + 原地转 + 再起步
# ≈ 2.5 s，一圈多 18 s（DDS 黑匣子 16:47–16:57）。放宽到 1.05 rad（60°）；要配合把 RotationShim angular_dist_threshold
# 调大（~0.9 rad，狗上 ~/nav_tune/shim_turn.sh），否则换成下一站的新路径偏角 > 0.5 rad 时 shim 照样原地转
ROUTE_PASS_TURN = 1.05
# 11-24：Nav2 报到达时离站点超过这么远就不算（xy 容差 0.25 + 余量）。提前换下一站后，Nav2 有时 0.7 s 就报新目标
# 「到达」，狗其实离那站还有 ~0.9 m（4/6 趟第 10 站被这样跳过）。这时重新发这一站，最多 ROUTE_FAKE_RETRY 次
ROUTE_FAKE_ARRIVE = 0.5
ROUTE_FAKE_RETRY = 2
ROUTE_DWELL_MAX = 3600   # s：站点停留最长 1 小时
# 11-24 v1.1.11「连续通过」（任务运行参数 opts.through，默认开）：以停留站为界把路线切成几段，每段一个
# NavigateThroughPoses（Nav2 一次规划经过这段所有站的连续路径），中间站不减速不停车；停留站 / 终点照常停稳。
# 行为树用网页发布包里的 server/bt/navigate_through_go2w.xml（恢复动作同 navigate_go2w.xml，RemovePassedGoals 每轮都跑）。
# 网页离下一站不到 ROUTE_THROUGH_PASS 就算经过（和行为树里的半径一样）；Nav2 先划掉了也算
ROUTE_THROUGH_PASS = 0.5
# 11-25：一段的终点离狗不到这么远（goal checker xy 0.25 + 定位余量）就不能当这段的终点发，见 run_through
ROUTE_THROUGH_NEAR_END = 0.6
ROUTE_SEG_MAX = 30      # 一段最多几站（一直循环、没有停留站时也要分段）
THROUGH_BT = os.environ.get('GO2W_THROUGH_BT') or os.path.join(
    os.path.dirname(os.path.realpath(__file__)), 'bt', 'navigate_through_go2w.xml')
ROUTE_NOTE_MAX = 200     # 站点备注最多几个字符
# 地图显示方向（只转网页画面，不改地图坐标）：按地图名存在 data/map_views.json，大家看到的方向一样。
# 建图时还没有地图名，用 MAPPING_VIEW 暂存，存图时带给新地图
MAPPING_VIEW = '_mapping'
BRIDGE = '/go2w_cmd_bridge'
# 网页上的滑条 → cmd_bridge 参数（都是 double）
PARAMS = {'vmax': 'max_vx', 'wmax': 'max_wz', 'tmo': 'cmd_timeout'}
# 和 RViz 2D Pose Estimate 相同的初始协方差
COSTMAP_REFILL = 1.5   # s：清完代价地图后等障碍层按当前点云重新标记（全局 1 Hz 更新）
INIT_COV_XY, INIT_COV_YAW = 0.25, 0.06853891945200942
# 重定位微调（网页「应用到 AMCL」）：用户已经对准了，σ 取 5 cm / 2°，粒子不撒开
FINE_COV_XY, FINE_COV_YAW = 0.05 ** 2, math.radians(2) ** 2
# 自动定位（globloc.py，子进程跑）：用最近 AUTOLOC_SCANS 帧 scan（机器人静止时叠在一起）在整张地图上搜。
# 定位 / 导航档位启动完成后自动跑一次（GO2W_AUTO_LOCALIZE=0 关掉）；网页「自动定位」按钮随时可以再跑
AUTOLOC_SCANS = 5
AUTOLOC_STILL = (0.03, 0.05)   # 静止判定：|v| < 0.03 m/s 且 |wz| < 0.05 rad/s
AUTOLOC_TIMEOUT = 60.0
AUTOLOC_ON_START = os.environ.get('GO2W_AUTO_LOCALIZE', '1') != '0'
# 刚站起来（策略刚接管）机身还在晃，雷达高度也和趴着时不一样：策略 ready 后再稳这么久，只用这之后的 scan
AUTOLOC_SETTLE = 2.0
AUTOLOC_SETTLE_WAIT = 15.0   # s：最多等这么久机器人站稳、静止，等不到就报失败
# 两处一致（11-16）：起点附近分数常在 0.62–0.72，过不了 0.75，但位置每次都一样。挪一下再定：两次结果算出的
# map→odom 一致（里程计把两处连起来），各自分数 ≥ PAIR_SCORE、次优/最优 ≤ PAIR_RATIO，就接受。
# 同一个地方反复算不算数（同一批 scan 几乎一样，不独立）：两次之间要挪开 ≥ PAIR_SEP 米或转过 ≥ PAIR_TURN
AUTOLOC_PAIR_SCORE, AUTOLOC_PAIR_RATIO = 0.60, 0.80
AUTOLOC_PAIR_SEP, AUTOLOC_PAIR_TURN = 0.20, math.radians(30)
AUTOLOC_PAIR_TOL, AUTOLOC_PAIR_TOL_TH = 0.20, math.radians(4)
AUTOLOC_HIST_TTL = 300.0     # s：更早的结果不拿来配对（里程计也会慢慢漂）
# 自动定位失败后「移动后重试」（网页按钮，用户确认后才动）：优先前进，前面不够空就后退，前后都不行就原地转
AUTOLOC_MOVE_DIST, AUTOLOC_MOVE_TURN = 0.30, math.radians(45)
AUTOLOC_MOVE_V, AUTOLOC_MOVE_W = 0.15, 0.35   # 倒车时 cmd_bridge 自己补 reverse_offset
AUTOLOC_MOVE_TIMEOUT = 8.0   # s：一次移动最长时间，超时就停
AUTOLOC_MOVE_MAX = 3         # 没定位成功前最多挪几次
AUTOLOC_FOOT = (0.365, 0.24)  # 足迹半长、半宽（0.73×0.48，中心在旋转中心；和 nav2.yaml footprint 一致）
AUTOLOC_MOVE_MARGIN = 0.25   # 前进 / 后退：移动距离之外再空出这么多
AUTOLOC_MOVE_STOP = 0.20     # 移动中：运动方向这么近有点就立刻停
AUTOLOC_TURN_CLEAR = 0.65    # 原地转：这个半径内不能有点（足迹外接圆 0.44 m + 余量）
# 注意：scan 只保留 ≥0.45 m 的点（滤掉机身），离旋转中心 0.45 m 以内看不见，空间检查管不到那一圈
# 运控（go2w_ctrl）站起 / 趴下，见 robot_ctrl.py。GO2W_CTRL=0 关掉（不查站没站起来，也不自动趴下）
CTRL_ON = os.environ.get('GO2W_CTRL', '1') != '0'
CTRL_STALE = 2.0       # s：状态 topic 10 Hz，超过这么久没收到就当不知道
# 雷达无数据（11-19：.21 雷达掉线一个多小时，web.log 里只有「里程计不可用」的间接痕迹，网页红条只在浏览器里）：
# 建图 / 定位 / 导航跑稳 LIDAR_GRACE 秒后，/scan 连续 LIDAR_STALE 秒没数据 → ERROR 日志 + 持续异常 lidar_lost；
# 同时探一下雷达 IP 的 ARP，区分「雷达不在网上（供电 / 网线 / IP）」和「网络通但没点云（驱动 / 配置）」
LIDAR_GRACE = float(os.environ.get('GO2W_LIDAR_GRACE', '15'))
LIDAR_STALE = float(os.environ.get('GO2W_LIDAR_STALE', '5'))
LIDAR_CONFIGS = [c for c in (os.environ.get('GO2W_LIDAR_CONFIG'), '/tmp/go2w_MID360_config.json',
                             '/opt/go2w/base/go2w_bringup/share/go2w_bringup/config/MID360_config.json') if c]
LIDAR_ARP = os.environ.get('GO2W_LIDAR_ARP', '/proc/net/arp')


def lidar_ips():
    """雷达驱动配置里的雷达 IP（运行时配置优先）。读不到返回 []。"""
    for path in LIDAR_CONFIGS:
        try:
            with open(path, encoding='utf-8') as f:
                cfg = json.load(f)
        except (OSError, ValueError):
            continue
        ips = [c.get('ip') for c in cfg.get('lidar_configs', []) if isinstance(c, dict) and isinstance(c.get('ip'), str)]
        if ips:
            return ips
    return []


def arp_state(ip, path=None):
    """True = ARP 表里有这个 IP 的网卡地址（网络上找得到它）；False = 没有；None = 读不了 ARP 表。"""
    try:
        with open(path or LIDAR_ARP, encoding='ascii') as f:
            rows = f.read().splitlines()[1:]
    except OSError:
        return None
    for row in rows:
        col = row.split()
        if len(col) >= 4 and col[0] == ip:
            return int(col[2], 16) & 0x2 != 0 and col[3] != '00:00:00:00:00:00'
    return False


async def lidar_diagnose():
    """雷达没数据时给一句人话：往雷达 IP 的 9 号端口发一个 UDP 包（雷达不理）逼系统去 ARP，1.5 s 后看 ARP 表。不需要 root。"""
    ips = lidar_ips()
    if not ips:
        return '读不到雷达驱动配置里的雷达 IP，没法诊断网络'
    for ip in ips:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as u:
                u.setblocking(False)
                u.sendto(b'go2w', (ip, 9))
        except OSError:
            pass
    await asyncio.sleep(1.5)
    out = []
    for ip in ips:
        st = arp_state(ip)
        if st is None:
            out.append(f'雷达 {ip}：读不了 ARP 表，网络状态未知')
        elif st:
            out.append(f'雷达 {ip} 网络是通的，但没有点云：看雷达驱动日志 data/runtime/logs/navigation.log，或停止档位后重开')
        else:
            out.append(f'雷达 {ip} 在网络上没有回应（ARP 不通）：雷达没上电、网线没插好或者 IP 变了，检查雷达供电和网线')
    return '；'.join(out)
CTRL_WAIT = 3.0        # s：每一步切换（插值走完之后）最多等这么久，和同事的 sport_cli up 一致
CTRL_INTERP = 2.0      # s：FixStand 从趴姿插值到站姿的时间（config.yaml 的 ts 末项）
POLICY_WAIT = 20.0     # s：切策略重启运控后，最多等这么久收到新进程的 Passive
CTRL_RESEND = 0.5      # s：请求还没被接受就重发（运控 0.2 s 后作废没取走的请求，DDS 也可能丢）
# 爬台模式（11-23）：融合策略（rough + climb）下网页开关，开着时 5 Hz 发 2101 续约（运控 1 s 没收到就回 rough）。
# 开着最长这么久自动关，免得忘了关一直用 climb 策略走路
CLIMB_MAX = float(os.environ.get('GO2W_CLIMB_MAX', '300'))
# 网页站起来（或跑过任务）的机器狗，空闲这么久就原地趴下；0 = 不自动趴下
IDLE_LIE = float(os.environ.get('GO2W_IDLE_LIE', '30'))
# 电机过热自动趴下（11-18，用户：「有关节到 68 度就自动趴下」）：16 个电机（12 个腿关节 + 4 个轮子）任何一个
# ≥ 这个温度，站着就趴下（先取消任务、上锁、等停稳，和手动趴下同一流程），不管是不是「保持站立」、有没有任务；
# 还在这个温度以上时不让站起。温度每 BAT_PERIOD 秒读一次，超过 MOTOR_STALE 没读到就不判断。0 = 关掉
MOTOR_HOT_LIE = float(os.environ.get('GO2W_MOTOR_HOT_LIE', '68'))
MOTOR_HOT_RETRY = 5.0  # s：趴下失败（运控没响应等）隔这么久再试
MOTOR_NAMES = [f'{leg}{j}' for leg in ('右前', '左前', '右后', '左后') for j in ('髋', '大腿', '小腿')] + \
              [f'{leg}轮' for leg in ('右前', '左前', '右后', '左后')]   # 顺序同 LowState.motor_state
PREP_HOLD_TTL = 5.0    # s：网页自检进行中每 2 s 发一次 prep_hold，期间不算空闲
STILL_V, STILL_W = 0.05, 0.1   # 趴下前要求里程计速度低于这些（m/s、rad/s）
# 行为树里表示「在做恢复」的节点
RECOVERY_NODES = ('Spin', 'BackUp', 'Wait', 'DriveOnHeading')
ACTIVE = (GoalStatus.STATUS_ACCEPTED, GoalStatus.STATUS_EXECUTING, GoalStatus.STATUS_CANCELING)
RESULT = {GoalStatus.STATUS_SUCCEEDED: 'succeeded', GoalStatus.STATUS_CANCELED: 'canceled', GoalStatus.STATUS_ABORTED: 'aborted'}


def wrap(a):
    """角度归到 (-π, π]。"""
    return (a + math.pi) % (2 * math.pi) - math.pi


def yaw_of(q):
    return math.atan2(2 * (q.w * q.z + q.x * q.y), 1 - 2 * (q.y * q.y + q.z * q.z))


class StuckWatch:
    """导航中机器人一直停在原地（位置、朝向都基本没变）就报一次；动起来或任务结束后重新计时。"""
    def __init__(self, seconds=STUCK_SECONDS, move=STUCK_MOVE, turn=STUCK_TURN):
        self.seconds, self.move, self.turn = seconds, move, turn
        self.anchor, self.fired = None, False

    def update(self, now, pose, active):
        """pose = odom 下的 (x, y, yaw) 或 None。刚判定卡住时返回已经停了多少秒，其余时候返回 None。"""
        if not active or pose is None:
            self.anchor, self.fired = None, False
            return None
        a = self.anchor
        if (a is None or math.hypot(pose[0] - a[0], pose[1] - a[1]) > self.move
                or abs(math.atan2(math.sin(pose[2] - a[2]), math.cos(pose[2] - a[2]))) > self.turn):
            self.anchor, self.fired = (pose[0], pose[1], pose[2], now), False
            return None
        if not self.fired and now - a[3] >= self.seconds:
            self.fired = True
            return now - a[3]
        return None


def pack(kind, meta, payload=b''):
    j = json.dumps(meta, separators=(',', ':')).encode()
    return struct.pack('<BI', kind, len(j)) + j + payload


def secs(d):
    return d.sec + d.nanosec * 1e-9


def rot_matrix(q):
    x, y, z, w = q.x, q.y, q.z, q.w
    return np.array([[1-2*(y*y+z*z), 2*(x*y-z*w), 2*(x*z+y*w)],
                     [2*(x*y+z*w), 1-2*(x*x+z*z), 2*(y*z-x*w)],
                     [2*(x*z-y*w), 2*(y*z+x*w), 1-2*(x*x+y*y)]], dtype=np.float32)


def grid_blob(kind, m, place):
    """OccupancyGrid → 二进制帧。place = (x, y, yaw)：栅格原点在 fixed 坐标系下的位置和朝向。"""
    info = m.info
    grid = np.asarray(m.data, dtype=np.int8).astype(np.int16)
    grid[grid < 0] = 255
    meta = {'w': info.width, 'h': info.height, 'res': info.resolution,
            'ox': place[0], 'oy': place[1], 'oyaw': place[2], 'frame': m.header.frame_id}
    return pack(kind, meta, zlib.compress(grid.astype(np.uint8).tobytes(), 6)), grid


def roll_pitch(q):
    roll = math.atan2(2 * (q.w * q.x + q.y * q.z), 1 - 2 * (q.x * q.x + q.y * q.y))
    pitch = math.asin(max(-1.0, min(1.0, 2 * (q.w * q.y - q.z * q.x))))
    return roll, pitch


MATCH_TOL = 0.10  # m：scan 点离地图障碍物这么近就算「对上了」
MATCH_HOLD_S = 0.6  # s：设完初始位姿后这么久内的 scan 不算匹配率（AMCL 处理 /initialpose、更新 map→odom 需要时间）
GATE_QUERY_TIMEOUT = 8.0  # s：问建图扫描闸门 scan_budget 的超时（狗负载高时 3 s 不够）


class Rate:
    """最近 2 秒的消息频率。"""

    def __init__(self):
        self.stamps = []

    def tick(self):
        now = time.monotonic()
        self.stamps.append(now)
        while self.stamps and now - self.stamps[0] > 2.0:
            self.stamps.pop(0)

    def hz(self):
        now = time.monotonic()
        s = [t for t in self.stamps if now - t <= 2.0]
        return round(len(s) / 2.0, 1)


class SpeedCheck:
    """诊断用：同一段 /odom 位移，分别除以「消息时间戳差」和「本机收到消息的时间差」。

    planar_odom 的 twist 用的是时间戳差；如果雷达时间戳走得比真实时间快，位置是对的，
    速度却会整体偏小。rate = 时间戳走过的时间 / 真实走过的时间，正常应接近 1。
    """

    WINDOW = 0.5  # 秒（真实时间）

    def __init__(self):
        self.buf = []  # (wall, stamp, x, y, yaw)

    def add(self, wall, stamp, x, y, yaw):
        if self.buf and stamp <= self.buf[-1][1]:
            return
        self.buf.append((wall, stamp, x, y, yaw))
        while self.buf and wall - self.buf[0][0] > 2.0:
            self.buf.pop(0)

    def result(self):
        if len(self.buf) < 2 or time.monotonic() - self.buf[-1][0] > 0.5:
            return None
        last = self.buf[-1]
        first = next((s for s in self.buf if last[0] - s[0] <= self.WINDOW), self.buf[0])
        dw, ds = last[0] - first[0], last[1] - first[1]
        if dw < 0.2 or ds <= 0:
            return None
        c, s = math.cos(last[4]), math.sin(last[4])
        dx, dy = last[2] - first[2], last[3] - first[3]
        fwd, side = c * dx + s * dy, -s * dx + c * dy
        dyaw = math.atan2(math.sin(last[4] - first[4]), math.cos(last[4] - first[4]))
        return {'wall': [round(fwd / dw, 3), round(side / dw, 3), round(dyaw / dw, 3)],
                'stamp': [round(fwd / ds, 3), round(side / ds, 3), round(dyaw / ds, 3)],
                'rate': round(ds / dw, 3)}


class SysStats:
    """宿主机 CPU / 内存（容器用 host 网络，/proc/stat 和 /proc/meminfo 反映的是整机）。"""

    def __init__(self):
        self.prev = None

    def read(self):
        try:
            return self._read()
        except OSError:  # 读不到就显示 --，不影响其他状态
            return {'cpu': None, 'mem': None, 'memTotal': None}

    def _read(self):
        with open('/proc/stat') as f:
            v = [int(x) for x in f.readline().split()[1:]]
        idle, total = v[3] + v[4], sum(v)
        cpu = None
        if self.prev:
            dt, di = total - self.prev[1], idle - self.prev[0]
            cpu = round(100.0 * (1 - di / dt), 1) if dt > 0 else None
        self.prev = (idle, total)
        mem = {}
        with open('/proc/meminfo') as f:
            for line in f:
                k, val = line.split(':')
                mem[k] = int(val.split()[0])
        total_g = mem['MemTotal'] / 1048576
        used_g = (mem['MemTotal'] - mem.get('MemAvailable', mem['MemFree'])) / 1048576
        return {'cpu': cpu, 'mem': round(used_g, 2), 'memTotal': round(total_g, 1)}


class Bridge(TaughtMeasurements, Node):
    # 节点名 → 界面上的名字；按 launch 文件里的 name= 对应
    NODES = [
        ('livox_lidar_publisher', '雷达驱动'),
        ('fast_lio', 'FAST-LIO'),
        ('planar_odom', 'planar_odom'),
        ('pcl2scan_slam', 'pcl2scan'),
        ('amcl', 'AMCL'),
        ('slam_toolbox', 'slam_toolbox'),
        ('bt_navigator', 'Nav2'),
        ('go2w_cmd_bridge', 'cmd_bridge'),
    ]

    def __init__(self):
        super().__init__('go2w_web')
        self.init_taught_measurements()
        self.tf = Buffer()
        self.tf_listener = TransformListener(self.tf, self)
        self.rates = {k: Rate() for k in ('/scan', '/odom', '/odom_healthy', '/go2w_cmd_bridge/armed')}
        self.lock = threading.Lock()   # 保护 goal / statuses / bt_running（rclpy 线程写，asyncio 线程读）
        self.events = deque(maxlen=300)  # (级别, 节点, 消息) → 推给网页日志（本节点事件 + /rosout）
        self.rosout_budget, self.rosout_t, self.rosout_dropped = ROSOUT_PER_S, time.monotonic(), 0
        self.wants = lambda layer: False  # 由 Server 替换：有没有网页打开了这个图层（重的数据没人看就不算）

        # P2 图层：最新的二进制帧和版本号，广播循环发现版本变了才发
        self.blobs = {k: None for k in HEAVY.values()}
        self.vers = {k: 0 for k in HEAVY.values()}
        self.grid_raw = {}       # 代价地图原始字节，内容没变就不重发
        self.gcost = None        # 全局代价地图 (栅格, res, ox, oy, w, h)，给网页判断目标能不能去
        self.cloud_t = self.part_t = 0.0
        self.plan, self.plan_ver = [], 0
        self.cmd = (0.0, 0.0, 0.0, 0.0)  # /cmd_vel (vx, vy, wz, 收到时间)

        # P3
        self.fb_any = {}         # 所有目标的反馈（含 RViz、patrol 发的）：uuid → (剩余距离, 用时, 恢复次数)
        self.map_current = None  # map_server 正在用的地图名（读它的 yaml_filename 参数）
        self.inspect = {}        # topic → {'sub', 'text', 't', 'ver', 'rate'}；只在有网页查看时订阅
        self.ros_ops = deque()   # 要在 rclpy 线程里做的事（建 / 删订阅），由定时器执行
        self.create_timer(0.1, self.run_ros_ops)
        # 电量：/go2w/robot_lowstate（unitree_go/LowState，约 230 Hz、每帧 1.2 KB）里的 bms_state.soc。
        # 一直订阅太费 CPU（planar_odom 订同类消息吃 50%+），所以每 BAT_PERIOD 秒临时订一次，收到一帧就退订，
        # 而且不反序列化，只按 CDR 偏移读 soc 那一个字节
        self.battery, self.battery_t, self.bat_sub = None, 0.0, None
        self.motor_temp, self.motor_temp_t = None, 0.0   # 16 个电机温度 ℃（顺序见 MOTOR_TEMP_OFFSET）
        self.motor_info = None   # 同一帧的错误标志 / 力矩，和 motor_temp 一起更新、一起过期
        self.motor_fast = False  # 有网页在看力矩：读得勤一点（WebApp 维护）
        self.bat_poll_t = 0.0
        self.create_timer(MOTOR_FAST_PERIOD, self.poll_tick)
        self.poll_battery()

        self.map_blob, self.map_ver = None, 0
        self.map_file = None     # 网页上显示的是 data/maps 里直接读的哪张图（导航没开时）；None = 来自 /map
        self.near_occ = None    # 地图障碍物膨胀 MATCH_TOL 后的布尔栅格，算匹配率用
        self.map_info = None
        self.map_loaded = None   # 网页「切换地图」最近加载的地图名（map_server 重启后作废）
        self.amcl = None         # (位置 σ m, 航向 σ °, 收到时间)
        self.match = None        # scan / map 匹配率 %，平滑过
        self.match_hold = 0.0    # monotonic：刚设完初始位姿，到这之前的 scan 不算匹配率（AMCL 还没用上新位姿）
        self.scan_blob, self.scan_ver = None, 0
        self.map_raw = None      # 自动定位用：(OccupancyGrid 原始 int8 栅格 h×w, res, ox, oy)
        self.map_crc = None      # 最新 /map 内容的 crc32（自动保存判断有没有变化）
        self.scans_base = deque(maxlen=AUTOLOC_SCANS)   # 自动定位用：(收到时间, base_footprint 下的 N×2 点, 收到时是否静止)
        self.scan_evidence = deque(maxlen=100)
        self.slam_poses = deque(maxlen=32)
        self.map_sample = (0, None)
        self.odom = None
        self.odom_pose = None    # (x, y, yaw, 收到时间)，odom 坐标系；卡住检测用
        self.odom_wild = None    # (原因, monotonic, time)：里程计第一次出现离谱数值；FAST-LIO 重启（换档位）时清掉
        self.speed = SpeedCheck()
        self.rate_warned = 0.0
        self.healthy, self.healthy_t = None, 0.0
        self.armed, self.armed_t = None, 0.0
        self.fixed = 'map'

        self.goal = None        # 网页发出的当前目标（见 track_goal）
        self.on_goal_fail = None   # 单点目标被 Nav2 放弃时回调 (x, y, th)，在 rclpy 线程里调；Server 设置
        self.statuses = {}      # 所有 navigate_to_pose 目标的状态：uuid → status（含 RViz、patrol 发的）
        self.statuses_through = {}   # navigate_through_poses 目标的状态（11-24 任务「连续通过」）
        self.bt_running = set()
        self.params = {}

        latched = QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL,
                             reliability=ReliabilityPolicy.RELIABLE)
        self.create_subscription(OccupancyGrid, '/map', self.on_map, latched)
        self.create_subscription(LaserScan, '/scan', self.on_scan, qos_profile_sensor_data)
        self.create_subscription(PoseWithCovarianceStamped, '/pose', self.on_slam_pose, 10)
        self.create_subscription(Odometry, '/odom', self.on_odom, 20)
        self.create_subscription(Bool, '/odom_healthy', self.on_healthy, 10)
        self.create_subscription(Bool, f'{BRIDGE}/armed', self.on_armed, 10)
        self.create_subscription(GoalStatusArray, '/navigate_to_pose/_action/status', self.on_status, latched)
        self.create_subscription(GoalStatusArray, '/navigate_through_poses/_action/status', self.on_status_through, latched)
        self.create_subscription(BehaviorTreeLog, '/behavior_tree_log', self.on_bt, 10)
        # AMCL 的 /amcl_pose 是 transient_local，只在机器人移动后更新；用 latched 订阅才能一连上就拿到最近一次
        self.create_subscription(PoseWithCovarianceStamped, '/amcl_pose', self.on_amcl, latched)
        # P2：代价地图（costmap_2d 用 transient_local 发布）、路径、速度指令、粒子、点云、所有节点的日志
        self.create_subscription(OccupancyGrid, '/global_costmap/costmap', lambda m: self.on_costmap(m, T_GCOST), latched)
        self.create_subscription(OccupancyGrid, '/local_costmap/costmap', lambda m: self.on_costmap(m, T_LCOST), latched)
        self.create_subscription(Path, '/plan', self.on_plan, 1)
        self.create_subscription(Twist, '/cmd_vel', self.on_cmd, 10)
        self.create_subscription(ParticleCloud, '/particle_cloud', self.on_particles, qos_profile_sensor_data)
        self.create_subscription(PointCloud2, '/cloud_registered_body', self.on_cloud, qos_profile_sensor_data)
        self.create_subscription(Log, '/rosout', self.on_rosout, 100)
        # 所有 navigate_to_pose 目标的反馈（RViz、patrol 发的也有），任务卡片显示外部目标的进度用
        self.create_subscription(NavigateToPose.Impl.FeedbackMessage, '/navigate_to_pose/_action/feedback', self.on_any_feedback, 10)

        self.cmd_pub = self.create_publisher(Twist, '/cmd_vel', 10)
        self.loadmap_cli = self.create_client(LoadMap, '/map_server/load_map')
        # 禁行层（v1.1.8）：navigation.launch.py 里的第二个 map_server，专门发 KeepoutFilter 的掩膜。
        # 旧导航镜像没有它，服务一直不可用 → 网页照样能画、能存，只是提示要更新导航
        self.maskload_cli = self.create_client(LoadMap, '/filter_mask_server/load_map')
        self.mapparam_cli = self.create_client(GetParameters, '/map_server/get_parameters')
        self.taught_param_cli = self.create_client(SetParameters, '/taught_route/set_parameters')
        # 续建：从基准 posegraph 恢复。服务名照核实过的上游源码（slam_toolbox_common.cpp），
        # 注意 save_map.sh 里用的是 /slam_toolbox/serialize_map，两者同一套命名
        self.deser_cli = (self.create_client(DeserializePoseGraph, '/slam_toolbox/deserialize_map')
                          if DeserializePoseGraph is not None else None)
        self.scan_gate_set = self.create_client(SetParameters, '/go2w_mapping_scan_gate/set_parameters')
        self.scan_gate_get = self.create_client(GetParameters, '/go2w_mapping_scan_gate/get_parameters')
        # 建图自动保存：直接调 serialize_map（不起 ros2 命令行进程，30 s 一次也不费事）
        self.ser_cli = (self.create_client(SerializePoseGraph, '/slam_toolbox/serialize_map')
                        if SerializePoseGraph is not None else None)

        self.initpose_pub = self.create_publisher(PoseWithCovarianceStamped, '/initialpose', 10)
        self.nav = ActionClient(self, NavigateToPose, 'navigate_to_pose')
        # 11-24 多点导航：任务「连续通过」把一段站点一次发给 Nav2，规划一条连续路径，中间站不停
        self.nav_through = ActionClient(self, NavigateThroughPoses, 'navigate_through_poses')
        self.cancel_through_cli = self.create_client(CancelGoal, '/navigate_through_poses/_action/cancel_goal')
        self.planner = ActionClient(self, ComputePathToPose, 'compute_path_to_pose')
        self.cancel_cli = self.create_client(CancelGoal, '/navigate_to_pose/_action/cancel_goal')
        self.arm_cli = self.create_client(SetBool, f'{BRIDGE}/arm')
        self.pget_cli = self.create_client(GetParameters, f'{BRIDGE}/get_parameters')
        self.pset_cli = self.create_client(SetParameters, f'{BRIDGE}/set_parameters')
        # 出发前清代价地图：MID360 往下只看到约 7°，机器人身边贴地的一圈是盲区，人站过留下的障碍标记清不掉，
        # 起点落在障碍里 Nav2 就报「Either of the start or goal pose are an obstacle」（11-06 实测）
        # 11-09：clear_entirely 会把整张代价地图（含静态地图层）重置成「未知」，等下一轮更新才填回；
        # Theta* 不许走未知区后，清完 1.5 s 规划常报起点/终点是障碍。改用 clear_except（reset_distance 0 =
        # 全部清掉）：只清雷达障碍层（clearable_layers 默认含 voxel_layer），静态地图不动
        self.clear_clis = [self.create_client(ClearCostmapExceptRegion, f'/{c}/clear_except_{c}')
                           for c in ('global_costmap', 'local_costmap')]

    def event(self, level, msg):
        """记日志并推给网页。不会抛异常：停车等安全动作不能因为打日志失败而中断。

        rclpy 的同一处调用（同一行代码）不能先后用不同级别打日志，否则抛
        ValueError('Logger severity cannot be changed between calls.')，所以三个级别各占一行。
        """
        self.events.append((level, 'go2w_web', msg))
        try:
            if level == 'ERROR':
                self.get_logger().error(msg)
            elif level == 'WARN':
                self.get_logger().warn(msg)
            else:
                self.get_logger().info(msg)
        except Exception:
            pass

    # ---------- 订阅回调（rclpy 线程） ----------
    def on_map(self, m):
        info = m.info
        grid = np.asarray(m.data, dtype=np.int8).astype(np.int16)
        grid[grid < 0] = 255
        meta = {'w': info.width, 'h': info.height, 'res': info.resolution,
                'ox': info.origin.position.x, 'oy': info.origin.position.y,
                'oyaw': yaw_of(info.origin.orientation), 'frame': m.header.frame_id}
        self.map_blob = pack(T_MAP, meta, zlib.compress(grid.astype(np.uint8).tobytes(), 6))
        self.map_file = None
        occ = (grid >= 65) & (grid != 255)
        occ = occ.reshape(info.height, info.width)
        near = occ.copy()
        for _ in range(max(1, round(MATCH_TOL / info.resolution))):   # 4 邻域膨胀
            n2 = near.copy()
            n2[1:, :] |= near[:-1, :]; n2[:-1, :] |= near[1:, :]; n2[:, 1:] |= near[:, :-1]; n2[:, :-1] |= near[:, 1:]
            near = n2
        self.near_occ, self.map_info = near, (info.resolution, info.origin.position.x, info.origin.position.y, info.width, info.height)
        self.observe_taught_map(m, near)
        if abs(yaw_of(info.origin.orientation)) < 1e-6:   # 自动定位只支持不旋转的地图原点（slam_toolbox / map_server 都是）
            self.map_raw = (np.asarray(m.data, dtype=np.int8).reshape(info.height, info.width),
                            info.resolution, info.origin.position.x, info.origin.position.y)
        else:
            self.map_raw = None
        self.map_crc = zlib.crc32(np.asarray(m.data, dtype=np.int8).tobytes())   # 自动保存用来判断地图变没变
        self.map_ver += 1
        if m.header.frame_id == 'map':
            self.map_sample = (self.map_ver, self.map_raw)
        self.get_logger().info(f'map {info.width}x{info.height} @ {info.resolution:.3f} m')

    def show_file_map(self, name, m):
        """没有 map_server / slam_toolbox 时，直接显示 data/maps 里的地图（mapfile.load 的结果）。
        只给网页看、选点编辑路线；定位相关的东西（匹配率、自动定位用的栅格）只认 /map。"""
        grid = m['grid'].astype(np.int16)
        grid[grid < 0] = 255
        h, w = grid.shape
        meta = {'w': w, 'h': h, 'res': m['res'], 'ox': m['ox'], 'oy': m['oy'], 'oyaw': m['oyaw'], 'frame': 'map'}
        self.map_blob = pack(T_MAP, meta, zlib.compress(grid.astype(np.uint8).tobytes(), 6))
        self.map_file = name
        self.map_ver += 1
        self.get_logger().info(f'map {name} {w}x{h} @ {m["res"]:.3f} m（文件，导航没开）')

    def on_scan(self, m):
        self.rates['/scan'].tick()
        self.keep_scan_base(m)
        self.observe_taught_scan(m)
        tfm = self.lookup(m.header.frame_id)
        if tfm is None:
            return
        tx, ty, th = tfm
        r = np.asarray(m.ranges, dtype=np.float32)
        a = m.angle_min + np.arange(r.size, dtype=np.float32) * m.angle_increment
        ok = np.isfinite(r) & (r >= m.range_min) & (r <= m.range_max)
        r, a = r[ok], a[ok] + th
        xy = np.empty((r.size, 2), dtype=np.float32)
        xy[:, 0] = tx + r * np.cos(a)
        xy[:, 1] = ty + r * np.sin(a)
        self.scan_blob = pack(T_SCAN, {'n': int(r.size), 'frame': self.fixed}, xy.tobytes())
        self.scan_ver += 1
        # 匹配率：只在 map 坐标系下有意义（退回 odom 时地图和 scan 本来就不对齐）
        if self.fixed == 'map' and self.near_occ is not None and r.size >= 20 and time.monotonic() >= self.match_hold:
            res, ox, oy, w, h = self.map_info
            gx = np.floor((xy[:, 0] - ox) / res).astype(int)
            gy = np.floor((xy[:, 1] - oy) / res).astype(int)
            inside = (gx >= 0) & (gy >= 0) & (gx < w) & (gy < h)
            if inside.sum() >= 20:
                pct = 100.0 * self.near_occ[gy[inside], gx[inside]].mean()
                self.match = pct if self.match is None else 0.8 * self.match + 0.2 * pct

    def keep_scan_base(self, m):
        """自动定位用：把 scan 转到 base_footprint 存下来（和地图坐标系无关，定位错了也能用）。"""
        if m.header.frame_id in ('', BASE_FRAME):
            tx = ty = th = 0.0
        else:
            try:
                tr = self.tf.lookup_transform(BASE_FRAME, m.header.frame_id, rclpy.time.Time()).transform
            except Exception:
                return
            tx, ty, th = tr.translation.x, tr.translation.y, yaw_of(tr.rotation)
        r = np.asarray(m.ranges, dtype=np.float32)
        a = m.angle_min + np.arange(r.size, dtype=np.float32) * m.angle_increment + th
        ok = np.isfinite(r) & (r >= max(m.range_min, 0.45)) & (r <= min(m.range_max, 20.0))
        r, a = r[ok], a[ok]
        # 只叠加静止时的帧：原地转动时几帧叠在一起会糊掉（11-10 用 bag 测到过）
        v = self.odom or (0.0, 0.0, 0.0)
        still = math.hypot(v[0], v[1]) < AUTOLOC_STILL[0] and abs(v[2]) < AUTOLOC_STILL[1]
        now = time.monotonic()
        pts = np.stack([tx + r * np.cos(a), ty + r * np.sin(a)], 1)
        self.scans_base.append((now, pts, still))
        odom = self.odom_pose
        trusted = (self.healthy and now - self.healthy_t < STALE
                   and odom is not None and now - odom[3] < STALE)
        stamp = m.header.stamp
        self.scan_evidence.append(((stamp.sec, stamp.nanosec), now, pts, still and trusted, odom))

    def on_slam_pose(self, msg):
        if msg.header.frame_id != 'map':
            return
        stamp, pose = msg.header.stamp, msg.pose.pose
        self.slam_poses.append(((stamp.sec, stamp.nanosec), time.monotonic(),
                                (pose.position.x, pose.position.y, yaw_of(pose.orientation))))

    def on_amcl(self, m):
        c = m.pose.covariance
        self.amcl = (math.sqrt(max(0.0, (c[0] + c[7]) / 2)), math.degrees(math.sqrt(max(0.0, c[35]))), time.monotonic())

    # ---------- P2 图层 ----------
    def place(self, m):
        """栅格原点在 fixed 坐标系下的 (x, y, yaw)。局部代价地图在 odom 下，要经 TF 转到 map。"""
        o = m.info.origin
        f = (0.0, 0.0, 0.0) if m.header.frame_id in ('', self.fixed) else self.lookup(m.header.frame_id)
        if f is None:
            return None
        tx, ty, th = f
        c, s = math.cos(th), math.sin(th)
        return (tx + c*o.position.x - s*o.position.y, ty + s*o.position.x + c*o.position.y, th + yaw_of(o.orientation))

    def on_costmap(self, m, kind):
        raw = np.asarray(m.data, dtype=np.int8).tobytes()
        # 全局代价地图大多数时候不变（1 Hz 发布），内容相同就不重发；局部的随机器人移动，每次都发
        if kind == T_GCOST and self.grid_raw.get(kind) == raw:
            return
        place = self.place(m)
        if place is None:
            return
        self.blobs[kind], _ = grid_blob(kind, m, place)
        self.grid_raw[kind] = raw
        self.vers[kind] += 1

    def on_plan(self, m):
        pts = [(p.pose.position.x, p.pose.position.y) for p in m.poses]
        frame = m.header.frame_id or 'map'
        if pts and frame != self.fixed:
            f = self.lookup(frame)
            if f is None:
                return
            c, s = math.cos(f[2]), math.sin(f[2])
            pts = [(f[0] + c*x - s*y, f[1] + s*x + c*y) for x, y in pts]
        step = max(1, math.ceil(len(pts) / PLAN_MAX))
        keep = pts[::step] + ([pts[-1]] if pts and (len(pts) - 1) % step else [])
        self.plan = [[round(x, 3), round(y, 3)] for x, y in keep]
        self.plan_ver += 1

    def on_cmd(self, m):
        self.cmd = (m.linear.x, m.linear.y, m.angular.z, time.monotonic())

    def on_particles(self, m):
        now = time.monotonic()
        if not self.wants('particles') or now - self.part_t < PARTICLES_DT or self.fixed != 'map':
            return
        self.part_t = now
        ps = m.particles
        step = max(1, math.ceil(len(ps) / PARTICLES_MAX))
        arr = np.array([(p.pose.position.x, p.pose.position.y, yaw_of(p.pose.orientation)) for p in ps[::step]],
                       dtype=np.float32).reshape(-1, 3)
        self.blobs[T_PART] = pack(T_PART, {'n': len(arr), 'total': len(ps)}, arr.tobytes())
        self.vers[T_PART] += 1

    def on_cloud(self, m):
        """FAST-LIO 点云 → 机器人坐标系按离地高度过滤 → 5 cm 俯视体素（每格留最高点）→ fixed 坐标系 xy + 高度档。"""
        now = time.monotonic()
        if not self.wants('cloud') or now - self.cloud_t < CLOUD_DT:
            return
        self.cloud_t = now
        try:
            t = self.tf.lookup_transform(BASE_FRAME, m.header.frame_id, rclpy.time.Time()).transform
        except Exception:
            return
        pose = self.lookup(BASE_FRAME)
        off = {f.name: f.offset for f in m.fields}
        if pose is None or not all(k in off for k in 'xyz'):
            return
        dt = np.dtype({'names': ['x', 'y', 'z'], 'formats': ['<f4'] * 3,
                       'offsets': [off['x'], off['y'], off['z']], 'itemsize': m.point_step})
        a = np.frombuffer(bytes(m.data), dtype=dt, count=m.width * m.height)
        P = np.stack([a['x'], a['y'], a['z']], 1).astype(np.float32)
        P = P[np.isfinite(P).all(1)]
        tr = t.translation
        P = P @ rot_matrix(t.rotation).T + np.array([tr.x, tr.y, tr.z], dtype=np.float32)
        z = P[:, 2]
        P = P[(z >= CLOUD_Z[0]) & (z <= CLOUD_Z[1]) & (P[:, 0]**2 + P[:, 1]**2 <= CLOUD_RANGE**2)]
        if len(P):
            P = P[np.argsort(-P[:, 2])]
            k = np.floor(P[:, :2] / CLOUD_VOXEL).astype(np.int64)
            _, idx = np.unique(k[:, 0] * 1000003 + k[:, 1], return_index=True)
            P = P[idx]
            if len(P) > CLOUD_MAX:
                P = P[np.random.choice(len(P), CLOUD_MAX, replace=False)]
        x0, y0, th = pose
        c, s = math.cos(th), math.sin(th)
        xy = np.empty((len(P), 2), dtype=np.float32)
        xy[:, 0] = x0 + c*P[:, 0] - s*P[:, 1]
        xy[:, 1] = y0 + s*P[:, 0] + c*P[:, 1]
        band = np.clip((P[:, 2] - CLOUD_Z[0]) / (CLOUD_Z[1] - CLOUD_Z[0]) * 6, 0, 5).astype(np.uint8)
        self.blobs[T_CLOUD] = pack(T_CLOUD, {'n': len(xy), 'z': CLOUD_Z}, xy.tobytes() + band.tobytes())
        self.vers[T_CLOUD] += 1

    def on_rosout(self, m):
        level = m.level[0] if isinstance(m.level, (bytes, bytearray)) else m.level   # Log.level 是 byte 类型
        lv = ROS_LEVEL.get(level)
        if lv is None or m.name == 'go2w_web':   # DEBUG 不转；本节点的事件已经单独推送
            return
        now = time.monotonic()
        if now - self.rosout_t >= 1.0:
            if self.rosout_dropped:
                self.events.append(('WARN', 'go2w_web', f'/rosout 太多，过去 1 s 少转发了 {self.rosout_dropped} 条'))
            self.rosout_budget, self.rosout_t, self.rosout_dropped = ROSOUT_PER_S, now, 0
        if self.rosout_budget <= 0:
            self.rosout_dropped += 1
            return
        self.rosout_budget -= 1
        self.events.append((lv, m.name, m.msg))

    # ---------- P3 ----------
    def on_any_feedback(self, msg):
        fb = msg.feedback
        self.fb_any[bytes(msg.goal_id.uuid)] = (fb.distance_remaining, secs(fb.navigation_time), fb.number_of_recoveries,
                                                secs(fb.estimated_time_remaining), time.monotonic())

    def run_ros_ops(self):
        while self.ros_ops:
            self.ros_ops.popleft()()

    def inspect_on(self, topic):
        """开始查看某个 topic：按它的类型动态订阅（在 rclpy 线程里建，避免和执行器抢）。"""
        types = dict(self.get_topic_names_and_types())
        if topic not in types or not types[topic]:
            return False
        cls = get_message(types[topic][0])
        rec = {'sub': None, 'text': None, 't': 0.0, 'ver': 0, 'rate': Rate(), 'type': types[topic][0]}
        self.inspect[topic] = rec

        def create():
            if self.inspect.get(topic) is rec:
                rec['sub'] = self.create_subscription(cls, topic, lambda m: self.on_inspect(rec, m), qos_profile_sensor_data)
        self.ros_ops.append(create)
        return True

    def inspect_off(self, topic):
        rec = self.inspect.pop(topic, None)
        if rec:
            self.ros_ops.append(lambda: rec['sub'] is not None and self.destroy_subscription(rec['sub']))

    def on_inspect(self, rec, m):
        rec['rate'].tick()
        now = time.monotonic()
        if now - rec['t'] < INSPECT_DT:
            return
        rec['t'] = now
        try:
            text = message_to_yaml(m, truncate_length=12)
        except Exception as e:
            text = f'（这条消息转不成文字：{e!r}）'
        rec['text'] = text if len(text) < 8000 else text[:8000] + '\n…（太长，已截断）'
        rec['ver'] += 1

    def publish_cmd(self, v, vy, w):
        t = Twist()
        t.linear.x, t.linear.y, t.angular.z = float(v), float(vy), float(w)
        self.cmd_pub.publish(t)

    def load_map(self, name):
        req = LoadMap.Request()
        req.map_url = os.path.join(DATA, 'maps', f'{name}.yaml')
        return self.loadmap_cli.call_async(req)

    def load_mask(self, path):
        req = LoadMap.Request()
        req.map_url = path
        return self.maskload_cli.call_async(req)

    # ---------- 续建：从基准 posegraph 恢复 ----------
    def serialize_posegraph(self, path):
        """slam_toolbox 把当前位姿图写成 <path>.posegraph / <path>.data（path 不带扩展名）。"""
        if self.ser_cli is None or not self.ser_cli.service_is_ready():
            raise RuntimeError('slam_toolbox 的 serialize_map 服务不可用')
        req = SerializePoseGraph.Request()
        req.filename = path
        return self.ser_cli.call_async(req)

    def deserialize_posegraph(self, name, pose):
        """slam_toolbox 的 deserialize_map 服务，START_AT_GIVEN_POSE。

        为什么不信返回值：Humble 上游 deserializePoseGraphCallback 在文件读失败时也是
        `return true`，而且 DeserializePoseGraph.srv 的 response 段是空的（没有 result）。
        所以这里只负责把请求发出去，图到底加载没有交给 map_overlap_check.py 量。
        """
        if self.deser_cli is None or not self.deser_cli.service_is_ready():
            raise RuntimeError('slam_toolbox 的 deserialize_map 服务不可用（建图没在跑？）')
        req = DeserializePoseGraph.Request()
        req.filename = os.path.join(DATA, 'maps', name)
        # srv 里的常量在 rosidl 生成的 Request 类上，服务类本身只有 Request / Response（11-17 真机报 AttributeError）
        req.match_type = getattr(DeserializePoseGraph.Request, 'START_AT_GIVEN_POSE', 2)
        req.initial_pose = Pose2D(x=float(pose[0]), y=float(pose[1]), theta=float(pose[2]))
        return self.deser_cli.call_async(req)

    def fetch_map_current(self):
        if not self.mapparam_cli.service_is_ready():
            self.map_current = self.map_loaded = None
            return
        req = GetParameters.Request()
        req.names = ['yaml_filename']

        def done(fut):
            try:
                v = fut.result().values[0].string_value
                param = os.path.basename(v)[:-5] if v.endswith('.yaml') else (v or None)
                self.map_current = self.match_map(param)
            except Exception:
                pass
        self.mapparam_cli.call_async(req).add_done_callback(done)

    def match_map(self, param):
        """现在用的是哪张地图。map_server 的 yaml_filename 参数只反映启动时的地图：
        用 load_map 服务（网页「切换地图」）换图后它不会变（11-06 实测），所以以实际发出的 /map 为准，
        和 data/maps 里的文件比尺寸、原点、分辨率。内容一样分不出来时，优先网页加载的那张，再是参数。"""
        info = self.map_info
        if not info:
            return param
        res, ox, oy, w, h = info
        hits = [m['name'] for m in P.list_maps(os.path.join(DATA, 'maps'))
                if m['w'] == w and m['h'] == h and m['ox'] is not None
                and abs(m['res'] - res) < 1e-6 and abs(m['ox'] - ox) < 1e-3 and abs(m['oy'] - oy) < 1e-3]
        for name in (self.map_loaded, param):
            if name and name in hits:
                return name
        return hits[0] if len(hits) == 1 else param

    def on_odom(self, m):
        self.observe_taught_odom(m)
        self.rates['/odom'].tick()
        t = m.twist.twist
        self.odom = (t.linear.x, t.linear.y, t.angular.z)
        p, st = m.pose.pose, m.header.stamp
        if self.odom_wild is None:
            self.odom_wild = self.odom_sanity(t, p)
        self.odom_pose = (p.position.x, p.position.y, yaw_of(p.orientation), time.monotonic())
        self.speed.add(time.monotonic(), st.sec + st.nanosec * 1e-9, p.position.x, p.position.y, yaw_of(p.orientation))

    def odom_sanity(self, t, p):
        """里程计数值离谱（摔倒后 FAST-LIO 发散的样子）：返回 (原因, monotonic, time)，正常返回 None。"""
        now, why = time.monotonic(), None
        vals = (t.linear.x, t.linear.y, t.angular.z, p.position.x, p.position.y)
        last, self._odom_last = getattr(self, '_odom_last', None), (p.position.x, p.position.y, now)
        if not all(math.isfinite(v) for v in vals):
            why = '里程计输出了无效数值'
        elif math.hypot(t.linear.x, t.linear.y) > ODOM_WILD_SPEED:
            why = f'里程计速度 {math.hypot(t.linear.x, t.linear.y):.1f} m/s（狗最快约 1.2 m/s）'
        elif abs(t.angular.z) > ODOM_WILD_TURN:
            why = f'里程计转速 {t.angular.z:.1f} rad/s'
        elif last and now - last[2] < 0.5:
            jump = math.hypot(p.position.x - last[0], p.position.y - last[1])
            if jump > ODOM_WILD_JUMP:
                why = f'里程计位置 {now - last[2]:.2f} s 内跳了 {jump:.1f} m'
        return (why, now, time.time()) if why else None

    def on_healthy(self, m):
        self.rates['/odom_healthy'].tick()
        self.healthy, self.healthy_t = m.data, time.monotonic()

    def poll_tick(self):
        # 上一个临时订阅还在等第一帧（DDS 发现可能要几百 ms）就别重建，等满 BAT_PERIOD 还没收到才重订
        due = time.monotonic() - self.bat_poll_t >= BAT_PERIOD - 0.01
        if due or (self.motor_fast and self.bat_sub is None):
            self.poll_battery()

    def poll_battery(self):
        self.bat_poll_t = time.monotonic()
        if self.bat_sub is not None:
            self.destroy_subscription(self.bat_sub)   # 上一轮没收到（话题不在），这轮重订
            self.bat_sub = None
        try:
            self.bat_sub = self.create_subscription(get_message('unitree_go/msg/LowState'), BAT_TOPIC,
                                                    self.on_lowstate_raw, qos_profile_sensor_data, raw=True)
        except Exception as e:
            self.get_logger().warn(f'订阅 {BAT_TOPIC} 失败，网页不显示电量和电机温度：{e}', once=True)

    def on_lowstate_raw(self, data):
        if len(data) > BAT_SOC_OFFSET and data[1] == 1:   # 只认小端 CDR（00 01 ..）
            soc = data[BAT_SOC_OFFSET]
            if 0 < soc <= 100:   # 0 = BMS 还没报
                self.battery, self.battery_t = soc, time.monotonic()
            end = MOTOR_TEMP_OFFSET + MOTOR_STRIDE * (MOTOR_N - 1)
            if len(data) > end:
                t = [((data[MOTOR_TEMP_OFFSET + MOTOR_STRIDE*i] + 128) & 0xFF) - 128 for i in range(MOTOR_N)]   # int8
                if any(t) and all(-30 <= v <= 150 for v in t):   # 全 0 = 电机还没上报
                    self.motor_temp, self.motor_temp_t = t, time.monotonic()
                    self.motor_info = self._parse_motor_info(data, self.motor_temp_t)
        if self.bat_sub is not None:
            self.destroy_subscription(self.bat_sub)
            self.bat_sub = None

    def _parse_motor_info(self, data, now):
        if len(data) < MOTOR_ERR_OFFSET + 4 + MOTOR_STRIDE * (MOTOR_N - 1):
            return None
        tau = [struct.unpack_from('<f', data, MOTOR_TAU_OFFSET + MOTOR_STRIDE * i)[0] for i in range(MOTOR_N)]
        return {'err': [struct.unpack_from('<I', data, MOTOR_ERR_OFFSET + MOTOR_STRIDE * i)[0] for i in range(MOTOR_N)],
                'tau': [round(v, 2) if math.isfinite(v) else None for v in tau]}

    def on_armed(self, m):
        self.rates['/go2w_cmd_bridge/armed'].tick()
        self.armed, self.armed_t = m.data, time.monotonic()

    def on_status(self, m):
        with self.lock:
            self.statuses = {bytes(s.goal_info.goal_id.uuid): s.status for s in m.status_list}

    def on_status_through(self, m):
        with self.lock:
            self.statuses_through = {bytes(s.goal_info.goal_id.uuid): s.status for s in m.status_list}

    def on_bt(self, m):
        with self.lock:
            for e in m.event_log:
                if e.current_status == 'RUNNING':
                    self.bt_running.add(e.node_name)
                else:
                    self.bt_running.discard(e.node_name)

    def on_feedback(self, msg):
        fb = msg.feedback
        with self.lock:
            g = self.goal
            if g is None or bytes(msg.goal_id.uuid) != g['id']:
                return
            g['remaining'] = fb.distance_remaining
            g['total'] = max(g['total'] or 0.0, fb.distance_remaining)
            g['nav_time'] = secs(fb.navigation_time)
            g['eta'] = secs(fb.estimated_time_remaining)
            g['recoveries'] = fb.number_of_recoveries
            left = getattr(fb, 'number_of_poses_remaining', None)   # 多点导航才有：还没经过的点数（含终点）
            if left is not None:
                g['poses_left'] = int(left)
            if g.get('first_fb') is None and fb.distance_remaining > 0:
                g['first_fb'] = time.monotonic()   # 第一次有路径长度 = 规划出来了（记规划用时）

    def on_result(self, gid, fut):
        try:
            status = fut.result().status
        except Exception as e:  # 结果拿不到也要结束这个任务
            status, err = GoalStatus.STATUS_ABORTED, e
        else:
            err = None
        res = RESULT.get(status, 'aborted')
        with self.lock:
            g = self.goal
            if g is None or g['id'] != gid or g.get('passed'):   # passed：路线中间站已被下一站顶替，旧目标的 abort 不算失败
                return
            g['status'], g['end'] = res, time.monotonic()
            if res == 'succeeded':
                g['remaining'] = 0.0
        p = g['pose']
        if res == 'succeeded':
            self.event('INFO', f"到达目标 ({p[0]:.2f}, {p[1]:.2f})，用时 {g['nav_time'] or 0:.1f} s")
        elif res == 'canceled':
            self.event('INFO', '导航目标已取消')
        else:
            self.event('ERROR', f'导航失败（Nav2 放弃了这个目标）{": " + repr(err) if err else ""}')
            cb = self.on_goal_fail   # 异常记录（rclpy 线程 → Server 自己切回 asyncio）
            if cb and g.get('kind') != 'route':   # 巡逻任务的站点失败由 run_route 判断（可能在容差内算到达）
                cb(p)

    def on_params(self, fut):
        try:
            vals = fut.result().values
        except Exception:
            return
        for key, v in zip(PARAMS, vals):
            if v.type == ParameterType.PARAMETER_DOUBLE:
                self.params[key] = v.double_value
            elif v.type == ParameterType.PARAMETER_INTEGER:
                self.params[key] = float(v.integer_value)

    # ---------- 指令（asyncio 线程调用；返回 rclpy future） ----------
    def publish_initial_pose(self, x, y, th, fine=False):
        m = PoseWithCovarianceStamped()
        m.header.frame_id = 'map'
        m.header.stamp = self.get_clock().now().to_msg()
        # 匹配率从头算：旧值是按旧位姿平滑出来的（比如导航刚起来时 AMCL 默认位姿下的 7%），
        # 留着会让「自动定位成功」后立刻出发时误报「匹配率低」。等 AMCL 用上新位姿再算。
        self.match = None
        self.match_hold = time.monotonic() + MATCH_HOLD_S
        # 后续示教仅接受本次修正之后的测量，延迟送达的旧scan/odom不能刷新定位自检。
        with self._taught_lock:
            self._taught_pose_stamp = secs(m.header.stamp)
            self._taught_scan = None
        m.pose.pose.position.x, m.pose.pose.position.y = float(x), float(y)
        m.pose.pose.orientation.z, m.pose.pose.orientation.w = math.sin(th / 2), math.cos(th / 2)
        cov = [0.0] * 36
        cov[0] = cov[7] = FINE_COV_XY if fine else INIT_COV_XY
        cov[35] = FINE_COV_YAW if fine else INIT_COV_YAW
        m.pose.covariance = cov
        self.initpose_pub.publish(m)

    def stamped_pose(self, x, y, th):
        p = PoseStamped()
        p.header.frame_id = 'map'
        p.header.stamp = self.get_clock().now().to_msg()
        p.pose.position.x, p.pose.position.y = float(x), float(y)
        p.pose.orientation.z, p.pose.orientation.w = math.sin(th / 2), math.cos(th / 2)
        return p

    def send_through(self, poses, behavior_tree=''):
        g = NavigateThroughPoses.Goal()
        g.poses = [self.stamped_pose(x, y, th) for x, y, th in poses]
        g.behavior_tree = behavior_tree
        return self.nav_through.send_goal_async(g, feedback_callback=self.on_feedback)

    def send_goal(self, x, y, th):
        g = NavigateToPose.Goal()
        g.pose = self.stamped_pose(x, y, th)
        return self.nav.send_goal_async(g, feedback_callback=self.on_feedback)

    def compute_path(self, start, goal):
        g = ComputePathToPose.Goal()
        g.start = self.stamped_pose(start['x'], start['y'], start['th'])
        g.goal = self.stamped_pose(goal['x'], goal['y'], goal['th'])
        g.use_start = True
        g.planner_id = 'GridBased'
        return self.planner.send_goal_async(g)

    def track_goal(self, handle, pose, kind='goal'):
        gid = bytes(handle.goal_id.uuid)
        with self.lock:
            self.goal = {'id': gid, 'pose': pose, 'kind': kind, 't0': time.monotonic(), 'status': 'active', 'end': None,
                         'remaining': None, 'total': None, 'nav_time': None, 'eta': None, 'recoveries': 0,
                         'poses_left': None, 'first_fb': None}
        result = handle.get_result_async()
        result.add_done_callback(lambda f: self.on_result(gid, f))
        return result

    def cancel_all(self):
        """取消所有 navigate_to_pose 目标（零 uuid + 零时间戳 = 全部），包括 RViz、patrol 发的。"""
        if self.cancel_through_cli.service_is_ready():   # 多点导航的目标也一起取消（不等结果）
            self.cancel_through_cli.call_async(CancelGoal.Request())
        return self.cancel_cli.call_async(CancelGoal.Request())

    def clear_costmaps(self):
        """清空全局、局部代价地图（障碍层清空后按当前点云重新标记），返回各个服务的 future。"""
        return [c.call_async(ClearCostmapExceptRegion.Request(reset_distance=0.0))
                for c in self.clear_clis if c.service_is_ready()]

    def call_arm(self, on):
        req = SetBool.Request()
        req.data = bool(on)
        return self.arm_cli.call_async(req)

    def fetch_params(self):
        if not self.pget_cli.service_is_ready():
            return
        req = GetParameters.Request()
        req.names = list(PARAMS.values())
        self.pget_cli.call_async(req).add_done_callback(self.on_params)

    def set_param(self, key, value):
        p = Parameter()
        p.name = PARAMS[key]
        p.value = ParameterValue(type=ParameterType.PARAMETER_DOUBLE, double_value=float(value))
        req = SetParameters.Request()
        req.parameters = [p]
        return self.pset_cli.call_async(req)

    # ---------- 查询（asyncio 线程调用，只读） ----------
    def body_tilt(self):
        """机身总倾角（度，= 重力和机身 z 轴的夹角，和运控姿态保护同一个量）。planar_odom 把机器人 IMU 的
        俯仰 / 横滚发成 base_footprint → base_link（10 Hz）。返回 (时间戳 ns, 倾角°)，没有返回 None。
        新不新由调用方看时间戳变没变：这个时间戳和本机时钟不一定对得上（11-20 /odom 时间戳走速 0.2–5 倍）。"""
        try:
            t = self.tf.lookup_transform(BASE_FRAME, 'base_link', rclpy.time.Time())
            r, p = roll_pitch(t.transform.rotation)
            st = t.header.stamp
            return (st.sec * 1_000_000_000 + st.nanosec,
                    math.degrees(math.acos(max(-1.0, min(1.0, math.cos(r) * math.cos(p))))))
        except Exception:
            return None

    def lookup(self, frame):
        """frame 在 fixed 坐标系下的 (x, y, yaw)。没有 map 时退回 odom（例如只跑了里程计）。"""
        for fixed in ('map', 'odom'):
            try:
                t = self.tf.lookup_transform(fixed, frame, rclpy.time.Time())
            except Exception:
                continue
            self.fixed = fixed
            tr = t.transform
            return tr.translation.x, tr.translation.y, yaw_of(tr.rotation)
        return None

    def pose(self):
        p = self.lookup(BASE_FRAME)
        v = self.odom or (0.0, 0.0, 0.0)
        if p is None:
            return {'t': 'pose', 'valid': False}
        sc = self.speed.result()
        # 走起来（> 0.1 m/s）且时间戳走速偏离真实时间 5% 以上时，最多每 10 s 报一次
        if sc and abs(sc['wall'][0]) > 0.1 and abs(sc['rate'] - 1) > 0.05 and time.monotonic() - self.rate_warned > 10:
            self.rate_warned = time.monotonic()
            self.get_logger().warn(f"/odom 时间戳走速是真实时间的 {sc['rate']:.2f} 倍："
                                   f"twist 速度 {v[0]:.2f} m/s，按真实时间算 {sc['wall'][0]:.2f} m/s")
        # 机身姿态：planar_odom 把机器人 IMU 的俯仰 / 横滚发成 base_footprint → base_link
        tilt = None
        try:
            q = self.tf.lookup_transform(BASE_FRAME, 'base_link', rclpy.time.Time()).transform.rotation
            rp = roll_pitch(q)
            tilt = [round(math.degrees(rp[0]), 2), round(math.degrees(rp[1]), 2)]
        except Exception:
            pass
        return {'t': 'pose', 'valid': True, 'frame': self.fixed,
                'x': round(p[0], 4), 'y': round(p[1], 4), 'th': round(p[2], 4),
                'v': round(v[0], 3), 'vy': round(v[1], 3), 'w': round(v[2], 3), 'speed': sc, 'tilt': tilt,
                # 实际下发的速度指令（velocity_smoother 之后）；0.5 s 没有新指令按零算
                'cmd': [round(c, 3) for c in self.cmd[:3]] if time.monotonic() - self.cmd[3] < 0.5 else [0.0, 0.0, 0.0]}

    def loc_quality(self):
        # 机器人不动时 AMCL 不更新 /amcl_pose，协方差也不会变，所以一直用最近一次的值
        a = self.amcl
        return {'std': round(a[0], 4) if a else None, 'yaw': round(a[1], 2) if a else None,
                'match': round(self.match, 1) if self.match is not None and self.fixed == 'map' else None}

    def is_armed(self):
        return bool(self.armed) if time.monotonic() - self.armed_t < STALE else None

    def nav_active(self):
        with self.lock:
            return (any(s in ACTIVE for s in self.statuses.values()) or any(s in ACTIVE for s in self.statuses_through.values())
                    or bool(self.goal and self.goal['status'] == 'active'))

    def task(self):
        """任务卡片：网页发的目标显示详细进度；别处（RViz、patrol）发的只显示「外部目标」。"""
        with self.lock:
            g = dict(self.goal) if self.goal else None
            running = set(self.bt_running)
            foreign = [k for k, s in [*self.statuses.items(), *self.statuses_through.items()]
                       if s in ACTIVE and (not g or k != g['id'])]
        recovering = any(n in RECOVERY_NODES or n.startswith('Clear') for n in running)
        phase = 'recover' if recovering else 'follow' if 'FollowPath' in running else 'plan'
        if g:
            if g['status'] != 'active' and time.monotonic() - g['end'] > 5.0:
                with self.lock:
                    if self.goal and self.goal['id'] == g['id']:
                        self.goal = None
                g = None
        if g:
            return {'type': 'goal', 'status': g['status'],
                    'phase': phase if g['status'] == 'active' else 'arrived' if g['status'] == 'succeeded' else g['status'],
                    'goal': {'x': g['pose'][0], 'y': g['pose'][1], 'th': g['pose'][2]},
                    'remaining': g['remaining'], 'total': g['total'], 'eta': g['eta'],
                    'elapsed': g['nav_time'] if g['nav_time'] is not None else time.monotonic() - g['t0'],
                    'recoveries': g['recoveries']}
        if foreign:
            # 别处（RViz、命令行的 patrol.py）发的目标没有我们自己的 action 句柄，进度来自 /navigate_to_pose/_action/feedback
            fb = self.fb_any.get(foreign[0])
            fresh = fb and time.monotonic() - fb[4] < 3.0
            return {'type': 'external', 'status': 'active', 'phase': phase,
                    'remaining': fb[0] if fresh else None, 'elapsed': fb[1] if fresh else None,
                    'recoveries': fb[2] if fresh else 0, 'eta': fb[3] if fresh else None, 'total': None}
        return None

    def state(self, sys_stats, procs_status=None, transitions=None):
        now = time.monotonic()
        names = set(self.get_node_names())
        if 'bt_navigator' not in names:
            self.forget_nav()
        nodes = [{'id': n, 'name': label, 'up': n in names} for n, label in self.NODES]
        profiles = self.profiles(names, procs_status, transitions)
        topics = []
        for name, types in sorted(self.get_topic_names_and_types()):
            if name.startswith('/parameter_events'):
                continue
            r = self.rates.get(name)
            topics.append({'name': name, 'type': types[0] if types else '', 'pubs': self.count_publishers(name),
                           'hz': r.hz() if r else None})
        return {
            't': 'state',
            'fixed': self.fixed,
            'odomOk': bool(self.healthy) and now - self.healthy_t < STALE,
            'armed': self.is_armed() if 'go2w_cmd_bridge' in names else None,
            'battery': self.battery if now - self.battery_t < BAT_STALE else None,
            'motorTemp': self.motor_temp if now - self.motor_temp_t < MOTOR_STALE else None,
            'motorInfo': self.motor_info if now - self.motor_temp_t < MOTOR_STALE else None,
            'navReady': self.nav.server_is_ready(),
            'nodes': nodes, 'profiles': profiles, 'topics': topics,
            'hasMap': self.map_blob is not None,
            'params': dict(self.params),
            'loc': self.loc_quality(),
            'mapCurrent': self.map_current,
            'sys': sys_stats,
        }

    def forget_nav(self):
        """Nav2 不在了（停止或崩溃）：不会再有结果回来，清掉残留的任务状态，否则任务卡片和心跳判断会一直以为在导航。"""
        with self.lock:
            self.statuses, self.bt_running = {}, set()
            self.grid_raw = {}   # 重启后代价地图内容可能完全一样，也要重新发给网页
            g = self.goal
            if g and g['status'] == 'active':
                g['status'], g['end'] = 'aborted', time.monotonic()
            else:
                g = None
        if g:
            self.event('ERROR', 'Nav2 已经不在运行，当前导航目标作废')

    def profiles(self, names, procs_status=None, transitions=None):
        """档位状态：进程在 + 关键节点在 = 运行中；进程在、节点还没起来 = 启动中；
        拿不到本地 supervisor 状态时只看节点。"""
        profiles = {}
        for m in P.MODES:
            nodes_up = all(n in names for n in MODE_NODES[m])
            if procs_status is None:
                st = nodes_up and not (m == 'localization' and 'bt_navigator' in names)
            else:
                st = procs_status.get(m, False)
            profiles[m] = ('run' if nodes_up or procs_status is None else 'starting') if st else 'stop'
        # supervisor 启动的巡逻也要识别：取消 / 急停 / 切档位时连它一起停
        profiles['patrol'] = 'run' if 'go2w_patrol' in names or (procs_status or {}).get('patrol') else 'stop'
        profiles['bag'] = 'run' if any(n.startswith('rosbag2_recorder') for n in names) or (procs_status or {}).get('bag') else 'stop'
        # 遥控用的独立速度桥（不开导航时）：进程在 + 节点在才算能用
        profiles['bridge'] = 'run' if (procs_status or {}).get('bridge') and 'go2w_cmd_bridge' in names else 'stop'
        for k, v in (transitions or {}).items():   # 网页刚发起的启停，进程状态还没跟上
            profiles[k] = v
        return profiles


class Server(TaughtWeb, RT.RtabWeb):
    def __init__(self, node, static_dir, heartbeat):
        self.node, self.static_dir, self.heartbeat = node, static_dir, heartbeat
        # 版本号（11-18）：static/version.json，跟着 release 走；推给网页，和页面自己加载的版本比，不一样就提示刷新
        try:
            with open(os.path.join(static_dir, 'version.json'), encoding='utf-8') as f:
                self.version = str(json.load(f).get('version') or '') or None
        except (OSError, ValueError, AttributeError):
            self.version = None
        node.get_logger().info(f'网页版本 {self.version or "未知（没有 version.json）"}')
        self.clients = {}       # ws → {'remote', 'last_ping'}
        self.owner = None       # 控制方：最近一个发过运动相关指令的网页
        self.estop, self.estop_t = False, 0.0
        self.pending = set()
        node.wants = self.wants   # 点云、粒子没人看就不算，省扩展坞的 CPU
        self.camera = C.Camera(pack, T_CAM, node.event)
        self.sys = SysStats()
        rid, src = robot_id()
        node.get_logger().info(f'狗编号 {rid}（来自 {src}），录包名 = {rid}_MMDD_HHMMSS（异常自动保存）/ {rid}_trigger_MMDD_HHMMSS（手动录包、保存刚才 N 秒）')
        # P3：进程管理。本地调试（GO2W_FAKE_PROCS=1）换成假模拟器里的进程
        self.simulated = bool(os.environ.get('GO2W_FAKE_PROCS'))
        if self.simulated:
            from go2w_fake_procs import FakeProcs
            self.procs = FakeProcs(node)
        else:
            self.procs = P.RuntimeProcs()
        # RTAB-Map 影子库（rtab.py）：建图时顺带录，局部重扫用
        self.init_rtab(DATA, self.procs.tools_dir if not self.simulated else DATA, self.simulated)
        self.procs_status = None      # 最近一次 supervisor 检查结果；None = 暂不可用
        self.transitions = {}         # 网页发起、还没完成的启停：档位 → 'starting' / 'stopping'
        self.starting = {}            # 档位 → 开始启动的时间（启动超时 / 失败检测）
        self.bridge_t, self.bridge_started = -1e9, False   # 遥控用独立速度桥：上次启动时间、是否由网页启动过
        self.busy = False             # 同一时间只做一个启停操作
        self.map_choice = None        # 没有定位 / 导航在跑时，网页选好的「下次启动用的地图」
        # 最近用过 / 选过的地图（data/last_map.json）：导航没开时网页显示它，重启后也记得
        self.last_map_file = os.path.join(DATA, 'last_map.json')
        self.last_map = None
        try:
            with open(self.last_map_file, encoding='utf-8') as f:
                v = json.load(f).get('name')
            self.last_map = v if isinstance(v, str) and P.SAFE_NAME.fullmatch(v) else None
        except (OSError, ValueError, AttributeError):
            pass
        self.preview_key = None       # 已经显示的文件地图 (名字, 修改时间)；导航 / 建图在跑时为 None
        self.map_busy = False         # 保存和导出地图期间保护源快照及目标文件
        # 续建（docs/地图续建.md）：一次会话 = 基准图 + 新图名字 + 起点位姿 + 自检结果
        self.cont = None              # {base, name, source, pose, warn, check, started, paused}
        self.last_save_park = ''      # 最近一次存图时顺手记停车点的说明（续建收工的提示要带上）
        self.cont_hold = ()           # cont_start 进行中的 (基准, 新名)：自动保存清理不许碰
        self.cont_busy = False
        self.cont_blocked = False
        # 建图自动保存：name = 这次建图的自动保存名；t = 上次存成功的时间；crc = 那时 /map 的 crc
        self.autosave = {'name': None, 't': 0.0, 'wall': None, 'crc': None, 'dur': None, 'err': None,
                         'running': False, 'problem': False, 'count': 0}
        self.teleop = None            # (vx, vy, wz, 收到时间)；None = 没有在遥控
        self.maps = P.list_maps(os.path.join(DATA, 'maps'))
        self.bags = P.list_bags(os.path.join(DATA, 'bags'))
        self.rollback_file = os.path.join(DATA, 'rollback.json')
        self.rollback_on, self.rollback_t, self.rollback_started, self.rollback_saving = True, 0.0, False, False
        self.stuck = StuckWatch()
        # 异常记录 + 自动录包（incidents.py）。变化时推给所有网页
        self.incidents = INC.IncidentLog(os.path.join(DATA, 'incidents.json'), on_change=self.incident_push)
        if self.incidents.error:
            node.event('ERROR', self.incidents.error)
        self.inc_save = None          # 计划中的异常存包：{'at': 存包时刻, 't0': 第一条异常时刻, 'ids': [...]}
        self.inc_bag_t = {}           # 类型 → 上次为它自动存包的时间
        self.map_frozen = None        # 建图冻结（摔倒 / 里程计异常）：{reason, wild, wall, kept, kept_wall, gate}
        self.fall_prev = None         # 上一轮看到的运控状态（检测意外掉到 Passive）
        self.tilt_hist = deque()      # (monotonic, 机身倾角°)，最近 FALL_TILT_PRE 秒
        self.tilt_stamp = None        # 上一次取到的姿态 TF 时间戳
        self.fall_pending = None      # 掉到 Passive 后等 FALL_TILT_POST 秒再判断：{'t', 'prev'}
        self.lie_done_t = -1e9        # 网页最近一次让狗趴下结束的时间
        self.odom_wild_seen = None
        self.inc_open = {}            # 持续中的异常：'stuck' / 'estop' / 'motor_hot' / 'ctrl_offline' → id
        self.rollback_last = None     # 最近一次存成功的回溯包名
        self.loop = None
        self.quota_warned = False     # 超限又删不动时只报一次警，回到上限以下再复位
        try:
            with open(self.rollback_file) as f:
                self.rollback_on = bool(json.load(f).get('on', True))
        except (OSError, ValueError, AttributeError):
            pass
        self.uploads_file = os.path.join(DATA, 'bag_uploads.json')
        self.uploads = {}             # 包名 → {state: run/done/fail, pct, msg}；done 的存进 bag_uploads.json
        try:
            with open(self.uploads_file) as f:
                self.uploads = {k: {'state': 'done', 'pct': 100, 'msg': v} for k, v in json.load(f).items()}
        except (OSError, ValueError, AttributeError):
            pass
        self.routes_file = os.path.join(DATA, 'routes.json')
        self.routes = {}
        self.routes_error = False
        self.motion_stops = 0
        try:
            with open(self.routes_file, encoding='utf-8') as f:
                self.routes = {r['id']: self.route_record(r, r['id'], strict=False) for r in json.load(f)}
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, KeyError) as e:
            self.routes_error = True
            node.event('ERROR', f'无法读取导航任务 {self.routes_file}：{e}')
        self.route = None
        self.route_job = None
        self.goal_pending = False
        self.resume_file = os.path.join(DATA, 'route_resume.json')
        self.resumes = {}
        try:
            with open(self.resume_file, encoding='utf-8') as f:
                for rid, cp in json.load(f).items():
                    if cp.get('status') == 'active':   # 上次跑着的时候网页服务停了（重启 / 断电）：没机会写结束原因
                        cp.update(status='interrupted', reason='任务执行中网页服务停止（重启或断电）')
                    self.resumes[rid] = cp
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, AttributeError) as e:
            node.event('WARN', f'任务断点文件读不了，忽略：{e}')
        self.prune_resumes(write=True)
        self.views_file = os.path.join(DATA, 'map_views.json')
        self.map_views = {}
        try:
            with open(self.views_file, encoding='utf-8') as f:
                raw = json.load(f)
            self.map_views = {k: self.view_angle(v) for k, v in raw.items() if isinstance(k, str) and P.SAFE_NAME.fullmatch(k)}
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, AttributeError) as e:
            node.event('WARN', f'无法读取地图显示方向 {self.views_file}，全部按 0° 显示：{e}')
        # 地图显示名（11-23）：文件名还是英文（ROS / 云端 / 任务都用它），网页上可以起个中文名
        self.labels_file = os.path.join(DATA, 'map_labels.json')
        self.map_labels = {}
        try:
            with open(self.labels_file, encoding='utf-8') as f:
                raw = json.load(f)
            self.map_labels = {k: v for k, v in raw.items() if isinstance(k, str) and P.SAFE_NAME.fullmatch(k)
                               and isinstance(v, str) and self.label_error(v) is None}
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, AttributeError) as e:
            node.event('WARN', f'无法读取地图显示名 {self.labels_file}，全部显示文件名：{e}')
        # 禁行区（v1.1.8）：每张图的形状（<名字>.keepout.json）推给网页；掩膜缺了 / 旧了先补上，
        # 导航启动时 navigation.launch.py 直接按地图名读 keepout/<名字>.yaml
        maps_dir = os.path.join(DATA, 'maps')
        self.keepouts = KO.load_all(maps_dir, [m['name'] for m in self.maps])
        for x in self.keepouts:
            try:
                KO.ensure_mask(maps_dir, x)
            except (OSError, ValueError) as e:
                node.event('WARN', f'禁行区掩膜 {x} 写不了：{e}')
        self.keepout_live = None      # 导航里现在加载的是哪张图的禁行掩膜（网页确认过的）
        self.keepout_ready = False    # 上一拍 /filter_mask_server/load_map 可用吗（变成可用时补加载一次）
        self.keepout_loading = False
        self.map_loading = False
        self.checking_route = False
        # 自动定位：最近一次的结果推给网页（state.autoLoc）；seq 每次结束 +1，网页据此知道「刚定位成功」
        self.autoloc = {'state': 'idle', 'seq': 0}
        self.autoloc_busy = False
        self.autoloc_hist = []     # 最近几次自动定位的结果和对应的 map→odom，两处一致时接受（autoloc_pair）
        self.autoloc_moves = 0     # 这一轮已经「移动后重试」了几次
        self.pose_set_t = 0.0   # 最近一次（手动或自动）设置初始位姿的时间；启动后自动定位前用户已经手动给了就不再覆盖
        # 运控：站起 / 趴下（robot_ctrl.py）
        self.ctrl = None
        if CTRL_ON:
            try:
                self.ctrl = R.CtrlLink(node, node.event, fake=self.simulated)
            except Exception as e:
                node.event('ERROR', f'无法连接运控状态（域 0）：{e!r}；站起 / 趴下不可用')
        # 播报（audio.py）：本体喇叭，挂在运控那个域 0 节点上
        self.audio = AU.Audio(node.event, DATA, self.ctrl.ros if self.ctrl else None, self.push_audio, self.label_error,
                              os.environ.get('GO2W_AUDIO_LOCAL_EDIT', '1' if self.simulated else '0') == '1',
                              why='' if self.ctrl else '没连上域 0，喇叭不可用')
        self.ctrl_op = None        # 'up' / 'down' / 'policy'：正在站起 / 趴下 / 切策略
        self.climb = None          # 爬台模式开着：{'ws': 开的网页, 'who': 来源, 't': 开的时间}
        self.policies_cache = (-1e9, [])   # (列目录的时间, 可选策略)
        self.ctrl_abort = False    # 站起过程中被要求趴下
        self.ctrl_msg = ''         # 最近一次站起 / 趴下的结果
        self.stood_seq = 0         # 从趴着站起来的次数：网页据此作废定位（站起来时雷达位姿变了，要重新定位）
        self.hot_lie_t = -1e9      # 上次因电机过热发起趴下的时间（失败了隔 MOTOR_HOT_RETRY 再试）
        self.auto_lie = False      # 空闲自动趴下生效（网页站起来过或跑过任务；遥控器站起来的不管）
        self.stand_hold = False    # 网页点了「保持站立」：不自动趴下，直到下次出发或手动趴下
        self.idle_since = None     # 开始空闲的时间
        self.ctrl_online = None    # 运控在线状态（ctrl_watch 记变化）；None = 刚启动还没判断
        # 雷达无数据（lidar_watch）：active = 档位稳定在跑的起点；bad = 判定没数据的时刻；msg = 给人看的原因
        self.lidar = {'active': None, 'bad': None, 'msg': None}
        self.t_start = time.monotonic()
        self.prep_hold_t = -1e9    # 网页自检最近一次说「我在自检」的时间
        self.init_taught(DATA)
        self.taught_nav_file = os.path.join(DATA, 'taught_nav.json')
        try:
            with open(self.taught_nav_file) as f:
                self.taught_repo.enabled = bool(json.load(f).get('on', False))
        except (OSError, ValueError, AttributeError):
            pass

    # ---------- 工具 ----------
    def aw(self, fut, timeout=3.0):
        """把 rclpy future 变成可 await 的 asyncio future（回调在 rclpy 线程里触发）。"""
        loop = asyncio.get_running_loop()
        af = loop.create_future()

        def done(f):
            def settle():
                if af.done():
                    return
                try:
                    af.set_result(f.result())
                except Exception as e:
                    af.set_exception(e)
            loop.call_soon_threadsafe(settle)
        fut.add_done_callback(done)
        return asyncio.wait_for(af, timeout)

    async def disarm_and_cancel(self, reason):
        """停车：取消所有导航 + 上锁。服务不可用时照样尝试另一半，并如实报告。"""
        self.motion_stops += 1
        try:
            n, errors = self.node, []
            self.stop_route(reason)
            if self.teleop is not None:   # 遥控也立即停
                self.teleop = None
                n.publish_cmd(0.0, 0.0, 0.0)
            if n.cancel_cli.service_is_ready():
                try:
                    await self.aw(n.cancel_all(), 2.0)
                except Exception as e:
                    errors.append(f'取消导航失败：{e!r}')
            if n.arm_cli.service_is_ready():
                try:
                    r = await self.aw(n.call_arm(False), 2.0)
                    if r.success:
                        n.armed, n.armed_t = False, time.monotonic()   # 不等下一条 ~/armed，cmd_bridge 可能马上就被停掉
                    else:
                        errors.append(f'上锁失败：{r.message}')
                except Exception as e:
                    errors.append(f'上锁失败：{e!r}')
            else:
                errors.append('cmd_bridge 不在线，无法上锁（底盘没有在执行导航指令时不影响）')
            if errors:
                n.event('ERROR', f'{reason}：' + '；'.join(errors))
            else:
                n.event('WARN', f'{reason}：已取消导航并上锁')
            return errors
        finally:
            await self.taught_interrupt(reason)
            self.motion_stops -= 1

    # ---------- 指令 ----------
    def remote(self, ws):
        return self.clients.get(ws, {}).get('remote', '?')

    async def run_cmd(self, ws, d):
        try:
            ok, text, *data = await self.handle_cmd(ws, d)
        except Exception as e:
            self.node.get_logger().error(f'cmd {d.get("op")}: {e!r}')
            ok, text, data = False, f'后端出错：{e!r}', []
        reply = {'t': 'ack', 'id': d.get('id'), 'ok': ok, 'msg': text}
        if data:
            reply['data'] = data[0]
        self.tx_put(ws, json.dumps(reply))

    async def handle_cmd(self, ws, d):
        n, op = self.node, d.get('op')
        if self.map_busy and op in ('set_mode', 'load_map', 'save_map', 'align_map', 'edit_map', 'map_delete'):
            return False, '地图正在保存或对齐，请稍等'
        if ws.closed or ws not in self.clients:
            return False, '网页已断开'
        if isinstance(op, str) and op.startswith('taught_'):
            try:
                return await self.handle_taught(ws, d)
            except (TeachingError, ValueError) as e:
                return False, str(e)
        if isinstance(op, str) and op.startswith('audio_'):   # 播报：不算运动操作，不抢控制权
            return await self.audio.handle(d, self.remote(ws))
        if self._taught_job is not None and (op in ('goal', 'route_start', 'set_mode', 'load_map', 'autoloc_move')
                                            or (op == 'arm' and d.get('on'))):
            return False, '示教正在加载地图或定位自检，请保持静止并等待结果'
        if self.taught_recording() and op in ('goal', 'route_start', 'initial_pose', 'auto_localize', 'autoloc_move'):
            return False, '示教正在采集，请先暂停或结束采集'
        if self.taught_open() and self.taught_owner is not None and self.taught_owner is not ws and op == 'arm' and d.get('on'):
            return False, '另一个网页正在进行示教采集，请在原页面操作'
        if self.checking_route and op in ('goal', 'initial_pose', 'route_start', 'auto_localize', 'autoloc_move'):
            return False, '正在检查路线，请稍等'
        if self.climb is not None and op in ('goal', 'route_start', 'auto_localize', 'autoloc_move', 'taught_start'):
            return False, '爬台模式开着：先关掉爬台模式（切回 rough）再导航'
        if self.route_running() and (op in ('goal', 'initial_pose', 'auto_localize', 'autoloc_move')
                                     or (op == 'arm' and d.get('on'))):
            return False, '多点任务进行中：请先取消任务'
        # 先让准备凭据失效，再进入可能await的停车分支；同一页面取消也必须中断站起。
        cancelled_preparation = False
        if op in ('cancel', 'estop', 'lie_down') or (op == 'arm' and not d.get('on')):
            cancelled_preparation = self.cancel_taught_preparation(
                '用户停止了示教准备，请重新点击准备后继续')
        if op in ('goal', 'arm', 'cancel', 'estop'):
            self.owner = ws
        try:
            if op == 'estop':
                # 先停车，再记日志
                self.estop, self.estop_t = True, time.monotonic()
                self.climb_off(f'网页急停（{self.remote(ws)}）')
                errors = await self.disarm_and_cancel(f'网页急停（{self.remote(ws)}）')
                if 'estop' not in self.inc_open:
                    self.inc_open['estop'] = self.incident('estop', f'网页急停（{self.remote(ws)}）', ongoing=True)
                return (False, '急停已锁存，但：' + '；'.join(errors)) if errors else (True, '已急停：导航已取消，底盘已上锁')
            if op == 'release_estop':
                self.estop = False
                self.incident_close('estop')
                n.event('INFO', '急停已解除（底盘仍上锁）')
                return True, '急停已解除，底盘仍处于上锁'
            if op == 'initial_pose':
                if 'amcl' not in n.get_node_names():
                    return False, '定位（AMCL）没有运行，无法设置初始位姿'
                fine = d.get('cov') == 'fine'
                n.publish_initial_pose(d['x'], d['y'], d['th'], fine)
                self.pose_set_t = time.monotonic()
                self.autoloc_reset()
                n.event('INFO', f"{'重定位微调' if fine else '初始位姿'} ({d['x']:.2f}, {d['y']:.2f}, {math.degrees(d['th']):.1f}°)（{self.remote(ws)}）")
                # 11-09：定位错误期间（启动时 AMCL 默认在地图原点）雷达标的障碍会落在错误位置，
                # 机器人脚下的盲区又擦不掉 → 规划报「起点在障碍物里」。改位姿后清一次代价地图
                asyncio.ensure_future(self.clear_after_pose())
                return True, '位置已修正' if fine else '初始位姿已发布'
            if op == 'auto_localize':
                return await self.auto_localize(self.remote(ws))
            if op == 'autoloc_move':
                return await self.autoloc_move(ws)
            if op == 'stand_up':
                return await self.stand_up(self.remote(ws))
            if op == 'lie_down':
                self.owner = ws
                return await self.lie_down(self.remote(ws))
            if op == 'set_policy':
                return await self.switch_policy(d.get('name'), self.remote(ws))
            if op == 'climb':
                return self.set_climb(ws, bool(d.get('on')))
            if op == 'stand_hold':
                on = bool(d.get('on'))
                self.stand_hold, self.idle_since = on, None
                if on:
                    n.event('INFO', f'保持站立：不会自动趴下，直到下次出发或手动趴下（{self.remote(ws)}）')
                return True, '保持站立，不会自动趴下' if on else '空闲后自动趴下'
            if op == 'prep_hold':    # 网页自检进行中：别当成空闲趴下
                self.prep_hold_t = time.monotonic()
                return True, ''
            if op == 'goal':
                if self.estop:
                    return False, '急停中：先解除急停'
                reason = self.ctrl_blocker()
                if reason:
                    return False, reason
                if self.prof('patrol') != 'stop':
                    return False, '命令行启动的 patrol.py 在运行：先取消导航（会连巡逻一起停）'
                if not n.nav.server_is_ready():
                    return False, 'Nav2 没有运行（navigate_to_pose 不可用）'
                if self.goal_pending:
                    return False, '正在发送导航目标'
                ok, message = await self.check_ordinary_taught_binding(n.map_current)
                if not ok:
                    return False, message
                why = self.keepout_blocker(n.map_current, [d])
                if why:
                    return False, why
                self.route = None
                # 结束的旧目标（卡片会保留 5 s）先清掉：等 Nav2 接受新目标时，卡片不能显示旧目标的「已取消 / 失败」
                with n.lock:
                    if n.goal and n.goal['status'] != 'active':
                        n.goal = None
                self.goal_pending = True
                try:
                    h = await self.aw(n.send_goal(d['x'], d['y'], d['th']), 3.0)
                    if not h.accepted:
                        return False, 'Nav2 拒绝了这个目标'
                    n.track_goal(h, (d['x'], d['y'], d['th']))
                finally:
                    self.goal_pending = False
                n.event('INFO', f"导航目标 ({d['x']:.2f}, {d['y']:.2f}, {math.degrees(d['th']):.0f}°)")
                return True, '目标已下发' if n.is_armed() else '目标已下发，底盘上锁中，解锁后才会运动'
            if op == 'cancel':
                if cancelled_preparation:
                    return True, '已取消示教准备，不再继续后续站立或定位阶段'
                await self.taught_interrupt('手动取消，示教草稿已保留')
                self.motion_stops += 1
                try:
                    self.stop_route('手动取消')
                    # 巡逻中只取消当前目标的话，patrol.py 会重试，机器人又走起来：要连巡逻一起停
                    if self.prof('patrol') != 'stop':
                        await self.procs.stop_patrol()
                        await asyncio.sleep(0.5)
                        await self.refresh_procs()
                        n.event('INFO', '已停止巡逻')
                    if not n.cancel_cli.service_is_ready():
                        return False, 'Nav2 没有运行'
                    await self.aw(n.cancel_all(), 2.0)
                    return True, '已取消导航'
                finally:
                    self.motion_stops -= 1
            if op == 'arm':
                on = bool(d.get('on'))
                if not on:
                    await self.taught_interrupt('底盘上锁，示教草稿已保留')
                if on and self.estop:
                    return False, '急停中：先解除急停'
                if not on and self.route_running():
                    errors = await self.disarm_and_cancel('底盘上锁')
                    return (False, '；'.join(errors)) if errors else (True, '底盘已上锁，任务已取消')
                if not n.arm_cli.service_is_ready():
                    return False, '速度桥没有运行（刚开机或刚切换档位时要等几秒）'
                r = await self.aw(n.call_arm(on), 2.0)
                if r.success:
                    n.armed, n.armed_t = on, time.monotonic()
                n.event('WARN' if on else 'INFO', f"{'解锁' if on else '上锁'}：{r.message}（{self.remote(ws)}）")
                return r.success, r.message  # 以 cmd_bridge 的回复为准
            if op == 'set_param':
                key = d.get('key')
                if key not in PARAMS:
                    return False, f'不支持的参数：{key}'
                if not n.pset_cli.service_is_ready():
                    return False, 'cmd_bridge 没有运行'
                r = await self.aw(n.set_param(key, d['value']), 2.0)
                res = r.results[0]
                if res.successful:
                    n.params[key] = float(d['value'])
                    n.event('INFO', f"{BRIDGE} {PARAMS[key]} = {float(d['value']):.2f}")
                    return True, '参数已更新'
                return False, f'参数没有生效：{res.reason}'
            if op == 'set_mode':
                return self.set_mode(d.get('mode'), d.get('map'))
            if op in ('route_save', 'route_delete'):
                result = self.save_route(d) if op == 'route_save' else self.delete_route(d.get('route_id'))
                if result[0]:
                    await self.send_all(self.routes_message(), key='routes')
                return result
            if op == 'route_check':
                return await self.check_route(ws, d)
            if op == 'route_start':
                return await self.start_route_armed(ws, d)
            if op == 'bag_start':
                return await self.bag_start(d.get('name'), d.get('topics') or [])
            if op == 'bag_delete':
                return await self.bag_delete(d.get('name'), self.remote(ws))
            if op == 'rollback_set':
                return self.rollback_set(bool(d.get('on')), self.remote(ws))
            if op == 'set_taught_nav':
                return self.taught_nav_set(d.get('on') is True, self.remote(ws))
            if op == 'rollback_save':
                return await self.rollback_save(self.remote(ws), trigger=True)
            if op == 'bag_upload':
                return await self.bag_upload(d.get('name'), self.remote(ws))
            if op == 'bag_stop':
                await self.bag_stop()
                n.event('INFO', '录包已停止')
                return True, '录包已停止'
            if op == 'map_view':
                return await self.set_view(d.get('map'), d.get('angle'))
            if op == 'map_label':
                return await self.set_label(d.get('name'), d.get('label'), self.remote(ws))
            if op == 'keepout_save':
                return await self.keepout_save(d.get('map'), d.get('shapes'), self.remote(ws))
            if op == 'save_map':
                return await self.save_map(d.get('name', ''))
            if op == 'align_map':
                return await self.align_map(d.get('source'), d.get('name'), d.get('angle'))
            if op == 'edit_map':
                return await self.edit_map(d, self.remote(ws))
            if op == 'load_map':
                return await self.load_map(d.get('name', ''))
            if op == 'map_delete':
                result = await self.map_delete(d.get('names'), self.remote(ws))
                if result[0]:
                    await self.send_all(self.routes_message(), key='routes')
                    await self.send_all(self.views_message(), key='views')
                    await self.send_all(self.labels_message(), key='labels')
                    await self.send_all(self.keepout_message(), key='keepout')
                return result
            if op == 'rtab_start':
                return await self.rtab_start_map(self.remote(ws))
            if op == 'rtab_continue':
                res = await self.rtab_continue(d.get('map'), self.remote(ws), force=bool(d.get('force')))
                if not res[0]:
                    n.event('WARN', f'局部重扫没开始：{res[1]}（{self.remote(ws)}）')
                return res
            if op in ('rtab_pause', 'rtab_resume'):
                return await self.rtab_call(op[5:], self.remote(ws))
            if op == 'rtab_stop':
                return await self.rtab_stop(f'网页停止（{self.remote(ws)}）')
            if op == 'rtab_auto':
                return self.rtab_set_auto(bool(d.get('on')), self.remote(ws))
            if op == 'cont_preflight':
                return self.cont_preflight(d.get('base'))
            if op == 'cont_start':
                # 成功时 cont_start 自己记日志；失败也要记——网页提示只停 2.6 s，事后只能靠日志查
                res = await self.cont_start(d.get('base'), d.get('name'), d.get('pose'),
                                            d.get('source'), d.get('reason'))
                if res[0]:
                    self.background(self.rtab_on_cont_start())
                if not res[0]:
                    n.event('WARN', f'续建没开始：{res[1]}（{self.remote(ws)}）')
                return res
            if op == 'map_resume':
                return await self.map_resume(self.remote(ws))
            if op == 'cont_check':
                return await self.cont_check()
            if op == 'cont_finish':
                return await self.cont_finish(d.get('reason'))
            if op == 'cont_abort':
                return await self.cont_abort()
            if op == 'cont_parking':
                return self.cont_parking(d.get('base'), d.get('clear'))
            return False, f'不支持的操作：{op}'
        except asyncio.TimeoutError:
            return False, '对方没有响应（超时）'
        except (KeyError, TypeError, ValueError) as e:
            return False, f'指令格式不对：{e!r}'

    # ---------- 运控：站起 / 趴下（robot_ctrl.py） ----------
    # 站起 = 1004（Passive → FixStand，插值 2 s）→ 等站稳 → 1002（FixStand → Velocity，策略接管）→ 等 ready。
    # 趴下 = 1005（直接卸力塌下）。网页站起来（或跑过任务）后，空闲 IDLE_LIE 秒就原地趴下。
    def ctrl_get(self):
        return self.ctrl.get(CTRL_STALE) if self.ctrl else (None, False, -1)

    def ctrl_blocker(self):
        """导航能不能让机器狗动：要在策略（Velocity）里。没接运控（GO2W_CTRL=0）时不拦。"""
        if self.ctrl is None:
            return '无法连接运控状态，请检查运行容器的 control 组和域 0 通信' if CTRL_ON else None
        s, ready, _ = self.ctrl_get()
        if s is None:
            return '收不到运控状态（/go2w_ctrl/state），请检查运行容器的 control 组和域 0 通信'
        if s != 'Velocity' or not ready:
            return f'机器狗没有站起来（运控状态 {s}）'
        return None

    def moving(self):
        n = self.node
        v, p = n.odom, n.odom_pose
        if v is None or p is None or time.monotonic() - p[3] > STALE:
            return False   # 没有里程计（导航没开）：不知道，按静止算
        return abs(v[0]) > STILL_V or abs(v[1]) > STILL_V or abs(v[2]) > STILL_W

    async def ctrl_switch(self, api, src, dst, timeout, need_ready=True, valid=None):
        """在 src（元组）里时发 api（need_ready：还要等 src 的 ready），直到 dst 且 ready。返回 None 或失败原因。"""
        t0, sent = time.monotonic(), -1e9
        while True:
            # 示教准备绑定发起页面；每次重发和判断完成前都复核，取消后不能进入下一站起阶段。
            if valid is not None and not valid():
                return '发起页面已断开、准备已取消或控制权变化，已停止后续站立动作'
            if self.ctrl_abort and api != R.API_STANDDOWN:
                return '被「趴下」打断'
            s, ready, _ = self.ctrl_get()
            if s is None:
                return '收不到运控状态'
            if s == dst:
                if ready:
                    return None
            elif s not in src:
                return f'运控状态变成了 {s}'
            elif (ready or not need_ready) and time.monotonic() - sent > CTRL_RESEND:
                self.ctrl.send(api)
                sent = time.monotonic()
            if time.monotonic() - t0 > timeout:
                return f'{timeout:.0f} s 内没有完成（运控状态 {s}{"" if ready else "，未就绪"}）'
            await asyncio.sleep(0.05)

    def ctrl_done(self, ok, msg):
        self.ctrl_msg = msg
        self.node.event('INFO' if ok else 'ERROR', msg)
        return ok, msg

    async def stand_up(self, who, *, valid=None):
        if valid is not None and not valid():
            return False, '准备已取消，未继续站起'
        if self.ctrl is None:
            return False, self.ctrl_blocker() or '运控接入已关闭（GO2W_CTRL=0）'
        if self.ctrl_op:
            return False, '正在站起 / 趴下，请稍等'
        if self.estop:
            return False, '急停中：先解除急停'
        s, ready, _ = self.ctrl_get()
        if s is None:
            return False, '收不到运控状态（/go2w_ctrl/state），请检查运行容器的 control 组和域 0 通信'
        hot = self.motor_hot()
        if hot and s == 'Passive':
            return False, f'{MOTOR_NAMES[hot[1]]} {hot[0]} ℃，电机过热（≥{MOTOR_HOT_LIE:.0f} ℃），降温后再站起'
        self.auto_lie, self.stand_hold, self.idle_since = True, False, None   # 网页站起来的：空闲了自动趴下
        if s == 'Velocity' and ready:
            return True, '已经站好（策略运行中）'
        if s not in R.STATES:
            return False, f'运控状态 {s}，不能站起'
        self.ctrl_op, self.ctrl_abort, lying = 'up', False, s == 'Passive'
        try:
            n = self.node
            if lying:
                n.event('INFO', f'站起：Passive → FixStand（{who}）')
                err = await self.ctrl_switch(R.API_STANDUP, ('Passive',), 'FixStand', CTRL_INTERP + CTRL_WAIT, valid=valid)
                if err:
                    return self.ctrl_done(False, f'站起失败：{err}')
            if self.estop:
                return self.ctrl_done(False, '急停中：停在站立姿态，没有启动策略')
            s, ready, _ = self.ctrl_get()
            if s != 'Velocity':
                n.event('INFO', '启动策略：FixStand → Velocity')
                # FixStand 没站稳时运控也接受 1002，会在半站的姿态下切进策略：ctrl_switch 等 ready 才发
                err = await self.ctrl_switch(R.API_BALANCESTAND, ('FixStand',), 'Velocity',
                                             CTRL_WAIT + (0 if ready else CTRL_INTERP), valid=valid)
                if err:
                    return self.ctrl_done(False, f'启动策略失败：{err}')
            if lying:
                self.stood_seq += 1
                self.autoloc_reset()   # 雷达高度变了，趴着时的结果不算
            return self.ctrl_done(True, '已站起，策略已接管')
        finally:
            self.ctrl_op = None

    async def lie_down(self, who, auto=False):
        """原地趴下。手动的先取消任务、上锁，等停稳；自动的只在空闲时触发（idle_tick）。"""
        if self.ctrl is None:
            return False, self.ctrl_blocker() or '运控接入已关闭（GO2W_CTRL=0）'
        s, _, _ = self.ctrl_get()
        if s is None:
            return False, '收不到运控状态（/go2w_ctrl/state），请检查运行容器的 control 组和域 0 通信'
        if s == 'Passive':
            self.auto_lie = self.stand_hold = False
            return True, '已经趴着'
        if self.ctrl_op == 'down':
            return False, '正在趴下'
        self.climb_off(f'趴下（{who}）')
        self.ctrl_abort = True                 # 正在站起就打断
        while self.ctrl_op == 'up':
            await asyncio.sleep(0.05)
        self.ctrl_op = 'down'
        try:
            n = self.node
            if (self.route_running() or n.nav_active() or self.teleop is not None or n.is_armed()
                    or self.prof('patrol') != 'stop'):
                await self.disarm_and_cancel(f'趴下（{who}）')
                if self.prof('patrol') != 'stop':
                    await self.procs.stop_patrol()
            t0 = time.monotonic()
            while self.moving() and time.monotonic() - t0 < 3.0:   # 走着的时候塌下去太危险：等停稳
                await asyncio.sleep(0.1)
            s, _, _ = self.ctrl_get()
            n.event('WARN', f'趴下（{who}）：{s} → Passive')
            err = await self.ctrl_switch(R.API_STANDDOWN, ('FixStand', 'Velocity'), 'Passive', CTRL_WAIT, need_ready=False)
            if err:
                return self.ctrl_done(False, f'趴下失败：{err}')
            self.auto_lie = self.stand_hold = False
            self.idle_since = None
            return self.ctrl_done(True, '已趴下' + ('（电机过热自动）' if auto == 'hot' else '（空闲自动）' if auto else ''))
        finally:
            self.ctrl_op, self.ctrl_abort = None, False
            self.lie_done_t = time.monotonic()

    # ---------- 摔倒 / 里程计发散保护 ----------
    def fall_tick(self):
        """safety 循环 5 Hz：运控意外掉到 Passive、或里程计数值离谱 → 记异常（自动存回溯包），建图中就冻结。"""
        n = self.node
        now = time.monotonic()
        bt = n.body_tilt()
        if bt is not None and bt[0] != self.tilt_stamp:   # 时间戳没变 = planar_odom 没在发了，旧值不算
            self.tilt_stamp = bt[0]
            self.tilt_hist.append((now, bt[1]))
        while self.tilt_hist and now - self.tilt_hist[0][0] > FALL_TILT_PRE + FALL_TILT_POST + 0.5:
            self.tilt_hist.popleft()
        s, _, _ = self.ctrl_get()
        prev, self.fall_prev = self.fall_prev, (s or self.fall_prev)
        if (prev in ('FixStand', 'Velocity') and s == 'Passive' and self.ctrl_op != 'down'
                and now - self.lie_done_t > FALL_LIE_GRACE and self.fall_pending is None):
            self.fall_pending = {'t': now, 'prev': prev}
            if self.prof('mapping') == 'run':   # 建图先冻结（摔倒和趴下都不该再往图里加东西），判断稍后
                self.background(self.freeze_mapping(f'运控从 {prev} 掉到 Passive（不是网页让的）'))
        f = self.fall_pending
        if f and now - f['t'] >= FALL_TILT_POST:
            self.fall_pending = None
            seen = [a for t, a in self.tilt_hist if f['t'] - FALL_TILT_PRE <= t <= now]
            top = max(seen) if seen else None
            if top is None:
                self.incident('fall', f"运控从 {f['prev']} 意外掉到 Passive：没有机身姿态数据，分不清是摔倒还是遥控器让它趴下",
                              ago=now - f['t'])
            elif top >= FALL_TILT_DEG:
                self.incident('fall', f"摔倒：机身倾角最大 {top:.0f}°，运控姿态保护从 {f['prev']} 切到 Passive",
                              ago=now - f['t'])
            else:
                n.event('WARN', f"遥控器让狗趴下了：运控从 {f['prev']} 切到 Passive，不是网页发起的"
                                f"（机身倾角最大 {top:.0f}°，不是摔倒，不记异常）")
        w = n.odom_wild
        if w and w is not self.odom_wild_seen:
            self.odom_wild_seen = w
            self.incident('odom_wild', f'{w[0]}：FAST-LIO 很可能已经发散')
            if self.prof('mapping') == 'run':
                self.background(self.freeze_mapping(w[0], wild=True))

    async def freeze_mapping(self, reason, wild=False):
        """关扫描闸门（地图停在这一刻）、停自动保存（最近一份好的不再被覆盖）、暂停续建采集。"""
        n, a = self.node, self.autosave
        f = self.map_frozen
        if f:
            if wild and not f['wild']:
                f['wild'] = True
                f['reason'] += f'；随后{reason}'
                n.event('ERROR', f'建图冻结中又发现{reason}：里程计已经飘了，不能再继续建，只能停止建图后续建')
            return
        f = self.map_frozen = {'reason': reason, 'wild': wild, 'wall': time.time(),
                               'kept': a['name'] if a['wall'] else None, 'kept_wall': a['wall'], 'gate': None}
        try:
            await self.gate_set(0)
            f['gate'] = True
        except Exception as e:
            f['gate'] = False
            gate_err = str(e) or repr(e)
        if self.cont:
            self.cont['ready'] = False
            self.cont['check'] = {'ok': False, 'code': 'frozen', 'reason': f'建图冻结：{reason}'}
        self.background(self.rtab_on_freeze(reason))
        kept = (f"最近一份好的自动保存 data/maps/{f['kept']}（{time.strftime('%H:%M:%S', time.localtime(f['kept_wall']))}）"
                f"不会再被覆盖" if f['kept'] else '还没有自动保存过')
        n.event('ERROR', f'建图已冻结：{reason}。' +
                ('已关闭建图扫描闸门，地图停在这一刻；' if f['gate'] else
                 f'扫描闸门不可用（{gate_err}），slam 可能还在被写坏；') +
                f'自动保存已暂停，{kept}。建议：停止建图，把狗扶正、静止放平，再从这份图（或刚手动保存的图）续建' +
                ('' if wild else '；如果只是正常趴下、地图没乱，站起来后可以点「继续建图」'))

    async def map_resume(self, who):
        f = self.map_frozen
        if not f:
            return False, '建图没有被冻结'
        if f['wild'] or self.node.odom_wild:
            return False, '里程计已经飘过，继续建只会把地图写坏：请停止建图，扶正静止后从最近的好图续建'
        if self.prof('mapping') != 'run':
            return False, '建图没有在运行'
        s, _, _ = self.ctrl_get()
        if s not in ('FixStand', 'Velocity'):
            return False, f'先让狗站起来再继续建图（运控状态 {s}；趴着时雷达高度不对，扫到的墙会错位）'
        if self.cont:
            self.map_frozen = None
            self.node.event('WARN', f'解除建图冻结（{who}）：续建采集仍暂停，站稳后点「自检」，通过才恢复')
            return True, '已解除冻结：续建请站稳后点「自检」，通过后才会恢复采集'
        if f['gate']:
            await self.gate_set(-1)
        self.map_frozen = None
        self.node.event('WARN', f"继续建图（{who}）：解除冻结（原因：{f['reason']}），自动保存恢复")
        await self.rtab_on_resume(who)
        return True, '已继续建图，自动保存恢复'

    # ---------- 运控策略切换：只在趴着时，停掉 control 组换策略名重启 ----------
    def policies(self):
        t, lst = self.policies_cache
        if time.monotonic() - t > 30.0:
            try:
                lst = list(self.procs.list_policies())
            except Exception:
                lst = []
            self.policies_cache = (time.monotonic(), lst)
        return lst

    def policy_now(self):
        """当前运控策略 (名字或 None, 来源)。先看 supervisor 里 control 组的实际启动参数（网页切过策略也对），
        拿不到再读 runtime.env（开机默认值）。"""
        try:
            _, env = self.procs.control_env()
        except Exception:
            env = {}
        if env.get('GO2W_CONTROL_POLICY'):
            return env['GO2W_CONTROL_POLICY'], 'supervisor'
        return MM.current_policy()

    async def ctrl_wait_restart(self, t0, timeout):
        """等重启后的运控发来状态（收到时间晚于 t0）。返回 (None 或失败原因, 状态)。"""
        while time.monotonic() - t0 < timeout:
            st, ready, _, t = self.ctrl.snap
            if t > t0 and st is not None:
                if st == 'Passive' and ready:
                    return None, st
                # 新进程刚起来先报 Init（seq 0，真机约 1.3 s 后变 Passive），属于启动中，接着等
                if st not in ('Passive', 'Init'):
                    return f'新进程起来是 {st}，不是趴着（Passive）', st
            await asyncio.sleep(0.1)
        return f'{timeout:.0f} s 内没有收到新运控进程的状态', None

    async def switch_policy(self, name, who):
        if self.ctrl is None:
            return False, self.ctrl_blocker() or '运控接入已关闭（GO2W_CTRL=0）'
        if not isinstance(name, str) or name not in self.policies():
            return False, f'没有这个策略：{name}'
        cur, _ = self.policy_now()
        if name == cur:
            return True, f'已经是 {name}'
        if self.ctrl_op:
            return False, '运控正在操作（站起 / 趴下 / 切策略），请稍等'
        s, ready, _ = self.ctrl_get()
        if s != 'Passive' or not ready:
            return False, f'只能趴着时切换策略（现在：{s or "运控离线"}）'
        if self.ctrl_busy():
            return False, '还有任务、导航目标、遥控、巡逻或自检在进行，先停下来再切策略'
        n = self.node
        self.ctrl_op = 'policy'
        try:
            n.event('WARN', f'切换运控策略：{cur} → {name}（{who}），运控重启中')
            try:
                old = await self.procs.switch_policy(name)
            except Exception as e:
                return self.ctrl_done(False, f'切换策略失败：{e}')
            err, st = await self.ctrl_wait_restart(time.monotonic(), POLICY_WAIT)
            if err is None:
                return self.ctrl_done(True, f'运控策略已切换为 {name}（趴着待命；重启扩展坞后恢复为 runtime.env 里的策略）')
            if st is not None or not old:
                # 新进程起来了但不是趴着（已经在出力），或者不知道原来是什么：不再自动重启运控，交给人处理
                return self.ctrl_done(False, f'切换到 {name} 异常：{err}。没有自动退回，请检查机器狗和 control 组日志')
            n.event('ERROR', f'新策略 {name} 没起来（{err}），退回 {old}')
            try:
                await self.procs.switch_policy(old)
                err2, _ = await self.ctrl_wait_restart(time.monotonic(), POLICY_WAIT)
            except Exception as e:
                err2 = str(e)
            return self.ctrl_done(False, f'切换到 {name} 失败（{err}）' + (
                f'，已退回 {old}' if err2 is None else f'；退回 {old} 也失败（{err2}），请到扩展坞上检查 control 组'))
        finally:
            self.ctrl_op = None
            await self.refresh_procs()

    # ---------- 爬台模式（融合策略 2101） ----------
    def set_climb(self, ws, on):
        who = self.remote(ws)
        if not on:
            if self.climb is None:
                return True, '爬台模式已经是关的'
            self.climb_off(f'关闭爬台模式（{who}）')
            return True, '已关闭爬台模式，运控切回 rough'
        if self.ctrl is None:
            return False, self.ctrl_blocker() or '运控接入已关闭（GO2W_CTRL=0）'
        if self.estop:
            return False, '急停中：先解除急停'
        if self.ctrl_op:
            return False, '运控正在操作（站起 / 趴下 / 切策略），请稍等'
        s, ready, _ = self.ctrl_get()
        if s is None:
            return False, '收不到运控状态（/go2w_ctrl/state）'
        if s != 'Velocity' or not ready:
            return False, f'只能站着（策略运行中）开爬台模式（现在：{s}）'
        if self.ctrl.get_dual(CTRL_STALE) is None:
            return False, '当前运控策略不是融合策略（rough + climb），没有爬台模式'
        n = self.node
        if (self.route_running() or self.goal_pending or n.nav_active() or self.taught_recording()
                or self._taught_job is not None or self.prof('patrol') != 'stop'):
            return False, '有导航任务在跑：先取消任务再开爬台模式（爬台用遥控或网页摇杆）'
        if self.climb is not None:
            self.climb['ws'] = ws
            return True, '爬台模式已经开着'
        self.climb = {'ws': ws, 'who': who, 't': time.monotonic()}
        self.ctrl.climb(True)
        n.event('WARN', f'开启爬台模式（{who}）：切到 climb 策略，最长 {CLIMB_MAX:.0f} s 自动关')
        return True, '爬台模式已开：速度 |vx|≤0.5、|vy|≤0.15、|wz|≤0.15 时切到 climb'

    def climb_off(self, reason):
        if self.climb is None:
            return
        self.climb = None
        if self.ctrl is not None:
            self.ctrl.climb(False)
            self.ctrl.climb(False)   # 发两帧防丢；真丢了运控 1 s 没续约也会自己回 rough
        self.node.event('INFO', f'爬台模式已关：{reason}，运控切回 rough')

    def climb_tick(self):
        """safety 循环 5 Hz：开着就续约；出状况就关。"""
        c = self.climb
        if c is None:
            return
        s, ready, _ = self.ctrl_get()
        ws = c['ws']
        gap = time.monotonic() - self.clients[ws]['last_ping'] if ws in self.clients else None
        reason = ('急停' if self.estop
                  else '开启爬台模式的网页断开了' if gap is None
                  else f'开启爬台模式的网页心跳丢失 {gap:.1f} s' if gap > self.heartbeat
                  else f'运控状态 {s or "离线"}，不在行走状态' if s != 'Velocity'
                  else '运控状态里没有融合策略信息' if self.ctrl.get_dual(CTRL_STALE) is None
                  else f'已开 {CLIMB_MAX:.0f} s，自动关闭' if time.monotonic() - c['t'] > CLIMB_MAX
                  else None)
        if reason:
            self.climb_off(reason)
        else:
            self.ctrl.climb(True)

    def ctrl_busy(self):
        """有事在做，不算空闲：任务、导航、遥控、巡逻、检查、自动定位、站起趴下、档位切换、网页自检中、保持站立、还在走。"""
        n = self.node
        return bool(self.route_running() or self.goal_pending or n.nav_active() or self.teleop is not None
                    or self.taught_recording() or self._taught_job is not None
                    or self.checking_route or self.autoloc_busy or self.ctrl_op or self.busy or self.map_loading
                    or self.stand_hold or self.climb is not None or time.monotonic() - self.prep_hold_t < PREP_HOLD_TTL
                    or self.prof('patrol') != 'stop' or self.moving())

    def idle_tick(self):
        if self.ctrl is None or IDLE_LIE <= 0:
            return
        s, _, _ = self.ctrl_get()
        if s == 'Passive':
            self.auto_lie = self.stand_hold = False   # 已经趴下了（不管是谁让它趴的）：下次站起来再说
        if not self.auto_lie or s not in ('FixStand', 'Velocity') or self.ctrl_busy():
            self.idle_since = None
            return
        now = time.monotonic()
        if self.idle_since is None:
            self.idle_since = now
        elif now - self.idle_since >= IDLE_LIE:
            self.idle_since = None
            self.background(self.lie_down(f'空闲 {IDLE_LIE:.0f} s 自动', auto=True))

    def motor_hot(self):
        """最热的过热电机 (℃, 序号)；没有过热 / 温度读不到 / 功能关掉 = None。"""
        n = self.node
        t = n.motor_temp
        if MOTOR_HOT_LIE <= 0 or t is None or time.monotonic() - n.motor_temp_t > MOTOR_STALE:
            return None
        return max(((v, i) for i, v in enumerate(t) if v >= MOTOR_HOT_LIE), default=None)

    def hot_tick(self):
        """电机过热：站着就自动趴下。"""
        if self.ctrl is None:
            return
        hot = self.motor_hot()
        if not hot:
            self.incident_close('motor_hot')   # 降到阈值以下（或温度读不到了）：这次过热结束
            return
        s, _, _ = self.ctrl_get()
        now = time.monotonic()
        if s not in ('FixStand', 'Velocity') or self.ctrl_op == 'down' or now - self.hot_lie_t < MOTOR_HOT_RETRY:
            return
        self.hot_lie_t = now
        v, i = hot
        self.node.event('ERROR', f'电机过热：{MOTOR_NAMES[i]} {v} ℃（≥{MOTOR_HOT_LIE:.0f} ℃），自动趴下')
        if 'motor_hot' not in self.inc_open:   # 没趴成隔一会儿重试，仍是同一次过热
            self.inc_open['motor_hot'] = self.incident(
                'motor_hot', f'{MOTOR_NAMES[i]} {v} ℃（≥{MOTOR_HOT_LIE:.0f} ℃），自动趴下', ongoing=True)
        self.background(self.lie_down(f'{MOTOR_NAMES[i]} {v} ℃ 过热，自动', auto='hot'))

    def ctrl_watch(self):
        """运控在线 / 离线变化时记一条日志（网页上同时有横幅和提示）。后端启动 10 s 内还没收到不算离线。"""
        if self.ctrl is None or self.ctrl_op == 'policy':   # 切策略时运控本来就要重启一下
            return
        online = self.ctrl_get()[0] is not None
        prev = self.ctrl_online
        if prev is None:
            if online:
                self.ctrl_online = True
            elif time.monotonic() - self.t_start > 10.0:
                self.ctrl_online = False
                self.node.event('ERROR', '运控离线：后端启动以来没有收到运控状态（/go2w_ctrl/state），'
                                         '站起和出发都不可用。检查运行容器的 control 组和域 0 通信')
                self.inc_open['ctrl_offline'] = self.incident('ctrl_offline', '后端启动以来没有收到运控状态', ongoing=True)
            return
        if prev and not online:
            self.ctrl_online = False
            self.node.event('ERROR', f'运控离线：超过 {CTRL_STALE:.0f} s 没有收到运控状态（/go2w_ctrl/state），'
                                     '站起和出发都不可用。检查运行容器的 control 组和域 0 通信')
            self.inc_open['ctrl_offline'] = self.incident(
                'ctrl_offline', f'超过 {CTRL_STALE:.0f} s 没有收到运控状态', ongoing=True)
        elif not prev and online:
            self.ctrl_online = True
            self.incident_close('ctrl_offline')
            self.node.event('INFO', '运控已恢复：重新收到运控状态')

    def lidar_watch(self):
        """每秒一次。建图 / 定位 / 导航在 run 且已稳 LIDAR_GRACE 秒后才判断；档位停了就把没结束的异常收掉。"""
        L, n, now = self.lidar, self.node, time.monotonic()
        if not any(self.prof(m) == 'run' for m in P.MODES):
            if L['bad'] is not None:
                self.incident_close('lidar_lost')
                n.event('INFO', f"档位已停，雷达无数据的提醒结束（持续约 {now - L['bad']:.0f} s）")
            L.update(active=None, bad=None, msg=None)
            return
        if L['active'] is None:
            L['active'] = now
        if now - L['active'] < LIDAR_GRACE:
            return
        stamps = n.rates['/scan'].stamps
        quiet = now - max(stamps[-1] if stamps else 0.0, L['active'] + LIDAR_GRACE - LIDAR_STALE)
        if L['bad'] is None:
            if quiet < LIDAR_STALE:
                return
            L['bad'] = now
            driver = 'livox_lidar_publisher' in set(n.get_node_names())
            L['msg'] = f'/scan 已经 {quiet:.0f} s 没有数据' if driver else '雷达驱动没有运行（livox_lidar_publisher 不在）'
            self.inc_open['lidar_lost'] = self.incident('lidar_lost', L['msg'], ongoing=True, ago=quiet,
                                                        bag='雷达没数据，回溯包里也没有点云，不存')
            if driver:
                n.event('ERROR', f"雷达无数据：{L['msg']}，正在检查雷达网络……")
                asyncio.ensure_future(self.lidar_report(L['bad']))
            else:
                n.event('ERROR', f"雷达无数据：{L['msg']}，停止后重开档位")
        elif quiet < 1.0:
            self.incident_close('lidar_lost')
            n.event('INFO', f"雷达已恢复：/scan 又有数据了（断了约 {now - L['bad'] + LIDAR_STALE:.0f} s）")
            L.update(bad=None, msg=None)

    async def lidar_report(self, since):
        try:
            text = await lidar_diagnose()
        except Exception as e:
            text = f'诊断失败：{e!r}'
        L = self.lidar
        if L['bad'] != since:   # 诊断期间已经恢复 / 档位停了
            return
        L['msg'] = f"{L['msg']}。{text}"
        self.node.event('ERROR', f'雷达无数据：{text}')
        rid = self.inc_open.get('lidar_lost')
        rec = self.incidents.get(rid) if rid else None
        if rec is not None:
            rec['msg'] = L['msg']
            self.incidents.changed(rec)   # 存盘 + 推给云端监控页

    def lidar_view(self):
        L = self.lidar
        return None if L['bad'] is None else {'since': round(time.monotonic() - L['bad'], 1), 'msg': L['msg']}

    def ctrl_view(self):
        if self.ctrl is None:
            return {'on': CTRL_ON, 'state': None, 'ready': False, 'age': None}
        s, ready, seq = self.ctrl_get()
        left = None if self.idle_since is None else max(0.0, IDLE_LIE - (time.monotonic() - self.idle_since))
        rx = self.ctrl.last_rx()
        dual = self.ctrl.get_dual(CTRL_STALE)
        return {'on': True, 'state': s, 'ready': ready, 'seq': seq, 'op': self.ctrl_op, 'msg': self.ctrl_msg,
                'age': None if rx is None else round(time.monotonic() - rx, 1),   # 距最后一次收到运控状态多少秒；从没收到 = None
                'stood': self.stood_seq, 'auto': self.auto_lie and IDLE_LIE > 0, 'hold': self.stand_hold,
                'lieIn': None if left is None else round(left, 1), 'hotLie': MOTOR_HOT_LIE,
                'policy': self.policy_now()[0], 'policies': self.policies(),
                'dual': None if dual is None else {'policy': dual[0], 'switching': dual[1], 'req': dual[2]},
                'climb': None if self.climb is None else {'since': round(time.monotonic() - self.climb['t'], 1), 'max': CLIMB_MAX}}

    # ---------- 多点导航 ----------
    @staticmethod
    def route_next(n, loops, state):
        """循环路线的下一站：state = (站序号, 第几圈, 是否在回起点)，返回 None 表示走完。
        单程（loops = None）：0 → n-1。循环：0 → … → n-1 → 0 算一圈（终点直接回起点，不原路返回），
        下一圈从 1 出发；loops = 0 表示一直循环。"""
        i, loop, back = state
        if back:                          # 刚回到起点：这一圈走完
            return (1, loop + 1, False) if loops == 0 or loop < loops else None
        if i < n - 1:
            return i + 1, loop, False
        return (0, loop, True) if loops is not None else None

    @staticmethod
    def route_pose(points, i, back):
        """回到起点时也按 1 号点自己的朝向停（自动朝向 = 面向 2 号点），下一圈出发前不用再转。"""
        return points[i]

    @staticmethod
    def route_record(d, route_id, strict=True):
        name, map_name, points = d['name'], d['map'], d['points']
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 60:
            raise ValueError('任务名称需要 1–60 个字符')
        if not isinstance(map_name, str) or not map_name:
            raise ValueError('需要绑定地图')
        if not isinstance(points, list) or not 2 <= len(points) <= 50:
            raise ValueError('每条路线需要 2–50 个点')
        clean = []
        for point in points:
            if not isinstance(point, dict) or any(
                type(point.get(k)) not in (float, int) or not math.isfinite(point[k]) for k in ('x', 'y', 'th')
            ):
                raise ValueError('路线包含无效坐标')
            label, manual = point.get('name', ''), point.get('manualHeading', True)
            note = point.get('note', '')    # 11-18：站点备注（给云端监控页看），老任务没有 = 空
            if not isinstance(label, str) or len(label.strip()) > 40:
                raise ValueError('站点名称最多 40 个字符')
            if not isinstance(note, str) or len(note.strip()) > ROUTE_NOTE_MAX:
                raise ValueError(f'站点备注最多 {ROUTE_NOTE_MAX} 个字符')
            if type(manual) is not bool:
                raise ValueError('站点朝向类型无效')
            dwell = point.get('dwell', 0)   # 11-24：到这一站停留几秒（0 = 不停留）
            if isinstance(dwell, bool) or not isinstance(dwell, (int, float)) or not math.isfinite(dwell) \
                    or not 0 <= dwell <= ROUTE_DWELL_MAX:
                raise ValueError(f'站点停留时间需要 0–{ROUTE_DWELL_MAX} 秒')
            item = {'x': point['x'], 'y': point['y'], 'th': point['th'],
                    'name': label.strip(), 'note': note.strip(), 'manualHeading': manual}
            if round(dwell) > 0:
                item['dwell'] = int(round(dwell))
            clean.append(item)
        return {'id': route_id, 'name': name.strip(), 'map': map_name, 'points': clean,
                'opts': Server.route_opts(d.get('opts'), strict)}

    # 任务的运行参数（11-12 起跟任务一起保存）：循环、圈数（0 = 一直循环）、最大 vx / wz。老任务没有就用默认值
    ROUTE_OPTS = {'loop': False, 'loops': 1, 'vmax': 0.45, 'wmax': 1.2, 'through': True}

    @staticmethod
    def route_opts(o, strict=True):
        out = dict(Server.ROUTE_OPTS)
        if o is None:
            return out
        if not isinstance(o, dict):
            if strict:
                raise ValueError('运行参数格式不对')
            return out
        checks = {'loop': lambda v: type(v) is bool, 'through': lambda v: type(v) is bool,
                  'loops': lambda v: type(v) is int and 0 <= v <= 1000,
                  'vmax': lambda v: type(v) in (int, float) and math.isfinite(v) and 0.1 <= v <= 1.2,
                  'wmax': lambda v: type(v) in (int, float) and math.isfinite(v) and 0.2 <= v <= 1.5}
        msgs = {'loop': '循环开关无效', 'through': '连续通过开关无效', 'loops': '圈数需要 0–1000 的整数', 'vmax': '最大 vx 需要在 0.1–1.2 m/s 之间',
                'wmax': '最大 wz 需要在 0.2–1.5 rad/s 之间'}
        for k, ok in checks.items():
            if k not in o:
                continue
            if ok(o[k]):
                out[k] = float(o[k]) if k in ('vmax', 'wmax') else o[k]
            elif strict:
                raise ValueError(msgs[k])
        return out

    def save_route(self, d):
        if self.routes_error:
            return False, '任务文件读取失败，请检查后端日志'
        shown = self.shown_map()
        if not shown or d.get('map') != shown or self.map_loading or self.busy:
            return False, '请在路线对应的地图上保存'
        route_id = d.get('route_id')
        if route_id is not None and route_id not in self.routes:
            return False, '找不到要更新的任务'
        route_id = route_id or uuid.uuid4().hex
        record = self.route_record(d, route_id)
        self.write_routes({**self.routes, route_id: record})
        return True, f"已保存任务：{record['name']}", record

    def delete_route(self, route_id):
        if self.routes_error:
            return False, '任务文件读取失败，请检查后端日志'
        if route_id not in self.routes:
            return False, '找不到要删除的任务'
        if self.route_running() and self.route['record']['id'] == route_id:
            return False, '请先结束正在执行的任务'
        routes = dict(self.routes)
        record = routes.pop(route_id)
        self.write_routes(routes)
        return True, f"已删除任务：{record['name']}"

    def write_routes(self, routes):
        os.makedirs(DATA, exist_ok=True)
        tmp = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=DATA, delete=False) as f:
                tmp = f.name
                json.dump(list(routes.values()), f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.routes_file)
        finally:
            if tmp and os.path.exists(tmp):
                os.unlink(tmp)
        self.routes = routes
        self.prune_resumes(write=True)   # 任务删了、站点改了：断点作废

    @staticmethod
    def view_angle(v):
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            raise ValueError('角度必须是有限的数字')
        a = math.remainder(float(v), 2 * math.pi)     # 归到 [-π, π]
        return round(a, 6)

    def write_views(self, views):
        os.makedirs(DATA, exist_ok=True)
        tmp = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=DATA, delete=False) as f:
                tmp = f.name
                json.dump(views, f, ensure_ascii=False, indent=2, sort_keys=True)
            os.replace(tmp, self.views_file)
        finally:
            if tmp and os.path.exists(tmp):
                os.unlink(tmp)
        self.map_views = views

    def views_message(self):
        return json.dumps({'t': 'map_views', 'items': self.map_views})

    async def set_view(self, name, angle):
        if not isinstance(name, str) or not P.SAFE_NAME.fullmatch(name):
            return False, '地图名只能用字母、数字、下划线和减号'
        try:
            a = self.view_angle(angle)
        except ValueError as e:
            return False, str(e)
        views = dict(self.map_views)
        if abs(a) < 1e-6:
            views.pop(name, None)
        else:
            views[name] = a
        if views != self.map_views:
            try:
                self.write_views(views)
            except OSError as e:
                return False, f'保存显示方向失败：{e}'
            await self.send_all(self.views_message(), key='views')
        return True, ''

    # ---------- 禁行区（v1.1.8，11-23） ----------
    # 网页画线 / 画区域 → <名字>.keepout.json + keepout/<名字>.yaml 掩膜（server/keepout.py）。
    # 导航里由 filter_mask_server 发掩膜、KeepoutFilter 叠到全局和局部代价地图；地图本身和 AMCL 不受影响
    def keepout_message(self):
        return json.dumps({'t': 'keepout', 'items': self.keepouts}, ensure_ascii=False)

    def keepout_view(self):
        n = self.node
        ready = n.maskload_cli.service_is_ready()
        if ready and not self.keepout_ready and not self.keepout_loading and n.map_current:
            # 导航刚起来：launch 已按启动地图加载了掩膜，这里按网页认定的当前地图再加载一次，两边一定一致
            self.background(self.keepout_apply(n.map_current, '导航启动'))
        if not ready:
            self.keepout_live = None
        self.keepout_ready = ready
        return {'ready': ready, 'live': self.keepout_live, 'loading': self.keepout_loading}

    async def keepout_apply(self, name, why):
        """把 name 的掩膜加载进正在跑的导航；导航没开 / 旧镜像没有 filter_mask_server 就什么都不做。返回提示（空 = 没加载）。"""
        n = self.node
        if not name or not n.maskload_cli.service_is_ready():
            return ''
        self.keepout_loading = True
        try:
            path = await asyncio.to_thread(KO.ensure_mask, os.path.join(DATA, 'maps'), name)
            r = await self.aw(n.load_mask(path), 10.0)
            if r.result != 0:
                self.keepout_live = None
                n.event('ERROR', f'禁行区掩膜加载失败（{name}，错误码 {r.result}）：导航可能还在用旧的禁行区')
                return f'禁行区没有加载进导航（错误码 {r.result}）'
            self.keepout_live = name
            k = len(self.keepouts.get(name, []))
            n.event('INFO', f'禁行区已加载（{name}，{k} 个；{why}）')
            return f'导航已生效（{k} 个）' if k else '导航里的禁行区已清空'
        except Exception as e:
            self.keepout_live = None
            n.event('ERROR', f'禁行区掩膜加载失败（{name}）：{e!r}')
            return f'禁行区没有加载进导航（{e}）'
        finally:
            self.keepout_loading = False

    async def keepout_save(self, name, shapes, who):
        if not isinstance(name, str) or not P.SAFE_NAME.fullmatch(name):
            return False, '地图名不对'
        if name not in {m['name'] for m in self.maps}:
            return False, f'data/maps 里没有 {name}'
        old = self.keepouts.get(name, [])
        try:
            clean, _ = await asyncio.to_thread(KO.save, os.path.join(DATA, 'maps'), name, shapes, who)
        except ValueError as e:
            return False, str(e)
        except OSError as e:
            return False, f'禁行区没有保存：{e.strerror or e}'
        if clean:
            self.keepouts[name] = clean
        else:
            self.keepouts.pop(name, None)
        await self.send_all(self.keepout_message(), key='keepout')
        lines = sum(1 for s in clean if s['kind'] == 'line')
        self.node.event('INFO', f'禁行区已保存：{name}（{len(old)} → {len(clean)} 个，线 {lines}、区域 {len(clean) - lines}；{who}）')
        n = self.node
        live = n.map_current == name and self.prof('navigation') != 'stop'
        if live and n.maskload_cli.service_is_ready():
            note = await self.keepout_apply(name, '网页保存')
            return True, f'禁行区已保存，{note}' if note else '禁行区已保存'
        if live:
            return True, '禁行区已保存；这台的导航版本不支持禁行层（没有 filter_mask_server），需要更新导航镜像后才生效'
        return True, '禁行区已保存，下次用这张图导航时生效'

    def keepout_blocker(self, map_name, points):
        """目标 / 任务站点落在禁行范围里 → 原因（Nav2 会直接规划失败，先在网页上说清楚）。"""
        shapes = self.keepouts.get(map_name)
        if not shapes:
            return None
        for i, p in enumerate(points):
            try:
                k = KO.hit(shapes, float(p['x']), float(p['y']))
            except (KeyError, TypeError, ValueError):
                continue
            if k is not None:
                label = shapes[k].get('name') or f"第 {k + 1} 个禁行{'线' if shapes[k]['kind'] == 'line' else '区域'}"
                where = '目标点' if len(points) == 1 and 'name' not in p else f'第 {i + 1} 站'
                return f'{where}在「{label}」的禁行范围里（离线不到 {KO.INSCRIBED:.2f} m 或在区域内），换个位置'
        return None

    # ---------- 地图显示名（11-23） ----------
    # 只改网页上显示的名字（可以中文），data/maps 里的文件名不动：slam_toolbox / map_server / 云端调度 /
    # 任务 / 示教 / RTAB 库都还认文件名，所以正在导航用的图也能随时改。存 data/map_labels.json
    LABEL_MAX = 32

    @classmethod
    def label_error(cls, label):
        if len(label) > cls.LABEL_MAX:
            return f'显示名最多 {cls.LABEL_MAX} 个字'
        if any(ch in label for ch in '<>"\'\\/') or any(ord(ch) < 32 or 0x7f <= ord(ch) < 0xa0 for ch in label):
            return '显示名不能有 < > " \' \\ / 和控制字符'
        return None

    def write_labels(self, labels):
        tmp = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=DATA, delete=False) as f:
                tmp = f.name
                json.dump(labels, f, ensure_ascii=False, indent=2, sort_keys=True)
            os.replace(tmp, self.labels_file)
        finally:
            if tmp and os.path.exists(tmp):
                os.unlink(tmp)
        self.map_labels = labels

    def labels_message(self):
        return json.dumps({'t': 'map_labels', 'items': self.map_labels}, ensure_ascii=False)

    async def set_label(self, name, label, who):
        if not isinstance(name, str) or not P.SAFE_NAME.fullmatch(name):
            return False, '地图名不对'
        if name not in {m['name'] for m in self.maps}:
            return False, f'data/maps 里没有 {name}'
        if not isinstance(label, str):
            return False, '显示名不对'
        label = ' '.join(label.split())          # 去掉首尾空白，中间连续空白并成一个
        if label == name:
            label = ''
        if label:
            err = self.label_error(label)
            if err:
                return False, err
            dup = next((k for k, v in self.map_labels.items() if v == label and k != name), None)
            if dup:
                return False, f'「{label}」已经是 {dup} 的显示名了，换一个'
            if label in {m['name'] for m in self.maps} - {name}:
                return False, f'「{label}」是另一张地图的文件名，换一个'
        old = self.map_labels.get(name, '')
        if label == old:
            return True, '名字没变'
        labels = dict(self.map_labels)
        if label:
            labels[name] = label
        else:
            labels.pop(name, None)
        try:
            self.write_labels(labels)
        except OSError as e:
            return False, f'保存显示名失败：{e.strerror or e}'
        await self.send_all(self.labels_message(), key='labels')
        self.node.event('INFO', f'地图 {name} 显示名：{old or "（无）"} → {label or "（无，显示文件名）"}（{who}）')
        return True, f'已改名为「{label}」' if label else f'已恢复显示文件名 {name}'

    def routes_message(self):
        # resume：这个任务的断点（没有就是 null），网页和云端据此显示「继续任务」
        return json.dumps({'t': 'routes', 'items': [{**r, 'resume': self.resume_view(r)} for r in self.routes.values()]})

    # ---------- 任务接续（11-24） ----------
    @staticmethod
    def route_fp(record):
        """站点指纹：只看坐标和朝向（站名、备注、速度改了不影响接续）。"""
        pts = [(round(p['x'], 3), round(p['y'], 3), round(p['th'], 3)) for p in record['points']]
        return hashlib.sha1(json.dumps(pts).encode()).hexdigest()[:16]

    def resume_valid(self, rid, cp, now=None):
        r = self.routes.get(rid)
        now = time.time() if now is None else now
        return (r is not None and isinstance(cp, dict) and cp.get('fp') == self.route_fp(r)
                and type(cp.get('index')) is int and 0 <= cp['index'] < len(r['points'])
                and type(cp.get('loop')) is int and cp['loop'] >= 1 and type(cp.get('done')) is int
                and isinstance(cp.get('time'), (int, float)) and now - cp['time'] <= ROUTE_RESUME_MAX_AGE)

    def prune_resumes(self, write=False):
        # 正在跑的任务的断点不删（它的 time 每站都刷新）
        keep = {k: v for k, v in self.resumes.items() if self.resume_valid(k, v)}
        changed = len(keep) != len(self.resumes)
        self.resumes = keep
        if changed and write:
            self.write_resumes()
        return changed

    def write_resumes(self):
        tmp = None
        try:
            os.makedirs(DATA, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=DATA, delete=False) as f:
                tmp = f.name
                json.dump(self.resumes, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.resume_file)
        except OSError as e:
            self.node.event('WARN', f'任务断点没能写盘：{e}')
        finally:
            if tmp and os.path.exists(tmp):
                os.unlink(tmp)

    def push_routes(self):
        msg = self.routes_message()
        for ws in list(self.clients):
            self.tx_put(ws, msg, 'routes')

    def resume_view(self, record):
        """推给网页 / 云端的断点。running 的任务（status = active）不给：只有停下来的才能接续。"""
        cp = self.resumes.get(record['id'])
        if not cp or cp.get('status') == 'active' or not self.resume_valid(record['id'], cp):
            return None
        n, i, back = len(record['points']), cp['index'], bool(cp.get('back'))
        loops = cp.get('loops')
        nxt = Server.route_next(n, loops, (i, cp['loop'], back))
        return {'point': i + 1, 'count': n, 'back': back, 'loop': cp['loop'], 'loops': loops,
                'roundTrip': loops is not None, 'done': cp['done'], 'total': cp.get('total'),
                # 这一圈已经到过的站（1 起）：back = 正在回 1 号点，2..n 都到过了；第 2 圈起 1 号点是上一圈收尾时到的
                'finished': list(range(2, n + 1)) if back else list(range(1, i + 1)),
                'status': cp.get('status'), 'reason': cp.get('reason', ''), 'time': cp['time'],
                'expires': cp['time'] + ROUTE_RESUME_MAX_AGE, 'pose': cp.get('pose'),
                'pointName': record['points'][i].get('name', ''),
                'canSkip': nxt is not None,
                'skipPoint': None if nxt is None else nxt[0] + 1}

    def resume_checkpoint(self, route, status='active', reason=''):
        """记断点。还没到过任何一站（done = 0）就不记：那和从头出发一样。"""
        rid = route['record']['id']
        if status == 'succeeded' or route['done'] <= 0:
            if self.resumes.pop(rid, None) is not None:
                self.write_resumes()
                self.push_routes()
            return
        pose = None
        if status != 'active':
            here = self.node.lookup(BASE_FRAME)
            if here and self.node.fixed == 'map':
                pose = [round(here[0], 3), round(here[1], 3), round(here[2], 3)]
        prev = self.resumes.get(rid)
        self.resumes[rid] = {'fp': self.route_fp(route['record']), 'name': route['record']['name'],
                             'index': route['index'], 'loop': route['loop'], 'back': route['back'],
                             'done': route['done'], 'total': route['total'], 'loops': route['loops'],
                             'status': status, 'reason': reason, 'time': time.time(), 'pose': pose}
        self.write_resumes()
        if status != 'active' or (prev and prev.get('status') != 'active'):
            self.push_routes()

    def route_running(self):
        return self.route_job is not None and not self.route_job.done()

    def route_blocker(self, ws, route):
        n = self.node
        client = self.clients.get(ws)
        if client is None or ws.closed or time.monotonic() - client['last_ping'] > self.heartbeat:
            return '控制方网页连接已断开'
        if self.estop or not n.is_armed():
            return '急停或底盘未解锁'
        reason = self.ctrl_blocker()
        if reason:
            return reason
        if self.busy or self.map_loading or self.motion_stops or self.prof('navigation') != 'run' or not n.nav.server_is_ready():
            return '导航不可用'
        if not n.healthy or time.monotonic() - n.healthy_t >= STALE or not n.lookup(BASE_FRAME) or n.fixed != 'map':
            return '地图定位或里程计不可用'
        if n.map_current != route['map']:
            return f"请先切换到地图 {route['map']}"
        return None

    def cancel_late_goal(self, future):
        # 请求超时后仍可能被接受。只取消这个迟到的目标。
        try:
            handle = future.result()
            if handle.accepted:
                handle.cancel_goal_async()
        except Exception as e:
            self.node.event('ERROR', f'取消迟到的目标失败：{e}')

    async def plan_route(self, record, start=None, stopped=lambda: False):
        n, points = self.node, record['points']
        legs = list(zip(points, points[1:]))
        if start is not None:
            legs.insert(0, (start, points[0]))
        first = 1 if start is not None else 2
        deadline = time.monotonic() + 30.0
        paths = []
        for i, (a, b) in enumerate(legs, first):
            if stopped():
                return paths, i, '路线检查已中断'
            if self.busy or self.map_loading or n.map_current != record['map'] or not n.planner.server_is_ready():
                return paths, i, '当前地图的 Nav2 规划器不可用'
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return paths, i, '路线检查超时'
            handle = None
            request = n.compute_path(a, b)
            try:
                handle = await self.aw(request, min(2.0, remaining))
                if not handle.accepted:
                    return paths, i, 'Nav2 规划器拒绝了请求'
                reply = await self.aw(handle.get_result_async(), min(3.0, max(.01, deadline - time.monotonic())))
                if reply.status != GoalStatus.STATUS_SUCCEEDED or not reply.result.path.poses:
                    return paths, i, 'Nav2 找不到可行路径'
            except asyncio.TimeoutError:
                if handle is None:
                    request.add_done_callback(self.cancel_late_goal)
                else:
                    handle.cancel_goal_async()
                return paths, i, 'Nav2 规划超时'
            if stopped() or self.map_loading or self.busy or n.map_current != record['map']:
                return paths, i, '路线检查已中断'
            poses = reply.result.path.poses
            step = max(1, math.ceil(len(poses) / 180))
            sampled = poses[::step]
            if sampled[-1] is not poses[-1]:
                sampled.append(poses[-1])
            paths.append({'to': i, 'points': [[p.pose.position.x, p.pose.position.y] for p in sampled]})
        return paths, None, ''

    async def check_route(self, ws, d):
        record = self.route_record({**d, 'name': '规划检查'}, None)
        if self.checking_route or self.route_running() or self.goal_pending or self.node.nav_active() or self.teleop is not None or self.prof('patrol') != 'stop':
            return False, '请先结束当前导航、巡逻、遥控或路线检查'
        if self.prof('navigation') == 'stop':
            return False, '导航没有运行：出发前自检时会用 Nav2 检查路径'
        if self.busy or self.map_loading or self.node.map_current != record['map'] or not self.node.planner.server_is_ready():
            return False, '当前地图的 Nav2 规划器未就绪，路线尚未验证'
        self.checking_route = True
        try:
            paths, failed, reason = await self.plan_route(record, stopped=lambda: ws.closed or self.estop or self.node.nav_active())
            message = '全部站间路径可规划' if failed is None else f'第 {failed - 1} → {failed} 站：{reason}'
            return failed is None, message, {'paths': paths, 'failed': failed, 'simulated': self.simulated}
        finally:
            self.checking_route = False

    def start_route(self, ws, route_id, round_trip=False, loops=1, resume=None):
        """resume：None = 从头出发；'continue' = 从断点那一站接着跑；'skip' = 跳过断点那一站，从下一站接着跑。
        接续时圈数、已完成站数都用断点里的，round_trip / loops 参数不看。"""
        route = self.routes.get(route_id)
        if route is None:
            return False, '找不到这个导航任务'
        if resume not in (None, 'continue', 'skip'):
            return False, 'resume 只能是 continue 或 skip'
        cp = None
        if resume:
            cp = self.resumes.get(route_id)
            if not cp or cp.get('status') == 'active' or not self.resume_valid(route_id, cp):
                return False, '这个任务没有可以接续的断点（任务跑完了、站点改过、或者断点超过 24 小时）'
            round_trip, loops = cp.get('loops') is not None, cp.get('loops') if cp.get('loops') is not None else 1
            state, done = (cp['index'], cp['loop'], bool(cp.get('back'))), cp['done']
            if resume == 'skip':
                state = self.route_next(len(route['points']), cp.get('loops'), state)
                if state is None:
                    return False, '断点是最后一站：跳过就没有剩下的站了'
                done += 1
        if type(round_trip) is not bool or type(loops) is not int or not 0 <= loops <= 1000:
            return False, '循环圈数需要是 0–1000 的整数（0 = 一直循环）'
        if (self.route_running() or self.goal_pending or self.node.nav_active()
                or self.prof('patrol') != 'stop' or self.teleop is not None):
            return False, '请先结束当前导航、巡逻或遥控'
        reason = self.route_blocker(ws, route)
        if reason:
            return False, reason
        self.owner = ws
        self.auto_lie, self.stand_hold = True, False   # 任务结束后空闲 IDLE_LIE 秒自动趴下
        n = len(route['points'])
        self.route = {'record': route, 'index': 0, 'status': 'active', 'started': time.monotonic(),
                      'message': '', 'stop': asyncio.Event(), 'goal': None, 'checking': True,   # checking：出发前清代价地图中
                      # 循环：loops 为 None 表示单程；done = 已到达的站数，用来算进度。一圈到 n 站（含回到起点）
                      'loops': loops if round_trip else None, 'loop': 1, 'back': False, 'done': 0,
                      'total': n if not round_trip else None if loops == 0 else 1 + loops * n,
                      'target': route['points'][0], 'resume': None}
        how = '' if not round_trip else '，一直循环' if loops == 0 else f'，循环 {loops} 圈'
        if cp:
            i, loop, back = state
            self.route.update(index=i, loop=loop, back=back, done=done, total=cp.get('total'), resume=state,
                              target=self.route_pose(route['points'], i, back))
            lap = f"第 {loop} 圈" + ('' if not loops else f' / 共 {loops} 圈') + '，' if round_trip else ''
            self.route_job = asyncio.create_task(self.run_route(ws, self.route))
            where = '回 1 号点' if back else f'第 {i + 1} 站'
            skipped = f"（跳过第 {cp['index'] + 1} 站）" if resume == 'skip' else ''
            msg = f"接续任务：{route['name']}，{lap}从{where}继续{skipped}，之前已完成 {cp['done']} 站"
            self.node.event('INFO', msg)
            return True, msg
        if self.resumes.pop(route_id, None) is not None:   # 从头出发：旧断点作废
            self.write_resumes()
            self.push_routes()
        self.route_job = asyncio.create_task(self.run_route(ws, self.route))
        return True, f"开始任务：{route['name']}{how}"

    async def start_route_armed(self, ws, d):
        """网页「出发」：先把这次的速度上限（vmax / wmax，导航没开时在网页上选好的）设给 cmd_bridge，
        arm=true 时再解锁出发；出发失败就把刚解的锁再上回去。"""
        n, armed_here = self.node, False
        record = self.routes.get(d.get('route_id'))
        ok, message = await self.check_ordinary_taught_binding(record['map'] if record else n.map_current)
        if not ok:
            return False, message
        if record:
            why = self.keepout_blocker(record['map'], record['points'])
            if why:
                return False, why
        for key, top in (('vmax', 1.2), ('wmax', 1.5)):
            v = d.get(key)
            if v is None:
                continue
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 < v <= top:
                return False, f'速度上限 {key} 需要在 0–{top} 之间'
            if n.params.get(key) is not None and abs(n.params[key] - v) < 1e-6:
                continue
            if not n.pset_cli.service_is_ready():
                return False, 'cmd_bridge 没有运行，无法设置速度上限'
            r = await self.aw(n.set_param(key, v), 2.0)
            res = r.results[0]
            if not res.successful:
                return False, f'速度上限没有生效：{res.reason}'
            n.params[key] = float(v)
            n.event('INFO', f'{BRIDGE} {PARAMS[key]} = {float(v):.2f}（出发前）')
        if d.get('arm') and not n.is_armed():
            if self.estop:
                return False, '急停中：先解除急停'
            if self.prof('navigation') != 'run' or not n.arm_cli.service_is_ready():
                return False, '导航没有运行（cmd_bridge 不可用），无法解锁'
            self.owner = ws          # 先认控制方：解锁后这个网页断线会自动上锁
            r = await self.aw(n.call_arm(True), 2.0)
            n.event('WARN' if r.success else 'ERROR', f"启动任务前自动解锁：{r.message}（{self.remote(ws)}）")
            if not r.success:
                return False, f'解锁失败：{r.message}'
            n.armed, n.armed_t, armed_here = True, time.monotonic(), True
        result = self.start_route(ws, d.get('route_id'), d.get('round_trip', False), d.get('loops', 1), d.get('resume'))
        if armed_here and not result[0]:
            try:
                r = await self.aw(n.call_arm(False), 2.0)
                if r.success:
                    n.armed, n.armed_t = False, time.monotonic()
            except Exception as e:
                n.event('ERROR', f'任务没有启动，重新上锁失败：{e}')
        return result

    def finish_route(self, route, status, message):
        if status == 'aborted' and route['status'] == 'active':
            self.incident('nav_fail', f"巡逻任务「{route['record']['name']}」{message}")   # 先记，任务信息还是执行中的
        route.update(status=status, message=message, ended=time.monotonic())
        route['stop'].set()
        try:
            self.resume_checkpoint(route, status, message)
        except Exception as e:   # 记断点出错不能影响停车
            self.node.event('WARN', f'任务断点没记上：{e!r}')
        self.node.event('ERROR' if status == 'aborted' else 'INFO', message)

    def stop_route(self, reason):
        if self.route and self.route['status'] == 'active':
            self.finish_route(self.route, 'canceled', reason)

    async def clear_after_pose(self):
        """设置初始位姿后清代价地图：先等 AMCL 用新位姿更新 map→odom，再清，重新标记时障碍就落在对的位置。"""
        n = self.node
        await asyncio.sleep(0.5)
        futs = n.clear_costmaps()
        if not futs:
            return
        try:
            await asyncio.gather(*(self.aw(f, 2.0) for f in futs))
            n.event('INFO', '位姿已更新，已清除代价地图里的旧障碍标记')
        except Exception as e:
            n.event('WARN', f'设置位姿后清除代价地图没有完成：{e!r}')

    # ---------- 自动定位 ----------
    async def auto_localize(self, who, on_start=False):
        """用最近几帧 scan 在整张地图上找机器人（globloc.py 子进程），可信就发给 AMCL。

        返回 (ok, 提示, 结果)。不可信（分数低 / 有歧义）时不发位姿，只报告，让人手动给。
        机器人必须静止；导航目标执行中拒绝（定位跳变会让正在跑的规划失效）。
        """
        n = self.node
        if self.autoloc_busy:
            return False, '自动定位正在进行'
        if 'amcl' not in n.get_node_names():
            return False, '定位（AMCL）没有运行，无法自动定位'
        if n.nav_active():
            return False, '导航进行中：先取消导航再自动定位'
        if n.map_raw is None:
            return False, '还没有收到地图（或地图原点带旋转，暂不支持）'
        self.autoloc_busy = True
        self.autoloc = {'state': 'run', 'seq': self.autoloc['seq'], 'map': n.map_current}
        path = None
        try:
            # 先等站稳、静止（11-12：站起来后立刻定位总报「机器人在动」，自检卡住）
            scans, why, w0 = None, '', time.monotonic()
            while time.monotonic() - w0 < AUTOLOC_SETTLE_WAIT:
                scans, why = self.autoloc_scans()
                if scans is not None:
                    break
                await asyncio.sleep(0.2)
            if scans is None:
                raise RuntimeError(f'{AUTOLOC_SETTLE_WAIT:.0f} s 内没等到机器人站稳静止（{why}）')
            odom = self.fresh_odom()   # 静止时的里程计位姿：和别处的结果配对用（没有就不配对）
            if n.map_raw is None:
                raise RuntimeError('地图没有了')
            grid, res, ox, oy = n.map_raw
            map_name, map_ver = n.map_current, n.map_ver
            self.autoloc['map'] = map_name
            t0 = time.monotonic()
            fd, path = tempfile.mkstemp(prefix='go2w_autoloc_', suffix='.npz')
            os.close(fd)
            np.savez(path, grid=grid, res=res, origin=np.array([ox, oy]), pts=np.concatenate(scans).astype(np.float32))
            script = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'globloc.py')
            # nice：别和 FAST-LIO / AMCL 抢 CPU
            proc = await asyncio.create_subprocess_exec('nice', '-n', '10', sys.executable, script, path,
                                                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            try:
                out, err = await asyncio.wait_for(proc.communicate(), AUTOLOC_TIMEOUT)
            except asyncio.TimeoutError:
                proc.kill()
                raise RuntimeError(f'{AUTOLOC_TIMEOUT:.0f} s 内没有算完')
            if proc.returncode != 0:
                lines = err.decode(errors='replace').strip().splitlines()
                raise RuntimeError(lines[-1] if lines else f'退出码 {proc.returncode}')
            r = json.loads(out.decode().strip().splitlines()[-1])
            if 'error' in r:
                raise RuntimeError(r['error'])
        except Exception as e:
            msg = f'自动定位失败：{e}'
            self.autoloc = {'state': 'fail', 'seq': self.autoloc['seq'] + 1, 'map': n.map_current, 'msg': msg}
            n.event('ERROR', msg)
            return False, msg
        finally:
            self.autoloc_busy = False
            if path:
                try:
                    os.unlink(path)
                except OSError:
                    pass
        took = time.monotonic() - t0
        map_name = n.map_current or map_name
        where = f"({r['x']:.2f}, {r['y']:.2f}, {math.degrees(r['th']):.0f}°)"
        quality = f"分数 {r['score']:.2f}，次优/最优 {r['ratio']:.2f}，{took:.1f} s"
        res_ = {k: r[k] for k in ('x', 'y', 'th', 'score', 'ratio', 'second_pose')}
        # 只看栅格本身：启动时地图名（map_current）比栅格晚几秒才拿到，map_server 也可能重发同一张图
        now_raw = n.map_raw
        if n.map_ver != map_ver and (now_raw is None or now_raw[1:] != (res, ox, oy) or not np.array_equal(now_raw[0], grid)):
            msg = '自动定位期间地图变了，结果作废'
            self.autoloc = {'state': 'fail', 'seq': self.autoloc['seq'] + 1, 'map': map_name, 'msg': msg}
            n.event('WARN', msg)
            return False, msg
        if on_start and self.pose_set_t > t0:   # 算的时候用户已经手动给了位置：以手动为准
            self.autoloc = {'state': 'idle', 'seq': self.autoloc['seq']}
            n.event('INFO', f'自动定位结果 {where} 没有使用：期间已手动设置了初始位姿')
            return False, '期间已手动设置初始位姿'
        entry = self.autoloc_remember(map_name, odom, r)
        pair = None if r['ok'] else self.autoloc_pair(entry)
        if not r['ok'] and pair is None:
            ambiguous = r['score'] >= 0.75 and r['ratio'] > AUTOLOC_PAIR_RATIO
            why = '有多个相似位置，分不清' if ambiguous else '匹配分数太低（不在这张地图上，或环境变化太大）'
            left = AUTOLOC_MOVE_MAX - self.autoloc_moves
            hint = '可以点「移动后重试」，或手动给初始位置' if left > 0 else '请手动给初始位置'
            msg = f'自动定位不可信，没有使用：{why}。最佳 {where}，{quality}。{hint}'
            self.autoloc = {'state': 'fail', 'seq': self.autoloc['seq'] + 1, 'map': map_name, 'msg': msg, **res_}
            n.event('WARN', msg)
            return False, msg, res_
        n.publish_initial_pose(r['x'], r['y'], r['th'], fine=True)
        self.pose_set_t = time.monotonic()
        self.autoloc_reset()
        if pair is not None:
            prev, sep, turn, dd, dth = pair
            quality += (f"；和前一次（分数 {prev['score']:.2f}，次优/最优 {prev['ratio']:.2f}）相隔 {sep:.2f} m / "
                        f"{math.degrees(turn):.0f}°，两次结果相差 {dd:.2f} m / {math.degrees(dth):.1f}°，一致")
            res_['pair'] = True
        msg = f"自动定位成功{'（两处结果一致）' if pair is not None else ''} {where}（{quality}）"
        self.autoloc = {'state': 'ok', 'seq': self.autoloc['seq'] + 1, 'map': map_name, 'msg': msg, **res_}
        n.event('INFO', f'{msg}（{who}）')
        asyncio.ensure_future(self.clear_after_pose())
        return True, msg, res_

    def autoloc_scans(self):
        """能用来自动定位的 scan：站稳 AUTOLOC_SETTLE 秒之后收到的、静止的、最近 2 s 内的，至少 3 帧。
        返回 (点列表, '') 或 (None, 还差什么)。"""
        n, now = self.node, time.monotonic()
        since = 0.0
        if self.ctrl is not None:
            s, ready, _ = self.ctrl_get()
            if self.ctrl_op:
                return None, '正在站起 / 趴下'
            if s in ('FixStand', 'Velocity'):   # 趴着（Passive）也让定位：重定位标签里可能就是趴着在看
                st = self.ctrl.steady_since()
                if not ready or st is None:
                    return None, '还没站稳'
                if now - st < AUTOLOC_SETTLE:
                    return None, '刚站起来，等机身稳定'
                since = st + AUTOLOC_SETTLE
        recent = [(p, still) for t, p, still in list(n.scans_base) if now - t < 2.0 and t >= since]
        if len(recent) < 3:
            return None, '激光数据不足'
        scans = [p for p, still in recent if still]
        if len(scans) < 3:
            return None, '机器人在动'
        return scans, ''

    def fresh_odom(self, max_age=0.5):
        o = self.node.odom_pose
        return o[:3] if o is not None and time.monotonic() - o[3] < max_age else None

    def autoloc_reset(self):
        """定位成功 / 手动给了位置 / 换档位 / 从趴着站起来：之前的结果不再拿来配对，移动次数清零。"""
        self.autoloc_hist, self.autoloc_moves = [], 0

    def autoloc_remember(self, map_name, odom, r):
        """记下这次结果，以及它对应的 map→odom（map 位姿 ∘ odom 位姿⁻¹）。没有里程计就不记。"""
        if odom is None:
            return None
        ox, oy, oth = odom
        th = wrap(r['th'] - oth)
        c, s = math.cos(th), math.sin(th)
        e = {'map': map_name, 't': time.monotonic(), 'stood': self.stood_seq, 'odom': odom,
             'tmo': (r['x'] - (c * ox - s * oy), r['y'] - (s * ox + c * oy), th),
             'score': r['score'], 'ratio': r['ratio']}
        now = time.monotonic()
        self.autoloc_hist = [h for h in self.autoloc_hist
                             if h['map'] == map_name and h['stood'] == self.stood_seq and now - h['t'] < AUTOLOC_HIST_TTL][-5:]
        self.autoloc_hist.append(e)
        return e

    def autoloc_pair(self, e):
        """在之前挪开过的结果里找一个和 e 一致的：返回 (那一次, 相隔米数, 相隔角度, map→odom 位置差, 角度差) 或 None。"""
        if e is None or e['score'] < AUTOLOC_PAIR_SCORE or e['ratio'] > AUTOLOC_PAIR_RATIO:
            return None
        for p in reversed(self.autoloc_hist[:-1]):
            if p['score'] < AUTOLOC_PAIR_SCORE or p['ratio'] > AUTOLOC_PAIR_RATIO:
                continue
            sep = math.hypot(e['odom'][0] - p['odom'][0], e['odom'][1] - p['odom'][1])
            turn = abs(wrap(e['odom'][2] - p['odom'][2]))
            if sep < AUTOLOC_PAIR_SEP and turn < AUTOLOC_PAIR_TURN:
                continue   # 基本没动：同一批 scan，不独立
            dd = math.hypot(e['tmo'][0] - p['tmo'][0], e['tmo'][1] - p['tmo'][1])
            dth = abs(wrap(e['tmo'][2] - p['tmo'][2]))
            if dd <= AUTOLOC_PAIR_TOL and dth <= AUTOLOC_PAIR_TOL_TH:
                return p, sep, turn, dd, dth
        return None

    def latest_scan(self, max_age=0.5):
        s = list(self.node.scans_base)
        return s[-1][1] if s and time.monotonic() - s[-1][0] < max_age else None

    @staticmethod
    def autoloc_room(pts, kind, sign, reach):
        """base_footprint 下的点 pts：往 sign 方向直走 reach 米（kind='line'）/ 原地转（kind='turn'，reach=半径）有没有空间。"""
        if kind == 'turn':
            return not bool(np.any(np.hypot(pts[:, 0], pts[:, 1]) < reach))
        hl, hw = AUTOLOC_FOOT
        x = pts[:, 0] * sign
        return not bool(np.any((x > hl - 0.05) & (x < hl + reach) & (np.abs(pts[:, 1]) < hw + 0.10)))

    async def autoloc_move(self, ws):
        """自动定位失败后：小范围挪一次（用户在网页上确认过），停稳后再自动定位。
        前进 0.3 m → 前面不够空就后退 0.3 m → 都不行就原地转 45°。没解锁就临时解锁，结束后恢复上锁。"""
        n, who = self.node, self.remote(ws)
        a = self.autoloc
        if self.autoloc_busy:
            return False, '自动定位正在进行'
        if a.get('state') != 'fail' or a.get('map') != n.map_current or not n.map_current:
            return False, '只有这张地图上自动定位失败后才能移动重试'
        if self.autoloc_moves >= AUTOLOC_MOVE_MAX:
            return False, f'已经移动重试 {AUTOLOC_MOVE_MAX} 次还没定位上：请手动给初始位置'
        if self.estop:
            return False, '急停中：先解除急停'
        reason = self.ctrl_blocker()
        if reason:
            return False, reason
        if (n.nav_active() or self.route_running() or self.goal_pending or self.teleop is not None
                or self.checking_route or self.ctrl_op or self.prof('patrol') != 'stop'):
            return False, '有导航、任务、遥控或巡逻在进行：先停掉再移动重试'
        if not n.arm_cli.service_is_ready():
            return False, '速度桥没有运行（刚切换档位时要等几秒）'
        if self.fresh_odom() is None:
            return False, '没有里程计：无法控制移动距离'
        pts = self.latest_scan()
        if pts is None:
            return False, '没有最新的激光数据：无法检查周围空间'
        reach = AUTOLOC_MOVE_DIST + AUTOLOC_MOVE_MARGIN
        if self.autoloc_room(pts, 'line', 1, reach):
            kind, sign, what = 'line', 1, f'前进 {AUTOLOC_MOVE_DIST:.1f} m'
        elif self.autoloc_room(pts, 'line', -1, reach):
            kind, sign, what = 'line', -1, f'后退 {AUTOLOC_MOVE_DIST:.1f} m'
        elif self.autoloc_room(pts, 'turn', 1, AUTOLOC_TURN_CLEAR):
            kind, sign, what = 'turn', 1, f'原地左转 {math.degrees(AUTOLOC_MOVE_TURN):.0f}°'
        else:
            return False, '周围空间不够（前后都不到 0.55 m，转身半径 0.65 m 内也有东西）：请手动给初始位置'
        self.autoloc_busy = True
        self.owner = ws   # 发起的网页断开 / 心跳丢失 → lose_owner 上锁 → 下面的循环看到没解锁就停
        self.autoloc = {**a, 'state': 'move', 'msg': f'移动后重试：{what}…'}
        armed_here, err = False, None
        try:
            if not n.is_armed():
                r = await self.aw(n.call_arm(True), 2.0)
                if not r.success:
                    err = f'解锁失败：{r.message}'
                else:
                    n.armed, n.armed_t, armed_here = True, time.monotonic(), True
            if err is None:
                n.event('WARN', f'自动定位移动重试：{what}（{who}{"，临时解锁" if armed_here else ""}）')
                err, done = await self.autoloc_drive(ws, kind, sign)
        except Exception as e:
            err = f'移动出错：{e!r}'
        finally:
            n.publish_cmd(0.0, 0.0, 0.0)
            self.autoloc_moves += 1
            if armed_here:
                t0 = time.monotonic()
                while self.moving() and time.monotonic() - t0 < 2.0:   # 停稳再上锁
                    await asyncio.sleep(0.1)
                try:
                    r = await self.aw(n.call_arm(False), 2.0)
                    if r.success:
                        n.armed, n.armed_t = False, time.monotonic()
                    else:
                        n.event('ERROR', f'移动重试后恢复上锁失败：{r.message}')
                except Exception as e:
                    n.event('ERROR', f'移动重试后恢复上锁失败：{e!r}')
            self.autoloc_busy = False
        if err:
            msg = f'移动重试没有完成：{err}。可以再试，或手动给初始位置'
            self.autoloc = {**a, 'state': 'fail', 'seq': self.autoloc['seq'] + 1, 'msg': msg}
            n.event('WARN', msg)
            return False, msg
        n.event('INFO', f'自动定位移动重试：{done}，停稳后重新自动定位')
        return await self.auto_localize(f'{who}，移动后重试')

    async def autoloc_drive(self, ws, kind, sign):
        """按里程计闭环：直走 AUTOLOC_MOVE_DIST 或转 AUTOLOC_MOVE_TURN。返回 (错误, 实际做了什么)。"""
        n = self.node
        o0 = self.fresh_odom()
        t0, stops0 = time.monotonic(), self.motion_stops
        v, w = (sign * AUTOLOC_MOVE_V, 0.0) if kind == 'line' else (0.0, sign * AUTOLOC_MOVE_W)
        while True:
            if self.estop:
                return '急停', None
            if self.motion_stops > stops0 or n.is_armed() is False:
                return '底盘被上锁', None
            if self.owner is not ws:
                return '发起的网页断开了', None
            if self.teleop is not None or n.nav_active():
                return '有遥控或导航指令插进来', None
            s, ready, _ = self.ctrl_get()
            if self.ctrl is not None and (s != 'Velocity' or not ready):
                return f'机器狗不在行走状态（运控状态 {s}）', None
            o = self.fresh_odom()
            if o is None:
                return '里程计中断', None
            dist = math.hypot(o[0] - o0[0], o[1] - o0[1])
            turn = abs(wrap(o[2] - o0[2]))
            if kind == 'line' and dist >= AUTOLOC_MOVE_DIST - 0.02:
                return None, f"{'前进' if sign > 0 else '后退'} {dist:.2f} m"
            if kind == 'turn' and turn >= AUTOLOC_MOVE_TURN - math.radians(3):
                return None, f'原地转 {math.degrees(turn):.0f}°'
            pts = self.latest_scan()
            if pts is None:
                return '激光数据中断', None
            room = (self.autoloc_room(pts, 'line', sign, AUTOLOC_MOVE_STOP) if kind == 'line'
                    else self.autoloc_room(pts, 'turn', sign, AUTOLOC_TURN_CLEAR - 0.10))
            if not room:
                return (f"运动方向有东西靠近，已停（走了 {dist:.2f} m）" if kind == 'line'
                        else f'转身范围内有东西靠近，已停（转了 {math.degrees(turn):.0f}°）'), None
            if time.monotonic() - t0 > AUTOLOC_MOVE_TIMEOUT:
                return (f'{AUTOLOC_MOVE_TIMEOUT:.0f} s 内没有完成（走了 {dist:.2f} m，转了 {math.degrees(turn):.0f}°）'), None
            n.publish_cmd(v, 0.0, w)
            await asyncio.sleep(0.1)

    async def auto_localize_on_start(self, mode):
        """定位 / 导航档位刚启动：等地图和 scan 到齐、机器人静止，自动定位一次。"""
        n, t0 = self.node, time.monotonic()
        await asyncio.sleep(2.0)   # AMCL 刚激活，scan 刚开始出
        while time.monotonic() - t0 < 30:
            if self.prof(mode) == 'stop' or self.pose_set_t > t0:
                return
            now = time.monotonic()
            fresh = sum(1 for t, _, still in list(n.scans_base) if now - t < 2.0 and still)
            if n.map_raw is not None and n.map_current and fresh >= 3 and 'amcl' in n.get_node_names():
                break
            await asyncio.sleep(1.0)
        else:
            n.event('WARN', '启动后 30 s 内没等到地图 / 激光，没有自动定位，请手动给初始位置')
            return
        n.event('INFO', f'{self.MODE_NAME[mode]}已启动，开始自动定位（机器人保持静止）')
        await self.auto_localize('启动后自动', on_start=True)

    async def clear_before_start(self):
        """出发前清代价地图，再等一下让障碍层按当前点云重新标记。清不了只记日志，不拦着出发。"""
        n = self.node
        futs = n.clear_costmaps()
        if not futs:
            n.event('WARN', '代价地图清除服务不可用，没有清除旧的障碍标记')
            return
        try:
            await asyncio.gather(*(self.aw(f, 2.0) for f in futs))
        except Exception as e:
            n.event('WARN', f'清除代价地图没有完成：{e!r}')
            return
        await asyncio.sleep(COSTMAP_REFILL)
        n.event('INFO', '出发前已清除代价地图里的旧障碍标记')

    def through_passed(self, ws, route, seg, k):
        """连续通过：seg 里第 k 站经过了。记进度、写断点，下一站成为当前目标。"""
        n, points = self.node, route['record']['points']
        (i, loop, back), pt = seg[k][0], seg[k][1]
        route['done'] += 1
        n.event('INFO', f"经过第 {i + 1} 站{'（回到 1 号点）' if back else ''} ({pt['x']:.2f}, {pt['y']:.2f})，不停车")
        (j, loop2, back2), q = seg[k + 1][0], seg[k + 1][1]
        route.update(index=j, loop=loop2, back=back2, target=q)
        self.resume_checkpoint(route)
        reason = self.route_blocker(ws, route['record'])
        if reason:
            raise RuntimeError(reason)

    async def run_through(self, ws, route, state):
        """连续通过（v1.1.11）：从 state 起一段一段跑。返回 None = 全部跑完；'stop' = 被取消（已处理）；
        状态元组 = 多点导航用不了，从这一站起退回一站一站走。"""
        n, record = self.node, route['record']
        points, loops = record['points'], route['loops']
        total = len(points)
        if not os.path.isfile(THROUGH_BT):
            n.event('WARN', f'找不到多点导航行为树 {THROUGH_BT}，这次按一站一站走')
            return state
        route['through'] = True
        fake, fake_state = 0, None
        while state:
            if route['stop'].is_set():
                return 'stop'
            # 这一段：从 state 起，到停留站 / 终点 / ROUTE_SEG_MAX 站为止
            seg, s = [], state
            while s:
                i, loop, back = s
                nxt = self.route_next(total, loops, s)
                dwell = points[i].get('dwell', 0) if nxt is not None else 0
                seg.append((s, self.route_pose(points, i, back), dwell, nxt))
                if dwell or nxt is None or len(seg) >= ROUTE_SEG_MAX:
                    break
                s = nxt
            # 这段的终点就在狗旁边（环线回到出发位置）：控制器的 goal checker 只看路径终点，一开始就满足，
            # 同一拍报到达、中间站一个没走（11-25 bag go2w-01_1125_114120）。终点先拿掉，这段跑到它前一站，
            # 拿掉的站下一段再单独发（那时狗已经走远了）
            here = n.lookup(BASE_FRAME) if n.fixed == 'map' else None
            if here and len(seg) > 1:
                cut = seg[-1]
                while len(seg) > 1 and math.hypot(here[0] - seg[-1][1]['x'], here[1] - seg[-1][1]['y']) <= ROUTE_THROUGH_NEAR_END:
                    seg.pop()
                if seg[-1] is not cut:
                    d = math.hypot(here[0] - cut[1]['x'], here[1] - cut[1]['y'])
                    n.event('INFO', f'这段的终点第 {cut[0][0] + 1} 站离狗只有 {d:.2f} m（Nav2 会当场判到达），'
                                    f'先跑到第 {seg[-1][0][0] + 1} 站，后面的站再单独发')
            poses = []
            for k, (_, pt, dwell, nxt) in enumerate(seg):
                last = k == len(seg) - 1
                if last and (dwell or nxt is None):
                    th = pt['th']                       # 要停下来的站：站点自己的朝向
                else:                                   # 途经点 / 分段处：面向下一站
                    q = seg[k + 1][1] if not last else self.route_pose(points, nxt[0], nxt[2])
                    th = math.atan2(q['y'] - pt['y'], q['x'] - pt['x'])
                poses.append((pt['x'], pt['y'], th))
            i, loop, back = state
            route.update(index=i, loop=loop, back=back, target=seg[0][1], goal=None)
            self.resume_checkpoint(route)
            reason = self.route_blocker(ws, record)
            if reason:
                raise RuntimeError(reason)
            here = n.lookup(BASE_FRAME)
            length = sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip([here or poses[0]] + poses, poses))
            t_send = time.monotonic()
            request = n.send_through(poses, THROUGH_BT)
            try:
                handle = await self.aw(request, 3.0)
            except asyncio.TimeoutError:
                request.add_done_callback(self.cancel_late_goal)
                raise RuntimeError('Nav2 接收多点目标超时') from None
            if not handle.accepted:
                n.event('WARN', 'Nav2 拒绝多点目标，这次按一站一站走')
                return state
            if route['stop'].is_set():
                await self.aw(handle.cancel_goal_async(), 2.0)
                return 'stop'
            end = poses[-1]
            gid = bytes(handle.goal_id.uuid)
            result = n.track_goal(handle, end, 'route')
            route['goal'] = dict(zip(('x', 'y', 'th'), end))
            a, b = seg[0][0], seg[-1][0]
            span = f'{a[0] + 1}' if len(seg) == 1 else f'{a[0] + 1}–{b[0] + 1}'
            n.event('INFO', f"连续通过：第 {span} 站（{len(seg)} 站，约 {length:.1f} m）"
                            f"{'，到了停留' if seg[-1][2] else ''}")
            stop = asyncio.create_task(route['stop'].wait())
            done = asyncio.create_task(self.aw(result, 120.0 + length / 0.1))
            k, planned, left = 0, False, None
            try:
                while True:
                    await asyncio.wait((stop, done), timeout=0.1, return_when=asyncio.FIRST_COMPLETED)
                    if route['stop'].is_set():
                        await self.aw(handle.cancel_goal_async(), 2.0)
                        return 'stop'
                    with n.lock:
                        g = n.goal if n.goal and n.goal['id'] == gid else None
                        left = g.get('poses_left') if g else None
                        first_fb = g.get('first_fb') if g else None
                    if not planned and first_fb:
                        planned = True
                        n.event('INFO', f'多点规划用时 {first_fb - t_send:.1f} s（{len(seg)} 站）')
                    here = n.lookup(BASE_FRAME)
                    while k < len(seg) - 1:
                        pt = seg[k][1]
                        near = here and n.fixed == 'map' and math.hypot(here[0] - pt['x'], here[1] - pt['y']) <= ROUTE_THROUGH_PASS
                        dropped = left is not None and len(seg) - left > k   # Nav2 已经划掉了这一站
                        if not (near or dropped):
                            break
                        self.through_passed(ws, route, seg, k)
                        k += 1
                    if done.done():
                        break
                status = done.result().status
            except asyncio.TimeoutError:
                await self.aw(handle.cancel_goal_async(), 2.0)
                raise RuntimeError('等待到点超时') from None
            except RuntimeError:   # 经过某站时发现阻断条件（断线、上锁…）：先停下多点目标再报错
                try:
                    await self.aw(handle.cancel_goal_async(), 2.0)
                except Exception:
                    pass
                raise
            finally:
                stop.cancel()
                done.cancel()
                await asyncio.gather(stop, done, return_exceptions=True)
            if route['stop'].is_set():
                return 'stop'
            if status == GoalStatus.STATUS_CANCELED:
                self.finish_route(route, 'canceled', f'第 {route["index"] + 1} 点已取消')
                return 'stop'
            if status != GoalStatus.STATUS_SUCCEEDED and not planned and time.monotonic() - t_send < 5.0:
                # 一点反馈都没有就放弃了：多半是行为树没加载上（navigation.log 里有 BT 报错）
                n.event('WARN', '多点导航一开始就失败（行为树没加载上？看 navigation.log），这次按一站一站走')
                return seg[k][0]
            last_state, end_pt, dwell, nxt = seg[-1]
            here = n.lookup(BASE_FRAME)
            off = math.hypot(here[0] - end[0], here[1] - end[1]) if here and n.fixed == 'map' else None
            # Nav2 报整段到了，但中间还有站狗没经过、Nav2 也没划掉（剩余点数还多于 1）：中间站不能白送，按假到达处理
            skipped = status == GoalStatus.STATUS_SUCCEEDED and k < len(seg) - 1 and not (left is not None and left <= 1)
            if status == GoalStatus.STATUS_SUCCEEDED and ((off is not None and off > ROUTE_FAKE_ARRIVE) or skipped):
                cur = seg[k][0]
                fake = fake + 1 if fake_state == cur else 1
                fake_state = cur
                why = f'第 {cur[0] + 1} 站还没经过' if skipped else f'离第 {last_state[0] + 1} 站还有 {off:.2f} m'
                if fake > ROUTE_FAKE_RETRY:
                    raise RuntimeError(f'Nav2 报到达，但{why}（已重发 {ROUTE_FAKE_RETRY} 次）')
                n.event('WARN', f'Nav2 报到达，但{why}：不算到达，从第 {cur[0] + 1} 站重新出发（第 {fake} 次）')
                state = cur
                continue
            if status != GoalStatus.STATUS_SUCCEEDED:
                if k < len(seg) - 1 or off is None or off > ROUTE_ARRIVE_TOL:
                    raise RuntimeError('Nav2 未能到达' + (f'（离第 {last_state[0] + 1} 站 {off:.2f} m）' if off is not None else ''))
                n.event('WARN', f'第 {last_state[0] + 1} 站未精确到达（偏差 {off:.2f} m），视为已到')
            while k < len(seg) - 1:   # Nav2 到了终点：中间没来得及记的站都算经过
                self.through_passed(ws, route, seg, k)
                k += 1
            if dwell and not await self.route_dwell(ws, route, last_state[0], dwell):
                return 'stop'
            route['done'] += 1
            state = nxt
        return None

    async def route_dwell(self, ws, route, i, seconds):
        """到第 i 站后原地停留 seconds 秒。被取消 / 急停返回 False。停留中阻断条件（断线、上锁…）照常生效。"""
        n = self.node
        name = route['record']['points'][i].get('name') or f'第 {i + 1} 站'
        n.event('INFO', f'{name}停留 {seconds} s')
        route['dwell_until'] = time.monotonic() + seconds
        try:
            while True:
                left = route['dwell_until'] - time.monotonic()
                if left <= 0:
                    return True
                try:
                    await asyncio.wait_for(route['stop'].wait(), min(left, 0.5))
                    return False
                except asyncio.TimeoutError:
                    pass
                reason = self.route_blocker(ws, route['record'])
                if reason:
                    raise RuntimeError(f'停留中：{reason}')
        finally:
            route['dwell_until'] = None

    async def run_route(self, ws, route):
        n, record = self.node, route['record']
        try:
            await self.clear_before_start()
            if route['stop'].is_set():
                return
            pos = n.lookup(BASE_FRAME)
            if not pos or n.fixed != 'map':
                raise RuntimeError('无法读取当前位置')
            start = dict(zip(('x', 'y', 'th'), pos))
            points, loops = record['points'], route['loops']
            first = route['resume'] or (0, 1, False)
            near = math.hypot(start['x'] - points[0]['x'], start['y'] - points[0]['y'])
            if route['resume'] is None and near < ROUTE_NEAR_START:
                if points[0].get('dwell'):   # 第 1 站要停留：人已经在这了，原地停够再走
                    route.update(index=0, target=points[0])
                    if not await self.route_dwell(ws, route, 0, points[0]['dwell']):
                        return
                first = self.route_next(len(points), loops, first)
                route['done'] = 1
                n.event('INFO', f'已在第 1 站附近（{near:.2f} m），直接前往第 2 站')
            # 11-12：不再出发前用 Nav2 规划检查整圈（用户：路线判断不需要）；某段规划不了，跑到那一段时 Nav2 会报失败
            route['checking'] = False
            state = first
            if record['opts'].get('through', True):
                if n.nav_through.server_is_ready():
                    state = await self.run_through(ws, route, state)
                    if state == 'stop':
                        return
                else:
                    n.event('WARN', '导航没有多点导航服务（navigate_through_poses），这次按一站一站走')
            if state:
                route['through'] = False   # 退回一站一站走
            prev = None   # 上一站被不停车经过时它的 Nav2 目标：下一站没发出去就要取消它，免得狗自己走到那站
            fake, fake_state = 0, None   # 假到达重发次数（同一站连续计数）
            while state:
                i, loop, back = state
                if route['stop'].is_set():
                    return
                point = self.route_pose(points, i, back)
                route.update(index=i, loop=loop, back=back, target=point, goal=None)
                self.resume_checkpoint(route)   # 每站出发前写盘：网页服务突然停了也知道跑到哪了
                reason = self.route_blocker(ws, record)
                if reason:
                    if prev is not None:
                        await self.aw(prev.cancel_goal_async(), 2.0)
                    raise RuntimeError(reason)
                nxt = self.route_next(len(points), loops, state)
                dwell = points[i].get('dwell', 0) if nxt is not None else 0   # 终点停不停留都一样
                passing = nxt is not None and not dwell   # 后面还有站：不停车经过，朝向面向下一站；要停留的站照常停稳
                if passing:
                    q = self.route_pose(points, nxt[0], nxt[2])
                    pose = (point['x'], point['y'], math.atan2(q['y'] - point['y'], q['x'] - point['x']))
                else:
                    pose = (point['x'], point['y'], point['th'])
                here = n.lookup(BASE_FRAME)
                # 提前量不超过这段路的一半：站挨得近时（< 1.2 m）不会一过上一站就把这一站也算过了
                pass_dist = min(ROUTE_PASS_DIST, 0.5 * math.hypot(here[0] - pose[0], here[1] - pose[1])) if here else 0.0
                if passing and here:   # 拐角 = 来的方向（当前位置 → 本站）和去的方向（本站 → 下一站）的夹角
                    turn = pose[2] - math.atan2(pose[1] - here[1], pose[0] - here[0])
                    if abs(math.atan2(math.sin(turn), math.cos(turn))) > ROUTE_PASS_TURN:
                        passing = False   # 到点停下（朝向已经是面向下一站）
                request = n.send_goal(*pose)
                try:
                    handle = await self.aw(request, 3.0)
                except asyncio.TimeoutError:
                    request.add_done_callback(self.cancel_late_goal)
                    raise RuntimeError('Nav2 接收目标超时') from None
                if not handle.accepted:
                    if prev is not None:
                        await self.aw(prev.cancel_goal_async(), 2.0)
                    raise RuntimeError('Nav2 拒绝目标')
                prev = None
                if route['stop'].is_set():
                    await self.aw(handle.cancel_goal_async(), 2.0)
                    return
                result = n.track_goal(handle, pose, 'route')
                route['goal'] = dict(zip(('x', 'y', 'th'), pose))
                stop = asyncio.create_task(route['stop'].wait())
                done = asyncio.create_task(self.aw(result, 180.0))
                passed = False
                try:
                    while True:
                        await asyncio.wait((stop, done), timeout=0.1 if passing else None,
                                           return_when=asyncio.FIRST_COMPLETED)
                        if route['stop'].is_set():
                            return
                        if done.done():
                            break
                        here = n.lookup(BASE_FRAME)
                        if here and n.fixed == 'map' and math.hypot(here[0] - pose[0], here[1] - pose[1]) < pass_dist:
                            passed = True
                            break
                    if passed:   # passing 为 False 时不会走到这里
                        with n.lock:
                            g = n.goal
                            if g is not None and g['id'] == bytes(handle.goal_id.uuid):
                                g['passed'] = True
                                g['status'], g['end'] = 'succeeded', time.monotonic()
                                t_leg = g['nav_time'] or (g['end'] - g['t0'])
                            else:
                                t_leg = 0.0
                        n.event('INFO', f"经过第 {i + 1} 站 ({pose[0]:.2f}, {pose[1]:.2f})，用时 {t_leg:.1f} s，不停车前往下一站")
                        status = GoalStatus.STATUS_SUCCEEDED
                        prev = handle
                    else:
                        status = done.result().status
                except asyncio.TimeoutError:
                    await self.aw(handle.cancel_goal_async(), 2.0)
                    raise RuntimeError('等待到点超时') from None
                finally:
                    stop.cancel()
                    done.cancel()
                    await asyncio.gather(stop, done, return_exceptions=True)
                if route['stop'].is_set():
                    return
                if status == GoalStatus.STATUS_CANCELED:
                    self.finish_route(route, 'canceled', f'第 {i + 1} 点已取消')
                    return
                if status == GoalStatus.STATUS_SUCCEEDED and not passed:
                    here = n.lookup(BASE_FRAME)
                    off = math.hypot(here[0] - pose[0], here[1] - pose[1]) if here and n.fixed == 'map' else None
                    if off is not None and off > ROUTE_FAKE_ARRIVE:
                        fake = fake + 1 if fake_state == state else 1
                        fake_state = state
                        if fake > ROUTE_FAKE_RETRY:
                            raise RuntimeError(f'Nav2 报到达，但离站点还有 {off:.2f} m（已重发 {ROUTE_FAKE_RETRY} 次）')
                        n.event('WARN', f'第 {i + 1} 站 Nav2 报到达，但离站点还有 {off:.2f} m：不算到达，重新前往（第 {fake} 次）')
                        continue
                if status != GoalStatus.STATUS_SUCCEEDED:
                    here = n.lookup(BASE_FRAME)
                    off = math.hypot(here[0] - pose[0], here[1] - pose[1]) if here and n.fixed == 'map' else None
                    if off is None or off > ROUTE_ARRIVE_TOL:
                        raise RuntimeError('Nav2 未能到达' + (f'（离目标 {off:.2f} m）' if off is not None else ''))
                    n.event('WARN', f'第 {i + 1} 站未精确到达（偏差 {off:.2f} m），视为已到，继续下一站')
                if dwell and not await self.route_dwell(ws, route, i, dwell):
                    return
                route['done'] += 1
                state = self.route_next(len(points), loops, state)
            self.finish_route(route, 'succeeded', '已到达全部导航点' if loops is None else f'已完成循环 {loops} 圈')
        except Exception as e:
            if not route['stop'].is_set():
                self.finish_route(route, 'aborted', f'第 {route["index"] + 1} 点失败：{e}')
            else:
                n.event('ERROR', f'多点任务停止时出错：{e}')

    def task(self):
        goal, route = self.node.task(), self.route
        if not route:
            return goal
        status = route['status']
        # 结束的路线保留 10 s 给网页看结果；期间发了新的导航目标就立刻让位
        newer = goal and goal.get('status') == 'active' and goal.get('goal') != route['goal']
        if status != 'active' and (newer or time.monotonic() - route['ended'] > 10):
            self.route = None
            return goal
        record, i = route['record'], route['index']
        leg = goal if goal and goal['type'] == 'goal' and goal['goal'] == route['goal'] else {}
        if leg and route.get('through') and status == 'active':   # 连续通过：Nav2 给的是到这段终点的距离
            here, t = self.node.lookup(BASE_FRAME), route['target']
            leg = {**leg, 'remaining': math.hypot(here[0] - t['x'], here[1] - t['y']) if here else None, 'eta': None}
        phase = status
        if status == 'active':
            phase = 'checking' if route['checking'] else leg['phase'] if leg.get('status') == 'active' else 'plan'
            if route.get('dwell_until'):
                phase = 'dwell'
        n, loops = len(record['points']), route['loops']
        if status == 'succeeded':
            progress = 1.0
        elif route['total']:
            progress = route['done'] / route['total']
        else:   # 一直循环：显示当前这一圈的进度
            progress = ((route['done'] - 1) % n) / n if route['done'] else 0.0
        return {**leg, 'type': 'route', 'name': record['name'], 'status': status, 'phase': phase,
                'goal': route['target'], 'point': i + 1, 'count': n,
                'roundTrip': loops is not None, 'loop': route['loop'], 'loops': loops, 'back': route['back'],
                'progress': progress,
                'routeId': record['id'], 'pointName': record['points'][i]['name'],
                'pointNote': record['points'][i].get('note', ''),
                'message': route['message'], 'elapsed': route.get('ended', time.monotonic()) - route['started'],
                # resumed：这次是从断点接着跑的；resumable：停下了且记了断点，可以「继续任务」
                'resumed': route['resume'] is not None,
                'through': bool(route.get('through')),   # 11-24 这趟是不是「连续通过」（多点导航）
                # 停留中：还剩几秒（11-24）；不在停留就是 null
                'dwellLeft': max(0.0, route['dwell_until'] - time.monotonic()) if route.get('dwell_until') else None,
                'pointDwell': record['points'][i].get('dwell', 0),
                'resumable': status in ('aborted', 'canceled') and self.resume_view(record) is not None}

    # ---------- P3：运行档位、巡逻、录包、地图 ----------
    MODE_NAME = {'mapping': '建图', 'localization': '定位', 'navigation': '导航'}

    def prof(self, key):
        """当前档位状态（和推给网页的算法一致）：stop / starting / run / stopping。"""
        n = self.node
        return n.profiles(set(n.get_node_names()), self.procs_status, self.transitions).get(key, 'stop')

    async def refresh_procs(self):
        """启停之后马上重查进程，不等 2 s 一次的轮询：否则状态滞后，按钮会显示成相反的操作。"""
        try:
            self.procs_status = await self.procs.status()
        except Exception:
            self.procs_status = None

    def background(self, coro):
        t = asyncio.create_task(coro)
        self.pending.add(t)
        t.add_done_callback(self.pending.discard)

    def set_mode(self, mode, map_name=None, *, force=False, taught_route=False):
        n = self.node
        if self.busy or self.map_busy or self.cont_busy:
            return False, '上一个启停还没完成，稍等几秒'
        if mode is not None and mode not in P.MODES:
            return False, f'没有这个档位：{mode}'
        cur = [m for m in P.MODES if self.prof(m) != 'stop']
        if mode == 'navigation' and self.taught_repo.available and 'taught_route' not in n.get_node_names():
            force = True
        if mode is not None and cur == [mode] and not force:
            return True, f'{self.MODE_NAME[mode]}已经在运行'
        if mode in ('localization', 'navigation'):
            names = [m['name'] for m in self.maps]
            map_name = map_name or self.default_map()
            if map_name not in names:
                return False, 'data/maps 里没有可用的地图'
        if not cur and mode is None:
            return True, '没有在运行的档位'
        self.busy = True
        self.stop_route('切换运行档位')
        self.background(self.do_mode(mode, cur, map_name, taught_route=taught_route))
        if mode is None:
            return True, '正在停止……'
        return True, f"正在启动{self.MODE_NAME[mode]}{'（地图 ' + map_name + '）' if map_name else ''}：FAST-LIO 初始化时机器人要保持静止"

    async def do_mode(self, mode, cur, map_name, *, taught_route=False):
        n = self.node
        try:
            await self.taught_interrupt('运行档位切换，示教草稿已保留')
            self.taught_invalidate('运行服务已切换，请重新加载地图并定位自检')
            if cur or self.prof('patrol') != 'stop':
                for m in cur:
                    self.transitions[m] = 'stopping'
                if n.is_armed() or n.nav_active():
                    await self.disarm_and_cancel('切换运行档位')
                # 「停止全部」不走下面的 if mode 分支，所以重置要放在停档位这一侧：
                # 建图一停，续建的起点位姿和自检结果都不再成立
                await self.incident_flush('停止' + '、'.join(self.MODE_NAME[m] for m in cur) if cur else '停止巡逻')
                # RTAB 库靠这些档位的 /scan 和 TF：先停下存好库。它只是影子库，存不完也照常切档位（rtab_tick 会等它退出后收尾）
                rtab_ok, rtab_msg = await self.rtab_before_mode_stop()
                if not rtab_ok:
                    n.event('ERROR', f'RTAB 库没有正常停下（{rtab_msg}），照常切换档位；这一轮 RTAB 库可能不完整')
                if 'mapping' in cur:
                    # 停掉之前先存一份：地图只在 slam_toolbox 内存里，一停就没了（11-16 就这样丢过图）
                    t_wait = time.monotonic()
                    while self.autosave['running'] and time.monotonic() - t_wait < 60.0:   # 等正在写的那次写完
                        await asyncio.sleep(0.2)
                    if self.map_frozen:
                        f = self.map_frozen
                        n.event('INFO', '建图冻结中，停止前不再自动保存' +
                                (f"：保留 data/maps/{f['kept']}（{time.strftime('%H:%M:%S', time.localtime(f['kept_wall']))}）"
                                 if f['kept'] else ''))
                    else:
                        await self.autosave_now('停止建图前')
                    self.reset_cont('停止建图')
                ok = await self.procs.stop_modes()   # supervisor 正常停止导航组和巡逻，不影响控制组
                if 'mapping' in cur:
                    self.map_frozen = None
                self.bridge_t, self.bridge_started = -1e9, False   # 不算异常退出；停完马上可以重新拉起
                await self.refresh_procs()
                for m in cur:
                    self.transitions.pop(m, None)
                    self.starting.pop(m, None)
                if ok:
                    n.event('INFO', f"已停止{'、'.join(self.MODE_NAME[m] for m in cur) or '巡逻'}")
                else:
                    n.event('ERROR', '导航组尚未退出，没有继续启动新档位。请检查 go2w navigation status 和日志')
                    return
            if mode == 'navigation' and (self.procs_status or {}).get('bridge'):
                await self.stop_bridge('启动导航')
                if (self.procs_status or {}).get('bridge'):
                    n.event('ERROR', '遥控用的速度桥没有停掉，没有启动导航（两个速度桥不能同时跑）。可以再点一次')
                    return
            if mode:
                self.autoloc = {'state': 'idle', 'seq': self.autoloc['seq']}   # 上一轮的自动定位结果作废
                self.autoloc_reset()   # FAST-LIO 重启，odom 坐标系变了
                self.map_frozen, n.odom_wild, self.odom_wild_seen = None, None, None
                # 续建会话跟着建图档位走：档位一换，起点位姿和自检结果都作废。
                # 不清的话，停掉建图再重开后界面还显示上一个起点位姿，而它已经不对了
                self.reset_cont('切换运行档位')
                self.transitions[mode] = 'starting'
                self.starting[mode] = time.monotonic()
                if mode == 'mapping':
                    self.autosave_new_session()
                    self.cont_blocked = False
                    self.rtab_on_mapping_start()
                # 新版网页启动导航时始终启用示教代理：无参考时仍走原规划；有参考自动读取地图YAML。
                await self.procs.start_mode(mode, map_name, taught_route=(mode == 'navigation' and self.taught_repo.available) or taught_route)
                await asyncio.sleep(0.3)
                await self.refresh_procs()
                n.event('INFO', f"启动{self.MODE_NAME[mode]}{'（地图 ' + map_name + '）' if map_name else ''}，"
                                '日志在 data/logs/navigation.log')
                if mode != 'mapping':
                    self.map_choice = None
        except Exception as e:
            # 停档位时出错（例如 supervisor 10 s 内没等到导航组退出）也要清掉这些档位的「停止中」，
            # 否则界面一直卡在停止中、按钮全灰；之后档位状态按真实进程状态显示
            for m in {mode, *cur} - {None}:
                self.transitions.pop(m, None)
                self.starting.pop(m, None)
            await self.refresh_procs()
            n.event('ERROR', f'启停失败：{e!r}')
        finally:
            self.busy = False

    async def bag_start(self, name, topics):
        """分段录包：每 BAG_SEGMENT 秒一个包，包名 = 前缀_MMDD_HHMMSS。name 是前缀，不填（网页「开始录包」）用
        狗编号_trigger（11-24：人主动录的包带 _trigger_，和异常自动保存的 狗编号_MMDD_HHMMSS 区分开）。"""
        name = name or f'{robot_id()[0][:22]}_trigger'
        if not P.SAFE_NAME.match(name) or len(name) > 30:
            return False, '包名前缀只能用字母、数字、下划线和减号，最多 30 个字符'
        bad = [t for t in topics if t != P.ALL_TOPICS and not P.SAFE_TOPIC.match(t)]
        if not topics or bad:
            return False, f"topic 写法不对：{' '.join(bad) or '（空）'}"
        if self.prof('bag') != 'stop':
            return False, '已经在录包了'
        over = await self.bag_quota()
        if over:
            return False, f'{over}：已上传的旧包删完了还是超限，先上传或手动删掉一些包再录'
        self.transitions['bag'] = 'starting'
        try:
            await self.procs.start_bag(name, topics)
            await asyncio.sleep(0.3)
            await self.refresh_procs()
        finally:
            self.transitions.pop('bag', None)
        self.node.event('INFO', f"开始录包（每 {P.BAG_SEGMENT} 秒一个包，{name}_时间）：{' '.join(topics)}")
        return True, f'开始录包：每 {P.BAG_SEGMENT} 秒存一个包到 data/bags/{name}_<时间>'

    async def bag_delete(self, name, who):
        # 删掉 data/bags/<name> 整个目录（.mcap + metadata.yaml），删了不能恢复
        if not name or not P.SAFE_NAME.match(name):
            return False, '包名不对'
        bags_dir = os.path.join(DATA, 'bags')
        self.bags = P.list_bags(bags_dir)
        if not any(b['name'] == name for b in self.bags):
            return False, f'data/bags 里没有 {name}'
        if self.prof('bag') != 'stop' or self.transitions.get('bag'):
            # 正在录的那个不能删：网页启动的记得名字；后端重启过或者不是网页启动的，保险起见最新的一个也不让删
            if name in self.recording_names():
                return False, f'{name} 正在录制，先停止录包再删'
        if self.uploads.get(name, {}).get('state') == 'run':
            return False, f'{name} 正在上传，等上传完再删'
        path = os.path.join(bags_dir, name)
        if os.path.realpath(os.path.dirname(path)) != os.path.realpath(bags_dir):
            return False, '包名不对'
        size = next(b['size'] for b in self.bags if b['name'] == name)
        try:
            await asyncio.to_thread(shutil.rmtree, path)
        except OSError as e:
            self.bags = P.list_bags(bags_dir)
            return False, f'删除失败：{e.strerror or e}'
        self.bags = P.list_bags(bags_dir)
        self.node.event('WARN', f'已删除录包 {name}（{size:.0f} MB，{who}）')
        return True, f'已删除录包 {name}'

    async def bag_stop(self):
        self.transitions['bag'] = 'stopping'
        try:
            await self.procs.stop_bag()
            await self.refresh_procs()
            self.bags = P.list_bags(os.path.join(DATA, 'bags'))
        finally:
            self.transitions.pop('bag', None)

    def bag_over(self, total_mb, free_mb):
        """超限就返回原因，没超返回空串。"""
        if total_mb > BAG_LIMIT_GB * 1024:
            return f'录包总量 {total_mb / 1024:.1f} GB 超过上限 {BAG_LIMIT_GB:.0f} GB'
        if free_mb is not None and free_mb < BAG_MIN_FREE_GB * 1024:
            return f'磁盘只剩 {free_mb / 1024:.1f} GB（低于 {BAG_MIN_FREE_GB:.0f} GB）'
        return ''

    async def bag_quota(self):
        """容量检查：超限就从最旧的开始删已上传的包。返回删完之后仍然超限的原因（没超返回空串）。"""
        bags_dir = os.path.join(DATA, 'bags')
        self.bags = P.list_bags(bags_dir)
        try:
            free = shutil.disk_usage(bags_dir).free / 1048576
        except OSError:
            free = None
        total = sum(b['size'] for b in self.bags)
        over = self.bag_over(total, free)
        if not over:
            self.quota_warned = False
            return ''
        recording = self.prof('bag') != 'stop' or bool(self.transitions.get('bag'))
        busy = self.recording_names() if recording else set()
        for b in reversed(self.bags):   # list_bags 是新的在前
            if not self.bag_over(total, free):
                break
            if b['name'] in busy or self.uploads.get(b['name'], {}).get('state') != 'done':
                continue
            ok, _ = await self.bag_delete(b['name'], f'自动清理，{over}')
            if ok:
                total -= b['size']
                free = free + b['size'] if free is not None else None
        over = self.bag_over(total, free)
        if not over:
            self.quota_warned = False
            return ''
        if recording and not self.transitions.get('bag'):
            cur = next((b for b in self.bags if b['name'] in busy), None)
            disk_low = free is not None and free < BAG_MIN_FREE_GB * 1024
            if disk_low or (cur and cur['size'] > BAG_LIMIT_GB * 1024):
                which = ' ' + cur['name'] if cur else ''
                self.node.event('ERROR', f'{over}，自动停止录包{which}')
                await self.bag_stop()
                self.quota_warned = True
                return over
        if not self.quota_warned:
            self.quota_warned = True
            self.node.event('WARN', f'{over}，已上传的旧包删完了还是超限：没上传的包不会自动删，请上传或手动删除')
        return over

    def recording_names(self):
        """列表里正在写的包。网页录包是分段的：正在写的那段在 .recording 里，列表里的都已经写完，返回空。
        命令行手动跑的 ros2 bag record 不知道名字，保险起见把最新的一个当成正在录（只是不自动删它）。"""
        if (self.procs_status or {}).get('bag_seg'):
            return set()
        newest = next((b['name'] for b in self.bags), None)
        return {newest}

    def rollback_set(self, on, who):
        self.rollback_on = on
        try:
            with open(self.rollback_file, 'w') as f:
                json.dump({'on': on}, f)
        except OSError:
            pass
        self.node.event('INFO', f"回溯录制已{'打开' if on else '关闭'}（{who}）")
        return True, f"回溯录制已{'打开' if on else '关闭'}"

    def taught_nav_set(self, on, who):
        """网页「启用示教导航」开关：存到 data/taught_nav.json，下次开导航时按它决定带不带示教节点。"""
        repo, n = self.taught_repo, self.node
        f = repo.forced()
        if f is not None:
            return False, f"示教导航被环境变量 GO2W_TAUGHT={f} 固定为{'开' if f == '1' else '关'}，网页开关不起作用"
        if on and not repo.installed:
            return False, '后端没有示教算法包，打不开示教导航'
        if not on and self.taught_open():
            return False, '有没结束的示教采集草稿，先结束或放弃再关'
        repo.enabled = on
        try:
            with open(self.taught_nav_file, 'w') as fp:
                json.dump({'on': on}, fp)
        except OSError as e:
            n.event('WARN', f'示教导航开关没存下来（重启网页后会回到原状态）：{e}')
        node_up = 'taught_route' in n.get_node_names()
        nav = self.prof('navigation') == 'run'
        tail = ''
        if on and nav and not node_up:
            tail = '；导航正在运行、还没带示教节点，下次「出发」会先自动重开导航（机器人保持静止）'
        elif not on and node_up:
            tail = '；示教节点要等下次重开导航才会停'
        msg = f"示教导航已{'打开' if on else '关闭'}{tail}"
        n.event('INFO', f'{msg}（{who}）')
        return True, msg

    async def stop_bridge(self, why):
        n = self.node
        if self.teleop is not None:
            self.teleop = None
        if n.is_armed():
            await self.disarm_and_cancel(why)
        await self.procs.stop_bridge()
        await self.refresh_procs()

    async def bridge_tick(self):
        """没开导航时，保持一个遥控用的速度桥在跑；导航启动 / 运行时不跑（导航自带速度桥）。"""
        st, n = self.procs_status, self.node
        if st is None or self.busy:
            return
        nav = st.get('navigation') or 'navigation' in self.starting or self.transitions.get('navigation')
        now = time.monotonic()
        if not nav and not st.get('bridge') and now - self.bridge_t > BRIDGE_RETRY:
            if self.bridge_started:
                n.event('WARN', '遥控用的速度桥退出了，正在重新启动。data/logs/bridge.log 最后几行：\n'
                        + P.tail(os.path.join(DATA, 'logs', 'bridge.log'), 6))
            self.bridge_t, self.bridge_started = now, True
            await self.procs.start_bridge()
        elif nav and st.get('bridge'):
            self.bridge_started = False
            await self.stop_bridge('导航启动')

    async def rollback_tick(self):
        """建图 / 定位 / 导航运行时保持回溯进程在跑，都停了就停掉它。"""
        st, n = self.procs_status, self.node
        if st is None:
            return
        want = self.rollback_on and any(st.get(m) for m in P.MODES)
        now = time.monotonic()
        if want and not st.get('rollback') and now - self.rollback_t > ROLLBACK_RETRY:
            if self.rollback_started:
                n.event('WARN', '回溯录制进程退出了，正在重新启动。data/logs/rollback.log 最后几行：\n'
                        + P.tail(os.path.join(DATA, 'logs', 'rollback.log'), 6))
            self.rollback_t, self.rollback_started = now, True
            await self.procs.start_rollback(ROLLBACK_BUFFER)
        elif st.get('rollback') and not want:
            self.rollback_started = False
            await self.procs.stop_rollback()

    def stuck_tick(self):
        """导航目标没到、狗却长时间停着不动：记一条「长时受阻」（持续到动起来或导航结束），并自动存回溯包。"""
        n, now = self.node, time.monotonic()
        o = n.odom_pose
        pose = o[:3] if o and now - o[3] < STALE else None
        active = n.nav_active() and bool(n.is_armed()) and not self.estop
        still = self.stuck.update(now, pose, active)
        if 'stuck' in self.inc_open and not self.stuck.fired:   # 动起来了 / 导航结束：这次卡住结束
            self.incident_close('stuck')
        if still is None:
            return
        n.event('WARN', f'导航中已经 {still:.0f} 秒没动了，还没到目标')
        self.inc_open['stuck'] = self.incident(
            'stuck', f'导航中原地不动超过 {STUCK_SECONDS:.0f} 秒，还没到目标', ongoing=True, ago=still)

    # ---------- 异常记录 + 自动录包（incidents.py） ----------
    def incident_task(self):
        """正在跑的巡逻任务（异常记录里的 task）；没有 = None。"""
        r = self.route
        if not r or r['status'] != 'active':
            return None
        rec, i = r['record'], r['index']
        return {'routeId': rec['id'], 'name': rec['name'], 'point': i + 1, 'pointName': rec['points'][i]['name']}

    def incident(self, typ, msg, ongoing=False, ago=0.0, task=INC_AUTO, bag=True):
        """记一条异常并安排自动存包，返回 id。不抛异常：会在停车等安全动作的路径上调用。
        ago = 异常实际开始于多少秒前（卡住是判定时已经停了 8 秒）；task 不传 = 取当前任务。"""
        n = self.node
        try:
            pose = None
            try:
                p = n.lookup(BASE_FRAME)
                if p:
                    pose = {'x': round(p[0], 3), 'y': round(p[1], 3), 'th': round(p[2], 3), 'frame': n.fixed}
            except Exception:
                pass
            rec = self.incidents.open(typ, msg, robot_id()[0], ts=INC.now_ms() - ago * 1000, pose=pose,
                                      map_name=self.shown_map(), ongoing=ongoing,
                                      task=self.incident_task() if task is INC_AUTO else task)
            if bag is True:
                self.incident_bag(rec)
            else:
                self.incidents.set_bag(rec['id'], 'skipped', msg=bag or '这类异常不存包')
            if self.incidents.error:
                n.event('ERROR', self.incidents.error)
            return rec['id']
        except Exception as e:
            n.get_logger().error(f'异常记录失败（{typ}：{msg}）：{e!r}')
            return None

    def incident_close(self, key):
        rid = self.inc_open.pop(key, None)
        if rid:
            try:
                self.incidents.close(rid)
            except Exception as e:
                self.node.get_logger().error(f'异常记录结束失败（{key}）：{e!r}')

    def goal_failed(self, pose):
        """单点导航目标被 Nav2 放弃（rclpy 线程经 call_soon_threadsafe 转过来）。"""
        self.incident('nav_fail', f'导航目标 ({pose[0]:.2f}, {pose[1]:.2f}) 失败：Nav2 放弃了这个目标')

    def incident_bag(self, rec):
        """给这条异常安排自动存包：并进已计划的包 / 新计划一个 / 说明为什么不存。"""
        L, n, rid, typ, label = self.incidents, self.node, rec['id'], rec['type'], rec['label']
        now = time.monotonic()
        if not (self.procs_status or {}).get('rollback'):
            L.set_bag(rid, 'skipped', msg='回溯录制没在运行（建图 / 定位 / 导航都没开，或回溯被关掉）')
            n.event('WARN', f'{label}：回溯录制没在运行，没法自动存包')
            return
        p = self.inc_save
        if p and p['at'] - now >= INC_MERGE:
            p['ids'].append(rid)
            L.set_bag(rid, 'pending', msg='和前面的异常存进同一个包')
            return
        if now - self.inc_bag_t.get(typ, -1e9) < STUCK_COOLDOWN:
            L.set_bag(rid, 'skipped', msg=f'{STUCK_COOLDOWN:.0f} 秒内已为同类异常自动存过包')
            n.event('WARN', f'{label}：{STUCK_COOLDOWN:.0f} 秒内已为同类异常自动存过包，这次不存')
            return
        self.inc_bag_t[typ] = now
        p = self.inc_save = {'at': now + STUCK_AFTER, 't0': now, 'ids': [rid], 'done': asyncio.Event()}
        n.event('INFO', f'{label}：{STUCK_AFTER:.0f} 秒后自动保存回溯包（异常前 {STUCK_BEFORE:.0f} 秒 + 后 {STUCK_AFTER:.0f} 秒）')
        self.background(self.incident_save(p))

    async def incident_save(self, p):
        try:
            await self._incident_save(p)
        finally:
            p['done'].set()

    async def incident_flush(self, why):
        """停档位前：还在等「异常后 15 秒」的包马上存掉——档位一停回溯录制就跟着停，到点时已经没东西可存
        （11-19：摔倒后照提示马上停止建图，就会这样丢包）。"""
        p = self.inc_save
        if not p or p['done'].is_set():
            return
        left = p['at'] - time.monotonic()
        if left > 0:
            self.node.event('INFO', f'{why}：先把异常回溯包存掉（异常后只录到 {STUCK_AFTER - left:.0f} 秒）')
            p['at'] = time.monotonic()
        try:
            await asyncio.wait_for(p['done'].wait(), ROLLBACK_SAVE_TIMEOUT + 15.0)
        except asyncio.TimeoutError:
            self.node.event('WARN', f'{why}：异常回溯包没存完，继续停止')

    async def _incident_save(self, p):
        L = self.incidents
        while time.monotonic() < p['at']:          # 可能被 incident_flush 提前
            await asyncio.sleep(min(0.2, max(0.0, p['at'] - time.monotonic())))
        if self.inc_save is p:
            self.inc_save = None          # 从现在起的新异常另起一个包
        for _ in range(10):               # 正好有人在手动保存时等它存完
            if not self.rollback_saving:
                break
            await asyncio.sleep(1.0)
        for rid in p['ids']:
            L.set_bag(rid, 'saving')
        labels = '、'.join(dict.fromkeys(r['label'] for r in map(L.get, p['ids']) if r))
        try:
            # 起点固定在第一条异常前 STUCK_BEFORE 秒；等手动保存多过去的秒数也算进去
            ok, msg = await self.rollback_save(f'异常自动保存：{labels}', STUCK_BEFORE + time.monotonic() - p['t0'])
        except Exception as e:
            ok, msg = False, f'{e!r}'
        name = self.rollback_last if ok else None
        for rid in p['ids']:
            L.set_bag(rid, 'saved' if ok else 'failed', name=name, msg='' if ok else msg)
        if ok and name:
            # 包目录里也放一份异常说明：包被上传 / 拷走后也知道它为什么存的
            try:
                recs = [r for r in map(L.get, p['ids']) if r]
                with open(os.path.join(DATA, 'bags', name, 'incidents.json'), 'w', encoding='utf-8') as f:
                    json.dump(recs, f, ensure_ascii=False, indent=2)
            except OSError as e:
                self.node.get_logger().warn(f'包 {name} 里写异常说明失败：{e!r}')

    def incident_push(self, rec):
        data = json.dumps({'t': 'incident', 'item': rec}, ensure_ascii=False)
        for ws in list(self.clients):
            self.tx_put(ws, data)

    def incidents_message(self):
        return json.dumps({'t': 'incidents', 'items': self.incidents.recent(INC_PUSH)}, ensure_ascii=False)

    async def api_incidents(self, req):
        """GET /api/incidents?limit=50&before=<ts 毫秒>：最新的在前，翻页时 before = 上一页最后一条的 ts。"""
        try:
            limit = max(1, min(500, int(req.query.get('limit', 50))))
            before = int(req.query['before']) if req.query.get('before') else None
        except ValueError:
            return web.json_response({'error': 'limit / before 需要是整数'}, status=400,
                                     headers={'Access-Control-Allow-Origin': '*'})
        return web.json_response({'items': self.incidents.recent(limit, before)},
                                 dumps=lambda o: json.dumps(o, ensure_ascii=False),
                                 headers={'Access-Control-Allow-Origin': '*', 'Cache-Control': 'no-store'})

    async def rollback_save(self, who, seconds=ROLLBACK_SECONDS, trigger=False):
        """trigger：人在网页上点「保存刚才 N 秒」→ 包名 狗编号_trigger_MMDD_HHMMSS；异常自动保存不带（11-24）。"""
        if not (self.procs_status or {}).get('rollback'):
            return False, '回溯录制没在运行：先打开回溯，并启动建图 / 定位 / 导航'
        if self.rollback_saving:
            return False, '正在保存上一段，请稍等'
        self.rollback_saving = True
        bags_dir = os.path.join(DATA, 'bags')
        work = os.path.join(bags_dir, '.rollback')
        # 和分段录包同一个前缀，同一秒可能撞名：已有的包、正在录的段、正在存的回溯都避开，撞了加 _2
        base = time.strftime(f"{robot_id()[0]}{'_trigger' if trigger else ''}_%m%d_%H%M%S")
        name, k = base, 1
        while any(os.path.exists(os.path.join(d, name)) for d in (bags_dir, work, os.path.join(bags_dir, '.recording'))):
            k += 1
            name = f'{base}_{k}'
        error, done = os.path.join(work, f'{name}.error'), os.path.join(bags_dir, name, 'metadata.yaml')
        try:
            os.makedirs(work, exist_ok=True)
            tmp = os.path.join(work, 'request.tmp')
            with open(tmp, 'w') as f:
                f.write(f'{name} {float(seconds)}')
            os.replace(tmp, os.path.join(work, 'request'))
            t0 = time.monotonic()
            while time.monotonic() - t0 < ROLLBACK_SAVE_TIMEOUT:
                if os.path.exists(error):
                    with open(error, errors='replace') as f:
                        why = f.read()[:300]
                    os.remove(error)
                    self.node.event('ERROR', f'回溯保存失败：{why}')
                    return False, f'回溯保存失败：{why}'
                if os.path.exists(done):
                    self.bags = P.list_bags(bags_dir)
                    b = next((x for x in self.bags if x['name'] == name), {})
                    info = f"{b.get('dur') or 0:.0f} 秒，{b.get('size', 0):.0f} MB"
                    self.node.event('INFO', f'已保存回溯 {name}（{info}，{who}）')
                    self.rollback_last = name
                    return True, f'已保存回溯 {name}（{info}）'
                await asyncio.sleep(0.1)
            self.node.event('ERROR', f'回溯保存超时（{ROLLBACK_SAVE_TIMEOUT:.0f} s），看 data/logs/rollback.log')
            return False, '回溯保存超时，看 data/logs/rollback.log'
        finally:
            self.rollback_saving = False

    async def bag_upload(self, name, who):
        """rsync 把 data/bags/<name> 传到 166:~/go2w_bags/<name>。同一时间只传一个；进度和结果进网页日志。"""
        if not name or not P.SAFE_NAME.match(name):
            return False, '包名不对'
        bags_dir = os.path.join(DATA, 'bags')
        self.bags = P.list_bags(bags_dir)
        bag = next((b for b in self.bags if b['name'] == name), None)
        if bag is None:
            return False, f'data/bags 里没有 {name}'
        if self.prof('bag') != 'stop' or self.transitions.get('bag'):
            if name in self.recording_names():
                return False, f'{name} 正在录制，先停止录包再上传'
        busy = next((k for k, u in self.uploads.items() if u['state'] == 'run'), None)
        if busy:
            return False, f'正在上传 {busy}，等它传完'
        key = os.path.join(DATA, 'ssh', 'id_ed25519')
        if not os.path.exists(key) or not shutil.which('rsync') or not shutil.which('ssh'):
            return False, '扩展坞上缺上传工具：需要 data/ssh 密钥，以及镜像里的 rsync / ssh（重建 go2w-humble 镜像）'
        self.uploads[name] = {'state': 'run', 'pct': 0, 'msg': '连接中'}
        asyncio.create_task(self.bag_upload_run(name, bag['size'], key, who))
        return True, f'开始上传 {name} → {UPLOAD_LABEL}'

    async def bag_upload_run(self, name, size, key, who):
        n, t0 = self.node, time.monotonic()
        ssh = (f'ssh -i {key} -o UserKnownHostsFile={os.path.join(DATA, "ssh", "known_hosts")} '
               '-o StrictHostKeyChecking=yes -o BatchMode=yes -o ConnectTimeout=8 -o ServerAliveInterval=10')
        n.event('INFO', f'开始上传录包 {name}（{size:.0f} MB）→ {UPLOAD_LABEL}（{who}）')
        tail, last_pct = deque(maxlen=6), 0
        try:
            p = await asyncio.create_subprocess_exec(
                'rsync', '-rt', '--partial', '--info=progress2', '--no-inc-recursive', '-e', ssh,
                os.path.join(DATA, 'bags', name), UPLOAD_DEST,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            buf = b''
            while True:
                chunk = await p.stdout.read(4096)
                if not chunk:
                    break
                buf += chunk
                # progress2 用 \r 刷新同一行：「  12,345,678  45%   10.2MB/s    0:00:12」
                *lines, buf = buf.replace(b'\r', b'\n').split(b'\n')
                for raw in lines:
                    line = raw.decode('utf-8', 'replace').strip()
                    if not line:
                        continue
                    parts = line.split()
                    if len(parts) >= 3 and parts[1].endswith('%') and parts[1][:-1].isdigit():
                        pct = int(parts[1][:-1])
                        self.uploads[name] = {'state': 'run', 'pct': pct, 'msg': parts[2]}
                        if pct // 25 > last_pct // 25 and pct < 100:
                            n.event('INFO', f'上传 {name}：{pct}%（{parts[2]}）')
                        last_pct = max(last_pct, pct)
                    else:
                        tail.append(line)
            code = await p.wait()
        except Exception as e:
            code, tail = -1, deque([repr(e)])
        dt = time.monotonic() - t0
        if code == 0:
            msg = time.strftime('%m-%d %H:%M')
            self.uploads[name] = {'state': 'done', 'pct': 100, 'msg': msg}
            done = {k: u['msg'] for k, u in self.uploads.items() if u['state'] == 'done'}
            try:
                with open(self.uploads_file, 'w') as f:
                    json.dump(done, f, ensure_ascii=False, indent=1)
            except OSError:
                pass
            n.event('INFO', f'已上传录包 {name} → {UPLOAD_LABEL}/{name}（{size:.0f} MB，用时 {dt:.0f} s）')
        else:
            why = ' | '.join(tail) or f'rsync 退出码 {code}'
            self.uploads[name] = {'state': 'fail', 'pct': last_pct, 'msg': why[-200:]}
            n.event('ERROR', f'上传录包 {name} 失败（退出码 {code}，{dt:.0f} s）：{why[-300:]}')

    # ---------- 建图自动保存 ----------
    def autosave_new_session(self):
        """新的一次建图：起一个新的自动保存名（建图开始时间），之前那次的留着。"""
        a = self.autosave
        a.update(name=f"{AUTOSAVE_PREFIX}{time.strftime('%m%d_%H%M%S')}", t=time.monotonic(), wall=None,
                 crc=None, err=None, problem=False, count=0)

    def autosave_view(self):
        a = self.autosave
        if not a['name']:
            return None
        return {'name': a['name'], 'ago': (time.monotonic() - a['t']) if a['wall'] else None, 'wall': a['wall'],
                'count': a['count'], 'err': a['err'], 'running': a['running'],
                'period': self.autosave_period(), 'on': self.prof('mapping') == 'run', 'frozen': self.map_frozen}

    def autosave_period(self):
        # 单次存图（slam 写 posegraph 时会锁住建图）超过周期的 1/10 就拉长间隔，免得拖慢 slam
        d = self.autosave['dur']
        return max(AUTOSAVE_PERIOD, 10.0 * d) if d else AUTOSAVE_PERIOD

    def autosave_problem(self):
        """建图时雷达 / FAST-LIO / 里程计出问题，而 slam_toolbox 还活着：这时要马上存，它可能随时跟着挂。"""
        n = self.node
        names = set(n.get_node_names())
        if 'slam_toolbox' not in names:
            return None
        for node, label in (('livox_lidar_publisher', '雷达驱动'), ('fast_lio', 'FAST-LIO')):
            if node not in names:
                return f'{label}不在了'
        if not (bool(n.healthy) and time.monotonic() - n.healthy_t < STALE):
            return '里程计不可用'
        return None

    def autosave_tick(self):
        """procs_poll 每 2 s 调一次：到点了、或刚出问题，就在后台存一次。"""
        a = self.autosave
        if self.prof('mapping') != 'run':
            a['problem'] = False
            return
        if not a['name']:
            self.autosave_new_session()   # 后端重启时建图已经在跑
        if self.map_frozen:
            return        # 冻结了：最近那份好的不能再被覆盖
        if a['running'] or self.busy or self.map_busy or self.map_loading or self.cont_busy or self.cont_blocked:
            return
        now = time.monotonic()
        problem = self.autosave_problem()
        if problem and not a['problem'] and now - a['t'] >= AUTOSAVE_PROBLEM_GAP:
            a['problem'] = True
            self.background(self.autosave_now(f'建图出问题（{problem}）'))
            return
        if not problem:
            a['problem'] = False
        if now - a['t'] >= self.autosave_period():
            self.background(self.autosave_now(None))

    async def autosave_now(self, reason):
        """存一次。reason=None 是定时保存（只在地图有变化时存、成功不刷日志）。返回 (ok, msg)。"""
        a, n = self.autosave, self.node
        if a['running'] or self.map_busy:
            return False, '自动保存正在进行'
        if not a['name']:
            return False, '没有在建图'
        if self.map_frozen:
            return False, '建图已冻结（摔倒 / 里程计异常），自动保存暂停，最近一份好的不再覆盖'
        if not await self.mapping_save_allowed():
            return False, '续建还在验证，暂停自动保存'
        if a['running'] or self.map_busy:
            return False, '自动保存正在进行'
        raw, crc = n.map_raw, n.map_crc
        if raw is None:
            return False, '还没有收到 /map'
        if crc is not None and crc == a['crc']:
            a['t'] = time.monotonic()   # 地图没变：不重复写盘，下个周期再看
            return True, '地图没有变化'
        a['running'] = True
        t0 = time.monotonic()
        maps_dir = os.path.join(DATA, 'maps')
        tmp_dir = os.path.join(maps_dir, '.autosave_tmp')
        name = a['name']
        try:
            os.makedirs(tmp_dir, exist_ok=True)
            tmp = os.path.join(tmp_dir, name)
            res = await self.aw(n.serialize_posegraph(tmp), 60.0)
            code = getattr(res, 'result', 0)
            if code not in (0, None):
                raise RuntimeError(f'serialize_map 返回 {code}')
            for ext in ('.posegraph', '.data'):
                if not os.path.isfile(tmp + ext) or os.path.getsize(tmp + ext) == 0:
                    raise RuntimeError(f'slam_toolbox 没有写出 {name}{ext}')
            grid, res_m, ox, oy = raw
            await asyncio.get_running_loop().run_in_executor(None, mapfile.write, tmp, grid, res_m, ox, oy)
            if self.map_frozen:   # 写盘期间冻结了：这一份可能已经是坏的，不覆盖
                raise RuntimeError('写盘期间建图被冻结，这一份没有采用')
            base = os.path.join(maps_dir, name)
            self.autosave_keep_prev(maps_dir, name)
            for ext in ('.posegraph', '.data', '.pgm', '.yaml'):   # yaml 最后：列表里出现时其余文件已经齐了
                os.replace(tmp + ext, base + ext)
                if hasattr(os, 'geteuid') and os.geteuid() == 0:
                    try:
                        os.chown(base + ext, 1000, 1000)   # 和 save_map.sh 一样交给 unitree
                    except OSError:
                        pass
        except Exception as e:
            msg = str(e) or repr(e)
            if msg != a['err'] or reason:
                n.event('WARN', f"建图自动保存失败{'（' + reason + '）' if reason else ''}：{msg}")
            a['err'] = msg
            a['t'] = time.monotonic()   # 失败也等下个周期再试，别每 2 s 打一次
            return False, f'自动保存失败：{msg}'
        finally:
            a['running'] = False
        first = a['count'] == 0
        a.update(t=time.monotonic(), wall=time.time(), crc=crc, dur=time.monotonic() - t0, err=None, count=a['count'] + 1)
        if first or reason:
            n.event('INFO', f"建图自动保存{'（' + reason + '）' if reason else ''}：data/maps/{name}"
                            f"（{a['dur']:.1f} s；建图期间每 {self.autosave_period():.0f} s 覆盖这一份）")
        if first:
            self.autosave_prune()
        self.rtab_link(name)
        self.maps = P.list_maps(maps_dir)
        return True, f'已自动保存 {name}'

    @staticmethod
    def autosave_keep_prev(maps_dir, name):
        """覆盖前把现有的 <名字>.* 挪成 <名字>_prev.*（yaml 里的 image 跟着改），只留一份。"""
        base, prev = os.path.join(maps_dir, name), os.path.join(maps_dir, name + AUTOSAVE_PREV)
        if not os.path.isfile(base + '.yaml'):
            return
        try:
            if os.path.exists(prev + '.yaml'):
                os.remove(prev + '.yaml')            # 先让旧的 _prev 从地图列表里消失
            for ext in ('.pgm', '.posegraph', '.data'):
                if os.path.isfile(base + ext):
                    os.replace(base + ext, prev + ext)
            with open(base + '.yaml', encoding='utf-8') as f:
                lines = f.read().splitlines()
            lines = [f'image: {name}{AUTOSAVE_PREV}.pgm' if ln.startswith('image:') else ln for ln in lines]
            with open(prev + '.yaml.tmp', 'w', encoding='utf-8') as f:
                f.write('\n'.join(lines) + '\n')
            os.replace(prev + '.yaml.tmp', prev + '.yaml')
            os.remove(base + '.yaml')
        except OSError:
            pass   # 留不下上一份不影响这次保存

    def autosave_prune(self):
        """只留最近 AUTOSAVE_KEEP 次建图的自动保存，旧的挪进回收站（data/maps/.trash，和「管理地图」删除同一处）。
        只认网页自己起的名字（is_autosave）：续建 / 编辑 / 手动保存出来的图哪怕名字以 autosave_ 开头也不动。
        正在用的、被路线引用的、续建正在拿来当基准或目标的不动。"""
        maps_dir = os.path.join(DATA, 'maps')
        try:
            files = os.listdir(maps_dir)
        except OSError:
            return
        names = sorted({f[:-len('.yaml')] for f in files if f.endswith('.yaml') and is_autosave(f[:-len('.yaml')])})
        sess = lambda nm: nm[:-len(AUTOSAVE_PREV)] if nm.endswith(AUTOSAVE_PREV) else nm   # _prev 跟着本次建图走
        keep_sess = set(sorted({sess(nm) for nm in names})[-AUTOSAVE_KEEP:]) | {self.autosave['name']}
        keep = {self.node.map_current, self.node.map_loaded} | {r.get('map') for r in self.routes.values()}
        keep |= set(self.map_in_use()) | set(self.cont_hold)
        if self.cont:
            keep |= {self.cont.get('base'), self.cont.get('name')}
        trash, stamp = os.path.join(maps_dir, '.trash'), time.strftime('%Y%m%d_%H%M%S')
        for name in names:
            if name in keep or sess(name) in keep_sess:
                continue
            try:
                self.trash_map(maps_dir, name, os.path.join(trash, f'{stamp}_{name}'), [], None)
            except OSError as e:
                self.node.event('WARN', f'清理较早的建图自动保存 {name} 失败：{e}')
                continue
            self.node.event('INFO', f'较早的建图自动保存 {name} 挪进回收站 data/maps/.trash'
                                    f'（只留最近 {AUTOSAVE_KEEP} 次，{self.MAP_TRASH_DAYS} 天后清掉）')
        self.purge_trash(trash, self.MAP_TRASH_DAYS)

    async def save_map(self, name):
        if is_autosave(name):
            return False, 'autosave_月日_时分秒 这种名字留给建图自动保存（会被定期清理），请换个名字'
        if self.map_busy or self.autosave['running']:
            return False, '地图正在保存，请稍等'
        self.map_busy = True
        try:
            return await self._save_map(name)
        finally:
            self.map_busy = False

    async def _save_map(self, name):
        if self.busy or self.map_loading:
            return False, '档位或地图正在切换，请稍等'
        if self.prof('mapping') != 'run':
            return False, '保存地图需要「建图」正在运行'
        if not await self.mapping_save_allowed():
            return False, '续建还在验证，通过自检后才能保存'
        if self.map_frozen:
            f = self.map_frozen
            self.node.event('WARN', f"在建图冻结状态下保存 {name}：地图停在 "
                            f"{time.strftime('%H:%M:%S', time.localtime(f['wall']))}（{f['reason']}）" +
                            ('；里程计飘过，保存后先检查墙有没有错位' if f['wild'] else ''))
        if not P.SAFE_NAME.match(name):
            return False, '地图名只能用字母、数字、下划线和减号'
        self.maps = P.list_maps(os.path.join(DATA, 'maps'))
        if any(m['name'] == name for m in self.maps):
            return False, f'已经有同名地图 {name}，换个名字（不会覆盖）'
        self.node.event('INFO', f'保存地图 {name}……')
        park_pose, park_why = self.save_parking_pose()   # 存图之前取：存图那一刻的位置
        if await self.procs.save_map(name):
            self.maps = P.list_maps(os.path.join(DATA, 'maps'))
            self.node.event('INFO', f'地图已保存：data/maps/{name}')
            self.rtab_link(name)
            if MAPPING_VIEW in self.map_views:   # 建图时调好的显示方向带给新地图
                await self.set_view(name, self.map_views[MAPPING_VIEW])
            self.last_save_park = self.park_after_save(name, park_pose, park_why)
            return True, f'地图已保存：{name}' + self.last_save_park
        return False, '保存失败：' + (P.tail(os.path.join(DATA, 'logs', 'save_map.log'), 6) or '看 data/logs/save_map.log')

    # ---------- 续建（docs/地图续建.md） ----------
    # 定位、加载与首次自检都完成后才开始持续采集。
    PARKING_FILE = 'parkings.json'

    def parkings(self):
        """data/parkings.json：按基准图名存的续建停车点 {'<map>': {'pose': [x, y, yaw]}}。"""
        path = os.path.join(DATA, self.PARKING_FILE)
        try:
            with open(path, encoding='utf-8') as f:
                raw = json.load(f)
        except (OSError, ValueError):
            return {}
        out = {}
        for name, rec in (raw or {}).items():
            pose = (rec or {}).get('pose')
            if (isinstance(name, str) and P.SAFE_NAME.fullmatch(name)
                    and isinstance(pose, (list, tuple)) and len(pose) == 3
                    and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in pose)):
                out[name] = {'pose': [float(v) for v in pose]}
        return out

    def set_parking(self, base, pose=None):
        """把机器人现在的位置记成这张基准图的续建停车点；pose=None 是删掉。

        停车点是三级降级里的第 1 级：机器人停在框内就直接用，可重复、零耗时，
        也是唯一不需要每次重新定位的一级。
        """
        if not isinstance(base, str) or not P.SAFE_NAME.fullmatch(base):
            return False, '基准图名不合法'
        path = os.path.join(DATA, self.PARKING_FILE)
        data = {k: v for k, v in self.parkings().items()}
        if pose is None:
            if base not in data:
                return False, f'{base} 上没有存过停车点'
            del data[base]
            msg = f'已删掉 {base} 的续建停车点'
        else:
            if not (isinstance(pose, (list, tuple)) and len(pose) == 3
                    and all(isinstance(v, (int, float)) and not isinstance(v, bool)
                            and math.isfinite(v) for v in pose)):
                return False, '位姿必须是 [x, y, yaw]'
            data[base] = {'pose': [float(pose[0]), float(pose[1]), MC.normalize_yaw(pose[2])]}
            msg = (f"已记下 {base} 的续建停车点 "
                   f"({pose[0]:.2f}, {pose[1]:.2f}, {math.degrees(pose[2]):.0f}°)")
        tmp = path + '.tmp'
        try:
            os.makedirs(DATA, exist_ok=True)
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, path)
        except OSError as e:
            return False, f'存不下停车点：{e}'
        self.node.event('INFO', msg)
        return True, msg

    def save_parking_pose(self):
        """存图这一刻机器人在建图 map 系里的位姿，用来顺手记成新图的停车点。返回 (pose, 为什么没有)。

        存下的 pgm/yaml 就在建图时的 map 坐标系里（全新建图是 slam_toolbox 新建的 map 系；
        续建自检通过后 map 系和基准图对齐，存的新图也还是这个系），所以此刻的
        map→base_footprint 就是机器人在新图上的位置——不需要再定位。
        冻结时地图停在冻结那一刻，机器人位置可能已经和图对不上，不记。
        """
        n = self.node
        if self.map_frozen:
            return None, '建图处于冻结状态，机器人位置和存下的图不一定对得上'
        pose = n.lookup(BASE_FRAME)
        if pose is None or n.fixed != 'map':   # lookup 没有 map 时会退回 odom，那不算
            return None, '没有 map→base_footprint'
        if not all(math.isfinite(v) for v in pose):
            return None, '位姿不是有限数'
        return [float(v) for v in pose], ''

    def park_after_save(self, name, pose, why=''):
        """存图成功后把存图时的位置记成这张新图的停车点。返回附在保存提示后面的一句话。"""
        if pose is None:
            return f'；没有记停车点（{why}），下次续建请用自动定位' if why else ''
        ok, msg = self.set_parking(name, pose)
        if not ok:
            return f'；停车点没记上（{msg}），下次续建请用自动定位'
        tail = (f'；已把狗现在的位置记成这张图的停车点 ({pose[0]:.2f}, {pose[1]:.2f}, '
                f'{math.degrees(pose[2]):.0f}°)：狗不挪，下次续建可以直接「从停车点开始」')
        try:
            good, w = MC.check_start(self._base_grid(name), pose)
            if not good:
                tail += f'；注意：{w}'
        except RuntimeError:
            pass
        return tail

    def current_parking_pose(self):
        """现在就能当停车点用的位姿：有 map→base_footprint 才有意义（没有定位就是在猜）。"""
        n = self.node
        if n.fixed != 'map':
            return None
        return n.lookup(BASE_FRAME)

    def cont_state(self):
        """推给网页的续建状态。None 表示没有正在进行的续建。"""
        if not self.cont:
            return None
        c = self.cont
        return {'active': True, 'base': c['base'], 'name': c['name'], 'source': c['source'],
                'pose': c['pose'], 'note': c['note'], 'warn': c['warn'],
                'ready': c['ready'], 'check': c.get('check'), 'elapsed': round(time.monotonic() - c['started'], 1)}

    def cont_preflight(self, base):
        """续建前能不能开始。不动机器人，纯查文件。"""
        if not isinstance(base, str) or not P.SAFE_NAME.fullmatch(base):
            return False, '基准图名不合法'
        maps_dir = os.path.join(DATA, 'maps')
        slice_now, slice_src = self._slice_now()
        # 读不到真实配置时不能拿默认值去比：那会以「和基准一致」通过，而实际上什么都没核对。
        # 传 None 让 preflight 走「没有记录，无法核对」那条告警。
        # _slice_now() 读到文件时返回的来源是文件路径，读不到才是 'default'（以前这里比的是 'config'，
        # 永远不成立：读到了也从没和基准比过，还一直报「读不到」）
        found_slice = slice_src != 'default'
        checked_slice = slice_now if found_slice else None
        policy, policy_src = self.policy_now()
        ok, reasons, warns = MM.preflight(maps_dir, base, policy=policy,
                                          slice_now=checked_slice)
        if not policy:
            warns.append('读不到当前运控策略（环境变量和 runtime.env 里都没有 GO2W_CONTROL_POLICY），'
                         '没法核对站姿——续建前请确认没换过策略')
        if not found_slice:
            warns.append(f'读不到 pcl2scan_slam.yaml，没法核对切片高度（用的是默认值 '
                         f'{slice_now[0]:.2f}–{slice_now[1]:.2f} m）——续建前请确认它没被改过')
        park = self.parkings().get(base)
        detail = {'base': base, 'ok': ok, 'reasons': reasons, 'warns': warns,
                  'chain': MM.lineage(maps_dir, base), 'slice': slice_now, 'slice_source': slice_src,
                  'policy': policy, 'policy_source': policy_src, 'parking': park.get('pose') if park else None}
        if not ok:
            return False, '；'.join(reasons), detail
        return True, ('可以续建' + ('（注意：' + '；'.join(warns) + '）' if warns else '')), detail

    def _slice_now(self):
        for d in CONFIG_DIRS:
            if d:
                got, src = MM.current_slice(config_dir=d)
                if src == 'config':
                    return got, os.path.join(d, 'pcl2scan_slam.yaml')
        return MM.current_slice(config_dir='/nonexistent')

    def _base_grid(self, name, rec=None):
        if rec is None:
            try:
                rec = mapfile.load(os.path.join(DATA, 'maps', name + '.yaml'))
            except (OSError, ValueError, KeyError) as e:
                raise RuntimeError(f'读不出基准图 {name}：{e}') from e
        h, w = rec['grid'].shape
        return rec['ox'], rec['oy'], w, h, rec['res']

    async def _globloc_now(self, base, rec=None):
        """拿当前静止 scan 在**基准图**上做全局定位，返回 globloc 的结果 dict 或 None。

        为什么不用 n.map_raw：建图档位跑着的时候 /map 是 slam_toolbox 的实时图（此刻还是空的
        或只有刚扫到的一点），拿它去定位会得到完全无关的位置。续建要找的是磁盘上那张基准图。
        """
        # 失败时返回 {'error': 原因}（MC.accept_globloc 认这个键），不要返回 None：
        # 原因丢掉的话界面只能说「没有结果」，人不知道是在动、没站稳还是图读不出
        scans, why = self.autoloc_scans()
        if scans is None:
            return {'error': f'拿不到静止的激光（{why}）：让机器人停稳几秒再点'}
        try:
            rec = rec or mapfile.load(os.path.join(DATA, 'maps', f'{base}.yaml'))
        except (OSError, ValueError, KeyError) as e:
            return {'error': f'读不出基准图 {base}：{e}'}
        # globloc 要 OccupancyGrid 的原始 int8（未知是 -1），mapfile.load 给的正是这个口径
        grid = np.ascontiguousarray(rec['grid'].astype(np.int8))
        fd, path = tempfile.mkstemp(prefix='go2w_cont_', suffix='.npz')
        os.close(fd)
        try:
            np.savez(path, grid=grid, res=rec['res'], origin=np.array([rec['ox'], rec['oy']]),
                     pts=np.concatenate(scans).astype(np.float32))
            script = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'globloc.py')
            proc = await asyncio.create_subprocess_exec(
                'nice', '-n', '10', 'python3', script, path,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            out, _ = await asyncio.wait_for(proc.communicate(), AUTOLOC_TIMEOUT)
            if proc.returncode != 0:
                return {'error': f'自动定位程序出错（退出码 {proc.returncode}）'}
            return json.loads(out.decode().strip().splitlines()[-1])
        except asyncio.TimeoutError:
            return {'error': f'自动定位 {AUTOLOC_TIMEOUT:.0f} s 内没算完'}
        except (ValueError, OSError, IndexError) as e:
            return {'error': f'自动定位没有可用结果（{e}）'}
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    async def set_scan_budget(self, budget):
        if self.map_frozen and budget != 0:
            raise RuntimeError(f"建图已冻结（{self.map_frozen['reason']}），不能恢复采集")
        await self.gate_set(budget)
        self.cont_blocked = budget != -1

    async def gate_set(self, budget):
        cli = self.node.scan_gate_set
        if not cli.service_is_ready():
            raise RuntimeError('建图扫描闸门不可用，请更新运行镜像并重开建图')
        p = Parameter()
        p.name = 'scan_budget'
        p.value = ParameterValue(type=ParameterType.PARAMETER_INTEGER, integer_value=budget)
        req = SetParameters.Request()
        req.parameters = [p]
        response = await self.aw(cli.call_async(req))
        if len(response.results) != 1 or not response.results[0].successful:
            raise RuntimeError('建图扫描闸门没有应用新状态')

    async def mapping_save_allowed(self):
        if self.cont_blocked or self.cont_busy:
            return False
        if self.map_frozen:
            return True   # 冻结时闸门是故意关的，地图停在冻结那一刻，允许手动另存
        cli = self.node.scan_gate_get
        if not cli.service_is_ready():
            # 普通建图允许旧运行镜像，续建要求有扫描闸门。
            return True
        req = GetParameters.Request()
        req.names = ['scan_budget']
        try:
            values = (await self.aw(cli.call_async(req), GATE_QUERY_TIMEOUT)).values
        except Exception as e:
            # 11-20 真机：负载 8–10 时闸门的参数服务 3 s 内回不来，普通建图被误报「续建还在验证」存不了图。
            # 闸门只有网页自己会关（关时 cont_blocked 已置位，冻结另有 map_frozen），没在续建就放行；续建中仍然拒绝。
            if self.cont is None:
                self.node.event('WARN', f'问建图扫描闸门状态没有回应（{GATE_QUERY_TIMEOUT:.0f} s，{type(e).__name__}）；'
                                        '没有在续建，按闸门打开处理，照常保存')
                return True
            return False
        return (not self.cont_blocked and not self.cont_busy and len(values) == 1
                and values[0].type == ParameterType.PARAMETER_INTEGER
                and values[0].integer_value == -1)

    def cont_scan_ready(self):
        scans, why = self.autoloc_scans()
        if scans is None:
            raise RuntimeError(why)
        n = self.node
        odom = self.fresh_odom()
        if odom is None or not n.healthy or time.monotonic() - n.healthy_t >= STALE:
            raise RuntimeError('里程计不健康或没有新数据，停稳并检查里程计后再试')
        return odom

    @staticmethod
    def cont_same_pose(first, other):
        return (math.hypot(first[0] - other[0], first[1] - other[1]) <= 0.05
                and abs(MC.yaw_diff(first[2], other[2])) <= math.radians(3))

    async def cont_wait_map(self, version, base_grid):
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            seq, loaded = self.node.map_sample
            if seq > version:
                version = seq
                if loaded is not None and await asyncio.to_thread(MC.matches_loaded_grid, base_grid, loaded):
                    return
            await asyncio.sleep(0.05)
        raise RuntimeError('加载后没有收到与基准图相符的 SLAM 地图')

    async def cont_probe(self, odom):
        n = self.node
        recent = list(n.scan_evidence)
        newest = recent[-1][0] if recent else (0, 0)
        sent_at = time.monotonic()
        # Humble 的首帧可以绕过 pause_new_measurements。输入闸门只转发一帧，
        # 后续扫描在位姿和贴合率都确认前不会进入 SLAM。
        await self.set_scan_budget(1)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            for stamp, received, pose in reversed(list(n.slam_poses)):
                if received < sent_at or stamp <= newest:
                    continue
                for scan_stamp, t, pts, still, scan_odom in reversed(list(n.scan_evidence)):
                    if scan_stamp != stamp or t < sent_at:
                        continue
                    if not still or not self.cont_same_pose(odom, scan_odom):
                        raise RuntimeError('验证帧采集时机器人已移动或里程计不可用')
                    if not all(math.isfinite(v) for v in pose):
                        raise RuntimeError('SLAM 输出了无效位姿')
                    return pts, pose
            await asyncio.sleep(0.05)
        raise RuntimeError('没有收到与验证扫描时间一致的 SLAM 位姿')

    async def cont_start(self, base, name, pose, source, reason):
        n = self.node
        if self.cont_busy or self.busy or self.map_busy or self.map_loading or self.autosave['running']:
            return False, '档位、续建或保存操作正在进行，请稍等'
        if self.prof('mapping') != 'run':
            return False, '续建要先让「建图」跑起来'
        if self.cont and self.cont['ready']:
            return False, '续建正在采集，请先保存或放弃'
        if not isinstance(base, str) or not P.SAFE_NAME.fullmatch(base):
            return False, '基准图名不合法'
        if not isinstance(name, str) or not P.SAFE_NAME.fullmatch(name) or name == base:
            return False, '续建请填写一个不同的合法地图名'
        if is_autosave(name):
            return False, 'autosave_月日_时分秒 这种名字留给建图自动保存（会被定期清理），请换个名字'
        if source not in (None, 'manual', 'parking'):
            return False, '起点来源只能是自动定位、手动起点或停车点'
        if source == 'manual':
            if not (isinstance(pose, (list, tuple)) and len(pose) == 3
                    and all(isinstance(v, (int, float)) and not isinstance(v, bool)
                            and math.isfinite(v) for v in pose)):
                return False, '手动起点要给 [x, y, 朝向(弧度)] 三个有限数'
            pose = [float(v) for v in pose]
        ok, msg, detail = self.cont_preflight(base)
        if not ok:
            return False, msg, detail
        if any(m['name'] == name for m in P.list_maps(os.path.join(DATA, 'maps'))):
            return False, f'已存在地图 {name}，请换个名字'

        self.cont_busy = True
        self.cont_hold = (base, name)
        payload = {}
        phase = 'pause'
        try:
            await self.set_scan_budget(0)
            self.cont = None
            phase = 'base'
            base_grid = mapfile.load(os.path.join(DATA, 'maps', base + '.yaml'))
            if abs(base_grid['oyaw']) > 1e-6:
                raise RuntimeError('基准图的栅格原点带旋转，请选择原始 SLAM 地图')
            phase = 'scan'
            odom = self.cont_scan_ready()
            parking = self.parkings().get(base) if source == 'parking' else None
            if source == 'parking' and not MC.choose_parking(parking)[0]:
                return False, '这张图没有记录停车点，采集已暂停'
            phase = 'localize'
            globloc = await self._globloc_now(base, base_grid) if source is None else None
            settled = MC.settle(parking, None, globloc, manual=pose, force=source or 'globloc')
            diagnostics = MC.location_diagnostics(globloc)
            payload = {'base': base, 'source': settled['source'], 'pose': settled['pose'],
                       'warn': settled['warn'], 'tried': settled['tried'], **diagnostics}
            if diagnostics.get('ratio') is not None:
                n.event('INFO', f"续建 {base} 定位分数 {diagnostics['score']:.4f}，"
                        f"次优/最优 {diagnostics['ratio']:.4f}，候选 {diagnostics['candidates']}")
            if not settled['source']:
                if len(diagnostics.get('candidates', [])) > 1:
                    ref = base_grid
                    stride = max(1, math.ceil(max(ref['grid'].shape) / 180))
                    grid = ref['grid']
                    rows, cols = grid.shape
                    padded = np.pad(grid, ((0, -rows % stride), (0, -cols % stride)), constant_values=-1)
                    view = padded.reshape(math.ceil(rows / stride), stride,
                                          math.ceil(cols / stride), stride).max(axis=(1, 3))
                    h, w = view.shape
                    payload['preview'] = {'w': w, 'h': h, 'res': ref['res'] * stride,
                                          'ox': ref['ox'], 'oy': ref['oy'],
                                          'grid': base64.b64encode(view.tobytes()).decode('ascii')}
                return False, f"续建已暂停：{settled['tried'][-1][1]}。请换个视角或给出手动起点", payload
            good, why = MC.check_start(self._base_grid(base, base_grid), settled['pose'])
            if not good:
                return False, why, payload
            if not self.cont_same_pose(odom, self.cont_scan_ready()):
                raise RuntimeError('定位期间机器人已移动，请停稳重试')

            phase = 'load'
            self.cont = {'base': base, 'name': name, 'source': settled['source'], 'pose': settled['pose'],
                         'note': settled['note'], 'warn': settled['warn'], 'reason': reason,
                         'globloc': diagnostics or None, 'check': None, 'started': time.monotonic(),
                         'ready': False}
            # 给闸门关闭前已经进入 TF 队列的帧留出处理时间。反序列化会替换当前图。
            await asyncio.sleep(0.25)
            version = n.map_sample[0]
            await self.aw(n.deserialize_posegraph(base, settled['pose']), 30.0)
            await self.cont_wait_map(version, base_grid)
            if not self.cont_same_pose(odom, self.cont_scan_ready()):
                raise RuntimeError('加载基准图期间机器人已移动，请停稳重试')
            phase = 'probe'
            points, measured = await self.cont_probe(odom)
            phase = 'check'
            report = await self.cont_measure(base, points, measured)
            self.cont_record_check(report, measured)
            if not report['ok']:
                return False, report['reason'], dict(payload, check=report)
            if not self.cont_same_pose(odom, self.cont_scan_ready()):
                raise RuntimeError('验证期间机器人已移动，请停稳重试')
            phase = 'resume'
            await self.set_scan_budget(-1)
            self.cont['ready'] = True
            return True, f"自检通过，开始续建 {base} → {name}", dict(payload, check=report)
        except Exception as e:
            if self.cont_blocked:
                try:
                    await self.set_scan_budget(0)
                except Exception as gate_error:
                    n.event('ERROR', f'确认续建闸门关闭失败：{gate_error}')
            msg = str(e) or '操作超时'
            if self.cont:
                self.cont['check'] = {'ok': False, 'reason': msg, 'code': phase}
            return False, msg, dict(payload, code=phase)
        finally:
            self.cont_busy = False
            self.cont_hold = ()

    def _tool_script(self, name):
        """找工具脚本。扩展坞上 release 里是 web/current/tools，仓库里是 scripts/calib。
        找到了才返回路径，找不到明确抛错——静默降级会让「自检没跑」被误当成「自检通过」。"""
        here = os.path.dirname(os.path.abspath(__file__))
        cands = [os.path.join(here, name),                       # 和 server 放一起（工具目录）
                 os.path.join(here, '..', 'tools', name),        # web/current/tools
                 os.path.join(here, '..', '..', 'scripts', 'calib', name),  # 仓库
                 os.path.join('/opt/go2w/console/tools', name)]
        for p in cands:
            if os.path.isfile(p):
                return os.path.abspath(p)
        raise RuntimeError(f'找不到 {name}（试过 {"; ".join(cands)}）')

    async def cont_measure(self, base, pts, pose):
        c, s = math.cos(pose[2]), math.sin(pose[2])
        world = np.stack([pose[0] + c * pts[:, 0] - s * pts[:, 1],
                          pose[1] + s * pts[:, 0] + c * pts[:, 1]], 1)
        with tempfile.TemporaryDirectory(prefix='go2w_cont_check_') as tmp:
            path = os.path.join(tmp, 'scan.npz')
            np.savez(path, pts=world.astype(np.float32))
            proc = await asyncio.create_subprocess_exec(
                'nice', '-n', '10', 'python3', self._tool_script('map_overlap_check.py'),
                os.path.join(DATA, 'maps', base + '.yaml'), path, '--json', '--require-aligned',
                f'--pivot={pose[0]:.4f},{pose[1]:.4f}',
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            try:
                out, err = await asyncio.wait_for(proc.communicate(), 25.0)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                raise RuntimeError('自检 25 秒内没有算完')
        if proc.returncode not in (0, 1):
            raise RuntimeError(err.decode(errors='replace').strip() or f'自检退出码 {proc.returncode}')
        try:
            return json.loads(out.decode().strip().splitlines()[-1])
        except (ValueError, IndexError) as e:
            raise RuntimeError('自检没有输出可解析的结果') from e

    def cont_record_check(self, report, pose):
        report['at'] = 'current'
        report['at_pose'] = [round(v, 3) for v in pose]
        if report.get('code') == 'pose_offset':
            fix = MC.start_fix(self.cont['pose'], pose, report.get('correction'))
            if fix is not None:
                report['start_fix'] = [round(v, 3) for v in fix]
        self.cont['check'] = report
        self.node.event('INFO' if report['ok'] else 'WARN',
                        f"续建自检 {self.cont['source']}：{report['reason']} "
                        f"fit={report.get('fit')} fit_best={report.get('fit_best')} "
                        f"coverage={report.get('wall_coverage')} correction={report.get('correction')}")

    async def cont_check(self):
        if not self.cont:
            return False, '没有正在进行的续建'
        if self.cont_busy or self.busy or self.map_busy:
            return False, '续建或保存操作正在进行，请稍等'
        if not self.cont['ready']:
            check = self.cont.get('check')
            return False, (check or {}).get('reason', '续建已暂停，请重新定位后开始'), check
        self.cont_busy = True
        try:
            odom = self.cont_scan_ready()
            await self.set_scan_budget(0)
            n = self.node
            now = time.monotonic()
            for stamp, t, pts, still, scan_odom in reversed(list(n.scan_evidence)):
                if not still or now - t >= 0.5 or not self.cont_same_pose(odom, scan_odom):
                    continue
                try:
                    tf = n.tf.lookup_transform('map', BASE_FRAME, rclpy.time.Time(
                        seconds=stamp[0], nanoseconds=stamp[1])).transform
                except Exception:
                    continue
                tr = tf.translation
                pose = (tr.x, tr.y, yaw_of(tf.rotation))
                report = await self.cont_measure(self.cont['base'], pts, pose)
                self.cont_record_check(report, pose)
                self.cont['ready'] = False
                if report['ok']:
                    if not self.cont_same_pose(odom, self.cont_scan_ready()):
                        raise RuntimeError('自检期间机器人已移动，请停稳重试')
                    await self.set_scan_budget(-1)
                    self.cont['ready'] = True
                return report['ok'], report['reason'], report
            raise RuntimeError('没有与当前扫描时间对应的地图位姿，请停稳后重试')
        except Exception as e:
            self.cont['ready'] = False
            self.cont['check'] = {'ok': False, 'code': 'check', 'reason': str(e)}
            try:
                await self.set_scan_budget(0)
            except Exception as gate_error:
                self.cont_blocked = True
                self.node.event('ERROR', f'暂停续建失败，请停止建图：{gate_error}')
            return False, str(e), self.cont['check']
        finally:
            self.cont_busy = False

    async def cont_finish(self, reason):
        """收工：另存新名字 → 写 meta.json → 把路线指到新图。基准图一个字节都不动。"""
        n = self.node
        c = self.cont
        if not c:
            return False, '没有正在进行的续建'
        if not c['ready'] or not c.get('check') or not c['check'].get('ok'):
            return False, '先跑自检并通过（起点位姿不对就存图，等于把双线墙固化下来）'
        if self.busy or self.map_busy or self.cont_busy or self.autosave['running']:
            return False, '档位或地图正在切换，请稍等'
        self.map_busy = True
        try:
            self.last_save_park = ''
            ok, msg = await self._save_map(c['name'])
            if not ok:
                return False, msg
            maps_dir = os.path.join(DATA, 'maps')
            slice_now, _ = self._slice_now()
            meta = MM.build(c['name'], c['base'], c['pose'], c['source'],
                            globloc=c.get('globloc'), check=c['check'],
                            policy=self.policy_now()[0], slice=slice_now,
                            free_thresh_saved=MM.FREE_THRESH_DEFAULT,
                            git_revision=os.environ.get('GO2W_GIT_REVISION'),
                            reason=(reason if isinstance(reason, str) and reason.strip() else c.get('reason')),
                            tool='web-continue')
            MM.write(maps_dir, meta)
            changed = 0
            try:
                with open(self.routes_file, encoding='utf-8') as f:
                    raw = json.load(f)
                new, changed = MM.retarget_routes(raw, c['base'], c['name'])
                if changed:
                    tmp = self.routes_file + '.tmp'
                    with open(tmp, 'w', encoding='utf-8') as f:
                        json.dump(new, f, ensure_ascii=False, indent=1)
                    os.replace(tmp, self.routes_file)
                    self.routes = {r['id']: self.route_record(r, r['id'], strict=False) for r in new}
            except FileNotFoundError:
                pass
            except (OSError, ValueError, TypeError, KeyError) as e:
                n.event('WARN', f'路线没能改到新图（{e}）：需要手动把任务地图切到 {c["name"]}')
            self.maps = P.list_maps(maps_dir)
            self.cont = None
            tail = f'，{changed} 条任务已指到新图' if changed else ''
            n.event('INFO', f"续建完成：{c['name']}（基准 {c['base']} 未改动）{tail}")
            return True, f'已保存 {c["name"]}，基准 {c["base"]} 未改动{tail}{self.last_save_park}'
        finally:
            self.map_busy = False

    # 记停车点时定位匹配率至少要这么高。网页在 < 60% 时报「匹配差」，停车点会被反复复用，再严一档
    PARKING_MIN_MATCH = 70.0

    def parking_frame_ok(self, base):
        """现在的 map 坐标系是不是 base 这张基准图的坐标系。返回 (ok, 为什么不行)。

        停车点存的是基准图坐标。建图档刚启动时 map 系是 slam_toolbox 新建的（原点就是开机位置），
        这时记下的位置和基准图毫无关系，而且不会报错——下次续建会把起点放错地方，只能靠自检拦。
        所以只在两种情况下认：
          1. 导航 / 定位档，map_server 加载的正是 base，且定位匹配率够高；
          2. 建图档里正在续建这张 base，并且自检已经通过（map 系已经和基准图对齐）。
        """
        n = self.node
        if self.prof('mapping') == 'run':
            c = self.cont
            if not c:
                return False, ('建图档里还没开始续建：此刻的 map 坐标系是 slam_toolbox 新建的，'
                               '不是基准图的，记下来的位置是错的。要记停车点，请在导航 / 定位里'
                               f'加载 {base} 并定好位后记，或者续建自检通过后再记')
            if c['base'] != base:
                return False, f"正在续建的基准是 {c['base']}，不是 {base}：停车点要记在对应的基准图上"
            if not c['ready'] or not (c.get('check') or {}).get('ok'):
                return False, '续建还没自检通过：起点位姿没确认之前，map 坐标系不一定和基准图对得上'
            return True, ''
        if self.prof('navigation') == 'run' or self.prof('localization') == 'run':
            if n.map_current != base:
                return False, (f'当前加载的地图是 {n.map_current or "（还没读到）"}，不是 {base}：'
                               f'停车点是记在基准图坐标系里的，先切到 {base}')
            q = n.match if n.fixed == 'map' else None
            if q is None and time.monotonic() < n.match_hold + 3.0:
                return False, '刚设完位姿，定位匹配率还在重新计算，过 1–2 秒再记'
            if q is None:
                return False, '还没有定位（没设初始位姿？），记下来的位置不可信'
            if q < self.PARKING_MIN_MATCH:
                return False, f'定位匹配率 {q:.0f}% 低于 {self.PARKING_MIN_MATCH:.0f}%：先把位置定准再记'
            return True, ''
        return False, '导航、定位、建图都没在跑，拿不到可信的位置'

    def cont_parking(self, base, clear=False):
        """把机器人现在的位置记成（或清掉）这张基准图的续建停车点。"""
        if not isinstance(base, str) or not P.SAFE_NAME.fullmatch(base):
            return False, '基准图名不合法'
        if clear:
            return self.set_parking(base, None)
        good, why = self.parking_frame_ok(base)
        if not good:
            return False, why
        pose = self.current_parking_pose()
        if pose is None:
            return False, ('现在没有 map 定位，记不了停车点：'
                           '没有 map→base_footprint 的话记下来的位置是错的')
        ok, msg = self.set_parking(base, list(pose))
        if ok:
            # 位置记下来了，但还要确认它在这张图里
            try:
                good, why = MC.check_start(self._base_grid(base), list(pose))
            except RuntimeError as e:
                return True, f'{msg}；但读不出基准图核对覆盖范围：{e}'
            if not good:
                return True, f'{msg}；注意：{why}'
        return ok, msg

    def reset_cont(self, why=''):
        """丢掉续建会话。档位一换、基准图换了，起点位姿就不再成立——留着比清掉更危险。"""
        if not self.cont:
            return False
        name = self.cont['name']
        self.cont = None
        if why:
            self.node.event('WARN', f'续建会话已作废（{why}）：{name} 没有保存，'
                                    f'建图档位里已有的节点仍在')
        return True

    async def cont_abort(self):
        if self.cont_busy or self.busy or self.map_busy:
            return False, '续建或保存操作正在进行，请稍等'
        if not self.cont:
            return False, '没有正在进行的续建'
        self.cont_busy = True
        try:
            await self.set_scan_budget(0)
            self.reset_cont('手动放弃')
            return True, '本次续建已暂停；可以重新选择基准图续建'
        except Exception as e:
            return False, f'没能暂停扫描，请停止建图：{e}'
        finally:
            self.cont_busy = False

    async def align_map(self, source, name, angle):
        if self.busy or self.map_loading:
            return False, '档位或地图正在切换，请稍等'
        if not all(isinstance(v, str) and P.SAFE_NAME.fullmatch(v) for v in (source, name)):
            return False, '源地图和目标地图名只能用字母、数字、下划线和减号（最多 48 字符）'
        if isinstance(angle, bool) or not isinstance(angle, (int, float)) or not math.isfinite(angle) or abs(angle) > math.pi / 2:
            return False, '对齐角度必须在 -90° 到 90° 之间'
        self.maps = P.list_maps(os.path.join(DATA, 'maps'))
        if source not in [m['name'] for m in self.maps]:
            return False, '找不到已保存的原始地图'
        self.map_busy = True
        try:
            if not await self.procs.align_map(source, name, float(angle)):
                return False, '对齐导出失败，原图已保留：' + P.tail(os.path.join(DATA, 'logs/align_map.log'), 5)
            self.maps = P.list_maps(os.path.join(DATA, 'maps'))
            self.node.event('INFO', f'对齐地图已保存：{name}，原图 {source} 用于续建')
            return True, f'已保存 {name}（PGM / YAML / PNG），原图 {source} 保留用于续建'
        finally:
            self.map_busy = False

    async def edit_map(self, d, who):
        """网页地图编辑（v1.0.2）：在 source 上应用网页发来的修改，另存为 name（mapedit.py）。不覆盖任何已有地图。"""
        source, name = d.get('source'), d.get('name')
        if self.busy or self.map_loading:
            return False, '档位或地图正在切换，请稍等'
        if not all(isinstance(v, str) and P.SAFE_NAME.fullmatch(v) for v in (source, name)):
            return False, '地图名只能用字母、数字、下划线和减号（最多 48 个字符）'
        if source == name:
            return False, '新地图要换个名字：编辑不覆盖原图'
        if self.prof('mapping') != 'stop':
            return False, '建图运行中不能保存编辑（地图还在变），先停止建图'
        crc, w, h = d.get('crc'), d.get('w'), d.get('h')
        if not all(isinstance(v, int) and not isinstance(v, bool) for v in (crc, w, h)):
            return False, '指令格式不对（缺少底图校验）'
        maps_dir = os.path.join(DATA, 'maps')
        self.map_busy = True
        try:
            changed, _ = await asyncio.to_thread(ME.save, maps_dir, source, name, d.get('runs'), crc, w, h,
                                                 d.get('summary'), self.version)
            self.maps = P.list_maps(maps_dir)
        except (ValueError, OSError) as e:
            return False, f'没有保存：{e}'
        finally:
            self.map_busy = False
        moved, note = 0, ''
        # 编辑不改分辨率和原点，禁行区（世界坐标）原样带到新图；网页「禁行区」页有没保存的修改、
        # 用户勾了一起带过去时，带的是网页发来的那份（原图的禁行区不动）
        carry = d.get('keepout') if isinstance(d.get('keepout'), list) else self.keepouts.get(source)
        if carry:
            try:
                self.keepouts[name], _ = await asyncio.to_thread(KO.save, maps_dir, name, carry, who)
                await self.send_all(self.keepout_message(), key='keepout')
                note += f'；{len(self.keepouts[name])} 个禁行区已带到新图'
            except (ValueError, OSError) as e:
                note += f'；禁行区没能带到新图（{e}）'
        if d.get('routes'):
            if self.route_running():
                note = '；有任务正在执行，路线没有改到新图'
            else:
                routes = {k: (dict(r, map=name) if r.get('map') == source else r) for k, r in self.routes.items()}
                moved = sum(1 for k in routes if routes[k].get('map') != self.routes[k].get('map'))
                if moved:
                    try:
                        self.write_routes(routes)
                        await self.send_all(self.routes_message(), key='routes')
                    except OSError as e:
                        moved, note = 0, f'；路线没能改到新图（{e}）'
        self.node.event('INFO', f'地图编辑已保存：{name}（从 {source} 改了 {changed} 格{f"，{moved} 条任务改到新图" if moved else ""}；{who}）')
        return True, f'已保存 {name}：改了 {changed} 格' + (f'，{moved} 条任务已改到新图' if moved else '') + note

    # ---------- 删除地图（11-18） ----------
    # 不是真删：<名字>.* 整套文件挪进 data/maps/.trash/<时间>_<名字>/，绑在这张图上的任务（routes.json）
    # 和显示方向（map_views.json）也一起存进去，要恢复就把文件挪回 data/maps、任务手动并回 routes.json。
    # 回收站里超过 MAP_TRASH_DAYS 天的在下次删除时清掉
    MAP_TRASH_DAYS = 14

    def map_in_use(self):
        """正在用、不能删的地图 → 原因。"""
        n, used = self.node, {}
        if any(self.prof(m) != 'stop' for m in ('localization', 'navigation')) or self.busy:
            for x in (n.map_current, n.map_loaded):
                if x:
                    used[x] = '导航 / 定位正在用'
        if self.cont and self.cont.get('base'):
            used[self.cont['base']] = '续建正在拿它当基准'
        if self.route_running():
            used[self.route['record']['map']] = '正在执行它上面的任务'
        if self.taught_open():
            used[self._taught_capture['map']] = '有未结束的示教采集草稿'
        return used

    @staticmethod
    def trash_map(maps_dir, name, dest, routes, view, label=None):
        """把 <name>.* 挪进 dest（同一个文件系统，rename 不拷数据）。返回挪走的字节数。"""
        files = [f for f in os.listdir(maps_dir) if f.startswith(name + '.') and
                 (os.path.isfile(os.path.join(maps_dir, f)) or f == name + '.taught')]
        os.makedirs(dest)
        with open(os.path.join(dest, 'deleted.json'), 'w', encoding='utf-8') as f:
            json.dump({'map': name, 'deleted': time.strftime('%Y-%m-%d %H:%M:%S'), 'files': sorted(files),
                       'routes': routes, 'view': view, 'label': label}, f, ensure_ascii=False, indent=2)
        size = 0
        # yaml 先挪：中途出错时 list_maps 就不再列这张图，不会留下「有 yaml 没 pgm」的坏图
        for fn in sorted(files, key=lambda f: not f.endswith('.yaml')):
            src = os.path.join(maps_dir, fn)
            if os.path.isdir(src) and not os.path.islink(src):
                size += sum(os.path.getsize(os.path.join(root, child)) for root, _, children in os.walk(src)
                            for child in children if not os.path.islink(os.path.join(root, child)))
            else:
                size += os.lstat(src).st_size
            os.replace(src, os.path.join(dest, fn))
        return size

    @staticmethod
    def purge_trash(trash, days):
        cut = time.time() - days * 86400
        try:
            items = os.listdir(trash)
        except OSError:
            return
        for d in items:
            path = os.path.join(trash, d)
            try:
                if os.path.isdir(path) and os.path.getmtime(path) < cut:
                    shutil.rmtree(path)
            except OSError:
                pass

    async def map_delete(self, names, who):
        if not isinstance(names, list) or not names:
            return False, '没有选要删除的地图'
        if len(names) > 200 or len(set(names)) != len(names) or not all(isinstance(x, str) and P.SAFE_NAME.fullmatch(x) for x in names):
            return False, '地图名不对'
        if self.map_loading or self.map_busy or self.cont_busy:
            return False, '地图正在切换或保存，请稍等'
        if self.autosave.get('running'):
            return False, '正在自动保存地图，请几秒后再删'
        if self.routes_error:
            return False, '任务文件读取失败，没法同时处理这些地图上的任务，请检查后端日志'
        maps_dir = os.path.join(DATA, 'maps')
        self.maps = P.list_maps(maps_dir)
        have = {m['name'] for m in self.maps}
        missing = [x for x in names if x not in have]
        if missing:
            return False, f"data/maps 里没有 {'、'.join(missing)}"
        used = self.map_in_use()
        busy = [f'{x}（{used[x]}）' for x in names if x in used]
        if busy:
            return False, '不能删除：' + '；'.join(busy)
        self.map_busy = True
        trash, stamp = os.path.join(maps_dir, '.trash'), time.strftime('%Y%m%d_%H%M%S')
        routes, views, labels = dict(self.routes), dict(self.map_views), dict(self.map_labels)
        done, size, err = [], 0, None
        try:
            await asyncio.to_thread(self.purge_trash, trash, self.MAP_TRASH_DAYS)
            for x in names:
                mine = {k: r for k, r in routes.items() if r.get('map') == x}
                dest = os.path.join(trash, f'{stamp}_{x}')
                try:
                    size += await asyncio.to_thread(self.trash_map, maps_dir, x, dest, list(mine.values()), views.get(x),
                                                    labels.get(x))
                except OSError as e:
                    err = f'{x}：{e.strerror or e}'
                    break
                for k in mine:
                    routes.pop(k)
                views.pop(x, None)
                labels.pop(x, None)
                done.append((x, len(mine)))
                self.keepouts.pop(x, None)          # <名字>.keepout.json 已随 <名字>.* 进回收站
                try:
                    KO.remove_mask(maps_dir, x)     # 渲染出来的掩膜随时能重建，直接删
                except OSError:
                    pass
            if routes != self.routes:
                self.write_routes(routes)
            if views != self.map_views:
                self.write_views(views)
            if labels != self.map_labels:
                self.write_labels(labels)
        except OSError as e:
            err = err or f'更新任务 / 显示方向失败：{e.strerror or e}'
        finally:
            self.map_busy = False
            self.maps = P.list_maps(maps_dir)
            gone = {x for x, _ in done}
            if self.map_choice in gone:
                self.map_choice = None
            if self.last_map in gone:
                self.last_map = None
            self.preview_key = None   # 正在显示的文件地图可能刚被删，下一拍按 default_map 重读
        n_routes = sum(k for _, k in done)
        if done:
            self.node.event('WARN', f"已删除地图 {'、'.join(x for x, _ in done)}（{size / 1048576:.1f} MB"
                                    f"{'，连同 ' + str(n_routes) + ' 个任务' if n_routes else ''}；挪到 data/maps/.trash，"
                                    f"{self.MAP_TRASH_DAYS} 天后自动清掉；{who}）")
        if err:
            return False, (f"删了 {len(done)} 张后出错，" if done else '删除失败：') + err
        return True, f"已删除 {len(done)} 张地图" + (f'和 {n_routes} 个任务' if n_routes else '')

    async def load_map(self, name):
        n = self.node
        if name not in [m['name'] for m in self.maps]:
            return False, f'data/maps 里没有 {name}'
        if self.taught_recording():
            await self.disarm_and_cancel('地图切换，示教草稿已保留')
        self.taught_invalidate('地图已切换，请重新定位并自检')
        if not n.loadmap_cli.service_is_ready():
            self.map_choice = name
            self.save_last_map(name)
            if self.preview_tick():
                return True, f'已切换到 {name}（导航启动时用这张图）'
            return True, f'下次启动定位 / 导航时用 {name}'
        if self.map_loading:
            return False, '正在切换地图'
        self.map_loading = True
        try:
            self.stop_route('切换地图')
            if n.nav_active():
                await self.aw(n.cancel_all(), 2.0)
            r = await self.aw(n.load_map(name), 10.0)
            if r.result != 0:
                return False, f'map_server 加载失败（错误码 {r.result}）'
            n.map_current = n.map_loaded = name
            await self.sync_taught_map(name)
        finally:
            self.map_loading = False
        await self.keepout_apply(name, '切换地图')
        n.event('WARN', f'已切换到地图 {name}：请重新设置初始位姿')
        return True, f'已切换到 {name}，请重新设置初始位姿'

    # ---------- 导航没开时也能看地图 ----------
    def default_map(self):
        """网页选择 > map_server 正在用的 > 当前部署地图 > 网页历史 > 最新地图。"""
        names = [m['name'] for m in self.maps]
        deployed = os.path.basename(os.environ.get('GO2W_MAP', ''))
        if deployed.endswith(('.yaml', '.yml')):
            deployed = os.path.splitext(deployed)[0]
        for name in (self.map_choice, self.node.map_current, deployed, self.last_map):
            if name in names:
                return name
        return names[0] if names else None

    def save_last_map(self, name):
        if not name or name == self.last_map:
            return
        self.last_map = name
        try:
            os.makedirs(DATA, exist_ok=True)
            tmp = self.last_map_file + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump({'name': name}, f)
            os.replace(tmp, self.last_map_file)
        except OSError as e:
            self.node.get_logger().warn(f'记不住上次用的地图：{e!r}')

    def shown_map(self):
        """网页上现在显示的地图名：导航 / 定位在跑时是 map_server 的图，没开时是直接读的文件；建图中为 None。"""
        n = self.node
        return n.map_file if n.map_file is not None else n.map_current

    def preview_tick(self):
        """没有任何档位在跑时，把 default_map() 从文件读出来显示（换了选择或文件改了才重读）。返回是否在显示文件地图。"""
        n = self.node
        names = set(n.get_node_names())
        if any(self.prof(m) != 'stop' for m in P.MODES) or names & {'map_server', 'slam_toolbox'}:
            self.preview_key = None
            return False
        name = self.default_map()
        if not name:
            return False
        rec = next(m for m in self.maps if m['name'] == name)
        key = (name, rec['mtime'])
        if key == self.preview_key and n.map_file == name:
            return True
        self.preview_key = key
        try:
            m = mapfile.load(os.path.join(DATA, 'maps', f'{name}.yaml'))
        except Exception as e:
            n.event('WARN', f'读不了地图文件 {name}：{e}')
            return False
        n.show_file_map(name, m)
        return True

    async def procs_poll(self):
        """每 2 s 查一次进程；判断启动完成 / 失败 / 超时；每 6 s 刷新地图、录包列表和当前地图。"""
        n, k = self.node, 0
        while True:
            k += 1
            try:
                self.procs_status = await self.procs.status()
            except Exception as e:
                if self.procs_status is not None or k == 1:
                    n.get_logger().warn(f'查不到进程状态（{e!r}），运行档位只按节点判断')
                self.procs_status = None
            try:
                await self.rollback_tick()
            except Exception as e:
                n.get_logger().warn(f'回溯录制启停失败：{e!r}')
            try:
                self.stuck_tick()
            except Exception as e:
                n.get_logger().warn(f'卡住检测失败：{e!r}')
            try:
                self.autosave_tick()
            except Exception as e:
                n.get_logger().warn(f'建图自动保存检查失败：{e!r}')
            try:
                await self.bridge_tick()
            except Exception as e:
                n.get_logger().warn(f'遥控速度桥启停失败：{e!r}')
            names = set(n.get_node_names())
            for m, t0 in list(self.starting.items()):
                age = time.monotonic() - t0
                if all(x in names for x in MODE_NODES[m]):
                    self.starting.pop(m); self.transitions.pop(m, None)
                    n.event('INFO', f'{self.MODE_NAME[m]}已启动')
                    if AUTOLOC_ON_START and m in ('localization', 'navigation'):
                        self.background(self.auto_localize_on_start(m))
                elif self.procs_status is not None and not self.procs_status.get(m) and age > 4:
                    self.starting.pop(m); self.transitions.pop(m, None)
                    n.event('ERROR', f'{self.MODE_NAME[m]}启动失败，进程退出了。data/logs/navigation.log 最后几行：\n'
                            + P.tail(os.path.join(DATA, 'logs', 'navigation.log'), 8))
                elif age > START_TIMEOUT:
                    self.starting.pop(m); self.transitions.pop(m, None)
                    n.event('ERROR', f'{self.MODE_NAME[m]} {START_TIMEOUT:.0f} s 内没有起来，看 data/logs/navigation.log')
            if k % 3 == 1:
                self.maps = P.list_maps(os.path.join(DATA, 'maps'))
                try:
                    await self.bag_quota()   # 顺带刷新 self.bags
                except Exception as e:
                    self.bags = P.list_bags(os.path.join(DATA, 'bags'))
                    n.get_logger().warn(f'录包容量检查失败：{e!r}')
                n.fetch_map_current()
            if n.map_current and n.map_file is None:
                self.save_last_map(n.map_current)
            try:
                self.preview_tick()
            except Exception as e:
                n.get_logger().warn(f'显示地图文件失败：{e!r}')
            await asyncio.sleep(2.0)

    # ---------- 键盘遥控 ----------
    def on_teleop(self, ws, d):
        """网页按住按键时每 0.1 s 发一次；后端 0.3 s 收不到就发零速并停止发布。"""
        n, now = self.node, time.monotonic()
        if self._taught_job is not None:
            refuse = '示教正在定位自检，请保持静止'
        elif self.taught_open() and self.taught_owner is not None and self.taught_owner is not ws:
            refuse = '另一个网页正在进行示教采集'
        elif self.route_running() or self.checking_route:
            refuse = '多点任务或路线检查进行中，请先结束'
        else:
            refuse = '急停中' if self.estop else '底盘锁定中，先解锁' if not n.is_armed() else None
        if refuse:
            if now - self.clients[ws].get('teleop_warned', 0) > 2:
                self.clients[ws]['teleop_warned'] = now
                self.tx_put(ws, json.dumps({'t': 'toast', 'level': 'err', 'msg': f'遥控没有生效：{refuse}'}))
            return
        self.owner = ws
        if self.teleop is None and n.nav_active():
            self.background(self.aw(n.cancel_all(), 2.0))   # 遥控和导航不能同时发 /cmd_vel
            n.event('WARN', '开始键盘遥控：已取消导航目标')
        c = lambda v, m: max(-m, min(m, float(v)))
        self.teleop = (c(d.get('v', 0), TELEOP_MAX[0]), c(d.get('vy', 0), TELEOP_MAX[1]), c(d.get('w', 0), TELEOP_MAX[2]), now)

    def teleop_tick(self):
        if self.teleop is None:
            return
        if time.monotonic() - self.teleop[3] < TELEOP_TIMEOUT and not self.estop:
            self.node.publish_cmd(*self.teleop[:3])
        else:
            self.node.publish_cmd(0.0, 0.0, 0.0)   # 松手 / 断线：零速一次，然后不再发布，把 /cmd_vel 还给 Nav2
            self.teleop = None

    # ---------- topic 查看 ----------
    def set_inspect(self, ws, topic, on):
        watching = self.clients[ws].setdefault('inspect', set())
        if on and topic not in watching:
            if topic not in self.node.inspect and not self.node.inspect_on(topic):
                self.tx_put(ws, json.dumps({'t': 'msg', 'topic': topic, 'text': None, 'err': '这个 topic 现在没有发布者'}))
                return
            watching.add(topic)
            self.node.inspect[topic]['sent'] = -1
        elif not on and topic in watching:
            watching.discard(topic)
            if not any(topic in c.get('inspect', ()) for c in self.clients.values()):
                self.node.inspect_off(topic)

    # ---------- HTTP / WebSocket ----------
    async def index(self, _req):
        return web.FileResponse(os.path.join(self.static_dir, 'index.html'))

    async def ws(self, req):
        ws = web.WebSocketResponse(max_msg_size=1 << 20)
        await ws.prepare(req)
        remote = client_ip(req)
        c = self.clients[ws] = {'remote': remote, 'last_ping': time.monotonic(), 'layers': set(),
                                'txq': deque(), 'txlatest': {}, 'txev': asyncio.Event(), 'transport': req.transport}
        c['tx'] = asyncio.create_task(self.tx_run(ws, c))
        self.node.get_logger().info(f'client connected: {remote} ({len(self.clients)})')
        try:
            self.tx_put(ws, self.routes_message(), 'routes')
            self.tx_put(ws, self.views_message(), 'views')
            self.tx_put(ws, self.labels_message(), 'labels')
            self.tx_put(ws, self.keepout_message(), 'keepout')
            self.tx_put(ws, self.incidents_message(), 'incidents')
            self.tx_put(ws, self.audio.message(), 'audio')
            taught_map = (self._taught_capture or {}).get('map') or self.shown_map()
            if taught_map:
                c['taught_map'] = taught_map
                self.tx_put(ws, json.dumps({'t':'taught', **await self.taught_document(taught_map)}), 'taught')
            if self.node.map_blob:
                self.tx_put(ws, self.node.map_blob, 'map')
            if self.node.plan:
                self.tx_put(ws, json.dumps({'t': 'plan', 'pts': self.node.plan}), 'plan')
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                try:
                    d = json.loads(msg.data)
                except ValueError:
                    continue
                if d.get('t') == 'ping':
                    self.clients[ws]['last_ping'] = time.monotonic()
                    self.tx_put(ws, json.dumps({'t': 'pong', 'ts': d.get('ts')}), 'pong')
                elif d.get('t') == 'sub':
                    # 网页打开 / 关闭了哪些重图层；新打开的立刻补发最近一帧（全局代价地图可能很久才变一次）
                    new = {x for x in d.get('layers', []) if x in HEAVY}
                    added = new - self.clients[ws]['layers']
                    self.clients[ws]['layers'] = new
                    for layer in added:
                        blob = self.node.blobs[HEAVY[layer]]
                        if blob:
                            self.tx_put(ws, blob, layer)
                elif d.get('t') == 'teleop':
                    self.on_teleop(ws, d)
                elif d.get('t') == 'cam':
                    r = await self.camera.request(ws, remote, bool(d.get('on')))
                    self.tx_put(ws, json.dumps({'t': 'cam', **r}))
                elif d.get('t') == 'inspect' and isinstance(d.get('topic'), str):
                    self.set_inspect(ws, d['topic'], bool(d.get('on')))
                elif d.get('t') == 'motor_fast':   # 网页在看电机力矩：lowstate 读得勤一点
                    self.clients[ws]['mfast'] = bool(d.get('on'))
                    self.node.motor_fast = any(c.get('mfast') for c in self.clients.values())
                elif d.get('t') == 'cmd':
                    # 指令可能要等几秒（Nav2 接受目标、服务回复），单独跑，不耽误这条连接上的心跳
                    t = asyncio.create_task(self.run_cmd(ws, d))
                    self.pending.add(t)                    # asyncio 只弱引用任务，要自己持有
                    t.add_done_callback(self.pending.discard)
        finally:
            c['tx'].cancel()
            for topic in list(self.clients.get(ws, {}).get('inspect', ())):
                self.set_inspect(ws, topic, False)
            await self.camera.release(ws)
            self.clients.pop(ws, None)
            self.node.motor_fast = any(c.get('mfast') for c in self.clients.values())
            self.node.get_logger().info(f'client gone: {remote} ({len(self.clients)})')
            if self.climb is not None and self.climb['ws'] is ws:
                self.climb_off(f'开启爬台模式的网页断开（{remote}）')
            if ws is self.owner:
                self.owner = None
                await self.lose_owner(f'控制方网页断开（{remote}）', owner=ws)
            else:
                await self.taught_owner_lost(ws, f'采集页面断开（{remote}），草稿已保留')
        return ws

    async def lose_owner(self, reason, owner=None):
        n = self.node
        if self.route_running() or n.is_armed() or n.nav_active():
            task = self.incident_task()      # 停车会结束任务：先取任务信息
            await self.disarm_and_cancel(reason)
            self.incident('safety_stop', reason, task=task)
        if owner is not None:
            await self.taught_owner_lost(owner, reason)

    def wants(self, layer):
        return any(layer in c['layers'] for c in list(self.clients.values()))

    async def send_all(self, data, layer=None, key=None):
        """发给所有网页；layer 不为空时只发给打开了这个图层的网页。
        只放进每个网页自己的发送队列，不等网络：一个网页慢 / 卡住不会拖住推送循环和其他网页。
        key 不为空时同一个 key 只保留最新一条（位姿、地图这类，网慢时丢旧帧，不排队）。"""
        for ws, c in list(self.clients.items()):
            if layer is None or layer in c['layers']:
                self.tx_put(ws, data, key)

    def tx_put(self, ws, data, key=None):
        c = self.clients.get(ws)
        if c is None or c.get('txdead'):
            return
        if key is None:
            if len(c['txq']) >= TX_QUEUE_MAX:
                self.tx_fail(ws, c, f'发送积压超过 {TX_QUEUE_MAX} 条')
                return
            c['txq'].append(data)
        else:
            c['txlatest'][key] = data       # 还没发出去的旧帧直接换成新的
        c['txev'].set()

    async def tx_run(self, ws, c):
        """每个网页一个发送任务：先发排队的消息（指令回复、日志），再发最新帧，小的 JSON 先于大的二进制。"""
        try:
            while True:
                await c['txev'].wait()
                c['txev'].clear()
                while c['txq'] or c['txlatest']:
                    if c['txq']:
                        data = c['txq'].popleft()
                    else:
                        latest = c['txlatest']
                        data = latest.pop(min(latest, key=lambda k: isinstance(latest[k], bytes)))
                    if isinstance(data, bytes):
                        await asyncio.wait_for(ws.send_bytes(data), TX_TIMEOUT)
                    else:
                        await asyncio.wait_for(ws.send_str(data), TX_TIMEOUT)
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            self.tx_fail(ws, c, f'一条消息 {TX_TIMEOUT:.0f} s 没发出去')
        except Exception as e:
            self.tx_fail(ws, c, f'发送出错 {e!r}'[:200])

    def tx_fail(self, ws, c, why):
        if c.get('txdead'):
            return
        c['txdead'] = True
        if not ws.closed:
            self.node.get_logger().warn(f"网页 {c['remote']}：{why}，断开（网页会自动重连）")
        self.background(self.tx_close(ws, c))

    async def tx_close(self, ws, c):
        try:
            await asyncio.wait_for(ws.close(), 2.0)
        except Exception:
            pass
        tr = c.get('transport')
        if tr is not None and not tr.is_closing():   # 关闭握手没完成（对面不读了）：直接断 TCP，让 ws() 收尾
            tr.abort()

    async def safety(self):
        """心跳和急停的周期检查，和推送分开，不受网络慢的客户端影响。"""
        n, k = self.node, 0
        while True:
            await asyncio.sleep(0.1)
            k += 1
            try:
                self.teleop_tick()
                o = self.owner
                if o is not None and o in self.clients:
                    gap = time.monotonic() - self.clients[o]['last_ping']
                    if gap > self.heartbeat:
                        self.owner = None
                        await self.lose_owner(f"控制方网页心跳丢失 {gap:.1f} s（{self.clients[o]['remote']}）", owner=o)
                if self.route and self.route['status'] == 'active':
                    reason = self.route_blocker(self.owner, self.route['record'])
                    if reason:
                        task = self.incident_task()
                        await self.disarm_and_cancel(reason)
                        if not self.estop:      # 急停叫停的任务已经记成「急停」
                            self.incident('safety_stop', f'巡逻任务被叫停：{reason}', task=task)
                elif k % 2 == 0 and n.nav_active() and self.ctrl_op != 'down':
                    # 单个导航目标 / 巡逻中机器狗掉进 Passive（遥控器 LT+B、摔倒保护）：取消，别让 Nav2 对着趴着的狗一直发速度
                    s, _, _ = self.ctrl_get()
                    if s is not None and s != 'Velocity':
                        await self.disarm_and_cancel(f'机器狗不在行走状态（运控状态 {s}）')
                        self.incident('safety_stop', f'导航中机器狗不在行走状态（运控状态 {s}），已取消导航并上锁')
                if k % 2 == 0:
                    self.fall_tick()
                    self.climb_tick()
                if k % 5 == 0:
                    self.hot_tick()
                    self.idle_tick()
                    self.ctrl_watch()
                if k % 10 == 0:
                    self.lidar_watch()
                    self.rtab_tick()
                # 急停期间底盘被其他客户端解锁 → 再次上锁。
                # 只认急停 1 s 之后收到的「已解锁」，避免把上锁之前的旧状态当成又被解锁
                if self.estop and n.is_armed() and n.armed_t > self.estop_t + 1.0 and k % 5 == 0:
                    await self.disarm_and_cancel('急停期间底盘被解锁')
                    self.incident('safety_stop', '急停期间底盘被别处解锁，已重新上锁')
                if k % 50 == 0:
                    n.fetch_params()
            except Exception as e:  # 安全检查不能因为一次异常停掉
                n.get_logger().error(f'safety: {e!r}')

    async def broadcast(self):
        n = self.node
        map_ver = scan_ver = plan_ver = 0
        vers = {k: 0 for k in HEAVY.values()}
        k = 0
        while True:
            await asyncio.sleep(0.05)  # 20 Hz 节拍
            k += 1
            # 没有客户端时也记下地图版本：新客户端连上时 ws() 已经单独发过当前地图
            new_map = n.map_ver != map_ver
            map_ver = n.map_ver
            if not self.clients:
                n.events.clear()
                continue
            try:
                await self.send_all(json.dumps(n.pose()), key='pose')
                if k % 2 == 0 and n.scan_ver != scan_ver:  # scan 最多 10 Hz
                    scan_ver = n.scan_ver
                    await self.send_all(n.scan_blob, key='scan')
                if new_map:
                    await self.send_all(n.map_blob, key='map')
                if k % 4 == 0:  # 任务 5 Hz
                    await self.send_all(json.dumps({'t': 'task', 'task': self.task()}), key='task')
                if n.plan_ver != plan_ver:
                    plan_ver = n.plan_ver
                    await self.send_all(json.dumps({'t': 'plan', 'pts': n.plan}), key='plan')
                for layer, kind in HEAVY.items():
                    if n.vers[kind] != vers[kind]:
                        vers[kind] = n.vers[kind]
                        await self.send_all(n.blobs[kind], layer, key=layer)
                if n.events:  # 日志攒一批一起发（/rosout 可能一秒几十条）
                    items = []
                    while n.events:
                        items.append(n.events.popleft())
                    await self.send_all(json.dumps({'t': 'logs', 'items': items}))
                if k % 10 == 0:  # 状态 2 Hz
                    st = n.state(self.sys.read(), self.procs_status, self.transitions)
                    # mapCurrent = 网页上显示的地图（导航没开时是文件地图）；mapLive = 它来自 map_server，可以定位 / 规划
                    st.update(mapCurrent=self.shown_map(), mapLive=n.map_file is None and n.map_current is not None)
                    st.update(estop=self.estop, busy=self.busy, mapBusy=self.map_busy, maps=self.maps, bags=self.bags, uploads=self.uploads, mapChoice=self.map_choice,
                              rollback={'on': self.rollback_on, 'run': bool((self.procs_status or {}).get('rollback')), 'saving': self.rollback_saving, 'seconds': ROLLBACK_SECONDS},
                              procsKnown=self.procs_status is not None, teleop=self.teleop is not None,
                              autoLoc={**self.autoloc, 'movesLeft': max(0, AUTOLOC_MOVE_MAX - self.autoloc_moves)}, ctrl=self.ctrl_view(),
                              bagLive=sorted(n for n in self.recording_names() if n) if self.prof('bag') != 'stop' else [],
                              bagSegment=P.BAG_SEGMENT if (self.procs_status or {}).get('bag_seg') else None,
                              cont=self.cont_state(), contBusy=self.cont_busy,
                              contPaused=self.cont_blocked and self.prof('mapping') == 'run',
                              autosave=self.autosave_view(), lidar=self.lidar_view(), rtab=self.rtab_view(), webVersion=self.version,
                              keepout=self.keepout_view())
                    st.update(taughtAvailable=self.taught_repo.available,
                              taughtNavigation='taught_route' in n.get_node_names(),
                              taughtNav={'on': self.taught_repo.available, 'forced': self.taught_repo.forced(),
                                         'installed': self.taught_repo.installed})
                    await self.send_all(json.dumps(st), key='state')
                # topic 查看器：只发给正在看这个 topic 的网页
                for topic, rec in list(n.inspect.items()):
                    if rec['text'] is not None and rec['ver'] != rec.get('sent'):
                        rec['sent'] = rec['ver']
                        msg = json.dumps({'t': 'msg', 'topic': topic, 'type': rec['type'], 'hz': rec['rate'].hz(), 'text': rec['text']})
                        for ws, c in list(self.clients.items()):
                            if topic in c.get('inspect', ()):
                                self.tx_put(ws, msg, ('msg', topic))
            except Exception as e:  # 别让一次异常停掉推送
                n.get_logger().warn(f'broadcast: {e!r}')

    async def push_audio(self, text):
        await self.send_all(text, key='audio')

    async def api_audio(self, req):
        """上传一段音频（网页已转成 16 kHz 单声道 WAV）。要求自定义头：别的网站的表单 / 跨域请求带不上。"""
        def bad(msg, status=400):
            return web.json_response({'ok': False, 'msg': msg}, status=status)
        remote = client_ip(req)
        if req.headers.get('X-Go2w') != '1' or not self.audio.editable(remote):
            return bad('这个入口不能上传音频', 403)
        raw = bytearray()
        async for chunk in req.content.iter_chunked(65536):
            raw += chunk
            if len(raw) > AU.BODY_MAX:
                return bad(f'音频太长：最多 {AU.DUR_MAX:.0f} s', 413)
        try:
            x = await self.audio.add(req.query.get('name', ''), bytes(raw), remote)
        except AU.AudioError as e:
            return bad(str(e))
        except OSError as e:
            return bad(f'保存音频失败：{e.strerror or e}', 500)
        return web.json_response({'ok': True, 'msg': f'已上传「{x["name"]}」', 'item': x})

    def app(self):
        @web.middleware
        async def no_cache(req, handler):  # 开发期改完前端直接刷新就生效
            resp = await handler(req)
            resp.headers['Cache-Control'] = 'no-store'
            return resp

        app = web.Application(middlewares=[no_cache])
        app.router.add_get('/', self.index)
        app.router.add_get('/version.json', lambda _r: web.FileResponse(os.path.join(self.static_dir, 'version.json')))
        app.router.add_get('/ws', self.ws)
        self.taught_routes(app)
        app.router.add_get('/api/incidents', self.api_incidents)
        app.router.add_post('/api/audio', self.api_audio)
        for sub in ('css', 'js', 'img'):   # img/：机器人照片（可选，见 static/img/README.md）
            if os.path.isdir(os.path.join(self.static_dir, sub)):
                app.router.add_static(f'/{sub}/', os.path.join(self.static_dir, sub))

        async def start(a):
            self.loop = asyncio.get_running_loop()
            self.node.on_goal_fail = lambda pose: self.loop.call_soon_threadsafe(self.goal_failed, pose)
            a['tasks'] = [asyncio.create_task(self.broadcast()), asyncio.create_task(self.safety()),
                          asyncio.create_task(self.procs_poll()), asyncio.create_task(self.camera.monitor()),
                          asyncio.create_task(self.taught_loop())]
            await self.audio.start()

        async def stop(a):
            await self.audio.close()      # 正在播就退出喊话；要在 ctrl.close() 之前
            if self.taught_recording():
                await self.disarm_and_cancel('网页服务关闭，示教草稿已保留')
            if self.route_running():
                await self.disarm_and_cancel('网页服务关闭')
                await asyncio.gather(self.route_job, return_exceptions=True)
            await self.camera.release()
            tasks = [*a['tasks'], *self.pending]
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.to_thread(self.captures.close)
            await self.procs.close()
            if self.ctrl is not None:
                self.ctrl.close()

        async def close_clients(_a):
            # 停服时先主动关掉网页连接：aiohttp 优雅退出要等所有请求处理完（默认最多 60 s），
            # 而 WebSocket 有心跳、永远不会自己结束，不关的话 supervisor 等 10 s 就判定停止失败。
            # 页面收到 1001 会显示离线并自动重连；安全收尾（解除使能、取消路线）仍在 on_cleanup 的 stop 里
            async def bye(ws):
                await asyncio.wait_for(ws.close(code=WSCloseCode.GOING_AWAY, message=b'server shutdown'), 2.0)
            await asyncio.gather(*(bye(ws) for ws in list(self.clients)), return_exceptions=True)

        app.on_startup.append(start)
        app.on_shutdown.append(close_clients)
        app.on_cleanup.append(stop)
        return app


# 经常驻服务器（192.168.1.22，nginx）转发的连接，req.remote 都是服务器的地址；
# 只有来自这些代理的请求才信任 X-Forwarded-For，别人发这个头伪造不了 IP
TRUSTED_PROXIES = set(filter(None, os.environ.get('GO2W_TRUSTED_PROXIES', '192.168.1.22').split(',')))


def client_ip(req):
    if req.remote in TRUSTED_PROXIES:
        fwd = req.headers.get('X-Forwarded-For', '').split(',')[-1].strip()
        if fwd:
            return f'{fwd} 经 {req.remote}'
    return req.remote


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=int, default=8080)
    ap.add_argument('--static', default=os.path.join(here, '..', 'static'))
    ap.add_argument('--heartbeat', type=float, default=5.0, help='控制方网页心跳超时（秒）；1.0 在狗侧 Wi-Fi 上频繁误触发（11-20 一晚 4 次），改 5.0。网页断开仍立即上锁，网页遥控另有 0.3 s 超时')
    args = ap.parse_args()

    rclpy.init()
    node = Bridge()
    executor = SingleThreadedExecutor()
    executor.add_node(node)

    def spin():
        try:
            executor.spin()
        except ExternalShutdownException:
            pass
    ros_thread = threading.Thread(target=spin, daemon=True)
    ros_thread.start()
    node.get_logger().info(f'go2w_web on http://0.0.0.0:{args.port}，心跳超时 {args.heartbeat:.1f} s')
    try:
        web.run_app(Server(node, os.path.abspath(args.static), args.heartbeat).app(), host='0.0.0.0', port=args.port,
                    print=None, handle_signals=True, shutdown_timeout=3.0)
    finally:
        executor.shutdown()
        ros_thread.join(timeout=3.0)
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == '__main__':
    main()
