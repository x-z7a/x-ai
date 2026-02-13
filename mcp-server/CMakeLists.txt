cmake_minimum_required(VERSION 3.21)

project(xai_xplane_mcp_plugin VERSION 0.1.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)

option(XAI_MCP_PLUGIN_FETCH_CPP_MCP "Fetch and build cpp-mcp automatically." ON)
set(CPP_MCP_GIT_TAG "dc86c91f587e3a950a996d4fe6f8a0e2f5e9590d" CACHE STRING "cpp-mcp git tag or commit.")

option(XAI_MCP_PLUGIN_AUTO_DOWNLOAD_XPSDK "Auto-download X-Plane SDK during configure if XPLANE_SDK_ROOT is missing." ON)
set(XAI_MCP_PLUGIN_XPSDK_URL "https://developer.x-plane.com/wp-content/plugins/code-sample-generation/sdk_zip_files/XPSDK420.zip" CACHE STRING "X-Plane SDK zip URL.")
set(XAI_MCP_PLUGIN_XPSDK_CACHE_DIR "${CMAKE_BINARY_DIR}/_deps/xpsdk" CACHE PATH "Directory where auto-downloaded X-Plane SDK files are stored.")
set(XAI_MCP_PLUGIN_XPSDK_ZIP_PATH "" CACHE FILEPATH "Optional path to an existing X-Plane SDK zip file.")

