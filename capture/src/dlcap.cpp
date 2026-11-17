/*
 * dlcap - Blackmagic DeckLink capture bridge for vproc.
 *
 * Captures one DeckLink input with automatic format detection and streams the
 * raw frames into FIFOs that mpv reads with its rawvideo demuxer. Unlike
 * ffmpeg's DeckLink input, dlcap reads the HDR metadata (EOTF, colorspace,
 * mastering display, MaxCLL/MaxFALL) that arrives with each frame and survives
 * format changes.
 *
 * A "segment" is an unbroken run of valid signal in one format. Every segment
 * gets its own FIFO, so a reader always starts on a frame boundary and never
 * sees frames of two different formats.
 *
 * Protocol (one JSON object per line):
 *   stdout  events   hello, devices, device, signal, segment, colorimetry,
 *                    reader_gone, stats, log, error, exit
 *   stdin   commands {"cmd":"new_segment"} {"cmd":"stats"} {"cmd":"quit"}
 *
 * dlcap exits when stdin closes, so it never outlives its supervisor.
 */

#include "DeckLinkAPI.h"

#include <algorithm>
#include <atomic>
#include <cctype>
#include <cerrno>
#include <chrono>
#include <cinttypes>
#include <cmath>
#include <condition_variable>
#include <csignal>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <deque>
#include <iterator>
#include <mutex>
#include <optional>
#include <string>
#include <thread>
#include <utility>
#include <vector>

#include <fcntl.h>
#include <getopt.h>
#include <poll.h>
#include <sys/stat.h>
#include <unistd.h>

#ifndef BLACKMAGIC_DECKLINK_API_VERSION
#error "DeckLink SDK headers not found: point DECKLINK_SDK_INCLUDE at the SDK's Linux/include folder"
#endif

// Desktop Video SDK 14.3 moved pixel access from the frame to IDeckLinkVideoBuffer.
#if BLACKMAGIC_DECKLINK_API_VERSION >= 0x0e030000
#define DLCAP_VIDEO_BUFFER_API 1
#endif

#define DLCAP_VERSION "1.0.0"

using namespace std::chrono_literals;

namespace {

std::atomic<bool> g_quit{false};

// ---------------------------------------------------------------------------
// JSON event output
// ---------------------------------------------------------------------------

class Json {
public:
    Json& str(const char* k, const std::string& v) { key(k); body_ += quote(v); return *this; }
    Json& integer(const char* k, int64_t v) { key(k); body_ += std::to_string(v); return *this; }
    Json& boolean(const char* k, bool v) { key(k); body_ += v ? "true" : "false"; return *this; }
    Json& raw(const char* k, const std::string& json) { key(k); body_ += json; return *this; }

    Json& num(const char* k, double v) {
        key(k);
        if (!std::isfinite(v)) {
            body_ += "null";
        } else {
            char buf[32];
            std::snprintf(buf, sizeof buf, "%.10g", v);
            body_ += buf;
        }
        return *this;
    }

    std::string text() const { return "{" + body_ + "}"; }

    static std::string quote(const std::string& s) {
        std::string out = "\"";
        for (unsigned char c : s) {
            switch (c) {
            case '"': out += "\\\""; break;
            case '\\': out += "\\\\"; break;
            case '\n': out += "\\n"; break;
            case '\r': out += "\\r"; break;
            case '\t': out += "\\t"; break;
            default:
                if (c < 0x20) {
                    char buf[8];
                    std::snprintf(buf, sizeof buf, "\\u%04x", c);
                    out += buf;
                } else {
                    out += static_cast<char>(c);
                }
            }
        }
        return out + "\"";
    }

private:
    void key(const char* k) {
        if (!body_.empty()) body_ += ',';
        body_ += quote(k);
        body_ += ':';
    }

    std::string body_;
};

std::mutex g_out_mutex;

void emit(const Json& event) {
    const std::string line = event.text();
    std::lock_guard<std::mutex> lock(g_out_mutex);
    std::fwrite(line.data(), 1, line.size(), stdout);
    std::fputc('\n', stdout);
    std::fflush(stdout);
}

void log_event(const char* level, const std::string& message) {
    emit(Json().str("event", "log").str("level", level).str("message", message));
}

void error_event(const std::string& code, const std::string& message) {
    emit(Json().str("event", "error").str("code", code).str("message", message));
}

// DeckLink returns heap strings on Linux that the caller must free().
std::string take_string(const char* s) {
    if (!s) return {};
    std::string out(s);
    std::free(const_cast<char*>(s));
    return out;
}

// ---------------------------------------------------------------------------
// Signal description
// ---------------------------------------------------------------------------

struct Colorimetry {
    std::string matrix = "bt709";   // bt601 | bt709 | bt2020
    std::string transfer = "sdr";   // sdr | pq | hlg
    bool has_mastering = false;
    double primaries[6] = {0, 0, 0, 0, 0, 0};  // R xy, G xy, B xy
    double white[2] = {0, 0};
    double max_luminance = 0;
    double min_luminance = 0;
    double max_cll = 0;
    double max_fall = 0;

