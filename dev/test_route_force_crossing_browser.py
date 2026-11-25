"""Chrome路线编辑回归；只访问本地静态页面的mock模式。"""
import argparse
from pathlib import Path
from playwright.sync_api import sync_playwright, expect


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://127.0.0.1:8847')
    parser.add_argument('--out', default='/tmp/go2w-force-crossing-browser')
    args = parser.parse_args()
    assert args.url.startswith(('http://127.0.0.1:', 'http://localhost:'))
    out = Path(args.out); out.mkdir(parents=True, exist_ok=True)
    errors = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path='/opt/google/chrome/chrome', headless=True)
        page = browser.new_page(viewport={'width':1500, 'height':1000})
        page.route('https://fonts.googleapis.com/**', lambda route: route.abort())
        page.route('https://fonts.gstatic.com/**', lambda route: route.abort())
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(args.url+'/?mock&debug', wait_until='networkidle')
        page.wait_for_function('window.go2w?.store.mapCurrent')
        page.locator('#taskTabs [data-t=nav]').click()
        page.locator('#routeNew').click()
        page.evaluate('''() => {
            const s=go2w.store;
            s.routeDraft={id:null,map:s.mapCurrent,confirmed:false,points:
                Array.from({length:4},(_,i)=>({x:i+1,y:0,th:0,name:'ABCD'[i],manualHeading:true}))};
            document.querySelector('#routeName').dispatchEvent(new Event('input'));
        }''')
        boxes = page.locator('.route-point-crossing input[type=checkbox]')
        expect(boxes).to_have_count(4)
        boxes.nth(1).check()
        expect(page.locator('#routeConfirm')).to_be_disabled()
        expect(page.locator('#routeCrossingHint')).to_contain_text('至少选择两个点')
        boxes.nth(3).check()
        expect(page.locator('#routeConfirm')).to_be_enabled()
        expect(page.locator('#routeCrossingHint')).to_contain_text('第 2 站')
        expect(page.locator('#routeCrossingHint')).to_contain_text('第 4 站')
        page.locator('#routeConfirm').click()
        page.locator('#routeName').fill('强制跨越测试')
        page.locator('#routeSave').click()
        page.wait_for_function("go2w.store.routes.some(r=>r.name==='强制跨越测试')")
        saved = page.evaluate("go2w.store.routes.find(r=>r.name==='强制跨越测试')")
        assert [i for i, pt in enumerate(saved['points']) if pt.get('forceCrossing')] == [1, 3]
        page.reload(wait_until='networkidle')
        page.wait_for_function('window.go2w?.store.mapCurrent')
        page.locator('#taskTabs [data-t=nav]').click()
        page.locator('#routeSelect').select_option(saved['id'])
        expect(boxes.nth(1)).to_be_checked(); expect(boxes.nth(3)).to_be_checked()
        boxes.nth(1).uncheck()
        expect(page.locator('#routeSave')).to_be_disabled()
        expect(page.locator('#routeRun')).to_be_disabled()
        boxes.nth(3).uncheck()
        expect(page.locator('#routeSave')).to_be_enabled()
        boxes.nth(0).check(); boxes.nth(3).check()
        order = page.locator('.route-order').nth(3)
        order.fill('2'); order.press('Tab')
        assert page.evaluate('go2w.store.routeDraft.points.map(p=>p.name)') == ['A','D','B','C']
        expect(boxes.nth(0)).to_be_checked(); expect(boxes.nth(1)).to_be_checked()
        expect(page.locator('#routeCrossingHint')).to_contain_text('第 2 站')
        page.locator('#routePoints li').nth(1).get_by_role('button', name='删除第 2 点', exact=True).click()
        expect(page.locator('#routeConfirm')).to_be_disabled()
        expect(page.locator('#routeCrossingHint')).to_contain_text('至少选择两个点')
        boxes.nth(1).check()
        expect(page.locator('#routeConfirm')).to_be_enabled()
        page.locator('#routePoints').evaluate('el=>el.style.maxHeight="none"')
        page.locator('#routeCrossingHint').scroll_into_view_if_needed()
        page.screenshot(path=str(out/'desktop.png'), full_page=True)
        page.set_viewport_size({'width':390,'height':844})
        page.locator('#routeCrossingHint').scroll_into_view_if_needed()
        page.screenshot(path=str(out/'mobile.png'), full_page=True)
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), '移动端不能横向溢出'
        assert not errors, errors
        browser.close()
    print('PASS Chrome: 单选拦截、区间提示、保存重载、重排和删除保留标记、移动端无横向溢出')


if __name__ == '__main__':
    main()
