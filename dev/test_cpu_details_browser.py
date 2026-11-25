"""Exercise the CPU popup in Chrome without ROS or robot connections."""
import functools
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

from playwright.sync_api import expect, sync_playwright


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass


def main():
    server = ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(
        QuietHandler, directory=str(Path(__file__).resolve().parents[1]/'static')))
    Thread(target=server.serve_forever, daemon=True).start()
    errors, calls = [], []
    state = {'error': False, 'name': '<img src=x onerror=alert(1)>'}
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path='/opt/google/chrome/chrome', headless=True)
            context = browser.new_context(viewport={'width':1440, 'height':1000})
            context.route('https://fonts.googleapis.com/**', lambda r:r.abort())
            context.route('https://fonts.gstatic.com/**', lambda r:r.abort())
            page = context.new_page()
            page.on('pageerror', lambda error:errors.append(str(error)))
            url = f'http://127.0.0.1:{server.server_port}/'
            page.goto(url+'?mock&debug', wait_until='networkidle')
            button = page.locator('#chCpu')
            assert button.evaluate('(el) => el.tagName') == 'BUTTON', 'CPU chip must be keyboard accessible'
            button.click()
            dialog = page.get_by_role('dialog', name='CPU 占用前 5')
            expect(dialog).to_be_visible()
            expect(dialog).to_contain_text('模拟数据')
            expect(dialog.locator('tbody tr')).to_have_count(5)
            page.keyboard.press('Escape')
            expect(dialog).to_have_count(0)
            expect(button).to_be_focused()

            def api(route):
                calls.append(route.request.method)
                if state['error']:
                    route.fulfill(status=503, json={'ok':False, 'message':'采样失败，请稍后重试'})
                else:
                    route.fulfill(json={'ok':True, 'cpu':95., 'sampledAt':1795560000,
                        'scope':'visible_processes', 'items':[
                            {'pid':100+i, 'name':state['name'] if i==0 else f'worker_{i}', 'cpu':34.5-i*6}
                            for i in range(5)]})

            page.route('**/api/system/cpu', api)
            page.goto(url+'?debug', wait_until='networkidle')
            button.click()
            expect(dialog.locator('tbody tr')).to_have_count(5)
            expect(dialog.locator('tbody tr').first).to_contain_text(state['name'])
            expect(dialog.locator('tbody tr').first).to_contain_text('34.5%')
            expect(dialog.locator('tbody tr').first).to_contain_text('100')
            expect(dialog.locator('img')).to_have_count(0)
            expect(dialog).to_contain_text('容器外')
            expect(dialog.locator('.cpu-mode')).to_be_hidden()
            page.screenshot(path='/tmp/go2w-cpu-desktop.png')
            state['name'] = 'controller_server'
            expect(dialog.locator('tbody tr').first).to_contain_text(state['name'], timeout=6000)
            state['error'] = True
            expect(dialog.locator('[role=status]')).to_contain_text('采样失败', timeout=6000)
            expect(dialog.locator('tbody tr')).to_have_count(0)
            state['error'] = False
            expect(dialog.locator('tbody tr')).to_have_count(5, timeout=6000)
            dialog.get_by_role('button', name='关闭').click()
            count = len(calls)
            page.wait_for_timeout(2300)
            assert len(calls) == count, 'Closed popup must stop polling'
            assert set(calls) == {'GET'}

            # Closing aborts an outstanding request; a late response must not resurrect the popup.
            page.evaluate('''() => {
                window.cpuAborted = false;
                go2w.src.cpuStats = signal => new Promise(resolve => {
                    window.cpuLate = resolve;
                    signal.addEventListener('abort', () => window.cpuAborted = true);
                });
            }''')
            button.click()
            expect(dialog.locator('[role=status]')).to_contain_text('采样')
            page.keyboard.press('Escape')
            assert page.evaluate('window.cpuAborted')
            page.evaluate('cpuLate({ok:true,cpu:1,items:[]})')
            expect(dialog).to_have_count(0)
            expect(button).to_be_focused()

            page.evaluate('''() => {
                go2w.src.cpuStats = signal => new Promise((resolve, reject) =>
                    signal.addEventListener('abort', () => reject(new DOMException('timeout', 'AbortError'))));
            }''')
            button.click()
            expect(dialog.locator('[role=status]')).to_contain_text('读取超时', timeout=10000)
            dialog.get_by_role('button', name='关闭').click()
            page.evaluate('go2w.src.cpuStats = async () => ({ok:true,items:[],cpu:2})')
            button.click()
            expect(dialog.locator('[role=status]')).to_contain_text('暂无可统计')
            expect(dialog.locator('table')).to_be_hidden()
            dialog.get_by_role('button', name='关闭').click()

            # Mobile layout and modal focus; non-emergency keys must not operate the map beneath it.
            page.goto(url+'?mock&debug', wait_until='networkidle')
            page.set_viewport_size({'width':390, 'height':844})
            button.click()
            expect(dialog.locator('tbody tr')).to_have_count(5)
            page.keyboard.press('t')
            assert page.evaluate('go2w.mapView.tool') == 'pan'
            page.evaluate('() => { window.cpuStopCount=0;go2w.actions.estop=()=>window.cpuStopCount++; }')
            assert page.evaluate('window.cpuStopCount') == 0
            page.keyboard.press('Space')
            assert page.evaluate('window.cpuStopCount') == 1
            expect(dialog).to_be_visible()
            page.keyboard.press('Tab')
            assert dialog.evaluate('(el) => el.contains(document.activeElement)')
            box = dialog.bounding_box()
            assert box['x'] >= 0 and box['x']+box['width'] <= 391
            assert dialog.evaluate('(el) => el.scrollWidth <= el.clientWidth')
            page.screenshot(path='/tmp/go2w-cpu-mobile.png')
            dialog.get_by_role('button', name='关闭').click()
            assert not errors, errors
            browser.close()
            print('PASS CPU点击、真实HTTP适配、前5名、占比/PID、模拟标记、刷新/失败恢复、空数据/超时、关闭/取消、快捷键隔离/急停与移动布局')
    finally:
        server.shutdown()
        server.server_close()


if __name__ == '__main__':
    main()