    bool operator==(const Colorimetry& o) const {
        return matrix == o.matrix && transfer == o.transfer && has_mastering == o.has_mastering &&
               std::equal(std::begin(primaries), std::end(primaries), std::begin(o.primaries)) &&
               white[0] == o.white[0] && white[1] == o.white[1] &&
               max_luminance == o.max_luminance && min_luminance == o.min_luminance &&
               max_cll == o.max_cll && max_fall == o.max_fall;
    }
    bool operator!=(const Colorimetry& o) const { return !(*this == o); }

    std::string json() const {
        Json j;
        j.str("matrix", matrix).str("transfer", transfer).num("max_cll", max_cll).num("max_fall", max_fall);
        if (has_mastering) {
            char buf[256];
            std::snprintf(buf, sizeof buf, "[%.6g,%.6g,%.6g,%.6g,%.6g,%.6g]", primaries[0], primaries[1],
                          primaries[2], primaries[3], primaries[4], primaries[5]);
            std::string prim = buf;
            std::snprintf(buf, sizeof buf, "[%.6g,%.6g]", white[0], white[1]);
            Json m;
            m.raw("primaries", prim).raw("white", buf).num("max_luminance", max_luminance)
                .num("min_luminance", min_luminance);
            j.raw("mastering", m.text());
        } else {
            j.raw("mastering", "null");
        }
        return j.text();
    }
};

struct SignalFormat {
    BMDDisplayMode mode = bmdModeUnknown;
    std::string name;
    long width = 0;
    long height = 0;
    BMDTimeValue frame_duration = 0;
    BMDTimeScale time_scale = 0;
    BMDFieldDominance field = bmdProgressiveFrame;
    BMDPixelFormat pixel_format = bmdFormat10BitYUV;
    bool rgb = false;
    int bit_depth = 10;
};

const char* pixel_format_name(BMDPixelFormat pf) {
    switch (pf) {
    case bmdFormat10BitYUV: return "v210";
    case bmdFormat8BitYUV: return "2vuy";
    case bmdFormat10BitRGB: return "r210";
    case bmdFormat8BitBGRA: return "bgra";
    default: return "unknown";
    }
}

const char* field_name(BMDFieldDominance field) {
    switch (field) {
    case bmdUpperFieldFirst: return "tff";
    case bmdLowerFieldFirst: return "bff";
    case bmdProgressiveSegmentedFrame: return "psf";
    case bmdProgressiveFrame: return "progressive";
    default: return "unknown";
    }
}

// Follow the detected bit depth: a mismatch makes the driver keep reporting
// colorspace changes. 12-bit signals are captured at 10 bits, which is all the
// formats below (and mpv's decoders) carry.
BMDPixelFormat choose_pixel_format(BMDDetectedVideoInputFormatFlags flags, bool* rgb, int* bit_depth) {
    *bit_depth = (flags & bmdDetectedVideoInput8BitDepth)    ? 8
                 : (flags & bmdDetectedVideoInput12BitDepth) ? 12
                                                             : 10;
    *rgb = (flags & bmdDetectedVideoInputRGB444) != 0;
    if (*rgb) return *bit_depth == 8 ? bmdFormat8BitBGRA : bmdFormat10BitRGB;
    return *bit_depth == 8 ? bmdFormat8BitYUV : bmdFormat10BitYUV;
}

SignalFormat describe(IDeckLinkDisplayMode* mode, BMDPixelFormat pf, bool rgb, int bit_depth) {
    SignalFormat f;
    f.mode = mode->GetDisplayMode();
    const char* name = nullptr;
    if (mode->GetName(&name) == S_OK) f.name = take_string(name);
    f.width = mode->GetWidth();
    f.height = mode->GetHeight();
    mode->GetFrameRate(&f.frame_duration, &f.time_scale);
    f.field = mode->GetFieldDominance();
    f.pixel_format = pf;
    f.rgb = rgb;
    f.bit_depth = bit_depth;
    return f;
}

Colorimetry read_colorimetry(IDeckLinkVideoInputFrame* frame, const SignalFormat& fmt) {
    Colorimetry c;
    c.matrix = fmt.height <= 576 ? "bt601" : "bt709";

    IDeckLinkVideoFrameMetadataExtensions* meta = nullptr;
    if (frame->QueryInterface(IID_IDeckLinkVideoFrameMetadataExtensions, reinterpret_cast<void**>(&meta)) != S_OK ||
        !meta) {
        return c;
    }

    int64_t value = 0;
    if (meta->GetInt(bmdDeckLinkFrameMetadataColorspace, &value) == S_OK) {
        if (value == bmdColorspaceRec2020) c.matrix = "bt2020";
        else if (value == bmdColorspaceRec601) c.matrix = "bt601";
        else if (value == bmdColorspaceRec709) c.matrix = "bt709";
    }

    if (frame->GetFlags() & bmdFrameContainsHDRMetadata) {
        // CTA-861 EOTF codes: 0 SDR, 1 traditional HDR gamma, 2 PQ, 3 HLG.
        if (meta->GetInt(bmdDeckLinkFrameMetadataHDRElectroOpticalTransferFunc, &value) == S_OK) {
            c.transfer = value == 2 ? "pq" : value == 3 ? "hlg" : "sdr";
        }

        auto get = [&](BMDDeckLinkFrameMetadataID id, double* out) {
            double v = 0;
            if (meta->GetFloat(id, &v) == S_OK && std::isfinite(v)) {
                *out = v;
                return true;
            }
            return false;
        };

        c.has_mastering = get(bmdDeckLinkFrameMetadataHDRDisplayPrimariesRedX, &c.primaries[0]);
        get(bmdDeckLinkFrameMetadataHDRDisplayPrimariesRedY, &c.primaries[1]);
        get(bmdDeckLinkFrameMetadataHDRDisplayPrimariesGreenX, &c.primaries[2]);
        get(bmdDeckLinkFrameMetadataHDRDisplayPrimariesGreenY, &c.primaries[3]);
        get(bmdDeckLinkFrameMetadataHDRDisplayPrimariesBlueX, &c.primaries[4]);
        get(bmdDeckLinkFrameMetadataHDRDisplayPrimariesBlueY, &c.primaries[5]);
        get(bmdDeckLinkFrameMetadataHDRWhitePointX, &c.white[0]);
        get(bmdDeckLinkFrameMetadataHDRWhitePointY, &c.white[1]);
        get(bmdDeckLinkFrameMetadataHDRMaxDisplayMasteringLuminance, &c.max_luminance);
        get(bmdDeckLinkFrameMetadataHDRMinDisplayMasteringLuminance, &c.min_luminance);
        get(bmdDeckLinkFrameMetadataHDRMaximumContentLightLevel, &c.max_cll);
        get(bmdDeckLinkFrameMetadataHDRMaximumFrameAverageLightLevel, &c.max_fall);
        if (c.max_luminance <= 0) c.has_mastering = false;
    }

    meta->Release();
    return c;
}

// ---------------------------------------------------------------------------
// Frame ownership and queueing
// ---------------------------------------------------------------------------

// Owns one reference to a captured frame, tagged with its segment generation.
class FrameRef {
public:
    FrameRef() = default;
    FrameRef(IDeckLinkVideoInputFrame* frame, uint64_t generation) : frame_(frame), generation_(generation) {
        if (frame_) frame_->AddRef();
    }
    FrameRef(FrameRef&& o) noexcept : frame_(std::exchange(o.frame_, nullptr)), generation_(o.generation_) {}
    FrameRef& operator=(FrameRef&& o) noexcept {
        if (this != &o) {
            reset();
            frame_ = std::exchange(o.frame_, nullptr);
            generation_ = o.generation_;
        }
        return *this;
    }
    FrameRef(const FrameRef&) = delete;
    FrameRef& operator=(const FrameRef&) = delete;
    ~FrameRef() { reset(); }

