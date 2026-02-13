# x-ai X-Plane MCP Plugin

This project builds an X-Plane plugin that embeds a `cpp-mcp` server and exposes selected XPLM SDK APIs as MCP tools.

## What It Exposes

- Runtime/system:
  - `xplm.get_versions`
  - `xplm.get_runtime_info`
  - `xplm.get_system_paths`
  - `xplm.directory_list`
  - `xplm.datafile_load`
  - `xplm.datafile_save`
  - `xplm.debug_string`
  - `xplm.speak_string`
  - `xplm.get_virtual_key_description`
  - `xplm.reload_scenery`
- Plugin management:
  - `xplm.get_self_plugin_info`
  - `xplm.plugin_get_info`
  - `xplm.plugin_find`
  - `xplm.plugin_set_enabled`
  - `xplm.plugin_reload_all` (requires `confirm=true`)
  - `xplm.list_plugins`
  - `xplm.feature_get`
  - `xplm.feature_set`
- Commands:
  - `xplm.command_execute` (`once|begin|end`, optional create-if-missing)
- Objects/instances:
  - `xplm.object_load`
  - `xplm.object_unload`
  - `xplm.object_list`
  - `xplm.instance_create`
  - `xplm.instance_destroy`
  - `xplm.instance_set_position`
  - `xplm.instance_set_auto_shift`
  - `xplm.instance_list`
- DataRefs:
  - `xplm.dataref_info`
  - `xplm.dataref_list`
  - `xplm.dataref_get`
  - `xplm.dataref_set`
  - `xplm.dataref_get_array`
  - `xplm.dataref_set_array`
  - `xplm.dataref_get_bytes`
  - `xplm.dataref_set_bytes`

All XPLM calls are marshaled onto the X-Plane main thread via a flight-loop queue for thread safety.

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

- `build/xplane_plugin/<abi>/x-ai-mcp.xpl`

## VS Code (Local Build + Debug)

This repo includes ready-to-use VS Code config in `.vscode/`.

1. Install recommended extensions:
   - CMake Tools
   - C/C++
   - CodeLLDB
2. Optional: set deploy target plugin directory (for auto-copy before debug attach):

```bash
export XPLANE_PLUGIN_DIR="/absolute/path/to/X-Plane 12/Resources/plugins/x-ai-mcp"
```

3. Build:
   - Run task `CMake: Build Plugin (Debug)` from `Terminal -> Run Task`.
4. Debug:
   - Start X-Plane.
   - Run launch config `Attach to X-Plane (Build + Deploy)` (or `Build Only`).
   - Pick the X-Plane process when prompted.

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
- The plugin now exposes a broad non-callback SDK surface intended for external automation agents.
