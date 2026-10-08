// Native Metal harness: same scene, seed, film and render sequence as python.py.
#include <mitsuba/core/bitmap.h>
#include <mitsuba/core/fresolver.h>
#include <mitsuba/core/logger.h>
#include <mitsuba/core/parser.h>
#include <mitsuba/core/profiler.h>
#include <mitsuba/core/util.h>
#include <mitsuba/render/integrator.h>
#include <mitsuba/render/scene.h>
#include <mitsuba/render/srgb.h>
#include <nanothread/nanothread.h>
#include <chrono>
#include <fstream>
#include <iomanip>
#include <iostream>

using namespace mitsuba;
using Float = dr::MetalArray<float>;
using BenchmarkSpectrum = MuellerMatrix<Color<Float, 3>>;
using SceneT = Scene<Float, BenchmarkSpectrum>;
using Clock = std::chrono::steady_clock;
static double elapsed(Clock::time_point start) {
    return std::chrono::duration<double, std::milli>(Clock::now() - start).count();
}
static auto render(SceneT *scene, uint32_t seed, uint32_t spp, bool print) {
    auto start = Clock::now();
    auto sensor = scene->sensors().at(0);
    scene->integrator()->render(scene, sensor.get(), seed, spp, false, false, false);
    auto image = sensor->film()->develop(false);
    double trace = elapsed(start);
    auto evaluation = Clock::now();
    dr::eval(image.array());
    dr::sync_thread();
    auto end = Clock::now();
    double evaluation_ms = std::chrono::duration<double, std::milli>(end - evaluation).count();
    double total_ms = std::chrono::duration<double, std::milli>(end - start).count();
    if (print)
        std::cout << "{\"trace_ms\":" << trace << ",\"eval_sync_ms\":"
                  << evaluation_ms << ",\"total_ms\":" << total_ms << '}';
    return image;
}
int main(int argc, char **argv) {
    if (argc != 7) {
        std::cerr << "Usage: pollia-cpp-benchmark scene size spp repeats warmup output.f32\n";
        return 2;
    }
    try {
        auto start = Clock::now();
        uint32_t size = std::stoul(argv[2]), spp = std::stoul(argv[3]);
        int repeats = std::stoi(argv[4]), warmup = std::stoi(argv[5]);
        if (!size || !spp || repeats < 1 || warmup < 1)
            throw std::runtime_error("Invalid benchmark settings");
        Thread::static_initialization();
        Logger::static_initialization();
        Bitmap::static_initialization();
        Thread::thread()->logger()->set_log_level(Warn);
        jit_set_log_level_stderr(::LogLevel::Error);
        jit_init(1u << uint32_t(JitBackend::Metal));
        if (!jit_has_backend(JitBackend::Metal))
            throw std::runtime_error("Metal unavailable; no CPU fallback");
        pool_set_size(nullptr, 12);
        Profiler::static_initialization();
        color_management_static_initialization(false, false, true);
        SceneT::static_accel_initialization();
        file_resolver()->append(util::library_path().parent_path());
        double initialization = elapsed(start);
        start = Clock::now();
        parser::ParserConfig config("metal_rgb_polarized");
        auto state = parser::parse_file(config, argv[1], {{"size", std::to_string(size)}});
        parser::transform_all(config, state);
        auto objects = parser::instantiate(config, state);
        auto *scene = objects.size() == 1 ? dynamic_cast<SceneT *>(objects[0].get()) : nullptr;
        if (!scene) throw std::runtime_error("Expected one RGB polarized scene");
        dr::sync_thread();
        double scene_load = elapsed(start);
        std::cout << std::setprecision(12) << "{\"language\":\"cpp\",\"variant\":\"metal_rgb_polarized\","
                  << "\"initialize_ms\":" << initialization << ",\"scene_load_ms\":" << scene_load
                  << ",\"first\":";
        { auto image = render(scene, 1, spp, true); }
        for (int i = 0; i < warmup; ++i) { auto image = render(scene, 50 + i, spp, false); }
        std::cout << ",\"warm\":[";
        for (int i = 0; i < repeats; ++i) {
            if (i) std::cout << ',';
            auto image = render(scene, 100 + i, spp, true);
        }
        std::cout << ']';
        // Separate profiling pass: history instrumentation is outside timed samples.
        jit_set_flag(JitFlag::KernelHistory, true);
        auto begin = jit_kernel_history_begin();
        auto image = render(scene, 1, spp, false);
        auto history = jit_kernel_history_end(begin);
        jit_set_flag(JitFlag::KernelHistory, false);
        double device_ms = 0;
        size_t count = jit_kernel_history_size(history);
        if (!count) throw std::runtime_error("Expected Metal history entries");
        for (size_t i = 0; i < count; ++i) {
            if (jit_kernel_history_query(history, i, KernelHistoryField::Backend) != uint64_t(JitBackend::Metal))
                throw std::runtime_error("Non-Metal history entry");
            device_ms += double(jit_kernel_history_query(history, i, KernelHistoryField::ExecutionTime)) / 1e6;
        }
        jit_kernel_history_free(history);
        start = Clock::now();
        auto host = dr::migrate(image.array(), JitBackend::None);
        dr::sync_thread();
        const float *data = host.data();
        double readback_ms = elapsed(start);
        if (image.ndim() != 3 || image.shape(0) != size || image.shape(1) != size || image.shape(2) != 15)
            throw std::runtime_error("Unexpected Stokes image dimensions");
        std::ofstream file(argv[6], std::ios::binary);
        file.write(reinterpret_cast<const char *>(data), std::streamsize(host.size() * sizeof(float)));
        if (!file) throw std::runtime_error("Could not write output array");
        std::cout << ",\"readback_ms\":" << readback_ms << ",\"history_device_sum_ms\":" << device_ms
                  << ",\"history_entries\":" << count << ",\"shape\":[" << size << ',' << size << ",15]}\n";
        // Normal process exit releases native resources; teardown is in parent wall time.
        return 0;
    } catch (const std::exception &e) {
        std::cerr << e.what() << '\n';
        return 1;
    }
}
