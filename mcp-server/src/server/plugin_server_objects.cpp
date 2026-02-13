#include "../plugin_server.hpp"

#include "tool_common.hpp"

#include "mcp_tool.h"

namespace xai_mcp {

void PluginMcpServer::register_object_tools() {
    server_->register_tool(
        mcp::tool_builder("xplm_object_load")
            .with_description("Load OBJ and return managed object id.")
            .with_string_param("path", "Path relative to X-Plane system folder.")
            .build(),
        [this](const mcp::json& params, const std::string&) { return this->tool_object_load(params); });

    server_->register_tool(
        mcp::tool_builder("xplm_object_unload")
            .with_description("Unload managed object by id.")
            .with_number_param("object_id", "Managed object id.")
            .build(),
        [this](const mcp::json& params, const std::string&) { return this->tool_object_unload(params); });

    server_->register_tool(
        mcp::tool_builder("xplm_object_list")
            .with_description("List loaded managed objects.")
            .build(),
        [this](const mcp::json& params, const std::string&) { return this->tool_object_list(params); });

    server_->register_tool(
        mcp::tool_builder("xplm_instance_create")
            .with_description("Create instance from managed object id.")
            .with_number_param("object_id", "Managed object id.")
            .with_array_param("datarefs", "Optional ordered datarefs array.", "string", false)
            .build(),
        [this](const mcp::json& params, const std::string&) { return this->tool_instance_create(params); });

    server_->register_tool(
        mcp::tool_builder("xplm_instance_destroy")
            .with_description("Destroy managed instance by id.")
            .with_number_param("instance_id", "Managed instance id.")
            .build(),
        [this](const mcp::json& params, const std::string&) { return this->tool_instance_destroy(params); });

    server_->register_tool(
        mcp::tool_builder("xplm_instance_set_position")
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
        mcp::tool_builder("xplm_instance_set_auto_shift")
            .with_description("Enable auto-shift for a managed instance.")
            .with_number_param("instance_id", "Managed instance id.")
            .build(),
        [this](const mcp::json& params, const std::string&) { return this->tool_instance_set_auto_shift(params); });

    server_->register_tool(
        mcp::tool_builder("xplm_instance_list")
            .with_description("List managed instances.")
            .build(),
        [this](const mcp::json& params, const std::string&) { return this->tool_instance_list(params); });

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

}  // namespace xai_mcp
