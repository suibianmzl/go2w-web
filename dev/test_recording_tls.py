"""假ROS下启动真实HTTPS入口，使用临时受信任证书回读手机页。"""
import os
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.request import urlopen

ROOT=Path(__file__).resolve().parents[1]


class RecordingTLSTest(unittest.TestCase):
    def test_https_serves_recorder_with_a_trusted_certificate(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp)
            cert,key=base/'test.crt',base/'test.key'
            subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes',
                '-keyout',str(key),'-out',str(cert),'-days','1','-subj','/CN=localhost',
                '-addext','subjectAltName=DNS:localhost,IP:127.0.0.1'],
                stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True)
            with socket.socket() as sock:
                sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
            env={**os.environ,'GO2W_FAKE_PROCS':'1','GO2W_DATA':str(base/'data'),
                 'PYTHONPATH':os.pathsep.join([str(ROOT/'dev/fake_ros'),str(ROOT.parent/'go2w/src/navigation/go2w_taught_route')])}
            with (base/'server.log').open('w+') as log:
                server=subprocess.Popen([sys.executable,str(ROOT/'server/go2w_web.py'),'--port',str(port),
                    '--tls-cert',str(cert),'--tls-key',str(key)],env=env,stdout=log,stderr=log)
                try:
                    ctx=ssl.create_default_context(cafile=str(cert))
                    deadline=time.monotonic()+10
                    while True:
                        try:
                            with urlopen(f'https://localhost:{port}/record',context=ctx,timeout=1) as response:
                                self.assertEqual(response.status,200)
                                self.assertIn('保存前20秒和后10秒',response.read().decode())
                            break
                        except OSError:
                            if time.monotonic()>deadline or server.poll() is not None:
                                log.seek(0);self.fail(log.read())
                            time.sleep(.1)
                finally:
                    server.terminate()
                    try:server.wait(timeout=5)
                    except subprocess.TimeoutExpired:server.kill();server.wait(timeout=5)


if __name__=='__main__':
    unittest.main()
