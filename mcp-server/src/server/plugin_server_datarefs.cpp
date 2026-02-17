#include "../plugin_server.hpp"

#include "tool_common.hpp"

#include "mcp_tool.h"

#include "XPLMDataAccess.h"

#include <algorithm>
#include <cmath>
#include <vector>

namespace xai_mcp {
namespace {

enum class DataRefArrayMode {
    kInt,
    kFloat
};

enum class DataRefScalarMode {
    kAuto,
    kInt,
    kFloat,
    kDouble
};

bool supports_type(int type_bits, int type) {
    return (type_bits & type) != 0;
}

bool has_numeric_scalar_type(int type_bits) {
    return supports_type(type_bits, xplmType_Int) || supports_type(type_bits, xplmType_Float) ||
           supports_type(type_bits, xplmType_Double);
}

DataRefScalarMode parse_dataref_scalar_mode(const mcp::json& params) {
    if (!params.contains("mode")) {
        return DataRefScalarMode::kAuto;
    }
    if (!params["mode"].is_string()) {
        throw mcp::mcp_exception(mcp::error_code::invalid_params, "mode must be a string (auto|int|float|double).");
    }

    const std::string mode = params["mode"].get<std::string>();
    if (mode == "auto") {
        return DataRefScalarMode::kAuto;
    }
    if (mode == "int") {
        return DataRefScalarMode::kInt;
    }
    if (mode == "float") {
        return DataRefScalarMode::kFloat;
    }
    if (mode == "double") {
        return DataRefScalarMode::kDouble;
    }

    throw mcp::mcp_exception(mcp::error_code::invalid_params, "Invalid mode. Expected auto|int|float|double.");
}

const char* scalar_mode_to_string(DataRefScalarMode mode) {
    switch (mode) {
        case DataRefScalarMode::kInt:
            return "int";
        case DataRefScalarMode::kFloat:
            return "float";
        case DataRefScalarMode::kDouble:
            return "double";
        default:
            return "auto";
    }
}

const char* array_mode_to_string(DataRefArrayMode mode) {
    return mode == DataRefArrayMode::kInt ? "int" : "float";
}

const char* array_value_type_to_string(DataRefArrayMode mode) {
    return mode == DataRefArrayMode::kInt ? "int_array" : "float_array";
}

DataRefScalarMode resolve_scalar_mode_for_get(DataRefScalarMode requested, int type_bits) {
    if (requested == DataRefScalarMode::kAuto) {
        if (supports_type(type_bits, xplmType_Int)) {
            return DataRefScalarMode::kInt;
        }
        if (supports_type(type_bits, xplmType_Float)) {
            return DataRefScalarMode::kFloat;
        }
        if (supports_type(type_bits, xplmType_Double)) {
            return DataRefScalarMode::kDouble;
        }
        throw mcp::mcp_exception(mcp::error_code::invalid_params, "DataRef is not a numeric scalar type.");
    }

    if (requested == DataRefScalarMode::kInt && supports_type(type_bits, xplmType_Int)) {
        return requested;
    }
    if (requested == DataRefScalarMode::kFloat && supports_type(type_bits, xplmType_Float)) {
        return requested;
    }
    if (requested == DataRefScalarMode::kDouble && supports_type(type_bits, xplmType_Double)) {
        return requested;
    }

    throw mcp::mcp_exception(mcp::error_code::invalid_params, "Requested mode is not supported by this DataRef.");
}

bool is_integral_number(double value) {
    if (!std::isfinite(value)) {
        return false;
    }
    return std::fabs(value - std::round(value)) < 1e-9;
}

DataRefScalarMode resolve_scalar_mode_for_set(DataRefScalarMode requested, int type_bits, double input_value) {
    if (requested != DataRefScalarMode::kAuto) {
        return resolve_scalar_mode_for_get(requested, type_bits);
    }

    if (!has_numeric_scalar_type(type_bits)) {
        throw mcp::mcp_exception(mcp::error_code::invalid_params, "DataRef does not expose a numeric scalar value.");
    }

    if (is_integral_number(input_value) && supports_type(type_bits, xplmType_Int)) {
        return DataRefScalarMode::kInt;
    }
    if (supports_type(type_bits, xplmType_Double)) {
        return DataRefScalarMode::kDouble;
    }
    if (supports_type(type_bits, xplmType_Float)) {
        return DataRefScalarMode::kFloat;
    }
    return DataRefScalarMode::kInt;
}

DataRefArrayMode resolve_array_mode_for_get(int type_bits) {
    if (supports_type(type_bits, xplmType_IntArray)) {
        return DataRefArrayMode::kInt;
    }
    if (supports_type(type_bits, xplmType_FloatArray)) {
        return DataRefArrayMode::kFloat;
    }
    throw mcp::mcp_exception(mcp::error_code::invalid_params, "DataRef is not an int/float array type.");
}

DataRefArrayMode resolve_array_mode_for_set(int type_bits, const mcp::json& values) {
    const bool has_int_array = supports_type(type_bits, xplmType_IntArray);
    const bool has_float_array = supports_type(type_bits, xplmType_FloatArray);

    if (!has_int_array && !has_float_array) {
        throw mcp::mcp_exception(mcp::error_code::invalid_params, "DataRef does not expose an int/float array value.");
    }
    if (has_int_array && !has_float_array) {
        return DataRefArrayMode::kInt;
    }
    if (!has_int_array && has_float_array) {
        return DataRefArrayMode::kFloat;
    }

    bool all_integral = true;
    for (const auto& item : values) {
        if (!item.is_number()) {
            throw mcp::mcp_exception(mcp::error_code::invalid_params, "all array values must be numeric.");
        }
        if (!is_integral_number(item.get<double>())) {
            all_integral = false;
        }
    }
    return all_integral ? DataRefArrayMode::kInt : DataRefArrayMode::kFloat;
}

mcp::json require_value_arg(const mcp::json& params) {
    if (params.contains("value")) {
        return params["value"];
    }
    // Backward-compatible aliases from the old split tools.
    if (params.contains("values")) {
        return params["values"];
    }
    if (params.contains("hex")) {
        return params["hex"];
    }
    throw mcp::mcp_exception(
        mcp::error_code::invalid_params,
        "Missing argument: value (number | array<number> | hex string).");
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
            .with_description("Read a DataRef. The server auto-resolves scalar/array/bytes and returns a JSON value.")
            .with_string_param("name", "DataRef path.")
            .with_string_param("mode", "Optional scalar override: auto|int|float|double.", false)
            .with_number_param("offset", "Optional offset for array/bytes reads.", false)
            .with_number_param("max", "Optional maximum item/byte count for array/bytes reads.", false)
            .build(),
        [this](const mcp::json& params, const std::string&) { return this->tool_dataref_get(params); });

