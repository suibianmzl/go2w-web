#!/usr/bin/env python3
"""只读后端及浏览器内故障注入：负显示角、键盘、空/坏数据、迟到请求。仅使用 editor 地图。"""
import argparse
import copy
import json
from pathlib import Path
from urllib.request import urlopen

from playwright.sync_api import Error as PlaywrightError, expect, sync_playwright


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url',default='http://127.0.0.1:8838')
    parser.add_argument('--out',default='/home/tink/program/go2w/data/development/web-taught-20261118')
    args=parser.parse_args()
    assert args.url.startswith(('http://127.0.0.1:','http://localhost:'))
    out=Path(args.out);out.mkdir(parents=True,exist_ok=True)
    doc=json.loads(urlopen(args.url+'/api/taught/maps/editor').read())
    report={'steps':[],'errors':[],'posts':[],'websockets':[]}
    def passed(name,**data):
        report['steps'].append({'name':name,**data});print('PASS',name,json.dumps(data,ensure_ascii=False),flush=True)
    with sync_playwright() as p:
        browser=p.chromium.launch(executable_path='/opt/google/chrome/chrome',headless=True)
        def new_page(name):
            page=browser.new_page(viewport={'width':1440,'height':1000})
            page.on('pageerror',lambda error:report['errors'].append(f'{name}: {error}'))
            page.on('websocket',lambda ws:report['websockets'].append(ws.url))
            page.on('request',lambda req:report['posts'].append(req.url) if req.method=='POST' else None)
            return page
        try:
            page=new_page('negative-angle')
            page.goto(args.url+'/taught-editor?map=editor',wait_until='networkidle')
            page.locator('#editorRotateRight').click();expect(page.locator('#editorAngle')).to_have_text('显示 -90°')
            page.locator('#editorIndex').fill('1');page.locator('#editorIndex').press('Tab')
            x,y=float(page.locator('#editorX').input_value()),float(page.locator('#editorY').input_value())
            page.locator('#editorCanvas').focus();page.keyboard.press('ArrowRight')
            assert abs(float(page.locator('#editorX').input_value())-x)<.0002
            assert abs(float(page.locator('#editorY').input_value())-y-.05)<.0002
            expect(page.locator('#editorDirty')).to_have_text('有未保存修改')
            page.keyboard.press('Control+z');expect(page.locator('#editorDirty')).to_have_text('未修改')
            page.screenshot(path=str(out/'ui-editor-negative-angle-keyboard.png'),full_page=True)
            passed('负90度显示下按屏幕方向键编辑、键盘撤销保持世界坐标')

            empty=new_page('empty-maps')
            empty.route('**/api/taught/maps',lambda route:route.fulfill(json={'maps':[]}))
            empty.goto(args.url+'/taught-editor',wait_until='networkidle')
            expect(empty.locator('#editorMap')).to_be_disabled();expect(empty.locator('#editorSave')).to_be_disabled()
            assert '没有可编辑地图' in empty.locator('#editorMessage').inner_text()
            empty.screenshot(path=str(out/'ui-editor-empty-maps.png'),full_page=True)
            passed('空地图列表给出明确入口并禁止保存')

            malformed=new_page('malformed-points');bad=copy.deepcopy(doc);bad['points']=[{'x':'broken','y':0}]
            malformed.route('**/api/taught/maps/editor',lambda route:route.fulfill(json=bad))
            malformed.goto(args.url+'/taught-editor?map=editor',wait_until='networkidle')
            expect(malformed.locator('#editorSave')).to_be_disabled()
            assert '坐标无效' in malformed.locator('#editorMessage').inner_text()
            passed('损坏轨迹坐标可见报错且不生成可保存草稿')

            badgrid=new_page('malformed-grid')
            badgrid.route('**/api/taught/maps/editor/grid',lambda route:route.fulfill(body=b'\x01\x02',content_type='application/octet-stream'))
            badgrid.goto(args.url+'/taught-editor?map=editor',wait_until='networkidle')
            expect(badgrid.locator('#editorSave')).to_be_disabled()
            assert '地图二进制数据不完整' in badgrid.locator('#editorMessage').inner_text()
            badgrid.screenshot(path=str(out/'ui-editor-malformed-grid.png'),full_page=True)
            passed('损坏地图二进制明确拒绝，不崩溃或套用旧底图')

            delayed=new_page('late-response');pending=[]
            delayed.route('**/api/taught/maps',lambda route:route.fulfill(json={'maps':[{'name':'editor'},{'name':'slow_fake'}]}))
            delayed.route('**/api/taught/maps/slow_fake',lambda route:pending.append(route))
            delayed.route('**/api/taught/maps/slow_fake/grid',lambda route:pending.append(route))
            delayed.goto(args.url+'/taught-editor?map=slow_fake',wait_until='domcontentloaded')
            delayed.wait_for_function('document.querySelector("#editorMap").value==="slow_fake"')
            delayed.wait_for_timeout(100)
            delayed.evaluate('const s=document.querySelector("#editorMap");s.value="editor";s.dispatchEvent(new Event("change"));')
            expect(delayed.locator('#editorMapTitle')).to_have_text('editor')
            expect(delayed.locator('#editorCount')).to_have_text(f"{len(doc['points'])} 点")
            for route in pending:
                stale=copy.deepcopy(doc);stale['map']='slow_fake'
                try:route.fulfill(json=stale)
                except PlaywrightError:pass  # AbortController 已取消的旧请求，浏览器可直接拒绝晚到回复。
            delayed.wait_for_load_state('networkidle')
            expect(delayed.locator('#editorMapTitle')).to_have_text('editor')
            expect(delayed.locator('#editorMap')).to_have_value('editor')
            passed('加载中换选后，迟到旧图请求无法抢占当前地图',old_requests=len(pending))
            assert not report['errors'],report['errors']
            assert not report['posts'],report['posts']
            assert not report['websockets'],report['websockets']
            passed('边界测试未写后端文件、未建WS、无JS错误')
            report['ok']=True
        except Exception as error:
            report['ok']=False;report['error']=repr(error)
            raise
        finally:
            (out/'frontend-browser-edges.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
            browser.close()


if __name__=='__main__':
    main()
