"""真实 ROS MCAP 写入/读回；在隔离 ROS 容器内运行，不连接机器人。"""
import ctypes
import ctypes.util
import importlib.util
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tools'))
HAS_ROS=importlib.util.find_spec('rosbag2_py') is not None


@unittest.skipUnless(HAS_ROS,'requires real rosbag2_py; run in the isolated ROS image')
class McapCompressionTests(unittest.TestCase):
    def setUp(self):
        import rosbag2_py
        from rclpy.serialization import serialize_message
        from std_msgs.msg import String
        self.ros=rosbag2_py
        self.tmp=tempfile.TemporaryDirectory();self.directory=Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.records=[(1_000_000_000+i*1_000_000,'/payload',serialize_message(
            String(data=f'{i}:'+('lidar sample 0123456789 '*3000)))) for i in range(32)]

    def assert_lossless_compressed(self,path):
        files=list(Path(path).glob('*.mcap'));self.assertEqual(len(files),1)
        library=ctypes.CDLL(ctypes.util.find_library('zstd'))
        library.ZSTD_decompress.argtypes=[ctypes.c_void_p,ctypes.c_size_t,ctypes.c_void_p,ctypes.c_size_t]
        library.ZSTD_decompress.restype=ctypes.c_size_t
        chunks=0;opcodes=set();summary=None
        with files[0].open('rb') as f:
            self.assertEqual(f.read(8),b'\x89MCAP0\r\n')
            while True:
                header=f.read(9)
                if len(header)<9:break
                op,size=struct.unpack('<BQ',header);start=f.tell()
                if start+size>files[0].stat().st_size:break
                opcodes.add(op)
                if op==6:
                    _,_,plain,crc,n=struct.unpack('<QQQII',f.read(32))
                    self.assertEqual(f.read(n),b'zstd')
                    stored=struct.unpack('<Q',f.read(8))[0];data=f.read(stored)
                    output=ctypes.create_string_buffer(plain)
                    self.assertEqual(library.ZSTD_decompress(output,plain,data,stored),plain)
                    self.assertNotEqual(crc,0,'chunk CRC must remain enabled')
                    self.assertEqual(zlib.crc32(output.raw),crc)
                    chunks+=1
                elif op==2:summary=struct.unpack('<QQI',f.read(20))
                f.seek(start+size)
        self.assertGreater(chunks,0)
        self.assertIn(7,opcodes,'message index retained')
        self.assertIn(8,opcodes,'chunk index retained')
        self.assertTrue(summary and summary[0]>0 and summary[1]>0,'summary retained')
        reader=self.ros.SequentialReader()
        reader.open(self.ros.StorageOptions(uri=str(path),storage_id='mcap'),self.ros.ConverterOptions('cdr','cdr'))
        actual=[]
        while reader.has_next():
            topic,data,t=reader.read_next();actual.append((t,topic,data))
        self.assertEqual(actual,self.records,'all bytes and timestamps must survive compression')
        reader.seek(self.records[16][0]);self.assertEqual(reader.read_next()[2],self.records[16][0])
        self.assertLess(files[0].stat().st_size,sum(len(r[2]) for r in self.records)//4)

    def test_python_rollback(self):
        from rollback_recorder_py import write_bag
        path=self.directory/'rollback'
        write_bag(str(path),self.records,{'/payload':'std_msgs/msg/String'},
            self.ros.SequentialWriter,self.ros.StorageOptions,self.ros.ConverterOptions,self.ros.TopicMetadata)
        self.assert_lossless_compressed(path)

    def test_segment_writer(self):
        from segment_recorder import McapWriter,Segments
        path=self.directory/'segments'
        segments=Segments(str(path),'compression',30,McapWriter)
        for t,topic,data in self.records:segments.add(topic,'std_msgs/msg/String',data,t=t)
        segments.stop()
        bags=[p for p in path.iterdir() if not p.name.startswith('.')]
        self.assertEqual(len(bags),1);self.assert_lossless_compressed(bags[0])

    def test_missing_config_fails_instead_of_writing_uncompressed(self):
        import rollback_recorder_py,segment_recorder
        missing=str(self.directory/'missing.yaml')
        with patch.object(rollback_recorder_py,'MCAP_CONFIG',missing):
            with self.assertRaises(RuntimeError):
                rollback_recorder_py.write_bag(str(self.directory/'missing-rollback'),self.records,
                    {'/payload':'std_msgs/msg/String'},self.ros.SequentialWriter,self.ros.StorageOptions,
                    self.ros.ConverterOptions,self.ros.TopicMetadata)
        with patch.object(segment_recorder,'MCAP_CONFIG',missing):
            with self.assertRaises(RuntimeError):segment_recorder.McapWriter(str(self.directory/'missing-segment'))

    def test_cpp_writer_and_self_check(self):
        source=self.directory/'cpp';source.mkdir();build=self.directory/'build'
        (source/'CMakeLists.txt').write_text(f'''cmake_minimum_required(VERSION 3.16)
project(compression_check CXX)
set(CMAKE_CXX_STANDARD 17)
find_package(ament_cmake REQUIRED)
find_package(rclcpp REQUIRED)
find_package(rosbag2_cpp REQUIRED)
find_package(rosbag2_storage REQUIRED)
add_executable(compression_check {ROOT}/dev/mcap_compression_driver.cpp)
ament_target_dependencies(compression_check rclcpp rosbag2_cpp rosbag2_storage)
''')
        subprocess.run(['cmake','-S',str(source),'-B',str(build)],check=True,stdout=subprocess.DEVNULL)
        subprocess.run(['cmake','--build',str(build),'-j2'],check=True,stdout=subprocess.DEVNULL)
        config=build/'mcap_writer.yaml';shutil.copyfile(ROOT/'tools/mcap_writer.yaml',config)
        driver=build/'compression_check'
        subprocess.run([str(driver),'--self-check'],check=True)
        config.rename(config.with_suffix('.missing'))
        try:self.assertNotEqual(subprocess.run([str(driver),'--self-check'],capture_output=True).returncode,0)
        finally:config.with_suffix('.missing').rename(config)
        records=self.directory/'records.bin'
        with records.open('wb') as f:
            for t,_,data in self.records:f.write(struct.pack('<QI',t,len(data))+data)
        path=self.directory/'cpp-bag'
        subprocess.run([str(driver),str(path),str(records)],check=True)
        self.assert_lossless_compressed(path)
        playback=subprocess.run(['ros2','bag','play',str(path),'--rate','100'],
            stdin=subprocess.DEVNULL,capture_output=True,text=True,timeout=20)
        self.assertEqual(playback.returncode,0,playback.stdout+playback.stderr)


if __name__=='__main__':unittest.main()