    {
        auto set_tool = mcp::tool_builder("xplm_dataref_set")
            .with_description(
                "Write a DataRef. Provide `value` as number (scalar), array<number> (array), or hex string (bytes).")
            .with_string_param("name", "DataRef path.")
            .with_string_param("mode", "Optional scalar override: auto|int|float|double.", false)
            .with_number_param("offset", "Optional offset for array/bytes writes.", false)
            .build();
        set_tool.parameters_schema["properties"]["value"] = {
            {"description", "Value to write: number for scalar datarefs, array of numbers for array datarefs, or hex string for byte datarefs."},
            {"anyOf", mcp::json::array({
                {{"type", "number"}},
                {{"type", "array"}, {"items", {{"type", "number"}}}},
                {{"type", "string"}}
            })}
        };
        set_tool.parameters_schema["required"].push_back("value");
        server_->register_tool(
            std::move(set_tool),
            [this](const mcp::json& params, const std::string&) { return this->tool_dataref_set(params); });
    }
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
    const DataRefScalarMode requested_mode = parse_dataref_scalar_mode(params);
    const int offset = get_int_arg_or_default(params, "offset", 0);
    const int max_items = get_int_arg_or_default(params, "max", -1);
    if (offset < 0) {
        throw mcp::mcp_exception(mcp::error_code::invalid_params, "offset must be >= 0.");
    }

