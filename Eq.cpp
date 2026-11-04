// Spectra EQ C++ — same 10 bands and system-EQ file as Eq.py 2.6.18-drag.
// Build on Windows: build_cpp.bat
// Sound is changed by Equalizer APO (config\spectra.txt), not by replaying the speakers.

#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#include <mmdeviceapi.h>
#include <audioclient.h>
#include <mmreg.h>
#include <ksmedia.h>
#include <objbase.h>
#include <shellapi.h>

#include <algorithm>
#include <atomic>
#include <cctype>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

namespace {

constexpr int kBands = 10;
constexpr float kHz[kBands] = {20, 50, 100, 200, 500, 1000, 2000, 4000, 8000, 10000};
const wchar_t* kLabels[kBands] = {L"20", L"50", L"100", L"200", L"500", L"1k", L"2k", L"4k", L"8k", L"10k"};
constexpr float kGainMin = -12.f;
constexpr float kGainMax = 12.f;
constexpr float kTrimMin = -24.f;
constexpr float kTrimMax = 12.f;
constexpr float kFMin = 20.f;
constexpr float kFMax = 10000.f;
constexpr int kFft = 2048;
constexpr int kBars = 48;

struct Preset {
    const wchar_t* name;
    float g[kBands];
};

const Preset kPresets[] = {
    {L"Flat", {0, 0, 0, 0, 0, 0, 0, 0, 0, 0}},
    {L"Bass Boost", {8, 6, 4, 2, 0, 0, 0, 0, 0, 0}},
    {L"Treble", {0, 0, 0, 0, 0, 1, 3, 5, 7, 8}},
    {L"V-Shape", {6, 4, 1, -2, -4, -4, -2, 1, 4, 6}},
    {L"Vocal", {-3, -2, 0, 2, 5, 6, 4, 1, -1, -2}},
    {L"Electronic", {5, 4, 1, -1, 0, 2, 4, 5, 4, 3}},
    {L"Rock", {5, 3, 1, 0, -1, 1, 3, 4, 3, 2}},
    {L"Classical", {3, 2, 0, 0, 0, 0, 1, 2, 3, 4}},
    {L"Podcast", {-4, -2, 1, 4, 5, 4, 2, 0, -2, -4}},
    {L"Cinema", {6, 4, 2, 0, -1, 0, 2, 3, 2, 1}},
    {L"Night", {-2, 0, 2, 3, 2, 0, -2, -4, -6, -8}},
    {L"Loudness", {7, 5, 2, 0, -1, 0, 2, 4, 5, 4}},
};

float clampf(float v, float a, float b) { return v < a ? a : (v > b ? b : v); }

struct App {
    HWND hwnd = nullptr;
    float gains[kBands] = {};
    float preamp = 0.f;
    float master = 0.f;
    bool eqOn = false;
    bool muted = false;
    std::wstring status = L"Starting spectrum…";
    std::wstring preset = L"Flat";

    std::mutex specMx;
    float bars[kBars] = {};
    std::atomic<bool> run{true};
    std::thread cap;

