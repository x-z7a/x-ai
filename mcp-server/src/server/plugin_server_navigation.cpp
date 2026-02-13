#include "../plugin_server.hpp"

#include "tool_common.hpp"

#include "mcp_tool.h"

#include "XPLMNavigation.h"

#include <algorithm>
#include <cctype>

namespace {

struct NamedNavType {
    const char* name;
    XPLMNavType bit;
};

constexpr NamedNavType kNamedNavTypes[] = {
    {"airport", xplm_Nav_Airport},
    {"ndb", xplm_Nav_NDB},
    {"vor", xplm_Nav_VOR},
    {"ils", xplm_Nav_ILS},
    {"localizer", xplm_Nav_Localizer},
    {"glideslope", xplm_Nav_GlideSlope},
    {"outer_marker", xplm_Nav_OuterMarker},
    {"middle_marker", xplm_Nav_MiddleMarker},
    {"inner_marker", xplm_Nav_InnerMarker},
    {"fix", xplm_Nav_Fix},
    {"dme", xplm_Nav_DME},
    {"latlon", xplm_Nav_LatLon},
    {"tacan", xplm_Nav_TACAN}
};

std::string to_lower_ascii(std::string value) {
    std::transform(value.begin(), value.end(), value.begin(), [](unsigned char c) {
        return static_cast<char>(std::tolower(c));
    });
    return value;
}

bool is_single_bit(int value) {
    return value > 0 && (value & (value - 1)) == 0;
}

const char* nav_type_to_name(XPLMNavType type) {
    for (const auto& item : kNamedNavTypes) {
        if (item.bit == type) {
            return item.name;
        }
    }
    return "unknown";
}

mcp::json nav_type_mask_to_names(int mask) {
    mcp::json names = mcp::json::array();
    for (const auto& item : kNamedNavTypes) {
        if ((mask & item.bit) != 0) {
            names.push_back(item.name);
        }
    }
    return names;
}

XPLMNavType nav_type_from_string(const std::string& value) {
    const std::string normalized = to_lower_ascii(value);
    for (const auto& item : kNamedNavTypes) {
        if (normalized == item.name) {
            return item.bit;
        }
    }
    if (normalized == "glide_slope") {
        return xplm_Nav_GlideSlope;
    }
    if (normalized == "lat_lon") {
        return xplm_Nav_LatLon;
    }
    throw mcp::mcp_exception(mcp::error_code::invalid_params, "Unsupported nav type: " + value);
}

XPLMNavType nav_type_from_json_value(const mcp::json& value) {
    if (value.is_number_integer()) {
        return static_cast<XPLMNavType>(value.get<int>());
    }
    if (value.is_string()) {
        return nav_type_from_string(value.get<std::string>());
    }
    throw mcp::mcp_exception(mcp::error_code::invalid_params, "Nav type must be string or integer.");
}

int parse_nav_type_mask_arg(const mcp::json& params, const char* key, int fallback, bool require_single) {
    if (!params.contains(key)) {
        return fallback;
    }

    const auto& raw = params[key];
    int mask = 0;
    if (raw.is_array()) {
        for (const auto& value : raw) {
            mask |= static_cast<int>(nav_type_from_json_value(value));
        }
    } else {
        mask = static_cast<int>(nav_type_from_json_value(raw));
    }

    if (mask <= 0) {
        throw mcp::mcp_exception(mcp::error_code::invalid_params, std::string(key) + " must resolve to a positive nav type mask.");
    }
    if (require_single && !is_single_bit(mask)) {
        throw mcp::mcp_exception(mcp::error_code::invalid_params, std::string(key) + " must be exactly one nav type.");
    }
    return mask;
}

mcp::json nav_ref_to_json(XPLMNavRef nav_ref) {
    XPLMNavType type = xplm_Nav_Unknown;
    float latitude = 0.0f;
    float longitude = 0.0f;
    float height = 0.0f;
    int frequency = 0;
    float heading = 0.0f;
    char id[64] = {};
    char name[256] = {};
    char in_region = 0;

    XPLMGetNavAidInfo(
        nav_ref,
        &type,
        &latitude,
        &longitude,
        &height,
        &frequency,
        &heading,
        id,
        name,
        &in_region
    );

    return {
        {"nav_ref", nav_ref},
        {"type_bit", type},
        {"type_name", nav_type_to_name(type)},
        {"type_names", nav_type_mask_to_names(type)},
        {"latitude", latitude},
        {"longitude", longitude},
        {"height", height},
        {"frequency", frequency},
        {"heading", heading},
        {"id", id},
        {"name", name},
        {"in_region", in_region != 0}
    };
}

}  // namespace