    return run_on_main_thread([dataref_name, requested_mode, offset, max_items] {
        const XPLMDataRef ref = XPLMFindDataRef(dataref_name.c_str());
        if (!ref) {
            throw mcp::mcp_exception(mcp::error_code::invalid_params, "DataRef not found: " + dataref_name);
        }

        const int type_bits = XPLMGetDataRefTypes(ref);

        if (has_numeric_scalar_type(type_bits)) {
            const DataRefScalarMode resolved_mode = resolve_scalar_mode_for_get(requested_mode, type_bits);

            mcp::json payload = {
                {"name", dataref_name},
                {"kind", "scalar"},
                {"value_type", scalar_mode_to_string(resolved_mode)},
                {"type_bits", type_bits},
                {"writable", XPLMCanWriteDataRef(ref) != 0}
            };

            switch (resolved_mode) {
                case DataRefScalarMode::kInt:
                    payload["value"] = XPLMGetDatai(ref);
                    break;
                case DataRefScalarMode::kFloat:
                    payload["value"] = XPLMGetDataf(ref);
                    break;
                case DataRefScalarMode::kDouble:
                    payload["value"] = XPLMGetDatad(ref);
                    break;
                default:
                    throw mcp::mcp_exception(mcp::error_code::internal_error, "Unhandled scalar mode.");
            }

            return text_content(payload);
        }

        if (requested_mode != DataRefScalarMode::kAuto) {
            throw mcp::mcp_exception(
                mcp::error_code::invalid_params,
                "mode can only be used with numeric scalar DataRefs.");
        }

        if (supports_type(type_bits, xplmType_IntArray) || supports_type(type_bits, xplmType_FloatArray)) {
            const DataRefArrayMode mode = resolve_array_mode_for_get(type_bits);

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
                    {"kind", "array"},
                    {"value_type", array_value_type_to_string(mode)},
                    {"mode", array_mode_to_string(mode)},
                    {"type_bits", type_bits},
                    {"size", size},
                    {"offset", offset},
                    {"read", read},
                    {"value", out}
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
                {"kind", "array"},
                {"value_type", array_value_type_to_string(mode)},
                {"mode", array_mode_to_string(mode)},
                {"type_bits", type_bits},
                {"size", size},
                {"offset", offset},
                {"read", read},
                {"value", out}
            });
        }

        if (supports_type(type_bits, xplmType_Data)) {
            const int total = XPLMGetDatab(ref, nullptr, 0, 0);
            const int to_read = std::max(0, (max_items < 0) ? (total - offset) : max_items);
            std::vector<uint8_t> bytes(static_cast<size_t>(to_read), 0);
            const int read = XPLMGetDatab(ref, bytes.data(), offset, to_read);
            bytes.resize(static_cast<size_t>(std::max(0, read)));

            return text_content({
                {"name", dataref_name},
                {"kind", "bytes"},
                {"value_type", "bytes"},
                {"type_bits", type_bits},
                {"offset", offset},
                {"total", total},
                {"read", read},
                {"encoding", "hex"},
                {"value", bytes_to_hex(bytes)}
            });
        }

        throw mcp::mcp_exception(
            mcp::error_code::invalid_params,
            "DataRef does not expose a supported value type (scalar, int/float array, or bytes).");
    });
}

