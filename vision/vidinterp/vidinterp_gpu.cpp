// vidinterp-gpu - video frame interpolation with Lossless Scaling frame generation,
// with every frame staying on the GPU:
//
//   demux -> decode (Vulkan video, or CPU + hwupload) -> fps (CFR) -> scdet_vulkan
//     -> libplacebo to RGBA -> copy into lsfg-vk's source image -> LSFG
//     -> copy out of lsfg-vk's destination image -> libplacebo to NV12/P010
//     -> NVENC (via Vulkan->CUDA transfer) or a CPU encoder (via download)
//     -> mux (MP4/MKV file, or fragmented MP4 on stdout) together with the source audio.
//
// The output rate is a whole multiple of the input (-m) or any rate above it
// (--fps), each frame generated at its exact position between two source frames.
// That uses lsfg-vk's Context::acquireAt(), which is not upstream: it is our patch
// lsfg-vk-explicit-timestamp.patch, applied by install.sh at the pinned commit.
//
// Unlike vidinterp (the pipe version), this imports lsfg-vk's exported images and
// timeline semaphore into the VkDevice that FFmpeg itself decodes and filters on, so
// the only copies are GPU image-to-image copies. That is what makes 4K60 sources fit.
//
// Synchronisation:
//  - lsfg-vk's timeline semaphore follows the protocol in vidinterp.cpp (push()).
//  - FFmpeg frames (AVVkFrame) carry their own timeline semaphore per image: wait for
//    sem_value, signal sem_value + 1, update layout/access, all under lock_frame().
//  - Nothing here waits on the host for the GPU, except lsfg-vk's own dispatch() (which
//    waits for its previous iteration) and command-buffer reuse.

#define LSFGVK_EXPOSE_VULKAN 1
#include "lsfg-vk/lsfgvk.hpp"

extern "C" {
#include <libavcodec/avcodec.h>
#include <libavfilter/avfilter.h>
#include <libavfilter/buffersink.h>
#include <libavfilter/buffersrc.h>
#include <libavformat/avformat.h>
#include <libavutil/hwcontext.h>
#include <libavutil/hwcontext_vulkan.h>
#include <libavutil/opt.h>
#include <libavutil/parseutils.h>
#include <libavutil/pixdesc.h>
}

#include <algorithm>
#include <array>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <unistd.h>
#include <cstdlib>
#include <cstring>
#include <deque>
#include <functional>
#include <map>
#include <memory>
#include <numeric>
#include <stdexcept>
#include <string>
#include <type_traits>
#include <vector>

namespace {

// ---------------------------------------------------------------- options

struct Options {
    std::string input, output;
    uint32_t multiplier{2};
    // --fps: any output rate above the input's, instead of a whole multiple of it
    // (25 fps on a 120 Hz screen: 120, not 100). {0, 1} = rate x multiplier.
    AVRational outFps{0, 1};
    double start{0}, duration{0};
    float flow{1.0F};
    bool performance{false};
    bool fp16{true};
    std::string dll;
    double scene{10.0};
    int maxHeight{0};
    std::string encoder{"auto"};
    int quality{-1};
    std::string preset{"auto"};
    double maxrate{0};  // Mbit/s cap for streaming; 0 = no cap (quality target only)
    bool loop{false};   // endless stream: start over at the end (the player loops)
    // Decoder: auto = NVDEC (CUDA) where it can, else Vulkan video. FFmpeg 8.1's
    // Vulkan H.264 decode drifts on some streams (a 4K120 H.264 Main@5.1 file:
    // 639 of 720 frames wrong, correct only at IDRs); NVDEC is bit-exact there.
    std::string decode{"auto"};
    // Segment mode (--segment-dir): instead of one output, CMAF fragments of
    // segFrames SOURCE frames each - init.mp4 + seg_%05d.m4s, each written whole
    // (temp file + rename) - from segment startSeg up to endSeg (exclusive, -1 =
    // to the end). Every segment starts with a keyframe and carries absolute
    // timestamps, so segments made by different runs play as one timeline.
    std::string segDir;
    int segFrames{0};    // 0 = two seconds of source frames
    int startSeg{0};
    int endSeg{-1};
    bool verbose{false};
};

[[noreturn]] void usage(const char* argv0) {
    std::fprintf(stderr,
        "usage: %s -i INPUT -o OUTPUT|- [options]\n"
        "  -m N              multiplier (default 2)\n"
        "  --fps R           output frame rate instead of -m (e.g. 120 or 120000/1001), any\n"
        "                    rate above the input's: frames are made at their exact times\n"
        "  -s SECONDS        start position\n"
        "  -t SECONDS        duration\n"
        "  -f F              LSFG flow scale 0.25-1.0 (default 1.0)\n"
        "  -p                LSFG performance mode\n"
        "  --no-fp16         disable fp16 shaders\n"
        "  --scene T         scene-cut threshold, scdet scale (default 10, 0 = off)\n"
        "  --max-height H    downscale above this height\n"
        "  --encoder E       auto | av1_nvenc | hevc_nvenc | h264_nvenc | libx264 | libsvtav1\n"
        "  --quality Q       cq (nvenc) or crf (cpu encoders)\n"
        "  --preset P        NVENC preset p1 (fastest) .. p7, or auto (default: by output pixel rate)\n"
        "  --maxrate MBPS    cap the video bitrate (Mbit/s); 0 = no cap (default)\n"
        "  --loop            at the end, continue from the beginning (endless stream)\n"
        "  --decode D        auto (NVDEC if possible) | cuda | vulkan | cpu (default auto)\n"
        "  --segment-dir D   write CMAF segments (init.mp4, seg_NNNNN.m4s, meta.json) to D\n"
        "                    instead of -o; with --segment-frames N (source frames per\n"
        "                    segment, default 2 s), --start-segment K, --end-segment E\n"
        "  -d PATH           lsfg-vk.dll (default $LSFGVK_DLL_PATH)\n"
        "  -v                verbose\n", argv0);
    std::exit(2);
}

Options parse(int argc, char** argv) {
    Options o;
    if (const char* e = std::getenv("LSFGVK_DLL_PATH")) o.dll = e;
    for (int i = 1; i < argc; i++) {
        const std::string a{argv[i]};
        auto next = [&]() -> std::string { if (i + 1 >= argc) usage(argv[0]); return argv[++i]; };
        if (a == "-i") o.input = next();
        else if (a == "-o") o.output = next();
        else if (a == "-m") o.multiplier = std::stoul(next());
        else if (a == "--fps") {
            const std::string v{next()};
            if (av_parse_ratio(&o.outFps, v.c_str(), 1000000, 0, nullptr) < 0 || o.outFps.num <= 0
                    || o.outFps.den <= 0)
                usage(argv[0]);
        }
        else if (a == "-s") o.start = std::stod(next());
        else if (a == "-t") o.duration = std::stod(next());
        else if (a == "-f") o.flow = std::stof(next());
        else if (a == "-p") o.performance = true;
        else if (a == "--no-fp16") o.fp16 = false;
        else if (a == "--scene") o.scene = std::stod(next());
        else if (a == "--max-height") o.maxHeight = std::stoi(next());
        else if (a == "--encoder") o.encoder = next();
        else if (a == "--quality") o.quality = std::stoi(next());
        else if (a == "--preset") o.preset = next();
        else if (a == "--maxrate") o.maxrate = std::stod(next());
        else if (a == "--loop") o.loop = true;
        else if (a == "--decode") o.decode = next();
        else if (a == "--segment-dir") o.segDir = next();
        else if (a == "--segment-frames") o.segFrames = std::stoi(next());
        else if (a == "--start-segment") o.startSeg = std::stoi(next());
        else if (a == "--end-segment") o.endSeg = std::stoi(next());
        else if (a == "-d") o.dll = next();
        else if (a == "-v") o.verbose = true;
        else usage(argv[0]);
    }
    if (!o.segDir.empty()) {
        o.output = o.segDir;             // for the checks below; segments go there
        o.loop = false;                  // segments cover the file once; the player loops
        if (o.startSeg < 0 || (o.endSeg >= 0 && o.endSeg <= o.startSeg)) usage(argv[0]);
    }
    if (o.input.empty() || o.output.empty() || o.dll.empty() || (o.outFps.num == 0 && o.multiplier < 2)
            || o.flow < 0.25F || o.flow > 1.0F)
        usage(argv[0]);
    return o;
}

// ---------------------------------------------------------------- helpers

bool g_trace{std::getenv("VIDINTERP_TRACE") != nullptr};
#define TRACE(...) do { if (g_trace) { std::fprintf(stderr, "trace: " __VA_ARGS__); std::fputc(10, stderr); } } while (0)

std::string averr(int e) {
    char buf[AV_ERROR_MAX_STRING_SIZE]{};
    av_strerror(e, buf, sizeof buf);
    return buf;
}

int ck(int ret, const char* what) {
    if (ret < 0) throw std::runtime_error(std::string(what) + ": " + averr(ret));
    return ret;
}

void vkck(VkResult r, const char* what) {
    if (r != VK_SUCCESS)
        throw std::runtime_error(std::string(what) + " failed: VkResult " + std::to_string(r));
}

struct FrameDel { void operator()(AVFrame* f) const { av_frame_free(&f); } };
using FramePtr = std::unique_ptr<AVFrame, FrameDel>;
FramePtr allocFrame() {
    AVFrame* f{av_frame_alloc()};
    if (!f) throw std::bad_alloc();
    return FramePtr{f};
}

// ---------------------------------------------------------------- Vulkan functions

/// The device functions this program calls, loaded from FFmpeg's device.
struct VkFns {
#define VKFN(name) PFN_vk##name name{};
    VKFN(GetPhysicalDeviceProperties)
    VKFN(GetPhysicalDeviceMemoryProperties)
    VKFN(GetDeviceQueue)
    VKFN(CreateImage)
    VKFN(GetImageMemoryRequirements)
    VKFN(AllocateMemory)
    VKFN(BindImageMemory)
    VKFN(CreateSemaphore)
    VKFN(ImportSemaphoreFdKHR)
    VKFN(CreateCommandPool)
    VKFN(AllocateCommandBuffers)
    VKFN(ResetCommandBuffer)
    VKFN(BeginCommandBuffer)
    VKFN(EndCommandBuffer)
    VKFN(CmdPipelineBarrier2)
    VKFN(CmdCopyImage)
    VKFN(QueueSubmit)
    VKFN(WaitSemaphores)
    VKFN(GetSemaphoreCounterValue)
#undef VKFN