    void reset() {
        if (frame_) frame_->Release();
        frame_ = nullptr;
    }
    IDeckLinkVideoInputFrame* get() const { return frame_; }
    uint64_t generation() const { return generation_; }

private:
    IDeckLinkVideoInputFrame* frame_ = nullptr;
    uint64_t generation_ = 0;
};

// Read access to a frame's pixels for the lifetime of this object.
class FrameBytes {
public:
    explicit FrameBytes(IDeckLinkVideoInputFrame* frame) {
#ifdef DLCAP_VIDEO_BUFFER_API
        if (frame->QueryInterface(IID_IDeckLinkVideoBuffer, reinterpret_cast<void**>(&buffer_)) == S_OK && buffer_ &&
            buffer_->StartAccess(bmdBufferAccessRead) == S_OK) {
            accessing_ = true;
            buffer_->GetBytes(&data_);
        }
#else
        frame->GetBytes(&data_);
#endif
    }
    ~FrameBytes() {
#ifdef DLCAP_VIDEO_BUFFER_API
        if (buffer_) {
            if (accessing_) buffer_->EndAccess(bmdBufferAccessRead);
            buffer_->Release();
        }
#endif
    }
    FrameBytes(const FrameBytes&) = delete;
    FrameBytes& operator=(const FrameBytes&) = delete;

