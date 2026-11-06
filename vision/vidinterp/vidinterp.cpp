// vidinterp - frame interpolation of a raw video stream with Lossless Scaling
// frame generation (LSFG 3.1) through the lsfg-vk v2 pipeline library.
//
//   ffmpeg -i in.mkv -f rawvideo -pix_fmt rgba -
//     | vidinterp -W 1920 -H 1080 -m 4
//     | ffmpeg -f rawvideo -pix_fmt rgba -s 1920x1080 -r 96 -i - out.mkv
//
// stdin: raw frames (rgba, or x2bgr10le with --depth 10). stdout: the same format
// at <multiplier> times the frame rate. stderr: progress and statistics.
//
// How it talks to lsfg-vk: the library owns its own Vulkan device and exports the
// memory of its input image (2 array layers, the source of iteration i goes into
// layer i % 2), of its output image and a timeline semaphore as opaque fds. This
// program opens a second Vulkan device on the same GPU, imports all three and moves
// frames with buffer<->image copies. All ordering between the two devices goes
// through that one timeline semaphore; see Interpolator::push() for the value protocol.
//
// Video-specific behaviour (none of this exists in the game-oriented layer):
//  - scene cuts: frames across a cut are not morphed; the old frame is held for the
//    first half of the interval and the new one for the second half.
//  - --dedup: held frames (animation on twos/threes) are collapsed, and the motion is
//    interpolated across the whole held interval instead of in bursts.
//  - end of stream: the last frame is held for a full interval so the duration matches.

#define LSFGVK_EXPOSE_VULKAN 1
#include "lsfg-vk/lsfgvk.hpp"

#include <vulkan/vulkan.h>

#include <algorithm>
#include <array>
#include <atomic>
#include <condition_variable>
#include <deque>
#include <mutex>
#include <thread>
#include <chrono>
#include <cerrno>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <functional>
#include <memory>
#include <optional>
#include <stdexcept>
#include <string>
#include <fcntl.h>
#include <unistd.h>
#include <vector>

