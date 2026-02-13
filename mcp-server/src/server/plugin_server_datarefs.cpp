#include "../plugin_server.hpp"

#include "tool_common.hpp"

#include "mcp_tool.h"

#include "XPLMDataAccess.h"

#include <algorithm>
#include <vector>

namespace xai_mcp {
namespace {

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

void PluginMcpServer::register_dataref_tools() {
    server_->register_tool(
        mcp::tool_builder("xplm_dataref_info")
            .with_description("Get DataRef metadata.")
            .with_string_param("name", "DataRef path.")
            .build(),
        [this](const mcp::json& params, const std::string&) { return this->tool_dataref_info(params); });

    server_->register_tool(
        mcp::tool_builder("xplm_dataref_list")
            .with_description("List DataRefs with pagination.")
            .with_number_param("offset", "Start index.", false)
            .with_number_param("limit", "Maximum number of refs to return.", false)
            .build(),
        [this](const mcp::json& params, const std::string&) { return this->tool_dataref_list(params); });

    server_->register_tool(
        mcp::tool_builder("xplm_dataref_get")
            .with_description("Read a scalar numeric DataRef.")
            .with_string_param("name", "DataRef path.")
            .with_string_param("mode", "auto|int|float|double", false)
            .build(),
        [this](const mcp::json& params, const std::string&) { return this->tool_dataref_get(params); });

    server_->register_tool(
        mcp::tool_builder("xplm_dataref_set")
            .with_description("Write a scalar numeric DataRef.")
            .with_string_param("name", "DataRef path.")
            .with_number_param("value", "Numeric value to write.")
            .with_string_param("mode", "auto|int|float|double", false)
            .build(),
        [this](const mcp::json& params, const std::string&) { return this->tool_dataref_set(params); });

    server_->register_tool(
        mcp::tool_builder("xplm_dataref_get_array")
            .with_description("Read int/float array DataRef.")
            .with_string_param("name", "DataRef path.")
            .with_string_param("mode", "int|float", false)
            .with_number_param("offset", "Array offset.", false)
            .with_number_param("max", "Maximum items to read.", false)
            .build(),
        [this](const mcp::json& params, const std::string&) { return this->tool_dataref_get_array(params); });

    server_->register_tool(
        mcp::tool_builder("xplm_dataref_set_array")
            .with_description("Write int/float array DataRef.")
            .with_string_param("name", "DataRef path.")
            .with_string_param("mode", "int|float", false)
            .with_number_param("offset", "Array offset.", false)
            .with_array_param("values", "Values to write.", "number")
            .build(),
        [this](const mcp::json& params, const std::string&) { return this->tool_dataref_set_array(params); });

    server_->register_tool(
        mcp::tool_builder("xplm_dataref_get_bytes")
            .with_description("Read byte data from a DataRef and return hex.")
            .with_string_param("name", "DataRef path.")
            .with_number_param("offset", "Byte offset.", false)
            .with_number_param("max", "Maximum bytes to read.", false)
            .build(),
        [this](const mcp::json& params, const std::string&) { return this->tool_dataref_get_bytes(params); });

    server_->register_tool(
        mcp::tool_builder("xplm_dataref_set_bytes")
            .with_description("Write byte data to a DataRef from hex string.")
            .with_string_param("name", "DataRef path.")
            .with_string_param("hex", "Byte payload as hex.")
            .with_number_param("offset", "Byte offset.", false)
            .build(),
        [this](const mcp::json& params, const std::string&) { return this->tool_dataref_set_bytes(params); });
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

}  // namespace xai_mcp