    void load(const AVVulkanDeviceContext* h) {
        auto gipa{h->get_proc_addr};
        auto gdpa{reinterpret_cast<PFN_vkGetDeviceProcAddr>(gipa(h->inst, "vkGetDeviceProcAddr"))};
        auto inst = [&](const char* n) {
            auto p{gipa(h->inst, n)};
            if (!p) throw std::runtime_error(std::string("missing ") + n);
            return p;
        };
        auto dev = [&](const char* n) {
            auto p{gdpa(h->act_dev, n)};
            if (!p) throw std::runtime_error(std::string("missing ") + n);
            return p;
        };
#define INST(name) name = reinterpret_cast<PFN_vk##name>(inst("vk" #name));
#define DEV(name) name = reinterpret_cast<PFN_vk##name>(dev("vk" #name));
        INST(GetPhysicalDeviceProperties)
        INST(GetPhysicalDeviceMemoryProperties)
        DEV(GetDeviceQueue)
        DEV(CreateImage)
        DEV(GetImageMemoryRequirements)
        DEV(AllocateMemory)
        DEV(BindImageMemory)
        DEV(CreateSemaphore)
        DEV(ImportSemaphoreFdKHR)
        DEV(CreateCommandPool)
        DEV(AllocateCommandBuffers)
        DEV(ResetCommandBuffer)
        DEV(BeginCommandBuffer)
        DEV(EndCommandBuffer)
        DEV(CmdPipelineBarrier2)
        DEV(CmdCopyImage)
        DEV(QueueSubmit)
        DEV(WaitSemaphores)
        DEV(GetSemaphoreCounterValue)
#undef INST
#undef DEV
    }
};

// ---------------------------------------------------------------- LSFG stage

/// Owns the lsfg-vk context and the pool of RGBA frames generated frames are copied into.
class LsfgStage {
public:
    LsfgStage(AVBufferRef* hwdev, uint32_t w, uint32_t h, bool hbd, const Options& o)
            : m_o(o), m_w(w), m_h(h) {
        m_devCtx = reinterpret_cast<AVHWDeviceContext*>(hwdev->data);
        m_hw = static_cast<AVVulkanDeviceContext*>(m_devCtx->hwctx);
        m_vk.load(m_hw);
        m_dev = m_hw->act_dev;

        VkPhysicalDeviceProperties props;
        m_vk.GetPhysicalDeviceProperties(m_hw->phys_dev, &props);
        m_vk.GetPhysicalDeviceMemoryProperties(m_hw->phys_dev, &m_memProps);

        // A queue family FFmpeg created that can do transfers (compute implies transfer).
        for (int i = 0; i < m_hw->nb_qf; i++) {
            if (m_hw->qf[i].flags & (VK_QUEUE_COMPUTE_BIT | VK_QUEUE_GRAPHICS_BIT)) {
                m_qfi = uint32_t(m_hw->qf[i].idx);
                break;
            }
        }
        if (m_qfi == UINT32_MAX) throw std::runtime_error("no compute queue in FFmpeg's device");
        m_vk.GetDeviceQueue(m_dev, m_qfi, 0, &m_queue);

        const VkFormat fmt{hbd ? VK_FORMAT_A2B10G10R10_UNORM_PACK32 : VK_FORMAT_R8G8B8A8_UNORM};
        std::function<void(const std::string&)> log;
        if (o.verbose) log = [](const std::string& s) { std::fprintf(stderr, "lsfg-vk: %s\n", s.c_str()); };
        m_lsfg = std::make_unique<lsfgvk::Instance>(props.deviceName, o.dll, o.fp16, false, log);
        m_ctx = std::make_unique<lsfgvk::Context>(*m_lsfg, w, h, o.flow, o.performance,
            hbd ? lsfgvk::BitDepth::A2B10G10R10 : lsfgvk::BitDepth::R8G8B8A8);
        const auto fds{m_ctx->exportFds()};
        const VkImageUsageFlags common{VK_IMAGE_USAGE_STORAGE_BIT | VK_IMAGE_USAGE_SAMPLED_BIT};
        m_src = importImage(fds.sourceFd, fmt, 2, common | VK_IMAGE_USAGE_TRANSFER_DST_BIT);
        m_dst = importImage(fds.destinationFd, fmt, 1, common | VK_IMAGE_USAGE_TRANSFER_SRC_BIT);
        m_sync = importTimeline(fds.syncFd);
        m_mine = timeline();

        VkCommandPoolCreateInfo pci{VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO};
        pci.flags = VK_COMMAND_POOL_CREATE_RESET_COMMAND_BUFFER_BIT;
        pci.queueFamilyIndex = m_qfi;
        vkck(m_vk.CreateCommandPool(m_dev, &pci, nullptr, &m_pool), "vkCreateCommandPool");
        for (auto& c : m_cmds) {
            VkCommandBufferAllocateInfo ai{VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO};
            ai.commandPool = m_pool;
            ai.level = VK_COMMAND_BUFFER_LEVEL_PRIMARY;
            ai.commandBufferCount = 1;
            vkck(m_vk.AllocateCommandBuffers(m_dev, &ai, &c.cb), "vkAllocateCommandBuffers");
        }

        // Pool of output frames (RGBA / X2BGR10) that generated and passed-through
        // frames are copied into before they go to the output filter graph.
        m_outFrames = av_hwframe_ctx_alloc(hwdev);
        auto* fc{reinterpret_cast<AVHWFramesContext*>(m_outFrames->data)};
        fc->format = AV_PIX_FMT_VULKAN;
        fc->sw_format = hbd ? AV_PIX_FMT_X2BGR10 : AV_PIX_FMT_RGBA;
        fc->width = int(w);
        fc->height = int(h);
        auto* vfc{static_cast<AVVulkanFramesContext*>(fc->hwctx)};
        vfc->tiling = VK_IMAGE_TILING_OPTIMAL;
        vfc->usage = VkImageUsageFlagBits(VK_IMAGE_USAGE_TRANSFER_DST_BIT | VK_IMAGE_USAGE_TRANSFER_SRC_BIT
            | VK_IMAGE_USAGE_SAMPLED_BIT | VK_IMAGE_USAGE_STORAGE_BIT);
        ck(av_hwframe_ctx_init(m_outFrames), "output frames context");

        // Our aliases of the imported images start UNDEFINED; move them to GENERAL once.
        Cmd& c{begin()};
        for (uint32_t l = 0; l < 2; l++) lsfgBarrier(c.cb, m_src, l, true, 0, VK_IMAGE_LAYOUT_UNDEFINED);
        lsfgBarrier(c.cb, m_dst, 0, true, 0, VK_IMAGE_LAYOUT_UNDEFINED);
        submit(c, {}, {});
    }

    ~LsfgStage() { av_buffer_unref(&m_outFrames); }

    AVBufferRef* outFrames() const { return m_outFrames; }

    /// Copy a frame (any Vulkan RGBA frame of our size) into a new output-pool frame.
    FramePtr copy(const AVFrame* src) {
        FramePtr out{newOutFrame()};
        FrameLock s{src}, d{out.get()};
        Cmd& c{begin()};
        frameBarrier(c.cb, s, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL, VK_ACCESS_2_TRANSFER_READ_BIT);
        frameBarrier(c.cb, d, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, VK_ACCESS_2_TRANSFER_WRITE_BIT);
        copyImage(c.cb, s.vkf->img[0], VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL, 0,
            d.vkf->img[0], VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, 0);
        // Waits before signals: signal() increments sem_value, and the order in which
        // function arguments are evaluated is unspecified (GCC goes right to left).
        const Sem sw{s.wait()}, dw{d.wait()};
        const Sem ss{s.signal()}, ds{d.signal()};
        submit(c, {sw, dw}, {ss, ds});
        return out;
    }

    /// Feed `src` as the next source frame and generate one frame between the
    /// previous source and it for every entry of `at`: how far along, 0 = the
    /// previous frame, 1 = this one. Generated frames go to `sink`. With `discard`
    /// (or nothing in `at`) one frame is generated and dropped, which only keeps
    /// LSFG's pairs in step.
    /// Value protocol on lsfg-vk's timeline semaphore: see vidinterp.cpp, push().
    /// The explicit positions need lsfg-vk's acquireAt(), which is OUR patch
    /// (lsfg-vk-explicit-timestamp.patch, applied by install.sh), not upstream.
    template<typename Sink>
    void push(const AVFrame* src, const std::vector<float>& at, bool discard, Sink&& sink) {
        if (at.empty()) discard = true;
        const uint32_t total{discard ? 1U : uint32_t(at.size())};
        const uint32_t layer{m_iteration % 2};
        {
            FrameLock s{src};
            Cmd& c{begin()};
            frameBarrier(c.cb, s, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL, VK_ACCESS_2_TRANSFER_READ_BIT);
            lsfgBarrier(c.cb, m_src, layer, true, VK_ACCESS_2_TRANSFER_WRITE_BIT, VK_IMAGE_LAYOUT_GENERAL);
            copyImage(c.cb, s.vkf->img[0], VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL, 0,
                m_src, VK_IMAGE_LAYOUT_GENERAL, layer);
            lsfgBarrier(c.cb, m_src, layer, false, VK_ACCESS_2_TRANSFER_WRITE_BIT, VK_IMAGE_LAYOUT_GENERAL);
            // Wait for the previous iteration to be fully done, then start this one.
            const Sem sw{s.wait()};
            const Sem ss{s.signal()};
            submit(c, {sw, {m_sync, m_syncValue}}, {ss, {m_sync, m_syncValue + 1}});
            m_syncValue++;
        }

        TRACE("push: dispatch(%u) sync=%lu", total, (unsigned long)m_syncValue);
        m_ctx->dispatch(total);
        TRACE("push: dispatched");
        for (uint32_t k = 0; k < total; k++) {
            if (discard) m_ctx->acquire();
            else m_ctx->acquireAt(at[k]);
            TRACE("push: acquired %u", k);
            const uint64_t generated{++m_syncValue};
            const bool last{k + 1 == total};
            if (discard) {
                if (!last) {
                    Cmd& c{begin()};
                    submit(c, {{m_sync, generated}}, {{m_sync, ++m_syncValue}});
                }
                continue;
            }
            FramePtr out{newOutFrame()};
            {
                FrameLock d{out.get()};
                Cmd& c{begin()};
                lsfgBarrier(c.cb, m_dst, 0, true, VK_ACCESS_2_TRANSFER_READ_BIT, VK_IMAGE_LAYOUT_GENERAL);
                frameBarrier(c.cb, d, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, VK_ACCESS_2_TRANSFER_WRITE_BIT);
                copyImage(c.cb, m_dst, VK_IMAGE_LAYOUT_GENERAL, 0,
                    d.vkf->img[0], VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, 0);
                lsfgBarrier(c.cb, m_dst, 0, false, VK_ACCESS_2_TRANSFER_READ_BIT, VK_IMAGE_LAYOUT_GENERAL);
                const Sem dw{d.wait()};
                const Sem ds{d.signal()};
                if (last) {
                    submit(c, {{m_sync, generated}, dw}, {ds});
                } else {
                    submit(c, {{m_sync, generated}, dw}, {ds, {m_sync, generated + 1}});
                    m_syncValue++;
                }
            }
            sink(std::move(out));
        }
        m_iteration++;
    }

