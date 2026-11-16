#!/usr/bin/env python3
"""回溯录制：把最近 N 秒的 topic 留在内存里循环覆盖，收到请求时才存成 mcap 包。

和 ros2 bag record 是两个独立进程，节点名也不同，可以同时运行。
没用 rosbag2 自带的 --snapshot-mode，因为它的缓存按字节算，而且 /map、/tf_static 这种
只发一次的 topic 会被挤出缓存，存下来的包缺地图和静态坐标变换。

由网页后端在建图 / 定位 / 导航运行时启动。保存方式：后端把包名写进 data/bags/.rollback/request，
本进程写到 .rollback/<名字> 下，写完整体改名成 data/bags/<名字>（metadata.yaml 出现 = 写完）；
失败时写 .rollback/<名字>.error。
"""
import argparse
from collections import deque
import importlib
import math
import os
import re
import shutil
import threading
import time

SAFE_NAME = re.compile(r'^[A-Za-z0-9_\-]{1,48}$')
BAGS = os.path.join(os.environ.get('GO2W_DATA', '/go2w/data'), 'bags')
SAFE_TOPIC = re.compile(r'^/[A-Za-z_][A-Za-z0-9_]*(?:/[A-Za-z_][A-Za-z0-9_]*)*$')
SAFE_PREFIX = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{0,29}$')
KEEP_DISTINCT = {'/tf_static': 8}   # 可能有多个发布者，各留一份；其余只发一次的 topic 留最新一条


class Ring:
    """最近 seconds 秒的消息；只发一次的 topic 另外长期保留，保存时放到包的开头。"""

    def __init__(self, seconds, max_bytes):
        self.seconds, self.max_bytes = seconds, max_bytes
        self.items, self.bytes, self.latched = deque(), 0, {}
        self.capped = 0   # 因为超过 max_bytes 被提前丢掉的条数（不是按时间过期的）

    def add(self, t, topic, data, latched=False):
        self.items.append((t, topic, data))
        self.bytes += len(data)
        limit = t - int(self.seconds * 1e9)
        while self.items and self.items[0][0] < limit:
            self.bytes -= len(self.items.popleft()[2])
        while self.items and self.bytes > self.max_bytes:   # 安全上限：防止吃光内存把导航挤掉
            self.bytes -= len(self.items.popleft()[2])
            self.capped += 1
        if latched:
            kept = self.latched.setdefault(topic, deque(maxlen=KEEP_DISTINCT.get(topic, 1)))
            for old in kept:
                if old[1] == data:
                    kept.remove(old)
                    break
            kept.append((t, data))

    def span(self):
        """缓存里实际覆盖了多少秒。"""
        return (self.items[-1][0] - self.items[0][0]) / 1e9 if len(self.items) > 1 else 0.0

    def snapshot(self, seconds=None):
        """按时间排序的 [(t, topic, data)]；缓存为空时返回空列表。seconds 给出时只取最近这么多秒。"""
        if not self.items:
            return []
        items = list(self.items)
        if seconds is not None:
            cut = items[-1][0] - int(seconds * 1e9)
            items = [x for x in items if x[0] >= cut]
        start = items[0][0]
        head = [(start, topic, data) for topic, kept in self.latched.items() for t, data in kept if t < start]
        return head + items


def write_bag(path, records, types, writer_cls, storage_options, converter_options, topic_metadata):
    writer = writer_cls()
    writer.open(storage_options(uri=path, storage_id='mcap'), converter_options('cdr', 'cdr'))
    for topic in sorted({r[1] for r in records}):
        writer.create_topic(topic_metadata(topic, types[topic], 'cdr'))
    for t, topic, data in records:
        writer.write(topic, data, t)
    if hasattr(writer, 'close'):
        writer.close()
    del writer


def chown_tree(path):
    if os.geteuid() != 0:
        return
    os.chown(path, 1000, 1000)
    for root, dirs, files in os.walk(path):
        for name in dirs + files:
            os.chown(os.path.join(root, name), 1000, 1000)


def save(name, records, types, bags_dir, write):
    """写到临时目录再整体改名，网页列表里不会出现写了一半的包。"""
    work = os.path.join(bags_dir, '.rollback')
    tmp, final = os.path.join(work, name), os.path.join(bags_dir, name)
    try:
        if not records:
            raise RuntimeError('缓存还是空的：topic 还没有数据')
        if os.path.exists(final):
            raise RuntimeError(f'{name} 已经存在')
        shutil.rmtree(tmp, ignore_errors=True)
        write(tmp, records, types)
        chown_tree(tmp)
        os.rename(tmp, final)
    except Exception as e:
        shutil.rmtree(tmp, ignore_errors=True)
        with open(os.path.join(work, f'{name}.error'), 'w') as f:
            f.write(str(e) or repr(e))
        return False
    return True


def wanted(node, topics, all_topics):
    """要录的 topic：命令行列出的，加上 --all 时当前能看到的所有 topic（每次 discover 重查，后起的节点也会录上）。"""
    names = list(topics)
    if all_topics:
        names += sorted(n for n, _ in node.get_topic_names_and_types() if n not in names)
    return names