    const uint8_t* data() const { return static_cast<const uint8_t*>(data_); }

private:
    void* data_ = nullptr;
#ifdef DLCAP_VIDEO_BUFFER_API
    IDeckLinkVideoBuffer* buffer_ = nullptr;
    bool accessing_ = false;
#endif
};

// Small bounded queue between the SDK callback and the FIFO writer. When the
// reader falls behind, the oldest frames are dropped so latency stays bounded.
class FrameQueue {
public:
    explicit FrameQueue(size_t depth) : depth_(depth ? depth : 1) {}

    size_t push(FrameRef&& frame) {
        std::lock_guard<std::mutex> lock(mutex_);
        size_t dropped = 0;
        while (queue_.size() >= depth_) {
            queue_.pop_front();
            ++dropped;
        }
        queue_.push_back(std::move(frame));
        cv_.notify_one();
        return dropped;
    }

    std::optional<FrameRef> pop(std::chrono::milliseconds timeout) {
        std::unique_lock<std::mutex> lock(mutex_);
        if (!cv_.wait_for(lock, timeout, [&] { return !queue_.empty(); })) return std::nullopt;
        FrameRef frame = std::move(queue_.front());
        queue_.pop_front();
        return frame;
    }

    void clear() {
        std::lock_guard<std::mutex> lock(mutex_);
        queue_.clear();
    }

    size_t size() const {
        std::lock_guard<std::mutex> lock(mutex_);
        return queue_.size();
    }

private:
    const size_t depth_;
    mutable std::mutex mutex_;
    std::condition_variable cv_;
    std::deque<FrameRef> queue_;
};

// ---------------------------------------------------------------------------
// Capture
// ---------------------------------------------------------------------------

struct Options {
    std::string device = "0";
    std::string fifo_dir = "/run/vproc";
    size_t queue_depth = 2;
    int pipe_size = 1 << 20;
    bool list_only = false;
};

struct Stats {
    std::atomic<uint64_t> captured{0};
    std::atomic<uint64_t> written{0};
    std::atomic<uint64_t> no_signal{0};
    std::atomic<uint64_t> dropped_queue{0};     // reader too slow
    std::atomic<uint64_t> dropped_stale{0};     // belonged to a finished segment
    std::atomic<uint64_t> dropped_no_reader{0}; // nobody was reading the segment
};

class Capture final : public IDeckLinkInputCallback {
public:
    Capture(IDeckLinkInput* input, Options options)
        : input_(input), options_(std::move(options)), queue_(options_.queue_depth) {}

    ~Capture() { stop(); }

    bool start() {
        // Any mode works as a starting point: format detection switches to the
        // real signal through VideoInputFormatChanged.
        const BMDDisplayMode initial_mode = bmdModeHD1080p6000;
        IDeckLinkDisplayMode* mode = nullptr;
        if (input_->GetDisplayMode(initial_mode, &mode) != S_OK || !mode) {
            error_event("mode", "the card does not report the 1080p60 start-up mode");
            return false;
        }
        {
            std::lock_guard<std::mutex> lock(state_mutex_);
            format_ = describe(mode, bmdFormat10BitYUV, false, 10);
        }
        mode->Release();

        if (input_->SetCallback(this) != S_OK) {
            error_event("callback", "could not register the capture callback");
            return false;
        }
        if (input_->EnableVideoInput(initial_mode, bmdFormat10BitYUV, bmdVideoInputEnableFormatDetection) != S_OK) {
            error_event("busy", "could not enable video input; is another application using the card?");
            return false;
        }
        enabled_mode_ = initial_mode;
        enabled_pixel_format_ = bmdFormat10BitYUV;

        writer_ = std::thread(&Capture::writer_loop, this);
        if (input_->StartStreams() != S_OK) {
            error_event("start", "could not start the capture stream");
            return false;
        }
        started_ = true;
        return true;
    }

    void stop() {
        stopping_ = true;
        if (started_) {
            input_->StopStreams();
            input_->DisableVideoInput();
            started_ = false;
        }
        input_->SetCallback(nullptr);
        if (writer_.joinable()) writer_.join();
        queue_.clear();
        std::lock_guard<std::mutex> lock(state_mutex_);
        remove_fifo_locked();
    }

    // Ends the current segment; the next valid frame opens a fresh FIFO.
    void request_new_segment() {
        std::lock_guard<std::mutex> lock(state_mutex_);
        begin_new_segment_locked();
    }

    void emit_stats() {
        uint64_t segment;
        bool present;
        {
            std::lock_guard<std::mutex> lock(state_mutex_);
            segment = announce_pending_ ? 0 : generation_;
            present = signal_present_;
        }
        emit(Json()
                 .str("event", "stats")
                 .integer("segment", static_cast<int64_t>(segment))
                 .boolean("signal", present)
                 .integer("captured", static_cast<int64_t>(stats_.captured))
                 .integer("written", static_cast<int64_t>(stats_.written))
                 .integer("no_signal", static_cast<int64_t>(stats_.no_signal))
                 .integer("dropped_queue", static_cast<int64_t>(stats_.dropped_queue))
                 .integer("dropped_stale", static_cast<int64_t>(stats_.dropped_stale))
                 .integer("dropped_no_reader", static_cast<int64_t>(stats_.dropped_no_reader))
                 .integer("queue", static_cast<int64_t>(queue_.size())));
    }