namespace {

// ---------------------------------------------------------------- options

struct Options {
    uint32_t width{0}, height{0};
    uint32_t multiplier{2};
    float flow{1.0F};
    bool performance{false};
    bool fp16{true};
    int depth{8};
    std::string dll;
    std::string gpu;
    double sceneThreshold{10.0};  // same scale as ffmpeg scdet (0-100)
    bool dedup{false};
    double dupThreshold{0.35};    // mean abs luma diff (0-255) below which a frame is "held"
    uint32_t maxHold{4};          // longest held run that is still interpolated across
    uint32_t readbackSlots{4};
    bool verbose{false};
};

[[noreturn]] void usage(const char* argv0) {
    std::fprintf(stderr,
        "usage: %s -W <width> -H <height> [options] < in.raw > out.raw\n"
        "  -m, --multiplier N     output fps = N x input fps (default 2)\n"
        "  -f, --flow F           flow scale 0.25-1.0 (default 1.0)\n"
        "  -p, --performance      LSFG performance mode\n"
        "      --no-fp16          disable fp16 shaders\n"
        "      --depth 8|10       rgba (8) or x2bgr10le (10) frames (default 8)\n"
        "  -d, --dll PATH         path to lsfg-vk.dll (default: $LSFGVK_DLL_PATH)\n"
        "  -g, --gpu NAME         substring of the Vulkan device name\n"
        "      --scene T          scene-cut threshold, scdet scale (default 10, 0 = off)\n"
        "      --dedup            collapse held frames and interpolate across them\n"
        "      --dup-threshold T  mean abs luma diff for a held frame (default 0.35)\n"
        "      --max-hold N       longest held run to interpolate across (default 4)\n"
        "  -v, --verbose          log lsfg-vk messages\n", argv0);
    std::exit(2);
}

Options parse(int argc, char** argv) {
    Options o;
    const char* env{std::getenv("LSFGVK_DLL_PATH")};
    if (env) o.dll = env;
    for (int i = 1; i < argc; i++) {
        const std::string a{argv[i]};
        auto next = [&]() -> std::string {
            if (i + 1 >= argc) usage(argv[0]);
            return argv[++i];
        };
        if (a == "-W" || a == "--width") o.width = std::stoul(next());
        else if (a == "-H" || a == "--height") o.height = std::stoul(next());
        else if (a == "-m" || a == "--multiplier") o.multiplier = std::stoul(next());
        else if (a == "-f" || a == "--flow") o.flow = std::stof(next());
        else if (a == "-p" || a == "--performance") o.performance = true;
        else if (a == "--no-fp16") o.fp16 = false;
        else if (a == "--depth") o.depth = std::stoi(next());
        else if (a == "-d" || a == "--dll") o.dll = next();
        else if (a == "-g" || a == "--gpu") o.gpu = next();
        else if (a == "--scene") o.sceneThreshold = std::stod(next());
        else if (a == "--dedup") o.dedup = true;
        else if (a == "--dup-threshold") o.dupThreshold = std::stod(next());
        else if (a == "--max-hold") o.maxHold = std::stoul(next());
        else if (a == "-v" || a == "--verbose") o.verbose = true;
        else usage(argv[0]);
    }
    if (!o.width || !o.height || o.multiplier < 2 || o.dll.empty()
            || o.flow < 0.25F || o.flow > 1.0F || (o.depth != 8 && o.depth != 10)
            || o.maxHold < 1)
        usage(argv[0]);
    return o;
}

// ---------------------------------------------------------------- profiling

/// Wall time per phase of the main loop, printed at the end with VIDINTERP_PROFILE=1.
enum Phase { P_READ, P_STAGING_WAIT, P_UPLOAD, P_DISPATCH, P_ACQUIRE, P_READBACK_WAIT,
    P_WRITE, P_ANALYSE, P_COUNT };
const std::array<const char*, P_COUNT> PHASE_NAMES{
    "read stdin", "staging wait", "upload submit", "lsfg dispatch", "lsfg acquire",
    "readback wait", "write stdout", "frame analysis" };
std::array<double, P_COUNT> g_phase{};

struct Timed {
    Phase p;
    std::chrono::steady_clock::time_point t0{std::chrono::steady_clock::now()};
    explicit Timed(Phase ph) : p(ph) {}
    ~Timed() { g_phase[p] += std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count(); }
};

// ---------------------------------------------------------------- I/O

/// Grow a pipe's kernel buffer. With the 64 KiB default, a 33 MB 4K frame crosses the
/// pipe in ~500 wakeups on each side, which on a loaded host capped the decoder -> vidinterp
/// pipe at ~5 fps; 1 MiB (the default /proc/sys/fs/pipe-max-size) lifts that.
void grow_pipe(int fd) {
    int want{1 << 20};
    if (FILE* f = std::fopen("/proc/sys/fs/pipe-max-size", "r")) {
        int max{0};
        if (std::fscanf(f, "%d", &max) == 1 && max > 0) want = max;
        std::fclose(f);
    }
    ::fcntl(fd, F_SETPIPE_SZ, want);  // fails harmlessly when fd is not a pipe
}

bool read_full(int fd, uint8_t* dst, size_t n) {
    size_t got{0};
    while (got < n) {
        const ssize_t r{::read(fd, dst + got, n - got)};
        if (r == 0) {
            if (got) std::fprintf(stderr, "vidinterp: truncated last frame ignored\n");
            return false;
        }
        if (r < 0) {
            if (errno == EINTR) continue;
            throw std::runtime_error("read from stdin failed");
        }
        got += static_cast<size_t>(r);
    }
    return true;
}

void write_full(int fd, const uint8_t* src, size_t n) {
    while (n) {
        const ssize_t w{::write(fd, src, n)};
        if (w < 0) {
            if (errno == EINTR) continue;
            throw std::runtime_error("write to stdout failed (consumer gone?)");
        }
        src += w;
        n -= static_cast<size_t>(w);
    }
}

// ---------------------------------------------------------------- frame analysis

/// Subsampled luma grid, used for scene-cut and held-frame detection.
struct Thumb {
    std::vector<uint8_t> y;
};

Thumb make_thumb(const uint8_t* px, uint32_t w, uint32_t h, int depth) {
    constexpr uint32_t STEP{8};
    Thumb t;
    t.y.reserve((w / STEP + 1) * (h / STEP + 1));
    for (uint32_t yy = STEP / 2; yy < h; yy += STEP) {
        const auto* row{reinterpret_cast<const uint32_t*>(px) + static_cast<size_t>(yy) * w};
        for (uint32_t xx = STEP / 2; xx < w; xx += STEP) {
            const uint32_t v{row[xx]};
            uint32_t r, g, b;
            if (depth == 8) {
                r = v & 0xFF; g = (v >> 8) & 0xFF; b = (v >> 16) & 0xFF;
            } else {  // x2bgr10le: R in the low bits
                r = (v & 0x3FF) >> 2; g = ((v >> 10) & 0x3FF) >> 2; b = ((v >> 20) & 0x3FF) >> 2;
            }
            t.y.push_back(static_cast<uint8_t>((54 * r + 183 * g + 19 * b) >> 8));
        }
    }
    return t;
}

/// Mean absolute difference, 0-255.
double mafd(const Thumb& a, const Thumb& b) {
    uint64_t s{0};
    for (size_t i = 0; i < a.y.size(); i++)
        s += static_cast<uint64_t>(std::abs(int(a.y[i]) - int(b.y[i])));
    return a.y.empty() ? 0.0 : double(s) / double(a.y.size());
}

// ---------------------------------------------------------------- Vulkan

void check(VkResult r, const char* what) {
    if (r != VK_SUCCESS)
        throw std::runtime_error(std::string(what) + " failed: VkResult " + std::to_string(r));
}

struct Buffer {
    VkBuffer buf{};
    VkDeviceMemory mem{};
    uint8_t* map{};
};

class Gpu {
public:
    VkInstance instance{};
    VkPhysicalDevice phys{};
    VkDevice dev{};
    VkQueue queue{};
    uint32_t qfi{};
    std::string name;
    VkPhysicalDeviceMemoryProperties memProps{};
    PFN_vkImportSemaphoreFdKHR importSemaphoreFd{};

