// 回溯录制（C++ 版）：把最近 N 秒的 topic 留在内存里循环覆盖，收到请求时才存成 mcap 包。
//
// 和 rollback_recorder_py.py 行为一致，只是用 generic subscription 直接收序列化字节，
// 不走 rclpy 执行器（Python 版每条消息约 1 ms，--all 时 ~900 条/秒就吃满一个核）。
//
// 约定（网页后端依赖，不能改）：后端把「包名」或「包名 秒数」写进 <bags>/.rollback/request，
// 本进程写到 .rollback/<名字> 下，写完整体改名成 <bags>/<名字>（metadata.yaml 出现 = 写完）；
// 失败时写 .rollback/<名字>.error。
//
// 只发一次的 topic（TRANSIENT_LOCAL，例如 /map、/tf_static）另外长期保留，保存时放到包开头，
// 不会被循环缓存挤掉。

#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>

#include <atomic>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <deque>
#include <filesystem>
#include <fstream>
#include <map>
#include <memory>
#include <regex>
#include <set>
#include <sstream>
#include <string>
#include <thread>
#include <unordered_map>
#include <vector>

#include "rclcpp/rclcpp.hpp"
#include "rclcpp/serialized_message.hpp"
#include "rosbag2_cpp/writer.hpp"
#include "rosbag2_cpp/converter_options.hpp"
#include "rosbag2_storage/serialized_bag_message.hpp"
#include "rosbag2_storage/storage_options.hpp"
#include "rosbag2_storage/topic_metadata.hpp"

namespace fs = std::filesystem;
using Msg = std::shared_ptr<rclcpp::SerializedMessage>;

static const std::regex SAFE_NAME("^[A-Za-z0-9_\\-]{1,48}$");
static const std::regex SAFE_TOPIC("^/[A-Za-z_][A-Za-z0-9_]*(/[A-Za-z_][A-Za-z0-9_]*)*$");

static int64_t now_ns()
{
  return std::chrono::duration_cast<std::chrono::nanoseconds>(
    std::chrono::system_clock::now().time_since_epoch()).count();
}

static size_t msg_len(const Msg & m) {return m->get_rcl_serialized_message().buffer_length;}

static bool same_data(const Msg & a, const Msg & b)
{
  const auto & x = a->get_rcl_serialized_message();
  const auto & y = b->get_rcl_serialized_message();
  return x.buffer_length == y.buffer_length &&
         std::memcmp(x.buffer, y.buffer, x.buffer_length) == 0;
}

struct Item
{
  int64_t t;
  uint32_t topic;
  Msg msg;
};

// 最近 seconds 秒的消息；只发一次的 topic 另外长期保留。只在执行器线程里访问，不用加锁。
class Ring
{
public:
  Ring(double seconds, size_t max_bytes)
  : span_ns_(static_cast<int64_t>(seconds * 1e9)), max_bytes_(max_bytes) {}

  void add(int64_t t, uint32_t topic, const Msg & m, bool latched, size_t keep)
  {
    items_.push_back({t, topic, m});
    bytes_ += msg_len(m);
    const int64_t limit = t - span_ns_;
    while (!items_.empty() && items_.front().t < limit) {
      bytes_ -= msg_len(items_.front().msg);
      items_.pop_front();
    }
    while (!items_.empty() && bytes_ > max_bytes_) {   // 安全上限：防止吃光内存把导航挤掉
      bytes_ -= msg_len(items_.front().msg);
      items_.pop_front();
      ++capped;
    }
    if (latched) {
      auto & kept = latched_[topic];
      for (auto it = kept.begin(); it != kept.end(); ++it) {
        if (same_data(it->second, m)) {kept.erase(it); break;}
      }
      kept.emplace_back(t, m);
      while (kept.size() > keep) {kept.pop_front();}
    }
  }

  double span() const
  {
    return items_.size() > 1 ? (items_.back().t - items_.front().t) / 1e9 : 0.0;
  }

  size_t bytes() const {return bytes_;}

