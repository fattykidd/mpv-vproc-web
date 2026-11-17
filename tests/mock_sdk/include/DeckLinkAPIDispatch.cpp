// Simulated DeckLink card for tests. Plays a scripted sequence of input signals
// and behaves like the driver: format detection callbacks, "no input" frames,
// HDR metadata and the pixel format the client enabled.
//
// MOCK_DECKLINK_SCRIPT   steps separated by ';', each one of
//                          nosignal:<frames>
//                          <mode>:<pixel>:<transfer>:<frames>[:<maxcll>]
//                        mode  = 1080p60 | 1080i5994 | 2160p23976 | 2160p5994
//                        pixel = yuv8 | yuv10 | yuv12 | rgb8 | rgb10
//                        transfer = sdr | pq | hlg
// MOCK_FRAME_INTERVAL_MS delay between frames (default 2)
// MOCK_DEVICES           comma-separated device names
//
// Every frame starts with a 32-byte header the tests use to check alignment:
//   "DLCMOCK1", u64 step, u64 frame-in-step, u32 pixel format, u32 width.

#include "DeckLinkAPI.h"

#include <atomic>
#include <chrono>
#include <cstdlib>
#include <cstring>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

const REFIID IID_IDeckLinkInput = {{1}};
const REFIID IID_IDeckLinkProfileAttributes = {{2}};
const REFIID IID_IDeckLinkVideoFrameMetadataExtensions = {{3}};
const REFIID IID_IDeckLinkVideoBuffer = {{4}};

namespace {

char* dup(const std::string& s) { return strdup(s.c_str()); }

template <class Base>
class RefCounted : public Base {
public:
    ULONG STDMETHODCALLTYPE AddRef() override { return ++refs_; }
    ULONG STDMETHODCALLTYPE Release() override {
        const ULONG r = --refs_;
        if (r == 0) delete this;
        return r;
    }
    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID, LPVOID* ppv) override {
        *ppv = nullptr;
        return E_NOINTERFACE;
    }

private:
    std::atomic<ULONG> refs_{1};
};

struct ModeInfo {
    const char* key;
    BMDDisplayMode mode;
    const char* name;
    long width, height;
    BMDTimeValue duration;
    BMDTimeScale scale;
    BMDFieldDominance field;
};

const ModeInfo kModes[] = {
    {"1080p60", bmdModeHD1080p6000, "1080p60", 1920, 1080, 1000, 60000, bmdProgressiveFrame},
    {"1080i5994", bmdModeHD1080i5994, "1080i59.94", 1920, 1080, 1001, 30000, bmdUpperFieldFirst},
    {"2160p23976", bmdMode4K2160p2398, "2160p23.98", 3840, 2160, 1001, 24000, bmdProgressiveFrame},
    {"2160p5994", bmdMode4K2160p5994, "2160p59.94", 3840, 2160, 1001, 60000, bmdProgressiveFrame},
};

const ModeInfo* find_mode(BMDDisplayMode m) {
    for (const auto& i : kModes)
        if (i.mode == m) return &i;
    return nullptr;
}

class DisplayMode : public RefCounted<IDeckLinkDisplayMode> {
public:
    explicit DisplayMode(const ModeInfo* i) : i_(i) {}
    HRESULT GetName(const char** name) override { *name = dup(i_->name); return S_OK; }
    BMDDisplayMode GetDisplayMode() override { return i_->mode; }
    long GetWidth() override { return i_->width; }
    long GetHeight() override { return i_->height; }
    HRESULT GetFrameRate(BMDTimeValue* d, BMDTimeScale* s) override { *d = i_->duration; *s = i_->scale; return S_OK; }
    BMDFieldDominance GetFieldDominance() override { return i_->field; }

private:
    const ModeInfo* i_;
};

long row_bytes_for(BMDPixelFormat pf, long width) {
    switch (pf) {
    case bmdFormat10BitYUV: return ((width + 47) / 48) * 128;
    case bmdFormat10BitRGB: return ((width + 63) / 64) * 256;
    case bmdFormat8BitBGRA: return width * 4;
    default: return width * 2;
    }
}

struct Step {
    bool signal = false;
    const ModeInfo* mode = nullptr;
    bool rgb = false;
    int bits = 10;
    int eotf = 0;  // CTA-861
    double max_cll = 0;
    int frames = 0;
};

class Frame;