    explicit Gpu(const std::string& want) {
        VkApplicationInfo app{VK_STRUCTURE_TYPE_APPLICATION_INFO};
        app.pApplicationName = "vidinterp";
        app.apiVersion = VK_API_VERSION_1_2;
        VkInstanceCreateInfo ici{VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO};
        ici.pApplicationInfo = &app;
        check(vkCreateInstance(&ici, nullptr, &instance), "vkCreateInstance");

        uint32_t n{0};
        vkEnumeratePhysicalDevices(instance, &n, nullptr);
        std::vector<VkPhysicalDevice> devs(n);
        vkEnumeratePhysicalDevices(instance, &n, devs.data());
        for (auto d : devs) {
            VkPhysicalDeviceProperties p;
            vkGetPhysicalDeviceProperties(d, &p);
            const std::string nm{p.deviceName};
            const bool match{want.empty()
                ? p.deviceType == VK_PHYSICAL_DEVICE_TYPE_DISCRETE_GPU
                : nm.find(want) != std::string::npos};
            if (match) { phys = d; name = nm; break; }
        }
        if (!phys) throw std::runtime_error("no matching Vulkan device (use --gpu)");
        vkGetPhysicalDeviceMemoryProperties(phys, &memProps);

        // Any queue that can transfer; prefer a transfer-only one so copies overlap
        // with lsfg's compute work.
        vkGetPhysicalDeviceQueueFamilyProperties(phys, &n, nullptr);
        std::vector<VkQueueFamilyProperties> qf(n);
        vkGetPhysicalDeviceQueueFamilyProperties(phys, &n, qf.data());
        std::optional<uint32_t> best;
        for (uint32_t i = 0; i < n; i++) {
            const auto f{qf[i].queueFlags};
            if (!(f & (VK_QUEUE_TRANSFER_BIT | VK_QUEUE_COMPUTE_BIT | VK_QUEUE_GRAPHICS_BIT)))
                continue;
            const bool transferOnly{!(f & (VK_QUEUE_COMPUTE_BIT | VK_QUEUE_GRAPHICS_BIT))};
            if (!best || transferOnly) best = i;
            if (transferOnly) break;
        }
        qfi = best.value();

        const float prio{1.0F};
        VkDeviceQueueCreateInfo qci{VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO};
        qci.queueFamilyIndex = qfi;
        qci.queueCount = 1;
        qci.pQueuePriorities = &prio;
        const std::array<const char*, 2> exts{
            VK_KHR_EXTERNAL_MEMORY_FD_EXTENSION_NAME,
            VK_KHR_EXTERNAL_SEMAPHORE_FD_EXTENSION_NAME
        };
        VkPhysicalDeviceVulkan12Features f12{VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_VULKAN_1_2_FEATURES};
        f12.timelineSemaphore = VK_TRUE;
        VkDeviceCreateInfo dci{VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO};
        dci.pNext = &f12;
        dci.queueCreateInfoCount = 1;
        dci.pQueueCreateInfos = &qci;
        dci.enabledExtensionCount = exts.size();
        dci.ppEnabledExtensionNames = exts.data();
        check(vkCreateDevice(phys, &dci, nullptr, &dev), "vkCreateDevice");
        vkGetDeviceQueue(dev, qfi, 0, &queue);
        importSemaphoreFd = reinterpret_cast<PFN_vkImportSemaphoreFdKHR>(
            vkGetDeviceProcAddr(dev, "vkImportSemaphoreFdKHR"));
        if (!importSemaphoreFd) throw std::runtime_error("vkImportSemaphoreFdKHR missing");
    }

    uint32_t memType(uint32_t bits, VkMemoryPropertyFlags want) const {
        for (uint32_t i = 0; i < memProps.memoryTypeCount; i++)
            if ((bits & (1U << i)) && (memProps.memoryTypes[i].propertyFlags & want) == want)
                return i;
        throw std::runtime_error("no suitable memory type");
    }

    Buffer hostBuffer(VkDeviceSize size, VkBufferUsageFlags usage, bool readback) {
        Buffer b;
        VkBufferCreateInfo bci{VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO};
        bci.size = size;
        bci.usage = usage;
        check(vkCreateBuffer(dev, &bci, nullptr, &b.buf), "vkCreateBuffer");
        VkMemoryRequirements req;
        vkGetBufferMemoryRequirements(dev, b.buf, &req);
        VkMemoryAllocateInfo mai{VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO};
        mai.allocationSize = req.size;
        const VkMemoryPropertyFlags base{VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT
            | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT};
        try {
            // Reading uncached memory is very slow, so readbacks want HOST_CACHED.
            mai.memoryTypeIndex = memType(req.memoryTypeBits,
                readback ? base | VK_MEMORY_PROPERTY_HOST_CACHED_BIT : base);
        } catch (...) {
            mai.memoryTypeIndex = memType(req.memoryTypeBits, base);
        }
        check(vkAllocateMemory(dev, &mai, nullptr, &b.mem), "vkAllocateMemory(host)");
        check(vkBindBufferMemory(dev, b.buf, b.mem, 0), "vkBindBufferMemory");
        void* p{};
        check(vkMapMemory(dev, b.mem, 0, VK_WHOLE_SIZE, 0, &p), "vkMapMemory");
        b.map = static_cast<uint8_t*>(p);
        return b;
    }