    void finish() {
        m_ctx->idle();
        wait(m_sync, m_syncValue);
        wait(m_mine, m_mineValue);
    }

private:
    struct Sem { VkSemaphore s; uint64_t v; };

    /// lock_frame() for the lifetime of a submission, plus the frame's semaphore values.
    struct FrameLock {
        AVHWFramesContext* fc;
        AVVulkanFramesContext* vfc;
        AVVkFrame* vkf;
        explicit FrameLock(const AVFrame* f) {
            fc = reinterpret_cast<AVHWFramesContext*>(f->hw_frames_ctx->data);
            vfc = static_cast<AVVulkanFramesContext*>(fc->hwctx);
            vkf = reinterpret_cast<AVVkFrame*>(f->data[0]);
            vfc->lock_frame(fc, vkf);
        }
        ~FrameLock() { vfc->unlock_frame(fc, vkf); }
        Sem wait() const { return {vkf->sem[0], vkf->sem_value[0]}; }
        Sem signal() { return {vkf->sem[0], ++vkf->sem_value[0]}; }
        FrameLock(const FrameLock&) = delete;
        FrameLock& operator=(const FrameLock&) = delete;
    };

    struct Cmd { VkCommandBuffer cb{}; uint64_t done{0}; };

    Cmd& begin() {
        Cmd& c{m_cmds[m_cmdNext++ % m_cmds.size()]};
        if (c.done) wait(m_mine, c.done);  // its previous submission has finished
        vkck(m_vk.ResetCommandBuffer(c.cb, 0), "vkResetCommandBuffer");
        VkCommandBufferBeginInfo bi{VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO};
        bi.flags = VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT;
        vkck(m_vk.BeginCommandBuffer(c.cb, &bi), "vkBeginCommandBuffer");
        return c;
    }

    void submit(Cmd& c, std::initializer_list<Sem> waits, std::initializer_list<Sem> signals) {
        vkck(m_vk.EndCommandBuffer(c.cb), "vkEndCommandBuffer");
        std::vector<VkSemaphore> ws, ss;
        std::vector<uint64_t> wv, sv;
        std::vector<VkPipelineStageFlags> stages;
        for (const auto& w : waits) {
            ws.push_back(w.s); wv.push_back(w.v); stages.push_back(VK_PIPELINE_STAGE_ALL_COMMANDS_BIT);
        }
        for (const auto& s : signals) { ss.push_back(s.s); sv.push_back(s.v); }
        c.done = ++m_mineValue;
        ss.push_back(m_mine);
        sv.push_back(c.done);
        VkTimelineSemaphoreSubmitInfo tsi{VK_STRUCTURE_TYPE_TIMELINE_SEMAPHORE_SUBMIT_INFO};
        tsi.waitSemaphoreValueCount = uint32_t(wv.size());
        tsi.pWaitSemaphoreValues = wv.data();
        tsi.signalSemaphoreValueCount = uint32_t(sv.size());
        tsi.pSignalSemaphoreValues = sv.data();
        VkSubmitInfo si{VK_STRUCTURE_TYPE_SUBMIT_INFO};
        si.pNext = &tsi;
        si.waitSemaphoreCount = uint32_t(ws.size());
        si.pWaitSemaphores = ws.data();
        si.pWaitDstStageMask = stages.data();
        si.commandBufferCount = 1;
        si.pCommandBuffers = &c.cb;
        si.signalSemaphoreCount = uint32_t(ss.size());
        si.pSignalSemaphores = ss.data();
        // FFmpeg shares its queues between threads; take its lock around the submit.
#pragma GCC diagnostic push
#pragma GCC diagnostic ignored "-Wdeprecated-declarations"
        if (m_hw->lock_queue) m_hw->lock_queue(m_devCtx, m_qfi, 0);
        const VkResult r{m_vk.QueueSubmit(m_queue, 1, &si, VK_NULL_HANDLE)};
        if (m_hw->unlock_queue) m_hw->unlock_queue(m_devCtx, m_qfi, 0);
#pragma GCC diagnostic pop
        vkck(r, "vkQueueSubmit");
        if (g_trace) {  // debug: does this submission finish, and if not, which wait is unmet?
            VkSemaphoreWaitInfo wi{VK_STRUCTURE_TYPE_SEMAPHORE_WAIT_INFO};
            wi.semaphoreCount = 1; wi.pSemaphores = &m_mine; wi.pValues = &c.done;
            if (m_vk.WaitSemaphores(m_dev, &wi, 1000000000ULL) != VK_SUCCESS) {
                TRACE("submit %lu STUCK", (unsigned long)c.done);
                for (size_t i = 0; i < ws.size(); i++) {
                    uint64_t v{};
                    m_vk.GetSemaphoreCounterValue(m_dev, ws[i], &v);
                    TRACE("  wait %zu: %s counter %lu, needs %lu", i,
                        ws[i] == m_sync ? "lsfg-sync" : "frame-sem", (unsigned long)v, (unsigned long)wv[i]);
                }
            }
        }
    }

    void wait(VkSemaphore s, uint64_t v) {
        VkSemaphoreWaitInfo wi{VK_STRUCTURE_TYPE_SEMAPHORE_WAIT_INFO};
        wi.semaphoreCount = 1;
        wi.pSemaphores = &s;
        wi.pValues = &v;
        vkck(m_vk.WaitSemaphores(m_dev, &wi, UINT64_MAX), "vkWaitSemaphores");
    }

    FramePtr newOutFrame() {
        FramePtr f{allocFrame()};
        ck(av_hwframe_get_buffer(m_outFrames, f.get(), 0), "output frame");
        return f;
    }

    /// Barrier on an FFmpeg frame image; records the new state in the AVVkFrame.
    /// FFmpeg creates its images with concurrent sharing, so no ownership transfer.
    void frameBarrier(VkCommandBuffer cb, FrameLock& f, VkImageLayout layout, VkAccessFlags2 access) {
        VkImageMemoryBarrier2 b{VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER_2};
        b.srcStageMask = VK_PIPELINE_STAGE_2_ALL_COMMANDS_BIT;
        b.srcAccessMask = VkAccessFlags2(f.vkf->access[0]);
        b.dstStageMask = VK_PIPELINE_STAGE_2_TRANSFER_BIT;
        b.dstAccessMask = access;
        b.oldLayout = f.vkf->layout[0];
        b.newLayout = layout;
        b.srcQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
        b.dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
        b.image = f.vkf->img[0];
        b.subresourceRange = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1};
        VkDependencyInfo di{VK_STRUCTURE_TYPE_DEPENDENCY_INFO};
        di.imageMemoryBarrierCount = 1;
        di.pImageMemoryBarriers = &b;
        m_vk.CmdPipelineBarrier2(cb, &di);
        f.vkf->layout[0] = layout;
        // FFmpeg 8.x declares this as VkAccessFlagBits, newer versions as VkAccessFlagBits2;
        // the transfer bits used here have the same values in both.
        f.vkf->access[0] = static_cast<std::remove_reference_t<decltype(f.vkf->access[0])>>(access);
    }

