#include "plugin_server.hpp"

#include "XPLMPlugin.h"

#include <cstring>

namespace {

void copy_plugin_string(char* dest, const char* src) {
    if (!dest || !src) {
        return;
    }
    std::strncpy(dest, src, 255);
    dest[255] = '\0';
}

xai_mcp::PluginMcpServer g_plugin_server;

}  // namespace

PLUGIN_API int XPluginStart(char* outName, char* outSignature, char* outDescription) {
    copy_plugin_string(outName, xai_mcp::kPluginName);
    copy_plugin_string(outSignature, xai_mcp::kPluginSignature);
    copy_plugin_string(outDescription, xai_mcp::kPluginDescription);

    if (!g_plugin_server.start()) {
        xai_mcp::log_line("XPluginStart failed.");
        return 0;
    }

    return 1;
}

PLUGIN_API void XPluginStop(void) {
    g_plugin_server.stop();
}

PLUGIN_API int XPluginEnable(void) {
    return g_plugin_server.start() ? 1 : 0;
}

PLUGIN_API void XPluginDisable(void) {
    g_plugin_server.stop();
}

PLUGIN_API void XPluginReceiveMessage(XPLMPluginID, int, void*) {
}
