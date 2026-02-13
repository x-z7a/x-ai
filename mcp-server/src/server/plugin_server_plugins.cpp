#include "../plugin_server.hpp"

#include "tool_common.hpp"

#include "mcp_tool.h"

#include "XPLMPlugin.h"
#include "XPLMUtilities.h"

#include <algorithm>

namespace xai_mcp {

void PluginMcpServer::register_plugin_tools() {
    server_->register_tool(
        mcp::tool_builder("xplm_get_self_plugin_info")
            .with_description("Get plugin metadata for this plugin instance.")
            .build(),
        [this](const mcp::json& params, const std::string&) { return this->tool_get_self_plugin_info(params); });

    server_->register_tool(
        mcp::tool_builder("xplm_plugin_get_info")
            .with_description("Get plugin info by id, signature, or path. Defaults to current plugin.")
            .with_number_param("id", "Plugin ID.", false)
            .with_string_param("signature", "Plugin signature.", false)
            .with_string_param("path", "Plugin absolute path.", false)
            .build(),
        [this](const mcp::json& params, const std::string&) { return this->tool_plugin_get_info(params); });

    server_->register_tool(
        mcp::tool_builder("xplm_plugin_find")
            .with_description("Find plugin ID by signature or path.")
            .with_string_param("signature", "Plugin signature.", false)
            .with_string_param("path", "Plugin absolute path.", false)
            .build(),
        [this](const mcp::json& params, const std::string&) { return this->tool_plugin_find(params); });

    server_->register_tool(
        mcp::tool_builder("xplm_plugin_set_enabled")
            .with_description("Enable or disable a plugin by ID.")
            .with_number_param("id", "Plugin ID.")
            .with_boolean_param("enabled", "True to enable, false to disable.")
            .build(),
        [this](const mcp::json& params, const std::string&) { return this->tool_plugin_set_enabled(params); });

    server_->register_tool(
        mcp::tool_builder("xplm_plugin_reload_all")
            .with_description("Reload all plugins.")
            .with_boolean_param("confirm", "Must be true to proceed.")
            .build(),
        [this](const mcp::json& params, const std::string&) { return this->tool_plugin_reload_all(params); });

    server_->register_tool(
        mcp::tool_builder("xplm_list_plugins")
            .with_description("List loaded plugins with optional limit.")
            .with_number_param("limit", "Maximum number of plugins to return.", false)
            .build(),
        [this](const mcp::json& params, const std::string&) { return this->tool_list_plugins(params); });

    server_->register_tool(
        mcp::tool_builder("xplm_feature_get")
            .with_description("Check if an XPLM feature exists and whether it is enabled.")
            .with_string_param("name", "Feature name.")
            .build(),
        [this](const mcp::json& params, const std::string&) { return this->tool_feature_get(params); });

    server_->register_tool(
        mcp::tool_builder("xplm_feature_set")
            .with_description("Enable or disable an XPLM feature for this plugin.")
            .with_string_param("name", "Feature name.")
            .with_boolean_param("enabled", "Desired enabled state.")
            .build(),
        [this](const mcp::json& params, const std::string&) { return this->tool_feature_set(params); });

    server_->register_tool(
        mcp::tool_builder("xplm_command_execute")
            .with_description("Execute command by name. action=once|begin|end.")
            .with_string_param("name", "Command name.")
            .with_string_param("action", "once|begin|end")
            .with_boolean_param("create_if_missing", "Create command if missing.", false)
            .with_string_param("description", "Description used only when creating command.", false)
            .build(),
        [this](const mcp::json& params, const std::string&) { return this->tool_command_execute(params); });

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

}  // namespace xai_mcp
