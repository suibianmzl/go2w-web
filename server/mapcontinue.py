"""续建起点位姿的三级降级，以及续建本身的前置/收尾判定。

## 为什么起点位姿要单独成一个模块

slam_toolbox 的 `START_AT_GIVEN_POSE` 有个要命的性质：位姿给错**不会报错**。
旧图的格子在它自己的坐标上原样保留，只有新扫进来的墙整体偏一点，收工才发现是双线墙。

更要命的是服务本身的返回值不可信。2026-11-17 核对 Humble 源码
（`slam_toolbox/src/slam_toolbox_common.cpp`）：

    if (!serialization::read(filename, *mapper, *dataset, shared_from_this())) {
      RCLCPP_ERROR(..., "DeserializePoseGraph: Failed to read file: %s.", ...);
      return true;          // ← 读失败也返回 true
    }

而且 `DeserializePoseGraph.srv` 的 response 段是空的（只有 `---` 之后什么都没有），
根本没有 result 字段可以判。所以「服务调成功了」和「图真的加载了」是两件事，
必须靠 `map_overlap_check.py` 事后量。这个模块只负责把起点位姿选对，以及选出
来之后仍然要求自检。

## 三级降级（顺序固定，理由见 docs/地图续建.md）

1. **固定停车点**：默认。机器人停在框内就直接用，可重复、零耗时。
2. **自动定位**：复用 `globloc.py`，沿用它的门限（分数 ≥ 0.75、次优/最优 ≤ 0.80）。
3. **手动拖**：前两级不可信才走，并且明确告警——这是唯一可能悄悄偏掉的一级。

**硬约束：必须从基准图覆盖范围内出发。** 在图外启动时任何起点位姿都是猜的，
所以 settle() 对三级结果一视同仁地检查「在不在图里」。
"""
import math

# 和 globloc.py 一致的门限：那里已经在 11-10 用两段实录 bag 评估过
GLOBLOC_MIN_SCORE = 0.75
GLOBLOC_MAX_RATIO = 0.80
# 停车点的容差：机器人停在框内就认。超出这个范围说明没停到位，退下一级
PARKING_TOL_XY, PARKING_TOL_YAW = 0.30, math.radians(10)

SOURCE_LEVEL = {'parking': 1, 'globloc': 2, 'manual': 3}
SOURCE_NOTE = {
    'parking': '固定停车点',
    'globloc': '自动定位',
    'manual': '手动给的位姿',
}


def normalize_yaw(a):
    """归一到 (-π, π]。slam_toolbox 里 Pose2D.theta 就是这么用的。"""
    return (float(a) + math.pi) % (2 * math.pi) - math.pi


def yaw_diff(a, b):
    """两个航向的最短夹角（弧度）。"""
    return normalize_yaw(float(a) - float(b))


def choose_parking(parking, robot_pose=None):
    """第 1 级：停车点。命中返回 (pose, '')，否则 (None, 为什么不算)。

    **为什么 robot_pose 只用于「有定位时的核对」，不能当必要条件**：
    续建刚启动时 slam_toolbox 是新图，`map→base_footprint` 恒等于原点附近——
    不管机器人实际站在哪儿。所以「机器人是不是停在框内」在这一刻量不出来，
    只能由人保证（把狗停进画好的框）。真正的保险是随后的自检：
    假设的位姿不对，贴合率会掉下来，报「环境和图对不上」。

    robot_pose 给的时候（例如已经用基准图定位过）顺带核对一下，对不上就提示，
    但仍然采用停车点——因为定位可能是在别的图上做的。
    """
    if not parking or not parking.get('pose'):
        return None, '没有配置续建停车点'
    px, py, pyaw = (float(v) for v in parking['pose'][:3])
    return [px, py, normalize_yaw(pyaw)], ''


def parking_note(parking, robot_pose=None):
    """停车点级别的说明：明确写出「这是假设，不是量出来的」。"""
    if not parking or not parking.get('pose'):
        return ''
    px, py, pyaw = (float(v) for v in parking['pose'][:3])
    note = (f'按停车点假设起点 {px:.2f}, {py:.2f}, {math.degrees(pyaw):.0f}°'
            f'（续建刚开始时 map 系是新的，站没站在框内量不出来——由自检兜底）')
    if robot_pose is not None:
        off = math.hypot(robot_pose[0] - px, robot_pose[1] - py)
        if off > PARKING_TOL_XY:
            note += f'；但当前定位显示离停车点 {off:.2f} m，如果不是停在那儿，改用自动定位'
    return note