class Metadata : public RefCounted<IDeckLinkVideoFrameMetadataExtensions> {
public:
    Metadata(int eotf, bool bt2020, double cll) : eotf_(eotf), bt2020_(bt2020), cll_(cll) {}
    HRESULT GetInt(BMDDeckLinkFrameMetadataID id, int64_t* v) override {
        if (id == bmdDeckLinkFrameMetadataColorspace) { *v = bt2020_ ? bmdColorspaceRec2020 : bmdColorspaceRec709; return S_OK; }
        if (id == bmdDeckLinkFrameMetadataHDRElectroOpticalTransferFunc) { *v = eotf_; return S_OK; }
        return E_FAIL;
    }
    HRESULT GetFloat(BMDDeckLinkFrameMetadataID id, double* v) override {
        switch (id) {
        case bmdDeckLinkFrameMetadataHDRDisplayPrimariesRedX: *v = 0.708; return S_OK;
        case bmdDeckLinkFrameMetadataHDRDisplayPrimariesRedY: *v = 0.292; return S_OK;
        case bmdDeckLinkFrameMetadataHDRDisplayPrimariesGreenX: *v = 0.170; return S_OK;
        case bmdDeckLinkFrameMetadataHDRDisplayPrimariesGreenY: *v = 0.797; return S_OK;
        case bmdDeckLinkFrameMetadataHDRDisplayPrimariesBlueX: *v = 0.131; return S_OK;
        case bmdDeckLinkFrameMetadataHDRDisplayPrimariesBlueY: *v = 0.046; return S_OK;
        case bmdDeckLinkFrameMetadataHDRWhitePointX: *v = 0.3127; return S_OK;
        case bmdDeckLinkFrameMetadataHDRWhitePointY: *v = 0.3290; return S_OK;
        case bmdDeckLinkFrameMetadataHDRMaxDisplayMasteringLuminance: *v = 1000; return S_OK;
        case bmdDeckLinkFrameMetadataHDRMinDisplayMasteringLuminance: *v = 0.0001; return S_OK;
        case bmdDeckLinkFrameMetadataHDRMaximumContentLightLevel: *v = cll_; return S_OK;
        case bmdDeckLinkFrameMetadataHDRMaximumFrameAverageLightLevel: *v = cll_ / 4; return S_OK;
        default: return E_FAIL;
        }
    }

private:
    int eotf_;
    bool bt2020_;
    double cll_;
};

class Buffer : public RefCounted<IDeckLinkVideoBuffer> {
public:
    Buffer(IDeckLinkVideoInputFrame* owner, void* data) : owner_(owner), data_(data) { owner_->AddRef(); }
    ~Buffer() override { owner_->Release(); }
    HRESULT GetBytes(void** b) override { if (!accessing_) return E_FAIL; *b = data_; return S_OK; }
    HRESULT StartAccess(BMDBufferAccessFlags) override { accessing_ = true; return S_OK; }
    HRESULT EndAccess(BMDBufferAccessFlags) override { accessing_ = false; return S_OK; }

private:
    IDeckLinkVideoInputFrame* owner_;
    void* data_;
    bool accessing_ = false;
};

std::atomic<int> g_live_frames{0};

class Frame : public RefCounted<IDeckLinkVideoInputFrame> {
public:
    Frame(long w, long h, BMDPixelFormat pf, BMDFrameFlags flags, const Step& step, uint64_t step_index, uint64_t n)
        : w_(w), h_(h), pf_(pf), flags_(flags), step_(step), bytes_(row_bytes_for(pf, w) * h) {
        ++g_live_frames;
        std::memcpy(bytes_.data(), "DLCMOCK1", 8);
        std::memcpy(bytes_.data() + 8, &step_index, 8);
        std::memcpy(bytes_.data() + 16, &n, 8);
        const uint32_t pf32 = pf, w32 = static_cast<uint32_t>(w);
        std::memcpy(bytes_.data() + 24, &pf32, 4);
        std::memcpy(bytes_.data() + 28, &w32, 4);
    }
    ~Frame() override { --g_live_frames; }

    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID iid, LPVOID* ppv) override {
        if (iid == IID_IDeckLinkVideoFrameMetadataExtensions) {
            *ppv = static_cast<IDeckLinkVideoFrameMetadataExtensions*>(
                new Metadata(step_.eotf, step_.eotf >= 2, step_.max_cll));
            return S_OK;
        }
        if (iid == IID_IDeckLinkVideoBuffer) {
            *ppv = static_cast<IDeckLinkVideoBuffer*>(new Buffer(this, bytes_.data()));
            return S_OK;
        }
        *ppv = nullptr;
        return E_NOINTERFACE;
    }
    long GetWidth() override { return w_; }
    long GetHeight() override { return h_; }
    long GetRowBytes() override { return row_bytes_for(pf_, w_); }
    BMDPixelFormat GetPixelFormat() override { return pf_; }
    BMDFrameFlags GetFlags() override { return flags_; }
