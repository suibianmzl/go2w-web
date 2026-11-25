#!/usr/bin/env python3
"""回溯录制入口：优先跑 C++ 版（省 CPU：同样 --all 录点云，Python 版吃满一个核），跑不起来才退回 Python 版。

runtime supervisor 固定用 `python3 tools/rollback_recorder.py ...` 启动，所以入口名字不变。

C++ 程序从哪来（按顺序找，第一个自检通过的就 exec 过去，同一个 PID，SIGINT 照常）：
  1. tools/rollback_recorder_cpp 本身是可执行文件（老网页包的布局，打包时已编好）
  2. tools/rollback_recorder_cpp/rollback_recorder_cpp（在源码目录里编好的）
  3. 数据盘缓存 $GO2W_DATA/cache/rollback_cpp/<源码哈希>/rollback_recorder_cpp
都没有、但网页包里带了源码（tools/rollback_recorder_cpp/ 目录）→ 在狗上现编一次放进 3，
Orin 上约 1 分钟；以后同一份源码直接用缓存，网页包换了源码哈希变了才重编。
运行镜像换了导致缓存的程序加载不了 → 自检失败 → 删掉重编一次。

环境变量 GO2W_ROLLBACK_IMPL=py 强制用 Python 版。参数和行为两边一致。
`--build-only`：只编译 + 自检，不录（装新网页包后可以提前预热）。
"""
import fcntl
import hashlib
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
CPP = os.path.join(HERE, 'rollback_recorder_cpp')          # 可执行文件（老布局）或源码目录（新布局）
SRC_FILES = ('build.sh', 'CMakeLists.txt', 'rollback_recorder.cpp')
WRITER_YAML = os.path.join(HERE, 'mcap_writer.yaml')
CACHE = os.path.join(os.environ.get('GO2W_DATA') or '/go2w/data', 'cache', 'rollback_cpp')
EXE = 'rollback_recorder_cpp'
BUILD_TIMEOUT = 900
KEEP = 3                                                    # 缓存留最近几份源码的编译结果


def log(msg):
    print(f'[rollback] {msg}', file=sys.stderr, flush=True)


def is_exe(path):
    return os.path.isfile(path) and os.access(path, os.X_OK)   # 目录也有 x 权限，必须先判断是文件


def self_check(path):
    try:
        r = subprocess.run([path, '--self-check'], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as e:
        return f'{e}'
    if r.returncode != 0:
        return f'退出码 {r.returncode}：' + (r.stderr or r.stdout).strip()[-400:]
    return None


def src_hash():
    """源码 + 写包配置的哈希；缺文件返回 None（没法编）。"""
    h = hashlib.sha256()
    for path in [os.path.join(CPP, f) for f in SRC_FILES] + [WRITER_YAML]:
        try:
            with open(path, 'rb') as f:
                data = f.read()
        except OSError:
            return None
        h.update(os.path.basename(path).encode() + b'\0' + data + b'\0')
    return h.hexdigest()[:16]


def prune(keep_dir):
    try:
        dirs = [os.path.join(CACHE, d) for d in os.listdir(CACHE)
                if not d.startswith('.') and os.path.join(CACHE, d) != keep_dir]
    except OSError:
        return
    dirs = [d for d in dirs if os.path.isdir(d)]
    dirs.sort(key=lambda d: os.path.getmtime(d), reverse=True)
    for d in dirs[KEEP - 1:] + [d for d in dirs if '.tmp-' in d]:
        shutil.rmtree(d, ignore_errors=True)


def build(key):
    """编到缓存里，返回程序路径；失败返回 None。多个进程同时启动时用文件锁排队，只编一次。"""
    out_dir = os.path.join(CACHE, key)
    exe = os.path.join(out_dir, EXE)
    os.makedirs(CACHE, exist_ok=True)
    with open(os.path.join(CACHE, '.lock'), 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if is_exe(exe):
            err = self_check(exe)
            if err is None:
                return exe
            log(f'缓存的 C++ 版自检失败（{err}），删掉重编')
            shutil.rmtree(out_dir, ignore_errors=True)
        tmp = f'{out_dir}.tmp-{os.getpid()}'
        shutil.rmtree(tmp, ignore_errors=True)
        os.makedirs(tmp)
        log(f'网页包里只有 C++ 源码，现在编译（源码 {key}，约 1 分钟，期间回溯还没开始录）')
        t0 = time.monotonic()
        cmd = ['nice', '-n', '10', 'bash', os.path.join(CPP, 'build.sh'), CPP, os.path.join(tmp, EXE)]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=BUILD_TIMEOUT)
        except (OSError, subprocess.TimeoutExpired) as e:
            log(f'编译 C++ 版失败（{e}）')
            shutil.rmtree(tmp, ignore_errors=True)
            return None
        if r.returncode != 0 or not is_exe(os.path.join(tmp, EXE)):
            log(f'编译 C++ 版失败（退出码 {r.returncode}）：{(r.stderr or r.stdout).strip()[-800:]}')
            shutil.rmtree(tmp, ignore_errors=True)
            return None
        err = self_check(os.path.join(tmp, EXE))
        if err is not None:
            log(f'新编的 C++ 版自检失败（{err}）')
            shutil.rmtree(tmp, ignore_errors=True)
            return None
        os.rename(tmp, out_dir)
        log(f'C++ 版编译完成，用时 {time.monotonic() - t0:.0f} s → {exe}')
        prune(out_dir)
        return exe


def find_cpp():
    """返回可用的 C++ 程序路径；没有返回 None（调用方退回 Python 版）。"""
    for path in (CPP, os.path.join(CPP, EXE)):
        if is_exe(path):
            err = self_check(path)
            if err is None:
                return path
            log(f'{path} 自检失败（{err}）')
    if not os.path.isdir(CPP):
        log(f'没有 {CPP}，用 Python 版')
        return None
    key = src_hash()
    if key is None:
        log(f'{CPP} 里源码不全（要 {", ".join(SRC_FILES)} + tools/mcap_writer.yaml），用 Python 版')
        return None
    try:
        return build(key)
    except OSError as e:
        log(f'没法在 {CACHE} 编译 C++ 版（{e}）')
        return None


if __name__ == '__main__':
    build_only = '--build-only' in sys.argv[1:]
    if os.environ.get('GO2W_ROLLBACK_IMPL', 'cpp') == 'py':
        log('GO2W_ROLLBACK_IMPL=py，用 Python 版')
        cpp = None
    else:
        cpp = find_cpp()
    if build_only:
        log(f'C++ 版可用：{cpp}' if cpp else 'C++ 版不可用')
        sys.exit(0 if cpp else 1)
    if cpp:
        log(f'用 C++ 版（{cpp}）')
        os.execv(cpp, [cpp] + sys.argv[1:])
    log('用 Python 版（CPU 占用高，查上面的原因）')
    sys.path.insert(0, HERE)
    import rollback_recorder_py
    rollback_recorder_py.main()