  // 按时间排序；seconds < 0 表示整个缓存
  std::vector<Item> snapshot(double seconds, int64_t end_ns = 0) const
  {
    std::vector<Item> out;
    if (items_.empty()) {return out;}
    const int64_t end = end_ns ? end_ns : items_.back().t;
    auto first = items_.begin();
    if (seconds >= 0) {
      const int64_t cut = end - static_cast<int64_t>(seconds * 1e9);
      while (first != items_.end() && first->t < cut) {++first;}
    }
    if (first == items_.end() || first->t > end) {return out;}
    const int64_t start = first->t;
    for (const auto & [topic, kept] : latched_) {
      for (const auto & [t, m] : kept) {
        if (t < start) {out.push_back({start, topic, m});}
      }
    }
    for (auto it = first; it != items_.end() && it->t <= end; ++it) {out.push_back(*it);}
    return out;
  }

  uint64_t capped = 0;   // 因为超过 max_bytes 被提前丢掉的条数（不是按时间过期的）

private:
  int64_t span_ns_;
  size_t max_bytes_;
  size_t bytes_ = 0;
  std::deque<Item> items_;
  std::map<uint32_t, std::deque<std::pair<int64_t, Msg>>> latched_;
};

static void chown_tree(const fs::path & path)
{
  if (geteuid() != 0) {return;}
  std::error_code ec;
  if (::lchown(path.c_str(), 1000, 1000) != 0) {/* 尽力而为 */}
  for (auto it = fs::recursive_directory_iterator(path, ec); !ec && it != fs::recursive_directory_iterator();
    it.increment(ec))
  {
    if (::lchown(it->path().c_str(), 1000, 1000) != 0) {/* 尽力而为 */}
  }
}

static rosbag2_storage::StorageOptions mcap_options(const fs::path & path)
{
  rosbag2_storage::StorageOptions storage;
  storage.uri = path.string();
  storage.storage_id = "mcap";
  storage.storage_config_uri =
    (fs::canonical("/proc/self/exe").parent_path() / "mcap_writer.yaml").string();
  return storage;
}

static void write_bag(
  const fs::path & path, const std::vector<Item> & records,
  const std::vector<std::string> & names, const std::vector<std::string> & types)
{
  const auto storage = mcap_options(path);
  rosbag2_cpp::ConverterOptions conv;
  conv.input_serialization_format = "cdr";
  conv.output_serialization_format = "cdr";

  rosbag2_cpp::Writer writer;   // 析构时写 metadata.yaml
  writer.open(storage, conv);
  std::map<std::string, uint32_t> by_name;
  for (const auto & r : records) {by_name.emplace(names[r.topic], r.topic);}
  for (const auto & [name, id] : by_name) {   // 按名字排序建 topic，和 Python 版一致
    rosbag2_storage::TopicMetadata meta;
    meta.name = name;
    meta.type = types[id];
    meta.serialization_format = "cdr";
    writer.create_topic(meta);
  }
  for (const auto & r : records) {
    auto bag_msg = std::make_shared<rosbag2_storage::SerializedBagMessage>();
    bag_msg->topic_name = names[r.topic];
    bag_msg->time_stamp = r.t;
    // 不深拷贝：借用缓存里的字节，空删除器；r.msg 在 records 里一直活着
    bag_msg->serialized_data = std::shared_ptr<rcutils_uint8_array_t>(
      const_cast<rcutils_uint8_array_t *>(&r.msg->get_rcl_serialized_message()),
      [](rcutils_uint8_array_t *) {});
    writer.write(bag_msg);
  }
}

// 写到临时目录再整体改名，网页列表里不会出现写了一半的包
static bool save(
  const std::string & name, const std::vector<Item> & records,
  const std::vector<std::string> & names, const std::vector<std::string> & types,
  const fs::path & bags, std::string & err)
{
  const fs::path work = bags / ".rollback";
  const fs::path tmp = work / name, fin = bags / name;
  std::error_code ec;
  try {
    if (records.empty()) {throw std::runtime_error("缓存还是空的：topic 还没有数据");}
    if (fs::exists(fin)) {throw std::runtime_error(name + " 已经存在");}
    fs::remove_all(tmp, ec);
    write_bag(tmp, records, names, types);
    chown_tree(tmp);
    fs::rename(tmp, fin);
  } catch (const std::exception & e) {
    fs::remove_all(tmp, ec);
    err = e.what();
    if (err.empty()) {err = "unknown error";}
    std::ofstream(work / (name + ".error")) << err;
    return false;
  }
  return true;
}

