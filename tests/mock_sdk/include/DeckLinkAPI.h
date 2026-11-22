// Minimal stand-in for the Blackmagic DeckLink SDK, used only to compile and
// exercise dlcap in tests without hardware. It mirrors the subset of the
// Linux SDK that dlcap uses; it is not the real SDK and ships no Blackmagic code.
#pragma once

#include <cstdint>
#include <cstring>

// Like the real SDK, this header does not define the version macros; they live
// in DeckLinkAPIVersion.h. MOCK_DECKLINK_API_VERSION selects the frame API.
#ifndef MOCK_DECKLINK_API_VERSION
#define MOCK_DECKLINK_API_VERSION 0x0e020100
#endif

#define STDMETHODCALLTYPE
typedef int32_t HRESULT;
typedef uint32_t ULONG;
typedef void* LPVOID;
struct REFIID { uint8_t bytes[16]; };
inline bool operator==(const REFIID& a, const REFIID& b) { return std::memcmp(a.bytes, b.bytes, 16) == 0; }

#define S_OK ((HRESULT)0)
#define S_FALSE ((HRESULT)1)
#define E_FAIL ((HRESULT)0x80004005)
#define E_NOINTERFACE ((HRESULT)0x80004002)

constexpr uint32_t mk4(const char s[5]) {
    return (uint32_t(uint8_t(s[0])) << 24) | (uint32_t(uint8_t(s[1])) << 16) | (uint32_t(uint8_t(s[2])) << 8) |
           uint32_t(uint8_t(s[3]));
}

typedef uint32_t BMDDisplayMode;
typedef uint32_t BMDPixelFormat;
typedef uint32_t BMDFieldDominance;
typedef int64_t BMDTimeValue;
typedef int64_t BMDTimeScale;
typedef uint32_t BMDVideoInputFlags;
typedef uint32_t BMDVideoInputFormatChangedEvents;
typedef uint32_t BMDDetectedVideoInputFormatFlags;
typedef uint32_t BMDFrameFlags;
typedef uint32_t BMDDeckLinkFrameMetadataID;
typedef uint32_t BMDDeckLinkAttributeID;
typedef uint32_t BMDDeckLinkAPIInformationID;
typedef uint32_t BMDBufferAccessFlags;
typedef uint32_t BMDColorspace;
typedef uint32_t BMDDeckLinkHDMIInputEDIDID;
typedef uint32_t BMDDynamicRange;

enum : uint32_t {
    bmdModeUnknown = mk4("iunk"),
    bmdModeHD1080p6000 = mk4("Hp60"),
    bmdModeHD1080p5994 = mk4("Hp59"),
    bmdModeHD1080i5994 = mk4("Hi59"),
    bmdMode4K2160p2398 = mk4("4k23"),
    bmdMode4K2160p5994 = mk4("4k59"),

    bmdFormat8BitYUV = mk4("2vuy"),
    bmdFormat10BitYUV = mk4("v210"),
    bmdFormat8BitBGRA = mk4("BGRA"),
    bmdFormat10BitRGB = mk4("r210"),

    bmdUnknownFieldDominance = 0,
    bmdLowerFieldFirst = mk4("lowr"),
    bmdUpperFieldFirst = mk4("uppr"),
    bmdProgressiveFrame = mk4("prog"),
    bmdProgressiveSegmentedFrame = mk4("psf "),
};