set(XPLANE_SDK_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/third_party/XPSDK/SDK" CACHE PATH "Path to X-Plane SDK root (contains CHeaders and Libraries).")

if(NOT EXISTS "${XPLANE_SDK_ROOT}/CHeaders/XPLM/XPLMDefs.h")
    if(XAI_MCP_PLUGIN_AUTO_DOWNLOAD_XPSDK)
        file(MAKE_DIRECTORY "${XAI_MCP_PLUGIN_XPSDK_CACHE_DIR}")
        if(XAI_MCP_PLUGIN_XPSDK_ZIP_PATH)
            set(_xpsdk_zip "${XAI_MCP_PLUGIN_XPSDK_ZIP_PATH}")
        else()
            set(_xpsdk_zip "${XAI_MCP_PLUGIN_XPSDK_CACHE_DIR}/XPSDK420.zip")
        endif()
        set(_xpsdk_extract_dir "${XAI_MCP_PLUGIN_XPSDK_CACHE_DIR}/unpacked")
        set(_xpsdk_root_candidate "${_xpsdk_extract_dir}/SDK")

        if(NOT EXISTS "${_xpsdk_zip}")
            message(STATUS "Downloading X-Plane SDK from ${XAI_MCP_PLUGIN_XPSDK_URL}")
            find_program(_xpsdk_curl curl)
            if(_xpsdk_curl)
                execute_process(
                    COMMAND "${_xpsdk_curl}" -L --fail --output "${_xpsdk_zip}" "${XAI_MCP_PLUGIN_XPSDK_URL}"
                    RESULT_VARIABLE _xpsdk_download_code
                    OUTPUT_VARIABLE _xpsdk_download_stdout
                    ERROR_VARIABLE _xpsdk_download_stderr
                )
                if(NOT _xpsdk_download_code EQUAL 0)
                    message(FATAL_ERROR
                        "Failed to download X-Plane SDK with curl.\n"
                        "URL: ${XAI_MCP_PLUGIN_XPSDK_URL}\n"
                        "Code: ${_xpsdk_download_code}\n"
                        "Details: ${_xpsdk_download_stderr}")
                endif()
            else()
                file(DOWNLOAD
                    "${XAI_MCP_PLUGIN_XPSDK_URL}"
                    "${_xpsdk_zip}"
                    SHOW_PROGRESS
                    STATUS _xpsdk_download_status
                    TLS_VERIFY ON
                )
                list(GET _xpsdk_download_status 0 _xpsdk_download_code)
                list(GET _xpsdk_download_status 1 _xpsdk_download_message)
                if(NOT _xpsdk_download_code EQUAL 0)
                    message(FATAL_ERROR
                        "Failed to download X-Plane SDK.\n"
                        "URL: ${XAI_MCP_PLUGIN_XPSDK_URL}\n"
                        "Status: ${_xpsdk_download_code}\n"
                        "Message: ${_xpsdk_download_message}")
                endif()
            endif()
        endif()

        if(NOT EXISTS "${_xpsdk_root_candidate}/CHeaders/XPLM/XPLMDefs.h")
            message(STATUS "Extracting X-Plane SDK from ${_xpsdk_zip}")
            file(REMOVE_RECURSE "${_xpsdk_extract_dir}")
            file(MAKE_DIRECTORY "${_xpsdk_extract_dir}")
            file(ARCHIVE_EXTRACT
                INPUT "${_xpsdk_zip}"
                DESTINATION "${_xpsdk_extract_dir}"
            )
        endif()

        if(EXISTS "${_xpsdk_root_candidate}/CHeaders/XPLM/XPLMDefs.h")
            set(XPLANE_SDK_ROOT "${_xpsdk_root_candidate}" CACHE PATH "Path to X-Plane SDK root (contains CHeaders and Libraries)." FORCE)
            message(STATUS "Using auto-downloaded X-Plane SDK at: ${XPLANE_SDK_ROOT}")
        else()
            message(FATAL_ERROR
                "X-Plane SDK extraction did not produce expected layout.\n"
                "Expected: ${_xpsdk_root_candidate}/CHeaders/XPLM/XPLMDefs.h")
        endif()
    else()
        message(FATAL_ERROR
            "X-Plane SDK not found.\n"
            "Set XPLANE_SDK_ROOT to the extracted SDK directory or enable XAI_MCP_PLUGIN_AUTO_DOWNLOAD_XPSDK.\n"
            "Expected file: ${XPLANE_SDK_ROOT}/CHeaders/XPLM/XPLMDefs.h")
    endif()
endif()

include(FetchContent)

if(XAI_MCP_PLUGIN_FETCH_CPP_MCP)
    FetchContent_Declare(
        cpp_mcp
        GIT_REPOSITORY https://github.com/hkr04/cpp-mcp.git
        GIT_TAG ${CPP_MCP_GIT_TAG}
    )

    set(BUILD_SHARED_LIBS OFF CACHE BOOL "" FORCE)
    set(MCP_BUILD_TESTS OFF CACHE BOOL "" FORCE)
    set(MCP_SSL OFF CACHE BOOL "" FORCE)

    FetchContent_MakeAvailable(cpp_mcp)
else()
    message(FATAL_ERROR "XAI_MCP_PLUGIN_FETCH_CPP_MCP=OFF is not yet supported.")
endif()

add_library(xai_mcp_plugin SHARED
    src/plugin_main.cpp
    src/plugin_server.cpp
)

target_include_directories(xai_mcp_plugin PRIVATE
    "${XPLANE_SDK_ROOT}/CHeaders"
    "${XPLANE_SDK_ROOT}/CHeaders/XPLM"
    "${cpp_mcp_SOURCE_DIR}/include"
    "${cpp_mcp_SOURCE_DIR}/common"
)

target_compile_definitions(xai_mcp_plugin PRIVATE
    XPLM200=1
    XPLM210=1
    XPLM300=1
    XPLM301=1
    XPLM400=1
    XPLM410=1
    XPLM420=1
)

if(APPLE)
    target_compile_definitions(xai_mcp_plugin PRIVATE APL=1 IBM=0 LIN=0)
elseif(WIN32)
    target_compile_definitions(xai_mcp_plugin PRIVATE APL=0 IBM=1 LIN=0)
else()
    target_compile_definitions(xai_mcp_plugin PRIVATE APL=0 IBM=0 LIN=1)
endif()

target_link_libraries(xai_mcp_plugin PRIVATE mcp)

if(WIN32)
    target_link_libraries(xai_mcp_plugin PRIVATE
        "${XPLANE_SDK_ROOT}/Libraries/Win/XPLM_64.lib"
        "${XPLANE_SDK_ROOT}/Libraries/Win/XPWidgets_64.lib"
        ws2_32
    )
elseif(APPLE)
    target_link_options(xai_mcp_plugin PRIVATE "-F${XPLANE_SDK_ROOT}/Libraries/Mac")
    target_link_libraries(xai_mcp_plugin PRIVATE
        "-framework XPLM"
        "-framework XPWidgets"
    )
endif()

if(WIN32)
    set(XAI_PLUGIN_ABI "win_x64")
elseif(APPLE)
    set(XAI_PLUGIN_ABI "mac_x64")
else()
    set(XAI_PLUGIN_ABI "lin_x64")
endif()

set(XAI_PLUGIN_OUTPUT_DIR "${CMAKE_BINARY_DIR}/xplane_plugin/${XAI_PLUGIN_ABI}")

set_target_properties(xai_mcp_plugin PROPERTIES
    OUTPUT_NAME "x-ai"
    PREFIX ""
    SUFFIX ".xpl"
    LIBRARY_OUTPUT_DIRECTORY "${XAI_PLUGIN_OUTPUT_DIR}"
    RUNTIME_OUTPUT_DIRECTORY "${XAI_PLUGIN_OUTPUT_DIR}"
)