#if MOCK_DECKLINK_API_VERSION < 0x0e030000
    HRESULT GetBytes(void** b) override { *b = bytes_.data(); return S_OK; }
#endif

private:
    long w_, h_;
    BMDPixelFormat pf_;
    BMDFrameFlags flags_;
    Step step_;
    std::vector<uint8_t> bytes_;
};

std::vector<Step> parse_script(const char* text) {
    std::vector<Step> steps;
    std::string s = text ? text : "1080p60:yuv10:sdr:100000";
    size_t start = 0;
    while (start <= s.size()) {
        size_t end = s.find(';', start);
        if (end == std::string::npos) end = s.size();
        std::string item = s.substr(start, end - start);
        start = end + 1;
        if (item.empty()) continue;

        std::vector<std::string> f;
        size_t p = 0;
        while (p <= item.size()) {
            size_t q = item.find(':', p);
            if (q == std::string::npos) q = item.size();
            f.push_back(item.substr(p, q - p));
            p = q + 1;
        }
        Step st;
        if (f[0] == "nosignal") {
            st.frames = std::atoi(f[1].c_str());
        } else {
            st.signal = true;
            for (const auto& m : kModes)
                if (f[0] == m.key) st.mode = &m;
            st.rgb = f[1].rfind("rgb", 0) == 0;
            st.bits = std::atoi(f[1].c_str() + 3);
            st.eotf = f[2] == "pq" ? 2 : f[2] == "hlg" ? 3 : 0;
            st.frames = std::atoi(f[3].c_str());
            if (f.size() > 4) st.max_cll = std::atof(f[4].c_str());
        }
        steps.push_back(st);
    }
    return steps;
}

class Input : public RefCounted<IDeckLinkInput> {
public:
    Input() : script_(parse_script(std::getenv("MOCK_DECKLINK_SCRIPT"))) {
        const char* iv = std::getenv("MOCK_FRAME_INTERVAL_MS");
        interval_ = std::chrono::milliseconds(iv ? std::atoi(iv) : 2);
    }
    ~Input() override { StopStreams(); }

    HRESULT GetDisplayMode(BMDDisplayMode mode, IDeckLinkDisplayMode** out) override {
        const ModeInfo* i = find_mode(mode);
        if (!i) return E_FAIL;
        *out = new DisplayMode(i);
        return S_OK;
    }
    HRESULT EnableVideoInput(BMDDisplayMode mode, BMDPixelFormat pf, BMDVideoInputFlags) override {
        std::lock_guard<std::mutex> l(m_);
        enabled_mode_ = find_mode(mode);
        enabled_pf_ = pf;
        return enabled_mode_ ? S_OK : E_FAIL;
    }
    HRESULT DisableVideoInput() override { return S_OK; }
    HRESULT StartStreams() override {
        paused_ = false;
        if (!running_.exchange(true)) thread_ = std::thread(&Input::run, this);
        return S_OK;
    }
    HRESULT StopStreams() override {
        running_ = false;
        if (thread_.joinable() && thread_.get_id() != std::this_thread::get_id()) thread_.join();
        return S_OK;
    }
    HRESULT PauseStreams() override { paused_ = true; return S_OK; }
    HRESULT FlushStreams() override { return S_OK; }
    HRESULT SetCallback(IDeckLinkInputCallback* cb) override { std::lock_guard<std::mutex> l(m_); cb_ = cb; return S_OK; }

private:
    static BMDPixelFormat class_of(bool rgb, int bits) {
        if (rgb) return bits == 8 ? bmdFormat8BitBGRA : bmdFormat10BitRGB;
        return bits == 8 ? bmdFormat8BitYUV : bmdFormat10BitYUV;
    }