enum : uint32_t { bmdVideoInputEnableFormatDetection = 1 << 0 };
enum : uint32_t {
    bmdVideoInputDisplayModeChanged = 1 << 0,
    bmdVideoInputFieldDominanceChanged = 1 << 1,
    bmdVideoInputColorspaceChanged = 1 << 2,
};
enum : uint32_t {
    bmdDetectedVideoInputYCbCr422 = 1 << 0,
    bmdDetectedVideoInputRGB444 = 1 << 1,
    bmdDetectedVideoInput8BitDepth = 1 << 3,
    bmdDetectedVideoInput10BitDepth = 1 << 4,
    bmdDetectedVideoInput12BitDepth = 1 << 5,
};
enum : uint32_t {
    bmdFrameHasNoInputSource = 1u << 31,
    bmdFrameContainsHDRMetadata = 1 << 1,
};
enum : uint32_t {
    bmdColorspaceRec601 = 1 << 0,
    bmdColorspaceRec709 = 1 << 1,
    bmdColorspaceRec2020 = 1 << 2,
};
enum : uint32_t {
    bmdDeckLinkFrameMetadataHDRElectroOpticalTransferFunc = mk4("eotf"),
    bmdDeckLinkFrameMetadataColorspace = mk4("cspc"),
    bmdDeckLinkFrameMetadataHDRDisplayPrimariesRedX = mk4("hdrx"),
    bmdDeckLinkFrameMetadataHDRDisplayPrimariesRedY = mk4("hdry"),
    bmdDeckLinkFrameMetadataHDRDisplayPrimariesGreenX = mk4("hdgx"),
    bmdDeckLinkFrameMetadataHDRDisplayPrimariesGreenY = mk4("hdgy"),
    bmdDeckLinkFrameMetadataHDRDisplayPrimariesBlueX = mk4("hdbx"),
    bmdDeckLinkFrameMetadataHDRDisplayPrimariesBlueY = mk4("hdby"),
    bmdDeckLinkFrameMetadataHDRWhitePointX = mk4("hdwx"),
    bmdDeckLinkFrameMetadataHDRWhitePointY = mk4("hdwy"),
    bmdDeckLinkFrameMetadataHDRMaxDisplayMasteringLuminance = mk4("hdml"),
    bmdDeckLinkFrameMetadataHDRMinDisplayMasteringLuminance = mk4("hmil"),
    bmdDeckLinkFrameMetadataHDRMaximumContentLightLevel = mk4("mcll"),
    bmdDeckLinkFrameMetadataHDRMaximumFrameAverageLightLevel = mk4("fall"),
};
enum : uint32_t {
    bmdDynamicRangeSDR = 0,
    bmdDynamicRangeHDRStaticPQ = 1 << 29,
    bmdDynamicRangeHDRStaticHLG = 1 << 30,
};
enum : uint32_t { bmdDeckLinkHDMIInputEDIDDynamicRange = mk4("HIDy") };
enum : uint32_t {
    BMDDeckLinkPersistentID = mk4("peid"),
    BMDDeckLinkVideoIOSupport = mk4("vios"),
    BMDDeckLinkSupportsInputFormatDetection = mk4("infd"),
    BMDDeckLinkSupportsHDRMetadata = mk4("hdrm"),
    BMDDeckLinkAPIVersion = mk4("vers"),
    bmdDeviceSupportsCapture = 1 << 0,
    bmdBufferAccessRead = 1 << 0,
};

extern const REFIID IID_IDeckLinkInput;
extern const REFIID IID_IDeckLinkProfileAttributes;
extern const REFIID IID_IDeckLinkVideoFrameMetadataExtensions;
extern const REFIID IID_IDeckLinkVideoBuffer;
extern const REFIID IID_IDeckLinkHDMIInputEDID;

class IUnknown {
public:
    virtual HRESULT STDMETHODCALLTYPE QueryInterface(REFIID iid, LPVOID* ppv) = 0;
    virtual ULONG STDMETHODCALLTYPE AddRef() = 0;
    virtual ULONG STDMETHODCALLTYPE Release() = 0;
    virtual ~IUnknown() = default;
};

class IDeckLinkDisplayMode : public IUnknown {
public:
    virtual HRESULT GetName(const char** name) = 0;
    virtual BMDDisplayMode GetDisplayMode() = 0;
    virtual long GetWidth() = 0;
    virtual long GetHeight() = 0;
    virtual HRESULT GetFrameRate(BMDTimeValue* frameDuration, BMDTimeScale* timeScale) = 0;
    virtual BMDFieldDominance GetFieldDominance() = 0;
};