namespace xai_mcp {

void PluginMcpServer::register_navigation_tools() {
    server_->register_tool(
        mcp::tool_builder("xplm_nav_list")
            .with_description("List navaids with optional type filter and pagination.")
            .with_number_param("offset", "Start index within filtered result set.", false)
            .with_number_param("limit", "Maximum navaids to return.", false)
            .with_number_param("type_mask", "Optional nav type bitmask filter.", false)
            .with_array_param("types", "Optional nav type names (airport|ndb|vor|ils|localizer|glideslope|outer_marker|middle_marker|inner_marker|fix|dme|latlon|tacan).", "string", false)
            .build(),
        [this](const mcp::json& params, const std::string&) { return this->tool_nav_list(params); });

    server_->register_tool(
        mcp::tool_builder("xplm_nav_info")
            .with_description("Get full nav-aid metadata by nav_ref.")
            .with_number_param("nav_ref", "Navigation reference ID.")
            .build(),
        [this](const mcp::json& params, const std::string&) { return this->tool_nav_info(params); });

    server_->register_tool(
        mcp::tool_builder("xplm_nav_find")
            .with_description("Find a nav-aid using name/id fragments, optional location/frequency and type filters.")
            .with_string_param("name_fragment", "Name fragment.", false)
            .with_string_param("id_fragment", "ID fragment.", false)
            .with_number_param("lat", "Latitude for nearest search.", false)
            .with_number_param("lon", "Longitude for nearest search.", false)
            .with_number_param("frequency", "Frequency in nav.dat units.", false)
            .with_number_param("type_mask", "Optional nav type bitmask filter.", false)
            .with_array_param("types", "Optional nav type names.", "string", false)
            .build(),
        [this](const mcp::json& params, const std::string&) { return this->tool_nav_find(params); });

    server_->register_tool(
        mcp::tool_builder("xplm_nav_find_first_of_type")
            .with_description("Find first nav-aid of a single nav type.")
            .with_string_param("type", "Single nav type name or integer bit value.")
            .build(),
        [this](const mcp::json& params, const std::string&) { return this->tool_nav_find_first_of_type(params); });

    server_->register_tool(
        mcp::tool_builder("xplm_nav_find_last_of_type")
            .with_description("Find last nav-aid of a single nav type.")
            .with_string_param("type", "Single nav type name or integer bit value.")
            .build(),
        [this](const mcp::json& params, const std::string&) { return this->tool_nav_find_last_of_type(params); });

    server_->register_tool(
        mcp::tool_builder("xplm_nav_next")
            .with_description("Get next nav-aid reference after nav_ref.")
            .with_number_param("nav_ref", "Current nav_ref.")
            .build(),
        [this](const mcp::json& params, const std::string&) { return this->tool_nav_next(params); });

    server_->register_tool(
        mcp::tool_builder("xplm_gps_destination")
            .with_description("Get current GPS destination.")
            .build(),
        [this](const mcp::json& params, const std::string&) { return this->tool_gps_destination(params); });

    server_->register_tool(
        mcp::tool_builder("xplm_fms_status")
            .with_description("Get basic FMS status (count/displayed/destination).")
            .build(),
        [this](const mcp::json& params, const std::string&) { return this->tool_fms_status(params); });

    server_->register_tool(
        mcp::tool_builder("xplm_fms_entry_get")
            .with_description("Read one legacy FMS entry by index.")
            .with_number_param("index", "FMS index.")
            .build(),
        [this](const mcp::json& params, const std::string&) { return this->tool_fms_entry_get(params); });

    server_->register_tool(
        mcp::tool_builder("xplm_fms_entry_set_nav")
            .with_description("Set legacy FMS entry to a nav_ref and altitude.")
            .with_number_param("index", "FMS index.")
            .with_number_param("nav_ref", "Navigation reference ID.")
            .with_number_param("altitude", "Altitude in feet.", false)
            .build(),
        [this](const mcp::json& params, const std::string&) { return this->tool_fms_entry_set_nav(params); });

    server_->register_tool(
        mcp::tool_builder("xplm_fms_entry_set_latlon")
            .with_description("Set legacy FMS entry to a latitude/longitude waypoint.")
            .with_number_param("index", "FMS index.")
            .with_number_param("lat", "Latitude.")
            .with_number_param("lon", "Longitude.")
            .with_number_param("altitude", "Altitude in feet.", false)
            .build(),
        [this](const mcp::json& params, const std::string&) { return this->tool_fms_entry_set_latlon(params); });

    server_->register_tool(
        mcp::tool_builder("xplm_fms_entry_clear")
            .with_description("Clear one legacy FMS entry.")
            .with_number_param("index", "FMS index.")
            .build(),
        [this](const mcp::json& params, const std::string&) { return this->tool_fms_entry_clear(params); });

    server_->register_tool(
        mcp::tool_builder("xplm_fms_entry_set_displayed")
            .with_description("Set displayed legacy FMS entry index.")
            .with_number_param("index", "FMS index.")
            .build(),
        [this](const mcp::json& params, const std::string&) { return this->tool_fms_entry_set_displayed(params); });

    server_->register_tool(
        mcp::tool_builder("xplm_fms_entry_set_destination")
            .with_description("Set destination legacy FMS entry index.")
            .with_number_param("index", "FMS index.")
            .build(),
        [this](const mcp::json& params, const std::string&) { return this->tool_fms_entry_set_destination(params); });

}

mcp::json PluginMcpServer::tool_nav_list(const mcp::json& raw_params) {
    const mcp::json params = normalize_params(raw_params);
    const int offset = get_int_arg_or_default(params, "offset", 0);
    const int limit = get_int_arg_or_default(params, "limit", 200);
    if (offset < 0) {
        throw mcp::mcp_exception(mcp::error_code::invalid_params, "offset must be >= 0.");
    }
    if (limit <= 0) {
        throw mcp::mcp_exception(mcp::error_code::invalid_params, "limit must be > 0.");
    }

    int type_mask = 0;
    if (params.contains("type_mask")) {
        type_mask |= parse_nav_type_mask_arg(params, "type_mask", 0, false);
    }
    if (params.contains("types")) {
        type_mask |= parse_nav_type_mask_arg(params, "types", 0, false);
    }

    return run_on_main_thread([offset, limit, type_mask] {
        mcp::json entries = mcp::json::array();
        int total = 0;
        int matched_total = 0;

        for (XPLMNavRef ref = XPLMGetFirstNavAid(); ref != XPLM_NAV_NOT_FOUND; ref = XPLMGetNextNavAid(ref)) {
            ++total;

            XPLMNavType nav_type = xplm_Nav_Unknown;
            XPLMGetNavAidInfo(ref, &nav_type, nullptr, nullptr, nullptr, nullptr, nullptr, nullptr, nullptr, nullptr);
            if (type_mask != 0 && (nav_type & type_mask) == 0) {
                continue;
            }

            if (matched_total >= offset && static_cast<int>(entries.size()) < limit) {
                entries.push_back(nav_ref_to_json(ref));
            }
            ++matched_total;
        }

        return text_content({
            {"offset", offset},
            {"limit", limit},
            {"type_mask", type_mask},
            {"type_names", nav_type_mask_to_names(type_mask)},
            {"total", total},
            {"matched_total", matched_total},
            {"returned", static_cast<int>(entries.size())},
            {"entries", entries}
        });
    });
}

mcp::json PluginMcpServer::tool_nav_info(const mcp::json& raw_params) {
    const mcp::json params = normalize_params(raw_params);
    const XPLMNavRef nav_ref = require_int_arg(params, "nav_ref");

    return run_on_main_thread([nav_ref] {
        if (nav_ref == XPLM_NAV_NOT_FOUND) {
            return text_content({
                {"nav_ref", nav_ref},
                {"found", false}
            });
        }
        mcp::json payload = nav_ref_to_json(nav_ref);
        payload["found"] = true;
        return text_content(payload);
    });
}

mcp::json PluginMcpServer::tool_nav_find(const mcp::json& raw_params) {
    const mcp::json params = normalize_params(raw_params);

    const std::string name_fragment = get_string_arg_or_default(params, "name_fragment", "");
    const std::string id_fragment = get_string_arg_or_default(params, "id_fragment", "");

    const bool has_lat = params.contains("lat");
    const bool has_lon = params.contains("lon");
    if (has_lat != has_lon) {
        throw mcp::mcp_exception(mcp::error_code::invalid_params, "lat and lon must be provided together.");
    }

    const bool has_frequency = params.contains("frequency");
    const int frequency = has_frequency ? require_int_arg(params, "frequency") : 0;
    float latitude = has_lat ? static_cast<float>(require_number_arg(params, "lat")) : 0.0f;
    float longitude = has_lon ? static_cast<float>(require_number_arg(params, "lon")) : 0.0f;

    int type_mask = 0;
    if (params.contains("type_mask")) {
        type_mask |= parse_nav_type_mask_arg(params, "type_mask", 0, false);
    }
    if (params.contains("types")) {
        type_mask |= parse_nav_type_mask_arg(params, "types", 0, false);
    }
    if (type_mask == 0) {
        for (const auto& item : kNamedNavTypes) {
            type_mask |= item.bit;
        }
    }

    return run_on_main_thread([name_fragment, id_fragment, has_lat, has_frequency, latitude, longitude, frequency, type_mask] {
        float lat_copy = latitude;
        float lon_copy = longitude;
        int freq_copy = frequency;

        XPLMNavRef nav_ref = XPLMFindNavAid(
            name_fragment.empty() ? nullptr : name_fragment.c_str(),
            id_fragment.empty() ? nullptr : id_fragment.c_str(),
            has_lat ? &lat_copy : nullptr,
            has_lat ? &lon_copy : nullptr,
            has_frequency ? &freq_copy : nullptr,
            type_mask
        );

        if (nav_ref == XPLM_NAV_NOT_FOUND) {
            return text_content({
                {"found", false},
                {"name_fragment", name_fragment},
                {"id_fragment", id_fragment},
                {"type_mask", type_mask},
                {"type_names", nav_type_mask_to_names(type_mask)}
            });
        }

        mcp::json payload = nav_ref_to_json(nav_ref);
        payload["found"] = true;
        payload["name_fragment"] = name_fragment;
        payload["id_fragment"] = id_fragment;
        payload["type_mask"] = type_mask;
        payload["type_names"] = nav_type_mask_to_names(type_mask);
        return text_content(payload);
    });
}

mcp::json PluginMcpServer::tool_nav_find_first_of_type(const mcp::json& raw_params) {
    const mcp::json params = normalize_params(raw_params);
    if (!params.contains("type")) {
        throw mcp::mcp_exception(mcp::error_code::invalid_params, "Missing argument: type");
    }
    const int type_mask = parse_nav_type_mask_arg(params, "type", 0, true);

    return run_on_main_thread([type_mask] {
        const XPLMNavRef nav_ref = XPLMFindFirstNavAidOfType(type_mask);
        if (nav_ref == XPLM_NAV_NOT_FOUND) {
            return text_content({
                {"found", false},
                {"type_bit", type_mask},
                {"type_name", nav_type_to_name(type_mask)}
            });
        }

        mcp::json payload = nav_ref_to_json(nav_ref);
        payload["found"] = true;
        payload["query_type_bit"] = type_mask;
        payload["query_type_name"] = nav_type_to_name(type_mask);
        return text_content(payload);
    });
}

mcp::json PluginMcpServer::tool_nav_find_last_of_type(const mcp::json& raw_params) {
    const mcp::json params = normalize_params(raw_params);
    if (!params.contains("type")) {
        throw mcp::mcp_exception(mcp::error_code::invalid_params, "Missing argument: type");
    }
    const int type_mask = parse_nav_type_mask_arg(params, "type", 0, true);

    return run_on_main_thread([type_mask] {
        const XPLMNavRef nav_ref = XPLMFindLastNavAidOfType(type_mask);
        if (nav_ref == XPLM_NAV_NOT_FOUND) {
            return text_content({
                {"found", false},
                {"type_bit", type_mask},
                {"type_name", nav_type_to_name(type_mask)}
            });
        }

        mcp::json payload = nav_ref_to_json(nav_ref);
        payload["found"] = true;
        payload["query_type_bit"] = type_mask;
        payload["query_type_name"] = nav_type_to_name(type_mask);
        return text_content(payload);
    });
}

mcp::json PluginMcpServer::tool_nav_next(const mcp::json& raw_params) {
    const mcp::json params = normalize_params(raw_params);
    const XPLMNavRef nav_ref = require_int_arg(params, "nav_ref");

    return run_on_main_thread([nav_ref] {
        const XPLMNavRef next_ref = XPLMGetNextNavAid(nav_ref);
        if (next_ref == XPLM_NAV_NOT_FOUND) {
            return text_content({
                {"found", false},
                {"input_nav_ref", nav_ref}
            });
        }

        mcp::json payload = nav_ref_to_json(next_ref);
        payload["found"] = true;
        payload["input_nav_ref"] = nav_ref;
        return text_content(payload);
    });
}

mcp::json PluginMcpServer::tool_gps_destination(const mcp::json& raw_params) {
    (void)normalize_params(raw_params);
    return run_on_main_thread([] {
        const XPLMNavType destination_type = XPLMGetGPSDestinationType();
        const XPLMNavRef destination_ref = XPLMGetGPSDestination();

        mcp::json payload = {
            {"destination_type_bit", destination_type},
            {"destination_type_name", nav_type_to_name(destination_type)},
            {"destination_ref", destination_ref},
            {"has_destination", destination_ref != XPLM_NAV_NOT_FOUND}
        };
        if (destination_ref != XPLM_NAV_NOT_FOUND) {
            payload["destination"] = nav_ref_to_json(destination_ref);
        }
        return text_content(payload);
    });
}

mcp::json PluginMcpServer::tool_fms_status(const mcp::json& raw_params) {
    (void)normalize_params(raw_params);
    return run_on_main_thread([] {
        return text_content({
            {"entry_count", XPLMCountFMSEntries()},
            {"displayed_index", XPLMGetDisplayedFMSEntry()},
            {"destination_index", XPLMGetDestinationFMSEntry()}
        });
    });
}

mcp::json PluginMcpServer::tool_fms_entry_get(const mcp::json& raw_params) {
    const mcp::json params = normalize_params(raw_params);
    const int index = require_int_arg(params, "index");

    return run_on_main_thread([index] {
        XPLMNavType nav_type = xplm_Nav_Unknown;
        char id[256] = {};
        XPLMNavRef nav_ref = XPLM_NAV_NOT_FOUND;
        int altitude = 0;
        float latitude = 0.0f;
        float longitude = 0.0f;
        XPLMGetFMSEntryInfo(index, &nav_type, id, &nav_ref, &altitude, &latitude, &longitude);

        mcp::json payload = {
            {"index", index},
            {"type_bit", nav_type},
            {"type_name", nav_type_to_name(nav_type)},
            {"id", id},
            {"nav_ref", nav_ref},
            {"altitude", altitude},
            {"latitude", latitude},
            {"longitude", longitude},
            {"has_nav_ref", nav_ref != XPLM_NAV_NOT_FOUND}
        };
        if (nav_ref != XPLM_NAV_NOT_FOUND) {
            payload["nav"] = nav_ref_to_json(nav_ref);
        }
        return text_content(payload);
    });
}

mcp::json PluginMcpServer::tool_fms_entry_set_nav(const mcp::json& raw_params) {
    const mcp::json params = normalize_params(raw_params);
    const int index = require_int_arg(params, "index");
    const XPLMNavRef nav_ref = require_int_arg(params, "nav_ref");
    const int altitude = get_int_arg_or_default(params, "altitude", 0);

    return run_on_main_thread([index, nav_ref, altitude] {
        XPLMSetFMSEntryInfo(index, nav_ref, altitude);
        return text_content({
            {"index", index},
            {"nav_ref", nav_ref},
            {"altitude", altitude},
            {"success", true}
        });
    });
}

mcp::json PluginMcpServer::tool_fms_entry_set_latlon(const mcp::json& raw_params) {
    const mcp::json params = normalize_params(raw_params);
    const int index = require_int_arg(params, "index");
    const float latitude = static_cast<float>(require_number_arg(params, "lat"));
    const float longitude = static_cast<float>(require_number_arg(params, "lon"));
    const int altitude = get_int_arg_or_default(params, "altitude", 0);

    return run_on_main_thread([index, latitude, longitude, altitude] {
        XPLMSetFMSEntryLatLon(index, latitude, longitude, altitude);
        return text_content({
            {"index", index},
            {"latitude", latitude},
            {"longitude", longitude},
            {"altitude", altitude},
            {"success", true}
        });
    });
}

mcp::json PluginMcpServer::tool_fms_entry_clear(const mcp::json& raw_params) {
    const mcp::json params = normalize_params(raw_params);
    const int index = require_int_arg(params, "index");

    return run_on_main_thread([index] {
        XPLMClearFMSEntry(index);
        return text_content({
            {"index", index},
            {"success", true}
        });
    });
}

mcp::json PluginMcpServer::tool_fms_entry_set_displayed(const mcp::json& raw_params) {
    const mcp::json params = normalize_params(raw_params);
    const int index = require_int_arg(params, "index");

    return run_on_main_thread([index] {
        XPLMSetDisplayedFMSEntry(index);
        return text_content({
            {"index", index},
            {"displayed_index", XPLMGetDisplayedFMSEntry()}
        });
    });
}

mcp::json PluginMcpServer::tool_fms_entry_set_destination(const mcp::json& raw_params) {
    const mcp::json params = normalize_params(raw_params);
    const int index = require_int_arg(params, "index");

    return run_on_main_thread([index] {
        XPLMSetDestinationFMSEntry(index);
        return text_content({
            {"index", index},
            {"destination_index", XPLMGetDestinationFMSEntry()}
        });
    });
}

}  // namespace xai_mcp
