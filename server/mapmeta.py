"""续建地图的元数据（`<name>.meta.json`）与续建前置检查。

## 为什么要有 meta.json

地图从这一版起是「四件套 + 一份元数据」：

    <name>.pgm  <name>.yaml  <name>.posegraph  <name>.data
    <name>.meta.json

前三件给 map_server / AMCL 用，后两件给 slam_toolbox 续建用。meta.json 记血缘：
这张图从哪来、谁接的、用什么姿势接的、当时是什么站姿。出问题时靠它回答
「为什么这张图和基准对不上」——最常见的原因是换了运控策略（站高和雷达俯仰变了，
续建会错层），或者切片高度改了。这两项一旦和基准不一致，续建的图就是废的，
所以 preflight() 直接拦下来，而不是等收工才发现墙是双线。

## 设计约束

- 续建**永远另存新名字**，绝不覆盖基准图。基准是不动产，可回滚是这套方案的安全垫。
- 续建保持地图坐标系不变，站点绝对坐标原样有效，只需要把 routes.json 里的
  map 字段指向新图（见 retarget_routes）。
- 没有 posegraph 的地图**不能**当基准：slam_toolbox 只能从 .posegraph/.data 恢复图，
  光有 .pgm/.yaml 的话续建无从谈起（lab_20261021 就是这种，它在旧默认图里）。
"""
import json
import os
import time

SCHEMA = 1

# 和 pcl2scan_slam.yaml 一致的切片高度，作为读不到配置时的兜底
SLICE_DEFAULT = (0.30, 1.60)
# save_map.sh 的 --free 口径。用默认 0.25 时未知格会被 map_server 读成空地（11-05 实测丢了 32 万个未知格）
FREE_THRESH_DEFAULT = 0.196

# 续建链最长几层。每层都让 posegraph 长胖（已 25–35 MB），且叠加一次里程计中断的风险
MAX_CHAIN = 4

START_SOURCES = ('parking', 'globloc', 'manual')


def meta_path(maps_dir, name):
    return os.path.join(maps_dir, f'{name}.meta.json')


def map_path(maps_dir, name):
    return os.path.join(maps_dir, f'{name}.yaml')


def has_posegraph(maps_dir, name):
    """.posegraph 和 .data 必须成对存在，缺一个 slam_toolbox 读不出来。"""
    return (os.path.isfile(os.path.join(maps_dir, f'{name}.posegraph'))
            and os.path.isfile(os.path.join(maps_dir, f'{name}.data')))


