#!/usr/bin/env python3
"""分段录包：一直录，每 N 秒（默认 30）切成一个独立的 mcap 包，包名是这一段的开始时间。

替代网页「开始录包」原来用的 ros2 bag record。和 --max-bag-duration 不同，每一段都是
单独的包（自己的目录和 metadata.yaml），而且 /map、/tf_static 这种只发一次的 topic
会写进每一段的开头，单独拿出任何一段都能回放、分析。段与段之间不丢消息。

正在写的那段放在 data/bags/.recording/<名字>，写完整体改名成 data/bags/<名字>，
网页列表里不会出现写了一半的包。Ctrl+C / SIGINT 时把当前这段写完再退出。
"""
import argparse
from collections import deque
import math
import os
import threading
import time

from rollback_recorder_py import BAGS, KEEP_DISTINCT, MCAP_CONFIG, SAFE_PREFIX, SAFE_TOPIC, chown_tree, msg_class, wanted

SAFE_PREFIX_LEN = 30   # 前缀 + _MMDD_HHMMSS 不能超过 SAFE_NAME 的 48


class McapWriter:
    def __init__(self, path):
        from rosbag2_py import ConverterOptions, SequentialWriter, StorageOptions
        self.w = SequentialWriter()
        self.w.open(StorageOptions(uri=path, storage_id='mcap', storage_config_uri=MCAP_CONFIG),
                    ConverterOptions('cdr', 'cdr'))

    def create_topic(self, topic, type_str):
        from rosbag2_py import TopicMetadata
        self.w.create_topic(TopicMetadata(topic, type_str, 'cdr'))

    def write(self, topic, data, t):
        self.w.write(topic, data, t)

    def close(self):
        if hasattr(self.w, 'close'):
            self.w.close()
        del self.w   # 老版本 rosbag2_py 没有 close，析构时写 metadata.yaml


class Segments:
    """按时间切段。writer_factory(path) 返回带 create_topic(topic, type) / write(topic, data, t) / close() 的对象。"""

    def __init__(self, bags_dir, prefix, seconds, writer_factory, log=print, clock=time.time_ns, local=time.localtime):
        self.bags_dir, self.prefix, self.seconds = bags_dir, prefix, seconds
        self.work = os.path.join(bags_dir, '.recording')
        self.factory, self.log, self.clock, self.local = writer_factory, log, clock, local
        self.types, self.latched = {}, {}
        self.cur = None           # {'name', 'path', 'writer', 'topics', 't0', 'n'}
        self.lock = threading.Lock()
        self.closing = []         # 后台收尾的线程
        os.makedirs(self.work, exist_ok=True)
        chown_tree(self.work)

    def taken(self, name):
        # 11-12 起回溯包也用 go2w_ 开头，同一秒可能撞名：正在存的回溯包在 .rollback 里
        return any(os.path.exists(os.path.join(d, name))
                   for d in (self.bags_dir, self.work, os.path.join(self.bags_dir, '.rollback')))

    def free_name(self, base):
        name, k = base, 1
        while self.taken(name):
            k += 1
            name = f'{base}_{k}'
        return name

    def name_for(self, t_ns):
        return self.free_name(f'{self.prefix}_' + time.strftime('%m%d_%H%M%S', self.local(t_ns / 1e9)))

    def open(self, t):
        name = self.name_for(t)
        path = os.path.join(self.work, name)
        cur = {'name': name, 'path': path, 'writer': self.factory(path), 'topics': set(), 't0': t, 'n': 0}
        self.cur = cur
        # 只发一次的 topic 放在每段开头，时间戳记成这一段的开始
        for topic, kept in self.latched.items():
            for _, data in kept:
                self._write(cur, t, topic, data)

    def _write(self, cur, t, topic, data):
        if topic not in cur['topics']:
            cur['writer'].create_topic(topic, self.types[topic])
            cur['topics'].add(topic)
        cur['writer'].write(topic, data, t)
        cur['n'] += 1

    def add(self, topic, type_str, data, latched=False, t=None):
        t = self.clock() if t is None else t
        with self.lock:
            self.types[topic] = type_str
            if latched:
                kept = self.latched.setdefault(topic, deque(maxlen=KEEP_DISTINCT.get(topic, 1)))
                for old in kept:
                    if old[1] == data:
                        kept.remove(old)
                        break
                kept.append((t, data))
            if self.cur and t - self.cur['t0'] >= self.seconds * 1e9:
                self._rotate(t)
            if self.cur is None:
                self.open(t)   # open 已经把只发一次的 topic（包括这一条）写进去了
                if latched:
                    return
            self._write(self.cur, t, topic, data)

    def tick(self):
        """定时调用：没有消息进来时也按时切段。"""
        t = self.clock()
        with self.lock:
            if self.cur and t - self.cur['t0'] >= self.seconds * 1e9:
                self._rotate(t)

    def _rotate(self, t):
        old = self.cur
        self.cur = None
        self.open(t)
        th = threading.Thread(target=self.finish, args=(old,), daemon=True)
        self.closing = [x for x in self.closing if x.is_alive()] + [th]
        th.start()

    def finish(self, cur):
        """关掉写入器，整体改名到 data/bags。失败的段留在 .recording 里，日志里说明。"""
        try:
            cur['writer'].close()
            chown_tree(cur['path'])
            final = os.path.join(self.bags_dir, cur['name'])
            if os.path.exists(final):   # 录这段期间同名的回溯包先存好了：这段换个名字，不丢数据
                old, cur['name'] = cur['name'], self.free_name(cur['name'])
                final = os.path.join(self.bags_dir, cur['name'])
                self.log(f'{old} 已经被占用，这段改名为 {cur["name"]}')
            os.rename(cur['path'], final)
            self.log(f'已保存 {cur["name"]}：{cur["n"]} 条')
        except Exception as e:
            self.log(f'保存 {cur["name"]} 失败：{e!r}，文件留在 {cur["path"]}', error=True)

    def stop(self):
        with self.lock:
            cur, self.cur = self.cur, None
        for th in self.closing:
            th.join()
        if cur:
            self.finish(cur)


