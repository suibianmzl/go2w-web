#!/usr/bin/env python3
"""真实 Chrome + 拦截文件 API 验证控制点/曲线/迟到响应；不写后端、不建 WS。"""
import argparse
import copy
import json
import struct
import zlib
from pathlib import Path
from urllib.parse import unquote, urlparse
from urllib.request import urlopen

from playwright.sync_api import expect, sync_playwright


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://127.0.0.1:8838')
    parser.add_argument('--out', default='/home/tink/program/go2w/data/development/web-taught-refine-20261119')
    parser.add_argument('--live-backend', action='store_true', help='另在明确标记 simulated 的假ROS后端预览并从趴姿准备；不开始采集或遥控')
    args = parser.parse_args()
    assert args.url.startswith(('http://127.0.0.1:', 'http://localhost:'))
    out = Path(args.out); out.mkdir(parents=True, exist_ok=True)
    report = {'steps': [], 'errors': [], 'websockets': [], 'requests': []}
    controls = [{'x': -2., 'y': 1., 'continuous_id': 0}, {'x': 0., 'y': 2., 'continuous_id': 0}, {'x': 2., 'y': 1., 'continuous_id': 0}]
    meta = json.dumps({'w': 200, 'h': 200, 'res': .05, 'ox': -5, 'oy': -5, 'oyaw': 0}).encode()
    packet = b'\x01' + struct.pack('<I', len(meta)) + meta + zlib.compress(bytes(40000))

    def preview(name, points, revision=4):
        a, b, c = points[0], points[1], points[-1]
        dense = [a, {'x': (a['x']+b['x'])/2, 'y': (a['y']+b['y'])/2+.25, 'continuous_id': a['continuous_id']},
                 b, {'x': (b['x']+c['x'])/2, 'y': (b['y']+c['y'])/2+.25, 'continuous_id': c['continuous_id']}, c]
        collision = any(p['y'] < 0 for p in points)
        gaps = len({p['continuous_id'] for p in points}) > 1
        valid = not collision and not gaps
        return {'ok': valid, 'msg': '仍有断口，请明确连接后预览' if gaps else '完整足迹碰撞，请调整控制点' if collision else '预览已通过',
                'data': {'map': name, 'revision': revision, 'map_token': name+'-token', 'control_points': points,
                         'preview_points': dense, 'processing': {'input_count': len(points), 'output_count': len(dense),
                         'interpolation': 'cubic_spline', 'max_offset': 0, 'iterations': 0, 'full_sweep_checked': valid},
                         'executable': valid, 'report': {'ok': valid}}}

    def document(name, points=None, revision=4):
        points = copy.deepcopy(points or controls)
        p = preview(name, points, revision)['data']
        return {'map': name, 'revision': revision, 'map_token': name+'-token', 'control_points': points,
                'points': p['preview_points'], 'processing': p['processing'], 'state': 'ready', 'executable': True, 'length': 4.8}

    docs = {name: document(name) for name in ['curve', 'other']}
    gap_controls = copy.deepcopy(controls); gap_controls[-1]['continuous_id'] = 9
    docs['gap'] = document('gap', gap_controls)
    pending = []; mode = {'hold': False, 'unavailable': False}; saved = []

    def fixture(route):
        path = unquote(urlparse(route.request.url).path)
        if path == '/api/taught/maps':
            return route.fulfill(json={'maps': [{'name': name} for name in docs]})
        name = path.split('/')[4]
        if path.endswith('/grid'):
            return route.fulfill(body=packet, content_type='application/octet-stream')
        if path.endswith('/preview'):
            body = route.request.post_data_json
            report['requests'].append({'map': name, 'kind': 'preview', 'body': body})
            assert body['expected_revision'] == docs[name]['revision']
            assert body['map_token'] == name+'-token'
            if mode['unavailable']:
                return route.fulfill(status=503, json={'ok': False, 'msg': '预览服务暂不可用'})
            result = preview(name, body['points'], docs[name]['revision'])
            if mode['hold']:
                mode['hold'] = False; pending.append((route, result)); return
            return route.fulfill(json=result)
        if route.request.method == 'POST':
            body = route.request.post_data_json; saved.append(copy.deepcopy(body))
            docs[name] = document(name, body['points'], docs[name]['revision']+1)
            return route.fulfill(json={'ok': True, 'msg': '已保存', 'data': docs[name]})
        return route.fulfill(json=docs[name])

    def passed(name, **values):
        report['steps'].append({'name': name, **values}); print('PASS', name, flush=True)

    def change(page, index, axis, value):
        page.locator('#editorIndex').fill(str(index)); page.locator('#editorIndex').press('Tab')
        page.locator('#editor'+axis).fill(str(value)); page.locator('#editorApply').click()

    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path='/opt/google/chrome/chrome', headless=True)
        page = browser.new_page(viewport={'width': 1440, 'height': 1000})
        page.on('pageerror', lambda e: report['errors'].append(str(e)))
        page.on('websocket', lambda ws: report['websockets'].append(ws.url))
        page.route('**/api/taught/**', fixture)
        # 服务忽略取消时仍应有版本保护；记录实际 canvas 路线，验证画的是后端稠密曲线。
        page.add_init_script("""window.__strokes=[];
          const originalFetch=window.fetch;window.fetch=(url,opts={})=>originalFetch(url,String(url).endsWith('/preview')?{...opts,signal:undefined}:opts);
          const C=CanvasRenderingContext2D.prototype, begin=C.beginPath, line=C.lineTo, move=C.moveTo, stroke=C.stroke;
          C.beginPath=function(){this.__points=[];return begin.call(this)};
          C.moveTo=function(x,y){this.__points?.push([x,y]);return move.call(this,x,y)};
          C.lineTo=function(x,y){this.__points?.push([x,y]);return line.call(this,x,y)};
          C.stroke=function(){window.__strokes.push({color:this.strokeStyle,points:this.__points});return stroke.call(this)};""")
        try:
            page.goto(args.url+'/taught-editor?map=curve', wait_until='networkidle')
            expect(page.locator('#editorCount')).to_have_text('3 点')
            expect(page.locator('#editorPreviewStatus')).to_contain_text('3 → 5')
            assert page.evaluate('window.__strokes.some(s=>s.color==="#38e1dc" && s.points?.length===5)')
            expect(page.locator('#editorSave')).to_be_disabled()
            page.screenshot(path=str(out/'ui-spline-preview.png'), full_page=True)
            passed('重开仅三控制点、五点后端曲线实际绘制、已保存参考保持独立')

            change(page, 2, 'Y', 2.2)
            expect(page.locator('#editorSave')).to_be_enabled()
            page.locator('#editorSave').click()
            expect(page.locator('#editorDirty')).to_have_text('未修改')
            assert len(saved[-1]['points']) == 3, saved[-1]
            expect(page.locator('#editorCount')).to_have_text('3 点')
            page.reload(wait_until='networkidle'); expect(page.locator('#editorCount')).to_have_text('3 点')
            expect(page.locator('#editorProcessing')).to_contain_text('无需额外调整')
            passed('保存发送控制点、保存回复与刷新不把稠密结果变成句柄')

            mode['hold'] = True; change(page, 2, 'Y', 2.3)
            page.wait_for_function('document.querySelector("#editorPreviewStatus").getAttribute("aria-busy")==="true"')
            page.wait_for_timeout(250); assert pending
            change(page, 2, 'Y', 2.4); expect(page.locator('#editorSave')).to_be_enabled()
            before = page.locator('#editorPreviewStatus').inner_text()
            pending.pop(0)[0].fulfill(json={'ok': False, 'msg': '迟到碰撞结果', 'data': None})
            page.wait_for_timeout(150)
            assert page.locator('#editorPreviewStatus').inner_text() == before
            expect(page.locator('#editorSave')).to_be_enabled()
            passed('服务忽略取消时，迟到旧预览也不会覆盖新控制点结果')

            change(page, 2, 'Y', -1)
            expect(page.locator('#editorPreviewStatus')).to_contain_text('碰撞')
            expect(page.locator('#editorSave')).to_be_disabled()
            assert page.evaluate('window.__strokes.some(s=>s.color==="#f06782" && s.points?.length===5)')
            page.screenshot(path=str(out/'ui-spline-collision.png'), full_page=True)
            page.locator('#editorUndo').click(); expect(page.locator('#editorSave')).to_be_enabled()
            passed('碰撞结果保留红色曲线与草稿、禁保存、撤销后重新预览')

            mode['unavailable'] = True; change(page, 2, 'Y', 2.5)
            expect(page.locator('#editorPreviewStatus')).to_contain_text('暂不可用')
            expect(page.locator('#editorSave')).to_be_disabled()
            expect(page.locator('#editorDirty')).to_have_text('有未保存修改')
            expect(page.locator('#editorY')).to_have_value('2.5')
            assert page.evaluate('window.__strokes.slice(-8).every(s=>s.color!=="#38e1dc" || !s.points || s.points.length!==5)')
            mode['unavailable'] = False; page.locator('#editorPreviewRetry').click()
            expect(page.locator('#editorSave')).to_be_enabled()
            passed('服务故障保留控制点和历史、明确报错、可重试且无直线冒充曲线')

            mode['hold'] = True; change(page, 2, 'Y', 2.6); page.wait_for_timeout(250); assert pending
            page.locator('#editorMap').select_option('other'); page.locator('.scrim [data-a=o]').click()
            expect(page.locator('#editorMapTitle')).to_have_text('other')
            expect(page.locator('#editorPreviewStatus')).to_contain_text('3 → 5')
            stale_route, stale_result = pending.pop(0); stale_route.fulfill(json=stale_result)
            page.wait_for_timeout(150); expect(page.locator('#editorMap')).to_have_value('other')
            expect(page.locator('#editorCount')).to_have_text('3 点')
            passed('换图后迟到曲线不能叠上新地图')

            page.locator('#editorMap').select_option('gap')
            expect(page.locator('#editorPreviewStatus')).to_contain_text('断口')
            expect(page.locator('#editorSave')).to_be_disabled()
            assert {p['continuous_id'] for p in report['requests'][-1]['body']['points']} == {0, 9}
            page.locator('#editorJoinGaps').click()
            expect(page.locator('.scrim [data-a=o]')).to_have_text('连接断口并预览')
            page.locator('.scrim [data-a=c]').click()
            expect(page.locator('#editorDirty')).to_have_text('未修改')
            page.locator('#editorJoinGaps').click(); page.locator('.scrim [data-a=o]').click()
            expect(page.locator('#editorSave')).to_be_enabled()
            page.locator('#editorUndo').click()
            expect(page.locator('#editorPreviewStatus')).to_contain_text('断口')
            expect(page.locator('#editorSave')).to_be_disabled()
            passed('断口不会自动合并，连接需明确确认且可撤销')

            for width, height in [(900, 780), (375, 900)]:
                page.set_viewport_size({'width': width, 'height': height}); page.wait_for_timeout(150)
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
                page.screenshot(path=str(out/f'ui-spline-{width}.png'), full_page=True)
            assert not report['errors'], report['errors']; assert not report['websockets'], report['websockets']
            passed('窄屏无横向溢出、全程无JS错误或WS连接')

            main_page = browser.new_page(viewport={'width': 1440, 'height': 1000})
            main_page.on('pageerror', lambda e: report['errors'].append(str(e)))
            main_page.on('websocket', lambda ws: report['websockets'].append(ws.url))
            main_page.goto(args.url+'/?mock&debug', wait_until='networkidle')
            main_page.locator('#taskTabs [data-t=taught]').click()
            main_page.locator('#taughtPrepare').click()
            box = main_page.locator('.scrim').last
            expect(box).to_contain_text('趴着或站着都可以准备')
            expect(box).to_contain_text('自动站起')
            assert '已经站立' not in box.inner_text()
            box.locator('[data-still]').check()
            main_page.screenshot(path=str(out/'ui-prepare-auto-stand.png'), full_page=True)
            box.locator('[data-a=o]').click()
            expect(main_page.locator('#taughtStart')).to_be_enabled()
            assert main_page.evaluate('go2w.store.taught.capture===null')
            # 用接口返回形状的八项快照检查展示，不把纯前端fixture当作真实传感器自检。
            main_page.evaluate('''() => {
              window.__goodChecks=[
                ['standing','站立就绪','需站起并由策略接管'],
                ['stack','导航与定位服务','需运行定位、导航、速度桥和示教节点'],
                ['map','底图一致','实际/map与所选文件一致'],
                ['measurement','测量与坐标变换','测量与TF按同一时刻，均为新鲜数据'],
                ['localization_quality','定位质量','位置σ 0.030m，测量时刻匹配率 100.0%；需要≤0.3m、≥60%'],
                ['localized','定位来源已确认','换图或站起后需重新定位'],
                ['still','机器人静止','开始和继续采集前保持静止'],
                ['exclusive','任务状态','没有其他导航任务或地图切换']
              ].map(([key,label,message])=>({key,label,message,ok:true}));
              go2w.store.taught.check={state:'ready',ok:true,checks:window.__goodChecks};
            }''')
            expect(main_page.locator('#taughtPrepare')).to_be_disabled()
            expect(main_page.locator('#taughtPrepare')).to_have_text('已准备')
            expect(main_page.locator('#taughtCheckNote')).to_have_text('自检通过')
            expect(main_page.locator('#taughtCheckResult')).to_have_attribute('data-s','ok')
            expect(main_page.locator('#taughtChecks li[data-s=ok]')).to_have_count(8)
            expect(main_page.locator('#taughtChecks svg')).to_have_count(8)
            assert not main_page.locator('#taughtCheckDetails').evaluate('el=>el.open')
            assert all(len(text)<=8 for text in main_page.locator('#taughtChecks li').all_inner_texts())
            main_page.locator('#taughtCheckDetailsLabel').focus()
            main_page.keyboard.press('Enter')
            expect(main_page.locator('#taughtCheckMessages')).to_be_visible()
            expect(main_page.locator('#taughtCheckMessages')).to_contain_text('0.030m')
            main_page.keyboard.press('Enter')
            assert not main_page.locator('#taughtCheckDetails').evaluate('el=>el.open')
            main_page.screenshot(path=str(out/'ui-checks-ready.png'), full_page=True)
            passed('自检结果高亮、八项图标短标签、完成后置灰与键盘展开详情')
            main_page.evaluate('''() => {
              const d=go2w.store.taught;
              d.check={state:'failed',ok:false,checks:window.__goodChecks.map(item=>item.key==='localization_quality'
                ? {...item,ok:false,message:'匹配率 45%，需要至少60%；请修正定位。'} : item)};
            }''')
            expect(main_page.locator('#taughtCheckResult')).to_have_attribute('data-s','crit')
            expect(main_page.locator('#taughtCheckProblems')).to_contain_text('匹配率 45%')
            expect(main_page.locator('#taughtCheckProblems li')).to_have_count(1)
            expect(main_page.locator('#taughtPrepare')).to_be_enabled()
            expect(main_page.locator('#taughtStart')).to_be_disabled()
            main_page.screenshot(path=str(out/'ui-checks-failed.png'), full_page=True)
            passed('失败只展开问题项、红色状态和具体原因，允许重新准备')
            main_page.evaluate("go2w.store.taught.check={state:'ready',ok:true,checks:window.__goodChecks}")
            expect(main_page.locator('#taughtPrepare')).to_be_disabled()
            for width, height in [(900,780),(375,900)]:
                main_page.set_viewport_size({'width':width,'height':height})
                main_page.locator('#taughtCheckResult').scroll_into_view_if_needed()
                assert main_page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
                assert main_page.locator('#taughtChecks').evaluate('el=>el.scrollWidth<=el.clientWidth+1')
                main_page.screenshot(path=str(out/f'ui-checks-{width}.png'), full_page=True)
            main_page.set_viewport_size({'width':1440,'height':1000})
            passed('自检面板900及375px无横向溢出，状态保持两列')
            main_page.locator('#taughtStart').click(); main_page.locator('.scrim [data-a=o]').click()
            # 模拟源边界只提供服务器结束结果，不把模拟当作真实平滑或足迹验证。
            main_page.evaluate('''() => {
              const d=go2w.store.taught; d.capture.sample_count=8;
              go2w.src.cmd.taughtFinish=() => {
                d.capture.state='processing';
                return new Promise(resolve=>window.__finishResult=()=>resolve({ok:true,data:{...d,revision:d.revision+1,
                  capture:{...d.capture,state:'finished'},processing:{input_count:8,output_count:41,
                    interpolation:'recorded',max_offset:0,iterations:0,full_sweep_checked:true}}}));
              };
            }''')
            main_page.locator('#taughtFinish').click()
            expect(main_page.locator('.scrim [data-a=o]')).to_have_text('结束并平滑保存')
            main_page.locator('.scrim [data-a=o]').click()
            expect(main_page.locator('#taughtProcessing')).to_contain_text('正在加密采样、平滑路线')
            main_page.evaluate('window.__finishResult()')
            expect(main_page.locator('#taughtProcessing')).to_contain_text('8 → 41')
            expect(main_page.locator('#taughtProcessing')).to_contain_text('无需调整')
            main_page.locator('#taughtProcessing').scroll_into_view_if_needed()
            main_page.screenshot(path=str(out/'ui-recorded-processing-result.png'), full_page=True)
            assert not report['errors'], report['errors']; assert not report['websockets'], report['websockets']
            passed('主面板允许趴姿准备且仍需明确开始采集，结束过程中和完成后均显示平滑信息')

            if args.live_backend:
                before = json.loads(urlopen(args.url+'/api/taught/maps/editor').read())
                lab_before = json.loads(urlopen(args.url+'/api/taught/maps/lab').read())
                assert before.get('simulated') is True and lab_before.get('simulated') is True, '只允许明确标记的假ROS后端'
                live_editor = browser.new_page(viewport={'width': 1440, 'height': 1000})
                live_editor.on('pageerror', lambda e: report['errors'].append(str(e)))
                live_editor.goto(args.url+'/taught-editor?map=editor', wait_until='networkidle')
                expect(live_editor.locator('#editorPreviewStatus')).to_contain_text('预览通过')
                with live_editor.expect_response(lambda response: response.url.endswith('/api/taught/maps/editor/preview')) as response:
                    change(live_editor, 2, 'Y', 2.0)
                actual_preview = response.value.json()
                assert actual_preview['ok'], actual_preview
                details = actual_preview['data']; processing = details['processing']
                assert processing['interpolation'] == 'cubic_spline' and processing['max_offset'] == 0
                assert processing['full_sweep_checked'] is True
                assert len(details['preview_points']) > len(details['control_points'])
                expect(live_editor.locator('#editorCount')).to_have_text(f"{len(details['control_points'])} 点")
                expect(live_editor.locator('#editorSummary')).to_contain_text(f"{len(details['preview_points'])} 曲线点")
                expect(live_editor.locator('#editorSave')).to_be_enabled()
                expect(live_editor.locator('#editorPreviewStatus')).to_contain_text('无需额外调整')
                live_editor.screenshot(path=str(out/'ui-live-spline-preview.png'), full_page=True)
                assert json.loads(urlopen(args.url+'/api/taught/maps/editor').read())['revision'] == before['revision']
                report['live_spline'] = {'revision': before['revision'], 'controls': len(details['control_points']),
                                        'preview_points': len(details['preview_points']), 'processing': processing}
                passed('真实后端样条预览与稀疏句柄分离、完整足迹通过、预览未写修订', **report['live_spline'])

                live = browser.new_page(viewport={'width': 1440, 'height': 1000})
                states = []; commands = []
                def watch_socket(ws):
                    def received(payload):
                        if not isinstance(payload, str): return
                        value = json.loads(payload)
                        state = value.get('check', {}).get('state') if value.get('t') == 'taught' and value.get('map') == 'lab' else None
                        if state and (not states or states[-1] != state): states.append(state)
                    def sent(payload):
                        if isinstance(payload, str):
                            value = json.loads(payload)
                            if value.get('t') == 'cmd': commands.append(value.get('op'))
                    ws.on('framereceived', received); ws.on('framesent', sent)
                live.on('websocket', watch_socket)
                live.on('pageerror', lambda e: report['errors'].append(str(e)))
                live.goto(args.url+'/?debug', wait_until='networkidle')
                live.wait_for_function('go2w.store.ctrl?.on && go2w.store.link.ok')
                if live.evaluate('go2w.store.ctrl.state') == 'Velocity':
                    reset = live.evaluate('go2w.src.cmd.lieDown()')
                    assert reset.get('ok') is True, reset
                    live.wait_for_function('go2w.store.ctrl.state==="Passive" && !go2w.store.ctrl.op', timeout=20000)
                    commands.clear(); states.clear()
                assert live.evaluate('go2w.store.ctrl.state') == 'Passive', '本例需要假机器人初始趴姿'
                live.locator('#taskTabs [data-t=taught]').click()
                live.locator('#taughtMapSel').select_option('lab')
                expect(live.locator('#taughtPrepare')).to_be_enabled()
                live.locator('#taughtPrepare').click(); live.locator('.scrim [data-still]').check()
                live.locator('.scrim [data-a=o]').click()
                live.wait_for_function('go2w.store.taught?.check?.state==="settling"', timeout=20000)
                expect(live.locator('#taughtCheckNote')).to_contain_text('站稳')
                expect(live.locator('#taughtStart')).to_be_disabled()
                expect(live.locator('#taughtPrepare')).to_be_disabled()
                live.screenshot(path=str(out/'ui-live-prepare-settling.png'), full_page=True)
                live.wait_for_function('go2w.store.taught?.check?.state==="ready"', timeout=110000)
                expect(live.locator('#taughtStart')).to_be_enabled()
                expect(live.locator('#taughtPrepare')).to_be_disabled()
                expect(live.locator('#taughtPrepare')).to_have_text('已准备')
                expect(live.locator('#taughtCheckNote')).to_have_text('自检通过')
                expect(live.locator('#taughtChecks li[data-s=ok]')).to_have_count(8)
                report['live_prepare'] = {'initial_ctrl': 'Passive', 'final_ctrl': live.evaluate('go2w.store.ctrl.state'),
                                          'states': states, 'commands': commands,
                                          'capture_started': live.evaluate('!!go2w.store.taught.capture'),
                                          'armed': live.evaluate('go2w.store.armed'), 'lab_revision': lab_before['revision']}
                live.screenshot(path=str(out/'ui-live-prepare-ready.png'), full_page=True)
                assert live.evaluate('go2w.store.ctrl.state') == 'Velocity'
                assert live.evaluate('go2w.store.taught.check.ok && !go2w.store.taught.capture && !go2w.store.armed')
                assert 'taught_prepare' in commands, commands
                assert not any(op in commands for op in ['stand_up', 'arm', 'taught_start', 'teleop']), commands
                assert states.index('standing') < states.index('settling') < states.index('localizing') < states.index('ready'), states
                # 快速载图可能在两次状态广播之间完成；只对已广播的loading核对顺序。
                if 'loading' in states:
                    assert states.index('settling') < states.index('loading') < states.index('localizing'), states
                assert json.loads(urlopen(args.url+'/api/taught/maps/lab').read())['revision'] == lab_before['revision']
                passed('真实假ROS主页趴姿准备顺序完整，自检通过后未自动采集、解锁或行走', **report['live_prepare'])
                assert not report['errors'], report['errors']
            report['ok'] = True
        except Exception as error:
            report['ok'] = False; report['error'] = repr(error)
            page.screenshot(path=str(out/'ui-refine-failure.png'), full_page=True)
            raise
        finally:
            (out/'frontend-refine-browser.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
            browser.close()


if __name__ == '__main__':
    main()
