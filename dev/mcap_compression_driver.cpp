// Exercise the production C++ writer without DDS or a robot.
#define main rollback_main
#include "../tools/rollback_recorder_cpp/rollback_recorder.cpp"
#undef main

int main(int argc, char ** argv)
{
  if (argc == 2 && std::string(argv[1]) == "--self-check") {return self_check();}
  if (argc != 3) {return 2;}
  std::ifstream input(argv[2], std::ios::binary);
  std::vector<Item> records;
  uint64_t t;
  uint32_t size;
  while (input.read(reinterpret_cast<char *>(&t), sizeof(t))) {
    if (!input.read(reinterpret_cast<char *>(&size), sizeof(size))) {return 3;}
    auto msg = std::make_shared<rclcpp::SerializedMessage>(size);
    auto & bytes = msg->get_rcl_serialized_message();
    if (!input.read(reinterpret_cast<char *>(bytes.buffer), size)) {return 3;}
    bytes.buffer_length = size;
    records.push_back({static_cast<int64_t>(t), 0, msg});
  }
  write_bag(argv[1], records, {"/payload"}, {"std_msgs/msg/String"});
  return 0;
}