    /// Ownership transfer of an lsfg-vk image layer from/to VK_QUEUE_FAMILY_EXTERNAL.
    void lsfgBarrier(VkCommandBuffer cb, VkImage img, uint32_t layer, bool acquire,
            VkAccessFlags2 access, VkImageLayout oldLayout) {
        VkImageMemoryBarrier2 b{VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER_2};
        b.srcStageMask = acquire ? VK_PIPELINE_STAGE_2_NONE : VK_PIPELINE_STAGE_2_TRANSFER_BIT;
        b.srcAccessMask = acquire ? 0 : access;
        b.dstStageMask = acquire ? VK_PIPELINE_STAGE_2_TRANSFER_BIT : VK_PIPELINE_STAGE_2_NONE;
        b.dstAccessMask = acquire ? access : 0;
        b.oldLayout = oldLayout;
        b.newLayout = VK_IMAGE_LAYOUT_GENERAL;
        b.srcQueueFamilyIndex = acquire ? VK_QUEUE_FAMILY_EXTERNAL : m_qfi;
        b.dstQueueFamilyIndex = acquire ? m_qfi : VK_QUEUE_FAMILY_EXTERNAL;
        b.image = img;
        b.subresourceRange = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, layer, 1};
        VkDependencyInfo di{VK_STRUCTURE_TYPE_DEPENDENCY_INFO};
        di.imageMemoryBarrierCount = 1;
        di.pImageMemoryBarriers = &b;
        m_vk.CmdPipelineBarrier2(cb, &di);
    }

    void copyImage(VkCommandBuffer cb, VkImage src, VkImageLayout srcLayout, uint32_t srcLayer,
            VkImage dst, VkImageLayout dstLayout, uint32_t dstLayer) {
        VkImageCopy r{};
        r.srcSubresource = {VK_IMAGE_ASPECT_COLOR_BIT, 0, srcLayer, 1};
        r.dstSubresource = {VK_IMAGE_ASPECT_COLOR_BIT, 0, dstLayer, 1};
        r.extent = {m_w, m_h, 1};
        m_vk.CmdCopyImage(cb, src, srcLayout, dst, dstLayout, 1, &r);
    }

    VkImage importImage(int fd, VkFormat fmt, uint32_t layers, VkImageUsageFlags usage) {
        VkExternalMemoryImageCreateInfo ext{VK_STRUCTURE_TYPE_EXTERNAL_MEMORY_IMAGE_CREATE_INFO};
        ext.handleTypes = VK_EXTERNAL_MEMORY_HANDLE_TYPE_OPAQUE_FD_BIT;
        VkImageCreateInfo ici{VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO};
        ici.pNext = &ext;
        ici.imageType = VK_IMAGE_TYPE_2D;
        ici.format = fmt;
        ici.extent = {m_w, m_h, 1};
        ici.mipLevels = 1;
        ici.arrayLayers = layers;
        ici.samples = VK_SAMPLE_COUNT_1_BIT;
        ici.tiling = VK_IMAGE_TILING_OPTIMAL;
        ici.usage = usage;
        ici.sharingMode = VK_SHARING_MODE_EXCLUSIVE;
        ici.initialLayout = VK_IMAGE_LAYOUT_UNDEFINED;
        VkImage img{};
        vkck(m_vk.CreateImage(m_dev, &ici, nullptr, &img), "vkCreateImage(import)");
        VkMemoryRequirements req;
        m_vk.GetImageMemoryRequirements(m_dev, img, &req);
        VkMemoryDedicatedAllocateInfo ded{VK_STRUCTURE_TYPE_MEMORY_DEDICATED_ALLOCATE_INFO};
        ded.image = img;
        VkImportMemoryFdInfoKHR imp{VK_STRUCTURE_TYPE_IMPORT_MEMORY_FD_INFO_KHR};
        imp.pNext = &ded;
        imp.handleType = VK_EXTERNAL_MEMORY_HANDLE_TYPE_OPAQUE_FD_BIT;
        imp.fd = fd;
        VkMemoryAllocateInfo mai{VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO};
        mai.pNext = &imp;
        mai.allocationSize = req.size;
        mai.memoryTypeIndex = UINT32_MAX;
        for (uint32_t i = 0; i < m_memProps.memoryTypeCount; i++)
            if ((req.memoryTypeBits & (1U << i))
                    && (m_memProps.memoryTypes[i].propertyFlags & VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT)) {
                mai.memoryTypeIndex = i;
                break;
            }
        if (mai.memoryTypeIndex == UINT32_MAX) throw std::runtime_error("no device-local memory type");
        VkDeviceMemory mem{};
        vkck(m_vk.AllocateMemory(m_dev, &mai, nullptr, &mem), "vkAllocateMemory(import)");
        vkck(m_vk.BindImageMemory(m_dev, img, mem, 0), "vkBindImageMemory(import)");
        return img;
    }

    VkSemaphore timeline() {
        VkSemaphoreTypeCreateInfo tci{VK_STRUCTURE_TYPE_SEMAPHORE_TYPE_CREATE_INFO};
        tci.semaphoreType = VK_SEMAPHORE_TYPE_TIMELINE;
        VkSemaphoreCreateInfo sci{VK_STRUCTURE_TYPE_SEMAPHORE_CREATE_INFO};
        sci.pNext = &tci;
        VkSemaphore s{};
        vkck(m_vk.CreateSemaphore(m_dev, &sci, nullptr, &s), "vkCreateSemaphore");
        return s;
    }

    VkSemaphore importTimeline(int fd) {
        VkSemaphore s{timeline()};
        VkImportSemaphoreFdInfoKHR imp{VK_STRUCTURE_TYPE_IMPORT_SEMAPHORE_FD_INFO_KHR};
        imp.semaphore = s;
        imp.handleType = VK_EXTERNAL_SEMAPHORE_HANDLE_TYPE_OPAQUE_FD_BIT;
        imp.fd = fd;
        vkck(m_vk.ImportSemaphoreFdKHR(m_dev, &imp), "vkImportSemaphoreFdKHR");
        return s;
    }

    const Options& m_o;
    uint32_t m_w, m_h;
    AVHWDeviceContext* m_devCtx{};
    AVVulkanDeviceContext* m_hw{};
    VkFns m_vk;
    VkDevice m_dev{};
    VkPhysicalDeviceMemoryProperties m_memProps{};
    uint32_t m_qfi{UINT32_MAX};
    VkQueue m_queue{};
    std::unique_ptr<lsfgvk::Instance> m_lsfg;
    std::unique_ptr<lsfgvk::Context> m_ctx;
    VkImage m_src{}, m_dst{};
    VkSemaphore m_sync{}, m_mine{};
    uint64_t m_syncValue{0}, m_mineValue{0};
    VkCommandPool m_pool{};
    std::array<Cmd, 64> m_cmds{};
    uint64_t m_cmdNext{0};
    AVBufferRef* m_outFrames{};
    uint32_t m_iteration{0};
};

// ---------------------------------------------------------------- segment files

/// Where the muxer writes in segment mode: memory, cut into files by the caller.
struct MemSink {
    std::vector<uint8_t> data;
    static int write(void* opaque, const uint8_t* buf, int size) {
        auto* s{static_cast<MemSink*>(opaque)};
        s->data.insert(s->data.end(), buf, buf + size);
        return size;
    }
};

/// Written under a temporary name and renamed, so a reader never sees half a file.
void writeFileAtomic(const std::string& path, const void* data, size_t size) {
    const std::string tmp{path + ".tmp" + std::to_string(::getpid())};
    FILE* f{std::fopen(tmp.c_str(), "wb")};
    if (!f) throw std::runtime_error("cannot write " + tmp);
    const bool ok{std::fwrite(data, 1, size, f) == size};
    if (std::fclose(f) != 0 || !ok) { std::remove(tmp.c_str()); throw std::runtime_error("cannot write " + tmp); }
    if (std::rename(tmp.c_str(), path.c_str()) != 0) throw std::runtime_error("cannot rename to " + path);
}

/// RFC 6381 codec string for Media Source Extensions, from the AV1 encoder's av1C
/// record ("av01.<profile>.<level><tier>.<bitdepth>").
std::string av1CodecString(const AVCodecContext* enc) {
    if (enc->extradata_size >= 4 && enc->extradata[0] == 0x81) {
        const uint8_t b1{enc->extradata[1]}, b2{enc->extradata[2]};
        const int depth{(b2 & 0x20) ? 12 : (b2 & 0x40) ? 10 : 8};
        char s[32];
        std::snprintf(s, sizeof s, "av01.%d.%02d%c.%02d", b1 >> 5, b1 & 0x1f, (b2 & 0x80) ? 'H' : 'M', depth);
        return s;
    }
    return "av01.0.08M.08";   // a safe claim for anything up to 1080p60 8-bit
}

/// The audio codecs browsers take in fragmented MP4, as codec strings.
std::string audioCodecString(const AVCodecParameters* p) {
    switch (p->codec_id) {
        case AV_CODEC_ID_OPUS: return "opus";
        case AV_CODEC_ID_AAC: return p->profile == AV_PROFILE_AAC_HE || p->profile == AV_PROFILE_AAC_HE_V2
            ? "mp4a.40.5" : "mp4a.40.2";
        case AV_CODEC_ID_MP3: return "mp4a.6B";
        case AV_CODEC_ID_FLAC: return "flac";
        default: return "";
    }
}

// ---------------------------------------------------------------- filter graphs

struct Graph {
    AVFilterGraph* g{};
    AVFilterContext* src{};
    AVFilterContext* sink{};
    AVFilterContext* det{};   // second output labelled [det], when the chain has one
    ~Graph() { avfilter_graph_free(&g); }

    /// Build "buffer -> <chain> -> buffersink". Every filter gets the Vulkan device,
    /// so hwupload/libplacebo/scdet_vulkan run on it instead of creating their own.
    /// A chain may end in two outputs, [out] and [det]; each gets its own sink.
    void build(const std::string& chain, AVBufferRef* hwdev, int w, int h, AVPixelFormat fmt,
            AVBufferRef* hwFrames, AVRational tb, AVRational fr, AVRational sar) {
        g = avfilter_graph_alloc();
        src = avfilter_graph_alloc_filter(g, avfilter_get_by_name("buffer"), "in");
        AVBufferSrcParameters* p{av_buffersrc_parameters_alloc()};
        p->format = fmt;
        p->width = w;
        p->height = h;
        p->time_base = tb;
        p->frame_rate = fr;
        p->sample_aspect_ratio = sar.num ? sar : AVRational{1, 1};
        p->hw_frames_ctx = hwFrames;
        ck(av_buffersrc_parameters_set(src, p), "buffersrc parameters");
        av_free(p);
        ck(avfilter_init_str(src, nullptr), "buffersrc init");
        sink = avfilter_graph_alloc_filter(g, avfilter_get_by_name("buffersink"), "out");
        ck(avfilter_init_str(sink, nullptr), "buffersink init");

        // The segment API, because hw_device_ctx must be set between creating a filter
        // and initialising it (hwupload checks it in init).
        AVFilterGraphSegment* seg{};
        ck(avfilter_graph_segment_parse(g, chain.c_str(), 0, &seg), "filter chain");
        ck(avfilter_graph_segment_create_filters(seg, 0), "create filters");
        for (size_t c = 0; c < seg->nb_chains; c++)
            for (size_t f = 0; f < seg->chains[c]->nb_filters; f++)
                if (AVFilterContext* fc = seg->chains[c]->filters[f]->filter)
                    fc->hw_device_ctx = av_buffer_ref(hwdev);
        ck(avfilter_graph_segment_apply_opts(seg, 0), "filter options");
        ck(avfilter_graph_segment_init(seg, 0), "filter init");
        AVFilterInOut* ins{};
        AVFilterInOut* outs{};
        ck(avfilter_graph_segment_link(seg, 0, &ins, &outs), "filter link");
        avfilter_graph_segment_free(&seg);
        // One open input (the buffer source); one or two open outputs.
        if (!ins || ins->next || !outs) throw std::runtime_error("filter chain needs one input and an output");
        ck(avfilter_link(src, 0, ins->filter_ctx, unsigned(ins->pad_idx)), "link input");
        for (AVFilterInOut* o = outs; o; o = o->next) {
            AVFilterContext* target{sink};
            if (o->name && std::string(o->name) == "det") {
                det = avfilter_graph_alloc_filter(g, avfilter_get_by_name("buffersink"), "det");
                ck(avfilter_init_str(det, nullptr), "buffersink init (det)");
                target = det;
            }
            ck(avfilter_link(o->filter_ctx, unsigned(o->pad_idx), target, 0), "link output");
        }
        avfilter_inout_free(&ins);
        avfilter_inout_free(&outs);
        ck(avfilter_graph_config(g, nullptr), "filter graph config");
    }
};

