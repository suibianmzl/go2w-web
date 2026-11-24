"""本地假ROS与虚拟麦克风：提前一键上传等待20秒语音，刷新后的待处理语音可恢复。"""
import argparse
import json
from pathlib import Path
from playwright.sync_api import sync_playwright, expect


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url',default='http://127.0.0.1:8097')
    parser.add_argument('--data',default='/tmp/go2w-bundle-browser')
    args=parser.parse_args()
    assert args.url.startswith(('http://127.0.0.1:','http://localhost:'))
    data=Path(args.data);assert str(data).startswith('/tmp/go2w-')
    errors=[]
    with sync_playwright() as p:
        browser=p.chromium.launch(executable_path='/opt/google/chrome/chrome',headless=True,
            args=['--use-fake-device-for-media-stream','--use-fake-ui-for-media-stream'])
        context=browser.new_context(permissions=['microphone'],viewport={'width':1440,'height':1000})
        context.route('https://fonts.googleapis.com/**',lambda route:route.abort())
        context.route('https://fonts.gstatic.com/**',lambda route:route.abort())
        main_page=context.new_page();main_page.on('pageerror',lambda e:errors.append(str(e)))
        main_page.goto(args.url+'/?debug',wait_until='networkidle')
        main_page.wait_for_function('window.go2w?.store.link.ok')
        assert main_page.evaluate("go2w.src.cmd.setMode('localization','test')")['ok']
        main_page.wait_for_function('go2w.store.profiles.localization === "run" && !go2w.store.busy && go2w.store.rollback.run',timeout=45000)
        phone=context.new_page()
        phone.set_viewport_size({'width':375,'height':812});phone.on('pageerror',lambda e:errors.append(str(e)))
        phone.goto(args.url+'/record',wait_until='networkidle')
        expect(phone.locator('#recordButton')).to_be_enabled(timeout=10000)
        phone.locator('#voiceSeconds').fill('20');phone.locator('#voiceSeconds').press('Tab')
        phone.locator('#recordButton').click()
        main_page.wait_for_function('go2w.store.bags.length===1',timeout=20000)
        name=main_page.evaluate('go2w.store.bags[0].name')
        assert not (data/'bags'/name/'voice-note.audio').exists()
        result=main_page.evaluate('(name)=>go2w.src.cmd.uploadBag(name)',name)
        assert result['ok'],result
        main_page.wait_for_function('(name)=>go2w.store.uploads[name]?.phase==="waiting"',arg=name)
        assert not (data/'remote'/name).exists(),'语音尚未完成时不能提前传数据'
        expect(phone.locator('#voiceStatus')).to_contain_text('数据包与语音备注已保存',timeout=30000)
        main_page.wait_for_function('(name)=>go2w.store.uploads[name]?.state==="done"',arg=name,timeout=10000)
        local,dest=data/'bags'/name,data/'remote'/name
        link=json.loads((dest/'recording-link.json').read_text())
        assert link['bag']['name']==name and link['audio']['state']=='ready'
        for filename in [*link['bag']['data_files'],'metadata.yaml','capture.json','voice-note.audio','voice-note.json']:
            assert (local/filename).read_bytes()==(dest/filename).read_bytes(),filename
        voice=json.loads((dest/'voice-note.json').read_text())
        assert voice['request_id']==link['request_id']
        print('PASS 数据包完成时提前点击一次，等待20秒录音后真实rsync一起传至本地临时目标，逐文件一致',flush=True)

        orphan=data/'bags/orphan-capture';orphan.mkdir()
        (orphan/'metadata.yaml').write_text('duration:\n  nanoseconds: 1000000000\n')
        (orphan/'orphan_0.mcap').write_bytes(b'isolated-test-only')
        (orphan/'capture.json').write_text(json.dumps({'name':orphan.name,'request_id':'orphan-one','voice':{'state':'pending','requested_s':5}}))
        phone.reload(wait_until='networkidle')
        button=phone.get_by_role('button',name='结束待处理语音')
        expect(button).to_have_count(1,timeout=6000)
        button.click();phone.locator('.scrim [data-a=o]').click()
        expect(button).to_have_count(0,timeout=6000)
        capture=json.loads((orphan/'capture.json').read_text())
        assert capture['voice']['state']=='unavailable'
        assert capture['request_id']=='orphan-one'
        print('PASS 手机页重新打开后可以明确结束未保存的语音，并保留原包与对应记录编号',flush=True)
        assert not errors,errors
        browser.close()


if __name__=='__main__':main()