    int drag = -1;  // 0..9 bands, 10 preamp, 11 master
    RECT sliders[kBands]{};
    RECT preRc{};
    RECT masterRc{};
    RECT eqRc{};
    RECT muteRc{};
    RECT presetRc[12]{};
    RECT specRc{};
};

App g;

std::wstring apoConfigDir() {
    const wchar_t* keys[] = {L"ProgramW6432", L"ProgramFiles", L"ProgramFiles(x86)"};
    for (const wchar_t* key : keys) {
        wchar_t buf[MAX_PATH] = {};
        DWORD n = GetEnvironmentVariableW(key, buf, MAX_PATH);
        if (!n || n >= MAX_PATH) continue;
        std::wstring path = std::wstring(buf) + L"\\EqualizerAPO\\config";
        if (GetFileAttributesW(path.c_str()) != INVALID_FILE_ATTRIBUTES) return path;
    }
    const wchar_t* roots[] = {L"C:\\Program Files\\EqualizerAPO\\config",
                              L"C:\\Program Files (x86)\\EqualizerAPO\\config"};
    for (const wchar_t* path : roots) {
        if (GetFileAttributesW(path) != INVALID_FILE_ATTRIBUTES) return path;
    }
    return L"";
}

bool writeText(const std::wstring& path, const std::string& body) {
    HANDLE h = CreateFileW(path.c_str(), GENERIC_WRITE, FILE_SHARE_READ, nullptr, CREATE_ALWAYS,
                           FILE_ATTRIBUTE_NORMAL, nullptr);
    if (h == INVALID_HANDLE_VALUE) return false;
    DWORD wrote = 0;
    BOOL ok = WriteFile(h, body.data(), (DWORD)body.size(), &wrote, nullptr);
    CloseHandle(h);
    return ok && wrote == body.size();
}

std::string readText(const std::wstring& path) {
    HANDLE h = CreateFileW(path.c_str(), GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE, nullptr,
                           OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (h == INVALID_HANDLE_VALUE) return "";
    std::string s;
    char buf[4096];
    DWORD n = 0;
    while (ReadFile(h, buf, sizeof buf, &n, nullptr) && n) s.append(buf, buf + n);
    CloseHandle(h);
    return s;
}

void askApoPermission(const std::wstring& folder) {
    std::wstring params = L"\"" + folder + L"\" /grant *S-1-5-32-545:(OI)(CI)M /C";
    ShellExecuteW(nullptr, L"runas", L"icacls.exe", params.c_str(), nullptr, SW_SHOWNORMAL);
}

bool applyApo() {
    std::wstring folder = apoConfigDir();
    if (folder.empty()) {
        g.status = L"Equalizer APO is not installed. Install it, tick Speakers, reboot, then press EQ ON.";
        return false;
    }
    float use[kBands];
    float pre = 0.f;
    if (!g.eqOn) {
        for (int i = 0; i < kBands; ++i) use[i] = 0.f;
    } else {
        for (int i = 0; i < kBands; ++i) use[i] = g.gains[i];
        pre = g.preamp + g.master;
        if (g.muted) pre = -120.f;
    }
    std::string body = "# Spectra EQ — this file is rewritten while Spectra is open.\nPreamp: ";
    char num[32];
    snprintf(num, sizeof num, "%.1f dB\nGraphicEQ: ", pre);
    body += num;
    for (int i = 0; i < kBands; ++i) {
        if (i) body += "; ";
        snprintf(num, sizeof num, "%d %.1f", (int)kHz[i], use[i]);
        body += num;
    }
    body += "\n";
    std::wstring spec = folder + L"\\spectra.txt";
    std::wstring cfg = folder + L"\\config.txt";
    if (!writeText(spec, body)) {
        askApoPermission(folder);
        g.status = L"Click Yes on the Windows prompt so Spectra can save the EQ, then press EQ ON again.";
        return false;
    }
    std::string existing = readText(cfg);
    std::string low = existing;
    for (char& c : low) c = (char)tolower((unsigned char)c);
    if (low.find("spectra.txt") == std::string::npos) {
        std::string next = "Include: spectra.txt\n" + existing;
        if (!writeText(cfg, next)) {
            askApoPermission(folder);
            g.status = L"Click Yes on the Windows prompt so Spectra can save the EQ, then press EQ ON again.";
            return false;
        }
    }
    if (g.eqOn && g.muted) g.status = L"System EQ muted.";
    else if (g.eqOn) g.status = L"System EQ ON. These knobs change every sound you play.";
    else g.status = L"EQ off. Sound is back to flat.";
    return true;
}

void fft2048(float* re, float* im) {
    int n = kFft;
    for (int i = 1, j = 0; i < n; ++i) {
        int bit = n >> 1;
        for (; j & bit; bit >>= 1) j ^= bit;
        j ^= bit;
        if (i < j) {
            std::swap(re[i], re[j]);
            std::swap(im[i], im[j]);
        }
    }
    for (int len = 2; len <= n; len <<= 1) {
        float ang = -6.28318530718f / (float)len;
        float wRe = std::cos(ang), wIm = std::sin(ang);
        for (int i = 0; i < n; i += len) {
            float cRe = 1.f, cIm = 0.f;
            for (int j = 0; j < len / 2; ++j) {
                int u = i + j, v = u + len / 2;
                float tRe = cRe * re[v] - cIm * im[v];
                float tIm = cRe * im[v] + cIm * re[v];
                re[v] = re[u] - tRe;
                im[v] = im[u] - tIm;
                re[u] += tRe;
                im[u] += tIm;
                float nRe = cRe * wRe - cIm * wIm;
                cIm = cRe * wIm + cIm * wRe;
                cRe = nRe;
            }
        }
    }
}

void pushSpectrum(const float* mono, int frames, int rate) {
    static float ring[kFft];
    static int pos = 0;
    static int seen = 0;
    static int since = 0;
    static float smooth[kBars] = {};
    if (rate < 8000) rate = 48000;
    for (int i = 0; i < frames; ++i) {
        ring[pos] = mono[i];
        pos = (pos + 1) % kFft;
        if (seen < kFft) ++seen;
        ++since;
    }
    if (seen < kFft || since < 512) return;
    since = 0;
    float ordered[kFft];
    for (int i = 0; i < kFft; ++i) ordered[i] = ring[(pos + i) % kFft];
    float re[kFft], im[kFft];
    for (int i = 0; i < kFft; ++i) {
        float w = 0.5f * (1.f - std::cos(6.28318530718f * i / (kFft - 1)));
        re[i] = ordered[i] * w;
        im[i] = 0.f;
    }
    fft2048(re, im);
    float mag[kFft / 2];
    for (int i = 0; i < kFft / 2; ++i) {
        float a = std::sqrt(re[i] * re[i] + im[i] * im[i]) / (kFft * 0.5f);
        mag[i] = 20.f * std::log10(a + 1e-8f);
    }
    float out[kBars];
    double log0 = std::log((double)kFMin);
    double log1 = std::log((double)kFMax);
    double binHz = (double)rate / (double)kFft;
    for (int b = 0; b < kBars; ++b) {
        double f0 = std::exp(log0 + (log1 - log0) * b / kBars);
        double f1 = std::exp(log0 + (log1 - log0) * (b + 1) / kBars);
        int i0 = std::max(1, (int)(f0 / binHz));
        int i1 = std::min(kFft / 2 - 1, (int)(f1 / binHz));
        if (i1 < i0) i1 = i0;
        float peak = -120.f;
        for (int i = i0; i <= i1; ++i) peak = std::max(peak, mag[i]);
        float norm = clampf((peak + 60.f) / 60.f, 0.f, 1.f);
        smooth[b] = smooth[b] * 0.72f + norm * 0.28f;
        out[b] = smooth[b];
    }
    std::lock_guard<std::mutex> lock(g.specMx);
    memcpy(g.bars, out, sizeof out);
}

bool isFloatFormat(const WAVEFORMATEX* fmt) {
    if (fmt->wFormatTag == WAVE_FORMAT_IEEE_FLOAT) return true;
    if (fmt->wFormatTag == WAVE_FORMAT_EXTENSIBLE) {
        auto* ext = reinterpret_cast<const WAVEFORMATEXTENSIBLE*>(fmt);
        return IsEqualGUID(ext->SubFormat, KSDATAFORMAT_SUBTYPE_IEEE_FLOAT);
    }
    return false;
}

void captureLoop() {
    HRESULT hr = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
    bool uninit = SUCCEEDED(hr);
    IMMDeviceEnumerator* en = nullptr;
    IMMDevice* dev = nullptr;
    IAudioClient* client = nullptr;
    IAudioCaptureClient* cap = nullptr;
    WAVEFORMATEX* mix = nullptr;
    hr = CoCreateInstance(__uuidof(MMDeviceEnumerator), nullptr, CLSCTX_ALL, __uuidof(IMMDeviceEnumerator),
                          (void**)&en);
    if (FAILED(hr)) {
        g.status = L"Audio device list failed.";
        if (uninit) CoUninitialize();
        return;
    }
    hr = en->GetDefaultAudioEndpoint(eRender, eConsole, &dev);
    if (FAILED(hr)) {
        g.status = L"No default speakers found.";
        en->Release();
        if (uninit) CoUninitialize();
        return;
    }
    hr = dev->Activate(__uuidof(IAudioClient), CLSCTX_ALL, nullptr, (void**)&client);
    if (FAILED(hr)) {
        g.status = L"Could not open the speaker device.";
        dev->Release();
        en->Release();
        if (uninit) CoUninitialize();
        return;
    }
    hr = client->GetMixFormat(&mix);
    if (FAILED(hr) || !mix) {
        g.status = L"Could not read the speaker format.";
        client->Release();
        dev->Release();
        en->Release();
        if (uninit) CoUninitialize();
        return;
    }
    hr = client->Initialize(AUDCLNT_SHAREMODE_SHARED, AUDCLNT_STREAMFLAGS_LOOPBACK, 10000000, 0, mix, nullptr);
    if (FAILED(hr)) {
        g.status = L"Speaker listen failed. Another app may have exclusive control.";
        CoTaskMemFree(mix);
        client->Release();
        dev->Release();
        en->Release();
        if (uninit) CoUninitialize();
        return;
    }
    hr = client->GetService(__uuidof(IAudioCaptureClient), (void**)&cap);
    if (FAILED(hr)) {
        g.status = L"Speaker listen failed.";
        CoTaskMemFree(mix);
        client->Release();
        dev->Release();
        en->Release();
        if (uninit) CoUninitialize();
        return;
    }
    client->Start();
    const bool flt = isFloatFormat(mix);
    const int ch = std::max(1, (int)mix->nChannels);
    const int rate = (int)mix->nSamplesPerSec;
    const int bits = mix->wBitsPerSample;
    std::vector<float> mono;
    while (g.run.load()) {
        UINT32 packet = 0;
        cap->GetNextPacketSize(&packet);
        if (!packet) {
            Sleep(8);
            continue;
        }
        while (packet && g.run.load()) {
            BYTE* data = nullptr;
            UINT32 frames = 0;
            DWORD flags = 0;
            if (FAILED(cap->GetBuffer(&data, &frames, &flags, nullptr, nullptr))) break;
            mono.assign(frames, 0.f);
            bool silent = (flags & AUDCLNT_BUFFERFLAGS_SILENT) || !data;
            if (!silent && flt) {
                auto* s = reinterpret_cast<float*>(data);
                for (UINT32 i = 0; i < frames; ++i) {
                    float acc = 0.f;
                    for (int c = 0; c < ch; ++c) acc += s[i * ch + c];
                    mono[i] = acc / (float)ch;
                }
            } else if (!silent && bits == 16) {
                auto* s = reinterpret_cast<int16_t*>(data);
                for (UINT32 i = 0; i < frames; ++i) {
                    float acc = 0.f;
                    for (int c = 0; c < ch; ++c) acc += s[i * ch + c] / 32768.f;
                    mono[i] = acc / (float)ch;
                }
            }
            cap->ReleaseBuffer(frames);
            if (!mono.empty()) pushSpectrum(mono.data(), (int)mono.size(), rate);
            cap->GetNextPacketSize(&packet);
        }
    }
    client->Stop();
    cap->Release();
    CoTaskMemFree(mix);
    client->Release();
    dev->Release();
    en->Release();
    if (uninit) CoUninitialize();
}

float sliderValue(const RECT& rc, int y, float lo, float hi) {
    int h = std::max(1, (int)(rc.bottom - rc.top));
    float t = 1.f - clampf((float)(y - rc.top) / (float)h, 0.f, 1.f);
    return lo + t * (hi - lo);
}

void layout(int w, int h) {
    g.specRc = {16, 16, w - 16, std::max(180, h / 2 - 20)};
    int y = g.specRc.bottom + 16;
    g.eqRc = {16, y, 140, y + 36};
    g.muteRc = {150, y, 250, y + 36};
    int sliderTop = y + 86;
    int sliderBot = h - 78;
    if (sliderBot < sliderTop + 80) sliderBot = sliderTop + 80;
    int gap = 8;
    int usable = w - 32 - 150;
    int sw = std::max(28, (usable - gap * (kBands - 1)) / kBands);
    for (int i = 0; i < kBands; ++i) {
        int x = 16 + i * (sw + gap);
        g.sliders[i] = {x, sliderTop, x + sw, sliderBot};
    }
    int tx = g.sliders[kBands - 1].right + 16;
    g.preRc = {tx, sliderTop, tx + 56, sliderBot};
    g.masterRc = {tx + 68, sliderTop, tx + 124, sliderBot};
    int px = 16;
    int py = h - 48;
    for (int i = 0; i < 12; ++i) {
        int bw = 96;
        if (px + bw > w - 16) {
            px = 16;
            py += 34;
        }
        g.presetRc[i] = {px, py, px + bw, py + 28};
        px += bw + 6;
    }
}

bool hit(const RECT& r, POINT p) {
    return p.x >= r.left && p.x < r.right && p.y >= r.top && p.y < r.bottom;
}

void setDragValue(int y) {
    if (g.drag >= 0 && g.drag < kBands) {
        g.gains[g.drag] = clampf(sliderValue(g.sliders[g.drag], y, kGainMin, kGainMax), kGainMin, kGainMax);
        g.preset = L"Custom";
    } else if (g.drag == 10) {
        g.preamp = clampf(sliderValue(g.preRc, y, kTrimMin, kTrimMax), kTrimMin, kTrimMax);
    } else if (g.drag == 11) {
        g.master = clampf(sliderValue(g.masterRc, y, kTrimMin, kTrimMax), kTrimMin, kTrimMax);
    }
    if (g.eqOn) applyApo();
}

void paintSlider(HDC dc, const RECT& rc, float value, float lo, float hi, const wchar_t* label) {
    HBRUSH bg = CreateSolidBrush(RGB(232, 236, 242));
    FillRect(dc, &rc, bg);
    DeleteObject(bg);
    float t = (value - lo) / (hi - lo);
    int y = rc.bottom - (int)(t * (rc.bottom - rc.top));
    RECT fill = rc;
    fill.top = y;
    HBRUSH fg = CreateSolidBrush(RGB(0, 102, 204));
    FillRect(dc, &fill, fg);
    DeleteObject(fg);
    FrameRect(dc, &rc, (HBRUSH)GetStockObject(GRAY_BRUSH));
    RECT lab = {rc.left - 4, rc.bottom + 2, rc.right + 8, rc.bottom + 20};
    DrawTextW(dc, label, -1, &lab, DT_CENTER | DT_SINGLELINE);
    wchar_t num[16];
    swprintf(num, 16, L"%+.0f", value);
    RECT val = {rc.left - 6, rc.top - 18, rc.right + 10, rc.top};
    DrawTextW(dc, num, -1, &val, DT_CENTER | DT_SINGLELINE);
}

void paintButton(HDC dc, const RECT& rc, const wchar_t* text, bool hot) {
    HBRUSH br = CreateSolidBrush(hot ? RGB(0, 102, 204) : RGB(255, 255, 255));
    FillRect(dc, &rc, br);
    DeleteObject(br);
    FrameRect(dc, &rc, (HBRUSH)GetStockObject(GRAY_BRUSH));
    SetBkMode(dc, TRANSPARENT);
    SetTextColor(dc, hot ? RGB(255, 255, 255) : RGB(17, 24, 39));
    RECT t = rc;
    DrawTextW(dc, text, -1, &t, DT_CENTER | DT_VCENTER | DT_SINGLELINE);
    SetTextColor(dc, RGB(17, 24, 39));
}

void paint(HWND hwnd) {
    PAINTSTRUCT ps;
    HDC dc = BeginPaint(hwnd, &ps);
    RECT cr;
    GetClientRect(hwnd, &cr);
    HDC mem = CreateCompatibleDC(dc);
    HBITMAP bmp = CreateCompatibleBitmap(dc, cr.right, cr.bottom);
    HGDIOBJ old = SelectObject(mem, bmp);
    HBRUSH page = CreateSolidBrush(RGB(245, 247, 251));
    FillRect(mem, &cr, page);
    DeleteObject(page);
    SetBkMode(mem, TRANSPARENT);

    float bars[kBars];
    {
        std::lock_guard<std::mutex> lock(g.specMx);
        memcpy(bars, g.bars, sizeof bars);
    }
    HBRUSH specBg = CreateSolidBrush(RGB(18, 28, 44));
    FillRect(mem, &g.specRc, specBg);
    DeleteObject(specBg);
    int bw = std::max(2, (g.specRc.right - g.specRc.left - 8) / kBars);
    int base = g.specRc.bottom - 8;
    int tall = g.specRc.bottom - g.specRc.top - 28;
    for (int i = 0; i < kBars; ++i) {
        int h = (int)(bars[i] * tall);
        RECT b{g.specRc.left + 6 + i * bw, base - h, g.specRc.left + 6 + i * bw + bw - 2, base};
        int red = 40 + (int)(180 * bars[i]);
        HBRUSH br = CreateSolidBrush(RGB(red, 90, 220));
        FillRect(mem, &b, br);
        DeleteObject(br);
    }
    SetTextColor(mem, RGB(180, 196, 214));
    TextOutW(mem, g.specRc.left + 8, g.specRc.top + 6, L"20 Hz", 5);
    TextOutW(mem, g.specRc.right - 52, g.specRc.top + 6, L"10 kHz", 6);
    SetTextColor(mem, RGB(17, 24, 39));

    paintButton(mem, g.eqRc, g.eqOn ? L"EQ ON" : L"EQ OFF", g.eqOn);
    paintButton(mem, g.muteRc, g.muted ? L"Muted" : L"Mute", g.muted);
    for (int i = 0; i < kBands; ++i) paintSlider(mem, g.sliders[i], g.gains[i], kGainMin, kGainMax, kLabels[i]);
    paintSlider(mem, g.preRc, g.preamp, kTrimMin, kTrimMax, L"Pre");
    paintSlider(mem, g.masterRc, g.master, kTrimMin, kTrimMax, L"Master");
    for (int i = 0; i < 12; ++i) paintButton(mem, g.presetRc[i], kPresets[i].name, g.preset == kPresets[i].name);

    TextOutW(mem, 16, g.eqRc.bottom + 8, g.status.c_str(), (int)g.status.size());
    BitBlt(dc, 0, 0, cr.right, cr.bottom, mem, 0, 0, SRCCOPY);
    SelectObject(mem, old);
    DeleteObject(bmp);
    DeleteDC(mem);
    EndPaint(hwnd, &ps);
}

LRESULT CALLBACK wndProc(HWND hwnd, UINT msg, WPARAM wp, LPARAM lp) {
    switch (msg) {
    case WM_CREATE: {
        RECT cr;
        GetClientRect(hwnd, &cr);
        layout(cr.right, cr.bottom);
        g.cap = std::thread(captureLoop);
        SetTimer(hwnd, 1, 33, nullptr);
        return 0;
    }
    case WM_SIZE:
        layout(LOWORD(lp), HIWORD(lp));
        InvalidateRect(hwnd, nullptr, FALSE);
        return 0;
    case WM_TIMER:
        InvalidateRect(hwnd, nullptr, FALSE);
        return 0;
    case WM_LBUTTONDOWN: {
        POINT p{LOWORD(lp), HIWORD(lp)};
        if (hit(g.eqRc, p)) {
            g.eqOn = !g.eqOn;
            if (g.eqOn && apoConfigDir().empty()) g.eqOn = false;
            applyApo();
        } else if (hit(g.muteRc, p)) {
            g.muted = !g.muted;
            if (g.eqOn) applyApo();
            else g.status = g.muted ? L"Mute is armed. Press EQ ON to silence the system EQ." : L"Mute off.";
        } else {
            g.drag = -1;
            for (int i = 0; i < kBands; ++i)
                if (hit(g.sliders[i], p)) g.drag = i;
            if (hit(g.preRc, p)) g.drag = 10;
            if (hit(g.masterRc, p)) g.drag = 11;
            if (g.drag >= 0) {
                SetCapture(hwnd);
                setDragValue(p.y);
            }
            for (int i = 0; i < 12; ++i) {
                if (!hit(g.presetRc[i], p)) continue;
                memcpy(g.gains, kPresets[i].g, sizeof g.gains);
                g.preset = kPresets[i].name;
                if (g.eqOn) applyApo();
            }
        }
        InvalidateRect(hwnd, nullptr, FALSE);
        return 0;
    }
    case WM_MOUSEMOVE:
        if (g.drag >= 0) {
            setDragValue((short)HIWORD(lp));
            InvalidateRect(hwnd, nullptr, FALSE);
        }
        return 0;
    case WM_LBUTTONUP:
        if (g.drag >= 0) {
            g.drag = -1;
            ReleaseCapture();
        }
        return 0;
    case WM_PAINT:
        paint(hwnd);
        return 0;
    case WM_DESTROY:
        g.run = false;
        if (g.cap.joinable()) g.cap.join();
        PostQuitMessage(0);
        return 0;
    default:
        return DefWindowProcW(hwnd, msg, wp, lp);
    }
}

}  // namespace

int WINAPI WinMain(HINSTANCE inst, HINSTANCE, LPSTR, int show) {
    WNDCLASSW wc = {};
    wc.lpfnWndProc = wndProc;
    wc.hInstance = inst;
    wc.lpszClassName = L"SpectraEQCpp";
    wc.hCursor = LoadCursor(nullptr, IDC_ARROW);
    wc.hbrBackground = (HBRUSH)(COLOR_WINDOW + 1);
    RegisterClassW(&wc);
    HWND hwnd = CreateWindowW(L"SpectraEQCpp", L"Spectra EQ  —  C++", WS_OVERLAPPEDWINDOW, CW_USEDEFAULT,
                              CW_USEDEFAULT, 1100, 720, nullptr, nullptr, inst, nullptr);
    g.hwnd = hwnd;
    ShowWindow(hwnd, show);
    MSG msg;
    while (GetMessageW(&msg, nullptr, 0, 0)) {
        TranslateMessage(&msg);
        DispatchMessageW(&msg);
    }
    return 0;
}
