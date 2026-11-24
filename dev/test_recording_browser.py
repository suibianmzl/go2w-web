"""真实Chrome虚拟麦克风+本地假ROS；先用隔离GO2W_DATA启动dev/run.sh。"""
import argparse
import json
from pathlib import Path
from urllib.request import urlopen
from playwright.sync_api import sync_playwright, expect


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://127.0.0.1:8097')
    parser.add_argument('--out', default='/tmp/go2w-recording-browser')
    args = parser.parse_args()
    assert args.url.startswith(('http://127.0.0.1:', 'http://localhost:')), '只连接本地假ROS'
    out = Path(args.out); out.mkdir(parents=True, exist_ok=True)
    errors, saves = [], []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path='/opt/google/chrome/chrome', headless=True,
            args=['--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream'])
        context = browser.new_context(permissions=['microphone'], viewport={'width':1440,'height':1000})
        context.route('https://fonts.googleapis.com/**', lambda route: route.abort())
        context.route('https://fonts.gstatic.com/**', lambda route: route.abort())
        page = context.new_page()
        page.on('pageerror', lambda e: errors.append(str(e)))
        def watch(ws):
            ws.on('framesent', lambda data: saves.append(json.loads(data)) if isinstance(data,str) and 'rollback_save' in data else None)
        page.on('websocket', watch)
        page.goto(args.url+'/?debug', wait_until='networkidle')
        page.wait_for_function('window.go2w?.store.link.ok')
        result = page.evaluate("go2w.src.cmd.setMode('localization','test')")
        assert result['ok'], result
        page.wait_for_function('go2w.store.profiles.localization === "run" && !go2w.store.busy && go2w.store.rollback.run', timeout=45000)
        assert page.locator('#voiceSeconds').input_value() == '5'
        page.locator('#srcBadge').click()
        page.keyboard.press('Shift+Enter');page.keyboard.press('Shift+Enter')
        expect(page.locator('#voiceStatus')).to_contain_text('正在录语音', timeout=5000)
        page.wait_for_function("go2w.actions.rollbackCapture.state.message === '数据包与语音备注已保存'", timeout=30000)
        assert len(saves) == 1, saves
        page.screenshot(path=str(out/'console.png'), full_page=True)
        print('PASS 主控制台快捷键、误触保护、真实5秒录音与落盘', flush=True)

        phone = context.new_page()
        phone.set_viewport_size({'width':375,'height':812})
        phone.on('pageerror', lambda e: errors.append(str(e)))
        phone.goto(args.url+'/record', wait_until='networkidle')
        expect(phone.locator('#recordButton')).to_be_enabled(timeout=10000)
        expect(phone.locator('audio')).to_have_count(1)
        audio = phone.locator('audio').first
        audio.evaluate('(a)=>a.play()')
        phone.wait_for_function('document.querySelector("audio").duration > 0')
        actual_duration = audio.evaluate('(a)=>a.duration')
        assert 4.5 <= actual_duration <= 6, actual_duration
        audio.evaluate('(a)=>a.pause()')
        phone.locator('#voiceSeconds').fill('1');phone.locator('#voiceSeconds').press('Tab')
        phone.locator('#voiceSeconds').focus();phone.keyboard.press('Shift+Enter')
        expect(phone.locator('#recordButton')).to_be_enabled()
        phone.locator('#recordButton').click()
        expect(phone.locator('#recordButton')).to_be_disabled()
        expect(phone.locator('#voiceStatus')).to_contain_text('数据包与语音备注已保存', timeout=30000)
        expect(phone.locator('audio')).to_have_count(2, timeout=6000)
        phone.reload(wait_until='networkidle')
        expect(phone.locator('audio')).to_have_count(2)
        expect(phone.locator('#voiceSeconds')).to_have_value('1')
        print('PASS 手机点击一次保存一个包、1秒设置、输入框快捷键隔离、刷新后语音回放', flush=True)

        for width,height in [(375,812),(812,375),(768,1024)]:
            phone.set_viewport_size({'width':width,'height':height})
            phone.emulate_media(reduced_motion='reduce')
            rect=phone.locator('#recordButton').bounding_box()
            assert abs(rect['x']+rect['width']/2-width/2)<2, rect
            assert rect['y']+rect['height']<=height, rect
            assert phone.evaluate('document.documentElement.scrollWidth <= innerWidth'), (width,height)
            phone.locator('#recordResults').last.scroll_into_view_if_needed()
            phone.screenshot(path=str(out/f'phone-{width}-{height}.png'),full_page=True)
        print('PASS 375px竖屏、横屏、平板、减少动画和底部按钮居中', flush=True)

        # 浏览器安全上下文关闭：仍发录包请求，明确说明语音未录制。
        phone.add_init_script('Object.defineProperty(window,"isSecureContext",{value:false});')
        phone.reload(wait_until='networkidle')
        expect(phone.locator('#voiceStatus')).to_contain_text('HTTPS')
        expect(phone.locator('#recordButton')).to_be_enabled(timeout=5000)
        phone.locator('#recordButton').click()
        expect(phone.locator('#voiceStatus')).to_contain_text('数据包已保存；',timeout=30000)
        expect(phone.locator('#voiceStatus')).to_contain_text('HTTPS')
        print('PASS 普通局域网HTTP失败提示与数据录包保留',flush=True)

        recovery=context.new_page()
        recovery.add_init_script('const NativeWebSocket=window.WebSocket; window.WebSocket=class extends NativeWebSocket {constructor(...args){super(...args);(window.__testSockets ||= []).push(this);}};')
        lookups=[]
        recovery.on('request',lambda req:lookups.append(req.url) if '?request_id=' in req.url else None)
        recovery.on('pageerror',lambda e:errors.append(str(e)))
        recovery.goto(args.url+'/record',wait_until='networkidle')
        expect(recovery.locator('#recordButton')).to_be_enabled(timeout=5000)
        recovery.locator('#voiceSeconds').fill('21');recovery.locator('#voiceSeconds').press('Tab')
        expect(recovery.locator('#voiceSeconds')).to_have_value('20')
        recovery.locator('#recordButton').click()
        expect(recovery.locator('#voiceStatus')).to_contain_text('正在录语音',timeout=3000)
        recovery.evaluate('__testSockets[0].close()')
        context.set_offline(True);recovery.wait_for_timeout(1200)
        expect(recovery.locator('#voiceStatus')).to_contain_text('正在录语音')
        context.set_offline(False)
        expect(recovery.locator('#voiceStatus')).to_contain_text('数据包与语音备注已保存',timeout=40000)
        assert lookups, '断线后必须用稳定请求ID查询结果，不能猜包名'
        latest=recovery.locator('audio').first
        latest.evaluate('(a)=>a.play()')
        recovery.wait_for_function('document.querySelector("audio").duration > 0')
        elapsed=latest.evaluate('(a)=>a.duration')
        assert 19.5 <= elapsed <= 20.5, elapsed
        print('PASS 最大20秒真实录音、短暂断网后按请求ID恢复关联',flush=True)
        items=recovery.request.get(args.url+'/api/recordings').json()['items']
        recovery.route(args.url+'/api/recordings',lambda route:route.fulfill(json={'items':items}))
        latest.evaluate('(a)=>{window.oldPlayer=a;}')
        items.insert(0,{'name':'new-result','dur':30,'size':1,'audio':None})
        expect(recovery.get_by_role('heading',name='new-result')).to_be_visible(timeout=6000)
        assert recovery.evaluate('oldPlayer.isConnected && !oldPlayer.paused'), '新增结果不应中断当前语音'
        print('PASS 新增录包结果保留原播放器与播放进度',flush=True)
        assert not errors, errors
        browser.close()
    items=json.loads(urlopen(args.url+'/api/recordings').read())['items']
    assert len(items)==4 and len([i for i in items if i['audio']])==3, items
    assert all(i['dur']==30 for i in items)
    (out/'results.json').write_text(json.dumps({'items':items,'errors':errors},ensure_ascii=False,indent=2))
    print('PASS 后端4包均30秒，3段语音，页面无JS错误',flush=True)


if __name__ == '__main__':
    main()
