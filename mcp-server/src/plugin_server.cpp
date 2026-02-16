#include "plugin_server.hpp"

#include "XPLMUtilities.h"

#include <chrono>
#include <cstdlib>
#include <stdexcept>
#include <utility>

namespace {

constexpr float kMainThreadPumpIntervalSeconds = 0.01f;
constexpr float kAircraftStateUpdateIntervalSeconds = 0.1f;

int read_env_int(const char* name, int fallback) {
    const char* value = std::getenv(name);
    if (!value || value[0] == '\0') {
        return fallback;
    }

    char* end = nullptr;
    const long parsed = std::strtol(value, &end, 10);
    if (!end || *end != '\0' || parsed <= 0 || parsed > 65535) {
        return fallback;
    }
    return static_cast<int>(parsed);
}

std::string read_env_string(const char* name, const char* fallback) {
    const char* value = std::getenv(name);
    if (!value || value[0] == '\0') {
        return fallback;
    }
    return value;
}

}  // namespace

namespace xai_mcp {

void log_line(const std::string& line) {
    std::string message = "[x-ai-mcp] " + line + "\n";
    XPLMDebugString(message.c_str());
}

bool PluginMcpServer::start() {
    if (running_.load()) {
        return true;
    }

    shutting_down_.store(false);
    sim_thread_id_ = std::this_thread::get_id();

    XPLMCreateFlightLoop_t loop_params{};
    loop_params.structSize = sizeof(loop_params);
    loop_params.phase = xplm_FlightLoop_Phase_AfterFlightModel;
    loop_params.callbackFunc = &PluginMcpServer::flight_loop_callback;
    loop_params.refcon = this;

    flight_loop_id_ = XPLMCreateFlightLoop(&loop_params);
    if (!flight_loop_id_) {
        log_line("failed to create flight loop callback.");
        return false;
    }

    // Keep a lightweight main-thread pump active so worker threads never need
    // to call XPLMScheduleFlightLoop (which must be invoked on the sim thread).
    XPLMScheduleFlightLoop(flight_loop_id_, -1.0f, 1);

    mcp::server::configuration conf;
    conf.host = read_env_string("XAI_MCP_HOST", "0.0.0.0");
    conf.port = read_env_int("XAI_MCP_PORT", 8765);
    conf.name = "x-ai-xplane-mcp";
    conf.version = kServerVersion;
    conf.threadpool_size = 2;

    server_ = std::make_unique<mcp::server>(conf);
    server_->set_server_info(conf.name, conf.version);
    server_->set_capabilities({
        {"tools", mcp::json::object()}
    });

    register_tools();
    aircraft_state_update_elapsed_sec_ = 0.0f;
    refresh_aircraft_state_cache_main_thread();

    if (!server_->start(false)) {
        server_.reset();
        XPLMDestroyFlightLoop(flight_loop_id_);
        flight_loop_id_ = nullptr;
        log_line("failed to start MCP server.");
        return false;
    }

    running_.store(true);
    log_line("MCP server listening on " + conf.host + ":" + std::to_string(conf.port));
    return true;
}

void PluginMcpServer::stop() {
    if (!running_.load() && !server_) {
        return;
    }

    shutting_down_.store(true);
    run_on_main_thread([this] {
        for (auto& [_, instance] : instances_) {
            if (instance.ref) {
                XPLMDestroyInstance(instance.ref);
                instance.ref = nullptr;
            }
        }
        instances_.clear();

        for (auto& [_, object] : objects_) {
            if (object.ref) {
                XPLMUnloadObject(object.ref);
                object.ref = nullptr;
            }
        }
        objects_.clear();
        return mcp::json::object();
    });

    process_pending_jobs();

    if (server_) {
        server_->stop();
        server_.reset();
    }

    process_pending_jobs();
    clear_pending_jobs();

    if (flight_loop_id_) {
        XPLMDestroyFlightLoop(flight_loop_id_);
        flight_loop_id_ = nullptr;
    }

    {
        std::lock_guard<std::mutex> lock(aircraft_state_mutex_);
        aircraft_state_cache_ = mcp::json::object();
        aircraft_state_cache_ready_ = false;
    }
    aircraft_state_update_elapsed_sec_ = 0.0f;

    running_.store(false);
    log_line("MCP server stopped.");
}

float PluginMcpServer::flight_loop_callback(float elapsed_since_last_call, float, int, void* refcon) {
    auto* self = static_cast<PluginMcpServer*>(refcon);
    self->process_pending_jobs();
    if (self->shutting_down_.load()) {
        return 0.0f;
    }

    if (elapsed_since_last_call > 0.0f) {
        self->aircraft_state_update_elapsed_sec_ += elapsed_since_last_call;
    }
    if (!self->aircraft_state_cache_ready_ || self->aircraft_state_update_elapsed_sec_ >= kAircraftStateUpdateIntervalSeconds) {
        self->refresh_aircraft_state_cache_main_thread();
        self->aircraft_state_update_elapsed_sec_ = 0.0f;
    }

    return kMainThreadPumpIntervalSeconds;
}

void PluginMcpServer::register_tools() {
    register_runtime_tools();
    register_navigation_tools();
    register_plugin_tools();
    register_object_tools();
    register_dataref_tools();
}

void PluginMcpServer::process_pending_jobs() {
    std::queue<std::shared_ptr<MainThreadJob>> local_jobs;
    {
        std::lock_guard<std::mutex> lock(jobs_mutex_);
        std::swap(local_jobs, jobs_);
    }

    while (!local_jobs.empty()) {
        std::shared_ptr<MainThreadJob> job = std::move(local_jobs.front());
        local_jobs.pop();

        try {
            job->promise.set_value(job->fn());
        } catch (...) {
            job->promise.set_exception(std::current_exception());
        }
    }
}

void PluginMcpServer::clear_pending_jobs() {
    std::queue<std::shared_ptr<MainThreadJob>> local_jobs;
    {
        std::lock_guard<std::mutex> lock(jobs_mutex_);
        std::swap(local_jobs, jobs_);
    }

    while (!local_jobs.empty()) {
        std::shared_ptr<MainThreadJob> job = std::move(local_jobs.front());
        local_jobs.pop();
        job->promise.set_exception(std::make_exception_ptr(
            std::runtime_error("Plugin is shutting down.")));
    }
}

mcp::json PluginMcpServer::run_on_main_thread(std::function<mcp::json()> fn) {
    if (std::this_thread::get_id() == sim_thread_id_) {
        return fn();
    }

    if (shutting_down_.load()) {
        throw mcp::mcp_exception(mcp::error_code::internal_error, "Plugin is shutting down.");
    }

    auto job = std::make_shared<MainThreadJob>();
    job->fn = std::move(fn);
    std::future<mcp::json> result = job->promise.get_future();

    {
        std::lock_guard<std::mutex> lock(jobs_mutex_);
        jobs_.push(job);
    }

    if (result.wait_for(std::chrono::seconds(3)) == std::future_status::timeout) {
        throw mcp::mcp_exception(mcp::error_code::internal_error, "Timed out waiting for X-Plane main thread.");
    }
    return result.get();
}

}  // namespace xai_mcp
