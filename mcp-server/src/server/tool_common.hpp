#ifndef XAI_MCP_TOOL_COMMON_HPP
#define XAI_MCP_TOOL_COMMON_HPP

#include "mcp_message.h"

#include <string>
#include <vector>

namespace xai_mcp {

mcp::json text_content(const mcp::json& payload);
mcp::json normalize_params(const mcp::json& params);

std::string require_string_arg(const mcp::json& params, const char* key);
double require_number_arg(const mcp::json& params, const char* key);
int require_int_arg(const mcp::json& params, const char* key);
bool require_bool_arg(const mcp::json& params, const char* key);

int get_int_arg_or_default(const mcp::json& params, const char* key, int fallback);
std::string get_string_arg_or_default(const mcp::json& params, const char* key, const std::string& fallback);

std::string pointer_to_hex(const void* ptr);
std::string bytes_to_hex(const std::vector<uint8_t>& bytes);
std::vector<uint8_t> hex_to_bytes(const std::string& hex);

}  // namespace xai_mcp

#endif