def accept_globloc(result):
    """第 2 级：自动定位结果能不能用。和 globloc 自己的 ok 同口径，这里重算一遍便于单测。"""
    if not isinstance(result, dict):
        return None, '自动定位没有结果'
    if 'error' in result:
        return None, str(result['error'])
    try:
        score, ratio = float(result['score']), float(result['ratio'])
        pose = [float(result['x']), float(result['y']), normalize_yaw(result['th'])]
    except (KeyError, TypeError, ValueError):
        return None, '自动定位结果字段不全'
    if score < GLOBLOC_MIN_SCORE:
        return None, f'匹配分数 {score:.2f} 低于 {GLOBLOC_MIN_SCORE:.2f}'
    if ratio > GLOBLOC_MAX_RATIO:
        return None, f'次优/最优 {ratio:.2f} 高于 {GLOBLOC_MAX_RATIO:.2f}，有歧义，分不清在哪'
    return pose, ''


def settle(parking, robot_pose, globloc, manual=None, force=None):
    """按三级顺序定起点位姿，返回 dict：

        source  实际用了哪一级（parking / globloc / manual）
        pose    [x, y, yaw]
        note    给人看的一句
        tried   每一级为什么没用（诊断用）
        warn    需要显式告警的话（手动级一定有）

    force='manual' 时跳过前两级——人明确说了「我说的才算」。
    """
    tried, warn = [], ''
    if force not in (None, 'manual'):
        raise ValueError('force 只能是 manual')
    if force is None:
        pose, why = choose_parking(parking, robot_pose)
        if pose is not None:
            return {'source': 'parking', 'pose': pose,
                    'note': parking_note(parking, robot_pose),
                    'tried': tried, 'warn': ''}
        tried.append(('parking', why))
        pose, why = accept_globloc(globloc)
        if pose is not None:
            return {'source': 'globloc', 'pose': pose,
                    'note': f"自动定位 {pose[0]:.2f}, {pose[1]:.2f}, {math.degrees(pose[2]):.0f}°"
                            f"（分数 {float(globloc['score']):.2f}）",
                    'tried': tried, 'warn': ''}
        tried.append(('globloc', why))
    if not (isinstance(manual, (list, tuple)) and len(manual) == 3):
        return {'source': None, 'pose': None,
                'note': '前两级都不可信，而且没有手动给位姿',
                'tried': tried, 'warn': ''}
    pose = [float(manual[0]), float(manual[1]), normalize_yaw(manual[2])]
    return {'source': 'manual', 'pose': pose,
            'note': f'手动位姿 {pose[0]:.2f}, {pose[1]:.2f}, {math.degrees(pose[2]):.0f}°',
            'tried': tried,
            'warn': '起点位姿是手动给的：这一级没有自动校验，开走后务必看自检结果'}


def in_coverage(base, pose):
    """起点在不在基准图覆盖范围内。base 是 mapimage-like（有 inside 方法）或 (ox, oy, w, h, res)。

    图外起步时任何位姿都是猜的，这是硬约束，不给降级。
    """
    x, y = float(pose[0]), float(pose[1])
    if hasattr(base, 'inside'):
        return bool(base.inside([x], [y])[0])
    ox, oy, w, h, res = base
    return ox <= x < ox + w * res and oy <= y < oy + h * res


def check_start(base, pose):
    """settle() 之后、真正下发前必须过的一关。返回 (ok, reason)。"""
    if not pose:
        return False, '还没有起点位姿'
    if not in_coverage(base, pose):
        return False, (f'起点 ({pose[0]:.2f}, {pose[1]:.2f}) 不在基准图覆盖范围内：'
                       f'必须先开进基准图覆盖的地方再续建，图外起步任何位姿都是猜的')
    return True, ''


# 自检必须停在起点做：它拿「现在扫到的墙」和「基准图在起点位姿下预测的墙」比。
# 机器人已经开走的话，用来变换 scan 的起点位姿就不成立了，量出来的偏差是假的。
CHECK_DRIFT_XY = 0.50    # m
CHECK_DRIFT_YAW = math.radians(15)


def check_drift(start, now):
    """自检时机器人还在不在起点。返回 (ok, reason)。

    留着这条是为了把「你开走了」和「图对不上」分开：不分开的话，开走之后自检会以
    「环境和图对不上」报错，让人去怀疑地图，白折腾一轮。
    """
    if now is None:
        # 拿不到当前位姿时不能拦：趴着 / 没有 TF 也允许量一次，由贴合率自己说话
        return True, ''
    dx, dy = now[0] - start[0], now[1] - start[1]
    off, dth = math.hypot(dx, dy), abs(yaw_diff(now[2], start[2]))
    if off > CHECK_DRIFT_XY or dth > CHECK_DRIFT_YAW:
        return False, (f'机器人已经离开起点 {off:.2f} m / {math.degrees(dth):.0f}°'
                       f'（容差 {CHECK_DRIFT_XY:.1f} m / {math.degrees(CHECK_DRIFT_YAW):.0f}°）：'
                       f'自检要停在起点做，开走之后量出来的偏差不是起点位姿的问题')
    return True, ''