class IDeckLinkVideoBuffer : public IUnknown {
public:
    virtual HRESULT GetBytes(void** buffer) = 0;
    virtual HRESULT StartAccess(BMDBufferAccessFlags flags) = 0;
    virtual HRESULT EndAccess(BMDBufferAccessFlags flags) = 0;
};

class IDeckLinkVideoInputFrame : public IUnknown {
public:
    virtual long GetWidth() = 0;
    virtual long GetHeight() = 0;
    virtual long GetRowBytes() = 0;
    virtual BMDPixelFormat GetPixelFormat() = 0;
    virtual BMDFrameFlags GetFlags() = 0;
#if MOCK_DECKLINK_API_VERSION < 0x0e030000
    virtual HRESULT GetBytes(void** buffer) = 0;
#endif
    virtual HRESULT GetStreamTime(BMDTimeValue* frameTime, BMDTimeValue* frameDuration, BMDTimeScale timeScale) = 0;
};

class IDeckLinkHDMIInputEDID : public IUnknown {
public:
    virtual HRESULT SetInt(BMDDeckLinkHDMIInputEDIDID id, int64_t value) = 0;
    virtual HRESULT GetInt(BMDDeckLinkHDMIInputEDIDID id, int64_t* value) = 0;
    virtual HRESULT WriteToEDID() = 0;
};

class IDeckLinkVideoFrameMetadataExtensions : public IUnknown {
public:
    virtual HRESULT GetInt(BMDDeckLinkFrameMetadataID id, int64_t* value) = 0;
    virtual HRESULT GetFloat(BMDDeckLinkFrameMetadataID id, double* value) = 0;
};

class IDeckLinkAudioInputPacket : public IUnknown {};

class IDeckLinkInputCallback : public IUnknown {
public:
    virtual HRESULT STDMETHODCALLTYPE VideoInputFormatChanged(BMDVideoInputFormatChangedEvents events,
                                                              IDeckLinkDisplayMode* mode,
                                                              BMDDetectedVideoInputFormatFlags flags) = 0;
    virtual HRESULT STDMETHODCALLTYPE VideoInputFrameArrived(IDeckLinkVideoInputFrame* video,
                                                             IDeckLinkAudioInputPacket* audio) = 0;
};

class IDeckLinkInput : public IUnknown {
public:
    virtual HRESULT GetDisplayMode(BMDDisplayMode mode, IDeckLinkDisplayMode** out) = 0;
    virtual HRESULT EnableVideoInput(BMDDisplayMode mode, BMDPixelFormat format, BMDVideoInputFlags flags) = 0;
    virtual HRESULT DisableVideoInput() = 0;
    virtual HRESULT StartStreams() = 0;
    virtual HRESULT StopStreams() = 0;
    virtual HRESULT PauseStreams() = 0;
    virtual HRESULT FlushStreams() = 0;
    virtual HRESULT SetCallback(IDeckLinkInputCallback* callback) = 0;
};

class IDeckLinkProfileAttributes : public IUnknown {
public:
    virtual HRESULT GetFlag(BMDDeckLinkAttributeID id, bool* value) = 0;
    virtual HRESULT GetInt(BMDDeckLinkAttributeID id, int64_t* value) = 0;
};

class IDeckLink : public IUnknown {
public:
    virtual HRESULT GetModelName(const char** name) = 0;
    virtual HRESULT GetDisplayName(const char** name) = 0;
};

class IDeckLinkIterator : public IUnknown {
public:
    virtual HRESULT Next(IDeckLink** out) = 0;
};

class IDeckLinkAPIInformation : public IUnknown {
public:
    virtual HRESULT GetString(BMDDeckLinkAPIInformationID id, const char** value) = 0;
};

IDeckLinkIterator* CreateDeckLinkIteratorInstance();
IDeckLinkAPIInformation* CreateDeckLinkAPIInformationInstance();