struct Args
{
  double seconds = 20.0;
  long max_mb = 1500;
  fs::path bags;
  bool all = false;
  bool self_check = false;
  std::vector<std::string> topics;
};

static int usage(const std::string & why)
{
  std::fprintf(stderr,
    "用法: rollback_recorder_cpp [--seconds S] [--max-mb N] [--bags DIR] [--all] [topic ...]\n"
    "错误: %s\n", why.c_str());
  return 2;
}

// 返回 0 表示成功；否则是退出码
static int parse(int argc, char ** argv, Args & a)
{
  const char * data = std::getenv("GO2W_DATA");
  a.bags = fs::path(data && *data ? data : "/go2w/data") / "bags";
  for (int i = 1; i < argc; ++i) {
    std::string s = argv[i], val;
    bool has_eq = false;
    if (s.rfind("--", 0) == 0) {
      auto eq = s.find('=');
      if (eq != std::string::npos) {val = s.substr(eq + 1); s = s.substr(0, eq); has_eq = true;}
    }
    auto next = [&](std::string & out) {
        if (has_eq) {out = val; return true;}
        if (i + 1 >= argc) {return false;}
        out = argv[++i];
        return true;
      };
    if (s == "--seconds" || s == "--max-mb" || s == "--bags") {
      std::string v;
      if (!next(v)) {return usage(s + " 缺少参数");}
      char * end = nullptr;
      if (s == "--seconds") {
        a.seconds = std::strtod(v.c_str(), &end);
        if (end == v.c_str() || *end) {return usage("--seconds 必须是数字");}
      } else if (s == "--max-mb") {
        a.max_mb = std::strtol(v.c_str(), &end, 10);
        if (end == v.c_str() || *end) {return usage("--max-mb 必须是整数");}
      } else {
        a.bags = v;
      }
    } else if (s == "--all") {
      a.all = true;
    } else if (s == "--self-check") {
      a.self_check = true;
    } else if (s == "--ros-args") {
      break;
    } else if (!s.empty() && s[0] == '-') {
      return usage("未知参数 " + s);
    } else {
      a.topics.push_back(s);
    }
  }
  if (a.self_check) {return 0;}
  if (a.topics.empty() && !a.all) {return usage("要么列出 topic，要么加 --all");}
  if (!std::isfinite(a.seconds) || a.seconds < 1 || a.seconds > 3600) {
    return usage("--seconds 必须在 1–3600 秒之间");
  }
  if (a.max_mb < 1) {return usage("--max-mb 必须是正整数");}
  for (const auto & t : a.topics) {
    if (!std::regex_match(t, SAFE_TOPIC)) {return usage("topic 名字无效");}
  }
  return 0;
}

// 启动前自检（入口脚本调用）：库能加载、mcap 存储插件能写。失败时入口退回 Python 版
static int self_check()
{
  char tmpl[] = "/tmp/rollback_selfcheck_XXXXXX";
  if (!::mkdtemp(tmpl)) {std::perror("mkdtemp"); return 1;}
  const fs::path dir(tmpl);
  std::error_code ec;
  try {
    const auto storage = mcap_options(dir / "bag");
    rosbag2_cpp::ConverterOptions conv{"cdr", "cdr"};
    {
      rosbag2_cpp::Writer writer;
      writer.open(storage, conv);
      rosbag2_storage::TopicMetadata meta;
      meta.name = "/self_check";
      meta.type = "std_msgs/msg/String";
      meta.serialization_format = "cdr";
      writer.create_topic(meta);
    }
    const bool ok = fs::exists(dir / "bag" / "metadata.yaml");
    fs::remove_all(dir, ec);
    if (!ok) {std::fprintf(stderr, "self-check: 没有生成 metadata.yaml\n"); return 1;}
  } catch (const std::exception & e) {
    fs::remove_all(dir, ec);
    std::fprintf(stderr, "self-check: %s\n", e.what());
    return 1;
  }
  std::puts("ok");
  return 0;
}

