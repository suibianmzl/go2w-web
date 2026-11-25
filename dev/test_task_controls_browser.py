"""Navigation task controls in the real UI with the local frontend simulation."""
import argparse
import functools
import json
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from playwright.sync_api import sync_playwright, expect


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--screenshots', default='/tmp/go2w-task-controls-ui')
    args = parser.parse_args()
    shots = Path(args.screenshots)
    shots.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(
        QuietHandler, directory=str(Path(__file__).resolve().parents[1] / 'static')))
    Thread(target=server.serve_forever, daemon=True).start()
    route = {'id': 'limits', 'map': 'lab_20261021', 'name': '点限速测试',
             'opts': {'vmax': .6, 'wmax': 2, 'through': False},
             'points': [{'x': 5.6, 'y': 4.6, 'th': 0, 'name': 'A', 'vmax': .3},
                        {'x': 6.6, 'y': 4.6, 'th': 0, 'name': 'B'},
                        {'x': 7.6, 'y': 4.6, 'th': 0, 'name': 'C'}]}
    resume = {'time': 1795560000, 'status': 'canceled', 'point': 2, 'done': 1,
              'total': 3, 'count': 3, 'finished': [1], 'canSkip': True, 'skipPoint': 3}
    errors = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path='/opt/google/chrome/chrome', headless=True)
            context = browser.new_context(viewport={'width': 1440, 'height': 1080})
            context.route('https://fonts.googleapis.com/**', lambda request: request.abort())
            context.route('https://fonts.gstatic.com/**', lambda request: request.abort())
            page = context.new_page()
            page.on('pageerror', lambda error: errors.append(str(error)))
            url = f'http://127.0.0.1:{server.server_port}/?mock&debug'
            page.goto(url, wait_until='networkidle')
            page.wait_for_function('window.go2w?.store.mapCurrent')
            page.evaluate('(routes) => localStorage.setItem("go2w.mock.routes", JSON.stringify(routes))', [route])
            page.reload(wait_until='networkidle')
            page.locator('#routeSelect').select_option('limits')
            page.evaluate('go2w.store.onRouteEdit()')
            speeds = page.get_by_role('spinbutton', name='第 1 站最大 vx')
            expect(speeds).to_have_count(1)
            expect(speeds).to_have_value('0.3')
            inherited = page.get_by_role('spinbutton', name='第 2 站最大 vx')
            expect(inherited).to_have_value('')
            expect(inherited).to_have_attribute('placeholder', '继承 0.60')
            expect(page.locator('#rtW')).to_have_value('2')
            page.locator('#rtV').fill('0.7')
            expect(inherited).to_have_attribute('placeholder', '继承 0.70')
            inherited.fill('1.3')
            expect(page.locator('#routeSave')).to_be_disabled()
            expect(page.locator('#routeRun')).to_be_disabled()
            expect(page.locator('.route-point-speed-error').nth(1)).to_contain_text('0.1–1.2')
            inherited.fill('0.25')
            assert page.evaluate('go2w.store.routeDraft.points[1].vmax') == .25
            expect(page.locator('#routePoints .route-head').nth(1)).to_contain_text('0.25')
            page.locator('.route-order').nth(1).fill('1')
            page.locator('.route-order').nth(1).press('Tab')
            assert page.evaluate('go2w.store.routeDraft.points.map(p => [p.name,p.vmax ?? null])') == [['B', .25], ['A', .3], ['C', None]]
            # Changing map position must retain the point's speed, name and note.
            page.evaluate('''() => { go2w.store.routeReplace=0;
                go2w.actions.routePoint({x:6.8,y:4.6,th:0,manualHeading:true}); }''')
            assert page.evaluate('go2w.store.routeDraft.points[0].vmax') == .25
            page.locator('#routeConfirm').click()
            page.locator('#routeSave').click()
            page.wait_for_function('go2w.store.routes[0].opts.vmax === .7')
            page.reload(wait_until='networkidle')
            page.locator('#routeSelect').select_option('limits')
            page.evaluate('go2w.store.onRouteEdit()')
            expect(page.get_by_role('spinbutton', name='第 1 站最大 vx')).to_have_value('0.25')
            expect(page.get_by_role('spinbutton', name='第 2 站最大 vx')).to_have_value('0.3')
            page.get_by_role('spinbutton', name='第 1 站最大 vx').fill('')
            page.locator('#routeSave').click()
            page.wait_for_function('!("vmax" in go2w.store.routes[0].points[0])')
            print('PASS 点限速编辑、继承任务上限、非法值阻止保存/出发、排序/换位置保留和保存重载', flush=True)

            page.evaluate('''(resume) => {
                const routes=go2w.store.routes.map(r=>({...r,resume}));
                routes.push({...routes[0],id:'other',name:'另一任务',resume:null});
                localStorage.setItem('go2w.mock.routes',JSON.stringify(routes));
            }''', resume)
            page.reload(wait_until='networkidle')
            page.locator('#routeSelect').select_option('limits')
            page.evaluate('go2w.store.onRouteEdit()')
            terminate = page.get_by_role('button', name='终止任务', exact=True)
            expect(terminate).to_have_count(1)
            terminate.click()
            dialog = page.get_by_role('dialog')
            expect(dialog).to_contain_text('断点')
            expect(dialog).to_contain_text('保留')
            dialog.get_by_role('button', name='取消', exact=True).click()
            assert page.evaluate('!!go2w.store.routes[0].resume')
            failure = page.evaluate('''() => {
                const original=Storage.prototype.setItem;
                Storage.prototype.setItem=()=>{throw new Error('test storage full');};
                try { return go2w.src.cmd.terminateRoute('limits'); }
                finally { Storage.prototype.setItem=original; }
            }''')
            assert failure['ok'] is False
            assert page.evaluate('!!go2w.store.routes[0].resume'), '持久化失败必须保留断点'
            page.screenshot(path=str(shots / 'desktop.png'), full_page=True)
            page.locator('#routePoints').screenshot(path=str(shots / 'desktop-points.png'))
            page.locator('#routeResume').screenshot(path=str(shots / 'desktop-resume.png'))
            page.set_viewport_size({'width': 390, 'height': 844})
            # The right panel may be folded on mobile; open it with the existing toolbar toggle.
            page.evaluate('document.body.classList.remove("right-off")')
            terminate.scroll_into_view_if_needed()
            page.screenshot(path=str(shots / 'mobile.png'), full_page=True)
            page.locator('#routePoints').screenshot(path=str(shots / 'mobile-points.png'))
            page.locator('#routeResume').screenshot(path=str(shots / 'mobile-resume.png'))
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), '移动页面不应出现横向溢出'
            terminate.click()
            # Another client can start an unrelated task while this confirmation is open.
            page.wait_for_function('go2w.store.odomOk')
            assert page.evaluate("go2w.src.cmd.startRoute('other')")['ok']
            page.wait_for_function('go2w.store.task.routeId === "other" && go2w.store.task.status === "active"')
            dialog.get_by_role('button', name='终止任务', exact=True).click()
            page.wait_for_function('!go2w.store.routes.find(r=>r.id==="limits").resume')
            assert page.evaluate('go2w.store.task.routeId') == 'other'
            assert page.evaluate('go2w.store.task.status') == 'active'
            assert page.evaluate("go2w.src.cmd.terminateRoute('other')")['ok'] is False
            assert page.evaluate('go2w.store.routes.length') == 2
            page.reload(wait_until='networkidle')
            assert page.evaluate('go2w.store.routes.length') == 2
            assert not page.evaluate('go2w.store.routes.find(r=>r.id==="limits").resume')
            print('PASS 终止确认可取消、断点持久清除、保存路线保留、其他活动任务不受影响、同任务活动时拒绝', flush=True)
            page.locator('#routeNew').click()
            page.evaluate('''() => { go2w.actions.routePoint({x:5.6,y:4.6,th:0});
                go2w.actions.routePoint({x:6.6,y:4.6,th:0}); }''')
            expect(page.get_by_role('spinbutton', name='第 1 站最大 vx')).to_have_value('')
            expect(page.get_by_role('spinbutton', name='第 1 站最大 vx')).to_have_attribute('placeholder', '继承 0.45')
            assert page.evaluate('go2w.src.cmd.startRoute("limits", {arm:true,vmax:.7,wmax:2})')['ok']
            expect(page.locator('#slV')).to_be_disabled()
            expect(page.locator('#slV')).to_have_attribute('title', '任务执行中，最大 vx 由任务和站点限速共同约束')
            page.evaluate('go2w.src.cmd.cancel()')
            expect(page.locator('#slV')).to_be_enabled()
            simulated = page.evaluate('''async () => {
                const {createStore}=await import('/js/store.js');
                const {createMockSource}=await import('/js/source/mock.js');
                const store=createStore(), src=createMockSource(store,{emit(){}});
                store.routes=[{id:'speed-lifecycle',map:store.mapCurrent,name:'限速行驶',
                    opts:{vmax:.6,wmax:2},points:[
                        {x:5.6,y:4.6,th:0,vmax:.3},
                        {x:6.6,y:4.6,th:0,vmax:.7},
                        {x:7.6,y:4.6,th:0}]}];
                const start=src.cmd.startRoute('speed-lifecycle',{roundTrip:true,loops:1,vmax:.6,wmax:2});
                const initial=store.params.vmax, override=src.cmd.setParam('vmax',1.2), afterOverride=store.params.vmax;
                const legs=[]; let previous='';
                for(let i=0;i<2400;i++){
                    src.tick(.05);
                    const t=store.task, key=`${t.point}/${t.back}`;
                    if(t.status==='active' && key!==previous){legs.push([t.point,t.back,store.params.vmax]);previous=key;}
                    if(t.status==='succeeded' || t.status==='aborted')break;
                }
                const status=store.task.status, restored=store.params.vmax;
                const after=src.cmd.setParam('vmax',.5);
                return {start,initial,override,afterOverride,legs,status,restored,afterAllowed:after?.ok!==false,afterValue:store.params.vmax};
            }''')
            assert simulated['start']['ok']
            assert simulated['initial'] == .3, simulated
            assert simulated['override']['ok'] is False, simulated
            assert simulated['afterOverride'] == .3, simulated
            assert simulated['legs'] == [[2, False, .3], [3, False, .6], [1, True, .3]], simulated
            assert simulated['status'] == 'succeeded', simulated
            assert simulated['restored'] == .6, simulated
            assert simulated['afterAllowed'] and simulated['afterValue'] == .5, simulated
            print('PASS 模拟行驶按任务与两端点限速、循环回起点、执行中拒绝覆盖 vx、结束恢复任务上限', flush=True)
            assert not errors, errors
            browser.close()
    finally:
        server.shutdown()
        server.server_close()


if __name__ == '__main__':
    main()