mcp::json PluginMcpServer::tool_dataref_set(const mcp::json& raw_params) {
    const mcp::json params = normalize_params(raw_params);
    const std::string dataref_name = require_string_arg(params, "name");
    const DataRefScalarMode requested_mode = parse_dataref_scalar_mode(params);
    const int offset = get_int_arg_or_default(params, "offset", 0);
    if (offset < 0) {
        throw mcp::mcp_exception(mcp::error_code::invalid_params, "offset must be >= 0.");
    }
    const mcp::json input_value = require_value_arg(params);

    return run_on_main_thread([dataref_name, requested_mode, offset, input_value] {
        const XPLMDataRef ref = XPLMFindDataRef(dataref_name.c_str());
        if (!ref) {
            throw mcp::mcp_exception(mcp::error_code::invalid_params, "DataRef not found: " + dataref_name);
        }
        if (!XPLMCanWriteDataRef(ref)) {
            throw mcp::mcp_exception(mcp::error_code::invalid_params, "DataRef is read-only: " + dataref_name);
        }

        const int type_bits = XPLMGetDataRefTypes(ref);

        if (input_value.is_number()) {
            const double numeric_input = input_value.get<double>();
            const DataRefScalarMode resolved_mode =
                resolve_scalar_mode_for_set(requested_mode, type_bits, numeric_input);

            switch (resolved_mode) {
                case DataRefScalarMode::kInt:
                    XPLMSetDatai(ref, static_cast<int>(std::llround(numeric_input)));
                    break;
                case DataRefScalarMode::kFloat:
                    XPLMSetDataf(ref, static_cast<float>(numeric_input));
                    break;
                case DataRefScalarMode::kDouble:
                    XPLMSetDatad(ref, numeric_input);
                    break;
                default:
                    throw mcp::mcp_exception(mcp::error_code::internal_error, "Unhandled scalar mode.");
            }

            mcp::json payload = {
                {"name", dataref_name},
                {"kind", "scalar"},
                {"value_type", scalar_mode_to_string(resolved_mode)},
                {"type_bits", type_bits},
                {"written_value", numeric_input}
            };

            switch (resolved_mode) {
                case DataRefScalarMode::kInt:
                    payload["current_value"] = XPLMGetDatai(ref);
                    break;
                case DataRefScalarMode::kFloat:
                    payload["current_value"] = XPLMGetDataf(ref);
                    break;
                case DataRefScalarMode::kDouble:
                    payload["current_value"] = XPLMGetDatad(ref);
                    break;
                default:
                    break;
            }

            return text_content(payload);
        }

        if (requested_mode != DataRefScalarMode::kAuto) {
            throw mcp::mcp_exception(
                mcp::error_code::invalid_params,
                "mode can only be used when writing a numeric scalar value.");
        }

        if (input_value.is_array()) {
            const DataRefArrayMode mode = resolve_array_mode_for_set(type_bits, input_value);

            int size = 0;
            int write_count = 0;
            if (mode == DataRefArrayMode::kInt) {
                size = XPLMGetDatavi(ref, nullptr, 0, 0);
                std::vector<int> values;
                values.reserve(input_value.size());
                for (const auto& item : input_value) {
                    if (!item.is_number()) {
                        throw mcp::mcp_exception(mcp::error_code::invalid_params, "all array values must be numeric.");
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
                values.reserve(input_value.size());
                for (const auto& item : input_value) {
                    if (!item.is_number()) {
                        throw mcp::mcp_exception(mcp::error_code::invalid_params, "all array values must be numeric.");
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
                {"kind", "array"},
                {"value_type", array_value_type_to_string(mode)},
                {"mode", array_mode_to_string(mode)},
                {"type_bits", type_bits},
                {"size", size},
                {"offset", offset},
                {"write_count", write_count}
            });
        }

        if (input_value.is_string()) {
            if (!supports_type(type_bits, xplmType_Data)) {
                throw mcp::mcp_exception(mcp::error_code::invalid_params, "DataRef does not expose byte data.");
            }
            const std::vector<uint8_t> bytes = hex_to_bytes(input_value.get<std::string>());

            if (!bytes.empty()) {
                XPLMSetDatab(ref, const_cast<uint8_t*>(bytes.data()), offset, static_cast<int>(bytes.size()));
            }

            std::vector<uint8_t> confirm(bytes.size(), 0);
            const int read = bytes.empty() ? 0 : XPLMGetDatab(ref, confirm.data(), offset, static_cast<int>(confirm.size()));
            confirm.resize(static_cast<size_t>(std::max(0, read)));

            return text_content({
                {"name", dataref_name},
                {"kind", "bytes"},
                {"value_type", "bytes"},
                {"type_bits", type_bits},
                {"offset", offset},
                {"written", static_cast<int>(bytes.size())},
                {"confirm_read", read},
                {"encoding", "hex"},
                {"current_value", bytes_to_hex(confirm)}
            });
        }

        throw mcp::mcp_exception(
            mcp::error_code::invalid_params,
            "value must be number (scalar), array<number> (array), or hex string (bytes).");
    });
}

}  // namespace xai_mcp
