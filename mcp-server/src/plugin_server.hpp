#ifndef XAI_MCP_PLUGIN_SERVER_HPP
#define XAI_MCP_PLUGIN_SERVER_HPP

#include "mcp_message.h"
#include "mcp_server.h"

#include "XPLMInstance.h"
#include "XPLMProcessing.h"
#include "XPLMScenery.h"

#include <atomic>
#include <functional>
#include <future>
#include <map>
#include <memory>
#include <mutex>
#include <queue>
#include <string>
#include <thread>
#include <vector>

namespace xai_mcp {

inline constexpr const char* kPluginName = "x-ai-mcp";
inline constexpr const char* kPluginSignature = "com.github.x-z7a/x-ai-mcp";
inline constexpr const char* kPluginDescription = "X-Plane MCP bridge exposing XPLM SDK tools.";
inline constexpr const char* kServerVersion = "0.1.0";

void log_line(const std::string& line);

class PluginMcpServer {
public:
    bool start();
    void stop();

private:
    struct MainThreadJob {
        std::function<mcp::json()> fn;
        std::promise<mcp::json> promise;
    };

    static float flight_loop_callback(float, float, int, void* refcon);
#include "server/plugin_server_tool_methods.inc"

    void process_pending_jobs();
    void clear_pending_jobs();
    mcp::json run_on_main_thread(std::function<mcp::json()> fn);
    void refresh_aircraft_state_cache_main_thread();
    mcp::json get_aircraft_state_cache() const;

    std::unique_ptr<mcp::server> server_;
    std::thread::id sim_thread_id_;
    XPLMFlightLoopID flight_loop_id_ = nullptr;

    std::mutex jobs_mutex_;
    std::queue<std::shared_ptr<MainThreadJob>> jobs_;

    std::atomic<bool> running_{false};
    std::atomic<bool> shutting_down_{false};

    mutable std::mutex aircraft_state_mutex_;
    mcp::json aircraft_state_cache_ = mcp::json::object();
    bool aircraft_state_cache_ready_ = false;
    float aircraft_state_update_elapsed_sec_ = 0.0f;

    struct LoadedObject {
        XPLMObjectRef ref = nullptr;
        std::string path;
    };

    struct ManagedInstance {
        XPLMInstanceRef ref = nullptr;
        int object_id = 0;
        std::vector<std::string> datarefs;
        bool auto_shift = false;
    };

    std::map<int, LoadedObject> objects_;
    std::map<int, ManagedInstance> instances_;
    int next_object_id_ = 1;
    int next_instance_id_ = 1;
};

}  // namespace xai_mcp

#endif