class Recorder
{
public:
  Recorder(rclcpp::Node::SharedPtr node, const Args & a)
  : node_(std::move(node)), args_(a),
    ring_(a.seconds, static_cast<size_t>(a.max_mb) * 1048576),
    work_(a.bags / ".rollback"), request_(work_ / "request")
  {
    fs::create_directories(work_);
    chown_tree(work_);
    std::error_code ec;
    fs::remove(request_, ec);
    timers_.push_back(node_->create_wall_timer(std::chrono::seconds(2), [this] {discover();}));
    timers_.push_back(node_->create_wall_timer(std::chrono::milliseconds(200), [this] {poll();}));
    timers_.push_back(node_->create_wall_timer(std::chrono::seconds(30), [this] {report();}));
    discover();
  }

  ~Recorder() {join();}

  void join()
  {
    if (saver_.joinable()) {saver_.join();}
  }

private:
  rclcpp::Logger log() const {return node_->get_logger();}

  // 要录的 topic：命令行列出的，加上 --all 时当前能看到的所有 topic（每次 discover 重查）
  std::vector<std::string> wanted()
  {
    std::vector<std::string> out = args_.topics;
    if (args_.all) {
      std::set<std::string> listed(out.begin(), out.end());
      for (const auto & kv : node_->get_topic_names_and_types()) {   // std::map，已按名字排序
        if (!listed.count(kv.first)) {out.push_back(kv.first);}
      }
    }
    return out;
  }

  void discover()
  {
    for (const auto & topic : wanted()) {
      if (subs_.count(topic) || failed_.count(topic)) {continue;}
      auto pubs = node_->get_publishers_info_by_topic(topic);
      if (pubs.empty()) {continue;}
      const std::string type = pubs[0].topic_type();
      bool latched = false, reliable = true;
      for (const auto & p : pubs) {
        const auto & q = p.qos_profile().get_rmw_qos_profile();
        latched = latched || q.durability == RMW_QOS_POLICY_DURABILITY_TRANSIENT_LOCAL;
        reliable = reliable && q.reliability == RMW_QOS_POLICY_RELIABILITY_RELIABLE;
      }
      rclcpp::QoS qos(latched ? 1 : 50);
      if (reliable) {qos.reliable();} else {qos.best_effort();}
      if (latched) {qos.transient_local();} else {qos.durability_volatile();}

      const uint32_t id = static_cast<uint32_t>(names_.size());
      const size_t keep = topic == "/tf_static" ? 8 : 1;   // 可能有多个发布者，各留一份
      try {
        auto sub = node_->create_generic_subscription(
          topic, type, qos,
          [this, id, latched, keep](std::shared_ptr<rclcpp::SerializedMessage> m) {
            ring_.add(now_ns(), id, m, latched, keep);
          });
        names_.push_back(topic);
        types_.push_back(type);
        subs_.emplace(topic, sub);
      } catch (const std::exception & e) {
        failed_.insert(topic);
        RCLCPP_ERROR(log(), "%s：找不到消息类型 %s（%s），跳过", topic.c_str(), type.c_str(), e.what());
        continue;
      }
      RCLCPP_INFO(log(), "回溯：%s（%s%s）", topic.c_str(), type.c_str(), latched ? "，只发一次" : "");
    }
  }

