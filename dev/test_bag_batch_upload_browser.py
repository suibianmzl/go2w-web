"""Batch uploads, stable selection and per-bag controls in local Chrome mock mode."""
import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright, expect


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url',default='http://127.0.0.1:8098')
    parser.add_argument('--out',default='/tmp/go2w-bag-upload-browser')
    args=parser.parse_args()
    assert args.url.startswith(('http://127.0.0.1:','http://localhost:'))
    out=Path(args.out);out.mkdir(parents=True,exist_ok=True)
    errors=[]
    with sync_playwright() as p:
        browser=p.chromium.launch(executable_path='/opt/google/chrome/chrome',headless=True)
        context=browser.new_context(viewport={'width':1440,'height':1000},reduced_motion='reduce')
        context.route('https://fonts.googleapis.com/**',lambda r:r.abort())
        context.route('https://fonts.gstatic.com/**',lambda r:r.abort())
        page=context.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
        start=datetime(2026,11,25,tzinfo=timezone.utc)
        page.clock.install(time=start)
        page.goto(args.url+'/?mock&debug',wait_until='networkidle')
        page.wait_for_function('go2w?.store.source === "mock"')
        page.clock.pause_at(start+timedelta(seconds=2))
        page.evaluate('''() => {
          go2w.store.bags = ['first','second','third','fourth','fifth'].map(name => ({name:name+'-bag',dur:30,size:42}));
          go2w.store.uploads = {};
        }''')
        page.clock.run_for(1100)
        page.locator('#bagHistory > summary').click()
        rows=page.locator('#bagList .mrow.bag')
        expect(rows).to_have_count(5)
        expect(page.locator('#bagUploadSelected')).to_be_disabled()
        for name in ['first-bag','second-bag','third-bag']:
            page.get_by_role('checkbox',name='选择录包 '+name,exact=True).check()
        expect(page.locator('#bagUploadAll')).to_have_js_property('indeterminate',True)
        # The server republishes the list in a different order while selection survives.
        page.evaluate('go2w.store.bags.reverse(); go2w.store.uploads = {"fifth-bag":{state:"fail",msg:"temporary failure"}}')
        page.clock.run_for(1100)
        for name in ['first-bag','second-bag','third-bag']:
            expect(page.get_by_role('checkbox',name='选择录包 '+name,exact=True)).to_be_checked()
        page.locator('#bagUploadSelected').click()
        states=page.evaluate('Object.values(go2w.store.uploads).map(u=>u.state)')
        assert states.count('run')==2 and states.count('queued')==1,states
        page.clock.run_for(1000)
        expect(page.locator('#bagList .bag-st.run')).to_have_count(2)
        expect(page.locator('#bagList .bag-st.queued')).to_have_count(1)
        expect(page.get_by_role('button',name='删除录包 third-bag',exact=True)).to_be_disabled()
        expect(page.get_by_role('button',name='上传录包 fourth-bag',exact=True)).to_be_enabled()
        page.get_by_role('button',name='上传录包 fourth-bag',exact=True).click()
        assert page.evaluate('go2w.src.cmd.uploadBag("third-bag").ok') is False
        assert page.evaluate('go2w.src.cmd.deleteBag("fourth-bag").ok') is False
        page.clock.run_for(6000)
        assert page.evaluate('["first-bag","second-bag","third-bag","fourth-bag"].every(n=>go2w.store.uploads[n].state==="done")')
        expect(page.locator('#bagList .bag-st.done')).to_have_count(4)
        page.locator('#bagUploadAll').check()
        expect(page.locator('#bagList input[data-bag-select]:checked')).to_have_count(1)
        expect(page.get_by_role('checkbox',name='选择录包 first-bag',exact=True)).to_be_disabled()
        result=page.evaluate('go2w.src.cmd.uploadBag("first-bag")')
        assert result['ok'] and '无需重复上传' in result['msg']
        assert page.evaluate('go2w.store.uploads["first-bag"].state')=='done'
        verify=page.get_by_role('button',name='核对已上传录包 first-bag',exact=True)
        verify.click()
        expect(page.locator('.scrim')).to_contain_text('仍使用同一个目录')
        page.locator('.scrim [data-a=c]').click()
        verify.click()
        page.locator('.scrim [data-a=o]').click()
        assert page.evaluate('go2w.store.uploads["first-bag"].state')=='run'
        page.clock.run_for(3000)
        assert page.evaluate('go2w.store.uploads["first-bag"].state')=='done'
        page.locator('#bagUploadSelected').click()
        expect(page.locator('.scrim')).to_have_count(0)
        assert page.evaluate('go2w.store.uploads["fifth-bag"].state')=='run'
        page.clock.run_for(3000)
        assert page.evaluate('go2w.store.bags.length')==5
        expect(page.locator('#bagUploadAll')).to_be_disabled()
        expect(page.locator('#bagList input[data-bag-select]:checked')).to_have_count(0)
        for width,height in [(1440,1000),(375,812)]:
            page.set_viewport_size({'width':width,'height':height})
            page.locator('#drawer').evaluate('(e)=>e.style.setProperty("--dh","680px")')
            page.locator('#recordingControls').evaluate('(e)=>e.scrollTop=0')
            page.locator('#bagUploadSelected').scroll_into_view_if_needed()
            assert page.locator('#recordingControls').evaluate('(e)=>e.scrollWidth<=e.clientWidth'),width
            page.locator('#recordingControls').screenshot(path=str(out/f'uploads-{width}.png'))
        assert not errors,errors
        print('PASS batch selection/repaint, two uploads plus queue, independent row controls, duplicate/delete safeguards and desktop/mobile layout')
        browser.close()


if __name__=='__main__':main()
