#!/usr/bin/env python3
"""RTAB 影子库辅助（网页后端按需调用）：发初始位姿并等定位 / 切建图 / pause / resume。只用 /rtab 命名空间。"""
import math
import sys
import time

import rclpy
from geometry_msgs.msg import PoseWithCovarianceStamped
from rclpy.node import Node
from std_srvs.srv import Empty
from tf2_ros import Buffer, TransformListener


def yaw_of(q):
    return math.atan2(2 * (q.w * q.z + q.x * q.y), 1 - 2 * (q.y * q.y + q.z * q.z))


def current_pose(node, timeout=5.0):
    buf = Buffer()
    TransformListener(buf, node)
    end = time.time() + timeout
    while time.time() < end:
        rclpy.spin_once(node, timeout_sec=0.1)
        try:
            t = buf.lookup_transform('map', 'base_footprint', rclpy.time.Time())
            tr = t.transform
            return tr.translation.x, tr.translation.y, yaw_of(tr.rotation)
        except Exception:
            pass
    raise RuntimeError('5 s 内拿不到 map→base_footprint（导航/定位要先在跑并且定位成功）')


def find_name(node, kind, suffix, timeout=30.0):
    """在 ROS 图里找 rtabmap 的服务 / 话题全名（可能是 /rtab/x，也可能是 /rtab/rtabmap/x），不再猜。"""
    end = time.time() + timeout
    while time.time() < end:
        names = (node.get_service_names_and_types() if kind == 'service'
                 else node.get_topic_names_and_types())
        hits = sorted(n for n, _ in names if n.startswith('/rtab') and n.endswith('/' + suffix))
        if hits:
            return hits[0]
        rclpy.spin_once(node, timeout_sec=0.2)
    names = (node.get_service_names_and_types() if kind == 'service' else node.get_topic_names_and_types())
    seen = sorted(n for n, _ in names if 'rtab' in n)
    raise RuntimeError(f'{timeout:.0f} s 内找不到 rtabmap 的{kind} …/{suffix}。看到的 rtab 相关名字：{seen or "（一个都没有）"}')


def call_empty(node, suffix):
    name = find_name(node, 'service', suffix)
    cli = node.create_client(Empty, name)
    if not cli.wait_for_service(timeout_sec=10.0):
        raise RuntimeError(f'{name} 不可用')
    fut = cli.call_async(Empty.Request())
    rclpy.spin_until_future_complete(node, fut, timeout_sec=10.0)
    if not fut.done() or fut.exception() is not None:
        raise RuntimeError(f'调用 {name} 失败：{fut.exception()!r}' if fut.done() else f'{name} 没有回应')
    print(f'已调用 {name}', flush=True)


def localize(node, x, y, yaw, timeout, force=False):
    from rtabmap_msgs.msg import Info
    srv = find_name(node, 'service', 'set_mode_mapping')
    t_info = find_name(node, 'topic', 'info')
    t_init = find_name(node, 'topic', 'initialpose')
    t_loc = find_name(node, 'topic', 'localization_pose', timeout=3.0) if any(
        n.endswith('/localization_pose') for n, _ in node.get_topic_names_and_types()) else None
    print(f'rtabmap 接口：{srv}，{t_info}，{t_init}' + (f'，{t_loc}' if t_loc else ''), flush=True)
    cli = node.create_client(Empty, srv)
    if not cli.wait_for_service(timeout_sec=10.0):
        raise RuntimeError(f'{srv} 不可用')
    hit = {}
    seen = {'n': 0, 'ref': 0}

    def on_info(msg):
        seen['n'] += 1
        seen['ref'] = getattr(msg, 'ref_id', 0)
        for f in ('proximity_detection_id', 'loop_closure_id'):
            v = getattr(msg, f, 0)
            if v and v > 0:
                hit[f] = v
    node.create_subscription(Info, t_info, on_info, 10)

    def on_loc(msg):   # 只有定位成功才发布，比 Info 字段可靠
        p = msg.pose.pose.position
        hit['localization_pose'] = '(%.2f, %.2f)' % (p.x, p.y)
    node.create_subscription(PoseWithCovarianceStamped, t_loc or t_init.rsplit('/', 1)[0] + '/localization_pose', on_loc, 10)
    pub = node.create_publisher(PoseWithCovarianceStamped, t_init, 10)
    msg = PoseWithCovarianceStamped()
    msg.header.frame_id = 'rtab_map'
    msg.pose.pose.position.x, msg.pose.pose.position.y = x, y
    msg.pose.pose.orientation.z, msg.pose.pose.orientation.w = math.sin(yaw / 2), math.cos(yaw / 2)
    end = time.time() + timeout
    sent = 0
    start, last = time.time(), time.time()
    while time.time() < end and not hit:
        if time.time() - last >= 5:
            last = time.time()
            print('  %2.0f s：收到 %d 帧，还没和旧节点对上（让狗动一动）' % (last - start, seen['n']), flush=True)
        if sent < 3 and time.time() - start >= 1.0 * sent:
            msg.header.stamp = node.get_clock().now().to_msg()
            pub.publish(msg)
            sent += 1
        for _ in range(10):
            rclpy.spin_once(node, timeout_sec=0.1)
    if not hit:
        if not force:
            raise RuntimeError(f'{timeout:.0f} s 内没和旧图对上。确认起点位姿，或把狗挪动 0.5 m / 转一下再试')
        print('没收到定位确认，按 --force 直接切建图（stop 时会报告两轮是否连上）', flush=True)
    else:
        print('已和旧图对上：', hit, flush=True)
    print('调用 /rtab/set_mode_mapping …', flush=True)
    fut = cli.call_async(Empty.Request())
    rclpy.spin_until_future_complete(node, fut, timeout_sec=10.0)
    if not fut.done():
        raise RuntimeError('切建图模式没有回应')
    if fut.exception() is not None:
        raise RuntimeError(f'切建图模式出错：{fut.exception()!r}')
    print('已切到建图模式，开始录新会话', flush=True)


def main():
    rclpy.init()
    node = Node('go2w_rtab_helper')
    try:
        if sys.argv[1] == 'pose':
            print('%.3f %.3f %.4f' % current_pose(node))
        elif sys.argv[1] == 'call':
            call_empty(node, sys.argv[2])
        elif sys.argv[1] == 'names':
            for kind in ('service', 'topic'):
                names = node.get_service_names_and_types() if kind == 'service' else node.get_topic_names_and_types()
                for _ in range(15):
                    if any('rtab' in n for n, _ in names):
                        break
                    rclpy.spin_once(node, timeout_sec=0.2)
                    names = node.get_service_names_and_types() if kind == 'service' else node.get_topic_names_and_types()
                print(kind, sorted(n for n, _ in names if 'rtab' in n), flush=True)
        elif sys.argv[1] == 'localize':
            x, y, yaw = map(float, sys.argv[2:5])
            localize(node, x, y, yaw, float(sys.argv[5]) if len(sys.argv) > 5 else 40.0,
                     force=len(sys.argv) > 6 and sys.argv[6] == 'force')
    except RuntimeError as e:
        print('失败：', e, file=sys.stderr, flush=True)
        sys.exit(1)
    except Exception as e:   # 其他异常也写清楚，别只剩一个退出码
        import traceback
        traceback.print_exc()
        print('失败（意外异常）：', repr(e), file=sys.stderr, flush=True)
        sys.exit(1)
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