// ---------------------------------------------------------------- main pipeline

/// get_format: the hardware format asked for in dec->opaque (CUDA or Vulkan), else
/// software decoding (the graph uploads).
enum AVPixelFormat pickHw(AVCodecContext* c, const enum AVPixelFormat* fmts) {
    const auto want{static_cast<AVPixelFormat>(reinterpret_cast<intptr_t>(c->opaque))};
    for (const auto* p = fmts; *p != AV_PIX_FMT_NONE; p++)
        if (*p == want) return *p;
    for (const auto* p = fmts; *p != AV_PIX_FMT_NONE; p++)
        if (!(av_pix_fmt_desc_get(*p)->flags & AV_PIX_FMT_FLAG_HWACCEL)) return *p;
    return fmts[0];
}

/// A decoder for `id` that can decode on `type` through a device context.
const AVCodec* hwDecoder(AVCodecID id, AVHWDeviceType type) {
    void* it{};
    while (const AVCodec* c = av_codec_iterate(&it)) {
        if (!av_codec_is_decoder(c) || c->id != id) continue;
        for (int i = 0; const AVCodecHWConfig* h = avcodec_get_hw_config(c, i); i++)
            if (h->device_type == type && (h->methods & AV_CODEC_HW_CONFIG_METHOD_HW_DEVICE_CTX)) return c;
    }
    return nullptr;
}