    /// Create an image with exactly lsfg-vk's create info and bind the imported fd.
    VkImage importImage(int fd, VkFormat fmt, uint32_t w, uint32_t h, uint32_t layers,
            VkImageUsageFlags usage) {
        VkExternalMemoryImageCreateInfo ext{VK_STRUCTURE_TYPE_EXTERNAL_MEMORY_IMAGE_CREATE_INFO};
        ext.handleTypes = VK_EXTERNAL_MEMORY_HANDLE_TYPE_OPAQUE_FD_BIT;
        VkImageCreateInfo ici{VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO};
        ici.pNext = &ext;
        ici.imageType = VK_IMAGE_TYPE_2D;
        ici.format = fmt;
        ici.extent = {w, h, 1};
        ici.mipLevels = 1;
        ici.arrayLayers = layers;
        ici.samples = VK_SAMPLE_COUNT_1_BIT;
        ici.tiling = VK_IMAGE_TILING_OPTIMAL;
        ici.usage = usage;
        ici.sharingMode = VK_SHARING_MODE_EXCLUSIVE;
        ici.initialLayout = VK_IMAGE_LAYOUT_UNDEFINED;
        VkImage img{};
        check(vkCreateImage(dev, &ici, nullptr, &img), "vkCreateImage(import)");

        VkMemoryRequirements req;
        vkGetImageMemoryRequirements(dev, img, &req);
        VkMemoryDedicatedAllocateInfo ded{VK_STRUCTURE_TYPE_MEMORY_DEDICATED_ALLOCATE_INFO};
        ded.image = img;
        VkImportMemoryFdInfoKHR imp{VK_STRUCTURE_TYPE_IMPORT_MEMORY_FD_INFO_KHR};
        imp.pNext = &ded;
        imp.handleType = VK_EXTERNAL_MEMORY_HANDLE_TYPE_OPAQUE_FD_BIT;
        imp.fd = fd;  // ownership passes to the driver on success
        VkMemoryAllocateInfo mai{VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO};
        mai.pNext = &imp;
        mai.allocationSize = req.size;
        mai.memoryTypeIndex = memType(req.memoryTypeBits, VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT);
        VkDeviceMemory mem{};
        check(vkAllocateMemory(dev, &mai, nullptr, &mem), "vkAllocateMemory(import)");
        check(vkBindImageMemory(dev, img, mem, 0), "vkBindImageMemory(import)");
        return img;
    }

    VkSemaphore importTimeline(int fd) {
        VkSemaphoreTypeCreateInfo tci{VK_STRUCTURE_TYPE_SEMAPHORE_TYPE_CREATE_INFO};
        tci.semaphoreType = VK_SEMAPHORE_TYPE_TIMELINE;
        VkSemaphoreCreateInfo sci{VK_STRUCTURE_TYPE_SEMAPHORE_CREATE_INFO};
        sci.pNext = &tci;
        VkSemaphore s{};
        check(vkCreateSemaphore(dev, &sci, nullptr, &s), "vkCreateSemaphore(import)");
        VkImportSemaphoreFdInfoKHR imp{VK_STRUCTURE_TYPE_IMPORT_SEMAPHORE_FD_INFO_KHR};
        imp.semaphore = s;
        imp.handleType = VK_EXTERNAL_SEMAPHORE_HANDLE_TYPE_OPAQUE_FD_BIT;
        imp.fd = fd;
        check(importSemaphoreFd(dev, &imp), "vkImportSemaphoreFdKHR");
        return s;
    }

    VkSemaphore timeline() {
        VkSemaphoreTypeCreateInfo tci{VK_STRUCTURE_TYPE_SEMAPHORE_TYPE_CREATE_INFO};
        tci.semaphoreType = VK_SEMAPHORE_TYPE_TIMELINE;
        VkSemaphoreCreateInfo sci{VK_STRUCTURE_TYPE_SEMAPHORE_CREATE_INFO};
        sci.pNext = &tci;
        VkSemaphore s{};
        check(vkCreateSemaphore(dev, &sci, nullptr, &s), "vkCreateSemaphore");
        return s;
    }

    void wait(VkSemaphore s, uint64_t v) {
        VkSemaphoreWaitInfo wi{VK_STRUCTURE_TYPE_SEMAPHORE_WAIT_INFO};
        wi.semaphoreCount = 1;
        wi.pSemaphores = &s;
        wi.pValues = &v;
        check(vkWaitSemaphores(dev, &wi, UINT64_MAX), "vkWaitSemaphores");
    }

    /// Submit one command buffer with timeline waits/signals.
    void submit(VkCommandBuffer cb,
            std::initializer_list<std::pair<VkSemaphore, uint64_t>> waits,
            std::initializer_list<std::pair<VkSemaphore, uint64_t>> signals) {
        std::vector<VkSemaphore> ws, ss;
        std::vector<uint64_t> wv, sv;
        std::vector<VkPipelineStageFlags> stages;
        for (auto& [s, v] : waits) {
            ws.push_back(s); wv.push_back(v);
            stages.push_back(VK_PIPELINE_STAGE_ALL_COMMANDS_BIT);
        }
        for (auto& [s, v] : signals) { ss.push_back(s); sv.push_back(v); }
        VkTimelineSemaphoreSubmitInfo tsi{VK_STRUCTURE_TYPE_TIMELINE_SEMAPHORE_SUBMIT_INFO};
        tsi.waitSemaphoreValueCount = wv.size();
        tsi.pWaitSemaphoreValues = wv.data();
        tsi.signalSemaphoreValueCount = sv.size();
        tsi.pSignalSemaphoreValues = sv.data();
        VkSubmitInfo si{VK_STRUCTURE_TYPE_SUBMIT_INFO};
        si.pNext = &tsi;
        si.waitSemaphoreCount = ws.size();
        si.pWaitSemaphores = ws.data();
        si.pWaitDstStageMask = stages.data();
        si.commandBufferCount = 1;
        si.pCommandBuffers = &cb;
        si.signalSemaphoreCount = ss.size();
        si.pSignalSemaphores = ss.data();
        check(vkQueueSubmit(queue, 1, &si, VK_NULL_HANDLE), "vkQueueSubmit");
    }
};

/// Barrier on an imported image. Ownership comes from and goes back to
/// VK_QUEUE_FAMILY_EXTERNAL, because the other user is lsfg-vk's device; the
/// layout stays GENERAL, which is what lsfg-vk keeps its images in.
void externalBarrier(VkCommandBuffer cb, VkImage img, uint32_t layer, uint32_t qfi,
        bool acquire, VkAccessFlags access, VkImageLayout oldLayout) {
    VkImageMemoryBarrier b{VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER};
    b.srcAccessMask = acquire ? 0 : access;
    b.dstAccessMask = acquire ? access : 0;
    b.oldLayout = oldLayout;
    b.newLayout = VK_IMAGE_LAYOUT_GENERAL;
    b.srcQueueFamilyIndex = acquire ? VK_QUEUE_FAMILY_EXTERNAL : qfi;
    b.dstQueueFamilyIndex = acquire ? qfi : VK_QUEUE_FAMILY_EXTERNAL;
    b.image = img;
    b.subresourceRange = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, layer, 1};
    vkCmdPipelineBarrier(cb,
        acquire ? VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT : VK_PIPELINE_STAGE_TRANSFER_BIT,
        acquire ? VK_PIPELINE_STAGE_TRANSFER_BIT : VK_PIPELINE_STAGE_BOTTOM_OF_PIPE_BIT,
        0, 0, nullptr, 0, nullptr, 1, &b);
}

