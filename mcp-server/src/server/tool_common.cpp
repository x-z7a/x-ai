#include "tool_common.hpp"

#include <cstdint>
#include <cstdlib>
#include <iomanip>
#include <sstream>
#include <stdexcept>

namespace xai_mcp {

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

}  // namespace xai_mcp
