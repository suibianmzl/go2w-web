#!/usr/bin/env python3
"""实际 Chrome UI 验收：mock 采集及 editor 专用地图文件；不向真实主页下运动命令。"""
import argparse
from pathlib import Path
import json
import math
import struct
from urllib.request import urlopen

from playwright.sync_api import sync_playwright, expect

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://127.0.0.1:8838')
    parser.add_argument('--out', default='/home/tink/program/go2w/data/development/web-taught-20261118')
    args = parser.parse_args()
    assert args.url.startswith(('http://127.0.0.1:', 'http://localhost:')), '只允许本地假 ROS 后端'
    out = Path(args.out); out.mkdir(parents=True, exist_ok=True)
    results = {'url': args.url, 'steps': [], 'page_errors': [], 'editor_posts': [], 'editor_websockets': []}


    def passed(name, **data):
        results['steps'].append({'name': name, **data})
        print('PASS', name, json.dumps(data, ensure_ascii=False), flush=True)


    def modal(page, check=False):
        box = page.locator('.scrim').last
        expect(box).to_be_visible()
        if check:
            box.locator('[data-still]').check()
        box.locator('[data-a=o]').click()


    def monitor(page, name):
        page.on('pageerror', lambda e: results['page_errors'].append(f'{name}: {e}'))


    def mock_capture(page):
        return page.evaluate('go2w.store.taught.capture')


    def await_capture(page, state):
        page.wait_for_function('(state)=>go2w.store.taught?.capture?.state===state', arg=state)


    def drive_mock(page, duration=700):
        assert 'mock' in page.url
        page.locator('#taughtTeleop').click()
        page.keyboard.down('w'); page.wait_for_timeout(duration); page.keyboard.up('w'); page.wait_for_timeout(500)


    def get_editor_doc():
        return json.loads(urlopen(args.url + '/api/taught/maps/editor').read())


    packet = urlopen(args.url + '/api/taught/maps/editor/grid').read()
    length = struct.unpack('<I', packet[1:5])[0]
    grid = json.loads(packet[5:5+length])


    def point_on_canvas(page, x, y, display_angle=0):
        # 与产品共享同一变换约定，但这里由地图四角和 DOM 尺寸独立计算，检验实际拖动/旋转结果。
        box = page.locator('#editorCanvas').bounding_box()
        w, h = box['width'], box['height']
        yaw = grid.get('oyaw', 0)
        corners = []
        for u, v in [(0,0),(grid['w']*grid['res'],0),(0,grid['h']*grid['res']),(grid['w']*grid['res'],grid['h']*grid['res'])]:
            wx = grid['ox']+math.cos(yaw)*u-math.sin(yaw)*v
            wy = grid['oy']+math.sin(yaw)*u+math.cos(yaw)*v
            corners.append((math.cos(display_angle)*wx-math.sin(display_angle)*wy,math.sin(display_angle)*wx+math.cos(display_angle)*wy))
        left,right=min(p[0] for p in corners),max(p[0] for p in corners)
        bottom,top=min(p[1] for p in corners),max(p[1] for p in corners)
        scale=max(.01,min((w-50)/max(.1,right-left),(h-170)/max(.1,top-bottom))*.94)
        rx=math.cos(display_angle)*x-math.sin(display_angle)*y
        ry=math.sin(display_angle)*x+math.cos(display_angle)*y
        return {'x':w/2+(rx-(left+right)/2)*scale,'y':h/2-(ry-(bottom+top)/2)*scale}


    def choose_index(page, index):
        page.locator('#editorIndex').fill(str(index));page.locator('#editorIndex').press('Tab')


    def change_y(page, index, y):
        choose_index(page,index);page.locator('#editorY').fill(str(y));page.locator('#editorApply').click()


    def save_editor(page):
        with page.expect_response(lambda r: r.request.method == 'POST' and r.url.endswith('/api/taught/maps/editor'), timeout=125000) as r:
            page.locator('#editorSave').click()
        value=r.value.json();results['editor_posts'].append(value)
        return value


    with sync_playwright() as p:
        browser=p.chromium.launch(executable_path='/opt/google/chrome/chrome',headless=True)
        try:
            mock=browser.new_page(viewport={'width':1600,'height':1000})
            monitor(mock,'mock')
            mock.goto(args.url+'/?mock&debug',wait_until='networkidle')
            mock.locator('#taskTabs [data-t=taught]').click()
            expect(mock.locator('#taughtStart')).to_be_disabled()
            mock.locator('#taughtPrepare').click();modal(mock,check=True)
            expect(mock.locator('#taughtStart')).to_be_enabled()
            mock.locator('#taughtStart').click();modal(mock);await_capture(mock,'recording')
            session=mock_capture(mock)['session_id']
            drive_mock(mock)
            assert mock_capture(mock)['sample_count'] >= 3
            mock.screenshot(path=str(out/'ui-mock-recording.png'),full_page=True)
            mock.locator('#taughtPause').click();await_capture(mock,'paused')
            count=mock_capture(mock)['sample_count'];mock.wait_for_timeout(700)
            assert mock_capture(mock)['sample_count'] == count
            mock.locator('#taughtPrepare').click();modal(mock,check=True)
            assert mock_capture(mock)['session_id'] == session and mock_capture(mock)['state'] == 'paused'
            mock.locator('#taughtResume').click();modal(mock);await_capture(mock,'recording')
            drive_mock(mock)
            assert len({point['continuous_id'] for point in mock_capture(mock)['points']}) >= 2
            passed('模拟显式准备、遥控采样、暂停不采样、同图准备与人工恢复',samples=mock_capture(mock)['sample_count'])
            mock.evaluate('go2w.src.cmd.simulateDrop()');await_capture(mock,'interrupted')
            mock.wait_for_function('!go2w.store.link.ok')
            mock.wait_for_function('go2w.store.link.ok')
            assert mock_capture(mock)['state'] == 'interrupted'
            mock.locator('#taughtPrepare').click();modal(mock,check=True)
            mock.locator('#taughtResume').click();modal(mock);await_capture(mock,'recording')
            mock.locator('#taskTabs [data-t=nav]').click();modal(mock);await_capture(mock,'paused')
            expect(mock.locator('#flowNav')).to_be_visible()
            mock.locator('#taskTabs [data-t=taught]').click()
            expect(mock.locator('#flowTaught')).to_be_visible()
            mock.locator('#taughtFinish').click();modal(mock);await_capture(mock,'finished')
            assert mock.evaluate('go2w.store.taught.revision') == 1
            assert mock.locator('[data-l=taught]').count() == 1
            assert '点' in mock.locator('#taughtLayerMeta').inner_text()
            mock.screenshot(path=str(out/'ui-mock-saved.png'),full_page=True)
            passed('模拟断线不自恢复、切任务先暂停、结束保存和唯一图层')

            editor=browser.new_page(viewport={'width':1440,'height':1000})
            monitor(editor,'editor');editor.on('websocket',lambda ws:results['editor_websockets'].append(ws.url))
            editor.goto(args.url+'/taught-editor?map=editor',wait_until='networkidle')
            expect(editor.locator('#editorNew')).to_be_enabled()
            assert '0.05 m' in editor.locator('#editorMeta').inner_text()
            if get_editor_doc()['points']:
                editor.locator('#editorNew').click();modal(editor)
            editor.locator('[data-editor-tool=add]').click()
            for x,y in [(-2.,1.),(0.,1.),(2.,1.)]:editor.locator('#editorCanvas').click(position=point_on_canvas(editor,x,y))
            expect(editor.locator('#editorCount')).to_have_text('3 点')
            editor.locator('[data-editor-tool=select]').click()
            a=point_on_canvas(editor,0.,1.);b=point_on_canvas(editor,0.,1.3);box=editor.locator('#editorCanvas').bounding_box()
            editor.mouse.move(box['x']+a['x'],box['y']+a['y']);editor.mouse.down();editor.mouse.move(box['x']+b['x'],box['y']+b['y'],steps=8);editor.mouse.up()
            assert abs(float(editor.locator('#editorY').input_value())-1.3) < .025
            editor.locator('#editorInsert').click();expect(editor.locator('#editorCount')).to_have_text('4 点')
            editor.locator('#editorDelete').click();expect(editor.locator('#editorCount')).to_have_text('3 点')
            editor.locator('#editorUndo').click();expect(editor.locator('#editorCount')).to_have_text('4 点')
            editor.locator('#editorRedo').click();expect(editor.locator('#editorCount')).to_have_text('3 点')
            editor.locator('#editorUndo').click()
            editor.locator('#editorRangeStart').fill('2');editor.locator('#editorRangeEnd').fill('3');editor.locator('#editorRangeSelect').click()
            assert '选中 2 点' in editor.locator('#editorSummary').inner_text()
            editor.locator('#editorDelete').click();expect(editor.locator('#editorCount')).to_have_text('2 点')
            editor.locator('#editorUndo').click();expect(editor.locator('#editorCount')).to_have_text('4 点')
            passed('独立编辑器绘制、实际拖点、插入、删除、区间选择、撤销重做')
            editor.locator('#editorRotateLeft').click();expect(editor.locator('#editorAngle')).to_have_text('显示 90°')
            choose_index(editor,1)
            a=point_on_canvas(editor,-2.,1.,math.pi/2);b=point_on_canvas(editor,-2.,1.1,math.pi/2);box=editor.locator('#editorCanvas').bounding_box()
            editor.mouse.move(box['x']+a['x'],box['y']+a['y']);editor.mouse.down();editor.mouse.move(box['x']+b['x'],box['y']+b['y'],steps=8);editor.mouse.up()
            assert abs(float(editor.locator('#editorX').input_value())+2) < .025
            assert abs(float(editor.locator('#editorY').input_value())-1.1) < .025
            editor.screenshot(path=str(out/'ui-editor-rotated-draft.png'),full_page=True)
            response=save_editor(editor);assert response['ok'],response
            expect(editor.locator('#editorDirty')).to_have_text('未修改')
            saved=get_editor_doc();assert saved['revision'] > 0 and saved['executable']
            assert abs(saved['points'][0]['x']+2)<.05 and abs(saved['points'][0]['y']-1.1)<.05
            editor.reload(wait_until='networkidle')
            saved_controls=saved.get('control_points',saved['points'])
            expect(editor.locator('#editorCount')).to_have_text(f"{len(saved_controls)} 点")
            expect(editor.locator('#editorDirty')).to_have_text('未修改')
            passed('旋转90度后拖点保持世界坐标、真实校验保存与刷新恢复',revision=saved['revision'],points=len(saved['points']))

            other=browser.new_page(viewport={'width':1280,'height':900});monitor(other,'editor-other')
            other.goto(args.url+'/taught-editor?map=editor',wait_until='networkidle')
            change_y(editor,1,1.11)
            change_y(other,len(saved_controls),1.05)
            updated=save_editor(other);assert updated['ok'],updated
            conflict=save_editor(editor);assert conflict['ok'] is False,conflict
            expect(editor.locator('#editorDirty')).to_have_text('有未保存修改')
            assert abs(float(editor.locator('#editorY').input_value())-1.11)<1e-4
            assert '草稿已保留' in editor.locator('#editorMessage').inner_text()
            editor.screenshot(path=str(out/'ui-editor-conflict.png'),full_page=True)
            passed('真实并发修订冲突保留草稿与撤销链',response=conflict)
            editor.locator('#editorReload').click();modal(editor)
            expect(editor.locator('#editorDirty')).to_have_text('未修改')
            for width,height,name in [(900,780,'narrow'),(375,900,'mobile')]:
                editor.set_viewport_size({'width':width,'height':height});editor.wait_for_timeout(250)
                assert editor.evaluate('document.documentElement.scrollWidth <= window.innerWidth+1')
                editor.locator('#editorSave').scroll_into_view_if_needed()
                editor.screenshot(path=str(out/f'ui-editor-{name}.png'),full_page=True)
                assert editor.locator('#editorMap').is_visible()
                passed('编辑器窄屏无水平溢出',width=width)
            assert not results['editor_websockets'],results['editor_websockets']

            real=browser.new_page(viewport={'width':1440,'height':900});monitor(real,'real-readonly')
            commands=[]
            def inspect_ws(ws):
                ws.on('framesent',lambda payload:commands.append(json.loads(payload)) if isinstance(payload,str) and json.loads(payload).get('t')=='cmd' else None)
            real.on('websocket',inspect_ws)
            real.goto(args.url+'/',wait_until='networkidle')
            expect(real.locator('#taskTabs [data-t=taught]')).to_be_visible()
            real.screenshot(path=str(out/'ui-real-readonly.png'),full_page=True)
            assert not commands,commands
            assert not results['page_errors'],results['page_errors']
            passed('真实主页只读结构检查、编辑器无WS、全部页面无JS错误')
            results['ok']=True
        except Exception as error:
            results['ok']=False;results['error']=repr(error)
            all_pages=[page for context in browser.contexts for page in context.pages]
            for i,page in enumerate(all_pages):
                try:page.screenshot(path=str(out/f'ui-failure-{i}.png'),full_page=True)
                except Exception:pass
            raise
        finally:
            (out/'frontend-browser-report.json').write_text(json.dumps(results,ensure_ascii=False,indent=2))
            browser.close()


if __name__ == '__main__':
    main()
