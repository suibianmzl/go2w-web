"""播报（11-25）：用狗本体的喇叭放音频，域 0（Go2W 内网）的 audiohub / vui 服务。只碰这两个服务，不发 Sport 请求。

  audiohub「喊话」：进入 4001 → 把 WAV 分块上传 4003 → 等播完 → 退出 4002。音频存在扩展坞 data/audio，不占狗本体的存储。
  vui：取音量 1004、设音量 1003（0–10）。
接口编号来自社区代码，没有官方文档；11-25 用 scripts/audiotest 在真机上试过，有声音。

网页 / 云端协议
  后端 → 浏览器：{t:'audio', avail, why, items:[{id, name, dur, size, ts}], cur, vol, last, limit}
    cur：正在播的 {clip, name, dur, phase: 'send' 上传中 / 'play' 播放中, elapsed, who}；没在播 = null
    last：最近一次的结果 {clip, name, ok, msg, ts}
  指令 op：audio_play {clip} | audio_stop | audio_volume {v} | audio_rename {clip, name} | audio_delete {clip} | audio_refresh
  上传：POST /api/audio?name=显示名，头 X-Go2w: 1，body 是 16 kHz 单声道 16 bit WAV（网页先转好）
  本机来的连接（127.0.0.1 = 云端网关，公网入口没有鉴权）只能播 / 停，不能改音频和音量；GO2W_AUDIO_LOCAL_EDIT=1 放开
新的播报打断旧的。播报和导航、运控互不影响：播不出来只记日志。
站点播报（v1.1.15）：任务站点的 audio = 音频 id，go2w_web.py 的 route_audio 到站时带 tag 调 play，任务取消 / 失败时 stop_tag。
"""
import asyncio
import base64
import io
import json
import os
import tempfile
import time
import uuid
import wave

A_ENTER, A_EXIT, A_BLOCK = 4001, 4002, 4003
V_SET_VOL, V_GET_VOL = 1003, 1004
RATE = 16000
BLOCK = 16384        # base64 字符数 / 块（约 0.38 s 音频）
GAP = 0.05           # 块间隔 s
TAIL = 1.5           # 播完再等多久才退出喊话
DUR_MIN, DUR_MAX = 0.2, 60.0
CLIPS_MAX = 100
BODY_MAX = int(DUR_MAX * RATE * 2) + 65536


class AudioError(Exception):
    pass


