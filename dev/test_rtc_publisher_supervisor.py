import importlib.util
from pathlib import Path


path = Path(__file__).resolve().parents[1] / 'deploy' / 'rtc_publisher_supervisor.py'
spec = importlib.util.spec_from_file_location('rtc_publisher_supervisor', path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

health = {
    'robots': [
        {'robot_id': 'go2w-02', 'online': True, 'video_upload_enabled': True},
        {'robot_id': 'go2w-01', 'online': True, 'video_upload_enabled': True},
        {'robot_id': 'disabled', 'online': True, 'video_upload_enabled': False},
        {'robot_id': 'offline', 'online': False, 'video_upload_enabled': True},
        {'robot_id': 'bad/id', 'online': True, 'video_upload_enabled': True},
    ],
}
targets = module.target_robots(health)
assert [robot['robot_id'] for robot in targets] == ['go2w-01', 'go2w-02']

healthy = {
    'camera_source': True,
    'rtc_publisher': {'videoWidth': 1280, 'readyState': 4},
}
assert module.publisher_healthy(healthy) is True
assert module.publisher_healthy({**healthy, 'camera_source': False}) is False
assert module.publisher_healthy({**healthy, 'rtc_publisher': {'videoWidth': 0}}) is False

url = module.publisher_url(
    'http://127.0.0.1:8091/rtc_publisher.html?x=1',
    'http://127.0.0.1:8080',
    'go2w-02',
)
assert 'robot=go2w-02' in url
assert 'ws=ws%3A%2F%2F127.0.0.1%3A8080%2Fquadruped%2Fcamera%2Fws' in url
assert 'x=1' in url

print('PASS rtc publisher supervisor: all eligible robots, health checks, publisher URL')
