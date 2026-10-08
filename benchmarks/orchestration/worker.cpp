// Same request protocol and Metal render sequence as worker.py.
#include <mitsuba/core/bitmap.h>
#include <mitsuba/core/fresolver.h>
#include <mitsuba/core/logger.h>
#include <mitsuba/core/parser.h>
#include <mitsuba/core/profiler.h>
#include <mitsuba/core/util.h>
#include <mitsuba/render/integrator.h>
#include <mitsuba/render/scene.h>
#include <nanothread/nanothread.h>
#include <sys/mman.h>
#include <fcntl.h>
#include <unistd.h>
#include <atomic>
#include <chrono>
#include <cstring>
#include <iomanip>
#include <iostream>
#include <sstream>
#include <vector>

using namespace mitsuba;
using WorkerFloat = dr::MetalArray<float>;
using WorkerSpectrum = MuellerMatrix<Color<WorkerFloat, 3>>;
using WorkerScene = Scene<WorkerFloat, WorkerSpectrum>;
using Clock = std::chrono::steady_clock;
static double ms(Clock::time_point start) {
    return std::chrono::duration<double,std::milli>(Clock::now()-start).count();
}
int main(int argc, char **argv) {
    if (argc != 6 && argc != 7) return 2;
    try {
        const size_t size = std::stoul(argv[2]), spp = std::stoul(argv[3]);
        const size_t channels = std::stoul(argv[4]);
        const bool gpu_pack = argc == 7 && std::string(argv[6]) == "gpu";
        if (!size || !spp || (channels != 3 && channels != 15)) return 2;
        const size_t nbytes = size * size * channels * sizeof(float);
        int fd = open(argv[5], O_RDWR);
        if (fd < 0) throw std::runtime_error("Open shared buffer failed");
        void *shared = mmap(nullptr,nbytes,PROT_READ|PROT_WRITE,MAP_SHARED,fd,0);
        close(fd);
        if (shared == MAP_FAILED) throw std::runtime_error("mmap failed");
        Thread::static_initialization();
        Logger::static_initialization();
        Bitmap::static_initialization();
        Thread::thread()->logger()->set_log_level(Warn);
        jit_set_log_level_stderr(::LogLevel::Error);
        jit_init(1u << uint32_t(JitBackend::Metal));
        if (!jit_has_backend(JitBackend::Metal)) throw std::runtime_error("Metal unavailable; no CPU fallback");
        pool_set_size(nullptr,12);
        Profiler::static_initialization();
        color_management_static_initialization(false,false,true);
        WorkerScene::static_accel_initialization();
        file_resolver()->append(util::library_path().parent_path());
        parser::ParserConfig config("metal_rgb_polarized");
        auto state = parser::parse_file(config,argv[1],{{"size",std::to_string(size)}});
        parser::transform_all(config,state);
        auto objects = parser::instantiate(config,state);
        auto scene = objects.size()==1 ? dynamic_cast<WorkerScene *>(objects[0].get()) : nullptr;
        if (!scene) throw std::runtime_error("Expected one scene");
        auto sensor = scene->sensors().at(0);
        std::vector<float> packed(size*size*channels);
        WorkerFloat host_frame;
        const float *frame_data = nullptr;
        auto render = [&](uint32_t seed) {
            scene->integrator()->render(scene,sensor.get(),seed,uint32_t(spp),false,false,false);
            auto image = sensor->film()->develop(false);
            dr::eval(image.array());
            dr::sync_thread();
            return image;
        };
        auto readback = [&](const auto &image, double &readback_ms, double &pack_ms) {
            auto start = Clock::now();
            WorkerFloat device_frame = image.array();
            pack_ms = 0;
            if (channels == 3 && gpu_pack) {
                using UInt32 = dr::uint32_array_t<WorkerFloat>;
                auto indices = dr::arange<UInt32>(size*size*3);
                device_frame = dr::gather<WorkerFloat>(device_frame, (indices/3u)*15u + indices%3u);
                dr::eval(device_frame);
                dr::sync_thread();
                pack_ms = ms(start);
                start = Clock::now();
            }
            host_frame = dr::migrate(device_frame,JitBackend::None);
            dr::sync_thread();
            const float *data = host_frame.data();
            readback_ms = ms(start);
            start = Clock::now();
            if (channels == 15 || gpu_pack) frame_data = data;
            else {
                for (size_t i=0;i<size*size;++i)
                    memcpy(packed.data()+i*3,data+i*15,3*sizeof(float));
                frame_data = packed.data();
            }
            pack_ms += ms(start);
        };
        auto start = Clock::now();
        auto image = render(1);
        double first_ms = ms(start), unused_a, unused_b;
        readback(image,unused_a,unused_b);
        jit_set_flag(JitFlag::KernelHistory,true);
        auto begin = jit_kernel_history_begin();
        image = render(2);
        auto history = jit_kernel_history_end(begin);
        jit_set_flag(JitFlag::KernelHistory,false);
        size_t count = jit_kernel_history_size(history);
        if (!count) throw std::runtime_error("No Metal kernels");
        for (size_t i=0;i<count;++i)
            if (jit_kernel_history_query(history,i,KernelHistoryField::Backend) != uint64_t(JitBackend::Metal))
                throw std::runtime_error("Non-Metal kernel");
        jit_kernel_history_free(history);
        std::cout << std::setprecision(12) << "{\"ready\":true,\"first_render_ms\":" << first_ms
                  << ",\"bytes\":" << nbytes << ",\"variant\":\"metal_rgb_polarized\",\"history_entries\":" << count << "}\n" << std::flush;
        std::string line;
        while (std::getline(std::cin,line)) {
            std::istringstream request(line);
            std::string op, transport;
            uint32_t seed;
            request >> op;
            if (op=="quit") break;
            if (op=="ping") { std::cout << "{\"ok\":true}\n" << std::flush; continue; }
            double render_ms=0, readback_ms=0, pack_ms=0;
            if (op=="render") {
                request >> seed >> transport;
                start = Clock::now();
                image = render(seed);
                render_ms = ms(start);
                if (transport!="none") readback(image,readback_ms,pack_ms);
            } else if (op=="send") request >> transport;
            else throw std::runtime_error("Unknown command");
            if (transport!="none" && transport!="local" && transport!="pipe" && transport!="shared")
                throw std::runtime_error("Unknown transport");
            std::cout << "{\"render_ms\":" << render_ms << ",\"readback_ms\":" << readback_ms
                      << ",\"pack_ms\":" << pack_ms << ",\"bytes\":"
                      << ((transport=="pipe" || transport=="shared") ? nbytes : 0) << "}\n" << std::flush;
            start=Clock::now();
            if (transport=="pipe") {
                std::cout.write(reinterpret_cast<const char *>(frame_data),std::streamsize(nbytes));
                std::cout.flush();
            } else if (transport=="shared") {
                memcpy(shared,frame_data,nbytes);
                std::atomic_thread_fence(std::memory_order_release);
            }
            double transfer_ms=ms(start);
            std::cout << "{\"transfer_ms\":" << transfer_ms << "}\n" << std::flush;
        }
        munmap(shared,nbytes);
        return 0;
    } catch (const std::exception &e) { std::cerr << e.what() << '\n'; return 1; }
}