def canonical_wav(raw):
    """校验并重写成最简 44 字节头的 WAV（去掉多余的块，固件未必认）。返回 (bytes, 秒)。"""
    try:
        with wave.open(io.BytesIO(raw), 'rb') as w:
            fmt = (w.getframerate(), w.getnchannels(), w.getsampwidth())
            pcm = w.readframes(w.getnframes())
    except (wave.Error, EOFError, ValueError) as e:
        raise AudioError(f'不是能用的 WAV{"：" + str(e) if str(e) else ""}')
    if fmt != (RATE, 1, 2):
        raise AudioError('需要 16 kHz 单声道 16 bit WAV')
    pcm = pcm[:len(pcm) // 2 * 2]
    dur = len(pcm) / (RATE * 2)
    if not DUR_MIN <= dur <= DUR_MAX:
        raise AudioError(f'时长 {dur:.1f} s：要在 {DUR_MIN}–{DUR_MAX:.0f} s 之间')
    out = io.BytesIO()
    with wave.open(out, 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm)
    return out.getvalue(), dur


class HubLink:
    """audiohub / vui 的请求和回复，挂在 CtrlLink 已有的域 0 节点上（不另开 DDS 参与者）。"""

    def __init__(self, ros):
        from rclpy.qos import QoSProfile, ReliabilityPolicy
        from unitree_api.msg import Request, Response
        self.Request, self.n, self.waiting, self.pubs = Request, 0, {}, {}
        # BEST_EFFORT 订阅能和可靠 / 不可靠的发布者都匹配上
        qos = QoSProfile(depth=50, reliability=ReliabilityPolicy.BEST_EFFORT)
        for s in ('audiohub', 'vui'):
            self.pubs[s] = ros.create_publisher(Request, f'/api/{s}/request', 50)
            ros.create_subscription(Response, f'/api/{s}/response', self.on_reply, qos)

    def online(self, s):
        return self.pubs[s].get_subscription_count() > 0

    def on_reply(self, m):   # ROS 线程
        w = self.waiting.pop(m.header.identity.id, None)
        if w is None:
            return
        loop, cb = w
        try:
            loop.call_soon_threadsafe(cb, m.header.status.code, m.data)
        except RuntimeError:   # 事件循环已经关了
            pass

    def send(self, s, api, param='', cb=None):
        self.n += 1
        r = self.Request()
        r.header.identity.id = rid = int(time.time() * 1e6) * 100 + self.n % 100
        r.header.identity.api_id = api
        r.parameter = param
        if cb is not None:
            while len(self.waiting) > 500:   # 一直没回复的旧请求别攒着
                self.waiting.pop(next(iter(self.waiting)), None)
            self.waiting[rid] = (asyncio.get_running_loop(), cb)
        self.pubs[s].publish(r)
        return rid

    async def call(self, s, api, param='', timeout=2.0):
        """(code, data)；没等到回复 code = None。"""
        fut = asyncio.get_running_loop().create_future()
        rid = self.send(s, api, param, lambda code, data: fut.done() or fut.set_result((code, data)))
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            return None, ''
        finally:
            self.waiting.pop(rid, None)


class Audio:
    def __init__(self, event, data_dir, ros, push, name_error, local_edit, why=''):
        """ros：域 0 节点，没有就传 None（why 写原因）。push(json 文本)：推给所有网页。name_error(名字) → 错误文字或 None。"""
        self.event, self.dir, self.push, self.name_error, self.local_edit = event, os.path.join(data_dir, 'audio'), push, name_error, local_edit
        self.index = os.path.join(self.dir, 'index.json')
        self.items, self.cur, self.last, self.vol = {}, None, None, None
        self.job, self.lock, self.task = None, asyncio.Lock(), None
        self.tag, self.closed = None, False     # tag：正在播的这段是谁发起的（任务对象）；网页手动播 = None
        self.used = lambda clip: []             # 哪些任务站点在用这段音频（go2w_web.py 填），在用的不能删
        self.avail, self.why, self.vol_tries = False, why, 0
        self.link = None
        if ros is not None:
            try:
                self.link = HubLink(ros)
                self.why = '没找到狗的喇叭服务（audiohub）'
            except Exception as e:
                self.why = f'连不上喇叭服务：{e!r}'
                event('WARN', self.why)
        try:
            with open(self.index, encoding='utf-8') as f:
                for x in json.load(f):
                    if os.path.isfile(self.path(x['id'])):
                        self.items[x['id']] = {k: x[k] for k in ('id', 'name', 'dur', 'size', 'ts')}
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, KeyError) as e:
            event('WARN', f'无法读取音频列表 {self.index}：{e!r}')

    # ---------- 状态 ----------
    def path(self, clip):
        return os.path.join(self.dir, f'{clip}.wav')

    def message(self):
        c = self.cur
        cur = c and {**{k: c[k] for k in ('clip', 'name', 'dur', 'phase', 'who')}, 'elapsed': round(time.monotonic() - c['t'], 2)}
        return json.dumps({'t': 'audio', 'avail': self.avail, 'why': '' if self.avail else self.why, 'items': list(self.items.values()),
                           'cur': cur, 'vol': self.vol, 'last': self.last, 'limit': DUR_MAX}, ensure_ascii=False)

    async def notify(self):
        await self.push(self.message())

    async def start(self):
        self.task = asyncio.create_task(self.run())

    async def run(self):
        while True:
            try:
                await self.tick()
            except Exception as e:
                self.event('WARN', f'播报状态：{e!r}')
            await asyncio.sleep(1.0)

    async def tick(self):
        if self.link is None:
            return
        on = self.link.online('audiohub')
        if on != self.avail:
            self.avail, self.vol_tries = on, 0
            await self.notify()
        if on and self.vol is None and self.vol_tries < 6 and self.link.online('vui'):
            self.vol_tries += 1
            await self.read_volume()

    async def read_volume(self):
        code, data = await self.link.call('vui', V_GET_VOL)
        try:
            v = json.loads(data)['volume'] if code == 0 else None
        except (ValueError, TypeError, KeyError):
            v = None
        if isinstance(v, (int, float)) and not isinstance(v, bool) and int(v) != self.vol:
            self.vol = int(v)
            await self.notify()

    async def close(self):
        self.closed = True
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        async with self.lock:
            await self.halt()

    # ---------- 播放 ----------
    async def halt(self):
        j, self.job = self.job, None
        if j is None or j.done():
            return False
        j.cancel()
        await asyncio.gather(j, return_exceptions=True)
        return True

    async def play(self, clip, who, tag=None, valid=None):
        """valid：拿到锁之后再问一次还要不要播（任务可能刚好被取消了）。"""
        x = self.items.get(clip) if isinstance(clip, str) else None
        if x is None:
            return False, '没有这段音频'
        if not self.avail:
            return False, self.why
        async with self.lock:
            if valid is not None and not valid():
                return True, '任务已停，不播'
            if await self.halt():
                await asyncio.sleep(0.2)   # 让狗先退出上一次喊话
            if self.closed or (valid is not None and not valid()):
                return True, '任务已停，不播'
            self.tag = tag
            self.job = asyncio.create_task(self.run_clip(dict(x), who))
        return True, f'开始播报「{x["name"]}」'

    async def stop(self, who):
        async with self.lock:
            if not await self.halt():
                return True, '没有在播报'
        self.event('INFO', f'停止播报（{who}）')
        return True, '已停止播报'

    async def stop_tag(self, tag, why):
        """只停 tag 自己发起的那段；别人（网页手动）播的不动。"""
        async with self.lock:
            if self.tag is tag and await self.halt():
                self.event('INFO', f'停止播报（{why}）')

    async def run_clip(self, x, who):
        self.cur = {'clip': x['id'], 'name': x['name'], 'dur': x['dur'], 'phase': 'send', 'who': who, 't': time.monotonic()}
        entered, ok, msg, bad = False, False, '', []
        try:
            with open(self.path(x['id']), 'rb') as f:
                b64 = base64.b64encode(f.read()).decode()
            await self.notify()
            self.event('INFO', f'播报「{x["name"]}」（{x["dur"]:.1f} s，{who}）')
            entered = True
            code, _ = await self.link.call('audiohub', A_ENTER)
            if code != 0:
                raise AudioError('狗没有回复进入喊话的请求' if code is None else f'狗拒绝进入喊话（code={code}）')
            blocks = [b64[i:i + BLOCK] for i in range(0, len(b64), BLOCK)]
            for i, b in enumerate(blocks, 1):
                self.link.send('audiohub', A_BLOCK, json.dumps({
                    'current_block_size': len(b), 'block_content': b,
                    'current_block_index': i, 'total_block_number': len(blocks)}),
                    lambda code, _data: bad.append(code) if code else None)
                await asyncio.sleep(GAP)
                if bad:
                    raise AudioError(f'狗拒收音频（code={bad[0]}）')
            self.cur.update(phase='play', t=time.monotonic())
            await self.notify()
            await asyncio.sleep(x['dur'] + TAIL)
            if bad:
                raise AudioError(f'狗拒收音频（code={bad[0]}）')
            ok, msg = True, '播完'
        except asyncio.CancelledError:
            msg = '被停止'
            raise
        except (AudioError, OSError) as e:
            msg = str(e)
        except Exception as e:
            msg = repr(e)
        finally:
            if entered:
                try:
                    self.link.send('audiohub', A_EXIT)
                except Exception as e:
                    self.event('WARN', f'退出喊话没发出去：{e!r}')
            self.cur = None
            self.last = {'clip': x['id'], 'name': x['name'], 'ok': ok, 'msg': msg, 'ts': int(time.time() * 1000)}
            if not ok and msg != '被停止':
                self.event('WARN', f'播报「{x["name"]}」失败：{msg}')
            await self.notify()

    async def set_volume(self, v, who):
        if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 10:
            return False, '音量要 0–10'
        if self.link is None or not self.link.online('vui'):
            return False, '没找到狗的音量服务（vui）'
        code, _ = await self.link.call('vui', V_SET_VOL, json.dumps({'volume': v}))
        if code != 0:
            return False, '狗没有回复设音量的请求' if code is None else f'设音量失败（code={code}）'
        await self.read_volume()
        self.event('INFO', f'喇叭音量设为 {v}（{who}）')
        if self.vol != v:
            return False, f'狗回复成功，但读回来的音量是 {self.vol if self.vol is not None else "未知"}'
        return True, f'音量 {v}'

    # ---------- 音频文件 ----------
    def save(self, items):
        tmp = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=self.dir, delete=False) as f:
                tmp = f.name
                json.dump(list(items.values()), f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.index)
        finally:
            if tmp and os.path.exists(tmp):
                os.unlink(tmp)
        self.items = items

    def clean_name(self, name, skip=None):
        if not isinstance(name, str):
            raise AudioError('名字不对')
        name = ' '.join(name.split())
        if not name:
            raise AudioError('名字不能为空')
        err = self.name_error(name)
        if err:
            raise AudioError(err.replace('显示名', '名字'))
        return name, any(x['name'] == name for x in self.items.values() if x['id'] != skip)

    async def add(self, name, raw, who):
        wav, dur = await asyncio.to_thread(canonical_wav, raw)
        if len(self.items) >= CLIPS_MAX:
            raise AudioError(f'最多 {CLIPS_MAX} 段音频，先删几段')
        name, dup = self.clean_name(name)
        if dup:
            raise AudioError(f'已经有一段叫「{name}」，换个名字')
        clip = uuid.uuid4().hex[:12]
        os.makedirs(self.dir, exist_ok=True)
        x = {'id': clip, 'name': name, 'dur': round(dur, 2), 'size': len(wav), 'ts': int(time.time() * 1000)}
        try:
            with open(self.path(clip), 'wb') as f:
                f.write(wav)
            self.save({**self.items, clip: x})
        except OSError:
            if os.path.exists(self.path(clip)):
                os.unlink(self.path(clip))
            raise
        self.event('INFO', f'上传音频「{name}」（{dur:.1f} s，{who}）')
        await self.notify()
        return x

    async def rename(self, clip, name, who):
        x = self.items.get(clip) if isinstance(clip, str) else None
        if x is None:
            return False, '没有这段音频'
        name, dup = self.clean_name(name, clip)
        if dup:
            return False, f'已经有一段叫「{name}」，换个名字'
        self.save({**self.items, clip: {**x, 'name': name}})
        self.event('INFO', f'音频改名：{x["name"]} → {name}（{who}）')
        await self.notify()
        return True, f'已改名为「{name}」'

    async def delete(self, clip, who):
        x = self.items.get(clip) if isinstance(clip, str) else None
        if x is None:
            return False, '没有这段音频'
        if self.cur and self.cur['clip'] == clip:
            return False, '正在播这段，先停止'
        used = self.used(clip)
        if used:
            return False, f'站点在用，先换掉：{"；".join(used[:3])}{" 等" if len(used) > 3 else ""}'
        self.save({k: v for k, v in self.items.items() if k != clip})
        try:
            os.unlink(self.path(clip))
        except OSError as e:
            self.event('WARN', f'音频文件 {clip}.wav 删不掉：{e}')
        self.event('INFO', f'删除音频「{x["name"]}」（{who}）')
        await self.notify()
        return True, f'已删除「{x["name"]}」'

    # ---------- 入口 ----------
    def editable(self, remote):
        return self.local_edit or remote not in ('127.0.0.1', '::1')

    async def handle(self, d, remote):
        op = d['op']
        try:
            if op == 'audio_play':
                return await self.play(d.get('clip'), remote)
            if op == 'audio_stop':
                return await self.stop(remote)
            if op == 'audio_refresh':
                if self.link is not None and self.link.online('vui'):
                    await self.read_volume()
                return True, ''
            if not self.editable(remote):
                return False, '这个入口只能播放和停止'
            if op == 'audio_volume':
                return await self.set_volume(d.get('v'), remote)
            if op == 'audio_rename':
                return await self.rename(d.get('clip'), d.get('name'), remote)
            if op == 'audio_delete':
                return await self.delete(d.get('clip'), remote)
            return False, f'不支持的操作：{op}'
        except AudioError as e:
            return False, str(e)
        except OSError as e:
            return False, f'读写音频失败：{e.strerror or e}'