    // IUnknown: the object is owned by main(), so reference counting is nominal.
    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID, LPVOID* ppv) override {
        *ppv = nullptr;
        return E_NOINTERFACE;
    }
    ULONG STDMETHODCALLTYPE AddRef() override { return ++refs_; }
    ULONG STDMETHODCALLTYPE Release() override { return --refs_; }

    HRESULT STDMETHODCALLTYPE VideoInputFormatChanged(BMDVideoInputFormatChangedEvents,
                                                      IDeckLinkDisplayMode* mode,
                                                      BMDDetectedVideoInputFormatFlags flags) override {
        if (!mode) return S_OK;

        bool rgb = false;
        int bit_depth = 10;
        const BMDPixelFormat pixel_format = choose_pixel_format(flags, &rgb, &bit_depth);
        const BMDDisplayMode display_mode = mode->GetDisplayMode();

        // Only re-arm when something we capture with actually changed. This
        // also stops a 12-bit signal captured at 10 bits from looping.
        if (display_mode == enabled_mode_ && pixel_format == enabled_pixel_format_) return S_OK;

        input_->PauseStreams();
        if (input_->EnableVideoInput(display_mode, pixel_format, bmdVideoInputEnableFormatDetection) != S_OK) {
            error_event("enable", "could not switch the input to the detected format");
            input_->StartStreams();
            return S_OK;
        }
        input_->FlushStreams();
        input_->StartStreams();
        enabled_mode_ = display_mode;
        enabled_pixel_format_ = pixel_format;

        std::lock_guard<std::mutex> lock(state_mutex_);
        format_ = describe(mode, pixel_format, rgb, bit_depth);
        begin_new_segment_locked();
        log_event("info", "input format changed to " + format_.name + " " + pixel_format_name(pixel_format));
        return S_OK;
    }

    HRESULT STDMETHODCALLTYPE VideoInputFrameArrived(IDeckLinkVideoInputFrame* frame,
                                                     IDeckLinkAudioInputPacket*) override {
        if (!frame || stopping_) return S_OK;
        ++stats_.captured;

        std::unique_lock<std::mutex> lock(state_mutex_);

        if (frame->GetFlags() & bmdFrameHasNoInputSource) {
            ++stats_.no_signal;
            if (signal_present_ || !signal_reported_) {
                if (signal_present_) begin_new_segment_locked();
                signal_present_ = false;
                signal_reported_ = true;
                emit(Json().str("event", "signal").boolean("present", false));
            }
            return S_OK;
        }

        if (!signal_present_) {
            signal_present_ = true;
            signal_reported_ = true;
            emit(Json().str("event", "signal").boolean("present", true));
        }

        const Colorimetry colorimetry = read_colorimetry(frame, format_);

        if (announce_pending_) {
            if (!announce_segment_locked(frame, colorimetry)) return S_OK;
        } else if (colorimetry != colorimetry_) {
            colorimetry_ = colorimetry;
            emit(Json()
                     .str("event", "colorimetry")
                     .integer("segment", static_cast<int64_t>(generation_))
                     .raw("colorimetry", colorimetry.json()));
        }

        const uint64_t generation = generation_;
        lock.unlock();

        stats_.dropped_queue += queue_.push(FrameRef(frame, generation));
        return S_OK;
    }

private:
    std::string fifo_path(uint64_t generation) const {
        return options_.fifo_dir + "/seg-" + std::to_string(generation) + ".fifo";
    }

    void remove_fifo_locked() {
        if (!fifo_path_.empty()) {
            ::unlink(fifo_path_.c_str());
            fifo_path_.clear();
        }
    }

    void begin_new_segment_locked() {
        ++generation_;
        active_generation_.store(generation_);
        announce_pending_ = true;
        remove_fifo_locked();  // a reader that already has it open keeps reading to EOF
    }

