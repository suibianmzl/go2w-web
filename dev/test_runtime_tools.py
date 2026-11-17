"""Standalone Web helper entry points and save-map ROS invocation contracts.

Run with Python 3.10+ and numpy: python3 dev/test_runtime_tools.py
No ROS installation or robot connection is required.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


TOOLS = Path(__file__).resolve().parents[1] / 'tools'


class RuntimeToolsTest(unittest.TestCase):
    def test_recorder_and_alignment_help_load_their_dependencies(self):
        for name in ('segment_recorder.py', 'align_map.py'):
            with self.subTest(tool=name):
                result = subprocess.run([sys.executable, str(TOOLS / name), '--help'],
                                        capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('usage:', result.stdout)

    def test_save_map_rejects_invalid_names_before_starting_ros(self):
        for name in ('../outside', 'map;command', 'map\n', ''):
            with self.subTest(name=name):
                result = subprocess.run(['bash', str(TOOLS / 'save_map.sh'), name],
                                        capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn('用法', result.stderr)

    def test_save_map_passes_data_path_and_inherits_ros_environment(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            executable = directory / 'ros2'
            capture = directory / 'calls.jsonl'
            executable.write_text(
                '#!' + sys.executable + '\n'
                'import json, os, pathlib, sys\n'
                'with open(os.environ["TEST_ROS_CAPTURE"], "a") as out:\n'
                ' out.write(json.dumps({"argv":sys.argv[1:], '
                '"ros":os.environ["AMENT_PREFIX_PATH"]})+"\\n")\n'
                'if "-f" in sys.argv:\n'
                ' path=pathlib.Path(sys.argv[sys.argv.index("-f")+1])\n'
                ' path.with_suffix(".yaml").write_text("map")\n'
                ' path.with_suffix(".pgm").write_text("image")\n')
            executable.chmod(0o755)
            data = directory / 'data with spaces'
            environment = dict(os.environ, GO2W_DATA=str(data),
                               PATH=str(directory) + os.pathsep + os.environ['PATH'],
                               AMENT_PREFIX_PATH='/opt/ros/humble',
                               TEST_ROS_CAPTURE=str(capture))
            result = subprocess.run(['bash', str(TOOLS / 'save_map.sh'), 'field'],
                                    env=environment, capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            calls = [json.loads(line) for line in capture.read_text().splitlines()]
            target = str(data / 'maps/field')
            self.assertEqual(calls, [
                {'argv': ['run', 'nav2_map_server', 'map_saver_cli', '-f', target,
                          '--free', '0.196', '--occ', '0.65', '--ros-args',
                          '-p', 'save_map_timeout:=10.0'], 'ros': '/opt/ros/humble'},
                {'argv': ['service', 'call', '/slam_toolbox/serialize_map',
                          'slam_toolbox/srv/SerializePoseGraph',
                          json.dumps({'filename': target})], 'ros': '/opt/ros/humble'}])


if __name__ == '__main__':
    unittest.main()
