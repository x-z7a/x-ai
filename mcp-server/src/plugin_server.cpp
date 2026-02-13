#include "plugin_server.hpp"

#include "mcp_tool.h"

#include "XPLMDataAccess.h"
#include "XPLMPlugin.h"
#include "XPLMProcessing.h"
#include "XPLMUtilities.h"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <iomanip>
#include <sstream>
#include <stdexcept>
#include <utility>
#include <vector>

namespace {

int read_env_int(const char* name, int fallback) {
    const char* value = std::getenv(name);
    if (!value || value[0] == '\0') {
        return fallback;
    }

    char* end = nullptr;
    const long parsed = std::strtol(value, &end, 10);
    if (!end || *end != '\0' || parsed <= 0 || parsed > 65535) {
        return fallback;
    }
    return static_cast<int>(parsed);
}

std::string read_env_string(const char* name, const char* fallback) {
    const char* value = std::getenv(name);
    if (!value || value[0] == '\0') {
        return fallback;
    }
    return value;
}

mcp::json text_content(const mcp::json& payload) {
    return mcp::json::array({{
        {"type", "text"},
        {"text", payload.dump(2)}
    }});
}

mcp::json normalize_params(const mcp::json& params) {
    if (params.is_null()) {
        return mcp::json::object();
    }
    if (params.is_object()) {
        return params;
    }
    if (params.is_array() && params.empty()) {
        return mcp::json::object();
    }
    throw mcp::mcp_exception(mcp::error_code::invalid_params, "Tool arguments must be a JSON object.");
}

std::string require_string_arg(const mcp::json& params, const char* key) {
    if (!params.contains(key) || !params[key].is_string()) {
        throw mcp::mcp_exception(mcp::error_code::invalid_params, std::string("Missing string argument: ") + key);
    }
    return params[key].get<std::string>();
}

double require_number_arg(const mcp::json& params, const char* key) {
    if (!params.contains(key) || !params[key].is_number()) {
        throw mcp::mcp_exception(mcp::error_code::invalid_params, std::string("Missing numeric argument: ") + key);
    }
    return params[key].get<double>();
}

int require_int_arg(const mcp::json& params, const char* key) {
    if (!params.contains(key) || !params[key].is_number_integer()) {
        throw mcp::mcp_exception(mcp::error_code::invalid_params, std::string("Missing integer argument: ") + key);
    }
    return params[key].get<int>();
}

bool require_bool_arg(const mcp::json& params, const char* key) {
    if (!params.contains(key) || !params[key].is_boolean()) {
        throw mcp::mcp_exception(mcp::error_code::invalid_params, std::string("Missing boolean argument: ") + key);
    }
    return params[key].get<bool>();
}

int get_int_arg_or_default(const mcp::json& params, const char* key, int fallback) {
    if (!params.contains(key)) {
        return fallback;
    }
    if (!params[key].is_number_integer()) {
        throw mcp::mcp_exception(mcp::error_code::invalid_params, std::string("Argument must be integer: ") + key);
    }
    return params[key].get<int>();
}

std::string get_string_arg_or_default(const mcp::json& params, const char* key, const std::string& fallback) {
    if (!params.contains(key)) {
        return fallback;
    }
    if (!params[key].is_string()) {
        throw mcp::mcp_exception(mcp::error_code::invalid_params, std::string("Argument must be string: ") + key);
    }
    return params[key].get<std::string>();
}

std::string pointer_to_hex(const void* ptr) {
    std::ostringstream oss;
    oss << "0x" << std::hex << std::uppercase << reinterpret_cast<std::uintptr_t>(ptr);
    return oss.str();
}

std::string bytes_to_hex(const std::vector<uint8_t>& bytes) {
    std::ostringstream oss;
    oss << std::hex << std::setfill('0');
    for (const uint8_t value : bytes) {
        oss << std::setw(2) << static_cast<unsigned int>(value);
    }
    return oss.str();
}

std::vector<uint8_t> hex_to_bytes(const std::string& hex) {
    std::string clean = hex;
    if (clean.rfind("0x", 0) == 0 || clean.rfind("0X", 0) == 0) {
        clean = clean.substr(2);
    }
    if (clean.size() % 2 != 0) {
        throw mcp::mcp_exception(mcp::error_code::invalid_params, "hex must have an even number of characters.");
    }

    std::vector<uint8_t> out;
    out.reserve(clean.size() / 2);
    for (size_t i = 0; i < clean.size(); i += 2) {
        const std::string byte_str = clean.substr(i, 2);
        char* end = nullptr;
        const long value = std::strtol(byte_str.c_str(), &end, 16);
        if (!end || *end != '\0' || value < 0 || value > 255) {
            throw mcp::mcp_exception(mcp::error_code::invalid_params, "hex contains invalid characters.");
        }
        out.push_back(static_cast<uint8_t>(value));
    }
    return out;
}

XPLMDataFileType parse_data_file_type(const std::string& value) {
    if (value == "situation") {
        return xplm_DataFile_Situation;
    }
    if (value == "replay") {
        return xplm_DataFile_ReplayMovie;
    }
    throw mcp::mcp_exception(mcp::error_code::invalid_params, "type must be one of: situation, replay");
}

enum class DataRefArrayMode {
    kInt,
    kFloat
};

DataRefArrayMode parse_dataref_array_mode(const mcp::json& params) {
    const std::string mode = get_string_arg_or_default(params, "mode", "int");
    if (mode == "int") {
        return DataRefArrayMode::kInt;
    }
    if (mode == "float") {
        return DataRefArrayMode::kFloat;
    }
    throw mcp::mcp_exception(mcp::error_code::invalid_params, "mode must be int or float.");
}

const char* array_mode_to_string(DataRefArrayMode mode) {
    return mode == DataRefArrayMode::kInt ? "int" : "float";
}

enum class DataRefMode {
    kAuto,
    kInt,
    kFloat,
    kDouble
};

DataRefMode parse_dataref_mode(const mcp::json& params) {
    if (!params.contains("mode")) {
        return DataRefMode::kAuto;
    }
    if (!params["mode"].is_string()) {
        throw mcp::mcp_exception(mcp::error_code::invalid_params, "mode must be a string (auto|int|float|double).");
    }

    const std::string mode = params["mode"].get<std::string>();
    if (mode == "auto") {
        return DataRefMode::kAuto;
    }
    if (mode == "int") {
        return DataRefMode::kInt;
    }
    if (mode == "float") {
        return DataRefMode::kFloat;
    }
    if (mode == "double") {
        return DataRefMode::kDouble;
    }

    throw mcp::mcp_exception(mcp::error_code::invalid_params, "Invalid mode. Expected auto|int|float|double.");
}

const char* mode_to_string(DataRefMode mode) {
    switch (mode) {
        case DataRefMode::kInt:
            return "int";
        case DataRefMode::kFloat:
            return "float";
        case DataRefMode::kDouble:
            return "double";
        default:
            return "auto";
    }
}

DataRefMode resolve_numeric_mode(DataRefMode requested, int type_bits) {
    auto supports = [type_bits](int t) { return (type_bits & t) != 0; };
    if (requested == DataRefMode::kAuto) {
        if (supports(xplmType_Int)) {
            return DataRefMode::kInt;
        }
        if (supports(xplmType_Float)) {
            return DataRefMode::kFloat;
        }
        if (supports(xplmType_Double)) {
            return DataRefMode::kDouble;
        }
        throw mcp::mcp_exception(mcp::error_code::invalid_params, "DataRef is not a numeric scalar type.");
    }

    if (requested == DataRefMode::kInt && supports(xplmType_Int)) {
        return requested;
    }
    if (requested == DataRefMode::kFloat && supports(xplmType_Float)) {
        return requested;
    }
    if (requested == DataRefMode::kDouble && supports(xplmType_Double)) {
        return requested;
    }

    throw mcp::mcp_exception(mcp::error_code::invalid_params, "Requested mode is not supported by this DataRef.");
}

void ensure_array_mode_supported(DataRefArrayMode mode, int type_bits) {
    if (mode == DataRefArrayMode::kInt && (type_bits & xplmType_IntArray) != 0) {
        return;
    }
    if (mode == DataRefArrayMode::kFloat && (type_bits & xplmType_FloatArray) != 0) {
        return;
    }
    throw mcp::mcp_exception(mcp::error_code::invalid_params, "Requested array mode not supported by DataRef.");
}

}  // namespace