    void run() {
        uint64_t step_index = 0;
        const ModeInfo* notified_mode = nullptr;
        uint32_t notified_flags = 0;
        while (running_) {
            const Step& step = script_[std::min<size_t>(step_index, script_.size() - 1)];
            const bool last = step_index + 1 >= script_.size();
            for (int n = 0; running_ && (n < step.frames || last); ++n) {
                std::this_thread::sleep_for(interval_);
                if (paused_) continue;
                IDeckLinkInputCallback* cb;
                const ModeInfo* mode;
                BMDPixelFormat pf;
                {
                    std::lock_guard<std::mutex> l(m_);
                    cb = cb_;
                    mode = enabled_mode_;
                    pf = enabled_pf_;
                }
                if (!cb || !mode) continue;

                if (step.signal) {
                    const uint32_t flags = (step.rgb ? bmdDetectedVideoInputRGB444 : bmdDetectedVideoInputYCbCr422) |
                                           (step.bits == 8    ? bmdDetectedVideoInput8BitDepth
                                            : step.bits == 12 ? bmdDetectedVideoInput12BitDepth
                                                              : bmdDetectedVideoInput10BitDepth);
                    const bool mismatch = step.mode != mode || class_of(step.rgb, step.bits) != pf;
                    if (mismatch && (notified_mode != step.mode || notified_flags != flags)) {
                        notified_mode = step.mode;
                        notified_flags = flags;
                        DisplayMode dm(step.mode);
                        cb->VideoInputFormatChanged(bmdVideoInputDisplayModeChanged, &dm, flags);
                        continue;
                    }
                }

                const BMDFrameFlags fflags =
                    step.signal ? (step.eotf ? bmdFrameContainsHDRMetadata : 0) : bmdFrameHasNoInputSource;
                Frame* frame = new Frame(mode->width, mode->height, pf, fflags, step, step_index, n);
                cb->VideoInputFrameArrived(frame, nullptr);
                frame->Release();
            }
            if (!last) ++step_index;
        }
    }

    std::vector<Step> script_;
    std::chrono::milliseconds interval_;
    std::mutex m_;
    IDeckLinkInputCallback* cb_ = nullptr;
    const ModeInfo* enabled_mode_ = nullptr;
    BMDPixelFormat enabled_pf_ = bmdFormat10BitYUV;
    std::atomic<bool> running_{false};
    std::atomic<bool> paused_{false};
    std::thread thread_;
};

class Attributes : public RefCounted<IDeckLinkProfileAttributes> {
public:
    explicit Attributes(int64_t id) : id_(id) {}
    HRESULT GetFlag(BMDDeckLinkAttributeID, bool* v) override { *v = true; return S_OK; }
    HRESULT GetInt(BMDDeckLinkAttributeID a, int64_t* v) override {
        if (a == BMDDeckLinkPersistentID) { *v = id_; return S_OK; }
        if (a == BMDDeckLinkVideoIOSupport) { *v = bmdDeviceSupportsCapture; return S_OK; }
        return E_FAIL;
    }

private:
    int64_t id_;
};

class Device : public RefCounted<IDeckLink> {
public:
    Device(std::string name, int64_t id) : name_(std::move(name)), id_(id) {}
    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID iid, LPVOID* ppv) override {
        if (iid == IID_IDeckLinkInput) { *ppv = static_cast<IDeckLinkInput*>(new Input()); return S_OK; }
        if (iid == IID_IDeckLinkProfileAttributes) {
            *ppv = static_cast<IDeckLinkProfileAttributes*>(new Attributes(id_));
            return S_OK;
        }
        *ppv = nullptr;
        return E_NOINTERFACE;
    }
    HRESULT GetModelName(const char** n) override { *n = dup(name_); return S_OK; }
    HRESULT GetDisplayName(const char** n) override { *n = dup(name_); return S_OK; }

private:
    std::string name_;
    int64_t id_;
};

class Iterator : public RefCounted<IDeckLinkIterator> {
public:
    Iterator() {
        const char* env = std::getenv("MOCK_DEVICES");
        std::string s = env ? env : "DeckLink Mini Recorder 4K";
        size_t p = 0;
        while (p <= s.size()) {
            size_t q = s.find(',', p);
            if (q == std::string::npos) q = s.size();
            if (q > p) names_.push_back(s.substr(p, q - p));
            p = q + 1;
        }
    }
    HRESULT Next(IDeckLink** out) override {
        if (next_ >= names_.size()) return S_FALSE;
        *out = new Device(names_[next_], 1000 + static_cast<int64_t>(next_));
        ++next_;
        return S_OK;
    }

private:
    std::vector<std::string> names_;
    size_t next_ = 0;
};

class ApiInfo : public RefCounted<IDeckLinkAPIInformation> {
public:
    HRESULT GetString(BMDDeckLinkAPIInformationID, const char** v) override { *v = dup("mock-driver"); return S_OK; }
};

}  // namespace

IDeckLinkIterator* CreateDeckLinkIteratorInstance() {
    if (std::getenv("MOCK_NO_DRIVER")) return nullptr;
    return new Iterator();
}

IDeckLinkAPIInformation* CreateDeckLinkAPIInformationInstance() { return new ApiInfo(); }