def recover_leftovers(bags_dir, log=print):
    """上次异常退出留在 .recording 里的段：搬到 data/bags（没有 metadata.yaml，需要 ros2 bag reindex）。"""
    work = os.path.join(bags_dir, '.recording')
    try:
        names = os.listdir(work)
    except OSError:
        return
    for name in names:
        src, dst = os.path.join(work, name), os.path.join(bags_dir, name + '_unfinished')
        if os.path.isdir(src) and not os.path.exists(dst):
            try:
                os.rename(src, dst)
                log(f'上次没写完的 {name} 已移到 {dst}（缺 metadata.yaml，用 ros2 bag reindex 修复）', error=True)
            except OSError:
                pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--seconds', type=float, default=30.0, help='每段多长')
    ap.add_argument('--prefix', default='go2w', help='包名前缀，包名 = 前缀_MMDD_HHMMSS')
    ap.add_argument('--bags', default=BAGS)
    ap.add_argument('--all', action='store_true', help='录所有能看到的 topic（另外列出的 topic 也照录）')
    ap.add_argument('topics', nargs='*')
    args = ap.parse_args()
    if not args.topics and not args.all:
        ap.error('要么列出 topic，要么加 --all')
    if not SAFE_PREFIX.fullmatch(args.prefix):
        ap.error(f'前缀不对：{args.prefix!r}')
    if not math.isfinite(args.seconds) or not 1 <= args.seconds <= 3600:
        ap.error('--seconds 必须在 1–3600 秒之间')
    if any(not SAFE_TOPIC.fullmatch(topic) for topic in args.topics):
        ap.error('topic 名字无效')

    import rclpy
    from rclpy.node import Node
    from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy

    rclpy.init()
    node = Node('go2w_segment_recorder')

    def log(msg, error=False):
        (node.get_logger().error if error else node.get_logger().info)(msg)

    recover_leftovers(args.bags, log)
    seg = Segments(args.bags, args.prefix, args.seconds, McapWriter, log)
    subs, failed = {}, set()

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
                log(f'{topic}：找不到消息类型 {type_str}（{e}），跳过', error=True)
                continue
            latched = any(p.qos_profile.durability == DurabilityPolicy.TRANSIENT_LOCAL for p in pubs)
            reliable = all(p.qos_profile.reliability == ReliabilityPolicy.RELIABLE for p in pubs)
            qos = QoSProfile(depth=1 if latched else 100,
                             reliability=ReliabilityPolicy.RELIABLE if reliable else ReliabilityPolicy.BEST_EFFORT,
                             durability=DurabilityPolicy.TRANSIENT_LOCAL if latched else DurabilityPolicy.VOLATILE)
            subs[topic] = node.create_subscription(
                cls, topic, lambda data, t=topic, ty=type_str, la=latched: seg.add(t, ty, data, la), qos, raw=True)
            log(f'录制：{topic}（{type_str}{"，只发一次" if latched else ""}）')

    node.create_timer(2.0, discover)
    node.create_timer(0.5, seg.tick)
    discover()
    what = '所有 topic' if args.all else '%d 个 topic' % len(args.topics)
    log(f'分段录包开始：每 {args.seconds:.0f} 秒一个包，前缀 {args.prefix}，{what}')
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        seg.stop()
        log('分段录包已停止')
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == '__main__':
    main()