// ---------------------------------------------------------------- interpolator

class Interpolator {
public:
    Interpolator(const Options& o, Gpu& gpu) : m_o(o), m_gpu(gpu),
            m_frameBytes(size_t(o.width) * o.height * 4) {
        const auto depth{o.depth == 10 ? lsfgvk::BitDepth::A2B10G10R10 : lsfgvk::BitDepth::R8G8B8A8};
        const VkFormat fmt{o.depth == 10 ? VK_FORMAT_A2B10G10R10_UNORM_PACK32 : VK_FORMAT_R8G8B8A8_UNORM};

        std::function<void(const std::string&)> log;
        if (o.verbose) log = [](const std::string& s) { std::fprintf(stderr, "lsfg-vk: %s\n", s.c_str()); };
        m_lsfg = std::make_unique<lsfgvk::Instance>(gpu.name, o.dll, o.fp16, false, log);
        m_ctx = std::make_unique<lsfgvk::Context>(*m_lsfg, o.width, o.height, o.flow,
            o.performance, depth);
        const auto fds{m_ctx->exportFds()};

        const VkImageUsageFlags common{VK_IMAGE_USAGE_STORAGE_BIT | VK_IMAGE_USAGE_SAMPLED_BIT};
        m_src = gpu.importImage(fds.sourceFd, fmt, o.width, o.height, 2,
            common | VK_IMAGE_USAGE_TRANSFER_DST_BIT);
        m_dst = gpu.importImage(fds.destinationFd, fmt, o.width, o.height, 1,
            common | VK_IMAGE_USAGE_TRANSFER_SRC_BIT);
        m_sync = gpu.importTimeline(fds.syncFd);
        m_mine = gpu.timeline();

        VkCommandPoolCreateInfo pci{VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO};
        pci.queueFamilyIndex = gpu.qfi;
        check(vkCreateCommandPool(gpu.dev, &pci, nullptr, &m_pool), "vkCreateCommandPool");

        for (auto& s : m_upload) s = gpu.hostBuffer(m_frameBytes, VK_BUFFER_USAGE_TRANSFER_SRC_BIT, false);
        m_readback.resize(o.readbackSlots);
        for (auto& r : m_readback) r = gpu.hostBuffer(m_frameBytes, VK_BUFFER_USAGE_TRANSFER_DST_BIT, true);

        // Pre-recorded command buffers: upload[staging][layer], readback[slot].
        // The first use of each imported image goes through UNDEFINED once (init()).
        for (uint32_t s = 0; s < 2; s++)
            for (uint32_t l = 0; l < 2; l++)
                m_uploadCb[s][l] = record([&](VkCommandBuffer cb) {
                    externalBarrier(cb, m_src, l, gpu.qfi, true, VK_ACCESS_TRANSFER_WRITE_BIT, VK_IMAGE_LAYOUT_GENERAL);
                    VkBufferImageCopy c{};
                    c.imageSubresource = {VK_IMAGE_ASPECT_COLOR_BIT, 0, l, 1};
                    c.imageExtent = {o.width, o.height, 1};
                    vkCmdCopyBufferToImage(cb, m_upload[s].buf, m_src, VK_IMAGE_LAYOUT_GENERAL, 1, &c);
                    externalBarrier(cb, m_src, l, gpu.qfi, false, VK_ACCESS_TRANSFER_WRITE_BIT, VK_IMAGE_LAYOUT_GENERAL);
                });
        for (uint32_t r = 0; r < o.readbackSlots; r++)
            m_readbackCb.push_back(record([&](VkCommandBuffer cb) {
                externalBarrier(cb, m_dst, 0, gpu.qfi, true, VK_ACCESS_TRANSFER_READ_BIT, VK_IMAGE_LAYOUT_GENERAL);
                VkBufferImageCopy c{};
                c.imageSubresource = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 0, 1};
                c.imageExtent = {o.width, o.height, 1};
                vkCmdCopyImageToBuffer(cb, m_dst, VK_IMAGE_LAYOUT_GENERAL, m_readback[r].buf, 1, &c);
                externalBarrier(cb, m_dst, 0, gpu.qfi, false, VK_ACCESS_TRANSFER_READ_BIT, VK_IMAGE_LAYOUT_GENERAL);
                VkMemoryBarrier hb{VK_STRUCTURE_TYPE_MEMORY_BARRIER};
                hb.srcAccessMask = VK_ACCESS_TRANSFER_WRITE_BIT;
                hb.dstAccessMask = VK_ACCESS_HOST_READ_BIT;
                vkCmdPipelineBarrier(cb, VK_PIPELINE_STAGE_TRANSFER_BIT, VK_PIPELINE_STAGE_HOST_BIT,
                    0, 1, &hb, 0, nullptr, 0, nullptr);
            }));