    bool announce_segment_locked(IDeckLinkVideoInputFrame* frame, const Colorimetry& colorimetry) {
        // Trust the frame over the mode table for geometry.
        format_.width = frame->GetWidth();
        format_.height = frame->GetHeight();
        const long row_bytes = frame->GetRowBytes();
        const int64_t frame_bytes = static_cast<int64_t>(row_bytes) * format_.height;

        const std::string path = fifo_path(generation_);
        ::unlink(path.c_str());
        if (::mkfifo(path.c_str(), 0660) != 0) {
            error_event("fifo", "mkfifo " + path + ": " + std::strerror(errno));
            return false;
        }
        fifo_path_ = path;
        segment_frame_bytes_.store(frame_bytes);
        colorimetry_ = colorimetry;
        announce_pending_ = false;

        emit(Json()
                 .str("event", "segment")
                 .integer("segment", static_cast<int64_t>(generation_))
                 .str("path", path)
                 .str("mode", format_.name)
                 .integer("width", format_.width)
                 .integer("height", format_.height)
                 .integer("row_bytes", row_bytes)
                 .integer("frame_bytes", frame_bytes)
                 .integer("fps_num", static_cast<int64_t>(format_.time_scale))
                 .integer("fps_den", static_cast<int64_t>(format_.frame_duration))
                 .str("field", field_name(format_.field))
                 .str("pixel_format", pixel_format_name(format_.pixel_format))
                 .str("signal", format_.rgb ? "rgb444" : "ycbcr422")
                 .integer("bit_depth", format_.bit_depth)
                 .raw("colorimetry", colorimetry.json()));
        return true;
    }

    // Opens the segment FIFO without blocking, waiting for mpv to start reading.
    // Gives up when the segment ends or dlcap shuts down.
    int open_writer(uint64_t generation) {
        const std::string path = fifo_path(generation);
        while (!g_quit && !stopping_ && active_generation_.load() == generation) {
            const int fd = ::open(path.c_str(), O_WRONLY | O_NONBLOCK | O_CLOEXEC);
            if (fd >= 0) {
                ::fcntl(fd, F_SETPIPE_SZ, options_.pipe_size);  // best effort
                return fd;
            }
            if (errno != ENXIO && errno != EINTR) return -1;  // ENXIO: no reader yet
            std::this_thread::sleep_for(5ms);
        }
        return -1;
    }

    enum class WriteResult { Ok, ReaderGone, Aborted, Skipped };

    WriteResult write_frame(int fd, const FrameRef& frame) {
        IDeckLinkVideoInputFrame* f = frame.get();
        const int64_t total = static_cast<int64_t>(f->GetRowBytes()) * f->GetHeight();
        if (total != segment_frame_bytes_.load()) return WriteResult::Skipped;

        FrameBytes bytes(f);
        if (!bytes.data()) return WriteResult::Skipped;

        int64_t offset = 0;
        while (offset < total) {
            const ssize_t n = ::write(fd, bytes.data() + offset, static_cast<size_t>(total - offset));
            if (n > 0) {
                offset += n;
                continue;
            }
            if (n < 0 && errno == EINTR) continue;
            if (n < 0 && errno == EAGAIN) {
                if (g_quit || stopping_ || active_generation_.load() != frame.generation())
                    return WriteResult::Aborted;
                pollfd p{fd, POLLOUT, 0};
                ::poll(&p, 1, 50);
                if (p.revents & POLLERR) return WriteResult::ReaderGone;
                continue;
            }
            return WriteResult::ReaderGone;  // EPIPE or another hard error
        }
        return WriteResult::Ok;
    }

    void writer_loop() {
        int fd = -1;
        uint64_t fd_generation = 0;
        uint64_t abandoned_generation = 0;

        auto close_fd = [&] {
            if (fd >= 0) ::close(fd);
            fd = -1;
        };

        while (!g_quit && !stopping_) {
            std::optional<FrameRef> item = queue_.pop(100ms);
            const uint64_t active = active_generation_.load();

            // The segment ended: closing our end gives the reader a clean EOF.
            if (fd >= 0 && fd_generation != active) close_fd();
            if (!item) continue;

            FrameRef frame = std::move(*item);
            if (frame.generation() != active) {
                ++stats_.dropped_stale;
                continue;
            }
            if (frame.generation() == abandoned_generation) {
                ++stats_.dropped_no_reader;
                continue;
            }
            if (fd < 0) {
                fd = open_writer(frame.generation());
                if (fd < 0) {
                    ++stats_.dropped_no_reader;
                    continue;
                }
                fd_generation = frame.generation();
            }

            switch (write_frame(fd, frame)) {
            case WriteResult::Ok:
                ++stats_.written;
                break;
            case WriteResult::Skipped:
                ++stats_.dropped_stale;
                break;
            case WriteResult::Aborted:
                close_fd();
                break;
            case WriteResult::ReaderGone:
                // A FIFO can't be rejoined mid-stream; the supervisor asks for
                // a new segment when it wants to read again.
                emit(Json().str("event", "reader_gone").integer("segment", static_cast<int64_t>(fd_generation)));
                abandoned_generation = fd_generation;
                close_fd();
                break;
            }
        }
        close_fd();
    }

    IDeckLinkInput* input_;
    Options options_;
    FrameQueue queue_;
    Stats stats_;
    std::thread writer_;
    std::atomic<ULONG> refs_{1};
    std::atomic<bool> stopping_{false};
    bool started_ = false;

    // Touched only from the SDK's format-change callback (and start()).
    BMDDisplayMode enabled_mode_ = bmdModeUnknown;
    BMDPixelFormat enabled_pixel_format_ = bmdFormat10BitYUV;

