"""录包卡片真实Chrome交互与窄屏布局；仅使用前端mock，不连接机器人。"""
import argparse
from pathlib import Path
from playwright.sync_api import sync_playwright, expect


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url',default='http://127.0.0.1:8097')
    parser.add_argument('--out',default='/tmp/go2w-recording-card-browser')
    args=parser.parse_args()
    assert args.url.startswith(('http://127.0.0.1:','http://localhost:'))
    out=Path(args.out);out.mkdir(parents=True,exist_ok=True)
    errors=[]
    with sync_playwright() as p:
        browser=p.chromium.launch(executable_path='/opt/google/chrome/chrome',headless=True)
        context=browser.new_context(viewport={'width':1050,'height':850},reduced_motion='reduce')
        context.route('https://fonts.googleapis.com/**',lambda r:r.abort())
        context.route('https://fonts.gstatic.com/**',lambda r:r.abort())
        context.add_init_script('Object.defineProperty(window,"isSecureContext",{value:false});')
        page=context.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
        page.goto(args.url+'/?mock&debug',wait_until='networkidle')
        page.wait_for_function('window.go2w?.store.source === "mock"')
        card=page.locator('#recordingControls')
        expect(card).to_be_visible()
        page.screenshot(path=str(out/'console.png'),full_page=True)
        # 将同一真实组件独立放大显示，主页面仍运行，所有交互仍走产品数据源。
        page.add_style_tag(content='#recordingControls{position:fixed;inset:16px;z-index:1000;padding:20px;background:var(--panel);border:1px solid var(--line-2);border-radius:12px;overflow:auto}')
        expect(card).not_to_contain_text('data/bags')
        expect(card).not_to_contain_text('数据录包可用；语音需要受信任的 HTTPS 连接')
        expect(card.locator('#voiceStatus')).to_be_empty()
        expect(card.locator('#voiceSeconds')).not_to_be_visible()
        expect(card.locator('#rbOn')).not_to_be_visible()
        topic=card.locator('#bagSelBtn').bounding_box();heading=card.locator('h4').bounding_box()
        assert topic['x']>heading['x']+heading['width'] and abs(topic['y']-heading['y'])<24
        start=card.get_by_role('button',name='开始持续录制',exact=True)
        current=card.locator('#rbSave')
        expect(current).to_have_text('录制当前数据')
        for button in (start,current):
            expect(button).to_be_enabled()
            rect=button.bounding_box();assert rect['height']>=44
            assert button.evaluate('(e)=>getComputedStyle(e).backgroundColor') not in ('rgba(0, 0, 0, 0)','transparent')
        a,b=start.bounding_box(),current.bounding_box();assert abs(a['y']-b['y'])<2
        card.locator('summary').click()
        expect(card.locator('#rbOn')).to_be_checked()
        expect(card.locator('#voiceSeconds')).to_be_visible()
        card.locator('#voiceSeconds').fill('7');card.locator('#voiceSeconds').press('Tab')
        assert page.evaluate('go2w.actions.rollbackCapture.state.seconds')==7
        assert card.locator('#bagSettings').evaluate('(e)=>e.previousElementSibling.id')=='bagList'
        card.screenshot(path=str(out/'desktop-expanded.png'))
        card.locator('summary').focus();card.locator('summary').press('Enter')
        expect(card.locator('#voiceSeconds')).not_to_be_visible()
        card.locator('#bagSelBtn').click();expect(card.locator('#bagSelList')).to_be_visible()
        card.locator('h4').click();expect(card.locator('#bagSelPop')).not_to_be_visible()
        start.click()
        expect(card.get_by_role('button',name='停止录制',exact=True)).to_be_visible(timeout=5000)
        expect(card.locator('#bagRecordingState')).to_have_text('正在录制中')
        expect(card.locator('#bagRecordingState')).to_be_visible()
        expect(card.locator('#bagSelBtn')).to_be_disabled()
        card.screenshot(path=str(out/'desktop-recording.png'))
        card.get_by_role('button',name='停止录制',exact=True).click()
        expect(start).to_be_visible(timeout=5000)
        expect(card.locator('#bagRecordingState')).not_to_be_visible()
        for width,height in [(1050,850),(375,812),(812,375)]:
            page.set_viewport_size({'width':width,'height':height})
            a,b=start.bounding_box(),current.bounding_box()
            assert abs(a['y']-b['y'])<2,(width,a,b)
            assert card.evaluate('(e)=>e.scrollWidth<=e.clientWidth'),width
            card.screenshot(path=str(out/f'card-{width}-{height}.png'))
            card.locator('summary').click()
            assert card.evaluate('(e)=>e.scrollWidth<=e.clientWidth'),width
            card.screenshot(path=str(out/f'expanded-{width}-{height}.png'))
            card.locator('summary').click()
        page.set_viewport_size({'width':1050,'height':850})
        before=page.evaluate('go2w.store.bags.length')
        current.click();expect(current).to_be_disabled()
        page.wait_for_function('(before)=>go2w.store.bags.length===before+1',arg=before,timeout=15000)
        expect(current).to_be_enabled(timeout=3000)
        assert not errors,errors
        print('PASS 录包卡片布局、更多设置、持续录制状态、当前数据录制及桌面/375px/横屏',flush=True)
        browser.close()


if __name__=='__main__':main()