        // Give our aliases of the imported images a defined layout. lsfg-vk already put
        // its own handles in GENERAL; layouts are tracked per VkImage, not per memory.
        VkCommandBuffer init{record([&](VkCommandBuffer cb) {
            for (uint32_t l = 0; l < 2; l++)
                externalBarrier(cb, m_src, l, gpu.qfi, true, 0, VK_IMAGE_LAYOUT_UNDEFINED);
            externalBarrier(cb, m_dst, 0, gpu.qfi, true, 0, VK_IMAGE_LAYOUT_UNDEFINED);
        })};
        gpu.submit(init, {}, {{m_mine, ++m_mineValue}});
        gpu.wait(m_mine, m_mineValue);
    }

    size_t frameBytes() const { return m_frameBytes; }

    /// Staging buffer the next source frame must be written into.
    uint8_t* stagingForNext() {
        const uint32_t s{m_iteration % 2};
        const Timed t{P_STAGING_WAIT};
        if (m_uploadDone[s]) m_gpu.wait(m_mine, m_uploadDone[s]);  // previous use finished
        return m_upload[s].map;
    }

    /// Feed the frame in stagingForNext() and generate `total` frames between the
    /// previous source frame and this one. Every generated frame is passed to `sink`
    /// in order; with `discard` they are generated (lsfg-vk needs every iteration to
    /// run to completion) but not read back.
    template<typename Sink>
    void push(uint32_t total, bool discard, Sink&& sink) {
        // --- the value protocol on the shared timeline semaphore (see lsfgvk.cpp) ---
        // we signal v            -> lsfg's dispatch() pre-pass starts
        // lsfg signals v+1       -> generated frame 0 is in the destination image
        // we signal v+2          -> frame 0 is copied out, lsfg may overwrite it
        // lsfg signals v+3       -> generated frame 1 ... and so on.
        // After the last generated frame we signal nothing; the next upload does.
        const uint32_t s{m_iteration % 2}, layer{m_iteration % 2};
        {
            const Timed t{P_UPLOAD};
            m_gpu.submit(m_uploadCb[s][layer],
                {{m_sync, m_syncValue}},                       // previous iteration fully done
                {{m_sync, m_syncValue + 1}, {m_mine, ++m_mineValue}});
        }
        m_syncValue++;
        m_uploadDone[s] = m_mineValue;

        {
            const Timed t{P_DISPATCH};
            m_ctx->dispatch(total);
        }
        std::vector<uint64_t> pending;  // m_mine values of readbacks not yet written out
        pending.reserve(total);
        for (uint32_t k = 0; k < total; k++) {
            {
                const Timed t{P_ACQUIRE};
                m_ctx->acquire();
            }
            const uint64_t generated{++m_syncValue};
            const bool last{k + 1 == total};
            const uint32_t slot{k % m_o.readbackSlots};

            // Before reusing a readback slot, the frame in it must have been written out.
            if (!discard && k >= m_o.readbackSlots) {
                const uint32_t out{k - m_o.readbackSlots};
                {
                    const Timed t{P_READBACK_WAIT};
                    m_gpu.wait(m_mine, pending[out]);
                }
                sink(m_readback[out % m_o.readbackSlots].map);
            }

            if (discard) {
                // Nothing to copy, but lsfg's next acquire() waits for our release.
                if (!last) {
                    m_gpu.submit(emptyCb(), {{m_sync, generated}}, {{m_sync, ++m_syncValue}});
                }
                continue;
            }
            if (last) {
                m_gpu.submit(m_readbackCb[slot], {{m_sync, generated}}, {{m_mine, ++m_mineValue}});
            } else {
                m_gpu.submit(m_readbackCb[slot], {{m_sync, generated}},
                    {{m_mine, ++m_mineValue}, {m_sync, generated + 1}});
                m_syncValue++;
            }
            pending.push_back(m_mineValue);
        }
        if (!discard) {
            const uint32_t first{total > m_o.readbackSlots ? total - m_o.readbackSlots : 0};
            for (uint32_t k = first; k < total; k++) {
                {
                    const Timed t{P_READBACK_WAIT};
                    m_gpu.wait(m_mine, pending[k]);
                }
                sink(m_readback[k % m_o.readbackSlots].map);
            }
        }
        m_iteration++;
    }

    void finish() {
        m_ctx->idle();
        m_gpu.wait(m_sync, m_syncValue);
    }