    std::mutex state_mutex_;
    SignalFormat format_;
    Colorimetry colorimetry_;
    uint64_t generation_ = 1;
    bool announce_pending_ = true;
    bool signal_present_ = false;
    bool signal_reported_ = false;
    std::string fifo_path_;

    std::atomic<uint64_t> active_generation_{1};
    std::atomic<int64_t> segment_frame_bytes_{0};
};

// ---------------------------------------------------------------------------
// Device discovery
// ---------------------------------------------------------------------------

struct Device {
    int index = 0;
    IDeckLink* decklink = nullptr;
    std::string name;
    std::string model;
    int64_t persistent_id = -1;
    bool can_capture = false;
    bool format_detection = false;
    bool hdr_metadata = false;
};

std::vector<Device> enumerate_devices(IDeckLinkIterator* iterator) {
    std::vector<Device> devices;
    IDeckLink* decklink = nullptr;
    while (iterator->Next(&decklink) == S_OK) {
        Device d;
        d.index = static_cast<int>(devices.size());
        d.decklink = decklink;

        const char* text = nullptr;
        if (decklink->GetDisplayName(&text) == S_OK) d.name = take_string(text);
        if (decklink->GetModelName(&text) == S_OK) d.model = take_string(text);

        IDeckLinkProfileAttributes* attributes = nullptr;
        if (decklink->QueryInterface(IID_IDeckLinkProfileAttributes, reinterpret_cast<void**>(&attributes)) == S_OK &&
            attributes) {
            int64_t value = 0;
            bool flag = false;
            if (attributes->GetInt(BMDDeckLinkPersistentID, &value) == S_OK) d.persistent_id = value;
            if (attributes->GetInt(BMDDeckLinkVideoIOSupport, &value) == S_OK)
                d.can_capture = (value & bmdDeviceSupportsCapture) != 0;
            if (attributes->GetFlag(BMDDeckLinkSupportsInputFormatDetection, &flag) == S_OK) d.format_detection = flag;
            if (attributes->GetFlag(BMDDeckLinkSupportsHDRMetadata, &flag) == S_OK) d.hdr_metadata = flag;
            attributes->Release();
        }
        devices.push_back(std::move(d));
    }
    return devices;
}

std::string device_json(const Device& d) {
    return Json()
        .integer("index", d.index)
        .str("name", d.name)
        .str("model", d.model)
        .integer("persistent_id", d.persistent_id)
        .boolean("can_capture", d.can_capture)
        .boolean("format_detection", d.format_detection)
        .boolean("hdr_metadata", d.hdr_metadata)
        .text();
}

std::string lower(std::string s) {
    for (auto& c : s) c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
    return s;
}

// Accepts an index ("0"), a persistent ID ("id:12345") or part of the name.
const Device* select_device(const std::vector<Device>& devices, const std::string& spec) {
    if (spec.rfind("id:", 0) == 0) {
        const int64_t id = std::strtoll(spec.c_str() + 3, nullptr, 0);
        for (const auto& d : devices)
            if (d.persistent_id == id) return &d;
        return nullptr;
    }
    if (!spec.empty() && spec.find_first_not_of("0123456789") == std::string::npos) {
        const size_t index = std::strtoul(spec.c_str(), nullptr, 10);
        return index < devices.size() ? &devices[index] : nullptr;
    }
    const std::string needle = lower(spec);
    for (const auto& d : devices)
        if (lower(d.name).find(needle) != std::string::npos) return &d;
    return nullptr;
}

// ---------------------------------------------------------------------------
// Command channel
// ---------------------------------------------------------------------------

// Extracts the "cmd" value from a one-line JSON object. The supervisor only
// ever sends {"cmd":"<word>"}, so a full parser isn't warranted.
std::string command_of(const std::string& line) {
    const size_t key = line.find("\"cmd\"");
    if (key == std::string::npos) return {};
    const size_t open = line.find('"', line.find(':', key));
    if (open == std::string::npos) return {};
    const size_t close = line.find('"', open + 1);
    if (close == std::string::npos) return {};
    return line.substr(open + 1, close - open - 1);
}

void run_command_loop(Capture& capture) {
    std::string pending;
    auto next_stats = std::chrono::steady_clock::now() + 2s;
    char buf[1024];

    while (!g_quit) {
        pollfd p{STDIN_FILENO, POLLIN, 0};
        const int ready = ::poll(&p, 1, 250);
        if (ready > 0) {
            const ssize_t n = ::read(STDIN_FILENO, buf, sizeof buf);
            if (n == 0) break;  // supervisor went away
            if (n < 0 && errno != EINTR && errno != EAGAIN) break;
            if (n > 0) pending.append(buf, static_cast<size_t>(n));

            size_t newline;
            while ((newline = pending.find('\n')) != std::string::npos) {
                const std::string cmd = command_of(pending.substr(0, newline));
                pending.erase(0, newline + 1);
                if (cmd == "new_segment") capture.request_new_segment();
                else if (cmd == "stats") capture.emit_stats();
                else if (cmd == "quit") g_quit = true;
                else if (!cmd.empty()) log_event("warning", "unknown command: " + cmd);
            }
        }
        if (std::chrono::steady_clock::now() >= next_stats) {
            capture.emit_stats();
            next_stats += 2s;
        }
    }
}