def read(maps_dir, name):
    try:
        with open(meta_path(maps_dir, name), encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write(maps_dir, meta):
    """原子写：半截文件比没有更糟——下次读会被当成「有元数据但字段不全」。"""
    name = meta['name']
    path = meta_path(maps_dir, name)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(meta, f, ensure_ascii=False, indent=1)
        f.write('\n')
    os.replace(tmp, path)


def build(name, parent, start_pose, start_source, **extra):
    """组装一条 meta。字段顺序固定，方便人眼比对和 diff。"""
    if start_source not in START_SOURCES:
        raise ValueError(f'start_source 只能是 {"/".join(START_SOURCES)}')
    if not (isinstance(start_pose, (list, tuple)) and len(start_pose) == 3):
        raise ValueError('start_pose 必须是 [x, y, yaw]（弧度）')
    meta = {
        'schema': SCHEMA,
        'name': name,
        'parent': parent,
        'continued_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
        'start_pose': [round(float(v), 4) for v in start_pose],
        'start_source': start_source,
    }
    for key in ('globloc', 'check', 'policy', 'slice', 'free_thresh_saved',
                'git_revision', 'reason', 'tool'):
        if extra.get(key) is not None:
            meta[key] = extra[key]
    return meta


def lineage(maps_dir, name, limit=16):
    """从这张图沿 parent 往上追到根，返回 [name, parent, ...]。防环。"""
    out, seen = [], set()
    cur = name
    while cur and cur not in seen and len(out) < limit:
        out.append(cur)
        seen.add(cur)
        meta = read(maps_dir, cur)
        cur = (meta or {}).get('parent')
    return out


def read_config(config_dir, filename):
    """读 go2w_bringup/config 下的一层 / 两层 yaml（只要 key: value，不依赖 PyYAML）。"""
    out = {}
    path = os.path.join(config_dir, filename)
    try:
        with open(path, encoding='utf-8') as f:
            for line in f:
                line = line.split('#', 1)[0]
                if ':' not in line:
                    continue
                key, value = line.split(':', 1)
                key = key.strip()
                if key:
                    out[key] = value.strip()
    except OSError:
        return None
    return out


def current_slice(config_dir=None, env=None):
    """现在这份 pcl2scan_slam.yaml 的切片高度。读不到就用默认值，并说明来源。

    来源要一起返回：调用方把 'default' 和 'config' 分开处理——核对站姿时，
    拿不到真实配置就只能告警，不能说「和基准一致」。
    """
    env = os.environ if env is None else env
    config_dir = config_dir or env.get('GO2W_CONFIG_DIR') or '/go2w/ws/src/go2w_bringup/config'
    cfg = read_config(config_dir, 'pcl2scan_slam.yaml')
    if cfg is None:
        return list(SLICE_DEFAULT), 'default'
    try:
        return [float(cfg['min_height']), float(cfg['max_height'])], 'config'
    except (KeyError, ValueError):
        return list(SLICE_DEFAULT), 'default'


# runtime.env 的候选位置。web 进程的环境变量里没有 GO2W_CONTROL_POLICY：`go2w web start`
# 只转发 WEB_ENVIRONMENT 里列出的几个键（写在 root 权限的 release 里，改它要出新 release），
# 而策略名明明白白写在 runtime.env 里，所以自己去读。
RUNTIME_ENV_FILES = ('/opt/go2w/config/runtime.env', '/etc/go2w/runtime.env')


def read_env_file(path, key):
    """从 shell 风格的 KEY="value" 文件里取一个键。读不到返回 None。"""
    try:
        with open(path, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line.startswith('export '):
                    line = line[7:].lstrip()
                if not line.startswith(key + '='):
                    continue
                value = line[len(key) + 1:].split(' #', 1)[0].strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in '"\'':
                    value = value[1:-1]
                return value or None
    except OSError:
        return None
    return None


def current_policy(env=None, env_files=None):
    """现在的运控策略名，返回 (名字或 None, 来源)。先看环境变量，再看 runtime.env。"""
    env = os.environ if env is None else env
    if env.get('GO2W_CONTROL_POLICY'):
        return env['GO2W_CONTROL_POLICY'], 'env'
    files = env_files
    if files is None:
        files = tuple(p for p in (env.get('GO2W_RUNTIME_ENV'),) if p) + RUNTIME_ENV_FILES
    for path in files:
        value = read_env_file(path, 'GO2W_CONTROL_POLICY')
        if value:
            return value, path
    return None, None


def _close(a, b, tol=0.005):
    return abs(float(a) - float(b)) <= tol


def preflight(maps_dir, base, *, policy=None, slice_now=None, target=None):
    """续建前能不能开始。返回 (ok, reasons, warnings)：reasons 非空就别开始。

    target 是这个新名字：已经存在就直接拒（续建绝不覆盖）。
    """
    reasons, warns = [], []
    if not base:
        return False, ['没有指定基准图'], warns
    if not os.path.isfile(map_path(maps_dir, base)):
        return False, [f'data/maps 里没有基准图 {base}.yaml'], warns
    if not has_posegraph(maps_dir, base):
        return False, [f'基准图 {base} 没有完整的 .posegraph + .data，slam_toolbox 无法从它恢复——'
                       f'这张图不能当续建基准，只能重建'], warns
    if target:
        if os.path.isfile(map_path(maps_dir, target)):
            reasons.append(f'已经有同名地图 {target}：续建必须另存新名字，不覆盖')
        if os.path.isfile(os.path.join(maps_dir, f'{target}.posegraph')):
            reasons.append(f'已经有同名 posegraph {target}：换个名字')

    chain = lineage(maps_dir, base)
    if len(chain) >= MAX_CHAIN:
        warns.append(f'续建链已经有 {len(chain)} 层（{" → ".join(chain)}）：posegraph 会越来越胖，'
                     f'并叠加里程计中断的风险。建议在合适的时机把新图定成新的基准')

    meta = read(maps_dir, base)
    if meta is None:
        # 基准没有 meta.json 是正常的：1116 这些图是在引入 meta 之前存的
        warns.append(f'{base} 没有 meta.json（早于这份数据契约），无法核对站姿和切片'
                     f'——续建后会用当前值写进新图，这一层无法追认')
        return not reasons, reasons, warns

    old_policy = meta.get('policy')
    if policy and old_policy and policy != old_policy:
        reasons.append(f'运控策略变了：基准是 {old_policy}，现在是 {policy}。'
                       f'换策略会改站高和雷达俯仰，续建会错层')
    elif policy and not old_policy:
        warns.append('基准没有记策略，无法核对站姿是否一致')

    old_slice = meta.get('slice')
    if slice_now and old_slice and len(old_slice) == 2:
        if not (_close(old_slice[0], slice_now[0]) and _close(old_slice[1], slice_now[1])):
            reasons.append(f'雷达切片高度变了：基准是 {old_slice[0]:.2f}–{old_slice[1]:.2f} m，'
                           f'现在是 {slice_now[0]:.2f}–{slice_now[1]:.2f} m。切片一变，'
                           f'旧墙线和新墙线不在同一层上')
    return not reasons, reasons, warns


# ---------- routes.json 的地图字段 ----------
def retarget_routes(routes, old, new):
    """把指向 old 的路线改到 new，返回 (新列表, 改了几条)。

    续建保持地图坐标系不变，站点绝对坐标原样有效——这正是选「原位续建」而不是
    「导出对齐图」的最大收益：align_map 那套旋转对齐会改坐标系，站点和初始位姿全部要重标。
    """
    changed = 0
    out = []
    for route in routes:
        if isinstance(route, dict) and route.get('map') == old:
            route = dict(route, map=new)
            changed += 1
        out.append(route)
    return out, changed