private:
    template<typename F>
    VkCommandBuffer record(F&& body) {
        VkCommandBufferAllocateInfo ai{VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO};
        ai.commandPool = m_pool;
        ai.level = VK_COMMAND_BUFFER_LEVEL_PRIMARY;
        ai.commandBufferCount = 1;
        VkCommandBuffer cb{};
        check(vkAllocateCommandBuffers(m_gpu.dev, &ai, &cb), "vkAllocateCommandBuffers");
        VkCommandBufferBeginInfo bi{VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO};
        bi.flags = VK_COMMAND_BUFFER_USAGE_SIMULTANEOUS_USE_BIT;
        check(vkBeginCommandBuffer(cb, &bi), "vkBeginCommandBuffer");
        body(cb);
        check(vkEndCommandBuffer(cb), "vkEndCommandBuffer");
        return cb;
    }

    VkCommandBuffer emptyCb() {
        if (!m_empty) m_empty = record([](VkCommandBuffer) {});
        return m_empty;
    }

    const Options& m_o;
    Gpu& m_gpu;
    size_t m_frameBytes;
    std::unique_ptr<lsfgvk::Instance> m_lsfg;
    std::unique_ptr<lsfgvk::Context> m_ctx;
    VkImage m_src{}, m_dst{};
    VkSemaphore m_sync{}, m_mine{};
    uint64_t m_syncValue{0}, m_mineValue{0};
    VkCommandPool m_pool{};
    std::array<Buffer, 2> m_upload;
    std::array<uint64_t, 2> m_uploadDone{0, 0};
    std::vector<Buffer> m_readback;
    std::array<std::array<VkCommandBuffer, 2>, 2> m_uploadCb{};
    std::vector<VkCommandBuffer> m_readbackCb;
    VkCommandBuffer m_empty{};
    uint32_t m_iteration{0};
};

// ---------------------------------------------------------------- main loop

// ---------------------------------------------------------------- threaded I/O

template<typename T>
class Channel {
public:
    void push(T v) {
        { const std::lock_guard l{m_m}; m_q.push_back(std::move(v)); }
        m_cv.notify_one();
    }
    T pop() {
        std::unique_lock l{m_m};
        m_cv.wait(l, [&] { return !m_q.empty(); });
        T v{std::move(m_q.front())};
        m_q.pop_front();
        return v;
    }
private:
    std::mutex m_m;
    std::condition_variable m_cv;
    std::deque<T> m_q;
};

/// A pool of frame buffers moving between the main loop and one I/O thread, so that
/// pipe reads and writes overlap with the GPU work instead of adding to it.
/// Buffers are indices; -1 marks end of stream, -2 an I/O error.
struct FramePool {
    std::vector<std::vector<uint8_t>> bufs;
    Channel<int> free, full;
    FramePool(size_t count, size_t bytes) : bufs(count, std::vector<uint8_t>(bytes)) {
        for (int i = 0; i < int(count); i++) free.push(i);
    }
    uint8_t* data(int i) { return bufs[size_t(i)].data(); }
};

struct Stats {
    uint64_t in{0}, unique{0}, out{0}, generated{0}, cuts{0}, held{0};
};

