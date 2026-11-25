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
        context=browser.new_context(viewport={'width':1440,'height':1000},reduced_motion='reduce')
        context.route('https://fonts.googleapis.com/**',lambda r:r.abort())
        context.route('https://fonts.gstatic.com/**',lambda r:r.abort())
        context.add_init_script('Object.defineProperty(window,"isSecureContext",{value:false});')
        page=context.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
        page.goto(args.url+'/?mock&debug',wait_until='networkidle')
        page.wait_for_function('window.go2w?.store.source === "mock"')
        card=page.locator('#recordingControls')
        expect(card).to_be_visible()
        page.screenshot(path=str(out/'console.png'),full_page=True)
        expect(card).not_to_contain_text('data/bags')
        expect(card).not_to_contain_text('数据录包可用；语音需要受信任的 HTTPS 连接')
        expect(card.locator('#voiceStatus')).to_be_empty()
        expect(card.locator('#voiceSeconds')).not_to_be_visible()
        expect(card.locator('#rbOn')).not_to_be_visible()
        history=card.locator('#bagHistory')
        settings=card.locator('#bagSettings > summary')
        expect(card.locator('#bagList')).not_to_be_visible()
        expect(card.locator('#bagCount')).to_have_text(str(page.evaluate('go2w.store.bags.length')))
        history.locator('summary').click()
        expect(card.locator('#bagList')).to_be_visible()
        expect(card.locator('#bagList .bag-up')).to_have_count(page.evaluate('go2w.store.bags.length'))
        history.locator('summary').focus();history.locator('summary').press('Enter')
        expect(card.locator('#bagList')).not_to_be_visible()
        topic=card.locator('#bagSelBtn').bounding_box();heading=card.locator('h4').bounding_box()
        assert topic['x']>heading['x']+heading['width'] and abs(topic['y']-heading['y'])<24
        start=card.get_by_role('button',name='开始持续录制',exact=True)
        current=card.locator('#rbSave')
        expect(start).to_have_attribute('aria-keyshortcuts','Shift+R')
        assert 'Shift+R' in start.get_attribute('title')
        assert 'Shift+Enter' in current.get_attribute('title')
        expect(current).to_have_text('录制当前数据')
        for button in (start,current):
            expect(button).to_be_enabled()
            rect=button.bounding_box();assert rect['height']>=44
            assert button.evaluate('(e)=>getComputedStyle(e).backgroundColor') not in ('rgba(0, 0, 0, 0)','transparent')
        a,b=start.bounding_box(),current.bounding_box();assert abs(a['y']-b['y'])<2
        settings.click()
        expect(card.locator('#rbOn')).to_be_checked()
        expect(card.locator('#voiceSeconds')).to_be_visible()
        card.locator('#voiceSeconds').fill('7');card.locator('#voiceSeconds').press('Tab')
        assert page.evaluate('go2w.actions.rollbackCapture.state.seconds')==7
        assert card.locator('#bagSettings').evaluate('(e)=>e.previousElementSibling.id')=='bagHistory'
        card.screenshot(path=str(out/'desktop-expanded.png'))
        settings.focus();settings.press('Enter')
        expect(card.locator('#voiceSeconds')).not_to_be_visible()
        card.locator('#bagSelBtn').click();expect(card.locator('#bagSelList')).to_be_visible()
        card.locator('h4').click();expect(card.locator('#bagSelPop')).not_to_be_visible()
        card.locator('h4').click();page.keyboard.press('Shift+R')
        expect(card.get_by_role('button',name='停止录制',exact=True)).to_be_visible(timeout=5000)
        assert 'Shift+R' in card.locator('#bagBtn').get_attribute('title')
        assert '停止持续录制' in card.locator('#bagBtn').get_attribute('title')
        expect(card.locator('#bagRecordingState')).to_have_text('正在录制中')
        expect(card.locator('#bagRecordingState')).to_be_visible()
        expect(card.locator('#bagSelBtn')).to_be_disabled()
        card.screenshot(path=str(out/'desktop-recording.png'))
        page.set_viewport_size({'width':375,'height':812})
        stop=card.get_by_role('button',name='停止录制',exact=True)
        stop.scroll_into_view_if_needed()
        expect(card.locator('#bagRecordingState')).to_be_visible()
        assert card.evaluate('(e)=>e.scrollWidth<=e.clientWidth')
        header=card.locator('.bag-header').bounding_box();topic=card.locator('#bagSelBtn').bounding_box()
        assert topic['x']+topic['width']<=header['x']+header['width']+1
        assert 44<=stop.bounding_box()['height']<=60
        card.screenshot(path=str(out/'mobile-recording.png'))
        page.set_viewport_size({'width':1440,'height':1000})
        card.locator('h4').click();page.keyboard.press('Shift+R')
        expect(start).to_be_visible(timeout=5000)
        expect(card.locator('#bagRecordingState')).not_to_be_visible()
        for width,height in [(1440,1000),(1050,850),(375,812),(812,375)]:
            page.set_viewport_size({'width':width,'height':height})
            start.scroll_into_view_if_needed()
            a,b=start.bounding_box(),current.bounding_box()
            assert abs(a['y']-b['y'])<2,(width,a,b)
            assert 44<=a['height']<=60 and 44<=b['height']<=60,(width,a,b)
            assert card.evaluate('(e)=>e.scrollWidth<=e.clientWidth'),width
            header=card.locator('.bag-header').bounding_box()
            topic=card.locator('#bagSelBtn').bounding_box()
            assert topic['x']>=header['x'] and topic['x']+topic['width']<=header['x']+header['width']+1,(width,topic,header)
            card.screenshot(path=str(out/f'card-{width}-{height}.png'))
            history.locator('summary').click()
            expect(card.locator('#bagList')).to_be_visible()
            assert card.evaluate('(e)=>e.scrollWidth<=e.clientWidth'),width
            history.locator('summary').click()
            settings.click()
            assert card.evaluate('(e)=>e.scrollWidth<=e.clientWidth'),width
            card.screenshot(path=str(out/f'expanded-{width}-{height}.png'))
            settings.click()
        page.set_viewport_size({'width':1440,'height':1000})
        before=page.evaluate('go2w.store.bags.length')
        history.locator('summary').click()
        card.locator('h4').click();page.keyboard.press('Shift+Enter');expect(current).to_be_disabled()
        page.wait_for_function('(before)=>go2w.store.bags.length===before+1',arg=before,timeout=15000)
        expect(current).to_be_enabled(timeout=3000)
        expect(card.locator('#bagCount')).to_have_text(str(before+1))
        expect(card.locator('#bagList')).to_be_visible()
        page.reload(wait_until='networkidle')
        expect(card.locator('#bagList')).not_to_be_visible()
        phone=context.new_page();phone.on('pageerror',lambda e:errors.append(str(e)))
        phone.goto(args.url+'/record.html?mock',wait_until='networkidle')
        expect(phone.locator('#recordButton')).to_be_enabled(timeout=5000)
        assert not errors,errors
        print('PASS 持续录制/当前数据快捷键与悬停提示、列表折叠、桌面/窄屏交互及手机录制页加载',flush=True)
        browser.close()


if __name__=='__main__':main()
