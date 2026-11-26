"""录包旁播放录音：真实Chrome录制/解码合成音频，不连接机器狗。"""
import functools
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.parse import urlsplit
from playwright.sync_api import sync_playwright, expect

ROOT=Path(__file__).resolve().parents[1]
AUDIO=b''
REQUESTS=[]
class Handler(SimpleHTTPRequestHandler):
    def log_message(self,*_): pass
    def do_GET(self):
        if urlsplit(self.path).path.startswith('/api/recordings/'):
            REQUESTS.append(self.path)
            if '/broken/' in self.path:
                self.send_error(404);return
            self.send_response(200);self.send_header('Content-Type','audio/webm')
            self.send_header('Content-Length',str(len(AUDIO)));self.end_headers();self.wfile.write(AUDIO);return
        super().do_GET()


def main():
    global AUDIO
    server=ThreadingHTTPServer(('127.0.0.1',0),functools.partial(Handler,directory=str(ROOT/'static')))
    Thread(target=server.serve_forever,daemon=True).start()
    errors=[];out=Path('/tmp/go2w-bag-voice-browser');out.mkdir(exist_ok=True)
    try:
        with sync_playwright() as p:
            browser=p.chromium.launch(executable_path='/opt/google/chrome/chrome',headless=True,
                args=['--autoplay-policy=no-user-gesture-required'])
            page=browser.new_page(viewport={'width':1440,'height':1000})
            page.route('https://fonts.googleapis.com/**',lambda route:route.abort())
            page.route('https://fonts.gstatic.com/**',lambda route:route.abort())
            page.on('pageerror',lambda e:errors.append(str(e)))
            page.goto(f'http://127.0.0.1:{server.server_port}/?mock&debug',wait_until='networkidle')
            page.wait_for_function('window.go2w?.store.source === "mock"')
            AUDIO=bytes(page.evaluate('''async () => {
                const context=new AudioContext(),destination=context.createMediaStreamDestination();
                await context.resume();const oscillator=context.createOscillator();oscillator.frequency.value=440;
                const gain=context.createGain();gain.gain.value=.15;oscillator.connect(gain).connect(destination);
                const recorder=new MediaRecorder(destination.stream,{mimeType:'audio/webm;codecs=opus'}),chunks=[];
                recorder.ondataavailable=e=>chunks.push(e.data);oscillator.start();recorder.start();
                await new Promise(r=>setTimeout(r,4000));
                const done=new Promise(r=>recorder.onstop=r);recorder.stop();await done;oscillator.stop();
                const bytes=Array.from(new Uint8Array(await new Blob(chunks).arrayBuffer()));await context.close();return bytes;
            }'''))
            assert len(AUDIO)>1000
            page.evaluate('''() => {go2w.store.bags=['voice-a','voice-b','none','pending','broken'].map(name=>({
                name,dur:30,size:42,audio:['voice-a','voice-b','broken'].includes(name)?{
                    url:`/api/recordings/${name}/audio`,duration_s:4,mime:'audio/webm'}:null,
                voice_state:name==='pending'?'pending':'unavailable',voice_reason:'通过HTTP访问，未获得麦克风权限'}));}''')
            page.locator('#bagHistory > summary').click()
            play=page.get_by_role('button',name='播放录音 voice-a',exact=True)
            expect(play).to_be_enabled()
            expect(page.get_by_role('button',name='播放录音 none',exact=True)).to_be_disabled()
            expect(page.get_by_role('button',name='播放录音 pending',exact=True)).to_be_disabled()
            expect(page.locator('#bagList')).to_contain_text('无录音')
            expect(page.locator('#bagList')).to_contain_text('录音待保存')
            assert REQUESTS==[],'展开列表只读元数据，不下载所有录音'
            page.evaluate('''() => {
                document.querySelector('[data-voice="voice-a"]').click();
                document.querySelector('[data-voice="voice-b"]').click();
            }''')
            page.wait_for_function('document.querySelector("#bagVoiceAudio").currentSrc.includes("voice-b") && document.querySelector("#bagVoiceAudio").currentTime>.1')
            expect(page.locator('#bagVoiceMessage')).to_be_empty()
            play.click()
            audio=page.locator('#bagVoiceAudio')
            page.wait_for_function('document.querySelector("#bagVoiceAudio").currentTime>.1')
            assert page.evaluate('document.querySelector("#bagVoiceAudio").readyState')>=2
            page.evaluate('window.originalBagAudio=document.querySelector("#bagVoiceAudio");go2w.store.uploads={"voice-a":{state:"run",phase:"sending",pct:20}}')
            expect(page.get_by_role('button',name='暂停录音 voice-a',exact=True)).to_be_visible()
            page.wait_for_timeout(650)
            assert page.evaluate('document.querySelector("#bagVoiceAudio")===window.originalBagAudio')
            assert not page.evaluate('document.querySelector("#bagVoiceAudio").paused')
            page.get_by_role('button',name='暂停录音 voice-a',exact=True).click()
            assert page.evaluate('document.querySelector("#bagVoiceAudio").paused')
            page.get_by_role('button',name='播放录音 voice-b',exact=True).click()
            page.wait_for_function('document.querySelector("#bagVoiceAudio").currentSrc.includes("voice-b") && !document.querySelector("#bagVoiceAudio").paused')
            assert page.locator('#bagHistory audio').count()==1
            for width in (1440,375):
                page.set_viewport_size({'width':width,'height':900})
                page.locator('#drawer').evaluate('(e)=>e.style.setProperty("--dh","680px")')
                card=page.locator('#recordingControls');card.evaluate('(e)=>e.scrollTop=0')
                page.locator('#bagUploadSelected').scroll_into_view_if_needed()
                assert card.evaluate('(e)=>e.scrollWidth<=e.clientWidth'),width
                card.screenshot(path=str(out/f'voice-{width}.png'))
            page.locator('#bagVoiceClose').click()
            expect(page.locator('#bagVoicePlayback')).not_to_be_visible()
            page.get_by_role('button',name='播放录音 broken',exact=True).click()
            expect(page.locator('#bagVoiceMessage')).to_contain_text('无法播放')
            page.get_by_role('button',name='播放录音 voice-a',exact=True).click()
            page.wait_for_function('!document.querySelector("#bagVoiceAudio").paused')
            page.locator('#bagHistory > summary').click()
            page.wait_for_function('document.querySelector("#bagVoiceAudio").paused')
            assert not errors,errors
            browser.close()
        print('PASS 实际WebM解码播放、暂停/切换、上传刷新不中断、懒加载、无录音/待保存、错误反馈及手机布局')
    finally:
        server.shutdown();server.server_close()

if __name__=='__main__':main()