def msg_class(type_str):
    pkg, kind, name = type_str.split('/')
    module = importlib.import_module(f'{pkg}.{kind}')
    if kind == 'action' and '_' in name:   # 动作的隐藏 topic，例如 NavigateToPose_FeedbackMessage
        base, part = name.rsplit('_', 1)
        return getattr(getattr(module, base).Impl, part)
    return getattr(module, name)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--seconds', type=float, default=20.0)
    # 11-12 由 400 改：录所有 topic 后也要保证 20 秒。扩展坞 8 GB，导航全开时可用约 5.5 GB；
    # 这只是防止吃光内存的安全上限，正常数据量（约 4–10 MB/s）20 秒远到不了
    ap.add_argument('--max-mb', type=int, default=1500, help='缓存安全上限，超过时丢最旧的（回溯会变短，日志里会报）')
    ap.add_argument('--bags', default=BAGS)
    ap.add_argument('--all', action='store_true', help='录所有能看到的 topic（另外列出的 topic 也照录）')
    ap.add_argument('topics', nargs='*')
    args = ap.parse_args()
    if not args.topics and not args.all:
        ap.error('要么列出 topic，要么加 --all')
    if not math.isfinite(args.seconds) or not 1 <= args.seconds <= 3600:
        ap.error('--seconds 必须在 1–3600 秒之间')
    if args.max_mb < 1:
        ap.error('--max-mb 必须是正整数')
    if any(not SAFE_TOPIC.fullmatch(topic) for topic in args.topics):
        ap.error('topic 名字无效')

    import rclpy
    from rclpy.node import Node
    from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
    from rosbag2_py import ConverterOptions, SequentialWriter, StorageOptions, TopicMetadata

    work = os.path.join(args.bags, '.rollback')
    request = os.path.join(work, 'request')
    os.makedirs(work, exist_ok=True)
    chown_tree(work)
    if os.path.exists(request):
        os.remove(request)

    rclpy.init()
    node = Node('go2w_rollback_recorder')
    ring, types, subs, failed = Ring(args.seconds, args.max_mb * 1048576), {}, {}, set()
    saving = threading.Event()
    saving_thread = None

    def write(path, records, tps):
        write_bag(path, records, tps, SequentialWriter, StorageOptions, ConverterOptions, TopicMetadata)

    def discover():
        for topic in wanted(node, args.topics, args.all):
            if topic in subs or topic in failed:
                continue
            pubs = node.get_publishers_info_by_topic(topic)
            if not pubs:
                continue
            type_str = pubs[0].topic_type
            try:
                cls = msg_class(type_str)
            except (ImportError, AttributeError, ValueError) as e:
                failed.add(topic)
                node.get_logger().error(f'{topic}：找不到消息类型 {type_str}（{e}），跳过')
                continue
            latched = any(p.qos_profile.durability == DurabilityPolicy.TRANSIENT_LOCAL for p in pubs)
            reliable = all(p.qos_profile.reliability == ReliabilityPolicy.RELIABLE for p in pubs)
            qos = QoSProfile(depth=1 if latched else 50,
                             reliability=ReliabilityPolicy.RELIABLE if reliable else ReliabilityPolicy.BEST_EFFORT,
                             durability=DurabilityPolicy.TRANSIENT_LOCAL if latched else DurabilityPolicy.VOLATILE)
            types[topic] = type_str
            subs[topic] = node.create_subscription(
                cls, topic, lambda data, t=topic, la=latched: ring.add(time.time_ns(), t, data, la), qos, raw=True)
            node.get_logger().info(f'回溯：{topic}（{type_str}{"，只发一次" if latched else ""}）')

    def worker(name, records, tps):
        try:
            t0 = time.monotonic()
            ok = save(name, records, tps, args.bags, write)
            if ok:
                node.get_logger().info(f'已保存 {name}：{len(records)} 条，用时 {time.monotonic() - t0:.1f} s')
            else:
                node.get_logger().error(f'保存 {name} 失败')
        finally:
            saving.clear()

    def poll():
        nonlocal saving_thread
        if saving.is_set() or not os.path.exists(request):
            return
        try:
            with open(request) as f:
                parts = f.read().split()
            os.remove(request)
        except OSError:
            return
        # 请求内容：「包名」或「包名 秒数」；不写秒数就存整个缓存
        name = parts[0] if parts else ''
        try:
            seconds = float(parts[1]) if len(parts) > 1 else None
        except ValueError:
            seconds = None
        if not SAFE_NAME.fullmatch(name):
            node.get_logger().error(f'包名不对：{name!r}')
            return
        if seconds is not None and seconds > ring.span() + 1:
            node.get_logger().warn(f'{name}：要 {seconds:.0f} 秒，缓存里只有 {ring.span():.1f} 秒')
        saving.set()
        # snapshot 在收消息的线程里做，不用加锁；写文件放到别的线程，缓存继续收
        saving_thread = threading.Thread(target=worker,
            args=(name, ring.snapshot(seconds), dict(types)))
        saving_thread.start()

    last_capped = [0]

    def report():
        # 每 30 秒报一次缓存大小和实际覆盖时长；被安全上限截短时报错，方便发现“回溯不满 20 秒”
        mb = ring.bytes / 1048576
        msg = f'回溯缓存 {mb:.0f} MB，覆盖 {ring.span():.1f} s（设定 {args.seconds:.0f} s，上限 {args.max_mb} MB）'
        if ring.capped > last_capped[0]:
            node.get_logger().error(msg + f'：超过上限，丢了 {ring.capped - last_capped[0]} 条，回溯不满设定时长')
            last_capped[0] = ring.capped
        else:
            node.get_logger().info(msg)

    node.create_timer(2.0, discover)
    node.create_timer(0.2, poll)
    node.create_timer(30.0, report)
    discover()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        # A normal SIGINT must finish any snapshot already writing metadata.
        # Keep its ROS logger alive until the worker has published the bag.
        if saving_thread is not None:
            saving_thread.join()
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == '__main__':
    main()