int run(const Options& o) {
    av_log_set_level(o.verbose ? AV_LOG_INFO : AV_LOG_ERROR);

    // ---- input
    AVFormatContext* in{};
    ck(avformat_open_input(&in, o.input.c_str(), nullptr, nullptr), "open input");
    ck(avformat_find_stream_info(in, nullptr), "stream info");
    const int vs{ck(av_find_best_stream(in, AVMEDIA_TYPE_VIDEO, -1, -1, nullptr, 0), "video stream")};
    const int as{av_find_best_stream(in, AVMEDIA_TYPE_AUDIO, -1, vs, nullptr, 0)};
    AVStream* vst{in->streams[vs]};
    AVRational rate{av_guess_frame_rate(in, vst, nullptr)};
    if (rate.num <= 0) throw std::runtime_error("unknown frame rate");
    const AVRational outRate{o.outFps.num > 0 ? o.outFps : AVRational{rate.num * int(o.multiplier), rate.den}};
    if (av_cmp_q(outRate, rate) <= 0) throw std::runtime_error("the output rate must be above the video's");
    const AVRational outTb{av_inv_q(outRate)};
    // Output frame n shows the video at source frame n * stepA / stepB (in source
    // frames from the first one): 1/4 for x4, 5/24 for 25 -> 120 fps. Frames that
    // fall between two source frames are generated at exactly that point, so any
    // rate plays evenly on a screen of that rate - a whole multiple could not (25
    // fps x4 = 100 fps on a 120 Hz screen held every fifth frame twice).
    int64_t stepA{int64_t(rate.num) * outRate.den}, stepB{int64_t(rate.den) * outRate.num};
    const int64_t stepGcd{std::gcd(stepA, stepB)};
    stepA /= stepGcd;
    stepB /= stepGcd;
    // The first output frame at or after source frame k.
    auto firstOut = [&](int64_t k) { return (k * stepB + stepA - 1) / stepA; };
    const double ratio{double(stepB) / double(stepA)};

    // ---- Vulkan device (FFmpeg's; lsfg-vk's images get imported into it)
    AVBufferRef* hwdev{};
    // disable_multiplane: NV12/P010 frames as one image per plane. With multiplane images
    // FFmpeg 8.1's Vulkan -> CUDA transfer fails in cuExternalMemoryGetMappedMipmappedArray.
    AVDictionary* devOpts{};
    av_dict_set(&devOpts, "disable_multiplane", "1", 0);
    ck(av_hwdevice_ctx_create(&hwdev, AV_HWDEVICE_TYPE_VULKAN, "0", devOpts, 0), "Vulkan device");
    av_dict_free(&devOpts);

    // ---- CUDA device on the same GPU: NVDEC decodes into it, NVENC encodes from it.
    AVBufferRef* cudadev{};
    if (o.decode == "auto" || o.decode == "cuda" || o.encoder == "auto" || o.encoder.find("nvenc") != std::string::npos) {
        if (av_hwdevice_ctx_create_derived(&cudadev, AV_HWDEVICE_TYPE_CUDA, hwdev, 0) < 0
                && av_hwdevice_ctx_create(&cudadev, AV_HWDEVICE_TYPE_CUDA, "0", nullptr, 0) < 0)
            cudadev = nullptr;
    }

    // ---- decoder
    // NVDEC (CUDA) first: bit-exact in every test, where FFmpeg 8.1's Vulkan H.264
    // decode drifted between IDRs on a real file. Then Vulkan video (for AV1 the
    // native "av1" decoder, not libdav1d, which avcodec_find_decoder returns first).
    // Then the CPU. Every choice is opened for real; a refusal moves to the next.
    const AVCodecID cid{vst->codecpar->codec_id};
    struct Choice { const char* name; const AVCodec* codec; AVBufferRef* device; AVPixelFormat fmt; };
    std::vector<Choice> choices;
    if ((o.decode == "auto" || o.decode == "cuda") && cudadev)
        if (const AVCodec* c = hwDecoder(cid, AV_HWDEVICE_TYPE_CUDA)) choices.push_back({"NVDEC", c, cudadev, AV_PIX_FMT_CUDA});
    if (o.decode == "auto" || o.decode == "vulkan")
        if (const AVCodec* c = hwDecoder(cid, AV_HWDEVICE_TYPE_VULKAN)) choices.push_back({"Vulkan", c, hwdev, AV_PIX_FMT_VULKAN});
    if (const AVCodec* c = avcodec_find_decoder(cid)) choices.push_back({"CPU", c, nullptr, AV_PIX_FMT_NONE});
    AVCodecContext* dec{};
    const char* decName{"?"};
    for (const Choice& ch : choices) {
        AVCodecContext* d{avcodec_alloc_context3(ch.codec)};
        ck(avcodec_parameters_to_context(d, vst->codecpar), "decoder parameters");
        if (ch.device) {
            d->hw_device_ctx = av_buffer_ref(ch.device);
            d->opaque = reinterpret_cast<void*>(intptr_t(ch.fmt));
            d->get_format = pickHw;
        } else {
            d->thread_count = 0;
        }
        d->pkt_timebase = vst->time_base;
        if (avcodec_open2(d, ch.codec, nullptr) >= 0) {
            dec = d;
            decName = ch.name;
            break;
        }
        avcodec_free_context(&d);
    }
    if (!dec) throw std::runtime_error("no decoder for this video");
    // CUDA frames are copied into Vulkan frames (on the GPU) before graph A.
    AVBufferRef* vkUpload{};

    // Segment mode: segment k covers source frames [k*segN, (k+1)*segN); its output
    // frames are those from firstOut(k*segN) on, whichever run makes them. At a
    // rate that is not a whole multiple a segment's first frame can be a generated
    // one, and segments can differ by a frame in length.
    const bool segMode{!o.segDir.empty()};
    const int segN{!segMode ? 0 : o.segFrames > 0 ? o.segFrames
        : std::max(1, int(std::lround(2.0 * av_q2d(rate))))};
    auto segOfOut = [&](int64_t n) { return n * stepA / (stepB * segN); };
    auto segStartsAt = [&](int64_t n) { return n == firstOut(segOfOut(n) * segN); };
    const int64_t segOutMax{segMode ? firstOut(segN) : 0};   // the most frames a segment has
    const double start{segMode ? double(o.startSeg) * segN / av_q2d(rate) : o.start};
    if (start > 0)
        ck(av_seek_frame(in, -1, int64_t(start * AV_TIME_BASE), AVSEEK_FLAG_BACKWARD), "seek");

    // Output size: fit under --max-height, even dimensions.
    int ow{dec->width}, oh{dec->height};
    if (o.maxHeight > 0 && oh > o.maxHeight) {
        ow = int(int64_t(ow) * o.maxHeight / oh + 1) / 2 * 2;
        oh = o.maxHeight;
    }
    const AVPixFmtDescriptor* sd{av_pix_fmt_desc_get(dec->sw_pix_fmt != AV_PIX_FMT_NONE ? dec->sw_pix_fmt : dec->pix_fmt)};
    const bool hbd{sd && sd->comp[0].depth > 8};
    const AVColorSpace matrix{dec->colorspace != AVCOL_SPC_UNSPECIFIED ? dec->colorspace
        : (oh >= 720 ? AVCOL_SPC_BT709 : AVCOL_SPC_SMPTE170M)};
    const bool hdr{dec->color_trc == AVCOL_TRC_SMPTE2084 || dec->color_trc == AVCOL_TRC_ARIB_STD_B67};

    // ---- encoder choice
    std::string encName{o.encoder};
    if (encName == "auto") {
        // NVENC needs libnvidia-encode; probe by opening a tiny encoder.
        encName = "libx264";
        if (const AVCodec* t = avcodec_find_encoder_by_name("av1_nvenc")) {
            AVCodecContext* tc{avcodec_alloc_context3(t)};
            tc->width = 256; tc->height = 256; tc->pix_fmt = AV_PIX_FMT_NV12; tc->time_base = {1, 25};
            if (avcodec_open2(tc, t, nullptr) >= 0) encName = "av1_nvenc";
            avcodec_free_context(&tc);
        }
    }
    const bool nvenc{encName.find("nvenc") != std::string::npos};
    const AVCodec* ec{avcodec_find_encoder_by_name(encName.c_str())};
    if (!ec) throw std::runtime_error("encoder not found: " + encName);

    // ---- CUDA frames for NVENC input (Vulkan -> CUDA transfer happens on the GPU)
    AVBufferRef* cudaFrames{};
    const AVPixelFormat encSw{hbd ? AV_PIX_FMT_P010 : (encName == "libsvtav1" ? AV_PIX_FMT_YUV420P : AV_PIX_FMT_NV12)};
    if (nvenc) {
        if (!cudadev) throw std::runtime_error("NVENC needs a CUDA device and none could be created");
        cudaFrames = av_hwframe_ctx_alloc(cudadev);
        auto* fc{reinterpret_cast<AVHWFramesContext*>(cudaFrames->data)};
        fc->format = AV_PIX_FMT_CUDA;
        fc->sw_format = encSw;
        fc->width = ow;
        fc->height = oh;
        fc->initial_pool_size = 8;
        ck(av_hwframe_ctx_init(cudaFrames), "CUDA frames");
    }

    AVCodecContext* enc{avcodec_alloc_context3(ec)};
    enc->width = ow;
    enc->height = oh;
    enc->time_base = outTb;
    enc->framerate = outRate;
    enc->sample_aspect_ratio = vst->codecpar->sample_aspect_ratio;
    // A keyframe every second when streaming (each starts a fragment the browser can
    // play), every two seconds for files.
    enc->gop_size = segMode ? int(segOutMax) : int((o.output == "-" ? 1 : 2) * av_q2d(outRate));
    if (o.maxrate > 0) {
        // Quality target as usual, but never above the cap: VBR with a peak rate and a
        // two-second buffer, so short bursts are allowed and the average holds.
        enc->rc_max_rate = int64_t(o.maxrate * 1e6);
        enc->rc_buffer_size = int(std::min(2 * o.maxrate * 1e6, 2e9));
    }
    enc->colorspace = matrix;
    enc->color_range = AVCOL_RANGE_MPEG;
    enc->color_primaries = hdr ? AVCOL_PRI_BT2020 : AVCOL_PRI_BT709;
    enc->color_trc = hdr ? dec->color_trc : AVCOL_TRC_BT709;
    if (nvenc) {
        enc->pix_fmt = AV_PIX_FMT_CUDA;
        enc->hw_frames_ctx = av_buffer_ref(cudaFrames);
        // NVENC is the bottleneck at 4K (measured on the RTX 5060, 4K60 x2 AV1: p5 1.04x,
        // p3 1.38x, p1 1.8x real time), so the preset follows the output pixel rate.
        std::string preset{o.preset};
        if (preset == "auto") {
            const double pixelRate{double(ow) * oh * av_q2d(outRate)};
            preset = pixelRate > 3840.0 * 2160 * 100 ? "p1" : pixelRate > 2560.0 * 1440 * 100 ? "p3" : "p5";
        }
        av_opt_set(enc->priv_data, "preset", preset.c_str(), 0);
        encName += " " + preset;
        av_opt_set(enc->priv_data, "tune", "hq", 0);
        // A frame marked I (segment starts) becomes an IDR, so every segment can be
        // decoded on its own - the player appends segments from different runs.
        av_opt_set_int(enc->priv_data, "forced-idr", 1, 0);
        av_opt_set(enc->priv_data, "rc", "vbr", 0);
        av_opt_set_int(enc->priv_data, "cq", o.quality >= 0 ? o.quality : 28, 0);
        enc->bit_rate = 0;
    } else {
        enc->pix_fmt = encSw;
        if (encName == "libx264") av_opt_set(enc->priv_data, "preset", "veryfast", 0);
        if (encName == "libsvtav1") av_opt_set(enc->priv_data, "preset", "8", 0);
        av_opt_set_int(enc->priv_data, "crf", o.quality >= 0 ? o.quality : (encName == "libsvtav1" ? 30 : 18), 0);
    }

    // ---- output
    AVFormatContext* out{};
    const bool toStdout{o.output == "-"};
    if (segMode && encName.rfind("av1", 0) != 0 && encName.rfind("libsvtav1", 0) != 0)
        throw std::runtime_error("segment mode needs an AV1 encoder (the player's codec string is AV1)");
    ck(avformat_alloc_output_context2(&out, nullptr, (toStdout || segMode) ? "mp4" : nullptr,
        (toStdout || segMode) ? nullptr : o.output.c_str()), "output format");
    if (out->oformat->flags & AVFMT_GLOBALHEADER) enc->flags |= AV_CODEC_FLAG_GLOBAL_HEADER;
    ck(avcodec_open2(enc, ec, nullptr), "open encoder");
    AVStream* ovs{avformat_new_stream(out, nullptr)};
    ck(avcodec_parameters_from_context(ovs->codecpar, enc), "video stream parameters");
    ovs->time_base = outTb;
    ovs->avg_frame_rate = outRate;
    AVStream* oas{};
    // In segment mode only what a browser's Media Source accepts in MP4.
    const std::string audioCodec{as >= 0 ? audioCodecString(in->streams[as]->codecpar) : ""};
    if (as >= 0 && (!segMode || !audioCodec.empty())
            && avformat_query_codec(out->oformat, in->streams[as]->codecpar->codec_id,
            FF_COMPLIANCE_NORMAL) == 1) {
        oas = avformat_new_stream(out, nullptr);
        ck(avcodec_parameters_copy(oas->codecpar, in->streams[as]->codecpar), "audio parameters");
        oas->codecpar->codec_tag = 0;
        oas->time_base = in->streams[as]->time_base;
    } else if (as >= 0) {
        std::fprintf(stderr, "vidinterp-gpu: audio codec %s cannot go into this container; audio dropped\n",
            avcodec_get_name(in->streams[as]->codecpar->codec_id));
    }
    MemSink segSink;
    AVDictionary* mopts{};
    if (segMode) {
        // The muxer writes into memory; the header becomes init.mp4 and every
        // fragment flushed with av_write_frame(NULL) becomes one segment file.
        auto* iobuf{static_cast<uint8_t*>(av_malloc(1 << 16))};
        out->pb = avio_alloc_context(iobuf, 1 << 16, 1, &segSink, nullptr, &MemSink::write, nullptr);
        out->flags |= AVFMT_FLAG_CUSTOM_IO;
        // frag_discont: keep the packets' own timestamps in each fragment (tfdt). Without
        // it a run starting at segment 3 wrote its first segment at 0 s, not 6 s.
        av_dict_set(&mopts, "movflags", "frag_custom+empty_moov+default_base_moof+frag_discont", 0);
        // And no shifting of the first timestamp to zero (libavformat's default for MP4).
        out->avoid_negative_ts = AVFMT_AVOID_NEG_TS_DISABLED;
    } else {
        ck(avio_open(&out->pb, toStdout ? "pipe:1" : o.output.c_str(), AVIO_FLAG_WRITE), "open output");
        if (toStdout || std::string(out->oformat->name).find("mp4") != std::string::npos)
            av_dict_set(&mopts, "movflags", toStdout ? "frag_keyframe+empty_moov+default_base_moof"
                : "+faststart", 0);
    }
    ck(avformat_write_header(out, &mopts), "write header");
    av_dict_free(&mopts);
    if (segMode) {
        avio_flush(out->pb);
        const std::string init{o.segDir + "/init.mp4"};
        if (::access(init.c_str(), F_OK) != 0) writeFileAtomic(init, segSink.data.data(), segSink.data.size());
        segSink.data.clear();
        // What the player needs before the first segment: codecs for the Media
        // Source, the segment layout, and how many segments the file has.
        double duration{in->duration > 0 ? double(in->duration) / AV_TIME_BASE
            : vst->duration > 0 ? vst->duration * av_q2d(vst->time_base) : 0.0};
        const int64_t frames{std::llround(duration * av_q2d(rate))};
        char meta[1024];
        std::snprintf(meta, sizeof meta,
            "{\"video_codec\": \"%s\", \"audio_codec\": \"%s\", \"segment_frames\": %d, "
            "\"fps\": [%d, %d], \"out_fps\": [%d, %d], \"mult\": %.6g, \"width\": %d, \"height\": %d, "
            "\"duration\": %.6f, \"segments\": %lld}\n",
            av1CodecString(enc).c_str(), oas ? audioCodec.c_str() : "", segN, rate.num, rate.den,
            outRate.num, outRate.den, ratio, ow, oh, duration, (long long)((frames + segN - 1) / segN));
        writeFileAtomic(o.segDir + "/meta.json", meta, std::strlen(meta));
    }

    std::fprintf(stderr, "vidinterp-gpu: %dx%d @ %d/%d -> %dx%d @ %d/%d x%.4g, %s%s, decoder %s, encoder %s\n",
        dec->width, dec->height, rate.num, rate.den, ow, oh, outRate.num, outRate.den,
        ratio, hbd ? "10-bit" : "8-bit", hdr ? " HDR" : "", decName, encName.c_str());

    std::unique_ptr<LsfgStage> lsfg;
    Graph ga, gb;
    bool graphsReady{false};
    int64_t outPts{segMode ? firstOut(int64_t(o.startSeg) * segN) : 0};
    uint64_t nIn{0}, nOut{0}, nCuts{0};
    const auto t0{std::chrono::steady_clock::now()};
    auto lastReport{t0};
    // Files and streams start their timeline at the start position; segments keep
    // the file's own timeline, so no offset there.
    const int64_t startOut{!segMode && start > 0 ? int64_t(start * AV_TIME_BASE) : 0};
    const double endTime{segMode ? (o.endSeg >= 0 ? double(o.endSeg) * segN / av_q2d(rate) : 1e300)
        : o.duration > 0 ? start + o.duration : 1e300};

    // ---- segment writing: video packets belong to segment segOfOut(pts) and a new
    // segment begins at its forced keyframe; audio waits in a queue and is written
    // into the segment its timestamp falls in.
    int64_t curSeg{-1};
    uint64_t segmentsWritten{0};
    std::deque<AVPacket*> audioQ;
    auto packetSeconds = [&](const AVPacket* p) {
        return p->pts * av_q2d(out->streams[p->stream_index]->time_base);
    };
    auto writeAudioBefore = [&](double seconds) {
        while (!audioQ.empty() && packetSeconds(audioQ.front()) < seconds) {
            AVPacket* p{audioQ.front()};
            audioQ.pop_front();
            ck(av_write_frame(out, p), "write audio");
            av_packet_free(&p);
        }
    };
    auto finishSegment = [&](double audioUntil) {
        if (curSeg < 0) return;
        writeAudioBefore(audioUntil);
        ck(av_write_frame(out, nullptr), "flush fragment");
        avio_flush(out->pb);
        char name[64];
        std::snprintf(name, sizeof name, "/seg_%05lld.m4s", (long long)curSeg);
        writeFileAtomic(o.segDir + name, segSink.data.data(), segSink.data.size());
        segSink.data.clear();
        segmentsWritten++;
        TRACE("segment %lld written", (long long)curSeg);
    };
    auto segmentStart = [&](int64_t seg) { return double(seg) * segN / av_q2d(rate); };

    auto writePacket = [&](AVPacket* pkt, AVStream* st, AVRational from) {
        av_packet_rescale_ts(pkt, from, st->time_base);
        pkt->stream_index = st->index;
        if (!segMode) {
            ck(av_interleaved_write_frame(out, pkt), "write packet");
            return;
        }
        if (st != ovs) {                         // audio: queued for its segment
            audioQ.push_back(av_packet_clone(pkt));
            return;
        }
        const int64_t seg{segOfOut(av_rescale_q(pkt->pts, st->time_base, outTb))};
        if (seg != curSeg) {
            finishSegment(segmentStart(curSeg + 1));
            curSeg = seg;
            // Audio from before this segment (the seek's preroll) is not in any.
            while (!audioQ.empty() && packetSeconds(audioQ.front()) < segmentStart(seg)) {
                av_packet_free(&audioQ.front());
                audioQ.pop_front();
            }
        }
        writeAudioBefore(packetSeconds(pkt));
        ck(av_write_frame(out, pkt), "write video");
    };

    AVPacket* epkt{av_packet_alloc()};
    auto drainEncoder = [&]() {
        while (true) {
            const int r{avcodec_receive_packet(enc, epkt)};
            if (r == AVERROR(EAGAIN) || r == AVERROR_EOF) return;
            ck(r, "encode");
            writePacket(epkt, ovs, enc->time_base);
        }
    };
    auto encodeFrame = [&](AVFrame* f) {  // f: Vulkan NV12/P010 frame from graph B, or null to flush
        TRACE("encode pts %ld", f ? (long)f->pts : -1L);
        if (!f) { ck(avcodec_send_frame(enc, nullptr), "flush encoder"); drainEncoder(); return; }
        FramePtr e{allocFrame()};
        if (nvenc) {
            ck(av_hwframe_get_buffer(cudaFrames, e.get(), 0), "CUDA frame");
            ck(av_hwframe_transfer_data(e.get(), f, 0), "Vulkan -> CUDA transfer");
        } else {
            e->format = encSw;
            ck(av_hwframe_transfer_data(e.get(), f, 0), "download frame");
            TRACE("downloaded");
        }
        ck(av_frame_copy_props(e.get(), f), "frame props");
        e->pts = f->pts;
        // Each segment opens with a keyframe (an IDR with forced-idr), whatever the GOP.
        e->pict_type = segMode && segStartsAt(e->pts) ? AV_PICTURE_TYPE_I : AV_PICTURE_TYPE_NONE;
        ck(avcodec_send_frame(enc, e.get()), "send frame to encoder");
        drainEncoder();
    };

    FramePtr gbOut{allocFrame()};
    auto drainB = [&]() {
        while (true) {
            const int r{av_buffersink_get_frame(gb.sink, gbOut.get())};
            if (r == AVERROR(EAGAIN) || r == AVERROR_EOF) return;
            ck(r, "graph B");
            encodeFrame(gbOut.get());
            av_frame_unref(gbOut.get());
        }
    };
    auto emit = [&](FramePtr f) {  // an output-pool RGBA frame, in output order
        TRACE("emit %ld", (long)outPts);
        f->pts = outPts++;
        f->duration = 1;
        f->color_range = AVCOL_RANGE_JPEG;
        ck(av_buffersrc_add_frame_flags(gb.src, f.get(), 0), "graph B input");
        nOut++;
        drainB();
    };

    // prev: last source frame fed to lsfg (RGBA Vulkan frame from graph A), and its
    // index (source frames from the start of the timeline, as firstOut counts).
    FramePtr prev;
    int64_t prevIndex{0};

    // The output frames between source frame k (prev) and k + 1 (cur), in order.
    // Each one is prev itself, cur itself, or generated at its exact position; a
    // position within 5% of an output frame of a source frame shows that source
    // frame (an LSFG frame there would look the same, only softer). At a cut
    // nothing is morphed: the first half shows prev, the rest cur.
    auto emitInterval = [&](int64_t k, const AVFrame* cur, bool cut) {
        const int64_t n0{firstOut(k)}, n1{firstOut(k + 1)};
        if (outPts != n0) throw std::runtime_error("internal: output timeline out of step");
        struct Slot { int kind; float at; };        // kind 0 = prev, 1 = generated, 2 = cur
        std::vector<Slot> slots;
        std::vector<float> at;
        for (int64_t n = n0; n < n1; n++) {
            const int64_t num{n * stepA - k * stepB};   // position = num / stepB source frames
            if (num * 20 < stepA) slots.push_back({0, 0.0F});
            else if ((stepB - num) * 20 < stepA) slots.push_back({2, 1.0F});
            else {
                const float t{static_cast<float>(num) / static_cast<float>(stepB)};
                slots.push_back({cut ? (t < 0.5F ? 0 : 2) : 1, t});
                if (!cut) at.push_back(t);
            }
        }
        // prev copies come first and cur copies last, generated frames between.
        size_t i{0};
        for (; i < slots.size() && slots[i].kind == 0; i++) emit(lsfg->copy(prev.get()));
        lsfg->push(cur, at, cut, [&](FramePtr g) { emit(std::move(g)); i++; });
        for (; i < slots.size(); i++) emit(lsfg->copy(cur));
        if (cut) nCuts++;
    };
    // Segment mode with an end: the first frame of segment endSeg is still needed -
    // it is what the last interval of the segment before interpolates towards - but
    // it is not output itself, and nothing after it is looked at.
    bool boundaryReached{false};
    auto onSource = [&](FramePtr cur, bool cut) {
        if (boundaryReached) return;
        TRACE("source %lu%s", (unsigned long)nIn, cut ? " (cut)" : "");
        const int64_t srcIndex{int64_t(o.startSeg) * segN + int64_t(nIn)};
        nIn++;
        if (segMode && o.endSeg >= 0 && srcIndex >= int64_t(o.endSeg) * segN) {
            boundaryReached = true;
            if (!prev) return;
        }
        if (!prev) {
            lsfg->push(cur.get(), {}, true, [](FramePtr) {});  // lsfg's first iteration has no pair
            prev = std::move(cur);
            prevIndex = srcIndex;
            return;
        }
        emitInterval(prevIndex, cur.get(), cut);
        prev = std::move(cur);
        prevIndex = srcIndex;

        const auto now{std::chrono::steady_clock::now()};
        if (now - lastReport > std::chrono::seconds(5)) {
            lastReport = now;
            const double s{std::chrono::duration<double>(now - t0).count()};
            std::fprintf(stderr, "vidinterp-gpu: in %lu, out %lu, %.1f in-fps, %.1f out-fps, speed %.2fx\n",
                (unsigned long)nIn, (unsigned long)nOut, nIn / s, nOut / s, nIn / s / av_q2d(rate));
        }
    };

    // Graph A has two outputs when cuts are detected: the frames for LSFG, and a small
    // 8-bit copy of each that went through scdet_vulkan. They are paired by pts; a
    // frame waits until its cut decision is there (the branches need not keep step).
    FramePtr gaOut{allocFrame()};
    std::deque<FramePtr> pendingSource;
    std::map<int64_t, bool> cutAt;
    int64_t forceCutPts{-1};   // --loop: the first frame after the wrap, in graph A's output tb
    auto releaseSources = [&](bool flushing) {
        while (!pendingSource.empty()) {
            const int64_t pts{pendingSource.front()->pts};
            bool cut{false};
            if (ga.det) {
                auto it{cutAt.find(pts)};
                if (it == cutAt.end() && !flushing) return;   // decision not in yet
                if (it != cutAt.end()) { cut = it->second; cutAt.erase(it); }
            }
            // The wrap from the last frame back to the first is a cut whatever the
            // detector says: LSFG must not morph the ending into the beginning.
            if (forceCutPts >= 0 && pts >= forceCutPts) {
                cut = true;
                forceCutPts = -1;
            }
            FramePtr f{std::move(pendingSource.front())};
            pendingSource.pop_front();
            onSource(std::move(f), cut);
        }
    };
    auto drainA = [&](bool flushing = false) {
        while (true) {
            const int r{av_buffersink_get_frame(ga.sink, gaOut.get())};
            if (r == AVERROR(EAGAIN) || r == AVERROR_EOF) break;
            ck(r, "graph A");
            FramePtr f{allocFrame()};
            av_frame_move_ref(f.get(), gaOut.get());
            pendingSource.push_back(std::move(f));
        }
        while (ga.det) {
            const int r{av_buffersink_get_frame(ga.det, gaOut.get())};
            if (r == AVERROR(EAGAIN) || r == AVERROR_EOF) break;
            ck(r, "graph A (scene detection)");
            cutAt[gaOut->pts] = av_dict_get(gaOut->metadata, "lavfi.scd.time", nullptr, 0) != nullptr;
            av_frame_unref(gaOut.get());
        }
        releaseSources(flushing);
    };

    auto setupGraphs = [&](const AVFrame* first) {
        const bool hw{first->format == AV_PIX_FMT_VULKAN};
        char chain[1024];
        // Cut detection runs on a 480-px 8-bit copy: FFmpeg 8.1's scdet_vulkan
        // over-scores 10-bit frames 64x (P010 samples are stored << 6), so ordinary
        // motion became "cuts" and was held instead of interpolated. Cheaper at 4K too.
        if (o.scene > 0) {
            std::snprintf(chain, sizeof chain,
                "%sfps=%d/%d,split=2[m][d];"
                "[d]libplacebo=w=480:h=-2:format=nv12,scdet_vulkan=threshold=%g[det];"
                "[m]libplacebo=w=%d:h=%d:format=%s[out]",
                hw ? "" : "hwupload,", rate.num, rate.den, o.scene, ow, oh, hbd ? "x2bgr10" : "rgba");
        } else {
            std::snprintf(chain, sizeof chain, "%sfps=%d/%d,libplacebo=w=%d:h=%d:format=%s",
                hw ? "" : "hwupload,", rate.num, rate.den, ow, oh, hbd ? "x2bgr10" : "rgba");
        }
        ga.build(chain, hwdev, first->width, first->height, AVPixelFormat(first->format),
            first->hw_frames_ctx, vst->time_base, rate, first->sample_aspect_ratio);

        lsfg = std::make_unique<LsfgStage>(hwdev, uint32_t(ow), uint32_t(oh), hbd, o);

        std::snprintf(chain, sizeof chain,
            "libplacebo=format=%s:colorspace=%d:color_primaries=%d:color_trc=%d:range=tv",
            av_get_pix_fmt_name(encSw), int(matrix), int(enc->color_primaries), int(enc->color_trc));
        gb.build(chain, hwdev, ow, oh, AV_PIX_FMT_VULKAN, lsfg->outFrames(), outTb, outRate, {1, 1});
        graphsReady = true;
    };

    // ---- main demux/decode loop
    AVPacket* pkt{av_packet_alloc()};
    FramePtr df{allocFrame()};
    bool videoDone{false};
    // --loop: each pass through the file is appended to the timeline. loopAdd is
    // the length of all earlier passes (seconds, source time), added to video
    // and audio timestamps alike; passEnd is where the current pass has got to.
    int passes{0};
    double loopAdd{0}, passEnd{0};
    bool markWrap{false};
    auto onDecoded = [&](AVFrame* f) {
        const int64_t ts{f->best_effort_timestamp};
        const double t{ts == AV_NOPTS_VALUE ? 0.0 : ts * av_q2d(vst->time_base)};
        if (passes == 0 && t + 0.5 / av_q2d(rate) < start) return;   // before the seek target
        if (boundaryReached) { videoDone = true; return; }
        // Segment mode reads the frame AT the end boundary (see onSource); files
        // and streams stop before it.
        if (!o.loop && t >= endTime + (segMode ? 0.5 / av_q2d(rate) : 0.0)) { videoDone = true; return; }
        FramePtr uploaded;
        if (f->format == AV_PIX_FMT_CUDA) {
            // NVDEC's frame into a Vulkan frame, GPU to GPU (CUDA external memory).
            if (!vkUpload) {
                const auto* cfc{reinterpret_cast<AVHWFramesContext*>(f->hw_frames_ctx->data)};
                vkUpload = av_hwframe_ctx_alloc(hwdev);
                auto* fc{reinterpret_cast<AVHWFramesContext*>(vkUpload->data)};
                fc->format = AV_PIX_FMT_VULKAN;
                fc->sw_format = cfc->sw_format;
                fc->width = f->width;
                fc->height = f->height;
                ck(av_hwframe_ctx_init(vkUpload), "Vulkan frames for NVDEC output");
            }
            uploaded = allocFrame();
            ck(av_hwframe_get_buffer(vkUpload, uploaded.get(), 0), "Vulkan frame");
            ck(av_hwframe_transfer_data(uploaded.get(), f, 0), "CUDA -> Vulkan transfer");
            ck(av_frame_copy_props(uploaded.get(), f), "frame props");
            f = uploaded.get();
        }
        if (!graphsReady) setupGraphs(f);
        passEnd = std::max(passEnd, t + 1.0 / av_q2d(rate));
        f->pts = (ts == AV_NOPTS_VALUE ? 0 : ts) + int64_t(std::llround(loopAdd / av_q2d(vst->time_base)));
        if (markWrap) {
            forceCutPts = av_rescale_q(f->pts, vst->time_base, av_buffersink_get_time_base(ga.sink));
            markWrap = false;
        }
        ck(av_buffersrc_add_frame_flags(ga.src, f, AV_BUFFERSRC_FLAG_KEEP_REF), "graph A input");
        drainA();
    };
    auto drainDecoder = [&]() {
        while (true) {
            const int r{avcodec_receive_frame(dec, df.get())};
            if (r == AVERROR(EAGAIN) || r == AVERROR_EOF) return;
            ck(r, "decode");
            onDecoded(df.get());
            av_frame_unref(df.get());
        }
    };

    int64_t lastAudioDts{INT64_MIN};   // output tb; a pass's audio may run past its video
    while (true) {
        while (!videoDone && av_read_frame(in, pkt) >= 0) {
            if (pkt->stream_index == vs) {
                ck(avcodec_send_packet(dec, pkt), "send packet");
                drainDecoder();
            } else if (oas && pkt->stream_index == as) {
                const AVRational tb{in->streams[as]->time_base};
                const double t{pkt->pts == AV_NOPTS_VALUE ? 0.0 : pkt->pts * av_q2d(tb)};
                if ((passes > 0 || t >= start) && (o.loop || t < endTime)) {
                    const int64_t off{av_rescale_q(startOut, AV_TIME_BASE_Q, tb)
                        - int64_t(std::llround(loopAdd / av_q2d(tb)))};
                    if (pkt->pts != AV_NOPTS_VALUE) pkt->pts -= off;
                    if (pkt->dts != AV_NOPTS_VALUE) pkt->dts -= off;
                    const int64_t dtsOut{pkt->dts == AV_NOPTS_VALUE ? INT64_MIN
                        : av_rescale_q(pkt->dts, tb, oas->time_base)};
                    if (dtsOut == INT64_MIN || dtsOut > lastAudioDts) {
                        if (dtsOut != INT64_MIN) lastAudioDts = dtsOut;
                        writePacket(pkt, oas, tb);
                    }
                }
            }
            av_packet_unref(pkt);
        }
        if (!videoDone) {
            avcodec_send_packet(dec, nullptr);
            drainDecoder();
        }
        if (!o.loop || videoDone || passEnd <= 0) break;
        // Start over. The decoder is reset, the timeline goes on: the next pass
        // begins where this one ended, and its first frame is a forced cut.
        avcodec_flush_buffers(dec);
        loopAdd += passEnd;
        passEnd = 0;
        passes++;
        markWrap = true;
        ck(av_seek_frame(in, -1, 0, AVSEEK_FLAG_BACKWARD), "seek to the start (loop)");
        TRACE("loop %d, timeline now %.3f s", passes, loopAdd);
    }
    if (!graphsReady) throw std::runtime_error("no video frames decoded");
    ck(av_buffersrc_add_frame_flags(ga.src, nullptr, 0), "flush graph A");
    drainA(true);   // frames still waiting for a cut decision go out as non-cuts
    // The file's last frame is held for a full interval; a segment run that stopped
    // at a boundary has already output everything up to it.
    if (prev && !boundaryReached)
        for (int64_t n = firstOut(prevIndex); n < firstOut(prevIndex + 1); n++) emit(lsfg->copy(prev.get()));
    prev.reset();
    ck(av_buffersrc_add_frame_flags(gb.src, nullptr, 0), "flush graph B");
    drainB();
    encodeFrame(nullptr);
    if (segMode) {
        finishSegment(boundaryReached ? endTime : 1e300);
        if (!boundaryReached && curSeg >= 0) {
            // Reached the end of the file: the real number of segments, which the
            // estimate in meta.json may miss by one.
            char end[64];
            std::snprintf(end, sizeof end, "{\"last_segment\": %lld}\n", (long long)curSeg);
            writeFileAtomic(o.segDir + "/end.json", end, std::strlen(end));
        }
        for (AVPacket* p : audioQ) av_packet_free(&p);
        audioQ.clear();
    }
    ck(av_write_trailer(out), "write trailer");   // in segment mode into memory, unused
    lsfg->finish();

    const double s{std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count()};
    std::fprintf(stderr, "vidinterp-gpu: done. in %lu, out %lu, cuts %lu, %.2f s, %.1f in-fps, "
        "%.1f out-fps, speed %.2fx%s\n", (unsigned long)nIn, (unsigned long)nOut, (unsigned long)nCuts,
        s, nIn / s, nOut / s, nIn / s / av_q2d(rate),
        segMode ? (", segments " + std::to_string(segmentsWritten)).c_str() : "");

    if (segMode) {
        av_freep(&out->pb->buffer);
        avio_context_free(&out->pb);
    } else {
        avio_closep(&out->pb);
    }
    avformat_free_context(out);
    av_packet_free(&pkt);
    av_packet_free(&epkt);
    avcodec_free_context(&enc);
    avcodec_free_context(&dec);
    avformat_close_input(&in);
    return 0;
}

}  // namespace

int main(int argc, char** argv) {
    try {
        return run(parse(argc, argv));
    } catch (const std::exception& e) {
        std::fprintf(stderr, "vidinterp-gpu: error: %s\n", e.what());
        std::_Exit(1);
    }
}
