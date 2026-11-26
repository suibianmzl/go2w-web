"""On-demand CPU attribution for processes visible to the web runtime."""
import asyncio
from pathlib import Path
import re
import time

from aiohttp import web


class CpuStats:
    WINDOW = .5
    CACHE = 1.

    def __init__(self, proc='/proc'):
        self.proc = Path(proc)
        self.cached = None
        self.cached_at = -float('inf')
        self.pending = None

    def routes(self, app):
        app.router.add_get('/api/system/cpu', self.handle)

    async def handle(self, _request):
        result = await self.read()
        return web.json_response(result, status=200 if result['ok'] else 503,
                                 headers={'Cache-Control':'no-store'})

    async def read(self):
        if self.cached is not None and time.monotonic() - self.cached_at < self.CACHE:
            return self.cached
        if self.pending is None or self.pending.done():
            self.pending = asyncio.create_task(self.collect())
        return await asyncio.shield(self.pending)

    def snapshot(self):
        with (self.proc/'stat').open() as file:
            fields = [int(v) for v in file.readline().split()[1:]]
        if len(fields) < 5:
            raise ValueError('incomplete CPU counters')
        total, idle = sum(fields[:8]), fields[3] + fields[4]  # guest counters already belong to user/nice
        processes, skipped = {}, 0
        for path in self.proc.iterdir():
            if not path.name.isdecimal():
                continue
            try:
                raw = (path/'stat').read_text()
                end = raw.rindex(')')
                name = raw[raw.index('(')+1:end]
                values = raw[end+1:].split()
                key = (int(path.name), int(values[19]))
                processes[key] = (int(values[11]) + int(values[12]), name)
            except (OSError, ValueError, IndexError):
                skipped += 1  # A process may exit between directory enumeration and stat.
        return total, idle, processes, skipped

    def module_name(self, pid, fallback, started):
        try:
            with (self.proc/str(pid)/'cmdline').open('rb') as file:
                args = file.read(65536).decode('utf-8', errors='replace').split('\0')
            raw = (self.proc/str(pid)/'stat').read_text()
            if int(raw[raw.rindex(')')+1:].split()[19]) != started:
                return fallback  # The PID may have been reused after the second snapshot.
            for arg in args:
                match = re.fullmatch(r'__node:=([A-Za-z_][A-Za-z0-9_]*)', arg)
                if match:
                    return match[1]
            if args and args[0]:
                executable = Path(args[0]).name
                if executable.startswith(('python', 'bash')) or executable in ('sh', 'dash'):
                    script = next((Path(arg).name for arg in args[1:]
                                   if arg.endswith(('.py', '.sh')) and not arg.startswith('-')), None)
                    if script:
                        return script
                return executable
        except (OSError, ValueError, IndexError):
            pass
        return fallback

    def compare(self, before, after):
        total = after[0] - before[0]
        if total <= 0:
            return {'ok':False, 'cpu':None, 'items':[], 'message':'CPU采样窗口无有效数据，请稍后重试'}
        items = []
        for key, (ticks, name) in after[2].items():
            previous = before[2].get(key)
            if previous is None or ticks < previous[0]:
                continue
            items.append({'pid':key[0], 'name':name, 'cpu':min(100., (ticks-previous[0])*100./total),
                          '_started':key[1]})
        items.sort(key=lambda row:(-row['cpu'], row['pid']))
        items = items[:5]
        for item in items:
            item['cpu'] = round(item['cpu'], 1)
            item['name'] = self.module_name(item['pid'], item['name'], item.pop('_started'))
        return {'ok':True, 'cpu':round(max(0., min(100., 100.*(1.-(after[1]-before[1])/total))), 1),
                'items':items, 'skipped':before[3]+after[3]}

    async def collect(self):
        started = time.monotonic()
        try:
            before = await asyncio.to_thread(self.snapshot)
            await asyncio.sleep(self.WINDOW)
            after = await asyncio.to_thread(self.snapshot)
            result = await asyncio.to_thread(self.compare, before, after)
        except (OSError, ValueError, IndexError):
            result = {'ok':False, 'cpu':None, 'items':[], 'message':'无法读取CPU进程统计'}
        result.update(sampledAt=time.time(), window=round(time.monotonic()-started, 3), scope='visible_processes')
        self.cached, self.cached_at = result, time.monotonic()
        return result