  void poll()
  {
    if (saving_.load() || !fs::exists(request_)) {return;}
    std::vector<std::string> parts;
    {
      std::ifstream f(request_);
      if (!f) {return;}
      std::string w;
      while (f >> w) {parts.push_back(w);}
    }
    std::error_code ec;
    fs::remove(request_, ec);
    if (ec) {return;}
    // 请求内容：「包名」或「包名 秒数」；不写秒数就存整个缓存
    const std::string name = parts.empty() ? "" : parts[0];
    double seconds = -1;
    int64_t end_ns = 0;
    if (parts.size() > 1) {
      char * end = nullptr;
      const double v = std::strtod(parts[1].c_str(), &end);
      if (end != parts[1].c_str() && !*end && std::isfinite(v)) {seconds = v;}
    }
    if (!std::regex_match(name, SAFE_NAME)) {
      RCLCPP_ERROR(log(), "包名不对：'%s'", name.c_str());
      return;
    }
    if (parts.size() > 2) {
      char * end = nullptr;
      const auto v = std::strtoll(parts[2].c_str(), &end, 10);
      if (end != parts[2].c_str() && !*end && v > 0) {end_ns = v;}
    }
    if (seconds >= 0 && seconds > ring_.span() + 1) {
      RCLCPP_WARN(log(), "%s：要 %.0f 秒，缓存里只有 %.1f 秒", name.c_str(), seconds, ring_.span());
    }
    join();   // 上一次的线程已经结束（saving_ 已清），回收掉
    saving_.store(true);
    // snapshot 在收消息的线程里做，不用加锁；写文件放到别的线程，缓存继续收
    auto records = std::make_shared<std::vector<Item>>(ring_.snapshot(seconds, end_ns));
    auto names = names_;
    auto types = types_;
    const fs::path bags = args_.bags;
    saver_ = std::thread([this, name, records, names, types, bags] {
          const auto t0 = std::chrono::steady_clock::now();
          std::string err;
          const bool ok = save(name, *records, names, types, bags, err);
          const double dt = std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count();
          if (ok) {
            RCLCPP_INFO(log(), "已保存 %s：%zu 条，用时 %.1f s", name.c_str(), records->size(), dt);
          } else {
            RCLCPP_ERROR(log(), "保存 %s 失败：%s", name.c_str(), err.c_str());
          }
          saving_.store(false);
        });
  }

  // 每 30 秒报一次缓存大小和实际覆盖时长；被安全上限截短时报错
  void report()
  {
    char buf[256];
    std::snprintf(buf, sizeof(buf), "回溯缓存 %.0f MB，覆盖 %.1f s（设定 %.0f s，上限 %ld MB）",
      ring_.bytes() / 1048576.0, ring_.span(), args_.seconds, args_.max_mb);
    if (ring_.capped > last_capped_) {
      RCLCPP_ERROR(log(), "%s：超过上限，丢了 %lu 条，回溯不满设定时长", buf,
        static_cast<unsigned long>(ring_.capped - last_capped_));
      last_capped_ = ring_.capped;
    } else {
      RCLCPP_INFO(log(), "%s", buf);
    }
  }

  rclcpp::Node::SharedPtr node_;
  Args args_;
  Ring ring_;
  fs::path work_, request_;
  std::vector<std::string> names_, types_;   // 下标 = topic id
  std::unordered_map<std::string, rclcpp::GenericSubscription::SharedPtr> subs_;
  std::set<std::string> failed_;
  std::vector<rclcpp::TimerBase::SharedPtr> timers_;
  std::atomic<bool> saving_{false};
  std::thread saver_;
  uint64_t last_capped_ = 0;
};

int main(int argc, char ** argv)
{
  Args args;
  if (int rc = parse(argc, argv, args)) {return rc;}
  if (args.self_check) {return self_check();}

  rclcpp::init(argc, argv);
  auto node = std::make_shared<rclcpp::Node>("go2w_rollback_recorder");
  int rc = 0;
  {
    std::unique_ptr<Recorder> rec;
    try {
      rec = std::make_unique<Recorder>(node, args);
      rclcpp::spin(node);   // SIGINT → 返回
    } catch (const std::exception & e) {
      RCLCPP_FATAL(node->get_logger(), "%s", e.what());
      rc = 1;
    }
    // 正常 SIGINT 也要等正在写的包写完 metadata 再退出
    if (rec) {rec->join();}
  }
  node.reset();
  if (rclcpp::ok()) {rclcpp::shutdown();}
  return rc;
}
