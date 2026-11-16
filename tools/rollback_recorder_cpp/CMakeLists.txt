cmake_minimum_required(VERSION 3.16)
project(rollback_recorder_cpp CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE Release)
endif()

find_package(ament_cmake REQUIRED)
find_package(rclcpp REQUIRED)
find_package(rosbag2_cpp REQUIRED)
find_package(rosbag2_storage REQUIRED)

add_executable(rollback_recorder_cpp rollback_recorder.cpp)
target_compile_options(rollback_recorder_cpp PRIVATE -Wall -Wextra)
ament_target_dependencies(rollback_recorder_cpp rclcpp rosbag2_cpp rosbag2_storage)
# 运行时靠容器里 source 过的 /opt/ros/humble 环境（LD_LIBRARY_PATH）找库