// ---------------------------------------------------------------------------
// main
// ---------------------------------------------------------------------------

void usage() {
    std::fprintf(stderr,
                 "usage: dlcap [--device SPEC] [--fifo-dir DIR] [--queue-depth N] [--list]\n"
                 "  --device SPEC       index (0), persistent id (id:1234) or part of the name\n"
                 "  --fifo-dir DIR      where segment FIFOs are created (default /run/vproc)\n"
                 "  --queue-depth N     frames buffered for a slow reader (default 2)\n"
                 "  --list              print the devices and exit\n");
}

bool parse_options(int argc, char** argv, Options* opts) {
    static const option long_options[] = {
        {"device", required_argument, nullptr, 'd'},
        {"fifo-dir", required_argument, nullptr, 'f'},
        {"queue-depth", required_argument, nullptr, 'q'},
        {"list", no_argument, nullptr, 'l'},
        {"help", no_argument, nullptr, 'h'},
        {nullptr, 0, nullptr, 0},
    };
    int c;
    while ((c = getopt_long(argc, argv, "d:f:q:lh", long_options, nullptr)) != -1) {
        switch (c) {
        case 'd': opts->device = optarg; break;
        case 'f': opts->fifo_dir = optarg; break;
        case 'q': opts->queue_depth = std::strtoul(optarg, nullptr, 10); break;
        case 'l': opts->list_only = true; break;
        default: return false;
        }
    }
    return true;
}

void on_signal(int) { g_quit = true; }

}  // namespace

int main(int argc, char** argv) {
    Options opts;
    if (!parse_options(argc, argv, &opts)) {
        usage();
        return 64;
    }

    std::signal(SIGPIPE, SIG_IGN);
    std::signal(SIGTERM, on_signal);
    std::signal(SIGINT, on_signal);

    std::string driver_version = "unknown";
    if (IDeckLinkAPIInformation* info = CreateDeckLinkAPIInformationInstance()) {
        const char* text = nullptr;
        if (info->GetString(BMDDeckLinkAPIVersion, &text) == S_OK) driver_version = take_string(text);
        info->Release();
    }
    emit(Json()
             .str("event", "hello")
             .str("version", DLCAP_VERSION)
             .str("sdk", BLACKMAGIC_DECKLINK_API_VERSION_STRING)
             .str("driver", driver_version));

    IDeckLinkIterator* iterator = CreateDeckLinkIteratorInstance();
    if (!iterator) {
        error_event("driver", "libDeckLinkAPI.so could not be loaded; install Desktop Video on the host and mount it");
        return 2;
    }

    std::vector<Device> devices = enumerate_devices(iterator);
    std::string list;
    for (const auto& d : devices) list += (list.empty() ? "" : ",") + device_json(d);
    emit(Json().str("event", "devices").raw("devices", "[" + list + "]"));

    auto release_all = [&] {
        for (auto& d : devices) d.decklink->Release();
        iterator->Release();
    };

    if (opts.list_only) {
        release_all();
        return 0;
    }

    const Device* device = select_device(devices, opts.device);
    if (!device) {
        error_event("no_device", devices.empty() ? "no DeckLink devices found"
                                                 : "no DeckLink device matches '" + opts.device + "'");
        release_all();
        return 3;
    }
    if (!device->can_capture || !device->format_detection) {
        error_event("unsupported", device->name + " does not support capture with format detection");
        release_all();
        return 4;
    }

    IDeckLinkInput* input = nullptr;
    if (device->decklink->QueryInterface(IID_IDeckLinkInput, reinterpret_cast<void**>(&input)) != S_OK || !input) {
        error_event("no_input", device->name + " has no input interface");
        release_all();
        return 4;
    }

    if (::mkdir(opts.fifo_dir.c_str(), 0770) != 0 && errno != EEXIST) {
        error_event("fifo", "cannot create " + opts.fifo_dir + ": " + std::strerror(errno));
        input->Release();
        release_all();
        return 5;
    }

    emit(Json().str("event", "device").raw("device", device_json(*device)));

    int status = 0;
    {
        Capture capture(input, opts);
        if (capture.start()) {
            run_command_loop(capture);
        } else {
            status = 5;
        }
        capture.stop();
    }

    input->Release();
    release_all();
    emit(Json().str("event", "exit").integer("status", status));
    return status;
}
