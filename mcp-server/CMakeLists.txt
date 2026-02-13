cmake_minimum_required(VERSION 3.21)

project(xai_xplane_mcp_plugin VERSION 0.1.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)

option(XAI_MCP_PLUGIN_FETCH_CPP_MCP "Fetch and build cpp-mcp automatically." ON)
set(CPP_MCP_GIT_TAG "dc86c91f587e3a950a996d4fe6f8a0e2f5e9590d" CACHE STRING "cpp-mcp git tag or commit.")

set(XPLANE_SDK_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/third_party/XPSDK/SDK" CACHE PATH "Path to X-Plane SDK root (contains CHeaders and Libraries).")

if(NOT EXISTS "${XPLANE_SDK_ROOT}/CHeaders/XPLM/XPLMDefs.h")
    message(FATAL_ERROR
        "X-Plane SDK not found.\n"
        "Set XPLANE_SDK_ROOT to the extracted SDK directory.\n"
        "Expected file: ${XPLANE_SDK_ROOT}/CHeaders/XPLM/XPLMDefs.h")
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
    OUTPUT_NAME "x-ai-mcp"
    PREFIX ""
    SUFFIX ".xpl"
    LIBRARY_OUTPUT_DIRECTORY "${XAI_PLUGIN_OUTPUT_DIR}"
    RUNTIME_OUTPUT_DIRECTORY "${XAI_PLUGIN_OUTPUT_DIR}"
)
