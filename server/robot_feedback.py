"""Physical feedback from little-endian Unitree LowState CDR.

Offsets include the four-byte CDR header: q=84, temperature=112,
tau_est=96, reserve[0]=120, stride=48; BMS SOC=1043, IMU rpy=68.
Controller FSM readiness alone is not measured pose feedback.
"""
import math
from pathlib import Path
import struct
import time

import yaml


class RobotFeedback:
    STALE = 1.5
    TOLERANCE = .45  # rad, against the running controller's configured 12-leg targets
    SETTLE = .3
    MAX_GAP = .5

    def __init__(self):
        self.received = 0.
        self.data = None
        self.targets = None
        self.standing_since = None
        self.config_key = None
        self.control_pid = None
        self.generation = 0

    def set_targets(self, stand, lie=None):
        def target(values):
            if not isinstance(values, (list, tuple)) or len(values) != 12:
                raise ValueError('expected twelve leg joints')
            values = tuple(float(v) for v in values)
            if not all(math.isfinite(v) for v in values) or not any(values):
                raise ValueError('invalid leg target')
            return values
        self.targets = (target(stand), target(lie) if lie is not None else None)
        self.generation += 1
        self.standing_since = None

    @property
    def stand(self):
        return self.targets[0] if self.targets else None

    def load_config(self, path):
        try:
            path = Path(path)
            key = (str(path), path.stat().st_mtime_ns)
            if key == self.config_key:
                return
            config = yaml.safe_load(path.read_text())['FSM']
            self.set_targets(config['FixStand']['qs'][-1], config.get('Passive',{}).get('soft_land',{}).get('q'))
            self.config_key = key
        except (OSError, ValueError, TypeError, KeyError, IndexError, yaml.YAMLError):
            self.targets = self.config_key = self.standing_since = None

    def load_running_config(self, groups, proc='/proc'):
        """Read --config from the supervised live process, including independent control releases."""
        try:
            control = groups['control']
            if not control['running'] or type(control['pid']) is not int or control['pid'] <= 0:
                raise ValueError('control not running')
            args = (Path(proc)/str(control['pid'])/'cmdline').read_bytes().decode().rstrip('\0').split('\0')
            if Path(args[0]).name != 'go2w_ctrl':
                raise ValueError('control still starting')
            path = next((arg.split('=',1)[1] if arg.startswith('--config=') else args[i+1]
                         for i,arg in enumerate(args) if arg == '--config' or arg.startswith('--config=')), None)
            if not path or not Path(path).is_absolute():
                raise ValueError('no live control config')
            if self.control_pid != control['pid']:
                self.control_pid = control['pid']
                self.generation += 1
                self.standing_since = None
            self.load_config(path)
        except (OSError, ValueError, KeyError, TypeError, IndexError):
            self.targets = self.config_key = self.standing_since = None

    def posture(self):
        targets = self.targets
        if self.data is None or targets is None:
            return 'unknown'
        q, tilt = self.data['q'], self.data['tilt']
        if not all(math.isfinite(v) for v in (*q, *tilt)):
            return 'unknown'
        if targets[1] is not None and max(abs(a-b) for a,b in zip(q,targets[1])) <= self.TOLERANCE:
            return 'lying'
        if max(map(abs,tilt)) <= .6 and max(abs(a-b) for a,b in zip(q,targets[0])) <= self.TOLERANCE:
            return 'standing'
        return 'unknown'

    def pose_valid(self):
        if self.data is None:
            return False
        q, tilt = self.data['q'], self.data['tilt']
        return (all(math.isfinite(v) for v in (*q,*tilt)) and any(q)
                and math.acos(max(-1.,min(1.,math.cos(tilt[0])*math.cos(tilt[1])))) <= 1.)

    def update(self, raw, now=None):
        if len(raw) < 1044 or raw[:2] != b'\x00\x01':
            return False
        now = time.monotonic() if now is None else now
        temps = [struct.unpack_from('<b',raw,112+48*i)[0] for i in range(16)]
        temps = temps if any(temps) and all(-30 <= v <= 150 for v in temps) else None
        tau = [struct.unpack_from('<f',raw,96+48*i)[0] for i in range(16)]
        self.data = {'battery':raw[1043] if raw[1043] <= 100 else None, 'motorTemp':temps,
                     'motorInfo':{'err':[struct.unpack_from('<I',raw,120+48*i)[0] for i in range(16)],
                                  'tau':[round(v,2) if math.isfinite(v) else None for v in tau]},
                     'q':tuple(struct.unpack_from('<f',raw,84+48*i)[0] for i in range(12)),
                     'tilt':struct.unpack_from('<ff',raw,68)}
        if now-self.received > self.STALE or self.posture() == 'lying' or not self.pose_valid():
            self.generation += 1  # Remember invalidation even when no browser or task observes that frame.
        if now-self.received > self.MAX_GAP or self.posture() != 'standing':
            self.standing_since = None
        if self.posture() == 'standing' and self.standing_since is None:
            self.standing_since = now
        self.received = now
        return True

    def view(self, now=None):
        now = time.monotonic() if now is None else now
        fresh = self.data is not None and self.received > 0 and now-self.received <= self.STALE
        posture = self.posture() if fresh else 'unknown'
        valid = fresh and self.pose_valid()
        return {key:self.data[key] if fresh else None for key in ('battery','motorTemp','motorInfo')} | {
            'robotFeedback':{'fresh':fresh, 'posture':posture,
                             'valid':bool(valid),
                             'age':round(now-self.received,1) if self.received else None,
                             'configured':self.stand is not None}}

    def confirmed(self, after, now=None):
        now = time.monotonic() if now is None else now
        return (self.view(now)['robotFeedback']['posture'] == 'standing'
                and self.received > after and self.standing_since is not None
                and self.received-max(self.standing_since,after) >= self.SETTLE)
