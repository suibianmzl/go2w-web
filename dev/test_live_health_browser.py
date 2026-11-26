"""No-navigation health and physical posture in Chrome with a local fake socket."""
import functools
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

from playwright.sync_api import expect, sync_playwright


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self,*_):pass


def main():
    server=ThreadingHTTPServer(('127.0.0.1',0),functools.partial(QuietHandler,directory=str(Path(__file__).resolve().parents[1]/'static')))
    Thread(target=server.serve_forever,daemon=True).start()
    errors=[]
    try:
        with sync_playwright() as p:
            browser=p.chromium.launch(executable_path='/opt/google/chrome/chrome',headless=True)
            page=browser.new_page(viewport={'width':1440,'height':1000})
            page.route('https://fonts.googleapis.com/**',lambda r:r.abort())
            page.route('https://fonts.gstatic.com/**',lambda r:r.abort())
            page.add_init_script('''window.WebSocket = class {
                constructor(){ this.readyState=1;window.healthSocket=this;setTimeout(()=>this.onopen?.({}),0); }
                send(raw){const m=JSON.parse(raw);if(m.t==='cmd'){window.lastHealthCommand=m;this.deliver({t:'ack',id:m.id,ok:true});}}
                deliver(d){this.onmessage?.({data:JSON.stringify(d)});}
                close(){this.readyState=3;this.onclose?.({});}
            };''')
            page.on('pageerror',lambda e:errors.append(str(e)))
            page.goto(f'http://127.0.0.1:{server.server_port}/?debug',wait_until='networkidle')
            health={'sys':{'cpu':74.,'mem':2.,'memTotal':8.},'battery':0,'motorTemp':[32]*16,
                    'robotFeedback':{'fresh':True,'valid':True,'configured':True,'posture':'lying','age':.1}}
            page.evaluate('(h)=>healthSocket.deliver({t:"system",...h})',health)
            expect(page.locator('#chCpu b')).to_have_text('74%')
            expect(page.locator('#sBat')).to_have_text('0%')
            expect(page.locator('#chMem b')).to_have_text('2.0 / 8 G')
            assert page.evaluate('Object.values(go2w.store.profiles).every(x=>x==="stop")')
            state={'t':'state',**health,'profiles':{'mapping':'stop','localization':'stop','navigation':'stop'},'topics':[],
                   'nodes':[],'params':{},'ctrl':{'on':True,'state':'Velocity','ready':True,'seq':3,
                   'op':None,'age':0,'policies':[],'feedback':health['robotFeedback']}}
            page.evaluate('(s)=>healthSocket.deliver(s)',state)
            expect(page.locator('#chPose b')).to_have_text('未确认站起')
            assert not page.evaluate('go2w.actions.walking()')
            assert page.locator('#chMem').evaluate('(el)=>el.getBoundingClientRect().right <= el.parentElement.getBoundingClientRect().right+1'), 'Health chips must not be clipped'
            # Even a successful command acknowledgement cannot replace physical feedback.
            page.locator('#poseUp').click()
            expect(page.locator('#chPose b')).to_have_text('未确认站起')
            page.screenshot(path='/tmp/go2w-health-unconfirmed.png')
            state['robotFeedback']['posture']='standing'
            state['ctrl']['feedback']['posture']='standing'
            page.evaluate('(s)=>healthSocket.deliver(s)',state)
            expect(page.locator('#chPose b')).to_have_text('已站起')
            assert page.evaluate('go2w.actions.walking()')
            # Climb is an ordinary mode choice: available before standing and without a modal or countdown.
            state['ctrl'].update(state='Passive',ready=True,climb=None,
                                 dual={'policy':'rough','switching':False,'req':False})
            state['robotFeedback']['posture']='lying'
            page.evaluate('(s)=>healthSocket.deliver(s)',state)
            expect(page.locator('#climbBtn')).to_be_enabled()
            page.locator('#climbBtn').click()
            assert page.evaluate('lastHealthCommand.op')=='climb'
            assert page.evaluate('lastHealthCommand.on') is True
            expect(page.get_by_role('dialog')).to_have_count(0)
            state['ctrl']['dual']={'policy':'climb','switching':False,'req':True}
            page.evaluate('(s)=>healthSocket.deliver(s)',state)
            expect(page.locator('#climbBtn')).to_have_text('开爬台模式')
            expect(page.locator('#climbTxt')).to_contain_text('外部请求')
            page.locator('#climbBtn').click()
            assert page.evaluate('lastHealthCommand.on') is True
            state['ctrl'].update(state='Velocity',ready=True,climb={'since':10000},
                                 dual={'policy':'climb','switching':False,'req':True})
            state['robotFeedback']['posture']='standing'
            page.evaluate('(s)=>healthSocket.deliver(s)',state)
            expect(page.locator('#climbTxt')).not_to_contain_text('剩')
            state['ctrl']['dual']=None
            page.evaluate('(s)=>healthSocket.deliver(s)',state)
            expect(page.locator('#climbRow')).to_be_visible()
            expect(page.locator('#climbTxt')).to_contain_text('等待运控反馈')
            page.locator('#climbBtn').click()
            assert page.evaluate('lastHealthCommand.on') is False
            state['robotFeedback']['posture']='unknown'
            state['ctrl']['physicalReady']=True
            page.evaluate('(s)=>healthSocket.deliver(s)',state)
            expect(page.locator('#chPose b')).to_have_text('策略运行')
            assert page.evaluate('go2w.actions.walking()')
            page.wait_for_timeout(3500)
            expect(page.locator('#chCpu b')).to_have_text('--')
            expect(page.locator('#sBat')).to_have_text('--')
            assert not page.evaluate('go2w.actions.walking()')
            page.evaluate('(s)=>healthSocket.deliver(s)',state)
            expect(page.locator('#chCpu b')).to_have_text('74%')
            page.set_viewport_size({'width':390,'height':844})
            assert page.locator('#chMem').evaluate('(el)=>el.getBoundingClientRect().right <= innerWidth')
            page.screenshot(path='/tmp/go2w-health-mobile.png')
            page.evaluate('healthSocket.close()')
            expect(page.locator('#chCpu b')).to_have_text('--')
            expect(page.locator('#sBat')).to_have_text('--')
            assert not errors,errors
            browser.close()
            print('PASS 无导航/无地图CPU内存电量、0%电量、FSM虚报及ACK不替代实际站起、实际反馈恢复、过期/断线清理')
    finally:
        server.shutdown();server.server_close()


if __name__=='__main__':main()
