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
inline constexpr const char* kPluginSignature = "com.x-ai.mcp.xplane";
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

    void register_tools();
    mcp::json tool_get_versions(const mcp::json& raw_params);
    mcp::json tool_get_runtime_info(const mcp::json& raw_params);
    mcp::json tool_get_system_paths(const mcp::json& raw_params);
    mcp::json tool_path_get_system(const mcp::json& raw_params);
    mcp::json tool_path_get_prefs(const mcp::json& raw_params);
    mcp::json tool_path_get_separator(const mcp::json& raw_params);
    mcp::json tool_path_extract_file_and_path(const mcp::json& raw_params);
    mcp::json tool_directory_list(const mcp::json& raw_params);
    mcp::json tool_datafile_load(const mcp::json& raw_params);
    mcp::json tool_datafile_save(const mcp::json& raw_params);
    mcp::json tool_debug_string(const mcp::json& raw_params);
    mcp::json tool_speak_string(const mcp::json& raw_params);
    mcp::json tool_get_virtual_key_description(const mcp::json& raw_params);
    mcp::json tool_reload_scenery(const mcp::json& raw_params);
    mcp::json tool_nav_list(const mcp::json& raw_params);
    mcp::json tool_nav_info(const mcp::json& raw_params);
    mcp::json tool_nav_find(const mcp::json& raw_params);
    mcp::json tool_nav_find_first_of_type(const mcp::json& raw_params);
    mcp::json tool_nav_find_last_of_type(const mcp::json& raw_params);
    mcp::json tool_nav_next(const mcp::json& raw_params);
    mcp::json tool_gps_destination(const mcp::json& raw_params);
    mcp::json tool_fms_status(const mcp::json& raw_params);
    mcp::json tool_fms_entry_get(const mcp::json& raw_params);
    mcp::json tool_fms_entry_set_nav(const mcp::json& raw_params);
    mcp::json tool_fms_entry_set_latlon(const mcp::json& raw_params);
    mcp::json tool_fms_entry_clear(const mcp::json& raw_params);
    mcp::json tool_fms_entry_set_displayed(const mcp::json& raw_params);
    mcp::json tool_fms_entry_set_destination(const mcp::json& raw_params);
    mcp::json tool_get_self_plugin_info(const mcp::json& raw_params);
    mcp::json tool_plugin_get_info(const mcp::json& raw_params);
    mcp::json tool_plugin_find(const mcp::json& raw_params);
    mcp::json tool_plugin_set_enabled(const mcp::json& raw_params);
    mcp::json tool_plugin_reload_all(const mcp::json& raw_params);
    mcp::json tool_list_plugins(const mcp::json& raw_params);
    mcp::json tool_feature_get(const mcp::json& raw_params);
    mcp::json tool_feature_set(const mcp::json& raw_params);
    mcp::json tool_command_execute(const mcp::json& raw_params);
    mcp::json tool_object_load(const mcp::json& raw_params);
    mcp::json tool_object_unload(const mcp::json& raw_params);
    mcp::json tool_object_list(const mcp::json& raw_params);
    mcp::json tool_instance_create(const mcp::json& raw_params);
    mcp::json tool_instance_destroy(const mcp::json& raw_params);
    mcp::json tool_instance_set_position(const mcp::json& raw_params);
    mcp::json tool_instance_set_auto_shift(const mcp::json& raw_params);
    mcp::json tool_instance_list(const mcp::json& raw_params);
    mcp::json tool_dataref_info(const mcp::json& raw_params);
    mcp::json tool_dataref_list(const mcp::json& raw_params);
    mcp::json tool_dataref_get(const mcp::json& raw_params);
    mcp::json tool_dataref_set(const mcp::json& raw_params);
    mcp::json tool_dataref_get_array(const mcp::json& raw_params);
    mcp::json tool_dataref_set_array(const mcp::json& raw_params);
    mcp::json tool_dataref_get_bytes(const mcp::json& raw_params);
    mcp::json tool_dataref_set_bytes(const mcp::json& raw_params);

    void process_pending_jobs();
    void clear_pending_jobs();
    mcp::json run_on_main_thread(std::function<mcp::json()> fn);

    std::unique_ptr<mcp::server> server_;
    std::thread::id sim_thread_id_;
    XPLMFlightLoopID flight_loop_id_ = nullptr;

    std::mutex jobs_mutex_;
    std::queue<std::shared_ptr<MainThreadJob>> jobs_;

    std::atomic<bool> running_{false};
    std::atomic<bool> shutting_down_{false};

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
