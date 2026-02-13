# x-ai X-Plane MCP Plugin

This project builds an X-Plane plugin that embeds a `cpp-mcp` server and exposes selected XPLM SDK APIs as MCP tools.

## What It Exposes

- Runtime/system:
  - `xplm_get_versions`
  - `xplm_get_runtime_info`
  - `xplm_get_system_paths`
  - `xplm_directory_list`
  - `xplm_datafile_load`
  - `xplm_datafile_save`
  - `xplm_debug_string`
  - `xplm_speak_string`
  - `xplm_get_virtual_key_description`
  - `xplm_reload_scenery`
- Plugin management:
  - `xplm_get_self_plugin_info`
  - `xplm_plugin_get_info`
  - `xplm_plugin_find`
  - `xplm_plugin_set_enabled`
  - `xplm_plugin_reload_all` (requires `confirm=true`)
  - `xplm_list_plugins`
  - `xplm_feature_get`
  - `xplm_feature_set`
- Commands:
  - `xplm_command_execute` (`once|begin|end`, optional create-if-missing)
- Objects/instances:
  - `xplm_object_load`
  - `xplm_object_unload`
  - `xplm_object_list`
  - `xplm_instance_create`
  - `xplm_instance_destroy`
  - `xplm_instance_set_position`
  - `xplm_instance_set_auto_shift`
  - `xplm_instance_list`
- DataRefs:
  - `xplm_dataref_info`
  - `xplm_dataref_list`
  - `xplm_dataref_get`
  - `xplm_dataref_set`
  - `xplm_dataref_get_array`
  - `xplm_dataref_set_array`
  - `xplm_dataref_get_bytes`
  - `xplm_dataref_set_bytes`

All XPLM calls are marshaled onto the X-Plane main thread via a flight-loop queue for thread safety.
Canonical MCP tool IDs use underscores (for client compatibility). Legacy dotted calls such as `xplm.dataref_get` are accepted for backward compatibility.

## Build Locally

1. Configure and build (SDK auto-download is enabled by default):

```bash
cmake -S mcp-server -B build \
  -DCMAKE_BUILD_TYPE=Release
cmake --build build --target xai_mcp_plugin
```

2. Optional overrides:
   - Use a pre-installed SDK: `-DXPLANE_SDK_ROOT=/path/to/SDK`
   - Use a local SDK zip: `-DXAI_MCP_PLUGIN_XPSDK_ZIP_PATH=/path/to/XPSDK420.zip`
   - Disable auto-download entirely: `-DXAI_MCP_PLUGIN_AUTO_DOWNLOAD_XPSDK=OFF`

Output binary:

- `build/xplane_plugin/<abi>/x-ai.xpl`

## Release Package

Tag pushes matching `v*` publish one merged release asset:

- `x-ai-<tag>.zip` (for example: `x-ai-v0.1.2.zip`)

Archive layout:

- `x-ai/mac_x64/x-ai.xpl`
- `x-ai/win_x64/x-ai.xpl`
- `x-ai/lin_x64/x-ai.xpl`

Release packaging fails if any required platform binary is missing.

## VS Code (Local Build + Debug)

This repo includes ready-to-use VS Code config in `.vscode/`.

1. Install recommended extensions:
   - CMake Tools
   - C/C++
   - CodeLLDB
2. Optional: set deploy target plugin directory (for auto-copy before debug attach):

```bash
export XPLANE_PLUGIN_DIR="/absolute/path/to/X-Plane 12/Resources/plugins/x-ai"
```

3. Build:
   - Run task `CMake: Build Plugin (Debug)` from `Terminal -> Run Task`.
4. Debug:
   - Start X-Plane.
   - Run launch config `Attach to X-Plane (Build + Deploy)` (or `Build Only`).
   - Pick the X-Plane process when prompted.

5. MCP client connection (VS Code / clients using `mcp.json`):

```json
{
  "servers": {
    "xplane MCP": {
      "type": "sse",
      "url": "http://127.0.0.1:8765/sse"
    }
  }
}
```

The embedded `cpp-mcp` server uses legacy SSE transport (`/sse` + `/message`), not streamable HTTP at `/`.

## Runtime Configuration

- `XAI_MCP_HOST` (default: `127.0.0.1`)
- `XAI_MCP_PORT` (default: `8765`)

Example:

```bash
XAI_MCP_HOST=0.0.0.0 XAI_MCP_PORT=9000
```

## Notes

- `cpp-mcp` is fetched during CMake configure via `FetchContent`.
- `cpp-mcp` commit pin: `dc86c91f587e3a950a996d4fe6f8a0e2f5e9590d`.
- Configure applies a compatibility patch so the embedded server accepts MCP protocol `2024-11-05` and `2025-11-25`.
- The plugin now exposes a broad non-callback SDK surface intended for external automation agents.