int run(const Options& o) {
    Gpu gpu{o.gpu};
    std::fprintf(stderr, "vidinterp: %ux%u x%u on %s, flow %.2f%s, %d-bit%s\n",
        o.width, o.height, o.multiplier, gpu.name.c_str(), o.flow,
        o.performance ? ", performance" : "", o.depth, o.dedup ? ", dedup" : "");
    Interpolator ip{o, gpu};
    const size_t fb{ip.frameBytes()};
    const int in{STDIN_FILENO}, out{STDOUT_FILENO};
    grow_pipe(in);
    grow_pipe(out);
    Stats st;

    // Reader thread: stdin -> input pool. The main loop holds at most two input buffers
    // (prev and cur), so 5 leaves the reader up to 3 frames of read-ahead.
    FramePool inPool{5, fb};
    std::string ioError;
    std::thread reader{[&] {
        try {
            while (true) {
                const int i{inPool.free.pop()};
                if (i < 0) return;  // shutdown
                if (!read_full(in, inPool.data(i), fb)) { inPool.full.push(-1); return; }
                inPool.full.push(i);
            }
        } catch (const std::exception& e) {
            ioError = e.what();
            inPool.full.push(-2);
        }
    }};
    // Writer thread: output pool -> stdout. Output frames are copied into the pool
    // because their source (a readback slot, or prev/cur) is reused right away.
    FramePool outPool{8, fb};
    std::atomic<bool> writeFailed{false};
    std::thread writer{[&] {
        while (true) {
            const int i{outPool.full.pop()};
            if (i < 0) return;
            if (!writeFailed) {
                try { write_full(out, outPool.data(i), fb); }
                catch (const std::exception& e) { ioError = e.what(); writeFailed = true; }
            }
            outPool.free.push(i);
        }
    }};
    struct Joiner {  // stop the threads on every exit path
        FramePool& inPool; FramePool& outPool; std::thread& reader; std::thread& writer;
        ~Joiner() {
            outPool.full.push(-1);
            if (writer.joinable()) writer.join();
            inPool.free.push(-1);
            // The reader may be blocked in read() on a pipe nobody closes; on the error
            // path main() leaves with _Exit, so it is detached rather than joined.
            if (reader.joinable()) reader.detach();
        }
    } joiner{inPool, outPool, reader, writer};

    // prev/cur: input pool buffers. prev is the last unique frame fed to lsfg; its CPU
    // copy is needed for held and cut output.
    int prev{-1}, cur{-1};
    Thumb prevThumb, lastThumb;
    uint32_t prevRun{0};      // how many input frames `prev` stands for
    double prevMafd{0.0};
    bool havePrev{false};

    auto emit = [&](const uint8_t* p) {
        if (writeFailed) throw std::runtime_error(ioError);
        const Timed t{P_WRITE};
        const int i{outPool.free.pop()};
        std::memcpy(outPool.data(i), p, fb);
        outPool.full.push(i);
        st.out++;
    };
    auto emitRepeat = [&](const uint8_t* p, uint32_t n) { for (uint32_t i = 0; i < n; i++) emit(p); };

    const auto t0{std::chrono::steady_clock::now()};
    auto lastReport{t0};

    // Output for the interval prev -> cur is: prev itself, then run*M - 1 frames.
    // It is written when cur arrives, because only then is the interval known.
    auto interval = [&](bool cut) {
        const uint32_t span{prevRun * o.multiplier};
        emit(inPool.data(prev));
        if (cut) {
            // lsfg must still see cur so the next interval has the right previous frame.
            ip.push(1, true, [](const uint8_t*) {});
            const uint32_t half{(span - 1) / 2};
            emitRepeat(inPool.data(prev), half);
            emitRepeat(inPool.data(cur), span - 1 - half);
            st.cuts++;
            return;
        }
        if (prevRun > o.maxHold) {
            // A long still: hold, then interpolate only the final source interval.
            emitRepeat(inPool.data(prev), (prevRun - 1) * o.multiplier);
            ip.push(o.multiplier - 1, false, [&](const uint8_t* p) { emit(p); st.generated++; });
            return;
        }
        ip.push(span - 1, false, [&](const uint8_t* p) { emit(p); st.generated++; });
    };

    // Frames are read into ordinary memory and only then copied into the staging buffer:
    // staging memory is uncached (write-combined), and reading it back from the CPU for
    // the analysis or the prev/cur copies costs ~70 ms per 1080p frame.
    auto stage = [&](int frame) {
        uint8_t* staging{ip.stagingForNext()};
        const Timed t{P_UPLOAD};
        std::memcpy(staging, inPool.data(frame), fb);
    };

    while (true) {
        {
            const Timed t{P_READ};
            cur = inPool.full.pop();
        }
        if (cur == -2) throw std::runtime_error(ioError);
        if (cur < 0) break;
        st.in++;
        Thumb th;
        {
            const Timed t{P_ANALYSE};
            th = make_thumb(inPool.data(cur), o.width, o.height, o.depth);
        }

        if (!havePrev) {
            stage(cur);
            prev = cur;
            // First iteration: lsfg has no previous frame, run it once and discard.
            ip.push(1, true, [](const uint8_t*) {});
            prevThumb = lastThumb = th;
            prevRun = 1;
            havePrev = true;
            st.unique++;
            continue;
        }

        const double dLast{mafd(th, lastThumb)};
        lastThumb = th;
        if (o.dedup && dLast < o.dupThreshold) {  // held frame: extend prev's run
            inPool.free.push(cur);
            prevRun++;
            st.held++;
            continue;
        }

        // scdet-style score: a cut is a jump that is large both absolutely and
        // relative to the motion just before it.
        const double d{mafd(th, prevThumb)};
        const double score{std::min(d, std::fabs(d - prevMafd)) * 100.0 / 255.0};
        const bool cut{o.sceneThreshold > 0 && score >= o.sceneThreshold};
        prevMafd = d;

        stage(cur);
        interval(cut);
        inPool.free.push(prev);
        prev = cur;
        prevThumb = th;
        prevRun = 1;
        st.unique++;

        const auto now{std::chrono::steady_clock::now()};
        if (now - lastReport > std::chrono::seconds(5)) {
            lastReport = now;
            const double s{std::chrono::duration<double>(now - t0).count()};
            std::fprintf(stderr, "vidinterp: in %lu, out %lu, %.1f in-fps, %.1f out-fps\n",
                (unsigned long)st.in, (unsigned long)st.out, st.in / s, st.out / s);
        }
    }

    if (havePrev) emitRepeat(inPool.data(prev), prevRun * o.multiplier);  // last frame, full duration
    ip.finish();
    outPool.full.push(-1);  // drain the writer before reporting
    writer.join();
    if (writeFailed) throw std::runtime_error(ioError);

    const double s{std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count()};
    std::fprintf(stderr,
        "vidinterp: done. in %lu (unique %lu, held %lu), out %lu (generated %lu), cuts %lu, "
        "%.2f s, %.1f in-fps, %.1f out-fps\n",
        (unsigned long)st.in, (unsigned long)st.unique, (unsigned long)st.held,
        (unsigned long)st.out, (unsigned long)st.generated, (unsigned long)st.cuts,
        s, st.in / s, st.out / s);
    if (std::getenv("VIDINTERP_PROFILE")) {
        for (int p = 0; p < P_COUNT; p++)
            std::fprintf(stderr, "  %-15s %7.3f s  %5.1f%%\n", PHASE_NAMES[p], g_phase[p],
                100.0 * g_phase[p] / s);
    }
    return 0;
}

}  // namespace

int main(int argc, char** argv) {
    try {
        return run(parse(argc, argv));
    } catch (const std::exception& e) {
        std::fprintf(stderr, "vidinterp: error: %s\n", e.what());
        std::_Exit(1);  // a detached reader thread may still be inside read()
    }
}