namespace xai_mcp {

void log_line(const std::string& line) {
    std::string message = "[x-ai-mcp] " + line + "\n";
    XPLMDebugString(message.c_str());
}

bool PluginMcpServer::start() {
    if (running_.load()) {
        return true;
    }

    shutting_down_.store(false);
    sim_thread_id_ = std::this_thread::get_id();

    XPLMCreateFlightLoop_t loop_params{};
    loop_params.structSize = sizeof(loop_params);
    loop_params.phase = xplm_FlightLoop_Phase_AfterFlightModel;
    loop_params.callbackFunc = &PluginMcpServer::flight_loop_callback;
    loop_params.refcon = this;

    flight_loop_id_ = XPLMCreateFlightLoop(&loop_params);
    if (!flight_loop_id_) {
        log_line("failed to create flight loop callback.");
        return false;
    }

    mcp::server::configuration conf;
    conf.host = read_env_string("XAI_MCP_HOST", "127.0.0.1");
    conf.port = read_env_int("XAI_MCP_PORT", 8765);
    conf.name = "x-ai-xplane-mcp";
    conf.version = kServerVersion;
    conf.threadpool_size = 2;

    server_ = std::make_unique<mcp::server>(conf);
    server_->set_server_info(conf.name, conf.version);
    server_->set_capabilities({
        {"tools", mcp::json::object()}
    });

    register_tools();

    if (!server_->start(false)) {
        server_.reset();
        XPLMDestroyFlightLoop(flight_loop_id_);
        flight_loop_id_ = nullptr;
        log_line("failed to start MCP server.");
        return false;
    }

    running_.store(true);
    log_line("MCP server listening on " + conf.host + ":" + std::to_string(conf.port));
    return true;
}

void PluginMcpServer::stop() {
    if (!running_.load() && !server_) {
        return;
    }

    shutting_down_.store(true);
    run_on_main_thread([this] {
        for (auto& [_, instance] : instances_) {
            if (instance.ref) {
                XPLMDestroyInstance(instance.ref);
                instance.ref = nullptr;
            }
        }
        instances_.clear();

        for (auto& [_, object] : objects_) {
            if (object.ref) {
                XPLMUnloadObject(object.ref);
                object.ref = nullptr;
            }
        }
        objects_.clear();
        return mcp::json::object();
    });

    process_pending_jobs();

    if (server_) {
        server_->stop();
        server_.reset();
    }

    process_pending_jobs();
    clear_pending_jobs();

    if (flight_loop_id_) {
        XPLMDestroyFlightLoop(flight_loop_id_);
        flight_loop_id_ = nullptr;
    }

    running_.store(false);
    log_line("MCP server stopped.");
}

float PluginMcpServer::flight_loop_callback(float, float, int, void* refcon) {
    auto* self = static_cast<PluginMcpServer*>(refcon);
    self->process_pending_jobs();
    return 0.0f;
}

void PluginMcpServer::register_tools() {
    server_->register_tool(
        mcp::tool_builder("xplm.get_versions")
            .with_description("Get X-Plane version, XPLM version, and host id.")
            .build(),
        [this](const mcp::json& params, const std::string&) { return this->tool_get_versions(params); });

    server_->register_tool(
        mcp::tool_builder("xplm.get_runtime_info")
            .with_description("Get runtime information like language, cycle, and elapsed time.")
            .build(),
        [this](const mcp::json& params, const std::string&) { return this->tool_get_runtime_info(params); });

    server_->register_tool(
        mcp::tool_builder("xplm.get_system_paths")
            .with_description("Get X-Plane system and preferences paths.")
            .build(),
        [this](const mcp::json& params, const std::string&) { return this->tool_get_system_paths(params); });

    server_->register_tool(
        mcp::tool_builder("xplm.directory_list")
            .with_description("List directory contents using XPLM path APIs.")
            .with_string_param("path", "Directory path in current XPLM path mode.")
            .with_number_param("offset", "Start index in directory listing.", false)
            .with_number_param("limit", "Max file entries to return.", false)
            .build(),
        [this](const mcp::json& params, const std::string&) { return this->tool_directory_list(params); });

    server_->register_tool(
        mcp::tool_builder("xplm.datafile_load")
            .with_description("Load an X-Plane data file.")
            .with_string_param("type", "situation|replay")
            .with_string_param("path", "Path relative to X-Plane system directory.")
            .build(),
        [this](const mcp::json& params, const std::string&) { return this->tool_datafile_load(params); });

    server_->register_tool(
        mcp::tool_builder("xplm.datafile_save")
            .with_description("Save an X-Plane data file.")
            .with_string_param("type", "situation|replay")
            .with_string_param("path", "Path relative to X-Plane system directory.")
            .build(),
        [this](const mcp::json& params, const std::string&) { return this->tool_datafile_save(params); });

    server_->register_tool(
        mcp::tool_builder("xplm.debug_string")
            .with_description("Write a line to Log.txt through XPLMDebugString.")
            .with_string_param("message", "Message to write.")
            .build(),
        [this](const mcp::json& params, const std::string&) { return this->tool_debug_string(params); });

    server_->register_tool(
        mcp::tool_builder("xplm.speak_string")
            .with_description("Display/speak a message through XPLMSpeakString.")
            .with_string_param("message", "Message to speak.")
            .build(),
        [this](const mcp::json& params, const std::string&) { return this->tool_speak_string(params); });

    server_->register_tool(
        mcp::tool_builder("xplm.get_virtual_key_description")
            .with_description("Get key description for an XPLM virtual key code.")
            .with_number_param("key", "Virtual key code.")
            .build(),
        [this](const mcp::json& params, const std::string&) { return this->tool_get_virtual_key_description(params); });

    server_->register_tool(
        mcp::tool_builder("xplm.reload_scenery")
            .with_description("Reload scenery.")
            .build(),
        [this](const mcp::json& params, const std::string&) { return this->tool_reload_scenery(params); });

    server_->register_tool(
        mcp::tool_builder("xplm.get_self_plugin_info")
            .with_description("Get plugin metadata for this plugin instance.")
            .build(),
        [this](const mcp::json& params, const std::string&) { return this->tool_get_self_plugin_info(params); });

    server_->register_tool(
        mcp::tool_builder("xplm.plugin_get_info")
            .with_description("Get plugin info by id, signature, or path. Defaults to current plugin.")
            .with_number_param("id", "Plugin ID.", false)
            .with_string_param("signature", "Plugin signature.", false)
            .with_string_param("path", "Plugin absolute path.", false)
            .build(),
        [this](const mcp::json& params, const std::string&) { return this->tool_plugin_get_info(params); });

    server_->register_tool(
        mcp::tool_builder("xplm.plugin_find")
            .with_description("Find plugin ID by signature or path.")
            .with_string_param("signature", "Plugin signature.", false)
            .with_string_param("path", "Plugin absolute path.", false)
            .build(),
        [this](const mcp::json& params, const std::string&) { return this->tool_plugin_find(params); });

    server_->register_tool(
        mcp::tool_builder("xplm.plugin_set_enabled")
            .with_description("Enable or disable a plugin by ID.")
            .with_number_param("id", "Plugin ID.")
            .with_boolean_param("enabled", "True to enable, false to disable.")
            .build(),
        [this](const mcp::json& params, const std::string&) { return this->tool_plugin_set_enabled(params); });

    server_->register_tool(
        mcp::tool_builder("xplm.plugin_reload_all")
            .with_description("Reload all plugins.")
            .with_boolean_param("confirm", "Must be true to proceed.")
            .build(),
        [this](const mcp::json& params, const std::string&) { return this->tool_plugin_reload_all(params); });

    server_->register_tool(
        mcp::tool_builder("xplm.list_plugins")
            .with_description("List loaded plugins with optional limit.")
            .with_number_param("limit", "Maximum number of plugins to return.", false)
            .build(),
        [this](const mcp::json& params, const std::string&) { return this->tool_list_plugins(params); });

    server_->register_tool(
        mcp::tool_builder("xplm.feature_get")
            .with_description("Check if an XPLM feature exists and whether it is enabled.")
            .with_string_param("name", "Feature name.")
            .build(),
        [this](const mcp::json& params, const std::string&) { return this->tool_feature_get(params); });

    server_->register_tool(
        mcp::tool_builder("xplm.feature_set")
            .with_description("Enable or disable an XPLM feature for this plugin.")
            .with_string_param("name", "Feature name.")
            .with_boolean_param("enabled", "Desired enabled state.")
            .build(),
        [this](const mcp::json& params, const std::string&) { return this->tool_feature_set(params); });

    server_->register_tool(
        mcp::tool_builder("xplm.command_execute")
            .with_description("Execute command by name. action=once|begin|end.")
            .with_string_param("name", "Command name.")
            .with_string_param("action", "once|begin|end")
            .with_boolean_param("create_if_missing", "Create command if missing.", false)
            .with_string_param("description", "Description used only when creating command.", false)
            .build(),
        [this](const mcp::json& params, const std::string&) { return this->tool_command_execute(params); });

    server_->register_tool(
        mcp::tool_builder("xplm.object_load")
            .with_description("Load OBJ and return managed object id.")
            .with_string_param("path", "Path relative to X-Plane system folder.")
            .build(),
        [this](const mcp::json& params, const std::string&) { return this->tool_object_load(params); });

    server_->register_tool(
        mcp::tool_builder("xplm.object_unload")
            .with_description("Unload managed object by id.")
            .with_number_param("object_id", "Managed object id.")
            .build(),
        [this](const mcp::json& params, const std::string&) { return this->tool_object_unload(params); });

    server_->register_tool(
        mcp::tool_builder("xplm.object_list")
            .with_description("List loaded managed objects.")
            .build(),
        [this](const mcp::json& params, const std::string&) { return this->tool_object_list(params); });

    server_->register_tool(
        mcp::tool_builder("xplm.instance_create")
            .with_description("Create instance from managed object id.")
            .with_number_param("object_id", "Managed object id.")
            .with_array_param("datarefs", "Optional ordered datarefs array.", "string", false)
            .build(),
        [this](const mcp::json& params, const std::string&) { return this->tool_instance_create(params); });

    server_->register_tool(
        mcp::tool_builder("xplm.instance_destroy")
            .with_description("Destroy managed instance by id.")
            .with_number_param("instance_id", "Managed instance id.")
            .build(),
        [this](const mcp::json& params, const std::string&) { return this->tool_instance_destroy(params); });

    server_->register_tool(
        mcp::tool_builder("xplm.instance_set_position")
            .with_description("Set instance position and per-instance data.")
            .with_number_param("instance_id", "Managed instance id.")
            .with_number_param("x", "Local X.")
            .with_number_param("y", "Local Y.")
            .with_number_param("z", "Local Z.")
            .with_number_param("pitch", "Pitch degrees.", false)
            .with_number_param("heading", "Heading degrees.", false)
            .with_number_param("roll", "Roll degrees.", false)
            .with_boolean_param("double_precision", "Use XPLMInstanceSetPositionDouble.", false)
            .with_array_param("data", "Per-instance dataref values.", "number", false)
            .build(),
        [this](const mcp::json& params, const std::string&) { return this->tool_instance_set_position(params); });

    server_->register_tool(
        mcp::tool_builder("xplm.instance_set_auto_shift")
            .with_description("Enable auto-shift for a managed instance.")
            .with_number_param("instance_id", "Managed instance id.")
            .build(),
        [this](const mcp::json& params, const std::string&) { return this->tool_instance_set_auto_shift(params); });

    server_->register_tool(
        mcp::tool_builder("xplm.instance_list")
            .with_description("List managed instances.")
            .build(),
        [this](const mcp::json& params, const std::string&) { return this->tool_instance_list(params); });

    server_->register_tool(
        mcp::tool_builder("xplm.dataref_info")
            .with_description("Get DataRef metadata.")
            .with_string_param("name", "DataRef path.")
            .build(),
        [this](const mcp::json& params, const std::string&) { return this->tool_dataref_info(params); });

    server_->register_tool(
        mcp::tool_builder("xplm.dataref_list")
            .with_description("List DataRefs with pagination.")
            .with_number_param("offset", "Start index.", false)
            .with_number_param("limit", "Maximum number of refs to return.", false)
            .build(),
        [this](const mcp::json& params, const std::string&) { return this->tool_dataref_list(params); });

    server_->register_tool(
        mcp::tool_builder("xplm.dataref_get")
            .with_description("Read a scalar numeric DataRef.")
            .with_string_param("name", "DataRef path.")
            .with_string_param("mode", "auto|int|float|double", false)
            .build(),
        [this](const mcp::json& params, const std::string&) { return this->tool_dataref_get(params); });

    server_->register_tool(
        mcp::tool_builder("xplm.dataref_set")
            .with_description("Write a scalar numeric DataRef.")
            .with_string_param("name", "DataRef path.")
            .with_number_param("value", "Numeric value to write.")
            .with_string_param("mode", "auto|int|float|double", false)
            .build(),
        [this](const mcp::json& params, const std::string&) { return this->tool_dataref_set(params); });

    server_->register_tool(
        mcp::tool_builder("xplm.dataref_get_array")
            .with_description("Read int/float array DataRef.")
            .with_string_param("name", "DataRef path.")
            .with_string_param("mode", "int|float", false)
            .with_number_param("offset", "Array offset.", false)
            .with_number_param("max", "Maximum items to read.", false)
            .build(),
        [this](const mcp::json& params, const std::string&) { return this->tool_dataref_get_array(params); });

    server_->register_tool(
        mcp::tool_builder("xplm.dataref_set_array")
            .with_description("Write int/float array DataRef.")
            .with_string_param("name", "DataRef path.")
            .with_string_param("mode", "int|float", false)
            .with_number_param("offset", "Array offset.", false)
            .with_array_param("values", "Values to write.", "number")
            .build(),
        [this](const mcp::json& params, const std::string&) { return this->tool_dataref_set_array(params); });

    server_->register_tool(
        mcp::tool_builder("xplm.dataref_get_bytes")
            .with_description("Read byte data from a DataRef and return hex.")
            .with_string_param("name", "DataRef path.")
            .with_number_param("offset", "Byte offset.", false)
            .with_number_param("max", "Maximum bytes to read.", false)
            .build(),
        [this](const mcp::json& params, const std::string&) { return this->tool_dataref_get_bytes(params); });

    server_->register_tool(
        mcp::tool_builder("xplm.dataref_set_bytes")
            .with_description("Write byte data to a DataRef from hex string.")
            .with_string_param("name", "DataRef path.")
            .with_string_param("hex", "Byte payload as hex.")
            .with_number_param("offset", "Byte offset.", false)
            .build(),
        [this](const mcp::json& params, const std::string&) { return this->tool_dataref_set_bytes(params); });
}

mcp::json PluginMcpServer::tool_get_versions(const mcp::json& raw_params) {
    (void)normalize_params(raw_params);
    return run_on_main_thread([] {
        int xplane_version = 0;
        int xplm_version = 0;
        XPLMHostApplicationID host_id = xplm_Host_Unknown;
        XPLMGetVersions(&xplane_version, &xplm_version, &host_id);

        return text_content({
            {"xplane_version", xplane_version},
            {"xplm_version", xplm_version},
            {"host_id", host_id}
        });
    });
}

mcp::json PluginMcpServer::tool_get_runtime_info(const mcp::json& raw_params) {
    (void)normalize_params(raw_params);
    return run_on_main_thread([] {
        int xplane_version = 0;
        int xplm_version = 0;
        XPLMHostApplicationID host_id = xplm_Host_Unknown;
        XPLMGetVersions(&xplane_version, &xplm_version, &host_id);

        return text_content({
            {"xplane_version", xplane_version},
            {"xplm_version", xplm_version},
            {"host_id", host_id},
            {"language", XPLMGetLanguage()},
            {"cycle_number", XPLMGetCycleNumber()},
            {"elapsed_time_sec", XPLMGetElapsedTime()}
        });
    });
}

mcp::json PluginMcpServer::tool_get_system_paths(const mcp::json& raw_params) {
    (void)normalize_params(raw_params);
    return run_on_main_thread([] {
        char system_path[512] = {};
        char prefs_path[512] = {};
        XPLMGetSystemPath(system_path);
        XPLMGetPrefsPath(prefs_path);

        return text_content({
            {"system_path", system_path},
            {"prefs_path", prefs_path},
            {"directory_separator", XPLMGetDirectorySeparator()}
        });
    });
}

mcp::json PluginMcpServer::tool_directory_list(const mcp::json& raw_params) {
    const mcp::json params = normalize_params(raw_params);
    const std::string path = require_string_arg(params, "path");
    const int offset = get_int_arg_or_default(params, "offset", 0);
    const int limit = get_int_arg_or_default(params, "limit", 200);
    if (offset < 0) {
        throw mcp::mcp_exception(mcp::error_code::invalid_params, "offset must be >= 0.");
    }
    if (limit <= 0) {
        throw mcp::mcp_exception(mcp::error_code::invalid_params, "limit must be > 0.");
    }

    return run_on_main_thread([path, offset, limit] {
        int first = offset;
        int remaining = limit;
        int total_files = 0;
        mcp::json entries = mcp::json::array();

        while (remaining > 0) {
            const int request_count = std::min(remaining, 512);
            std::vector<char> names_buf(1024 * 1024, 0);
            std::vector<char*> indices(static_cast<size_t>(request_count) + 2, nullptr);

            int total = 0;
            int returned = 0;
            XPLMGetDirectoryContents(
                path.c_str(),
                first,
                names_buf.data(),
                static_cast<int>(names_buf.size()),
                indices.data(),
                static_cast<int>(indices.size()),
                &total,
                &returned);

            total_files = total;
            if (returned <= 0) {
                break;
            }

            for (int i = 0; i < returned; ++i) {
                entries.push_back(indices[static_cast<size_t>(i)] ? indices[static_cast<size_t>(i)] : "");
            }

            first += returned;
            remaining -= returned;
            if (first >= total) {
                break;
            }
        }

        return text_content({
            {"path", path},
            {"offset", offset},
            {"returned", static_cast<int>(entries.size())},
            {"total", total_files},
            {"entries", entries}
        });
    });
}

mcp::json PluginMcpServer::tool_datafile_load(const mcp::json& raw_params) {
    const mcp::json params = normalize_params(raw_params);
    const XPLMDataFileType type = parse_data_file_type(require_string_arg(params, "type"));
    const std::string path = require_string_arg(params, "path");

    return run_on_main_thread([type, path] {
        const int ok = XPLMLoadDataFile(type, path.c_str());
        return text_content({
            {"type", type},
            {"path", path},
            {"success", ok != 0}
        });
    });
}

mcp::json PluginMcpServer::tool_datafile_save(const mcp::json& raw_params) {
    const mcp::json params = normalize_params(raw_params);
    const XPLMDataFileType type = parse_data_file_type(require_string_arg(params, "type"));
    const std::string path = require_string_arg(params, "path");

    return run_on_main_thread([type, path] {
        const int ok = XPLMSaveDataFile(type, path.c_str());
        return text_content({
            {"type", type},
            {"path", path},
            {"success", ok != 0}
        });
    });
}

mcp::json PluginMcpServer::tool_debug_string(const mcp::json& raw_params) {
    const mcp::json params = normalize_params(raw_params);
    const std::string message = require_string_arg(params, "message");

    return run_on_main_thread([message] {
        std::string line = message;
        if (line.empty() || line.back() != '\n') {
            line.push_back('\n');
        }
        XPLMDebugString(line.c_str());
        return text_content({
            {"success", true},
            {"message", message}
        });
    });
}

mcp::json PluginMcpServer::tool_speak_string(const mcp::json& raw_params) {
    const mcp::json params = normalize_params(raw_params);
    const std::string message = require_string_arg(params, "message");

    return run_on_main_thread([message] {
        XPLMSpeakString(message.c_str());
        return text_content({
            {"success", true},
            {"message", message}
        });
    });
}

mcp::json PluginMcpServer::tool_get_virtual_key_description(const mcp::json& raw_params) {
    const mcp::json params = normalize_params(raw_params);
    const int key = require_int_arg(params, "key");

    return run_on_main_thread([key] {
        const char vk = static_cast<char>(key);
        const char* desc = XPLMGetVirtualKeyDescription(vk);
        return text_content({
            {"key", key},
            {"description", desc ? desc : ""}
        });
    });
}

mcp::json PluginMcpServer::tool_reload_scenery(const mcp::json& raw_params) {
    (void)normalize_params(raw_params);
    return run_on_main_thread([] {
        XPLMReloadScenery();
        return text_content({
            {"success", true}
        });
    });
}

mcp::json PluginMcpServer::tool_get_self_plugin_info(const mcp::json& raw_params) {
    (void)normalize_params(raw_params);
    return run_on_main_thread([] {
        char name[256] = {};
        char path[256] = {};
        char signature[256] = {};
        char description[256] = {};

        const XPLMPluginID my_id = XPLMGetMyID();
        XPLMGetPluginInfo(my_id, name, path, signature, description);

        return text_content({
            {"id", my_id},
            {"name", name},
            {"path", path},
            {"signature", signature},
            {"description", description}
        });
    });
}

mcp::json PluginMcpServer::tool_plugin_get_info(const mcp::json& raw_params) {
    const mcp::json params = normalize_params(raw_params);
    return run_on_main_thread([params] {
        XPLMPluginID plugin_id = XPLMGetMyID();

        if (params.contains("id")) {
            plugin_id = require_int_arg(params, "id");
        } else if (params.contains("signature")) {
            plugin_id = XPLMFindPluginBySignature(require_string_arg(params, "signature").c_str());
        } else if (params.contains("path")) {
            plugin_id = XPLMFindPluginByPath(require_string_arg(params, "path").c_str());
        }

        if (plugin_id == XPLM_NO_PLUGIN_ID) {
            throw mcp::mcp_exception(mcp::error_code::invalid_params, "Plugin not found.");
        }

        char name[256] = {};
        char path[256] = {};
        char signature[256] = {};
        char description[256] = {};
        XPLMGetPluginInfo(plugin_id, name, path, signature, description);

        return text_content({
            {"id", plugin_id},
            {"enabled", XPLMIsPluginEnabled(plugin_id) != 0},
            {"name", name},
            {"path", path},
            {"signature", signature},
            {"description", description}
        });
    });
}

mcp::json PluginMcpServer::tool_plugin_find(const mcp::json& raw_params) {
    const mcp::json params = normalize_params(raw_params);
    return run_on_main_thread([params] {
        XPLMPluginID plugin_id = XPLM_NO_PLUGIN_ID;
        std::string by;
        std::string value;

        if (params.contains("signature")) {
            by = "signature";
            value = require_string_arg(params, "signature");
            plugin_id = XPLMFindPluginBySignature(value.c_str());
        } else if (params.contains("path")) {
            by = "path";
            value = require_string_arg(params, "path");
            plugin_id = XPLMFindPluginByPath(value.c_str());
        } else {
            throw mcp::mcp_exception(mcp::error_code::invalid_params, "Provide signature or path.");
        }

        return text_content({
            {"by", by},
            {"value", value},
            {"id", plugin_id},
            {"found", plugin_id != XPLM_NO_PLUGIN_ID}
        });
    });
}

mcp::json PluginMcpServer::tool_plugin_set_enabled(const mcp::json& raw_params) {
    const mcp::json params = normalize_params(raw_params);
    const int plugin_id = require_int_arg(params, "id");
    const bool enabled = require_bool_arg(params, "enabled");

    return run_on_main_thread([plugin_id, enabled] {
        bool success = true;
        if (enabled) {
            success = XPLMEnablePlugin(plugin_id) != 0;
        } else {
            XPLMDisablePlugin(plugin_id);
        }

        return text_content({
            {"id", plugin_id},
            {"requested_enabled", enabled},
            {"success", success},
            {"enabled", XPLMIsPluginEnabled(plugin_id) != 0}
        });
    });
}

mcp::json PluginMcpServer::tool_plugin_reload_all(const mcp::json& raw_params) {
    const mcp::json params = normalize_params(raw_params);
    const bool confirm = require_bool_arg(params, "confirm");
    if (!confirm) {
        throw mcp::mcp_exception(mcp::error_code::invalid_params, "confirm must be true.");
    }

    return run_on_main_thread([] {
        XPLMReloadPlugins();
        return text_content({
            {"success", true}
        });
    });
}

mcp::json PluginMcpServer::tool_list_plugins(const mcp::json& raw_params) {
    const mcp::json params = normalize_params(raw_params);
    int limit = -1;
    if (params.contains("limit")) {
        if (!params["limit"].is_number_integer()) {
            throw mcp::mcp_exception(mcp::error_code::invalid_params, "limit must be an integer.");
        }
        limit = params["limit"].get<int>();
        if (limit <= 0) {
            throw mcp::mcp_exception(mcp::error_code::invalid_params, "limit must be greater than 0.");
        }
    }

    return run_on_main_thread([limit] {
        const int count = XPLMCountPlugins();
        const int count_to_emit = (limit < 0) ? count : std::min(count, limit);

        mcp::json plugins = mcp::json::array();

        for (int i = 0; i < count_to_emit; ++i) {
            char name[256] = {};
            char path[256] = {};
            char signature[256] = {};
            char description[256] = {};

            const XPLMPluginID plugin_id = XPLMGetNthPlugin(i);
                XPLMGetPluginInfo(plugin_id, name, path, signature, description);
                plugins.push_back({
                    {"id", plugin_id},
                    {"enabled", XPLMIsPluginEnabled(plugin_id) != 0},
                    {"name", name},
                    {"path", path},
                    {"signature", signature},
                    {"description", description}
                });
        }

        return text_content({
            {"count", count},
            {"plugins", plugins}
        });
    });
}

mcp::json PluginMcpServer::tool_feature_get(const mcp::json& raw_params) {
    const mcp::json params = normalize_params(raw_params);
    const std::string feature_name = require_string_arg(params, "name");

    return run_on_main_thread([feature_name] {
        const bool has_feature = XPLMHasFeature(feature_name.c_str()) != 0;
        const bool enabled = has_feature ? (XPLMIsFeatureEnabled(feature_name.c_str()) != 0) : false;
        return text_content({
            {"name", feature_name},
            {"has_feature", has_feature},
            {"enabled", enabled}
        });
    });
}

mcp::json PluginMcpServer::tool_feature_set(const mcp::json& raw_params) {
    const mcp::json params = normalize_params(raw_params);
    const std::string feature_name = require_string_arg(params, "name");
    const bool enabled = require_bool_arg(params, "enabled");

    return run_on_main_thread([feature_name, enabled] {
        if (XPLMHasFeature(feature_name.c_str()) == 0) {
            throw mcp::mcp_exception(mcp::error_code::invalid_params, "Unsupported feature: " + feature_name);
        }
        XPLMEnableFeature(feature_name.c_str(), enabled ? 1 : 0);
        return text_content({
            {"name", feature_name},
            {"enabled", XPLMIsFeatureEnabled(feature_name.c_str()) != 0}
        });
    });
}

mcp::json PluginMcpServer::tool_command_execute(const mcp::json& raw_params) {
    const mcp::json params = normalize_params(raw_params);
    const std::string command_name = require_string_arg(params, "name");
    const std::string action = require_string_arg(params, "action");
    const bool create_if_missing = params.contains("create_if_missing") ? require_bool_arg(params, "create_if_missing") : false;
    const std::string create_description = get_string_arg_or_default(params, "description", command_name);

    return run_on_main_thread([command_name, action, create_if_missing, create_description] {
        XPLMCommandRef command_ref = XPLMFindCommand(command_name.c_str());
        bool created = false;
        if (!command_ref && create_if_missing) {
            command_ref = XPLMCreateCommand(command_name.c_str(), create_description.c_str());
            created = command_ref != nullptr;
        }
        if (!command_ref) {
            throw mcp::mcp_exception(mcp::error_code::invalid_params, "Command not found: " + command_name);
        }

        if (action == "once") {
            XPLMCommandOnce(command_ref);
        } else if (action == "begin") {
            XPLMCommandBegin(command_ref);
        } else if (action == "end") {
            XPLMCommandEnd(command_ref);
        } else {
            throw mcp::mcp_exception(mcp::error_code::invalid_params, "action must be once|begin|end");
        }

        return text_content({
            {"name", command_name},
            {"action", action},
            {"created", created},
            {"command_ref", pointer_to_hex(command_ref)}
        });
    });
}

mcp::json PluginMcpServer::tool_object_load(const mcp::json& raw_params) {
    const mcp::json params = normalize_params(raw_params);
    const std::string path = require_string_arg(params, "path");

    return run_on_main_thread([this, path] {
        XPLMObjectRef ref = XPLMLoadObject(path.c_str());
        if (!ref) {
            throw mcp::mcp_exception(mcp::error_code::invalid_params, "Failed to load object: " + path);
        }

        const int object_id = next_object_id_++;
        objects_[object_id] = LoadedObject{ref, path};

        return text_content({
            {"object_id", object_id},
            {"path", path},
            {"object_ref", pointer_to_hex(ref)}
        });
    });
}

mcp::json PluginMcpServer::tool_object_unload(const mcp::json& raw_params) {
    const mcp::json params = normalize_params(raw_params);
    const int object_id = require_int_arg(params, "object_id");

    return run_on_main_thread([this, object_id] {
        auto object_it = objects_.find(object_id);
        if (object_it == objects_.end()) {
            throw mcp::mcp_exception(mcp::error_code::invalid_params, "Unknown object_id.");
        }

        for (const auto& [instance_id, instance] : instances_) {
            if (instance.object_id == object_id) {
                throw mcp::mcp_exception(
                    mcp::error_code::invalid_params,
                    "Object is in use by instance_id=" + std::to_string(instance_id));
            }
        }

        XPLMUnloadObject(object_it->second.ref);
        const std::string path = object_it->second.path;
        objects_.erase(object_it);

        return text_content({
            {"object_id", object_id},
            {"path", path},
            {"success", true}
        });
    });
}

mcp::json PluginMcpServer::tool_object_list(const mcp::json& raw_params) {
    (void)normalize_params(raw_params);
    return run_on_main_thread([this] {
        mcp::json objects = mcp::json::array();
        for (const auto& [object_id, object] : objects_) {
            int ref_count = 0;
            for (const auto& [_, instance] : instances_) {
                if (instance.object_id == object_id) {
                    ++ref_count;
                }
            }
            objects.push_back({
                {"object_id", object_id},
                {"path", object.path},
                {"object_ref", pointer_to_hex(object.ref)},
                {"instance_ref_count", ref_count}
            });
        }

        return text_content({
            {"count", static_cast<int>(objects_.size())},
            {"objects", objects}
        });
    });
}

mcp::json PluginMcpServer::tool_instance_create(const mcp::json& raw_params) {
    const mcp::json params = normalize_params(raw_params);
    const int object_id = require_int_arg(params, "object_id");

    std::vector<std::string> datarefs;
    if (params.contains("datarefs")) {
        if (!params["datarefs"].is_array()) {
            throw mcp::mcp_exception(mcp::error_code::invalid_params, "datarefs must be an array.");
        }
        for (const auto& item : params["datarefs"]) {
            if (!item.is_string()) {
                throw mcp::mcp_exception(mcp::error_code::invalid_params, "datarefs must contain strings.");
            }
            datarefs.push_back(item.get<std::string>());
        }
    }

    return run_on_main_thread([this, object_id, datarefs] {
        auto object_it = objects_.find(object_id);
        if (object_it == objects_.end()) {
            throw mcp::mcp_exception(mcp::error_code::invalid_params, "Unknown object_id.");
        }

        std::vector<const char*> c_datarefs;
        c_datarefs.reserve(datarefs.size() + 1);
        for (const std::string& dataref : datarefs) {
            c_datarefs.push_back(dataref.c_str());
        }
        c_datarefs.push_back(nullptr);

        XPLMInstanceRef instance_ref = XPLMCreateInstance(object_it->second.ref, c_datarefs.data());
        if (!instance_ref) {
            throw mcp::mcp_exception(mcp::error_code::invalid_params, "Failed to create instance.");
        }

        const int instance_id = next_instance_id_++;
        instances_[instance_id] = ManagedInstance{instance_ref, object_id, datarefs, false};

        mcp::json datarefs_json = mcp::json::array();
        for (const auto& dataref : datarefs) {
            datarefs_json.push_back(dataref);
        }

        return text_content({
            {"instance_id", instance_id},
            {"object_id", object_id},
            {"instance_ref", pointer_to_hex(instance_ref)},
            {"datarefs", datarefs_json}
        });
    });
}

mcp::json PluginMcpServer::tool_instance_destroy(const mcp::json& raw_params) {
    const mcp::json params = normalize_params(raw_params);
    const int instance_id = require_int_arg(params, "instance_id");

    return run_on_main_thread([this, instance_id] {
        auto instance_it = instances_.find(instance_id);
        if (instance_it == instances_.end()) {
            throw mcp::mcp_exception(mcp::error_code::invalid_params, "Unknown instance_id.");
        }

        XPLMDestroyInstance(instance_it->second.ref);
        instances_.erase(instance_it);

        return text_content({
            {"instance_id", instance_id},
            {"success", true}
        });
    });
}

mcp::json PluginMcpServer::tool_instance_set_position(const mcp::json& raw_params) {
    const mcp::json params = normalize_params(raw_params);
    const int instance_id = require_int_arg(params, "instance_id");
    const double x = require_number_arg(params, "x");
    const double y = require_number_arg(params, "y");
    const double z = require_number_arg(params, "z");
    const double pitch = params.contains("pitch") ? require_number_arg(params, "pitch") : 0.0;
    const double heading = params.contains("heading") ? require_number_arg(params, "heading") : 0.0;
    const double roll = params.contains("roll") ? require_number_arg(params, "roll") : 0.0;
    const bool double_precision = params.contains("double_precision") ? require_bool_arg(params, "double_precision") : false;

    std::vector<float> data_values;
    if (params.contains("data")) {
        if (!params["data"].is_array()) {
            throw mcp::mcp_exception(mcp::error_code::invalid_params, "data must be an array.");
        }
        data_values.reserve(params["data"].size());
        for (const auto& item : params["data"]) {
            if (!item.is_number()) {
                throw mcp::mcp_exception(mcp::error_code::invalid_params, "data must contain only numeric values.");
            }
            data_values.push_back(static_cast<float>(item.get<double>()));
        }
    }

    return run_on_main_thread([this, instance_id, x, y, z, pitch, heading, roll, double_precision, data_values] {
        auto instance_it = instances_.find(instance_id);
        if (instance_it == instances_.end()) {
            throw mcp::mcp_exception(mcp::error_code::invalid_params, "Unknown instance_id.");
        }
        const ManagedInstance& instance = instance_it->second;

        const size_t expected_data_count = instance.datarefs.size();
        if (expected_data_count != data_values.size()) {
            if (!(expected_data_count == 0 && data_values.empty())) {
                throw mcp::mcp_exception(
                    mcp::error_code::invalid_params,
                    "data size mismatch; expected " + std::to_string(expected_data_count) + " values.");
            }
        }

        float dummy = 0.0f;
        const float* data_ptr = data_values.empty() ? &dummy : data_values.data();

        if (double_precision) {
            XPLMDrawInfoDouble_t pos{};
            pos.structSize = sizeof(pos);
            pos.x = x;
            pos.y = y;
            pos.z = z;
            pos.pitch = pitch;
            pos.heading = heading;
            pos.roll = roll;
            XPLMInstanceSetPositionDouble(instance.ref, &pos, data_ptr);
        } else {
            XPLMDrawInfo_t pos{};
            pos.structSize = sizeof(pos);
            pos.x = static_cast<float>(x);
            pos.y = static_cast<float>(y);
            pos.z = static_cast<float>(z);
            pos.pitch = static_cast<float>(pitch);
            pos.heading = static_cast<float>(heading);
            pos.roll = static_cast<float>(roll);
            XPLMInstanceSetPosition(instance.ref, &pos, data_ptr);
        }

        return text_content({
            {"instance_id", instance_id},
            {"double_precision", double_precision},
            {"x", x},
            {"y", y},
            {"z", z},
            {"pitch", pitch},
            {"heading", heading},
            {"roll", roll},
            {"data_count", static_cast<int>(data_values.size())}
        });
    });
}

mcp::json PluginMcpServer::tool_instance_set_auto_shift(const mcp::json& raw_params) {
    const mcp::json params = normalize_params(raw_params);
    const int instance_id = require_int_arg(params, "instance_id");

    return run_on_main_thread([this, instance_id] {
        auto instance_it = instances_.find(instance_id);
        if (instance_it == instances_.end()) {
            throw mcp::mcp_exception(mcp::error_code::invalid_params, "Unknown instance_id.");
        }

        XPLMInstanceSetAutoShift(instance_it->second.ref);
        instance_it->second.auto_shift = true;

        return text_content({
            {"instance_id", instance_id},
            {"auto_shift", true}
        });
    });
}

mcp::json PluginMcpServer::tool_instance_list(const mcp::json& raw_params) {
    (void)normalize_params(raw_params);
    return run_on_main_thread([this] {
        mcp::json instances = mcp::json::array();
        for (const auto& [instance_id, instance] : instances_) {
            mcp::json datarefs = mcp::json::array();
            for (const auto& dataref : instance.datarefs) {
                datarefs.push_back(dataref);
            }
            instances.push_back({
                {"instance_id", instance_id},
                {"object_id", instance.object_id},
                {"instance_ref", pointer_to_hex(instance.ref)},
                {"auto_shift", instance.auto_shift},
                {"datarefs", datarefs}
            });
        }

        return text_content({
            {"count", static_cast<int>(instances_.size())},
            {"instances", instances}
        });
    });
}

mcp::json PluginMcpServer::tool_dataref_info(const mcp::json& raw_params) {
    const mcp::json params = normalize_params(raw_params);
    const std::string dataref_name = require_string_arg(params, "name");

    return run_on_main_thread([dataref_name] {
        const XPLMDataRef ref = XPLMFindDataRef(dataref_name.c_str());
        if (!ref) {
            throw mcp::mcp_exception(mcp::error_code::invalid_params, "DataRef not found: " + dataref_name);
        }

        XPLMDataRefInfo_t info{};
        info.structSize = sizeof(info);
        XPLMGetDataRefInfo(ref, &info);

        return text_content({
            {"name", dataref_name},
            {"ref", pointer_to_hex(ref)},
            {"good", XPLMIsDataRefGood(ref) != 0},
            {"type_bits", XPLMGetDataRefTypes(ref)},
            {"writable", XPLMCanWriteDataRef(ref) != 0},
            {"owner", info.owner},
            {"canonical_name", info.name ? info.name : ""}
        });
    });
}

mcp::json PluginMcpServer::tool_dataref_list(const mcp::json& raw_params) {
    const mcp::json params = normalize_params(raw_params);
    const int offset = get_int_arg_or_default(params, "offset", 0);
    const int limit = get_int_arg_or_default(params, "limit", 100);
    if (offset < 0) {
        throw mcp::mcp_exception(mcp::error_code::invalid_params, "offset must be >= 0.");
    }
    if (limit <= 0) {
        throw mcp::mcp_exception(mcp::error_code::invalid_params, "limit must be > 0.");
    }

    return run_on_main_thread([offset, limit] {
        const int total = XPLMCountDataRefs();
        if (offset >= total) {
            return text_content({
                {"total", total},
                {"offset", offset},
                {"limit", limit},
                {"datarefs", mcp::json::array()}
            });
        }

        const int count = std::min(limit, total - offset);
        std::vector<XPLMDataRef> refs(static_cast<size_t>(count), nullptr);
        XPLMGetDataRefsByIndex(offset, count, refs.data());

        mcp::json datarefs = mcp::json::array();
        for (const XPLMDataRef ref : refs) {
            XPLMDataRefInfo_t info{};
            info.structSize = sizeof(info);
            XPLMGetDataRefInfo(ref, &info);
            datarefs.push_back({
                {"name", info.name ? info.name : ""},
                {"type_bits", info.type},
                {"writable", info.writable != 0},
                {"owner", info.owner},
                {"ref", pointer_to_hex(ref)}
            });
        }

        return text_content({
            {"total", total},
            {"offset", offset},
            {"limit", limit},
            {"returned", count},
            {"datarefs", datarefs}
        });
    });
}

mcp::json PluginMcpServer::tool_dataref_get(const mcp::json& raw_params) {
    const mcp::json params = normalize_params(raw_params);
    const std::string dataref_name = require_string_arg(params, "name");
    const DataRefMode requested_mode = parse_dataref_mode(params);

    return run_on_main_thread([dataref_name, requested_mode] {
        const XPLMDataRef ref = XPLMFindDataRef(dataref_name.c_str());
        if (!ref) {
            throw mcp::mcp_exception(mcp::error_code::invalid_params, "DataRef not found: " + dataref_name);
        }

        const int type_bits = XPLMGetDataRefTypes(ref);
        const DataRefMode resolved_mode = resolve_numeric_mode(requested_mode, type_bits);

        mcp::json payload = {
            {"name", dataref_name},
            {"mode", mode_to_string(resolved_mode)},
            {"type_bits", type_bits},
            {"writable", XPLMCanWriteDataRef(ref) != 0}
        };

        switch (resolved_mode) {
            case DataRefMode::kInt:
                payload["value"] = XPLMGetDatai(ref);
                break;
            case DataRefMode::kFloat:
                payload["value"] = XPLMGetDataf(ref);
                break;
            case DataRefMode::kDouble:
                payload["value"] = XPLMGetDatad(ref);
                break;
            default:
                throw mcp::mcp_exception(mcp::error_code::internal_error, "Unhandled DataRef mode.");
        }

        return text_content(payload);
    });
}

mcp::json PluginMcpServer::tool_dataref_set(const mcp::json& raw_params) {
    const mcp::json params = normalize_params(raw_params);
    const std::string dataref_name = require_string_arg(params, "name");
    const double input_value = require_number_arg(params, "value");
    const DataRefMode requested_mode = parse_dataref_mode(params);

    return run_on_main_thread([dataref_name, input_value, requested_mode] {
        const XPLMDataRef ref = XPLMFindDataRef(dataref_name.c_str());
        if (!ref) {
            throw mcp::mcp_exception(mcp::error_code::invalid_params, "DataRef not found: " + dataref_name);
        }
        if (!XPLMCanWriteDataRef(ref)) {
            throw mcp::mcp_exception(mcp::error_code::invalid_params, "DataRef is read-only: " + dataref_name);
        }

        const int type_bits = XPLMGetDataRefTypes(ref);
        const DataRefMode resolved_mode = resolve_numeric_mode(requested_mode, type_bits);

        switch (resolved_mode) {
            case DataRefMode::kInt:
                XPLMSetDatai(ref, static_cast<int>(std::llround(input_value)));
                break;
            case DataRefMode::kFloat:
                XPLMSetDataf(ref, static_cast<float>(input_value));
                break;
            case DataRefMode::kDouble:
                XPLMSetDatad(ref, input_value);
                break;
            default:
                throw mcp::mcp_exception(mcp::error_code::internal_error, "Unhandled DataRef mode.");
        }

        mcp::json payload = {
            {"name", dataref_name},
            {"mode", mode_to_string(resolved_mode)},
            {"type_bits", type_bits},
            {"written_value", input_value}
        };

        switch (resolved_mode) {
            case DataRefMode::kInt:
                payload["current_value"] = XPLMGetDatai(ref);
                break;
            case DataRefMode::kFloat:
                payload["current_value"] = XPLMGetDataf(ref);
                break;
            case DataRefMode::kDouble:
                payload["current_value"] = XPLMGetDatad(ref);
                break;
            default:
                break;
        }

        return text_content(payload);
    });
}

mcp::json PluginMcpServer::tool_dataref_get_array(const mcp::json& raw_params) {
    const mcp::json params = normalize_params(raw_params);
    const std::string dataref_name = require_string_arg(params, "name");
    const DataRefArrayMode mode = parse_dataref_array_mode(params);
    const int offset = get_int_arg_or_default(params, "offset", 0);
    const int max_items = get_int_arg_or_default(params, "max", -1);
    if (offset < 0) {
        throw mcp::mcp_exception(mcp::error_code::invalid_params, "offset must be >= 0.");
    }

    return run_on_main_thread([dataref_name, mode, offset, max_items] {
        const XPLMDataRef ref = XPLMFindDataRef(dataref_name.c_str());
        if (!ref) {
            throw mcp::mcp_exception(mcp::error_code::invalid_params, "DataRef not found: " + dataref_name);
        }

        const int type_bits = XPLMGetDataRefTypes(ref);
        ensure_array_mode_supported(mode, type_bits);

        if (mode == DataRefArrayMode::kInt) {
            const int size = XPLMGetDatavi(ref, nullptr, 0, 0);
            const int count = std::max(0, (max_items < 0) ? (size - offset) : max_items);
            std::vector<int> values(static_cast<size_t>(count), 0);
            const int read = XPLMGetDatavi(ref, values.data(), offset, count);
            values.resize(static_cast<size_t>(std::max(0, read)));

            mcp::json out = mcp::json::array();
            for (const int value : values) {
                out.push_back(value);
            }
            return text_content({
                {"name", dataref_name},
                {"mode", array_mode_to_string(mode)},
                {"type_bits", type_bits},
                {"size", size},
                {"offset", offset},
                {"read", read},
                {"values", out}
            });
        }

        const int size = XPLMGetDatavf(ref, nullptr, 0, 0);
        const int count = std::max(0, (max_items < 0) ? (size - offset) : max_items);
        std::vector<float> values(static_cast<size_t>(count), 0.0f);
        const int read = XPLMGetDatavf(ref, values.data(), offset, count);
        values.resize(static_cast<size_t>(std::max(0, read)));

        mcp::json out = mcp::json::array();
        for (const float value : values) {
            out.push_back(value);
        }
        return text_content({
            {"name", dataref_name},
            {"mode", array_mode_to_string(mode)},
            {"type_bits", type_bits},
            {"size", size},
            {"offset", offset},
            {"read", read},
            {"values", out}
        });
    });
}

mcp::json PluginMcpServer::tool_dataref_set_array(const mcp::json& raw_params) {
    const mcp::json params = normalize_params(raw_params);
    const std::string dataref_name = require_string_arg(params, "name");
    const DataRefArrayMode mode = parse_dataref_array_mode(params);
    const int offset = get_int_arg_or_default(params, "offset", 0);
    if (offset < 0) {
        throw mcp::mcp_exception(mcp::error_code::invalid_params, "offset must be >= 0.");
    }
    if (!params.contains("values") || !params["values"].is_array()) {
        throw mcp::mcp_exception(mcp::error_code::invalid_params, "values must be an array.");
    }

    return run_on_main_thread([dataref_name, mode, offset, params] {
        const XPLMDataRef ref = XPLMFindDataRef(dataref_name.c_str());
        if (!ref) {
            throw mcp::mcp_exception(mcp::error_code::invalid_params, "DataRef not found: " + dataref_name);
        }
        if (!XPLMCanWriteDataRef(ref)) {
            throw mcp::mcp_exception(mcp::error_code::invalid_params, "DataRef is read-only: " + dataref_name);
        }

        const int type_bits = XPLMGetDataRefTypes(ref);
        ensure_array_mode_supported(mode, type_bits);

        int size = 0;
        int write_count = 0;
        if (mode == DataRefArrayMode::kInt) {
            size = XPLMGetDatavi(ref, nullptr, 0, 0);
            std::vector<int> values;
            values.reserve(params["values"].size());
            for (const auto& item : params["values"]) {
                if (!item.is_number()) {
                    throw mcp::mcp_exception(mcp::error_code::invalid_params, "all values must be numeric.");
                }
                values.push_back(static_cast<int>(std::llround(item.get<double>())));
            }
            write_count = static_cast<int>(values.size());
            if (!values.empty()) {
                XPLMSetDatavi(ref, values.data(), offset, write_count);
            }
        } else {
            size = XPLMGetDatavf(ref, nullptr, 0, 0);
            std::vector<float> values;
            values.reserve(params["values"].size());
            for (const auto& item : params["values"]) {
                if (!item.is_number()) {
                    throw mcp::mcp_exception(mcp::error_code::invalid_params, "all values must be numeric.");
                }
                values.push_back(static_cast<float>(item.get<double>()));
            }
            write_count = static_cast<int>(values.size());
            if (!values.empty()) {
                XPLMSetDatavf(ref, values.data(), offset, write_count);
            }
        }

        return text_content({
            {"name", dataref_name},
            {"mode", array_mode_to_string(mode)},
            {"type_bits", type_bits},
            {"size", size},
            {"offset", offset},
            {"write_count", write_count}
        });
    });
}

mcp::json PluginMcpServer::tool_dataref_get_bytes(const mcp::json& raw_params) {
    const mcp::json params = normalize_params(raw_params);
    const std::string dataref_name = require_string_arg(params, "name");
    const int offset = get_int_arg_or_default(params, "offset", 0);
    const int max_bytes = get_int_arg_or_default(params, "max", -1);
    if (offset < 0) {
        throw mcp::mcp_exception(mcp::error_code::invalid_params, "offset must be >= 0.");
    }

    return run_on_main_thread([dataref_name, offset, max_bytes] {
        const XPLMDataRef ref = XPLMFindDataRef(dataref_name.c_str());
        if (!ref) {
            throw mcp::mcp_exception(mcp::error_code::invalid_params, "DataRef not found: " + dataref_name);
        }

        const int type_bits = XPLMGetDataRefTypes(ref);
        if ((type_bits & xplmType_Data) == 0) {
            throw mcp::mcp_exception(mcp::error_code::invalid_params, "DataRef does not expose byte data.");
        }

        const int total = XPLMGetDatab(ref, nullptr, 0, 0);
        const int to_read = std::max(0, (max_bytes < 0) ? (total - offset) : max_bytes);
        std::vector<uint8_t> bytes(static_cast<size_t>(to_read), 0);
        const int read = XPLMGetDatab(ref, bytes.data(), offset, to_read);
        bytes.resize(static_cast<size_t>(std::max(0, read)));

        return text_content({
            {"name", dataref_name},
            {"type_bits", type_bits},
            {"offset", offset},
            {"total", total},
            {"read", read},
            {"hex", bytes_to_hex(bytes)}
        });
    });
}

mcp::json PluginMcpServer::tool_dataref_set_bytes(const mcp::json& raw_params) {
    const mcp::json params = normalize_params(raw_params);
    const std::string dataref_name = require_string_arg(params, "name");
    const std::string hex = require_string_arg(params, "hex");
    const int offset = get_int_arg_or_default(params, "offset", 0);
    if (offset < 0) {
        throw mcp::mcp_exception(mcp::error_code::invalid_params, "offset must be >= 0.");
    }
    const std::vector<uint8_t> bytes = hex_to_bytes(hex);

    return run_on_main_thread([dataref_name, offset, bytes] {
        const XPLMDataRef ref = XPLMFindDataRef(dataref_name.c_str());
        if (!ref) {
            throw mcp::mcp_exception(mcp::error_code::invalid_params, "DataRef not found: " + dataref_name);
        }
        if (!XPLMCanWriteDataRef(ref)) {
            throw mcp::mcp_exception(mcp::error_code::invalid_params, "DataRef is read-only: " + dataref_name);
        }

        const int type_bits = XPLMGetDataRefTypes(ref);
        if ((type_bits & xplmType_Data) == 0) {
            throw mcp::mcp_exception(mcp::error_code::invalid_params, "DataRef does not expose byte data.");
        }

        if (!bytes.empty()) {
            XPLMSetDatab(ref, const_cast<uint8_t*>(bytes.data()), offset, static_cast<int>(bytes.size()));
        }

        std::vector<uint8_t> confirm(bytes.size(), 0);
        const int read = bytes.empty() ? 0 : XPLMGetDatab(ref, confirm.data(), offset, static_cast<int>(confirm.size()));
        confirm.resize(static_cast<size_t>(std::max(0, read)));

        return text_content({
            {"name", dataref_name},
            {"type_bits", type_bits},
            {"offset", offset},
            {"written", static_cast<int>(bytes.size())},
            {"confirm_read", read},
            {"confirm_hex", bytes_to_hex(confirm)}
        });
    });
}

void PluginMcpServer::process_pending_jobs() {
    std::queue<std::shared_ptr<MainThreadJob>> local_jobs;
    {
        std::lock_guard<std::mutex> lock(jobs_mutex_);
        std::swap(local_jobs, jobs_);
    }

    while (!local_jobs.empty()) {
        std::shared_ptr<MainThreadJob> job = std::move(local_jobs.front());
        local_jobs.pop();

        try {
            job->promise.set_value(job->fn());
        } catch (...) {
            job->promise.set_exception(std::current_exception());
        }
    }
}

void PluginMcpServer::clear_pending_jobs() {
    std::queue<std::shared_ptr<MainThreadJob>> local_jobs;
    {
        std::lock_guard<std::mutex> lock(jobs_mutex_);
        std::swap(local_jobs, jobs_);
    }

    while (!local_jobs.empty()) {
        std::shared_ptr<MainThreadJob> job = std::move(local_jobs.front());
        local_jobs.pop();
        job->promise.set_exception(std::make_exception_ptr(
            std::runtime_error("Plugin is shutting down.")));
    }
}

mcp::json PluginMcpServer::run_on_main_thread(std::function<mcp::json()> fn) {
    if (std::this_thread::get_id() == sim_thread_id_) {
        return fn();
    }

    if (shutting_down_.load()) {
        throw mcp::mcp_exception(mcp::error_code::internal_error, "Plugin is shutting down.");
    }

    auto job = std::make_shared<MainThreadJob>();
    job->fn = std::move(fn);
    std::future<mcp::json> result = job->promise.get_future();

    {
        std::lock_guard<std::mutex> lock(jobs_mutex_);
        jobs_.push(job);
    }

    if (flight_loop_id_) {
        XPLMScheduleFlightLoop(flight_loop_id_, -1.0f, 1);
    }

    if (result.wait_for(std::chrono::seconds(3)) == std::future_status::timeout) {
        throw mcp::mcp_exception(mcp::error_code::internal_error, "Timed out waiting for X-Plane main thread.");
    }
    return result.get();
}

}  // namespace xai_mcp
