#!/usr/bin/env python3
"""回溯录制入口：优先跑 C++ 版（rollback_recorder_cpp，省 CPU），跑不起来就退回 Python 版。

runtime supervisor 固定用 `python3 tools/rollback_recorder.py ...` 启动，所以入口名字不变。
C++ 版先跑 --self-check（库能加载、mcap 能写），通过才 exec 过去（同一个 PID，SIGINT 照常）。
环境变量 GO2W_ROLLBACK_IMPL=py 强制用 Python 版。参数和行为两边一致。
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CPP = os.path.join(HERE, 'rollback_recorder_cpp')


def cpp_ok():
    if os.environ.get('GO2W_ROLLBACK_IMPL', 'cpp') == 'py':
        return False
    if not os.access(CPP, os.X_OK):
        print(f'[rollback] 没有 {CPP}，用 Python 版', file=sys.stderr, flush=True)
        return False
    try:
        r = subprocess.run([CPP, '--self-check'], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f'[rollback] C++ 版自检失败（{e}），用 Python 版', file=sys.stderr, flush=True)
        return False
    if r.returncode != 0:
        tail = (r.stderr or r.stdout).strip()[-400:]
        print(f'[rollback] C++ 版自检失败（退出码 {r.returncode}）：{tail}，用 Python 版',
              file=sys.stderr, flush=True)
        return False
    return True


if __name__ == '__main__':
    if cpp_ok():
        print('[rollback] 用 C++ 版', file=sys.stderr, flush=True)
        os.execv(CPP, [CPP] + sys.argv[1:])
    sys.path.insert(0, HERE)
    import rollback_recorder_py
    rollback_recorder_py.main()
