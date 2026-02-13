#include "../plugin_server.hpp"

#include "tool_common.hpp"

#include "mcp_tool.h"

#include "XPLMUtilities.h"

#include <algorithm>
#include <vector>

namespace {

XPLMDataFileType parse_data_file_type(const std::string& value) {
    if (value == "situation") {
        return xplm_DataFile_Situation;
    }
    if (value == "replay") {
        return xplm_DataFile_ReplayMovie;
    }
    throw mcp::mcp_exception(mcp::error_code::invalid_params, "type must be one of: situation, replay");
}

}  // namespace

namespace xai_mcp {

void PluginMcpServer::register_runtime_tools() {
    server_->register_tool(
        mcp::tool_builder("xplm_get_versions")
            .with_description("Get X-Plane version, XPLM version, and host id.")
            .build(),
        [this](const mcp::json& params, const std::string&) { return this->tool_get_versions(params); });

    server_->register_tool(
        mcp::tool_builder("xplm_get_runtime_info")
            .with_description("Get runtime information like language, cycle, and elapsed time.")
            .build(),
        [this](const mcp::json& params, const std::string&) { return this->tool_get_runtime_info(params); });

    server_->register_tool(
        mcp::tool_builder("xplm_get_system_paths")
            .with_description("Get X-Plane system and preferences paths.")
            .build(),
        [this](const mcp::json& params, const std::string&) { return this->tool_get_system_paths(params); });

    server_->register_tool(
        mcp::tool_builder("xplm_path_get_system")
            .with_description("Get X-Plane system path (XPLMGetSystemPath).")
            .build(),
        [this](const mcp::json& params, const std::string&) { return this->tool_path_get_system(params); });

    server_->register_tool(
        mcp::tool_builder("xplm_path_get_prefs")
            .with_description("Get X-Plane preferences file path (XPLMGetPrefsPath).")
            .build(),
        [this](const mcp::json& params, const std::string&) { return this->tool_path_get_prefs(params); });

    server_->register_tool(
        mcp::tool_builder("xplm_path_get_separator")
            .with_description("Get current directory separator (XPLMGetDirectorySeparator).")
            .build(),
        [this](const mcp::json& params, const std::string&) { return this->tool_path_get_separator(params); });

    server_->register_tool(
        mcp::tool_builder("xplm_path_extract_file_and_path")
            .with_description("Split a full path into directory path and file name (XPLMExtractFileAndPath).")
            .with_string_param("full_path", "Full file path to split.")
            .build(),
        [this](const mcp::json& params, const std::string&) { return this->tool_path_extract_file_and_path(params); });

    server_->register_tool(
        mcp::tool_builder("xplm_directory_list")
            .with_description("List directory contents using XPLM path APIs.")
            .with_string_param("path", "Directory path in current XPLM path mode.")
            .with_number_param("offset", "Start index in directory listing.", false)
            .with_number_param("limit", "Max file entries to return.", false)
            .build(),
        [this](const mcp::json& params, const std::string&) { return this->tool_directory_list(params); });

    server_->register_tool(
        mcp::tool_builder("xplm_datafile_load")
            .with_description("Load an X-Plane data file.")
            .with_string_param("type", "situation|replay")
            .with_string_param("path", "Path relative to X-Plane system directory.")
            .build(),
        [this](const mcp::json& params, const std::string&) { return this->tool_datafile_load(params); });

    server_->register_tool(
        mcp::tool_builder("xplm_datafile_save")
            .with_description("Save an X-Plane data file.")
            .with_string_param("type", "situation|replay")
            .with_string_param("path", "Path relative to X-Plane system directory.")
            .build(),
        [this](const mcp::json& params, const std::string&) { return this->tool_datafile_save(params); });

    server_->register_tool(
        mcp::tool_builder("xplm_debug_string")
            .with_description("Write a line to Log.txt through XPLMDebugString.")
            .with_string_param("message", "Message to write.")
            .build(),
        [this](const mcp::json& params, const std::string&) { return this->tool_debug_string(params); });

    server_->register_tool(
        mcp::tool_builder("xplm_speak_string")
            .with_description("Display/speak a message through XPLMSpeakString.")
            .with_string_param("message", "Message to speak.")
            .build(),
        [this](const mcp::json& params, const std::string&) { return this->tool_speak_string(params); });

    server_->register_tool(
        mcp::tool_builder("xplm_get_virtual_key_description")
            .with_description("Get key description for an XPLM virtual key code.")
            .with_number_param("key", "Virtual key code.")
            .build(),
        [this](const mcp::json& params, const std::string&) { return this->tool_get_virtual_key_description(params); });

    server_->register_tool(
        mcp::tool_builder("xplm_reload_scenery")
            .with_description("Reload scenery.")
            .build(),
        [this](const mcp::json& params, const std::string&) { return this->tool_reload_scenery(params); });

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

mcp::json PluginMcpServer::tool_path_get_system(const mcp::json& raw_params) {
    (void)normalize_params(raw_params);
    return run_on_main_thread([] {
        char system_path[512] = {};
        XPLMGetSystemPath(system_path);
        return text_content({
            {"system_path", system_path}
        });
    });
}

mcp::json PluginMcpServer::tool_path_get_prefs(const mcp::json& raw_params) {
    (void)normalize_params(raw_params);
    return run_on_main_thread([] {
        char prefs_path[512] = {};
        XPLMGetPrefsPath(prefs_path);
        return text_content({
            {"prefs_path", prefs_path}
        });
    });
}

mcp::json PluginMcpServer::tool_path_get_separator(const mcp::json& raw_params) {
    (void)normalize_params(raw_params);
    return run_on_main_thread([] {
        const char* separator = XPLMGetDirectorySeparator();
        return text_content({
            {"directory_separator", separator ? separator : ""}
        });
    });
}

mcp::json PluginMcpServer::tool_path_extract_file_and_path(const mcp::json& raw_params) {
    const mcp::json params = normalize_params(raw_params);
    const std::string full_path = require_string_arg(params, "full_path");

    return run_on_main_thread([full_path] {
        std::vector<char> path_buf(full_path.begin(), full_path.end());
        path_buf.push_back('\0');

        char* file_part = XPLMExtractFileAndPath(path_buf.data());
        const std::string extracted_path = path_buf.data();
        const std::string file_name = file_part ? std::string(file_part) : std::string();

        return text_content({
            {"input_full_path", full_path},
            {"path", extracted_path},
            {"file_name", file_name}
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

}  // namespace xai_mcp
