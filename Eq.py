#!/usr/bin/env python3
"""
SPECTRA EQ — Live 20Hz→10kHz spectrum visualizer + 10-band graphic equalizer.
Single-file app: Eq.py

Windows:
  build_exe.bat          → standalone SpectraEQ.exe
  build_exe.bat run      → run Eq.py with Python (no EXE)
"""

from __future__ import annotations

import json
import math
import multiprocessing
import os
import sys
import threading
import time
import inspect
import traceback
import webbrowser
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

# PyInstaller onefile: unpack dir is sys._MEIPASS
if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
    os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

# Reduce pygame mixer noise; real I/O is via sounddevice
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

import numpy as np

try:
    import pygame
    from pygame import gfxdraw
except ImportError as e:
    print("Missing pygame. Install: pip install -r requirements.txt")
    print(e)
    sys.exit(1)

try:
    import sounddevice as sd
except ImportError:
    sd = None  # type: ignore

APP_VERSION = "2.6.18-drag"
VIZ_STYLES = ("Bars", "Wave", "Mirror", "Rings", "Needles")


# Native Windows WASAPI loopback (Microsoft AUDCLNT_STREAMFLAGS_LOOPBACK)
# https://learn.microsoft.com/en-us/windows/win32/coreaudio/loopback-recording
# Prefer soundcard; fall back to pure ctypes (no cffi) so frozen EXE still captures.
_SC_IMPORT_ERROR = ""
sc = None  # type: ignore
try:
    import soundcard as sc  # type: ignore
except Exception as _sc_exc:  # ImportError OR missing _cffi_backend in frozen EXE
    sc = None  # type: ignore
    _SC_IMPORT_ERROR = f"{type(_sc_exc).__name__}: {_sc_exc}"
    print(f"soundcard import failed: {_SC_IMPORT_ERROR}", file=sys.stderr)

_CTYPES_WASAPI = False
_CTYPES_WASAPI_ERR = ""

# Shared GUID structure for COM (must be one class, not recreated per call)
from ctypes import Structure as _CStructure, c_ubyte as _c_ubyte, c_ushort as _c_ushort, c_ulong as _c_ulong


class _GUID(_CStructure):
    _fields_ = [
        ("Data1", _c_ulong),
        ("Data2", _c_ushort),
        ("Data3", _c_ushort),
        ("Data4", _c_ubyte * 8),
    ]


def _guid(s: str) -> _GUID:
    """Parse '{xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx}' → GUID Structure."""
    s = s.strip("{}").replace("-", "")
    b = bytes.fromhex(s)
    g = _GUID()
    g.Data1 = int.from_bytes(b[0:4], "little")
    g.Data2 = int.from_bytes(b[4:6], "little")
    g.Data3 = int.from_bytes(b[6:8], "little")
    for i in range(8):
        g.Data4[i] = b[8 + i]
    return g


class _ComObj:
    """Minimal IUnknown vtable caller (Windows COM via ctypes)."""

    def __init__(self, ptr) -> None:
        from ctypes import POINTER, c_void_p, cast

        if not ptr:
            raise RuntimeError("null COM pointer")
        self._ptr = c_void_p(int(ptr) if not isinstance(ptr, c_void_p) else ptr.value or 0)
        self._vtbl = cast(cast(self._ptr, POINTER(c_void_p))[0], POINTER(c_void_p))

    def _fn(self, idx: int, restype, *argtypes):
        from ctypes import WINFUNCTYPE, c_void_p

        proto = WINFUNCTYPE(restype, c_void_p, *argtypes)
        return proto(self._vtbl[idx])

    def release(self) -> None:
        from ctypes import c_ulong

        try:
            self._fn(2, c_ulong)(self._ptr)
        except Exception:
            pass


def _com_init_thread() -> bool:
    """Initialize COM on the *current* thread. Returns True if we should CoUninitialize later."""
    if sys.platform not in ("win32", "cygwin"):
        return False
    try:
        from ctypes import windll

        ole32 = windll.ole32
        # Prefer STA for WASAPI (matches most UI apps); fall back to MTA.
        # COINIT_APARTMENTTHREADED=0x2, COINIT_MULTITHREADED=0x0
        for mode in (0x2, 0x0):
            hr = int(ole32.CoInitializeEx(None, mode)) & 0xFFFFFFFF
            # S_OK=0, S_FALSE=1 (already init this mode)
            if hr in (0, 1):
                return hr == 0
            # RPC_E_CHANGED_MODE — already initialized differently; COM is usable, don't uninit
            if hr == 0x80010106:
                return False
        return False
    except Exception:
        return False


def _com_create_mmdev_enumerator():
    """
    CoCreateInstance(MMDeviceEnumerator).
    MUST set argtypes on 64-bit Windows — otherwise pointers truncate and
    Windows returns REGDB_E_CLASSNOTREG (0x80040154) for a valid CLSID.
    Returns (punk_c_void_p or None, error_str).
    """
    from ctypes import POINTER, byref, c_long, c_ulong, c_void_p, windll

    CLSCTX_INPROC_SERVER = 0x1
    CLSCTX_LOCAL_SERVER = 0x4
    CLSCTX_ALL = 0x17
    CLSID = _guid("{BCDE0395-E52F-467C-8E3D-C4579291692E}")
    IID = _guid("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
    ole32 = windll.ole32
    try:
        ole32.CoCreateInstance.argtypes = [
            POINTER(_GUID),
            c_void_p,
            c_ulong,
            POINTER(_GUID),
            POINTER(c_void_p),
        ]
        ole32.CoCreateInstance.restype = c_long
    except Exception:
        pass
    last = ""
    for ctx in (
        CLSCTX_INPROC_SERVER,
        CLSCTX_ALL,
        CLSCTX_INPROC_SERVER | CLSCTX_LOCAL_SERVER,
    ):
        punk = c_void_p()
        try:
            hr = int(ole32.CoCreateInstance(byref(CLSID), None, ctx, byref(IID), byref(punk)))
        except Exception as exc:
            last = str(exc)
            continue
        hr_u = hr & 0xFFFFFFFF
        if hr_u == 0 and punk.value:
            return punk, ""
        last = f"CoCreateInstance: 0x{hr_u:08X}"
    return None, last or "CoCreateInstance failed"


def _com_uninit_thread(do_uninit: bool) -> None:
    if not do_uninit:
        return
    try:
        from ctypes import windll

        windll.ole32.CoUninitialize()
    except Exception:
        pass


def _ctypes_wasapi_init() -> Tuple[bool, str]:
    """Probe that ole32 / WASAPI COM is usable on this Windows box."""
    if sys.platform not in ("win32", "cygwin"):
        return False, "Windows only"
    owned = False
    try:
        owned = _com_init_thread()
        punk, err = _com_create_mmdev_enumerator()
        if punk is None:
            return False, err or "CoCreateInstance MMDeviceEnumerator failed"
        _ComObj(punk).release()
        return True, ""
    except Exception as exc:
        return False, str(exc)
    finally:
        _com_uninit_thread(owned)


if sys.platform in ("win32", "cygwin"):
    # Always enable ctypes path on Windows — probe at use-time, not import-time.
    # (Early CoCreateInstance can fail before COM is ready; soundcard often missing in EXE.)
    _ok, _err = _ctypes_wasapi_init()
    _CTYPES_WASAPI = True  # try anyway; record/list will report real errors
    _CTYPES_WASAPI_ERR = "" if _ok else (_err or "")
    if _ok:
        print("WASAPI ctypes: ready (no soundcard required)")
    else:
        print(f"WASAPI ctypes: probe soft-fail ({_CTYPES_WASAPI_ERR}) — will retry at capture", file=sys.stderr)

WASAPI_NATIVE = bool(sys.platform in ("win32", "cygwin"))
_WASAPI_INIT_ERROR = (
    ""
    if WASAPI_NATIVE
    else "WASAPI loopback is Windows-only"
)


def _ctypes_list_render_endpoints() -> Tuple[List["WasapiEndpoint"], str]:
    """Enumerate active render endpoints via IMMDeviceEnumerator (ctypes)."""
    from ctypes import (
        POINTER,
        Structure,
        byref,
        c_int,
        c_uint,
        c_ulong,
        c_ushort,
        c_void_p,
        c_wchar_p,
        cast,
        create_unicode_buffer,
        sizeof,
        windll,
    )
    from ctypes import wintypes

    if not _CTYPES_WASAPI and sys.platform not in ("win32", "cygwin"):
        return [], _CTYPES_WASAPI_ERR or "ctypes WASAPI unavailable"

    ole32 = windll.ole32
    ole32.CoTaskMemFree.argtypes = (c_void_p,)
    ole32.CoTaskMemFree.restype = None

    eRender, eConsole = 0, 0
    DEVICE_STATE_ACTIVE = 0x1
    STGM_READ = 0
    VT_LPWSTR = 31

    class PROPERTYKEY(Structure):
        _fields_ = [("fmtid", _GUID), ("pid", c_ulong)]

    class PROPVARIANT(Structure):
        _fields_ = [
            ("vt", c_ushort),
            ("wReserved1", c_ushort),
            ("wReserved2", c_ushort),
            ("wReserved3", c_ushort),
            ("data", c_void_p),
        ]

    out: List[WasapiEndpoint] = []
    err = ""
    enumerator = None
    collection = None
    owned_com = _com_init_thread()
    try:
        punk, cerr = _com_create_mmdev_enumerator()
        if punk is None:
            return [], cerr or "CoCreateInstance failed"
        enumerator = _ComObj(punk)

        # EnumAudioEndpoints(eRender, DEVICE_STATE_ACTIVE, &collection)
        coll_ptr = c_void_p()
        hr = enumerator._fn(3, c_ulong, c_int, c_ulong, POINTER(c_void_p))(
            enumerator._ptr, eRender, DEVICE_STATE_ACTIVE, byref(coll_ptr)
        )
        if hr != 0 or not coll_ptr.value:
            return [], f"EnumAudioEndpoints failed: 0x{hr & 0xFFFFFFFF:08X}"
        collection = _ComObj(coll_ptr)

        count = c_uint(0)
        hr = collection._fn(3, c_ulong, POINTER(c_uint))(collection._ptr, byref(count))
        if hr != 0:
            return [], f"GetCount failed: 0x{hr & 0xFFFFFFFF:08X}"

        # Default endpoint id
        default_id = ""
        try:
            def_ptr = c_void_p()
            hr = enumerator._fn(4, c_ulong, c_int, c_int, POINTER(c_void_p))(
                enumerator._ptr, eRender, eConsole, byref(def_ptr)
            )
            if hr == 0 and def_ptr.value:
                def_dev = _ComObj(def_ptr)
                id_ptr = c_wchar_p()
                if def_dev._fn(5, c_ulong, POINTER(c_wchar_p))(def_dev._ptr, byref(id_ptr)) == 0 and id_ptr:
                    default_id = id_ptr.value or ""
                    ole32.CoTaskMemFree(id_ptr)
                def_dev.release()
        except Exception:
            default_id = ""

        PKEY_FriendlyName = PROPERTYKEY()
        PKEY_FriendlyName.fmtid = _guid("{A45C254E-DF1C-4EFD-8020-67D146A850E0}")
        PKEY_FriendlyName.pid = 14

        for i in range(int(count.value)):
            dev_ptr = c_void_p()
            hr = collection._fn(4, c_ulong, c_uint, POINTER(c_void_p))(
                collection._ptr, i, byref(dev_ptr)
            )
            if hr != 0 or not dev_ptr.value:
                continue
            device = _ComObj(dev_ptr)
            try:
                id_ptr = c_wchar_p()
                hr = device._fn(5, c_ulong, POINTER(c_wchar_p))(device._ptr, byref(id_ptr))
                if hr != 0 or not id_ptr:
                    continue
                device_id = id_ptr.value or ""
                ole32.CoTaskMemFree(id_ptr)
                if not device_id:
                    continue

                name = f"Speakers {i + 1}"
                store_ptr = c_void_p()
                hr = device._fn(4, c_ulong, c_ulong, POINTER(c_void_p))(
                    device._ptr, STGM_READ, byref(store_ptr)
                )
                if hr == 0 and store_ptr.value:
                    store = _ComObj(store_ptr)
                    try:
                        pv = PROPVARIANT()
                        hr = store._fn(5, c_ulong, POINTER(PROPERTYKEY), POINTER(PROPVARIANT))(
                            store._ptr, byref(PKEY_FriendlyName), byref(pv)
                        )
                        if hr == 0 and pv.vt == VT_LPWSTR and pv.data:
                            name = cast(pv.data, c_wchar_p).value or name
                        try:
                            windll.oleaut32.PropVariantClear(byref(pv))
                        except Exception:
                            try:
                                windll.ole32.PropVariantClear(byref(pv))
                            except Exception:
                                pass
                    finally:
                        store.release()

                out.append(
                    WasapiEndpoint(
                        device_id=device_id,
                        name=name,
                        is_default=bool(default_id and device_id == default_id),
                        mic=None,
                        channels=2,
                        rate=48000,
                    )
                )
            finally:
                device.release()

        out.sort(key=lambda e: (0 if e.is_default else 1, e.name.lower()))
        if not out:
            err = "No active render endpoints"
    except Exception as exc:
        err = str(exc)
    finally:
        if collection is not None:
            collection.release()
        if enumerator is not None:
            enumerator.release()
        _com_uninit_thread(owned_com)
    return out, err


def _ctypes_loopback_record(
    device_id: Optional[str],
    blocksize: int,
    on_audio: Callable[[Any], None],
    alive_flag: Callable[[], bool],
    error_sink: Callable[[str], None],
) -> None:
    """
    Capture render endpoint with AUDCLNT_STREAMFLAGS_LOOPBACK (shared mode).
    Emits float32 (N, 2) buffers via on_audio until alive_flag() is False.
    """
    from ctypes import (
        POINTER,
        Structure,
        byref,
        c_byte,
        c_float,
        c_int,
        c_longlong,
        c_uint,
        c_ulong,
        c_ulonglong,
        c_ushort,
        c_void_p,
        c_wchar_p,
        cast,
        sizeof,
        windll,
    )

    ole32 = windll.ole32
    ole32.CoTaskMemFree.argtypes = (c_void_p,)
    ole32.CoTaskMemFree.restype = None

    AUDCLNT_SHAREMODE_SHARED = 0
    AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000
    AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM = 0x80000000
    AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY = 0x08000000
    CLSCTX_ALL = 0x17
    eRender, eConsole = 0, 0
    REFTIMES_PER_SEC = 10000000

    class WAVEFORMATEX(Structure):
        _fields_ = [
            ("wFormatTag", c_ushort),
            ("nChannels", c_ushort),
            ("nSamplesPerSec", c_ulong),
            ("nAvgBytesPerSec", c_ulong),
            ("nBlockAlign", c_ushort),
            ("wBitsPerSample", c_ushort),
            ("cbSize", c_ushort),
        ]

    enumerator = None
    device = None
    client = None
    capture = None
    pwfx = None
    owned_com = _com_init_thread()
    try:
        IID_IAudioClient = _guid("{1CB9AD4C-DBFA-4C32-B178-C2F568A703B2}")
        IID_IAudioCaptureClient = _guid("{C8ADBD64-E71E-48A0-A4DE-185C395CD317}")

        punk, cerr = _com_create_mmdev_enumerator()
        if punk is None:
            raise RuntimeError(cerr or "CoCreateInstance failed")
        enumerator = _ComObj(punk)

        dev_ptr = c_void_p()
        if device_id:
            hr = enumerator._fn(5, c_ulong, c_wchar_p, POINTER(c_void_p))(
                enumerator._ptr, device_id, byref(dev_ptr)
            )
        else:
            # Empty / None → default render endpoint (speakers)
            hr = enumerator._fn(4, c_ulong, c_int, c_int, POINTER(c_void_p))(
                enumerator._ptr, eRender, eConsole, byref(dev_ptr)
            )
        if hr != 0 or not dev_ptr.value:
            raise RuntimeError(f"Get device failed: 0x{hr & 0xFFFFFFFF:08X}")
        device = _ComObj(dev_ptr)

        # Activate IAudioClient
        client_ptr = c_void_p()
        hr = device._fn(3, c_ulong, POINTER(type(IID_IAudioClient)), c_ulong, c_void_p, POINTER(c_void_p))(
            device._ptr, byref(IID_IAudioClient), CLSCTX_ALL, None, byref(client_ptr)
        )
        if hr != 0 or not client_ptr.value:
            raise RuntimeError(f"Activate IAudioClient: 0x{hr & 0xFFFFFFFF:08X}")
        client = _ComObj(client_ptr)

        # GetMixFormat
        pwfx = POINTER(WAVEFORMATEX)()
        hr = client._fn(8, c_ulong, POINTER(POINTER(WAVEFORMATEX)))(client._ptr, byref(pwfx))
        if hr != 0 or not pwfx:
            raise RuntimeError(f"GetMixFormat: 0x{hr & 0xFFFFFFFF:08X}")

        n_ch = int(pwfx.contents.nChannels or 2)
        rate = int(pwfx.contents.nSamplesPerSec or 48000)
        bits = int(pwfx.contents.wBitsPerSample or 32)
        align = int(pwfx.contents.nBlockAlign or (n_ch * bits // 8))
        tag = int(pwfx.contents.wFormatTag or 0)

        flags = (
            AUDCLNT_STREAMFLAGS_LOOPBACK
            | AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM
            | AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY
        )
        hns = c_longlong(REFTIMES_PER_SEC // 10)  # 100 ms buffer
        hr = client._fn(
            3,
            c_ulong,
            c_int,
            c_ulong,
            c_longlong,
            c_longlong,
            POINTER(WAVEFORMATEX),
            c_void_p,
        )(client._ptr, AUDCLNT_SHAREMODE_SHARED, flags, hns, c_longlong(0), pwfx, None)
        if hr != 0:
            # Retry without autoconvert flags
            hr = client._fn(
                3,
                c_ulong,
                c_int,
                c_ulong,
                c_longlong,
                c_longlong,
                POINTER(WAVEFORMATEX),
                c_void_p,
            )(
                client._ptr,
                AUDCLNT_SHAREMODE_SHARED,
                AUDCLNT_STREAMFLAGS_LOOPBACK,
                hns,
                c_longlong(0),
                pwfx,
                None,
            )
        if hr != 0:
            raise RuntimeError(f"IAudioClient.Initialize LOOPBACK: 0x{hr & 0xFFFFFFFF:08X}")

        cap_ptr = c_void_p()
        hr = client._fn(14, c_ulong, POINTER(type(IID_IAudioCaptureClient)), POINTER(c_void_p))(
            client._ptr, byref(IID_IAudioCaptureClient), byref(cap_ptr)
        )
        if hr != 0 or not cap_ptr.value:
            raise RuntimeError(f"GetService IAudioCaptureClient: 0x{hr & 0xFFFFFFFF:08X}")
        capture = _ComObj(cap_ptr)

        hr = client._fn(10, c_ulong)(client._ptr)  # Start
        if hr != 0:
            raise RuntimeError(f"IAudioClient.Start: 0x{hr & 0xFFFFFFFF:08X}")

        is_float = tag in (0x0003, 0xFFFE) and bits == 32
        # WAVE_FORMAT_EXTENSIBLE often carries float mix format on modern Windows
        if tag == 0xFFFE and bits == 32:
            is_float = True

        while alive_flag():
            pkt = c_uint(0)
            hr = capture._fn(5, c_ulong, POINTER(c_uint))(capture._ptr, byref(pkt))
            if hr != 0:
                error_sink(f"GetNextPacketSize: 0x{hr & 0xFFFFFFFF:08X}")
                time.sleep(0.02)
                continue
            if pkt.value == 0:
                time.sleep(0.005)
                continue

            data_ptr = c_void_p()
            nframes = c_uint(0)
            flags_out = c_ulong(0)
            pos = c_ulonglong(0)
            qpc = c_ulonglong(0)
            hr = capture._fn(
                3,
                c_ulong,
                POINTER(c_void_p),
                POINTER(c_uint),
                POINTER(c_ulong),
                POINTER(c_ulonglong),
                POINTER(c_ulonglong),
            )(
                capture._ptr,
                byref(data_ptr),
                byref(nframes),
                byref(flags_out),
                byref(pos),
                byref(qpc),
            )
            if hr != 0:
                error_sink(f"GetBuffer: 0x{hr & 0xFFFFFFFF:08X}")
                time.sleep(0.02)
                continue
            try:
                nf = int(nframes.value)
                if nf <= 0 or not data_ptr.value:
                    continue
                silent = bool(flags_out.value & 0x2)
                if silent:
                    arr = np.zeros((nf, 2), dtype=np.float32)
                else:
                    nbytes = nf * align
                    raw = (c_byte * nbytes).from_address(int(data_ptr.value))
                    buf = bytes(raw)
                    if is_float:
                        samp = np.frombuffer(buf, dtype=np.float32)
                    elif bits == 16:
                        samp = np.frombuffer(buf, dtype=np.int16).astype(np.float32) / 32768.0
                    elif bits == 32:
                        samp = np.frombuffer(buf, dtype=np.int32).astype(np.float32) / 2147483648.0
                    else:
                        samp = np.frombuffer(buf, dtype=np.int16).astype(np.float32) / 32768.0
                    if n_ch <= 1:
                        mono = samp.reshape(-1)
                        arr = np.column_stack([mono, mono]).astype(np.float32, copy=False)
                    else:
                        interleaved = samp.reshape(-1, n_ch)
                        arr = interleaved[:, :2].astype(np.float32, copy=False)
                # Chunk to ~blocksize for spectrum/EQ
                bs = max(256, int(blocksize))
                for off in range(0, arr.shape[0], bs):
                    if not alive_flag():
                        break
                    on_audio(arr[off : off + bs])
            finally:
                capture._fn(4, c_ulong, c_uint)(capture._ptr, nframes)

        try:
            client._fn(11, c_ulong)(client._ptr)  # Stop
        except Exception:
            pass
    except Exception as exc:
        error_sink(str(exc))
    finally:
        if capture is not None:
            capture.release()
        if client is not None:
            client.release()
        if device is not None:
            device.release()
        if enumerator is not None:
            enumerator.release()
        try:
            if pwfx:
                ole32.CoTaskMemFree(cast(pwfx, c_void_p))
        except Exception:
            pass
        _com_uninit_thread(owned_com)


@dataclass
class WasapiEndpoint:
    """A playback device capturable via WASAPI loopback (render + LOOPBACK flag)."""

    device_id: str
    name: str
    is_default: bool = False
    mic: object = None
    channels: int = 2
    rate: int = 48000


def list_render_endpoints() -> Tuple[List[WasapiEndpoint], str]:
    """List render endpoints for Microsoft WASAPI loopback recording."""
    if not WASAPI_NATIVE:
        return [], _WASAPI_INIT_ERROR or "not available"

    err = ""
    # Prefer soundcard when available (handles COM better on some PCs).
    # Fall back to pure ctypes when soundcard/cffi is missing or fails.
    order = ("soundcard", "ctypes") if sc is not None else ("ctypes",)

    for backend in order:
        if backend == "soundcard" and sc is not None:
            out: List[WasapiEndpoint] = []
            try:
                mics = list(sc.all_microphones(include_loopback=True))
                loopbacks = [m for m in mics if getattr(m, "isloopback", False)]
                if not loopbacks:
                    for sp in sc.all_speakers():
                        try:
                            m = sc.get_microphone(sp.name, include_loopback=True)
                            if m is not None:
                                loopbacks.append(m)
                        except Exception:
                            continue
                try:
                    default_name = str(getattr(sc.default_speaker(), "name", "") or "")
                except Exception:
                    default_name = ""
                seen = set()
                for m in loopbacks:
                    name = str(getattr(m, "name", "") or "").strip()
                    if not name:
                        continue
                    key = name.lower()
                    if key in seen:
                        continue
                    seen.add(key)
                    mid = str(getattr(m, "id", name) or name)
                    ch = 2
                    try:
                        c = int(getattr(m, "channels", 2) or 2)
                        ch = 2 if c >= 2 else max(1, c)
                    except Exception:
                        ch = 2
                    is_def = bool(
                        default_name
                        and (
                            name.lower() == default_name.lower()
                            or default_name.lower() in name.lower()
                            or name.lower() in default_name.lower()
                        )
                    )
                    out.append(
                        WasapiEndpoint(
                            device_id=mid,
                            name=name,
                            is_default=is_def,
                            mic=m,
                            channels=ch,
                            rate=48000,
                        )
                    )
                out.sort(key=lambda e: (0 if e.is_default else 1, e.name.lower()))
                if out:
                    print(f"WASAPI soundcard: {len(out)} endpoint(s)")
                    return out, ""
                err = "soundcard found no loopback endpoints"
            except Exception as exc:
                err = str(exc)
                print(f"WASAPI soundcard error: {err}")

        if backend == "ctypes":
            try:
                ct_out, ct_err = _ctypes_list_render_endpoints()
                if ct_out:
                    print(f"WASAPI ctypes: {len(ct_out)} render endpoint(s)")
                    return ct_out, ""
                err = ct_err or err or "ctypes found no endpoints"
            except Exception as exc:
                err = str(exc)
                print(f"WASAPI ctypes error: {err}")

    # Last resort: default render endpoint — only if ctypes COM actually works
    owned = _com_init_thread()
    try:
        punk, cerr = _com_create_mmdev_enumerator()
        if punk is not None:
            try:
                _ComObj(punk).release()
            except Exception:
                pass
            print(f"WASAPI: using default speakers fallback ({err})")
            return [
                WasapiEndpoint(
                    device_id="",
                    name="Default Speakers (WASAPI loopback)",
                    is_default=True,
                    mic=None,
                    channels=2,
                    rate=48000,
                )
            ], ""
        err = cerr or err
    except Exception as exc:
        err = str(exc) or err
    finally:
        _com_uninit_thread(owned)
    print(f"WASAPI: no endpoints ({err})")
    return [], err or "no WASAPI endpoints"


class WasapiLoopbackRecorder:
    """Continuously capture loopback audio from a render endpoint."""

    def __init__(
        self,
        mic: object = None,
        device_id: Optional[str] = None,
        blocksize: int = 2048,
        samplerate: int = 48000,
        channels: int = 2,
        on_audio: Optional[Callable[[np.ndarray], None]] = None,
        force_ctypes: bool = False,
    ) -> None:
        if not WASAPI_NATIVE:
            raise RuntimeError(_WASAPI_INIT_ERROR or "WASAPI not available")
        self.blocksize = int(blocksize)
        self.sample_rate = int(samplerate) or 48000
        self.channels = max(1, int(channels) or 2)
        self.on_audio = on_audio
        self._alive = False
        self._thread: Optional[threading.Thread] = None
        self._error = ""
        self._mic = None if force_ctypes else mic
        self._device_id = device_id
        self._use_ctypes = bool(force_ctypes)

        if not self._use_ctypes:
            if self._mic is None and device_id and sc is not None:
                try:
                    self._mic = sc.get_microphone(device_id, include_loopback=True)
                except Exception:
                    self._mic = None
            if self._mic is None and sc is not None and not device_id:
                try:
                    sp = sc.default_speaker()
                    self._mic = sc.get_microphone(sp.name, include_loopback=True)
                except Exception:
                    self._mic = None

        if self._mic is None:
            # Always use ctypes on Windows when no soundcard mic handle
            self._use_ctypes = True

    @property
    def error(self) -> str:
        return self._error

    def start(self) -> None:
        if self._alive:
            return
        self._alive = True
        self._thread = threading.Thread(target=self._run, daemon=True, name="wasapi-loopback")
        self._thread.start()

    def stop(self) -> None:
        self._alive = False
        thr = self._thread
        if thr is not None and thr.is_alive():
            thr.join(timeout=2.0)
        self._thread = None

    def _run(self) -> None:
        if self._use_ctypes or self._mic is None:
            def _alive() -> bool:
                return bool(self._alive)

            def _err(msg: str) -> None:
                self._error = msg

            def _on(buf) -> None:
                if self.on_audio is not None:
                    self.on_audio(np.asarray(buf, dtype=np.float32))

            _ctypes_loopback_record(
                self._device_id,
                self.blocksize,
                _on,
                _alive,
                _err,
            )
            return

        mic = self._mic
        rate = self.sample_rate
        ch = self.channels
        block = self.blocksize
        try:
            with mic.recorder(samplerate=rate, channels=ch, blocksize=block) as rec:
                while self._alive:
                    try:
                        data = rec.record(numframes=block)
                        if data is None:
                            continue
                        arr = np.asarray(data, dtype=np.float32)
                        if arr.ndim == 1:
                            arr = arr.reshape(-1, 1)
                        if arr.shape[1] == 1 and ch >= 2:
                            arr = np.repeat(arr, 2, axis=1)
                        elif arr.shape[1] > 2:
                            arr = arr[:, :2]
                        if self.on_audio is not None:
                            self.on_audio(arr)
                    except Exception as exc:
                        self._error = str(exc)
                        time.sleep(0.05)
        except Exception as exc:
            self._error = str(exc)
            if _CTYPES_WASAPI and self._alive:
                self._error = ""
                self._use_ctypes = True
                self._mic = None
                # Re-enter ctypes path once (no recursion stack)
                def _alive() -> bool:
                    return bool(self._alive)

                def _err(msg: str) -> None:
                    self._error = msg

                def _on(buf) -> None:
                    if self.on_audio is not None:
                        self.on_audio(np.asarray(buf, dtype=np.float32))

                _ctypes_loopback_record(
                    self._device_id,
                    self.blocksize,
                    _on,
                    _alive,
                    _err,
                )


def probe_loopback(
    mic: object = None, device_id: Optional[str] = None, timeout: float = 0.35
) -> Tuple[bool, str, int, int]:
    """Try opening a loopback briefly. Silence is OK — open success matters."""
    if not WASAPI_NATIVE:
        return False, _WASAPI_INIT_ERROR or "not available", 0, 0
    try:
        rec = WasapiLoopbackRecorder(mic=mic, device_id=device_id, blocksize=512)
        rate, ch = rec.sample_rate, rec.channels

        def _on(buf: np.ndarray) -> None:
            pass

        rec.on_audio = _on
        rec.start()
        t0 = time.time()
        while time.time() - t0 < timeout:
            if rec.error:
                rec.stop()
                return False, rec.error, rate, ch
            time.sleep(0.02)
        err = rec.error
        rec.stop()
        if err:
            return False, err, rate, ch
        return True, "", rate, ch
    except Exception as exc:
        return False, str(exc), 0, 0


# ─── Crash / error logging (never kill silently) ─────────────────────────────
def _log_dir() -> str:
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    d = os.path.join(base, "logs")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        d = os.path.expanduser("~") or base
    return d


CRASH_LOG = os.path.join(_log_dir(), "spectra_eq_crash.log")
_last_ui_error = ""


def _app_base_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def apply_window_icon() -> None:
    """Title-bar icon. The same art is embedded in SpectraEQ.exe for the desktop."""
    path = os.path.join(_app_base_dir(), "spectraeq.png")
    if not os.path.isfile(path):
        return
    try:
        icon = pygame.image.load(path)
        icon = pygame.transform.smoothscale(icon, (64, 64))
        pygame.display.set_icon(icon)
    except Exception as exc:
        log_exception("window_icon", exc)


def find_apo_installer() -> Optional[str]:
    """Equalizer APO setup exe sitting next to Spectra (not inside the one-file EXE)."""
    roots = [_app_base_dir()]
    me = getattr(sys, "_MEIPASS", None)
    if me:
        roots.append(str(me))
    seen = set()
    for root in roots:
        key = os.path.normcase(os.path.abspath(root))
        if key in seen or not os.path.isdir(root):
            continue
        seen.add(key)
        try:
            names = os.listdir(root)
        except OSError:
            continue
        for fn in names:
            low = fn.lower()
            if not low.endswith(".exe"):
                continue
            if "spectra" in low:
                continue
            if "equalizerapo" in low.replace(" ", "") or ("equalizer" in low and "apo" in low):
                return os.path.join(root, fn)
    return None


def launch_file(path: str) -> bool:
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        rc = int(ctypes.windll.shell32.ShellExecuteW(None, "open", path, None, os.path.dirname(path), 1))
        return rc > 32
    except Exception as exc:
        log_exception("launch_file", exc)
        return False


CONFIG_PATH = os.path.join(_app_base_dir(), "spectra_eq_config.json")


def preset_folder() -> str:
    folder = os.path.join(_app_base_dir(), "presets")
    try:
        os.makedirs(folder, exist_ok=True)
    except OSError:
        return _app_base_dir()
    return folder


def _eqpreset_dialog(save: bool, default_name: str) -> Optional[str]:
    """Windows save/open dialog. Files are plain text with a .eqpreset extension."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return None

    class OPENFILENAMEW(ctypes.Structure):
        _fields_ = [
            ("lStructSize", wintypes.DWORD),
            ("hwndOwner", wintypes.HWND),
            ("hInstance", wintypes.HINSTANCE),
            ("lpstrFilter", wintypes.LPCWSTR),
            ("lpstrCustomFilter", wintypes.LPWSTR),
            ("nMaxCustFilter", wintypes.DWORD),
            ("nFilterIndex", wintypes.DWORD),
            ("lpstrFile", wintypes.LPWSTR),
            ("nMaxFile", wintypes.DWORD),
            ("lpstrFileTitle", wintypes.LPWSTR),
            ("nMaxFileTitle", wintypes.DWORD),
            ("lpstrInitialDir", wintypes.LPCWSTR),
            ("lpstrTitle", wintypes.LPCWSTR),
            ("Flags", wintypes.DWORD),
            ("nFileOffset", wintypes.WORD),
            ("nFileExtension", wintypes.WORD),
            ("lpstrDefExt", wintypes.LPCWSTR),
            ("lCustData", wintypes.LPARAM),
            ("lpfnHook", ctypes.c_void_p),
            ("lpTemplateName", wintypes.LPCWSTR),
            ("pvReserved", ctypes.c_void_p),
            ("dwReserved", wintypes.DWORD),
            ("FlagsEx", wintypes.DWORD),
        ]

    if not default_name:
        start = ""
    elif default_name.lower().endswith(".eqpreset"):
        start = default_name
    else:
        start = default_name + ".eqpreset"
    buf = ctypes.create_unicode_buffer(start, 4096)
    title = ctypes.create_unicode_buffer("Save Spectra EQ preset" if save else "Open Spectra EQ preset")
    initial = ctypes.create_unicode_buffer(preset_folder())
    # Double-null terminated filter. The extension is .eqpreset.
    filt = "Spectra EQ preset (*.eqpreset)\0*.eqpreset\0Text files (*.txt)\0*.txt\0\0"
    ofn = OPENFILENAMEW()
    ofn.lStructSize = ctypes.sizeof(OPENFILENAMEW)
    ofn.lpstrFilter = filt
    ofn.nFilterIndex = 1
    ofn.lpstrFile = ctypes.cast(buf, wintypes.LPWSTR)
    ofn.nMaxFile = 4096
    ofn.lpstrInitialDir = ctypes.cast(initial, wintypes.LPCWSTR)
    ofn.lpstrTitle = ctypes.cast(title, wintypes.LPCWSTR)
    ofn.lpstrDefExt = "eqpreset"
    ofn.Flags = 0x00000002 | 0x00000800 | 0x00080000  # overwrite prompt, path must exist, explorer
    if not save:
        ofn.Flags |= 0x00001000  # file must exist
    comdlg = ctypes.WinDLL("comdlg32", use_last_error=True)
    fn = comdlg.GetSaveFileNameW if save else comdlg.GetOpenFileNameW
    fn.argtypes = [ctypes.POINTER(OPENFILENAMEW)]
    fn.restype = wintypes.BOOL
    if not fn(ctypes.byref(ofn)):
        return None
    path = buf.value
    if path and not path.lower().endswith(".eqpreset"):
        path += ".eqpreset"
    return path or None


def preset_to_text(name: str, gains: List[float], preamp: float, master: float) -> str:
    lines = ["# Spectra EQ preset", f"name={name}"]
    for hz, gain in zip(BAND_HZ, gains):
        lines.append(f"{int(hz)}={float(gain):.1f}")
    lines.append(f"preamp={float(preamp):.1f}")
    lines.append(f"master={float(master):.1f}")
    lines.append("")
    return "\n".join(lines)


def preset_from_text(text: str) -> Optional[Dict[str, Any]]:
    gains: Dict[int, float] = {}
    name = "Preset"
    preamp = 0.0
    master = 0.0
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip().lower()
        val = val.strip()
        if key == "name":
            name = val[:40] or name
            continue
        if key == "preamp":
            preamp = float(val)
            continue
        if key == "master":
            master = float(val)
            continue
        hz = int(float(key))
        if hz == 16000:
            hz = 10000
        gains[hz] = float(val)
    if any(hz not in gains for hz in BAND_HZ):
        return None
    return {
        "name": name,
        "gains": [gains[hz] for hz in BAND_HZ],
        "preamp": preamp,
        "master": master,
    }

_CABLE_HINTS = (
    "cable",
    "vb-audio",
    "voicemeeter input",
    "cable input",
    "voice meeter",
)
_SAFE_PLAY_HINTS = (
    "cable",
    "vb-audio",
    "voicemeeter",
    "headphone",
    "headset",
    "earphone",
    "earbud",
    "earbuds",
    "airpods",
    "buds",
    "bluetooth",
    "bt ",
    " usb dac",
    "dac ",
)
_BOGUS_PLAY_HINTS = (
    "sound mapper",
    "microsoft sound mapper",
    "primary sound driver",
    "primary sound",
)


def is_bogus_play_name(name: str) -> bool:
    """True for mapper/primary stubs that always feedback into Speakers."""
    n = safe_name(name).lower()
    return any(x in n for x in _BOGUS_PLAY_HINTS)


def wrap_text(font: "pygame.font.Font", text: str, max_width: int) -> List[str]:
    """Word-wrap text to fit max_width pixels. Never truncates with ellipsis."""
    text = (text or "").replace("\n", " ").strip()
    if not text:
        return [""]
    words = text.split()
    lines: List[str] = []
    cur = ""
    for w in words:
        trial = w if not cur else f"{cur} {w}"
        if font.size(trial)[0] <= max_width:
            cur = trial
            continue
        if cur:
            lines.append(cur)
        # hard-break overlong tokens
        if font.size(w)[0] <= max_width:
            cur = w
        else:
            chunk = ""
            for ch in w:
                t2 = chunk + ch
                if font.size(t2)[0] <= max_width:
                    chunk = t2
                else:
                    if chunk:
                        lines.append(chunk)
                    chunk = ch
            cur = chunk
    if cur:
        lines.append(cur)
    return lines or [text]


def log_exception(where: str, exc: Optional[BaseException] = None) -> str:
    """Append a crash/error report; return short message for UI."""
    global _last_ui_error
    try:
        tb = traceback.format_exc()
        if exc is not None and (not tb or tb.strip() == "NoneType: None"):
            tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        msg = f"[{datetime.now().isoformat(timespec='seconds')}] {where}\n{tb}\n"
        with open(CRASH_LOG, "a", encoding="utf-8", errors="replace") as f:
            f.write(msg)
        print(msg, file=sys.stderr)
        short = f"{where}: {exc or 'error'}"
        _last_ui_error = short[:120]
        return short
    except Exception:
        return where


def install_crash_handlers() -> None:
    def _hook(etype, value, tb):
        try:
            text = "".join(traceback.format_exception(etype, value, tb))
            with open(CRASH_LOG, "a", encoding="utf-8", errors="replace") as f:
                f.write(f"[{datetime.now().isoformat(timespec='seconds')}] UNCAUGHT\n{text}\n")
            print(text, file=sys.stderr)
        except Exception:
            pass

    sys.excepthook = _hook
    if hasattr(threading, "excepthook"):
        def _thread_hook(args):  # type: ignore[no-untyped-def]
            log_exception(f"thread:{getattr(args, 'thread', None)}", args.exc_value)

        threading.excepthook = _thread_hook  # type: ignore[assignment]


def safe_name(raw: object, fallback: str = "Device") -> str:
    """PortAudio names can be weird bytes / nulls — keep UI stable."""
    try:
        if raw is None:
            return fallback
        if isinstance(raw, bytes):
            s = raw.decode("utf-8", errors="replace")
        else:
            s = str(raw)
        s = "".join(ch if 32 <= ord(ch) < 127 else "?" for ch in s)
        s = " ".join(s.split())
        return s[:80] if s else fallback
    except Exception:
        return fallback

# ─── Constants ───────────────────────────────────────────────────────────────
SAMPLE_RATE = 48000
BLOCK = 1024
FFT_SIZE = 2048
NYQUIST = SAMPLE_RATE / 2.0
F_MIN, F_MAX = 20.0, 10000.0

# 10 bands, log-spaced from 20 Hz through 10 kHz
BAND_HZ = [20, 50, 100, 200, 500, 1000, 2000, 4000, 8000, 10000]
BAND_LABELS = ["20", "50", "100", "200", "500", "1k", "2k", "4k", "8k", "10k"]

GAIN_MIN, GAIN_MAX = -12.0, 12.0
HISTORY = 48  # waterfall depth for "3D" mesh

PRESETS = {
    "Flat":       [0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
    "Bass Boost": [8, 6, 4, 2, 0, 0, 0, 0, 0, 0],
    "Treble":     [0, 0, 0, 0, 0, 1, 3, 5, 7, 8],
    "V-Shape":    [6, 4, 1, -2, -4, -4, -2, 1, 4, 6],
    "Vocal":      [-3, -2, 0, 2, 5, 6, 4, 1, -1, -2],
    "Electronic": [5, 4, 1, -1, 0, 2, 4, 5, 4, 3],
    "Rock":       [5, 3, 1, 0, -1, 1, 3, 4, 3, 2],
    "Classical":  [3, 2, 0, 0, 0, 0, 1, 2, 3, 4],
    "Podcast":    [-4, -2, 1, 4, 5, 4, 2, 0, -2, -4],
    "Cinema":     [6, 4, 2, 0, -1, 0, 2, 3, 2, 1],
    "Night":      [-2, 0, 2, 3, 2, 0, -2, -4, -6, -8],
    "Loudness":   [7, 5, 2, 0, -1, 0, 2, 4, 5, 4],
}

COMMON_ADJUST = {
    "+Bass":      lambda g: [min(GAIN_MAX, x + d) for x, d in zip(g, [3, 2, 1, 0, 0, 0, 0, 0, 0, 0])],
    "-Bass":      lambda g: [max(GAIN_MIN, x + d) for x, d in zip(g, [-3, -2, -1, 0, 0, 0, 0, 0, 0, 0])],
    "+Treble":    lambda g: [min(GAIN_MAX, x + d) for x, d in zip(g, [0, 0, 0, 0, 0, 0, 1, 2, 3, 3])],
    "-Treble":    lambda g: [max(GAIN_MIN, x + d) for x, d in zip(g, [0, 0, 0, 0, 0, 0, -1, -2, -3, -3])],
    "Warm":       lambda g: [min(GAIN_MAX, max(GAIN_MIN, x + d)) for x, d in zip(g, [2, 2, 1, 0, -1, -1, 0, 1, 0, -1])],
    "Bright":     lambda g: [min(GAIN_MAX, max(GAIN_MIN, x + d)) for x, d in zip(g, [-1, 0, 0, 0, 1, 2, 3, 3, 2, 1])],
    "Punch":      lambda g: [min(GAIN_MAX, max(GAIN_MIN, x + d)) for x, d in zip(g, [1, 3, 2, 0, -1, 0, 1, 0, 0, 0])],
    "Clear Mid":  lambda g: [min(GAIN_MAX, max(GAIN_MIN, x + d)) for x, d in zip(g, [0, 0, -2, -3, -2, 0, 1, 1, 0, 0])],
}

TIPS = {
    "EQ ON": "The equalizer is on. Click to turn it off and put the sound back to flat.",
    "EQ OFF": "Click to turn the equalizer on. The knobs then change every sound you play.",
    "Refresh": "Look again for speakers, headphones, and the system EQ.",
    "Flat": "Reset every band to 0 dB.",
    "Bass Boost": "Lift the low end.",
    "Treble": "Lift the high end.",
    "V-Shape": "More bass and treble, quieter mids.",
    "Vocal": "Bring voices forward.",
    "Electronic": "Bass and sparkle for electronic music.",
    "Rock": "A punchy rock curve.",
    "Classical": "A gentle wide curve.",
    "Podcast": "Clearer speech, less rumble and hiss.",
    "Cinema": "Bigger low end, like a movie.",
    "Night": "Quieter harsh highs for late listening.",
    "Loudness": "More bass and treble at lower volume.",
    "+Bass": "Add a little bass.",
    "-Bass": "Take away a little bass.",
    "+Treble": "Add a little treble.",
    "-Treble": "Take away a little treble.",
    "Warm": "Softer and a bit darker.",
    "Bright": "More presence and air.",
    "Punch": "More kick around the low mids.",
    "Clear Mid": "Pull muddy mids down.",
    "Reset": "Set every band, preamp, and master back to 0.",
    "Mute": "Silence all sound while the equalizer is on. Click again to unmute.",
    "Save": "Save the current knobs as a plain text .eqpreset file.",
    "Load": "Open a .eqpreset text file and apply those knobs.",
}


# ─── DSP: cascade peaking biquads (proper graphic EQ — low latency, no FFT smear) ─
def _band_q(bands: List[float], i: int) -> float:
    """Octave-ish Q from neighbor spacing (RBJ peaking)."""
    f = float(bands[i])
    lo = float(bands[i - 1]) if i > 0 else f / 2.2
    hi = float(bands[i + 1]) if i + 1 < len(bands) else f * 2.2
    # geometric bandwidth → Q ≈ f0 / BW
    bw = max(hi / lo, 1.05)
    # convert ratio bandwidth to Q (approx for peaking)
    q = math.sqrt(bw) / (bw - 1.0) if bw > 1.01 else 1.4
    return float(max(0.55, min(4.0, q * 0.85)))


class _Biquad:
    """Transposed Direct Form II peaking / highpass section."""

    __slots__ = ("b0", "b1", "b2", "a1", "a2", "z1", "z2")

    def __init__(self) -> None:
        self.b0 = 1.0
        self.b1 = 0.0
        self.b2 = 0.0
        self.a1 = 0.0
        self.a2 = 0.0
        self.z1 = 0.0
        self.z2 = 0.0

    def reset(self) -> None:
        self.z1 = 0.0
        self.z2 = 0.0

    def set_peaking(self, fs: float, f0: float, q: float, gain_db: float) -> None:
        if abs(float(gain_db)) < 0.05:
            self.b0, self.b1, self.b2 = 1.0, 0.0, 0.0
            self.a1, self.a2 = 0.0, 0.0
            return
        fs = max(8000.0, float(fs))
        f0 = min(max(20.0, float(f0)), fs * 0.45)
        q = max(0.1, float(q))
        A = 10.0 ** (float(gain_db) / 40.0)
        w0 = 2.0 * math.pi * f0 / fs
        cosw = math.cos(w0)
        sinw = math.sin(w0)
        alpha = sinw / (2.0 * q)
        b0 = 1.0 + alpha * A
        b1 = -2.0 * cosw
        b2 = 1.0 - alpha * A
        a0 = 1.0 + alpha / A
        a1 = -2.0 * cosw
        a2 = 1.0 - alpha / A
        inv = 1.0 / a0
        self.b0, self.b1, self.b2 = b0 * inv, b1 * inv, b2 * inv
        self.a1, self.a2 = a1 * inv, a2 * inv

    def set_highpass(self, fs: float, f0: float = 25.0, q: float = 0.707) -> None:
        """Gentle rumble filter — keeps LF controllable without killing musical bass."""
        fs = max(8000.0, float(fs))
        f0 = min(max(10.0, float(f0)), fs * 0.15)
        w0 = 2.0 * math.pi * f0 / fs
        cosw = math.cos(w0)
        sinw = math.sin(w0)
        alpha = sinw / (2.0 * max(0.1, q))
        b0 = (1.0 + cosw) * 0.5
        b1 = -(1.0 + cosw)
        b2 = (1.0 + cosw) * 0.5
        a0 = 1.0 + alpha
        a1 = -2.0 * cosw
        a2 = 1.0 - alpha
        inv = 1.0 / a0
        self.b0, self.b1, self.b2 = b0 * inv, b1 * inv, b2 * inv
        self.a1, self.a2 = a1 * inv, a2 * inv

    def process(self, x: np.ndarray) -> np.ndarray:
        y = np.empty_like(x, dtype=np.float64)
        z1, z2 = self.z1, self.z2
        b0, b1, b2 = self.b0, self.b1, self.b2
        a1, a2 = self.a1, self.a2
        for i, s in enumerate(x):
            out = b0 * s + z1
            z1 = b1 * s - a1 * out + z2
            z2 = b2 * s - a2 * out
            y[i] = out
        self.z1, self.z2 = z1, z2
        return y


class GraphicEQ:
    def __init__(self, bands: List[float]) -> None:
        self.bands = [float(b) for b in bands]
        self.gains = [0.0] * len(bands)
        self.preamp_db = 0.0
        self.master_db = 0.0
        self.bypass = False
        self.limiter_on = True
        self.auto_preamp = True
        self._sample_rate = float(SAMPLE_RATE)
        self._lock = threading.Lock()
        self._peak = 0.0
        self._hpf_l = _Biquad()
        self._hpf_r = _Biquad()
        self._bands_l = [_Biquad() for _ in bands]
        self._bands_r = [_Biquad() for _ in bands]
        self._sos = None  # scipy second-order sections when available
        self._zi_l = None
        self._zi_r = None
        self._use_sos = False
        try:
            from scipy.signal import sosfilt as _sf  # noqa: F401

            self._sosfilt = _sf
            from scipy.signal import sosfilt_zi as _sz

            self._sosfilt_zi = _sz
            self._use_sos = True
        except Exception:
            self._sosfilt = None
            self._sosfilt_zi = None
            self._use_sos = False
        self._rebuild()

    def _auto_preamp_db(self) -> float:
        if not self.auto_preamp:
            return 0.0
        # Compensate boost headroom so presets don't clip/distort
        boost = sum(g for g in self.gains if g > 0.0)
        return -min(10.0, boost * 0.32)

    def _is_unity(self) -> bool:
        return (
            abs(self.preamp_db) < 0.01
            and abs(self._auto_preamp_db()) < 0.01
            and all(abs(g) < 0.05 for g in self.gains)
        )

    def _rebuild(self) -> None:
        fs = self._sample_rate
        self._hpf_l.set_highpass(fs, 22.0, 0.707)
        self._hpf_r.set_highpass(fs, 22.0, 0.707)
        for i, f0 in enumerate(self.bands):
            q = _band_q(self.bands, i)
            g = float(self.gains[i])
            self._bands_l[i].set_peaking(fs, f0, q, g)
            self._bands_r[i].set_peaking(fs, f0, q, g)
        secs = [self._hpf_l] + list(self._bands_l)
        self._sos = np.asarray(
            [[bq.b0, bq.b1, bq.b2, 1.0, bq.a1, bq.a2] for bq in secs],
            dtype=np.float64,
        )
        if self._use_sos and self._sosfilt_zi is not None:
            try:
                zi = self._sosfilt_zi(self._sos)
                self._zi_l = np.array(zi, copy=True)
                self._zi_r = np.array(zi, copy=True)
            except Exception:
                self._zi_l = self._zi_r = None

    def set_gains(self, gains: List[float]) -> None:
        with self._lock:
            self.gains = [max(GAIN_MIN, min(GAIN_MAX, float(g))) for g in gains]
            self._rebuild()

    def set_band(self, i: int, db: float) -> None:
        with self._lock:
            self.gains[i] = max(GAIN_MIN, min(GAIN_MAX, float(db)))
            self._rebuild()

    def set_preamp(self, db: float) -> None:
        with self._lock:
            self.preamp_db = max(-24.0, min(12.0, float(db)))

    def set_master_db(self, db: float) -> None:
        with self._lock:
            self.master_db = max(-24.0, min(12.0, float(db)))

    def set_bypass(self, on: bool) -> None:
        with self._lock:
            self.bypass = bool(on)

    def set_sample_rate(self, rate: float) -> None:
        with self._lock:
            rate = float(rate) if rate and rate > 0 else float(SAMPLE_RATE)
            if abs(rate - self._sample_rate) < 0.5:
                return
            self._sample_rate = rate
            for s in (self._hpf_l, self._hpf_r, *self._bands_l, *self._bands_r):
                s.reset()
            self._peak = 0.0
            self._rebuild()

    def _gain_lin(self) -> float:
        pre = self.preamp_db + self._auto_preamp_db()
        return float(np.power(10.0, (pre + self.master_db) / 20.0))

    def _soft_limit(self, y: np.ndarray) -> np.ndarray:
        """Transparent peak limiter + soft knee — avoids harsh digital clip."""
        if not self.limiter_on:
            return np.tanh(y * 0.95)  # still soft-clip as safety
        peak = float(np.max(np.abs(y))) if y.size else 0.0
        self._peak = max(peak, self._peak * 0.993)
        ceiling = 0.95
        if self._peak > ceiling and self._peak > 1e-9:
            y = y * (ceiling / self._peak)
        # soft knee above 0.9
        abs_y = np.abs(y)
        over = abs_y > 0.9
        if np.any(over):
            s = np.sign(y)
            t = (abs_y - 0.9) / 0.1
            y = np.where(over, s * (0.9 + 0.1 * np.tanh(t)), y)
        return np.clip(y, -1.0, 1.0)

    def _process_chan(self, x: np.ndarray, hpf: _Biquad, bands: List[_Biquad], zi_attr: str) -> np.ndarray:
        x = np.asarray(x, dtype=np.float64).reshape(-1)
        if self.bypass:
            return x
        if self._use_sos and self._sos is not None and self._sosfilt is not None:
            try:
                zi = getattr(self, zi_attr)
                if zi is not None:
                    y, zi_new = self._sosfilt(self._sos, x, zi=zi)
                    setattr(self, zi_attr, zi_new)
                else:
                    y = self._sosfilt(self._sos, x)
                return np.asarray(y, dtype=np.float64)
            except Exception:
                pass
        y = hpf.process(x)
        if not self._is_unity():
            for i, bq in enumerate(bands):
                if abs(self.gains[i]) < 0.05:
                    continue
                y = bq.process(y)
        return y

    def process(self, mono: np.ndarray) -> np.ndarray:
        try:
            with self._lock:
                x = np.asarray(mono, dtype=np.float64).reshape(-1)
                if self.bypass:
                    y = x * self._gain_lin()
                else:
                    y = self._process_chan(x, self._hpf_l, self._bands_l, "_zi_l") * self._gain_lin()
                y = self._soft_limit(y)
                return y.astype(np.float32)
        except Exception as exc:
            log_exception("GraphicEQ.process", exc)
            return np.asarray(mono, dtype=np.float32).reshape(-1)

    def process_stereo(self, left: np.ndarray, right: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
        try:
            with self._lock:
                l = np.asarray(left, dtype=np.float64).reshape(-1)
                r = np.asarray(right, dtype=np.float64).reshape(-1)
                n = max(len(l), len(r))
                if len(l) < n:
                    l = np.pad(l, (0, n - len(l)))
                if len(r) < n:
                    r = np.pad(r, (0, n - len(r)))
                g = self._gain_lin()
                if self.bypass:
                    ol, or_ = l * g, r * g
                else:
                    ol = self._process_chan(l, self._hpf_l, self._bands_l, "_zi_l") * g
                    or_ = self._process_chan(r, self._hpf_r, self._bands_r, "_zi_r") * g
                stacked = self._soft_limit(np.column_stack([ol, or_]))
                return stacked[:, 0].astype(np.float32), stacked[:, 1].astype(np.float32)
        except Exception as exc:
            log_exception("GraphicEQ.process_stereo", exc)
            return (
                np.asarray(left, dtype=np.float32).reshape(-1),
                np.asarray(right, dtype=np.float32).reshape(-1),
            )

    def process_block(self, stereo: np.ndarray) -> np.ndarray:
        try:
            arr = np.asarray(stereo, dtype=np.float32)
            if arr.ndim == 1:
                y = self.process(arr)
                return np.column_stack([y, y])
            if arr.shape[1] < 2:
                y = self.process(arr[:, 0])
                return np.column_stack([y, y])
            l, r = self.process_stereo(arr[:, 0], arr[:, 1])
            n = arr.shape[0]
            return np.column_stack([l[:n], r[:n]]).astype(np.float32)
        except Exception as exc:
            log_exception("GraphicEQ.process_block", exc)
            return np.asarray(stereo, dtype=np.float32)


# ─── Spectrum analysis ───────────────────────────────────────────────────────
class Spectrum:
    def __init__(self) -> None:
        self.window = np.hanning(FFT_SIZE).astype(np.float32)
        self.buf = np.zeros(FFT_SIZE, dtype=np.float32)
        self.mags = np.zeros(FFT_SIZE // 2, dtype=np.float32)
        self.smooth = np.zeros(FFT_SIZE // 2, dtype=np.float32)
        self.history: deque = deque(maxlen=HISTORY)
        for _ in range(HISTORY):
            self.history.append(np.zeros(64, dtype=np.float32))
        self.lock = threading.Lock()
        self._sample_rate = float(SAMPLE_RATE)
        self.disp_n = 64
        self._hp_z = 0.0
        self._hp_r = 0.995
        self._rebuild_maps()

    def _rebuild_maps(self) -> None:
        rate = self._sample_rate
        freqs = np.fft.rfftfreq(FFT_SIZE, 1.0 / rate)[: FFT_SIZE // 2]
        self.freqs = freqs
        nyq = max(rate / 2.0 - 1.0, F_MIN * 2)
        edges = np.geomspace(F_MIN, min(F_MAX, nyq), self.disp_n + 1)
        self.bin_map: List[Tuple[int, int]] = []
        for a, b in zip(edges[:-1], edges[1:]):
            i0 = int(np.searchsorted(freqs, a))
            i1 = int(np.searchsorted(freqs, b))
            self.bin_map.append((max(1, i0), max(i0 + 1, i1)))
        self._weights = np.ones(self.disp_n, dtype=np.float32)
        for i, (a, b) in enumerate(self.bin_map):
            f_c = float(np.mean(freqs[a:b])) if b > a else 100.0
            f_c = max(f_c, 40.0)
            self._weights[i] = float(np.sqrt(1000.0 / f_c))
        self._weights /= float(np.median(self._weights) or 1.0)
        # Crush sub / rumble columns so the left side of the mesh doesn't brick-wall
        for i in range(min(8, self.disp_n)):
            self._weights[i] *= max(0.08, 0.12 + 0.10 * i)

    def set_sample_rate(self, rate: float) -> None:
        rate = float(rate) if rate and rate > 0 else float(SAMPLE_RATE)
        if abs(rate - self._sample_rate) < 0.5:
            return
        with self.lock:
            self._sample_rate = rate
            self._rebuild_maps()

    def _highpass(self, samples: np.ndarray) -> np.ndarray:
        """Strip DC / slow drift so the 20–50 Hz mesh columns don't brick-wall."""
        x = np.asarray(samples, dtype=np.float64).reshape(-1)
        if x.size == 0:
            return samples.astype(np.float32)
        mean = float(np.mean(x))
        self._hp_z = 0.98 * self._hp_z + 0.02 * mean
        return (x - self._hp_z).astype(np.float32)

    def push(self, samples: np.ndarray) -> None:
        try:
            samples = self._highpass(np.asarray(samples, dtype=np.float32).reshape(-1))
            n = len(samples)
            if n >= FFT_SIZE:
                self.buf[:] = samples[-FFT_SIZE:]
            else:
                self.buf = np.roll(self.buf, -n)
                self.buf[-n:] = samples
            frame = self.buf - float(np.mean(self.buf))
            spec = np.fft.rfft(frame * self.window)
            mags = np.abs(spec[: FFT_SIZE // 2]).astype(np.float32)
            mags[0] = 0.0
            # Kill bins below ~40 Hz (rumble / DC bleed)
            for bi, f in enumerate(self.freqs):
                if f < 40.0:
                    mags[bi] = 0.0
                elif f < 60.0:
                    mags[bi] *= 0.2
            mags = np.log1p(mags * 5.0)
            with self.lock:
                self.smooth = self.smooth * 0.62 + mags * 0.38
                row = np.zeros(self.disp_n, dtype=np.float32)
                for i, (a, b) in enumerate(self.bin_map):
                    if b > a:
                        row[i] = float(np.mean(self.smooth[a:b])) * self._weights[i]
                # Cap the leftmost columns so one rumble spike can't dominate
                if self.disp_n >= 3:
                    mid = float(np.median(row[3:])) if self.disp_n > 3 else float(np.median(row))
                    for i in range(min(3, self.disp_n)):
                        row[i] = min(row[i], mid * 1.15)
                peak = float(row.max()) if row.size else 0.0
                if peak < 0.10:
                    row[:] = 0
                else:
                    row = np.clip(row / max(peak * 0.9, 2.6), 0, 1)
                    row[row < 0.05] = 0
                self.history.append(row.copy())
        except Exception as exc:
            log_exception("spectrum.push", exc)

    def mesh(self) -> np.ndarray:
        with self.lock:
            try:
                return np.stack(list(self.history), axis=0)
            except Exception:
                return np.zeros((HISTORY, self.disp_n), dtype=np.float32)


# ─── System EQ (Windows audio effect — no virtual cable) ─────────────────────
class SystemApo:
    """
    Drive Equalizer APO, the usual Windows audio effect other EQ apps install.
    It processes sound inside Windows before the speakers, so every app is affected.
    Spectra only writes config\\spectra.txt; it does not play a second copy of the audio.
    """

    INCLUDE_NAME = "spectra.txt"
    URL = "https://stackdrop.it/downloads/EqualizerAPO.exe"

    @staticmethod
    def config_dir() -> Optional[str]:
        roots: List[str] = []
        for key in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)"):
            val = os.environ.get(key)
            if val:
                roots.append(val)
        roots.extend([r"C:\Program Files", r"C:\Program Files (x86)"])
        seen = set()
        for root in roots:
            path = os.path.join(root, "EqualizerAPO", "config")
            key = path.lower()
            if key in seen:
                continue
            seen.add(key)
            if os.path.isdir(path):
                return path
        return None

    @classmethod
    def installed(cls) -> bool:
        return cls.config_dir() is not None

    def apply(
        self,
        gains: List[float],
        preamp_db: float,
        master_db: float,
        enabled: bool,
        muted: bool,
    ) -> Tuple[bool, str]:
        folder = self.config_dir()
        if not folder:
            return False, "system effect not installed"
        if not enabled:
            use_gains = [0.0] * len(BAND_HZ)
            pre = 0.0
        else:
            use_gains = [float(g) for g in gains]
            pre = float(preamp_db) + float(master_db)
            if muted:
                pre = -120.0
        points = "; ".join(f"{int(hz)} {g:.1f}" for hz, g in zip(BAND_HZ, use_gains))
        body = (
            "# Spectra EQ — this file is rewritten while Spectra is open.\n"
            f"Preamp: {pre:.1f} dB\n"
            f"GraphicEQ: {points}\n"
        )
        spec = os.path.join(folder, self.INCLUDE_NAME)
        cfg = os.path.join(folder, "config.txt")
        try:
            self._write_text(spec, body)
            self._ensure_include(cfg)
        except OSError as exc:
            if self._needs_admin(exc):
                self._ask_write_permission(folder)
                return (
                    False,
                    "Click Yes on the Windows prompt so Spectra can save the EQ, then press EQ ON again.",
                )
            return False, f"Could not save system EQ: {exc}"
        if enabled and not muted:
            return True, "System EQ ON — these knobs change every sound you play."
        if enabled and muted:
            return True, "System EQ muted — all sound is silenced until you unmute."
        return True, "EQ off — sound is back to normal."

    @staticmethod
    def _needs_admin(exc: BaseException) -> bool:
        if getattr(exc, "errno", None) in (1, 13):
            return True
        low = str(exc).lower()
        return "permission" in low or "access" in low or "denied" in low

    @staticmethod
    def _write_text(path: str, body: str) -> None:
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(body)

    def _ensure_include(self, cfg_path: str) -> None:
        line = f"Include: {self.INCLUDE_NAME}"
        existing = ""
        if os.path.isfile(cfg_path):
            with open(cfg_path, "r", encoding="utf-8", errors="replace") as f:
                existing = f.read()
        if self.INCLUDE_NAME.lower() in existing.lower():
            return
        self._write_text(cfg_path, line + "\n" + existing)

    @staticmethod
    def _ask_write_permission(folder: str) -> None:
        if sys.platform != "win32":
            return
        try:
            import ctypes

            params = f'"{folder}" /grant *S-1-5-32-545:(OI)(CI)M /C'
            ctypes.windll.shell32.ShellExecuteW(None, "runas", "icacls.exe", params, None, 1)
        except Exception as exc:
            log_exception("apo_grant", exc)


# ─── Devices + audio engine (capture = speakers loopback only; no mics) ───────
@dataclass
class AudioDev:
    index: Optional[int]
    name: str
    kind: str  # wasapi | loopback | mix | soundcard | output
    hostapi: str = ""
    channels: int = 2
    extra: object = None  # WasapiSettings when loopback
    rate: int = SAMPLE_RATE


def _query_all_devices():
    if sd is None:
        return []
    try:
        devices = sd.query_devices()
        if isinstance(devices, dict):
            return [devices]
        return list(devices)
    except Exception as exc:
        log_exception("query_devices", exc)
        return []


def _query_hostapis():
    if sd is None:
        return []
    try:
        apis = sd.query_hostapis()
        if isinstance(apis, dict):
            return [apis]
        return list(apis)
    except Exception as exc:
        log_exception("query_hostapis", exc)
        return []


def _default_io() -> Tuple[Optional[int], Optional[int]]:
    if sd is None:
        return None, None
    try:
        d = sd.default.device
        if isinstance(d, (list, tuple)) and len(d) >= 2:
            din = int(d[0]) if d[0] is not None and int(d[0]) >= 0 else None
            dout = int(d[1]) if d[1] is not None and int(d[1]) >= 0 else None
            return din, dout
    except Exception as exc:
        log_exception("default.device", exc)
    return None, None


def _dev_rate(index: int, fallback: int = SAMPLE_RATE) -> int:
    try:
        info = sd.query_devices(index)
        rate = int(float(info.get("default_samplerate") or fallback))
        return rate if rate >= 8000 else fallback
    except Exception:
        return fallback


def _probe_capture(dev: AudioDev) -> bool:
    """Keep only capture sources that actually open."""
    # soundcard loopback — native WASAPI, no PortAudio flag needed
    if getattr(dev, "kind", "") == "soundcard":
        if sc is None or dev.extra is None:
            return False
        try:
            mic = dev.extra
            rate = int(dev.rate or 48000)
            ch = max(1, int(dev.channels or 2))
            with mic.recorder(samplerate=rate, channels=ch, blocksize=512) as rec:
                data = rec.record(numframes=512)
            ok = data is not None and getattr(data, "size", 0) > 0
            if ok:
                return True
        except Exception as exc:
            log_exception(f"probe_soundcard:{dev.name}", exc)
            return False
        return False

    if sd is None or dev.index is None:
        return False
    rates: List[int] = []
    for r in (dev.rate, _dev_rate(dev.index), 48000, 44100):
        if r and r not in rates:
            rates.append(r)
    for rate in rates:
        for ch in (dev.channels, 2, 1):
            if ch <= 0:
                continue
            stream = None
            try:
                kwargs = dict(
                    samplerate=rate,
                    blocksize=512,
                    device=dev.index,
                    channels=ch,
                    dtype="float32",
                    latency="high",
                )
                if dev.extra is not None:
                    kwargs["extra_settings"] = dev.extra
                stream = sd.InputStream(**kwargs)
                stream.start()
                time.sleep(0.03)
                stream.stop()
                stream.close()
                dev.channels = ch
                dev.rate = rate
                return True
            except Exception:
                try:
                    if stream is not None:
                        stream.close()
                except Exception:
                    pass
    return False


def _probe_output(dev: AudioDev) -> bool:
    if sd is None or dev.index is None:
        return False
    rates: List[int] = []
    for r in (dev.rate, _dev_rate(dev.index), 48000, 44100):
        if r and r not in rates:
            rates.append(r)
    for rate in rates:
        for ch in (dev.channels, 2, 1):
            stream = None
            try:
                stream = sd.OutputStream(
                    samplerate=rate,
                    blocksize=512,
                    device=dev.index,
                    channels=ch,
                    dtype="float32",
                    latency="high",
                )
                stream.start()
                time.sleep(0.03)
                stream.stop()
                stream.close()
                dev.channels = ch
                dev.rate = rate
                return True
            except Exception:
                try:
                    if stream is not None:
                        stream.close()
                except Exception:
                    pass
    return False



def make_wasapi_loopback_settings():
    """
    Build WASAPI loopback extra_settings across sounddevice versions.
    Returns (settings_or_None, mode, message)
      mode: "settings" — open OUTPUT device index as InputStream with these extras
            "devices"  — PortAudio lists loopback as separate INPUT devices (modern PA)
            "none"     — no PortAudio loopback path (use soundcard / Stereo Mix)
    Never raises. Never calls WasapiSettings(loopback=...) unless the kwarg exists.
    """
    if sd is None:
        return None, "none", "sounddevice not installed"

    # 1) Classic sounddevice ≤0.4.x: WasapiSettings(loopback=True)
    try:
        sig = inspect.signature(sd.WasapiSettings.__init__)
        if "loopback" in sig.parameters:
            try:
                return sd.WasapiSettings(loopback=True), "settings", ""
            except Exception as exc:
                log_exception("WasapiSettings(loopback=True)", exc)
    except Exception as exc:
        log_exception("inspect WasapiSettings", exc)

    # 2) Construct then set .loopback if the attribute exists
    try:
        s = sd.WasapiSettings()
        if hasattr(s, "loopback"):
            try:
                s.loopback = True
                return s, "settings", ""
            except Exception as exc:
                log_exception("WasapiSettings.loopback setattr", exc)
    except Exception as exc:
        log_exception("WasapiSettings()", exc)

    # 3) Modern sounddevice 0.5.x — no loopback flag; PortAudio exposes [Loopback] inputs
    #    Always prefer device-enumeration mode when WASAPI host exists.
    if _wasapi_hostapi_index() is not None:
        return None, "devices", ""

    return None, "none", "WASAPI host API not found (Windows only)."


def _pa_is_loopback_device(index: int) -> bool:
    if sd is None or index is None:
        return False
    try:
        lib = getattr(sd, "_lib", None)
        if lib is None:
            return False
        fn = getattr(lib, "PaWasapi_IsLoopback", None)
        if fn is None:
            return False
        return bool(fn(int(index)))
    except Exception:
        return False


def _wasapi_hostapi_index():
    if sd is None:
        return None
    try:
        for i, h in enumerate(_query_hostapis()):
            if "wasapi" in safe_name(h.get("name")).lower():
                return i
    except Exception as exc:
        log_exception("wasapi hostapi", exc)
    return None


def _looks_like_loopback_name(name: str) -> bool:
    low = (name or "").lower()
    return (
        "[loopback]" in low
        or "(loopback)" in low
        or low.endswith(" loopback")
        or " loopback" in low
        or low.startswith("loopback")
    )


def _base_device_name(name: str) -> str:
    n = safe_name(name)
    for token in (" [Loopback]", " (Loopback)", " Loopback", " (loopback)"):
        n = n.replace(token, "")
    return n.strip().lower()


def list_soundcard_loopbacks() -> List[AudioDev]:
    """
    Windows native WASAPI loopback via the `soundcard` package.
    Preferred path when PortAudio has no loopback= support (sounddevice 0.5.x).
    """
    out: List[AudioDev] = []
    if sc is None:
        print("soundcard: not installed — WASAPI speaker capture unavailable via this path")
        return out
    try:
        # Documented API: loopbacks are microphones with isloopback=True
        mics = list(sc.all_microphones(include_loopback=True))
    except Exception as exc:
        log_exception("soundcard.all_microphones(include_loopback=True)", exc)
        mics = []

    loopbacks = []
    for m in mics:
        try:
            if getattr(m, "isloopback", False):
                loopbacks.append(m)
        except Exception:
            continue

    # Fallback: resolve each speaker name as a loopback mic
    if not loopbacks:
        try:
            for sp in sc.all_speakers():
                sp_name = safe_name(getattr(sp, "name", ""))
                if not sp_name:
                    continue
                try:
                    mic = sc.get_microphone(sp_name, include_loopback=True)
                    if mic is not None:
                        loopbacks.append(mic)
                except Exception as exc:
                    log_exception(f"soundcard.get_microphone({sp_name})", exc)
        except Exception as exc:
            log_exception("soundcard.all_speakers fallback", exc)

    try:
        default_sp = sc.default_speaker()
        default_name = safe_name(getattr(default_sp, "name", ""))
    except Exception:
        default_name = ""

    seen = set()
    for i, mic in enumerate(loopbacks):
        try:
            name = safe_name(getattr(mic, "name", f"Speakers {i}"))
            if not name or name.lower() in seen:
                continue
            seen.add(name.lower())
            ch = 2
            try:
                chans = int(getattr(mic, "channels", 2) or 2)
                ch = 2 if chans >= 2 else max(1, chans)
            except Exception:
                ch = 2
            tag = name
            if default_name and (
                name.lower() == default_name.lower()
                or default_name.lower() in name.lower()
                or name.lower() in default_name.lower()
            ):
                tag = f"Default speakers - {name}"
            out.append(
                AudioDev(
                    index=None,
                    name=tag,
                    kind="soundcard",
                    hostapi="WASAPI (soundcard)",
                    channels=ch,
                    extra=mic,
                    rate=48000,
                )
            )
        except Exception as exc:
            log_exception(f"soundcard.loopback.entry:{i}", exc)
            continue

    print(f"soundcard: found {len(out)} WASAPI loopback speaker(s)")
    return out


def list_audio_devices(probe: bool = True):
    list_audio_devices.last_note = ""  # type: ignore[attr-defined]
    """
    Capture = speakers/headphones via WASAPI loopback (or Stereo Mix / soundcard).
    Never lists microphones. When probe=True, only devices that open are kept.
    """
    captures: List[AudioDev] = []
    outputs: List[AudioDev] = []
    loopback_note = ""
    try:
        if sd is None and sc is None and not WASAPI_NATIVE:
            captures.append(AudioDev(None, "(no audio backend)", "loopback", channels=1))
            outputs.append(AudioDev(None, "(none)", "output", channels=2))
            return captures, outputs

        hostapis = _query_hostapis() if sd is not None else []
        devices = _query_all_devices() if sd is not None else []

        def hapiname(i: int) -> str:
            try:
                if 0 <= i < len(hostapis):
                    return safe_name(hostapis[i].get("name"), "")
            except Exception:
                pass
            return ""

        candidates: List[AudioDev] = []

        # ── Mode 0 (PRIMARY on Windows): Microsoft WASAPI loopback ──
        # https://learn.microsoft.com/en-us/windows/win32/coreaudio/loopback-recording
        # Open the *render* endpoint with AUDCLNT_STREAMFLAGS_LOOPBACK (via soundcard).
        if WASAPI_NATIVE or sc is not None:
            try:
                endpoints, wasapi_err = list_render_endpoints()
                for ep in endpoints:
                    # Prefer soundcard listen when mic handle exists (avoids ctypes COM bugs)
                    if getattr(ep, "mic", None) is not None:
                        candidates.append(
                            AudioDev(
                                index=None,
                                name=("Default speakers - " + ep.name) if ep.is_default else ep.name,
                                kind="soundcard",
                                hostapi="WASAPI (soundcard)",
                                channels=ep.channels,
                                extra=ep.mic,
                                rate=ep.rate,
                            )
                        )
                    else:
                        candidates.append(
                            AudioDev(
                                index=None,
                                name=("Default speakers - " + ep.name) if ep.is_default else ep.name,
                                kind="wasapi",
                                hostapi="WASAPI loopback",
                                channels=ep.channels,
                                extra=ep,
                                rate=ep.rate,
                            )
                        )
                if endpoints:
                    loopback_note = ""
                    print(f"WASAPI: {len(endpoints)} render loopback endpoint(s)")
                elif wasapi_err:
                    loopback_note = wasapi_err
                    print(f"WASAPI: {wasapi_err}")
            except Exception as exc:
                log_exception("list_render_endpoints", exc)
                loopback_note = str(exc)

        wasapi_i = _wasapi_hostapi_index()
        extra, lb_mode, lb_msg = make_wasapi_loopback_settings()
        if not candidates and lb_msg:
            loopback_note = lb_msg or loopback_note

        _, dout = _default_io() if sd is not None else (None, None)

        # ── Mode A: classic PortAudio — OUTPUT index + WasapiSettings(loopback=True) ──
        if (
            not any(c.kind == "wasapi" for c in candidates)
            and sd is not None
            and wasapi_i is not None
            and lb_mode == "settings"
            and extra is not None
        ):
            order_indices: List[int] = []
            if dout is not None:
                order_indices.append(int(dout))
            for i, d in enumerate(devices):
                try:
                    if int(d.get("hostapi", -1)) != wasapi_i:
                        continue
                    if int(d.get("max_output_channels") or 0) <= 0:
                        continue
                    if i not in order_indices:
                        order_indices.append(i)
                except Exception:
                    continue

            for i in order_indices:
                try:
                    info = devices[i] if i < len(devices) else sd.query_devices(i)
                    name = safe_name(info.get("name"), f"Device {i}")
                    low = name.lower()
                    if any(k in low for k in ("microphone", "mic array", "headset microphone")):
                        continue
                    ch_out = int(info.get("max_output_channels") or 0)
                    if ch_out <= 0:
                        continue
                    ch = 2 if ch_out >= 2 else 1
                    rate = int(float(info.get("default_samplerate") or SAMPLE_RATE))
                    tag = f"Default speakers - {name}" if (dout is not None and i == dout) else name
                    candidates.append(
                        AudioDev(
                            i,
                            tag,
                            "loopback",
                            hapiname(wasapi_i),
                            ch,
                            extra,
                            rate if rate >= 8000 else SAMPLE_RATE,
                        )
                    )
                except Exception:
                    continue

        # ── Mode B: modern PortAudio — [Loopback] INPUT devices ──
        # Run whenever WASAPI exists and we didn't already fill via Mode A
        if sd is not None and wasapi_i is not None and not candidates:
            auto_extra = None
            try:
                auto_extra = sd.WasapiSettings(auto_convert=True)
            except Exception:
                try:
                    auto_extra = sd.WasapiSettings()
                except Exception:
                    auto_extra = None

            # Map output names → indices for pairing
            wasapi_outputs = {}
            for i, d in enumerate(devices):
                try:
                    if int(d.get("hostapi", -1)) != wasapi_i:
                        continue
                    if int(d.get("max_output_channels") or 0) <= 0:
                        continue
                    wasapi_outputs[_base_device_name(d.get("name") or "")] = i
                except Exception:
                    continue

            for i, d in enumerate(devices):
                try:
                    if int(d.get("hostapi", -1)) != wasapi_i:
                        continue
                    name = safe_name(d.get("name"), f"Device {i}")
                    ch_in = int(d.get("max_input_channels") or 0)
                    if ch_in <= 0:
                        continue
                    is_lb = _pa_is_loopback_device(i) or _looks_like_loopback_name(name)
                    if not is_lb:
                        continue
                    ch = 2 if ch_in >= 2 else 1
                    rate = int(float(d.get("default_samplerate") or SAMPLE_RATE))
                    tag = name
                    for tok in (" [Loopback]", " (Loopback)", " Loopback", " [loopback]", " (loopback)"):
                        tag = tag.replace(tok, "")
                    tag = tag.strip() or name
                    base = _base_device_name(name)
                    if dout is not None and wasapi_outputs.get(base) == dout:
                        tag = f"Default speakers - {tag}"
                    candidates.append(
                        AudioDev(
                            i,
                            tag,
                            "loopback",
                            hapiname(wasapi_i),
                            ch,
                            auto_extra,
                            rate if rate >= 8000 else SAMPLE_RATE,
                        )
                    )
                except Exception:
                    continue

            if not any(c.kind in ("loopback", "wasapi", "soundcard") for c in candidates):
                loopback_note = (
                    "PortAudio has no [Loopback] devices. "
                    "Using built-in WASAPI ctypes path."
                )

        if wasapi_i is None and sd is not None and not any(c.kind == "wasapi" for c in candidates):
            if not loopback_note:
                loopback_note = "WASAPI host API not listed by PortAudio (OK if soundcard works)."

        # Stereo Mix / What U Hear — system playback tap, not a mic
        if sd is not None:
            for i, d in enumerate(devices):
                try:
                    name = safe_name(d.get("name"))
                    low = name.lower()
                    if int(d.get("max_input_channels") or 0) <= 0:
                        continue
                    if not any(
                        k in low
                        for k in ("stereo mix", "what u hear", "wave out mix", "monitor of")
                    ):
                        continue
                    if any(x.index == i for x in candidates):
                        continue
                    ch = min(2, max(1, int(d["max_input_channels"])))
                    rate = int(float(d.get("default_samplerate") or SAMPLE_RATE))
                    candidates.append(
                        AudioDev(
                            i,
                            f"Mix  -  {name}",
                            "mix",
                            hapiname(int(d.get("hostapi", 0) or 0)),
                            ch,
                            None,
                            rate,
                        )
                    )
                except Exception:
                    continue

        if probe and candidates:
            for c in candidates:
                # Native WASAPI / soundcard: trust enum — don't drop on silent probe
                if c.kind in ("wasapi", "soundcard") and c.extra is not None:
                    captures.append(c)
                    continue
                if _probe_capture(c):
                    captures.append(c)
                else:
                    print(f"Skip capture (won't open): {c.name}")
        else:
            captures = list(candidates)

        # Playback targets for EQ ON
        out_cands: List[AudioDev] = []
        if sd is not None:
            if dout is not None:
                try:
                    info = sd.query_devices(dout)
                    ch = min(2, max(1, int(info.get("max_output_channels") or 2)))
                    dname = safe_name(info.get("name"), "Speakers")
                    if is_bogus_play_name(dname):
                        print(f"Skip default play stub: {dname}")
                    elif ch > 0:
                        rate = int(float(info.get("default_samplerate") or SAMPLE_RATE))
                        out_cands.append(
                            AudioDev(
                                dout,
                                f"Default  -  {dname}",
                                "output",
                                hapiname(int(info.get("hostapi", 0) or 0)),
                                ch,
                                None,
                                rate,
                            )
                        )
                except Exception as exc:
                    log_exception("default-output", exc)

            for i, d in enumerate(devices):
                try:
                    if int(d.get("max_output_channels") or 0) <= 0:
                        continue
                    if any(x.index == i for x in out_cands):
                        continue
                    name = safe_name(d.get("name"))
                    low = name.lower()
                    if any(k in low for k in ("microphone", "mic array")):
                        continue
                    if is_bogus_play_name(name):
                        print(f"Skip play stub (mapper/primary): {name}")
                        continue
                    ch = min(2, max(1, int(d["max_output_channels"])))
                    rate = int(float(d.get("default_samplerate") or SAMPLE_RATE))
                    out_cands.append(
                        AudioDev(
                            i,
                            name,
                            "output",
                            hapiname(int(d.get("hostapi", 0) or 0)),
                            ch,
                            None,
                            rate,
                        )
                    )
                except Exception:
                    continue

            if probe and out_cands:
                for o in out_cands:
                    if _probe_output(o):
                        outputs.append(o)
                    else:
                        print(f"Skip output (won't open): {o.name}")
            else:
                outputs = list(out_cands)

    except Exception as exc:
        log_exception("list_audio_devices", exc)

    list_audio_devices.last_note = loopback_note  # type: ignore[attr-defined]

    if not captures:
        if not WASAPI_NATIVE:
            hint = "WASAPI is Windows-only — run SpectraEQ.exe on Windows"
        else:
            hint = "No speaker loopback — allow mic access for this app in Windows Privacy"
        captures.append(
            AudioDev(
                None,
                f"(no capture — {hint[:90]})",
                "loopback",
                channels=1,
            )
        )
    if not outputs:
        outputs.append(AudioDev(None, "(no working output)", "output", channels=2))
    return captures, outputs


class AudioEngine:
    def __init__(self, eq: GraphicEQ, spectrum: Spectrum) -> None:
        self.eq = eq
        self.spectrum = spectrum
        self.muted = False
        self.affect_sound = False
        self.show_pre_eq = False
        self.level_l = 0.0
        self.level_r = 0.0
        self.mode = "idle"
        self._stream = None
        self._in_stream = None
        self._out_stream = None
        self._alive = False
        self._thread = None
        self._starting = False
        self._fallback_depth = 0
        self._last_audio_t = time.time()
        self._force_rate: Optional[int] = None
        self._cable_auto_done = False
        self._eq_block_msg = ""  # sticky reason EQ ON was refused (survives restart)
        self._system_eq = False  # True when the Windows audio effect is applying the knobs
        self._apo = SystemApo()
        self._apo_next = 0.0
        try:
            self.inputs, self.outputs = list_audio_devices(probe=True)
        except Exception as exc:
            log_exception("AudioEngine.init devices", exc)
            self.inputs = [AudioDev(None, "(no capture)", "loopback", channels=1)]
            self.outputs = [AudioDev(None, "(none)", "output", channels=2)]
        self.in_idx = 0
        self.out_idx = self._pick_default_play_idx()
        # Prefer default speakers capture when available
        for i, d in enumerate(self.inputs):
            nm = safe_name(d.name).lower()
            if d.kind in ("wasapi", "soundcard") and (
                "default" in nm or getattr(getattr(d, "extra", None), "is_default", False)
            ):
                self.in_idx = i
                break
        note = getattr(list_audio_devices, "last_note", "") or ""
        first = self.input_dev if self.inputs else None
        if first is not None and (
            first.index is not None
            or first.kind in ("wasapi", "soundcard")
            or (first.extra is not None and first.kind != "loopback")
        ):
            self.status = f"Ready — capturing «{self.short_name(first, 40)}» ({len(self.inputs)} source(s))"
        else:
            self.status = (note or "No capture device")[:160]
        self._lock = threading.Lock()
        self._q: deque = deque(maxlen=4)
        self._q_lock = threading.Lock()

    def _is_bogus_play_name(self, name: str) -> bool:
        return is_bogus_play_name(name)

    def _pick_default_play_idx(self) -> int:
        """Avoid Sound Mapper / Primary — prefer headphones, cable, then real speakers."""
        if not self.outputs:
            return 0
        scored: List[Tuple[int, int]] = []
        for i, d in enumerate(self.outputs):
            nm = safe_name(d.name).lower()
            if is_bogus_play_name(nm):
                score = 90
            elif any(h in nm for h in ("headphone", "headset", "earphone", "earbud", "airpods", "buds")):
                score = 0
            elif any(h in nm for h in _CABLE_HINTS):
                score = 1
            elif "speaker" in nm:
                score = 5
            else:
                score = 10
            scored.append((score, i))
        scored.sort()
        return scored[0][1]

    def _refresh_play_safety_tip(self) -> None:
        """System EQ does not replay the speakers, so idle capture is not a feedback block."""
        return

    def push_system_eq(self, force: bool = False) -> None:
        """Write the current knobs into the Windows audio effect."""
        if not self._system_eq:
            return
        now = time.time()
        if not force and now < self._apo_next:
            return
        self._apo_next = now + 0.12
        try:
            ok, msg = self._apo.apply(
                list(self.eq.gains),
                float(self.eq.preamp_db),
                float(self.eq.master_db),
                True,
                bool(self.muted),
            )
            if msg:
                self.status = msg
            if not ok:
                self._system_eq = False
                self.affect_sound = False
        except Exception as exc:
            self.status = log_exception("push_system_eq", exc)

    @property
    def input_dev(self) -> AudioDev:
        if not self.inputs:
            return AudioDev(None, "(no capture)", "loopback", channels=1)
        return self.inputs[self.in_idx % len(self.inputs)]

    @property
    def output_dev(self) -> AudioDev:
        if not self.outputs:
            return AudioDev(None, "(none)", "output", channels=2)
        return self.outputs[self.out_idx % len(self.outputs)]

    @property
    def device_label(self) -> str:
        aff = "EQ ON" if self.affect_sound else "EQ OFF"
        return f"{aff}   CAPTURE: {self.short_name(self.input_dev)}   PLAY: {self.short_name(self.output_dev)}"

    @staticmethod
    def short_name(dev: AudioDev, n: int = 0) -> str:
        """Clean device label. n<=0 keeps the full name (HUD wraps instead of …)."""
        name = safe_name(getattr(dev, "name", None), "Device")
        for prefix in (
            "Loopback  -  ",
            "Mix  -  ",
            "In  -  ",
            "Out  -  ",
            "Default  -  ",
            "Default speakers - ",
        ):
            if name.startswith(prefix):
                name = name[len(prefix) :]
                break
        if n and n > 0 and len(name) > n:
            return name[: n - 1] + "…"
        return name

    def cycle_input(self, delta: int = 1) -> None:
        try:
            if not self.inputs:
                return
            self.in_idx = (self.in_idx + delta) % len(self.inputs)
            self.restart()
        except Exception as exc:
            self.status = log_exception("cycle_input", exc)

    def cycle_output(self, delta: int = 1) -> None:
        try:
            if not self.outputs:
                return
            self.out_idx = (self.out_idx + delta) % len(self.outputs)
            self.restart()
        except Exception as exc:
            self.status = log_exception("cycle_output", exc)

    def set_output(self, idx: int) -> None:
        try:
            if not self.outputs:
                return
            self.out_idx = int(idx) % len(self.outputs)
            self._eq_block_msg = ""
            self.status = "Connecting play device…"
            self.restart()
        except Exception as exc:
            self.status = log_exception("set_output", exc)

    def set_input(self, idx: int) -> None:
        try:
            if not self.inputs:
                return
            self.in_idx = int(idx) % len(self.inputs)
            self._eq_block_msg = ""
            self.status = "Connecting capture…"
            self.restart()
        except Exception as exc:
            self.status = log_exception("set_input", exc)

    def set_affect(self, on: bool) -> None:
        """EQ ON writes the knobs into the Windows audio effect. It does not replay the speakers."""
        try:
            if on:
                if not SystemApo.installed():
                    self.affect_sound = False
                    self._system_eq = False
                    self._eq_block_msg = (
                        "Windows can't let a normal app EQ the speakers it is listening to. "
                        "Other equalizers install a small audio effect once. Spectra uses that. "
                        "Click Install system EQ, install Equalizer APO, tick your Speakers, reboot, "
                        "then open Spectra and press EQ ON. The knobs then change YouTube, games, and everything else."
                    )
                    self.status = self._eq_block_msg
                    return
                self._eq_block_msg = ""
                self._system_eq = True
                self.affect_sound = True
                ok, msg = self._apo.apply(
                    list(self.eq.gains),
                    float(self.eq.preamp_db),
                    float(self.eq.master_db),
                    True,
                    bool(self.muted),
                )
                self.status = msg
                if not ok:
                    self._system_eq = False
                    self.affect_sound = False
                    self._eq_block_msg = msg
                return
            was_system = self._system_eq
            self.affect_sound = False
            self._system_eq = False
            self._eq_block_msg = ""
            if was_system or SystemApo.installed():
                _ok, msg = self._apo.apply(
                    list(self.eq.gains),
                    float(self.eq.preamp_db),
                    float(self.eq.master_db),
                    False,
                    False,
                )
                self.status = msg
        except Exception as exc:
            self.status = log_exception("set_affect", exc)

    def toggle_affect(self) -> None:
        self.set_affect(not self.affect_sound)

    def _is_cable_name(self, name: str) -> bool:
        n = safe_name(name).lower()
        return any(h in n for h in _CABLE_HINTS)

    def _is_physical_speaker_play(self, d: AudioDev) -> bool:
        """True for Speakers/Default play devices that are NOT cable/headphones."""
        n = safe_name(d.name).lower()
        if any(h in n for h in _SAFE_PLAY_HINTS):
            return False
        if self._is_cable_name(n):
            return False
        return ("speaker" in n) or n.startswith("default") or ("realtek" in n and "headphone" not in n)

    def _try_speaker_eq_via_cable(self) -> bool:
        """
        Wire CAPTURE=VB-Cable loopback, PLAY=real Speakers.
        User must set Windows default playback to CABLE Input so apps feed the cable.
        """
        try:
            cable_in = None
            for i, d in enumerate(self.inputs):
                if self._is_cable_name(d.name):
                    cable_in = i
                    break
            if cable_in is None:
                return False
            speaker_out = None
            for i, d in enumerate(self.outputs):
                if self._is_physical_speaker_play(d):
                    speaker_out = i
                    break
            if speaker_out is None:
                # fallback: first non-cable output
                for i, d in enumerate(self.outputs):
                    if not self._is_cable_name(d.name):
                        speaker_out = i
                        break
            if speaker_out is None:
                return False
            self.in_idx = cable_in
            self.out_idx = speaker_out
            # Verify no feedback with this routing
            if self._feedback_risk(self.input_dev, self.output_dev):
                return False
            return True
        except Exception as exc:
            log_exception("speaker_eq_via_cable", exc)
            return False

    def _list_safe_play_names(self, limit: int = 4) -> List[str]:
        names: List[str] = []
        for d in self.outputs:
            nm = safe_name(d.name).lower()
            if any(h in nm for h in _SAFE_PLAY_HINTS):
                names.append(self.short_name(d, 28))
            if len(names) >= limit:
                break
        return names

    def _maybe_auto_select_cable(self) -> None:
        """Prefer headphones first, then virtual cable for PLAY when enabling EQ."""
        try:
            for hints in (_SAFE_PLAY_HINTS, _CABLE_HINTS):
                for i, d in enumerate(self.outputs):
                    name = safe_name(d.name).lower()
                    if any(h in name for h in hints):
                        if self._feedback_risk(self.input_dev, d):
                            continue
                        self.out_idx = i
                        self._cable_auto_done = True
                        self.status = f"Auto-selected play: {self.short_name(d, 40)}"
                        return
            self._cable_auto_done = True
        except Exception as exc:
            log_exception("auto_select_cable", exc)

    def _feedback_risk(self, idev: AudioDev, odev: AudioDev) -> bool:
        """True if playing EQ back into the same speakers we're loopback-capturing."""
        try:
            oname = safe_name(odev.name).lower()
            iname = safe_name(idev.name).lower()
            if any(h in oname for h in _SAFE_PLAY_HINTS):
                return False
            if any(
                k in oname
                for k in (
                    "primary sound",
                    "sound mapper",
                    "microsoft sound mapper",
                )
            ):
                return True
            def norm(s: str) -> str:
                for p in (
                    "default speakers  -  ",
                    "default  -  ",
                    "speakers: ",
                    "loopback  -  ",
                    "mix  -  ",
                ):
                    if s.startswith(p):
                        s = s[len(p) :]
                return _base_device_name(s)

            a, b = norm(iname), norm(oname)
            if a and b and (a in b or b in a):
                return True
            if "speaker" in oname and "speaker" in iname:
                return True
            if idev.index is not None and odev.index is not None and idev.index == odev.index:
                return True
        except Exception:
            pass
        return False

    def refresh_devices(self) -> None:
        try:
            cur_in = self.input_dev.name
            cur_out = self.output_dev.name
            self.inputs, self.outputs = list_audio_devices(probe=True)
            self.in_idx = next((i for i, d in enumerate(self.inputs) if d.name == cur_in), 0)
            self.out_idx = next((i for i, d in enumerate(self.outputs) if d.name == cur_out), -1)
            if self.out_idx < 0:
                self.out_idx = self._pick_default_play_idx()
            self.status = (
                f"Devices refreshed — {len(self.inputs)} capture / {len(self.outputs)} play "
                f"(Sound Mapper / Primary stubs hidden)"
            )
            self.restart()
        except Exception as exc:
            self.status = log_exception("refresh_devices", exc)
            self._start_idle_thread()

    def watchdog(self) -> None:
        """Auto-reconnect if the capture stream dies."""
        try:
            if self._starting:
                return
            # soundcard uses a thread, not a PortAudio InputStream
            if self.input_dev.kind in ("soundcard", "wasapi"):
                thr = getattr(self, "_thread", None)
                wr = getattr(self, "_wasapi_rec", None)
                alive_rec = True
                if wr is not None and getattr(wr, "error", ""):
                    alive_rec = False
                if self.mode in ("listen", "affect") and wr is None and thr is not None and not thr.is_alive() and self._alive:
                    self.status = "Loopback thread died — reconnecting…"
                    self.restart()
                elif self.mode in ("listen", "affect") and not alive_rec:
                    self.status = "WASAPI error — reconnecting…"
                    self.restart()
                return
            alive = False
            s = self._in_stream or self._stream
            if s is not None:
                try:
                    alive = bool(getattr(s, "active", False))
                except Exception:
                    alive = False
            if self.mode in ("listen", "affect") and not alive:
                self.status = "Stream dropped — reconnecting…"
                self.restart()
        except Exception as exc:
            log_exception("watchdog", exc)

    @staticmethod
    def _to_mono(indata, frames: int) -> np.ndarray:
        try:
            stereo = AudioEngine._to_stereo(indata, frames)
            return stereo.mean(axis=1).astype(np.float32, copy=False)
        except Exception:
            return np.zeros(max(1, frames), dtype=np.float32)

    @staticmethod
    def _to_stereo(indata, frames: int) -> np.ndarray:
        """Normalize capture buffer to float32 (N, 2)."""
        try:
            if indata is None:
                return np.zeros((max(1, frames), 2), dtype=np.float32)
            arr = np.asarray(indata, dtype=np.float32)
            if arr.ndim == 1:
                mono = arr.reshape(-1)
                if len(mono) < frames:
                    mono = np.pad(mono, (0, frames - len(mono)))
                elif len(mono) > frames:
                    mono = mono[:frames]
                return np.column_stack([mono, mono]).astype(np.float32, copy=False)
            if arr.shape[0] < frames:
                pad = np.zeros((frames - arr.shape[0], arr.shape[1]), dtype=np.float32)
                arr = np.vstack([arr, pad])
            elif arr.shape[0] > frames:
                arr = arr[:frames]
            if arr.shape[1] >= 2:
                return arr[:, :2].astype(np.float32, copy=False)
            mono = arr[:, 0]
            return np.column_stack([mono, mono]).astype(np.float32, copy=False)
        except Exception:
            return np.zeros((max(1, frames), 2), dtype=np.float32)

    def _device_rate(self, index: Optional[int], fallback: int = SAMPLE_RATE) -> int:
        if sd is None or index is None:
            return fallback
        return _dev_rate(index, fallback)

    def _matched_rate_candidates(self, idev: AudioDev, odev: AudioDev) -> List[int]:
        """Rates that should work for both capture and play; capture rate first."""
        rates: List[int] = []
        for r in (
            getattr(idev, "rate", 0),
            self._device_rate(idev.index),
            getattr(odev, "rate", 0),
            self._device_rate(odev.index),
            SAMPLE_RATE,
            48000,
            44100,
            96000,
        ):
            if r and int(r) not in rates:
                rates.append(int(r))
        return rates

    def _safe_callback(self, name: str, fn):
        def wrapped(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except Exception as exc:
                log_exception(f"audio_cb:{name}", exc)
                try:
                    if name == "play" and args:
                        args[0][:] = 0
                    elif len(args) >= 2 and hasattr(args[1], "__setitem__"):
                        args[1][:] = 0
                except Exception:
                    pass

        return wrapped

    def _open_input_stream(self, idev: AudioDev, callback, force_rate: Optional[int] = None) -> object:
        if sd is None:
            raise RuntimeError("sounddevice missing")
        if idev.index is None:
            raise RuntimeError("no capture device")
        rates: List[int] = []
        preferred = force_rate if force_rate else getattr(self, "_force_rate", None)
        for r in (preferred, getattr(idev, "rate", 0), self._device_rate(idev.index), SAMPLE_RATE, 48000, 44100):
            if r and int(r) not in rates:
                rates.append(int(r))
        last_err = None
        safe_cb = self._safe_callback("capture", callback)
        for rate in rates:
            for ch in (idev.channels, 2, 1):
                if ch <= 0:
                    continue
                for block in (0, BLOCK, 1024, 512):
                    kwargs = dict(
                        samplerate=rate,
                        blocksize=block,
                        device=idev.index,
                        channels=ch,
                        dtype="float32",
                        latency="high",
                        callback=safe_cb,
                    )
                    if idev.extra is not None:
                        kwargs["extra_settings"] = idev.extra
                    try:
                        stream = sd.InputStream(**kwargs)
                        stream.start()
                        idev.channels = ch
                        idev.rate = rate
                        return stream
                    except Exception as exc:
                        last_err = exc
                        continue
        raise RuntimeError(str(last_err) if last_err else "could not open capture")

    def _open_output_stream(self, odev: AudioDev, callback, force_rate: Optional[int] = None) -> object:
        if sd is None:
            raise RuntimeError("sounddevice missing")
        if odev.index is None:
            raise RuntimeError("no play device")
        rates: List[int] = []
        preferred = force_rate if force_rate else getattr(self, "_force_rate", None)
        for r in (preferred, getattr(odev, "rate", 0), self._device_rate(odev.index), SAMPLE_RATE, 48000, 44100):
            if r and int(r) not in rates:
                rates.append(int(r))
        last_err = None
        safe_cb = self._safe_callback("play", callback)
        for rate in rates:
            for ch in (odev.channels, 2, 1):
                for block in (0, BLOCK, 1024, 512):
                    try:
                        stream = sd.OutputStream(
                            samplerate=rate,
                            blocksize=block,
                            device=odev.index,
                            channels=ch,
                            dtype="float32",
                            latency="high",
                            callback=safe_cb,
                        )
                        stream.start()
                        odev.channels = ch
                        odev.rate = rate
                        return stream
                    except Exception as exc:
                        last_err = exc
                        continue
        raise RuntimeError(str(last_err) if last_err else "could not open play device")

    def _write_out(self, outdata, processed: np.ndarray) -> None:
        try:
            if outdata is None:
                return
            if self.muted or not self.affect_sound:
                outdata[:] = 0
                return
            arr = np.asarray(processed, dtype=np.float32)
            n = outdata.shape[0] if hasattr(outdata, "shape") else len(outdata)
            if arr.ndim == 1:
                if len(arr) < n:
                    arr = np.pad(arr, (0, n - len(arr)))
                elif len(arr) > n:
                    arr = arr[:n]
                stereo = np.column_stack([arr, arr])
            else:
                if arr.shape[0] < n:
                    pad = np.zeros((n - arr.shape[0], arr.shape[1]), dtype=np.float32)
                    arr = np.vstack([arr, pad])
                elif arr.shape[0] > n:
                    arr = arr[:n]
                if arr.shape[1] >= 2:
                    stereo = arr[:, :2]
                else:
                    stereo = np.column_stack([arr[:, 0], arr[:, 0]])
            if outdata.ndim == 1 or (outdata.ndim > 1 and outdata.shape[1] == 1):
                mono = stereo.mean(axis=1)
                if outdata.ndim == 1:
                    outdata[:] = mono
                else:
                    outdata[:, 0] = mono
            else:
                outdata[:, 0] = stereo[:, 0]
                outdata[:, 1] = stereo[:, 1]
                for c in range(2, outdata.shape[1]):
                    outdata[:, c] = stereo[:, c % 2]
        except Exception as exc:
            log_exception("write_out", exc)
            try:
                outdata[:] = 0
            except Exception:
                pass

    def _decay_levels(self, stereo: np.ndarray) -> None:
        try:
            if stereo.ndim == 1:
                peak_l = float(np.max(np.abs(stereo))) if stereo.size else 0.0
                peak_r = peak_l
            else:
                peak_l = float(np.max(np.abs(stereo[:, 0]))) if stereo.shape[0] else 0.0
                peak_r = float(np.max(np.abs(stereo[:, 1]))) if stereo.shape[1] > 1 and stereo.shape[0] else peak_l
            self.level_l = max(peak_l, self.level_l * 0.88)
            self.level_r = max(peak_r, self.level_r * 0.88)
        except Exception:
            self.level_l *= 0.88
            self.level_r *= 0.88

    def _push_audio(self, mono: np.ndarray) -> np.ndarray:
        """Legacy mono helper — wraps stereo path."""
        stereo = np.column_stack([mono, mono]).astype(np.float32)
        out = self._push_stereo(stereo)
        return out.mean(axis=1).astype(np.float32)

    def _push_stereo(self, stereo: np.ndarray) -> np.ndarray:
        """EQ stereo (when EQ ON), update meters, push mono mix to spectrum."""
        self._last_audio_t = time.time()
        arr = np.asarray(stereo, dtype=np.float32)
        if arr.ndim == 1:
            arr = np.column_stack([arr, arr])
        # Sanitize non-finite samples (WASAPI glitches)
        if not np.isfinite(arr).all():
            arr = np.nan_to_num(arr, nan=0.0, posinf=0.0, neginf=0.0)
        peak = float(np.max(np.abs(arr))) if arr.size else 0.0
        # Listen-only: don't process; soft silence for viz only
        if not self.affect_sound:
            if peak < 0.0012:
                z = np.zeros_like(arr)
                self.spectrum.push(np.zeros(arr.shape[0], dtype=np.float32))
                self._decay_levels(z)
                return z
            try:
                self.spectrum.push(arr.mean(axis=1).astype(np.float32))
            except Exception:
                pass
            self._decay_levels(arr)
            return arr
        # EQ ON: never gate to zero (kills quiet music / creates pumping)
        try:
            processed = self.eq.process_block(arr)
        except Exception:
            processed = arr
        try:
            mix = arr.mean(axis=1) if self.show_pre_eq else processed.mean(axis=1)
            self.spectrum.push(mix.astype(np.float32))
        except Exception:
            pass
        self._decay_levels(processed)
        return processed

    def _on_input_only(self, indata, frames, time_info, status) -> None:  # noqa: ARG002
        stereo = self._to_stereo(indata, frames)
        self._push_stereo(stereo)

    def stop(self) -> None:
        self._alive = False
        try:
            wr = getattr(self, "_wasapi_rec", None)
            if wr is not None:
                wr.stop()
            self._wasapi_rec = None
        except Exception:
            pass
        with self._lock:
            for attr in ("_out_stream", "_in_stream", "_stream"):
                s = getattr(self, attr, None)
                if s is not None:
                    try:
                        s.stop()
                    except Exception:
                        pass
                    try:
                        s.close()
                    except Exception:
                        pass
                    setattr(self, attr, None)
        self.mode = "idle"

    def start(self) -> None:
        if self._starting:
            return
        self._starting = True
        try:
            self._start_inner()
        except Exception as exc:
            self.status = log_exception("start", exc)
            try:
                self._start_idle_thread()
            except Exception:
                pass
        finally:
            self._starting = False
            self._fallback_depth = 0
            try:
                self._refresh_play_safety_tip()
            except Exception:
                pass

    def _start_wasapi_listen(self, idev: AudioDev) -> None:
        """Microsoft WASAPI loopback on a render endpoint → spectrum."""
        if WasapiLoopbackRecorder is None:
            raise RuntimeError("WASAPI not available")
        ep = idev.extra
        mic = getattr(ep, "mic", None) if ep is not None else None
        rate = int(getattr(idev, "rate", 48000) or 48000)
        ch = max(1, int(getattr(idev, "channels", 2) or 2))
        try:
            self.eq.set_sample_rate(rate)
        except Exception:
            pass
        try:
            self.spectrum.set_sample_rate(rate)
        except Exception:
            pass
        self._alive = True
        self.mode = "listen"
        # Prefer soundcard when a mic handle exists; ctypes only when needed.
        # (Forcing ctypes-only in the EXE caused CoCreateInstance 0x80040154 on some PCs.)
        use_ctypes = mic is None
        backend = "ctypes" if use_ctypes else "soundcard"
        self.status = (
            f"Live WASAPI ({backend}) «{self.short_name(idev)}». "
            "Play music — mesh moves. EQ ON shapes playback once PLAY is Headphones or VB-Cable (not Speakers/Sound Mapper)."
        )

        def on_audio(buf: np.ndarray) -> None:
            if not getattr(self, "_alive", False):
                return
            stereo = self._to_stereo(buf, buf.shape[0])
            self._push_stereo(stereo)

        def _open_rec(force_ct: bool, mic_obj) -> "WasapiLoopbackRecorder":
            r = WasapiLoopbackRecorder(
                mic=None if force_ct else mic_obj,
                device_id=getattr(ep, "device_id", None) or None,
                blocksize=BLOCK,
                samplerate=rate,
                channels=ch,
                on_audio=on_audio,
                force_ctypes=force_ct,
            )
            r.start()
            t0 = time.time()
            while time.time() - t0 < 0.45:
                if r.error:
                    break
                time.sleep(0.05)
            return r

        rec = _open_rec(use_ctypes, mic)
        if rec.error and mic is not None and not use_ctypes:
            try:
                rec.stop()
            except Exception:
                pass
            rec = _open_rec(True, None)
        if rec.error:
            raise RuntimeError(rec.error)
        self._wasapi_rec = rec

        def watch() -> None:
            while getattr(self, "_alive", False):
                if rec.error:
                    self.status = f"WASAPI error: {rec.error}"[:160]
                    break
                time.sleep(0.25)

        self._thread = threading.Thread(target=watch, daemon=True, name="wasapi-watch")
        self._thread.start()

    def _start_wasapi_affect(self, idev: AudioDev, odev: AudioDev) -> None:
        """WASAPI loopback capture → EQ → sounddevice play."""
        if WasapiLoopbackRecorder is None:
            raise RuntimeError("WASAPI not available")
        if sd is None or odev.index is None:
            raise RuntimeError("need sounddevice output for EQ ON")
        ep = idev.extra
        mic = getattr(ep, "mic", None) if ep is not None else None
        use_ctypes = mic is None
        rate = int(getattr(idev, "rate", 48000) or 48000)
        ch = max(1, int(getattr(idev, "channels", 2) or 2))
        self._q = deque(maxlen=4)
        self._q_lock = threading.Lock()
        self._alive = True

        def on_play(outdata, frames, time_info, status):  # noqa: ARG001
            with self._q_lock:
                stereo = (
                    self._q.popleft()
                    if self._q
                    else np.zeros((frames, 2), dtype=np.float32)
                )
            if stereo.shape[0] < frames:
                pad = np.zeros((frames - stereo.shape[0], 2), dtype=np.float32)
                stereo = np.vstack([stereo, pad])
            elif stereo.shape[0] > frames:
                stereo = stereo[:frames]
            processed = self._push_stereo(stereo)
            self._write_out(outdata, processed)

        self._out_stream = self._open_output_stream(odev, on_play, force_rate=rate)
        try:
            self.eq.set_sample_rate(rate)
        except Exception:
            pass
        try:
            self.spectrum.set_sample_rate(rate)
        except Exception:
            pass
        lim = "limiter ON" if self.eq.limiter_on else "limiter OFF"
        byp = "  -  BYPASS" if self.eq.bypass else ""
        self.mode = "affect"
        self.status = f"EQ ON — WASAPI loopback  -  {lim}{byp}  -  {rate} Hz"

        def on_audio(buf: np.ndarray) -> None:
            if not getattr(self, "_alive", False):
                return
            stereo = self._to_stereo(buf, buf.shape[0])
            with self._q_lock:
                self._q.append(stereo.copy())

        def _open_rec(force_ct: bool, mic_obj) -> "WasapiLoopbackRecorder":
            r = WasapiLoopbackRecorder(
                mic=None if force_ct else mic_obj,
                device_id=getattr(ep, "device_id", None) or None,
                blocksize=BLOCK,
                samplerate=rate,
                channels=ch,
                on_audio=on_audio,
                force_ctypes=force_ct,
            )
            r.start()
            t0 = time.time()
            while time.time() - t0 < 0.45:
                if r.error:
                    break
                time.sleep(0.05)
            return r

        rec = _open_rec(use_ctypes, mic)
        if rec.error and mic is not None and not use_ctypes:
            try:
                rec.stop()
            except Exception:
                pass
            rec = _open_rec(True, None)
        if rec.error:
            raise RuntimeError(rec.error)
        self._wasapi_rec = rec

    def _start_soundcard_listen(self, idev: AudioDev) -> None:
        """Capture speakers via soundcard WASAPI loopback → spectrum (listen / EQ OFF)."""
        mic = idev.extra
        if mic is None:
            raise RuntimeError("soundcard mic missing")
        rate = int(getattr(idev, "rate", 48000) or 48000)
        ch = max(1, int(getattr(idev, "channels", 2) or 2))
        try:
            self.eq.set_sample_rate(rate)
        except Exception:
            pass
        try:
            self.spectrum.set_sample_rate(rate)
        except Exception:
            pass
        self._alive = True
        self.mode = "listen"
        self.status = (
            f"Live on «{self.short_name(idev)}» (WASAPI loopback). "
            "Play music — mesh moves. EQ ON shapes playback once PLAY is Headphones or VB-Cable (not Speakers/Sound Mapper)."
        )

        def pump() -> None:
            try:
                with mic.recorder(samplerate=rate, channels=ch, blocksize=BLOCK) as rec:
                    while getattr(self, "_alive", False):
                        try:
                            data = rec.record(numframes=BLOCK)
                            stereo = self._to_stereo(data, BLOCK)
                            self._push_stereo(stereo)
                        except Exception as exc:
                            log_exception("soundcard.record", exc)
                            time.sleep(0.05)
            except Exception as exc:
                log_exception("soundcard.recorder", exc)
                self.status = f"Capture failed: {exc}"[:160]
                try:
                    self._start_idle_thread()
                except Exception:
                    pass

        self._thread = threading.Thread(target=pump, daemon=True, name="spectra-sc-listen")
        self._thread.start()

    def _start_soundcard_affect(self, idev: AudioDev, odev: AudioDev) -> None:
        """soundcard capture → EQ → sounddevice playback."""
        if sd is None or odev.index is None:
            raise RuntimeError("need sounddevice output for EQ ON")
        mic = idev.extra
        if mic is None:
            raise RuntimeError("soundcard mic missing")
        rate = int(getattr(idev, "rate", 48000) or 48000)
        ch = max(1, int(getattr(idev, "channels", 2) or 2))
        self._q = deque(maxlen=4)
        self._q_lock = threading.Lock()
        self._alive = True

        def on_play(outdata, frames, time_info, status):  # noqa: ARG001
            with self._q_lock:
                stereo = (
                    self._q.popleft()
                    if self._q
                    else np.zeros((frames, 2), dtype=np.float32)
                )
            if stereo.shape[0] < frames:
                pad = np.zeros((frames - stereo.shape[0], 2), dtype=np.float32)
                stereo = np.vstack([stereo, pad])
            elif stereo.shape[0] > frames:
                stereo = stereo[:frames]
            processed = self._push_stereo(stereo)
            self._write_out(outdata, processed)

        self._out_stream = self._open_output_stream(odev, on_play, force_rate=rate)
        try:
            self.eq.set_sample_rate(rate)
        except Exception:
            pass
        try:
            self.spectrum.set_sample_rate(rate)
        except Exception:
            pass
        self.mode = "affect"
        lim = "limiter ON" if self.eq.limiter_on else "limiter OFF"
        byp = "  -  BYPASS" if self.eq.bypass else ""
        self.status = f"EQ ON — WASAPI loopback  -  {lim}{byp}  -  {rate} Hz"

        def pump() -> None:
            try:
                with mic.recorder(samplerate=rate, channels=ch, blocksize=BLOCK) as rec:
                    while getattr(self, "_alive", False):
                        try:
                            data = rec.record(numframes=BLOCK)
                            stereo = self._to_stereo(data, BLOCK)
                            with self._q_lock:
                                self._q.append(stereo.copy())
                        except Exception as exc:
                            log_exception("soundcard.affect.record", exc)
                            time.sleep(0.05)
            except Exception as exc:
                log_exception("soundcard.affect.recorder", exc)
                self.status = f"EQ capture failed: {exc}"[:160]

        self._thread = threading.Thread(target=pump, daemon=True, name="spectra-sc-affect")
        self._thread.start()

    def _start_inner(self) -> None:
        self.stop()
        try:
            wr = getattr(self, "_wasapi_rec", None)
            if wr is not None:
                wr.stop()
            self._wasapi_rec = None
        except Exception:
            pass

        idev = self.input_dev
        odev = self.output_dev
        # System EQ is applied inside Windows. Never open a second playback stream for it.
        play_eq = bool(self.affect_sound and not self._system_eq)

        # Microsoft WASAPI loopback (render endpoint + LOOPBACK flag)
        # https://learn.microsoft.com/en-us/windows/win32/coreaudio/loopback-recording
        if idev.kind == "wasapi" and idev.extra is not None:
            try:
                if play_eq and sd is not None and odev.index is not None:
                    if self._feedback_risk(idev, odev) or self._is_bogus_play_name(odev.name):
                        self._start_wasapi_listen(idev)
                    else:
                        self._start_wasapi_affect(idev, odev)
                else:
                    self._start_wasapi_listen(idev)
                return
            except Exception as exc:
                log_exception("wasapi.start", exc)
                self.status = f"WASAPI loopback failed: {exc}"[:160]
                self._start_idle_thread()
                return

        # soundcard native WASAPI loopback (index is None by design)
        if idev.kind == "soundcard" and idev.extra is not None:
            try:
                if play_eq and sd is not None and odev.index is not None:
                    if self._feedback_risk(idev, odev) or self._is_bogus_play_name(odev.name):
                        self._start_soundcard_listen(idev)
                    else:
                        self._start_soundcard_affect(idev, odev)
                else:
                    self._start_soundcard_listen(idev)
                return
            except Exception as exc:
                log_exception("soundcard.start", exc)
                self.status = f"WASAPI loopback failed: {exc}"[:160]
                self._start_idle_thread()
                return

        if sd is None:
            self.status = "No sounddevice — idle"
            self._start_idle_thread()
            return

        if idev.index is None and idev.kind not in ("soundcard", "wasapi"):
            if not WASAPI_NATIVE:
                self.status = "No capture — run on Windows for WASAPI speaker loopback"
            else:
                self.status = "No capture device — click Refresh, or enable Stereo Mix in Windows"
            self._start_idle_thread()
            return

        try:
            if play_eq and odev.index is not None:
                self._q = deque(maxlen=4)
                self._q_lock = threading.Lock()

                def on_cap(indata, frames, time_info, status):  # noqa: ARG001
                    stereo = self._to_stereo(indata, frames)
                    with self._q_lock:
                        self._q.append(stereo.copy())

                def on_play(outdata, frames, time_info, status):  # noqa: ARG001
                    with self._q_lock:
                        stereo = (
                            self._q.popleft()
                            if self._q
                            else np.zeros((frames, 2), dtype=np.float32)
                        )
                    if stereo.shape[0] < frames:
                        pad = np.zeros((frames - stereo.shape[0], 2), dtype=np.float32)
                        stereo = np.vstack([stereo, pad])
                    elif stereo.shape[0] > frames:
                        stereo = stereo[:frames]
                    processed = self._push_stereo(stereo)
                    self._write_out(outdata, processed)

                opened = False
                last_pair_err: Optional[BaseException] = None
                for rate in self._matched_rate_candidates(idev, odev):
                    self._force_rate = rate
                    try:
                        try:
                            if self._in_stream:
                                self._in_stream.stop()
                                self._in_stream.close()
                        except Exception:
                            pass
                        try:
                            if self._out_stream:
                                self._out_stream.stop()
                                self._out_stream.close()
                        except Exception:
                            pass
                        self._in_stream = None
                        self._out_stream = None
                        self._in_stream = self._open_input_stream(idev, on_cap, force_rate=rate)
                        self._out_stream = self._open_output_stream(odev, on_play, force_rate=rate)
                        matched = int(getattr(idev, "rate", rate) or rate)
                        try:
                            self.eq.set_sample_rate(matched)
                        except Exception:
                            pass
                        opened = True
                        break
                    except Exception as pair_exc:
                        last_pair_err = pair_exc
                        continue
                self._force_rate = None

                if not opened:
                    log_exception("open_output", last_pair_err or RuntimeError("matched open failed"))
                    try:
                        if self._in_stream:
                            self._in_stream.stop()
                            self._in_stream.close()
                    except Exception:
                        pass
                    try:
                        if self._out_stream:
                            self._out_stream.stop()
                            self._out_stream.close()
                    except Exception:
                        pass
                    self.affect_sound = False
                    self._stream = self._open_input_stream(idev, self._on_input_only)
                    self._in_stream = self._stream
                    self._out_stream = None
                    self.mode = "listen"
                    self.status = f"EQ ON failed — listening only: {last_pair_err}"
                    return

                self._stream = self._in_stream
                self.mode = "affect"
                same = idev.index == odev.index and idev.kind == "loopback"
                lim = "limiter ON" if self.eq.limiter_on else "limiter OFF"
                byp = "  -  BYPASS" if self.eq.bypass else ""
                self.status = f"EQ ON — stereo  -  {lim}{byp}  -  {int(getattr(idev, 'rate', SAMPLE_RATE))} Hz"
                if same:
                    self.status += "  -  TIP: route apps through VB-Cable to avoid echo"
            else:
                self._force_rate = None
                self._stream = self._open_input_stream(idev, self._on_input_only)
                self._in_stream = self._stream
                self._out_stream = None
                try:
                    self.eq.set_sample_rate(getattr(idev, "rate", SAMPLE_RATE) or SAMPLE_RATE)
                except Exception:
                    pass
                self.mode = "listen"
                self.status = (
                    f"Live on «{self.short_name(idev)}» (WASAPI/mix). "
                    "Play music — mesh moves. EQ ON shapes playback."
                )
        except Exception as exc:
            err = str(exc)
            if self._fallback_depth < 1 and len(self.inputs) > 1:
                nxt = (self.in_idx + 1) % len(self.inputs)
                if nxt != self.in_idx:
                    self.in_idx = nxt
                    self._fallback_depth += 1
                    self.status = "Trying next capture device…"
                    self._start_inner()
                    return
            self.status = f"Capture failed: {err}"[:160]
            print(self.status)
            log_exception("device_open", exc)
            self._stream = None
            self._in_stream = None
            self._out_stream = None
            self._start_idle_thread()

    def restart(self) -> None:
        self.start()

    def _start_idle_thread(self) -> None:
        self.mode = "idle"
        self._alive = True

        def pump() -> None:
            z = np.zeros(BLOCK, dtype=np.float32)
            while getattr(self, "_alive", True):
                try:
                    self.spectrum.push(z)
                except Exception:
                    pass
                time.sleep(BLOCK / float(SAMPLE_RATE))

        try:
            self._thread = threading.Thread(target=pump, daemon=True, name="spectra-idle")
            self._thread.start()
        except Exception as exc:
            log_exception("idle_thread", exc)


# ─── Settings persistence ────────────────────────────────────────────────────
def load_eq_config() -> Dict[str, Any]:
    try:
        if not os.path.isfile(CONFIG_PATH):
            return {}
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        log_exception("load_eq_config", exc)
        return {}


def save_eq_config(data: Dict[str, Any]) -> None:
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as exc:
        log_exception("save_eq_config", exc)


# ─── UI helpers ──────────────────────────────────────────────────────────────
def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def hsv(h: float, s: float, v: float) -> Tuple[int, int, int]:
    i = int(h * 6)
    f = h * 6 - i
    p, q, t = v * (1 - s), v * (1 - f * s), v * (1 - (1 - f) * s)
    i %= 6
    r, g, b = [(v, t, p), (q, v, p), (p, v, t), (p, q, v), (t, p, v), (v, p, q)][i]
    return int(r * 255), int(g * 255), int(b * 255)


def project(x: float, y: float, z: float, w: int, h: int, t: float) -> Tuple[int, int]:
    """Fake 3D perspective for the spectrum mesh."""
    # x: freq 0..1, z: time 0..1, y: amplitude
    sway = math.sin(t * 0.4) * 0.04
    # rotate slightly in Y
    xr = (x - 0.5) * math.cos(0.35 + sway) + (z - 0.5) * math.sin(0.35 + sway)
    zr = -(x - 0.5) * math.sin(0.35 + sway) + (z - 0.5) * math.cos(0.35 + sway)
    depth = 1.55 + zr * 0.9
    px = w * 0.5 + xr * w * 0.78 / depth
    py = h * 0.72 - y * h * 0.55 / depth - zr * h * 0.22
    return int(px), int(py)


class Knob:
    """Rotary gain control plus a wide slider underneath. Up or right = louder."""

    def __init__(
        self,
        cx: int,
        cy: int,
        radius: int,
        value: float,
        vmin: float,
        vmax: float,
        label: str,
        accent: Tuple[int, int, int] = (0, 220, 190),
        slider_w: int = 64,
    ) -> None:
        self.cx = int(cx)
        self.cy = int(cy)
        self.radius = int(radius)
        self.value = float(value)
        self.vmin = float(vmin)
        self.vmax = float(vmax)
        self.label = label
        self.accent = accent
        self.dragging = False
        self.hot = False
        self._mode = ""
        self._last_y: Optional[int] = None
        sw = max(56, int(slider_w))
        sh = 22
        sx = self.cx - sw // 2
        sy = self.cy + self.radius + 30
        self.slider = pygame.Rect(sx, sy, sw, sh)
        self.reset = pygame.Rect(sx, self.slider.bottom + 6, sw, 22)
        self.reset_hot = False
        pad = max(radius + 18, sw // 2 + 6)
        top = self.cy - radius - 22
        self.rect = pygame.Rect(self.cx - pad, top, pad * 2, self.reset.bottom - top + 4)

    def _clamp(self) -> None:
        self.value = max(self.vmin, min(self.vmax, self.value))

    def set_from_slider_x(self, x: int) -> None:
        t = (x - self.slider.x) / max(1, self.slider.w)
        t = max(0.0, min(1.0, t))
        self.value = self.vmin + t * (self.vmax - self.vmin)
        self._clamp()

    def nudge(self, delta: float) -> None:
        self.value += delta
        self._clamp()

    def hit_reset(self, pos: Tuple[int, int]) -> bool:
        return self.reset.collidepoint(pos)

    def hit_slider(self, pos: Tuple[int, int]) -> bool:
        grab = self.slider.inflate(12, 16)
        return grab.collidepoint(pos)

    def hit(self, pos: Tuple[int, int]) -> bool:
        if self.hit_reset(pos):
            return False
        if self.hit_slider(pos):
            return True
        dx = pos[0] - self.cx
        dy = pos[1] - self.cy
        return (dx * dx + dy * dy) <= (self.radius + 18) ** 2

    def begin_drag(self, pos: Tuple[int, int]) -> None:
        self.dragging = True
        self._last_y = pos[1]
        if self.hit_slider(pos):
            self._mode = "slider"
            self.set_from_slider_x(pos[0])
        else:
            self._mode = "knob"
            # Short vertical travel covers the whole range (up = louder).
            top = self.cy - self.radius - 8
            bot = self.cy + self.radius + 8
            t = (pos[1] - top) / max(1, bot - top)
            t = max(0.0, min(1.0, t))
            self.value = self.vmax - t * (self.vmax - self.vmin)
            self._clamp()

    def drag_to(self, pos: Tuple[int, int]) -> None:
        if self._mode == "slider":
            self.set_from_slider_x(pos[0])
            return
        span = self.vmax - self.vmin
        if self._last_y is not None:
            dy = self._last_y - pos[1]
            self.value += dy * (span / 56.0)
            self._last_y = pos[1]
        self._clamp()

    def end_drag(self) -> None:
        self.dragging = False
        self._mode = ""
        self._last_y = None

    def needle_angle(self) -> float:
        """Radians: 135° (CCW from +x) across a 270° sweep."""
        t = (self.value - self.vmin) / max(1e-9, (self.vmax - self.vmin))
        t = max(0.0, min(1.0, t))
        start = math.radians(135)
        sweep = math.radians(270)
        return start + t * sweep

    def draw(self, surf: pygame.Surface, font: pygame.font.Font, font_sm: pygame.font.Font) -> None:
        r = self.radius
        card = self.rect.inflate(-4, -2)
        card_bg = pygame.Surface((card.w, card.h), pygame.SRCALPHA)
        card_bg.fill((18, 28, 42, 210) if (self.hot or self.dragging) else (12, 18, 28, 160))
        surf.blit(card_bg, card.topleft)
        edge = self.accent if abs(self.value) > 0.4 else (40, 64, 88)
        pygame.draw.rect(surf, edge, card, 1, border_radius=12)

        pygame.draw.circle(surf, (8, 12, 20), (self.cx, self.cy), r)
        pygame.draw.circle(surf, (28, 40, 56), (self.cx, self.cy), r - 3)
        ring = self.accent if (self.dragging or self.hot or abs(self.value) > 0.4) else (55, 80, 105)
        pygame.draw.circle(surf, ring, (self.cx, self.cy), r, 2)
        arc = pygame.Rect(self.cx - r + 5, self.cy - r + 5, (r - 5) * 2, (r - 5) * 2)
        try:
            pygame.draw.arc(surf, (32, 48, 64), arc, math.radians(135), math.radians(405), 3)
            t = (self.value - self.vmin) / max(1e-9, (self.vmax - self.vmin))
            t = max(0.0, min(1.0, t))
            if t > 0.01:
                pygame.draw.arc(
                    surf,
                    self.accent,
                    arc,
                    math.radians(135),
                    math.radians(135) + t * math.radians(270),
                    4,
                )
        except Exception:
            t = (self.value - self.vmin) / max(1e-9, (self.vmax - self.vmin))
            t = max(0.0, min(1.0, t))
        ang = self.needle_angle()
        nx = self.cx + int(math.cos(ang) * (r - 8))
        ny = self.cy + int(math.sin(ang) * (r - 8))
        pygame.draw.line(surf, (245, 250, 255), (self.cx, self.cy), (nx, ny), 2)
        pygame.draw.circle(surf, self.accent, (self.cx, self.cy), 5)

        db = font_sm.render(f"{self.value:+.0f}", True, (245, 250, 255))
        surf.blit(db, (self.cx - db.get_width() // 2, self.cy - r - 18))
        lab = font_sm.render(self.label, True, (170, 210, 225))
        surf.blit(lab, (self.cx - lab.get_width() // 2, self.cy + r + 6))

        track = self.slider
        pygame.draw.rect(surf, (8, 12, 18), track, border_radius=11)
        pygame.draw.rect(surf, (60, 100, 130), track, 1, border_radius=11)
        fill_w = max(10, int(track.w * t))
        pygame.draw.rect(surf, self.accent, pygame.Rect(track.x, track.y, fill_w, track.h), border_radius=11)
        if self.vmin < 0 < self.vmax:
            z = (0.0 - self.vmin) / (self.vmax - self.vmin)
            zx = track.x + int(track.w * z)
            pygame.draw.line(surf, (255, 255, 255), (zx, track.y + 3), (zx, track.bottom - 3), 2)
        thumb_x = track.x + int((track.w - 1) * t)
        pygame.draw.circle(surf, (255, 255, 255), (thumb_x, track.centery), 9)
        pygame.draw.circle(surf, self.accent, (thumb_x, track.centery), 9, 2)

        rbg = (48, 78, 98) if self.reset_hot else (24, 38, 52)
        pygame.draw.rect(surf, rbg, self.reset, border_radius=6)
        pygame.draw.rect(surf, (90, 150, 170), self.reset, 1, border_radius=6)
        rst = font_sm.render("Reset", True, (230, 244, 250))
        surf.blit(rst, (self.reset.centerx - rst.get_width() // 2, self.reset.centery - rst.get_height() // 2))


class Button:
    def __init__(self, rect: pygame.Rect, label: str, kind: str = "preset"):
        self.rect = rect
        self.label = label
        self.kind = kind
        self.hot = False


class Dropdown:
    """Clickable device picker — closed bar + open scrollable list (full names)."""

    ITEM_H = 44
    MAX_VIS = 10

    def __init__(self, rect: pygame.Rect, label: str, items: List[str], selected: int = 0):
        self.rect = rect
        self.label = label
        self.items = items
        self.selected = max(0, min(selected, max(0, len(items) - 1)))
        self.open = False
        self.hot = False
        self.hover = -1
        self.scroll = 0

    def set_items(self, items: List[str], selected: int) -> None:
        self.items = items
        self.selected = max(0, min(selected, max(0, len(items) - 1)))
        self.scroll = 0

    def menu_height(self) -> int:
        n = min(len(self.items), self.MAX_VIS)
        return n * self.ITEM_H + 4

    def menu_rect(self) -> pygame.Rect:
        h = self.menu_height()
        y = self.rect.bottom + 2
        # Expand menu wider than the closed bar so full device names fit
        w = max(self.rect.w, min(720, self.rect.w + 220))
        x = self.rect.x
        return pygame.Rect(x, y, w, h)

    def visible_slice(self) -> Tuple[int, int]:
        n = len(self.items)
        if n <= self.MAX_VIS:
            return 0, n
        start = max(0, min(self.scroll, n - self.MAX_VIS))
        return start, start + self.MAX_VIS

    def index_at(self, pos: Tuple[int, int]) -> int:
        if not self.open:
            return -1
        mr = self.menu_rect()
        if not mr.collidepoint(pos):
            return -1
        start, _ = self.visible_slice()
        rel = pos[1] - mr.y - 2
        i = start + rel // self.ITEM_H
        if 0 <= i < len(self.items):
            return int(i)
        return -1

    def handle_click(self, pos: Tuple[int, int]) -> Optional[int]:
        """Returns new selected index if picked, -1 if toggled closed/open, None if ignored."""
        if self.open:
            i = self.index_at(pos)
            if i >= 0:
                self.selected = i
                self.open = False
                return i
            if self.rect.collidepoint(pos):
                self.open = False
                return -1
            self.open = False
            return -1
        if self.rect.collidepoint(pos):
            self.open = True
            # keep selection visible
            if self.selected >= self.MAX_VIS:
                self.scroll = self.selected - self.MAX_VIS // 2
            return -1
        return None

    def handle_wheel(self, pos: Tuple[int, int], dy: int) -> bool:
        if not self.open or not self.menu_rect().collidepoint(pos):
            return False
        n = len(self.items)
        if n <= self.MAX_VIS:
            return True
        self.scroll = max(0, min(n - self.MAX_VIS, self.scroll - dy))
        return True

    def draw(self, surf: pygame.Surface, font: pygame.font.Font, font_sm: pygame.font.Font) -> None:
        bg = (36, 52, 78) if self.hot or self.open else (22, 32, 48)
        border = (0, 220, 255) if self.open else (70, 120, 160)
        pygame.draw.rect(surf, bg, self.rect, border_radius=6)
        pygame.draw.rect(surf, border, self.rect, 2, border_radius=6)
        tag = font_sm.render(self.label, True, (120, 210, 255))
        surf.blit(tag, (self.rect.x + 10, self.rect.y + 3))
        cur = self.items[self.selected] if self.items else "(none)"
        max_w = self.rect.w - 36
        lines = wrap_text(font_sm, cur, max_w)[:2]
        y = self.rect.y + 20
        for line in lines:
            txt = font_sm.render(line, True, (240, 250, 255))
            surf.blit(txt, (self.rect.x + 10, y))
            y += font_sm.get_height()
        # chevron
        cx = self.rect.right - 18
        cy = self.rect.centery
        pts = [(cx - 5, cy - 3), (cx + 5, cy - 3), (cx, cy + 5)] if not self.open else [
            (cx - 5, cy + 3),
            (cx + 5, cy + 3),
            (cx, cy - 5),
        ]
        pygame.draw.polygon(surf, (180, 230, 255), pts)

    def draw_menu(self, surf: pygame.Surface, font: pygame.font.Font) -> None:
        if not self.open or not self.items:
            return
        mr = self.menu_rect()
        overlay = pygame.Surface((mr.w, mr.h), pygame.SRCALPHA)
        overlay.fill((8, 14, 24, 245))
        surf.blit(overlay, mr.topleft)
        pygame.draw.rect(surf, (0, 200, 230), mr, 2, border_radius=4)
        start, end = self.visible_slice()
        for i in range(start, end):
            y = mr.y + 2 + (i - start) * self.ITEM_H
            r = pygame.Rect(mr.x + 2, y, mr.w - 4, self.ITEM_H - 2)
            if i == self.selected:
                pygame.draw.rect(surf, (0, 90, 110), r, border_radius=4)
            elif i == self.hover:
                pygame.draw.rect(surf, (40, 60, 90), r, border_radius=4)
            name = self.items[i]
            lines = wrap_text(font, name, r.w - 16)[:2]
            ty = r.y + 4
            col = (255, 255, 255) if i == self.selected else (210, 230, 240)
            for line in lines:
                t = font.render(line, True, col)
                surf.blit(t, (r.x + 8, ty))
                ty += font.get_height() - 1


# ─── Main App ────────────────────────────────────────────────────────────────
class SpectraEQ:
    def __init__(self, splash: Optional["Splash"] = None) -> None:
        if not pygame.get_init():
            pygame.init()
        self.splash = splash
        self.viz_style = 0
        self.w, self.h = 1360, 760
        self.viz_h = 340
        self._sizing = False
        self.clock = pygame.time.Clock()
        self.font = self._font(21)
        self.font_sm = self._font(17)
        self.font_lg = self._font(38, bold=True)
        self.font_tiny = self._font(15)
        self._viz_prev: Optional[pygame.Rect] = None
        self._viz_next: Optional[pygame.Rect] = None
        self._viz_prev_hot = False
        self._viz_next_hot = False
        self._viz_drag_band: Optional[int] = None
        self._viz_drag_y: Optional[int] = None
        self._viz_hover_band: Optional[int] = None

        self._boot(0.32, "Loading your settings")
        self.eq = GraphicEQ(BAND_HZ)
        self.spectrum = Spectrum()
        self.active_preset = "Flat"
        self.user_presets: Dict[str, Dict[str, Any]] = {}
        self._naming = False
        self._name_buf = ""
        self._load_open = False
        self._load_hits: List[Tuple[pygame.Rect, str]] = []
        self._load_settings()
        self._boot(0.58, "Starting audio")
        self.audio = AudioEngine(self.eq, self.spectrum)
        try:
            self.audio.start()
        except Exception as exc:
            log_exception("AudioEngine.start bootstrap", exc)
            try:
                self.audio.stop()
            except Exception:
                pass
            try:
                self.audio._start_idle_thread()
                self.audio.status = f"Audio init failed — idle: {exc}"[:140]
            except Exception:
                pass

        self.sliders: List[Knob] = []
        self.preamp: Optional[Knob] = None
        self.master: Optional[Knob] = None
        self.buttons: List[Button] = []
        self.dd_in: Optional[Dropdown] = None
        self.dd_out: Optional[Dropdown] = None
        self._setup_btn_phones: Optional[pygame.Rect] = None
        self._setup_btn_cable: Optional[pygame.Rect] = None
        self._setup_btn_retry: Optional[pygame.Rect] = None
        self._setup_btn_dismiss: Optional[pygame.Rect] = None
        try:
            self._boot(0.86, "Building the equalizer")
            if self.splash:
                self.splash.finish()
            apply_window_icon()
            self.screen = pygame.display.set_mode((self.w, self.h), pygame.RESIZABLE)
            self._layout()
            self._refresh_caption()
        except Exception as exc:
            log_exception("initial_layout", exc)
        self.running = True
        self.t0 = time.time()
        self._hover_tip = ""
        self._hover_pos = (0, 0)

    def _load_settings(self) -> None:
        cfg = load_eq_config()
        try:
            gains = cfg.get("gains")
            if isinstance(gains, list) and len(gains) == len(BAND_HZ):
                self.eq.set_gains([float(x) for x in gains])
            if "preamp" in cfg:
                self.eq.set_preamp(float(cfg["preamp"]))
            if "master" in cfg:
                self.eq.set_master_db(float(cfg["master"]))
            if "preset" in cfg and isinstance(cfg["preset"], str):
                self.active_preset = cfg["preset"]
            raw_presets = cfg.get("user_presets")
            if isinstance(raw_presets, dict):
                kept: Dict[str, Dict[str, Any]] = {}
                for name, item in raw_presets.items():
                    if not isinstance(item, dict):
                        continue
                    gains = item.get("gains")
                    if not isinstance(gains, list) or len(gains) != len(BAND_HZ):
                        continue
                    kept[str(name)[:24]] = {
                        "gains": [float(x) for x in gains],
                        "preamp": float(item.get("preamp", 0)),
                        "master": float(item.get("master", 0)),
                    }
                self.user_presets = kept
            if "auto_preamp" in cfg:
                self.eq.auto_preamp = bool(cfg["auto_preamp"])
            style = cfg.get("viz_style")
            if isinstance(style, str) and style in VIZ_STYLES:
                self.viz_style = VIZ_STYLES.index(style)
        except Exception as exc:
            log_exception("load_settings", exc)

    def _save_settings(self) -> None:
        try:
            save_eq_config(
                {
                    "gains": [float(g) for g in self.eq.gains],
                    "preamp": float(self.eq.preamp_db),
                    "master": float(self.eq.master_db),
                    "preset": str(getattr(self, "active_preset", "Flat")),
                    "auto_preamp": bool(self.eq.auto_preamp),
                    "user_presets": self.user_presets,
                    "viz_style": VIZ_STYLES[self.viz_style % len(VIZ_STYLES)],
                    "version": APP_VERSION,
                }
            )
        except Exception as exc:
            log_exception("save_settings", exc)

    @staticmethod
    def _font(size: int, bold: bool = False) -> pygame.font.Font:
        for name in ("segoeui", "calibri", "arial", "consolas", "dejavusans"):
            path = pygame.font.match_font(name, bold=bold)
            if path:
                try:
                    return pygame.font.Font(path, size)
                except Exception:
                    pass
        return pygame.font.SysFont("Arial", size, bold=bold)

    def _device_labels(self, devices: List[AudioDev], *, play: bool = False) -> List[str]:
        out = []
        for d in devices:
            short = AudioEngine.short_name(d)  # full name — dropdown wraps
            if play:
                low = safe_name(d.name).lower()
                if any(h in low for h in ("cable", "vb-audio", "voicemeeter")):
                    out.append(f"Cable: {short}")
                elif any(h in low for h in ("headphone", "headset", "earphone", "earbud", "airpods", "buds")):
                    out.append(f"Headphones: {short}")
                else:
                    out.append(f"Play: {short}")
            elif d.kind in ("loopback", "soundcard", "wasapi"):
                out.append(f"Speakers: {short}")
            elif d.kind == "mix":
                out.append(f"Mix: {short}")
            else:
                out.append(short)
        return out

    def _sync_dropdowns(self) -> None:
        if self.dd_in:
            self.dd_in.set_items(self._device_labels(self.audio.inputs, play=False), self.audio.in_idx)
        if self.dd_out:
            self.dd_out.set_items(self._device_labels(self.audio.outputs, play=True), self.audio.out_idx)

    def _layout(self) -> None:
        w, h = self.w, self.h
        viz_h = self.viz_h
        panel_top = viz_h + 6

        self.buttons = []
        y0 = panel_top + 12
        eq_lab = "EQ ON" if self.audio.affect_sound else "EQ OFF"
        self.buttons.append(Button(pygame.Rect(18, y0, 118, 54), eq_lab, "sys"))
        self.dd_in = Dropdown(
            pygame.Rect(148, y0, int(w * 0.34), 54),
            "CAPTURE",
            self._device_labels(self.audio.inputs, play=False),
            self.audio.in_idx,
        )
        self.dd_out = Dropdown(
            pygame.Rect(158 + int(w * 0.34), y0, int(w * 0.34), 54),
            "PLAY TO",
            self._device_labels(self.audio.outputs, play=True),
            self.audio.out_idx,
        )
        rx = self.dd_out.rect.right + 10
        self.buttons.append(Button(pygame.Rect(min(rx, w - 118), y0, 100, 54), "Refresh", "dev"))

        names = list(PRESETS.keys()) + list(COMMON_ADJUST.keys()) + ["Save", "Load", "Reset", "Mute"]
        bw, bh = 86, 30
        x0, y = 18, y0 + 64
        x = x0
        for name in names:
            if name in ("Save", "Load"):
                kind = "file"
            elif name in COMMON_ADJUST:
                kind = "adj"
            elif name in ("Reset", "Mute"):
                kind = "sys"
            else:
                kind = "preset"
            if x + bw > w - 16:
                x = x0
                y += bh + 6
            self.buttons.append(Button(pygame.Rect(x, y, bw, bh), name, kind))
            x += bw + 6
        self._btn_row_bottom = y + bh

        self.sliders = []
        n = len(BAND_HZ)
        side = 260
        margin = 18
        usable = w - margin * 2 - side
        gap = usable / max(1, n)
        knob_r = max(18, min(26, int(gap * 0.30)))
        slider_w = max(56, int(gap * 0.86))
        row_y = self._btn_row_bottom + knob_r + 28
        for i, lab in enumerate(BAND_LABELS):
            cx = int(margin + gap * i + gap * 0.5)
            accent = (0, 220, 190) if self.eq.gains[i] >= 0 else (255, 96, 130)
            self.sliders.append(
                Knob(cx, row_y, knob_r, self.eq.gains[i], GAIN_MIN, GAIN_MAX, lab, accent, slider_w)
            )

        self.preamp = Knob(
            w - 175, row_y, knob_r + 2, self.eq.preamp_db, -24, 12, "PRE", (255, 196, 72), 70
        )
        self.master = Knob(
            w - 62, row_y, knob_r + 2, self.eq.master_db, -24, 12, "MST", (170, 140, 255), 62
        )
        bottoms = [s.reset.bottom for s in self.sliders]
        if self.preamp:
            bottoms.append(self.preamp.reset.bottom)
        if self.master:
            bottoms.append(self.master.reset.bottom)
        need_h = max(bottoms) + 12 if bottoms else h
        if need_h != self.h:
            self.h = need_h
            self._apply_window_size()

    def _apply_window_size(self) -> None:
        if self._sizing:
            return
        self._sizing = True
        try:
            apply_window_icon()
            self.screen = pygame.display.set_mode((self.w, self.h), pygame.RESIZABLE)
        finally:
            self._sizing = False

    def _apply_preset(self, name: str) -> None:
        if name not in PRESETS:
            return
        gains = PRESETS[name][:]
        self.eq.set_gains(gains)
        for s, g in zip(self.sliders, gains):
            s.value = g
        self.active_preset = name
        self._save_settings()
        self.audio.push_system_eq(force=True)

    def _apply_user_preset(self, name: str) -> None:
        item = self.user_presets.get(name)
        if not item:
            return
        gains = [float(x) for x in item["gains"]]
        self.eq.set_gains(gains)
        for s, g in zip(self.sliders, gains):
            s.value = g
        pre = float(item.get("preamp", 0))
        mst = float(item.get("master", 0))
        self.eq.set_preamp(pre)
        self.eq.set_master_db(mst)
        if self.preamp:
            self.preamp.value = pre
        if self.master:
            self.master.value = mst
        self.active_preset = name
        self._load_open = False
        self._save_settings()
        self.audio.push_system_eq(force=True)
        self.audio.status = f"Loaded preset {name}"

    def _save_preset_file(self) -> None:
        name = self.active_preset if self.active_preset not in ("Custom", "Flat") else "My Preset"
        safe = "".join(ch if ch.isalnum() or ch in " -_" else "" for ch in name).strip() or "My Preset"
        path = _eqpreset_dialog(True, safe)
        if not path:
            self.audio.status = "Save cancelled."
            return
        text = preset_to_text(safe, list(self.eq.gains), self.eq.preamp_db, self.eq.master_db)
        try:
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(text)
        except OSError as exc:
            self.audio.status = f"Could not save preset: {exc}"
            return
        self.active_preset = os.path.splitext(os.path.basename(path))[0]
        self._save_settings()
        self.audio.status = f"Saved {os.path.basename(path)}"

    def _load_preset_file(self) -> None:
        path = _eqpreset_dialog(False, "")
        if not path:
            self.audio.status = "Open cancelled."
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
        except OSError as exc:
            self.audio.status = f"Could not open preset: {exc}"
            return
        try:
            item = preset_from_text(text)
        except ValueError:
            item = None
        if not item:
            self.audio.status = "That file is not a Spectra .eqpreset."
            return
        gains = [float(x) for x in item["gains"]]
        self.eq.set_gains(gains)
        for s, g in zip(self.sliders, gains):
            s.value = g
        pre = float(item["preamp"])
        mst = float(item["master"])
        self.eq.set_preamp(pre)
        self.eq.set_master_db(mst)
        if self.preamp:
            self.preamp.value = pre
        if self.master:
            self.master.value = mst
        self.active_preset = str(item.get("name") or os.path.splitext(os.path.basename(path))[0])
        self._save_settings()
        self.audio.push_system_eq(force=True)
        self.audio.status = f"Loaded {os.path.basename(path)}"

    def _store_user_preset(self, name: str) -> None:
        name = " ".join(name.split())[:24]
        if not name:
            self.audio.status = "Type a preset name, then press Enter."
            return
        self.user_presets[name] = {
            "gains": [float(g) for g in self.eq.gains],
            "preamp": float(self.eq.preamp_db),
            "master": float(self.eq.master_db),
        }
        self.active_preset = name
        self._naming = False
        self._name_buf = ""
        self._save_settings()
        self.audio.status = f"Saved preset {name}"

    def _delete_user_preset(self, name: str) -> None:
        if name not in self.user_presets:
            return
        del self.user_presets[name]
        self._save_settings()
        self.audio.status = f"Deleted preset {name}"

    def _apply_adjust(self, name: str) -> None:
        fn = COMMON_ADJUST.get(name)
        if not fn:
            return
        gains = fn(self.eq.gains[:])
        self.eq.set_gains(gains)
        for s, g in zip(self.sliders, gains):
            s.value = g
        self.active_preset = "Custom"
        self._save_settings()
        self.audio.push_system_eq(force=True)

    def _eq_is_blocked(self) -> bool:
        return bool(getattr(self.audio, "_eq_block_msg", "") and not self.audio.affect_sound)

    def _open_system_eq_install(self) -> None:
        path = find_apo_installer()
        if path and launch_file(path):
            self.audio.status = (
                "Started the Equalizer APO installer next to Spectra. "
                "Tick your Speakers, reboot, then press EQ ON."
            )
            return
        try:
            webbrowser.open(SystemApo.URL)
        except Exception as exc:
            self.audio.status = f"Open browser failed: {exc}"
            return
        self.audio.status = (
            "No Equalizer APO setup exe is next to Spectra yet. "
            "Download it from stackdrop.it/downloads/EqualizerAPO.exe, install it, tick Speakers, and reboot. "
            "It cannot live inside SpectraEQ.exe — Windows has to install the audio effect."
        )

    def _open_vb_cable(self) -> None:
        try:
            webbrowser.open("https://vb-audio.com/Cable/")
            self.audio.status = (
                "Opened VB-Cable download — install, then Refresh, then EQ ON "
                "(auto: Cable capture → Speakers play)"
            )[:160]
            self.audio._eq_block_msg = (
                "Install VB-Cable, then click Refresh + EQ ON. "
                "Windows default sound → CABLE Input. Spectra: Cable → EQ → Speakers."
            )
        except Exception as exc:
            self.audio.status = f"Open browser failed: {exc}"[:140]

    def _use_headphones_path(self) -> None:
        """Path A: PLAY to headphones while CAPTURE stays on Speakers loopback."""
        try:
            picked = None
            for i, d in enumerate(self.audio.outputs):
                nm = safe_name(d.name).lower()
                if any(
                    h in nm
                    for h in (
                        "headphone",
                        "headset",
                        "earphone",
                        "earbud",
                        "airpods",
                        "buds",
                        "bluetooth",
                    )
                ):
                    if not self.audio._feedback_risk(self.audio.input_dev, d):
                        picked = i
                        break
            if picked is None:
                self.audio._eq_block_msg = (
                    "No headphones found in PLAY list. Plug them in, set Windows "
                    "output to Headphones, click Refresh, then PLAY EQ TO → Headphones."
                )
                self.audio.status = self.audio._eq_block_msg[:160]
                if self.dd_out:
                    self.dd_out.open = True
                return
            self.audio.set_output(picked)
            self._sync_dropdowns()
            self.audio._eq_block_msg = ""
            self.audio.set_affect(True)
            self._sync_dropdowns()
            self._relayout_keep_gains()
            if self.audio.affect_sound:
                self.audio.status = (
                    f"EQ ON (Headphones) — playing to {AudioEngine.short_name(self.audio.output_dev, 40)}"
                )[:160]
            else:
                if self.dd_out:
                    self.dd_out.open = True
        except Exception as exc:
            self.audio.status = log_exception("use_headphones", exc)

    def _retry_eq_after_setup(self) -> None:
        try:
            self.audio.refresh_devices()
            self._sync_dropdowns()
            self.audio.set_affect(True)
            self._sync_dropdowns()
            self._relayout_keep_gains()
        except Exception as exc:
            self.audio.status = log_exception("retry_eq", exc)

    def _relayout_keep_gains(self) -> None:
        self._layout()
        for s, g in zip(self.sliders, self.eq.gains):
            s.value = g
            s.accent = (0, 220, 190) if g >= 0 else (230, 100, 130)
        if self.preamp:
            self.preamp.value = self.eq.preamp_db
        if self.master:
            self.master.value = self.eq.master_db

    def handle(self, event: pygame.event.Event) -> None:
        if event.type == pygame.QUIT:
            self.running = False
        elif event.type == pygame.VIDEORESIZE:
            if self._sizing:
                return
            new_w = max(1100, event.w)
            if new_w == self.w:
                return
            self.w = new_w
            self._relayout_keep_gains()
        elif event.type == pygame.MOUSEWHEEL:
            pos = pygame.mouse.get_pos()
            for dd in (self.dd_in, self.dd_out):
                if dd and dd.handle_wheel(pos, event.y):
                    return
            step = 1.0 if event.y > 0 else -1.0
            for i, s in enumerate(self.sliders):
                if s.hit(pos):
                    s.nudge(step)
                    self.eq.set_band(i, s.value)
                    s.accent = (0, 220, 190) if s.value >= 0 else (230, 100, 130)
                    self.active_preset = "Custom"
                    self.audio.push_system_eq(force=True)
                    self._save_settings()
                    return
            if self.preamp and self.preamp.hit(pos):
                self.preamp.nudge(step)
                self.eq.set_preamp(self.preamp.value)
                self.audio.push_system_eq(force=True)
                self._save_settings()
                return
            if self.master and self.master.hit(pos):
                self.master.nudge(step)
                self.eq.set_master_db(self.master.value)
                self.audio.push_system_eq(force=True)
                self._save_settings()
                return
        elif event.type == pygame.KEYDOWN:
            if self._naming:
                if event.key == pygame.K_ESCAPE:
                    self._naming = False
                    self._name_buf = ""
                elif event.key == pygame.K_BACKSPACE:
                    self._name_buf = self._name_buf[:-1]
                elif event.key in (pygame.K_RETURN, pygame.K_KP_ENTER):
                    self._store_user_preset(self._name_buf)
                else:
                    ch = event.unicode or ""
                    if ch.isprintable() and ch not in "\r\n\t" and len(self._name_buf) < 24:
                        self._name_buf += ch
                return
            if event.key == pygame.K_LEFT:
                self._cycle_viz(-1)
                return
            if event.key == pygame.K_RIGHT:
                self._cycle_viz(1)
                return
            if event.key == pygame.K_ESCAPE:
                if self.dd_in and self.dd_in.open:
                    self.dd_in.open = False
                elif self.dd_out and self.dd_out.open:
                    self.dd_out.open = False
                else:
                    self.running = False
            elif event.key == pygame.K_SPACE:
                self.audio.muted = not self.audio.muted
                self.audio.push_system_eq(force=True)
            elif event.key == pygame.K_e:
                self.audio.toggle_affect()
                self._sync_dropdowns()
                self._relayout_keep_gains()
            elif event.key == pygame.K_LEFTBRACKET:
                self.audio.cycle_input(-1)
                self._sync_dropdowns()
            elif event.key == pygame.K_RIGHTBRACKET:
                self.audio.cycle_input(1)
                self._sync_dropdowns()
            elif event.key == pygame.K_SEMICOLON:
                self.audio.cycle_output(-1)
                self._sync_dropdowns()
            elif event.key == pygame.K_QUOTE:
                self.audio.cycle_output(1)
                self._sync_dropdowns()
            elif event.key == pygame.K_r:
                self._apply_preset("Flat")
                self.eq.set_preamp(0)
                self.eq.set_master_db(0)
                if self.preamp:
                    self.preamp.value = 0
                if self.master:
                    self.master.value = 0
            else:
                keys = {
                    pygame.K_1: "Flat",
                    pygame.K_2: "Bass Boost",
                    pygame.K_3: "Treble",
                    pygame.K_4: "V-Shape",
                    pygame.K_5: "Vocal",
                    pygame.K_6: "Electronic",
                    pygame.K_7: "Rock",
                    pygame.K_8: "Classical",
                    pygame.K_9: "Podcast",
                    pygame.K_0: "Cinema",
                }
                if event.key in keys:
                    self._apply_preset(keys[event.key])
        elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 3:
            pos = event.pos
            if self._load_open:
                for rect, name in self._load_hits:
                    if rect.collidepoint(pos):
                        self._delete_user_preset(name)
                        return
            for i, s in enumerate(self.sliders):
                if s.hit(pos):
                    s.value = 0.0
                    self.eq.set_band(i, 0.0)
                    s.accent = (0, 220, 190)
                    self.active_preset = "Custom"
                    self.audio.push_system_eq(force=True)
                    return
            if self.preamp and self.preamp.hit(pos):
                self.preamp.value = 0.0
                self.eq.set_preamp(0.0)
                self.audio.push_system_eq(force=True)
                return
            if self.master and self.master.hit(pos):
                self.master.value = 0.0
                self.eq.set_master_db(0.0)
                self.audio.push_system_eq(force=True)
                return
        elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
            pos = event.pos
            # Setup overlay buttons (both EQ paths) take priority when blocked
            if self._eq_is_blocked():
                if self._setup_btn_cable and self._setup_btn_cable.collidepoint(pos):
                    self._open_system_eq_install()
                    return
                if self._setup_btn_retry and self._setup_btn_retry.collidepoint(pos):
                    self.audio._eq_block_msg = ""
                    self.audio.set_affect(True)
                    return
                if self._setup_btn_dismiss and self._setup_btn_dismiss.collidepoint(pos):
                    self.audio._eq_block_msg = ""
                    return
            if self._viz_prev and self._viz_prev.collidepoint(pos):
                self._cycle_viz(-1)
                return
            if self._viz_next and self._viz_next.collidepoint(pos):
                self._cycle_viz(1)
                return
            band = self._viz_band_at(pos)
            if band is not None:
                self._viz_drag_band = band
                self._viz_drag_y = pos[1]
                self.sliders[band].dragging = True
                return
            # Dropdowns first (capture clicks while open)
            for dd, setter in ((self.dd_in, self.audio.set_input), (self.dd_out, self.audio.set_output)):
                if dd is None:
                    continue
                if dd.open or dd.rect.collidepoint(pos):
                    picked = dd.handle_click(pos)
                    if picked is not None and picked >= 0:
                        setter(picked)
                    # close the other
                    other = self.dd_out if dd is self.dd_in else self.dd_in
                    if other:
                        other.open = False
                    return
            # close dropdowns on outside click
            if self.dd_in:
                self.dd_in.open = False
            if self.dd_out:
                self.dd_out.open = False

            if self._load_open:
                picked = None
                for rect, name in self._load_hits:
                    if rect.collidepoint(pos):
                        picked = name
                        break
                if picked:
                    self._apply_user_preset(picked)
                    return
                on_load = any(b.label == "Load" and b.rect.collidepoint(pos) for b in self.buttons)
                if not on_load:
                    self._load_open = False

            for i, s in enumerate(self.sliders):
                if s.hit_reset(pos):
                    s.value = 0.0
                    self.eq.set_band(i, 0.0)
                    s.accent = (0, 220, 190)
                    self.active_preset = "Custom"
                    self.audio.push_system_eq(force=True)
                    return
            if self.preamp and self.preamp.hit_reset(pos):
                self.preamp.value = 0.0
                self.eq.set_preamp(0.0)
                self.audio.push_system_eq(force=True)
                return
            if self.master and self.master.hit_reset(pos):
                self.master.value = 0.0
                self.eq.set_master_db(0.0)
                self.audio.push_system_eq(force=True)
                return

            grabbed = False
            for i, s in enumerate(self.sliders):
                if s.hit(pos):
                    s.begin_drag(pos)
                    self.eq.set_band(i, s.value)
                    s.accent = (0, 220, 190) if s.value >= 0 else (230, 100, 130)
                    self.active_preset = "Custom"
                    self.audio.push_system_eq()
                    grabbed = True
                    break
            if not grabbed and self.preamp and self.preamp.hit(pos):
                self.preamp.begin_drag(pos)
                self.eq.set_preamp(self.preamp.value)
                self.audio.push_system_eq()
                grabbed = True
            if not grabbed and self.master and self.master.hit(pos):
                self.master.begin_drag(pos)
                self.eq.set_master_db(self.master.value)
                self.audio.push_system_eq()
                grabbed = True
            if grabbed:
                return
            for b in self.buttons:
                if b.rect.collidepoint(pos):
                    if b.label in ("EQ ON", "EQ OFF"):
                        self.audio.toggle_affect()
                        self._sync_dropdowns()
                        self._relayout_keep_gains()
                    elif b.label == "Refresh":
                        self.audio.refresh_devices()
                        self._sync_dropdowns()
                    elif b.label == "Save":
                        self._save_preset_file()
                    elif b.label == "Load":
                        self._load_preset_file()
                    elif b.label in PRESETS:
                        self._apply_preset(b.label)
                    elif b.label in COMMON_ADJUST:
                        self._apply_adjust(b.label)
                    elif b.label == "Reset":
                        self._apply_preset("Flat")
                        self.eq.set_preamp(0)
                        self.eq.set_master_db(0)
                        if self.preamp:
                            self.preamp.value = 0
                        if self.master:
                            self.master.value = 0
                        self.audio.push_system_eq(force=True)
                    elif b.label == "Mute":
                        self.audio.muted = not self.audio.muted
                        self.audio.push_system_eq(force=True)
                    break
        elif event.type == pygame.MOUSEBUTTONUP and event.button == 1:
            self._viz_drag_band = None
            self._viz_drag_y = None
            for s in self.sliders:
                s.end_drag()
            if self.preamp:
                self.preamp.end_drag()
            if self.master:
                self.master.end_drag()
            self._save_settings()
            self.audio.push_system_eq(force=True)
        elif event.type == pygame.MOUSEMOTION:
            pos = event.pos
            if self._viz_drag_band is not None:
                self._viz_drag_gain(pos)
            self._viz_hover_band = None if self._viz_drag_band is not None else self._viz_band_at(pos)
            for i, s in enumerate(self.sliders):
                if s.dragging:
                    s.drag_to(pos)
                    self.eq.set_band(i, s.value)
                    s.accent = (0, 220, 190) if s.value >= 0 else (230, 100, 130)
                    self.active_preset = "Custom"
                    self.audio.push_system_eq()
            if self.preamp and self.preamp.dragging:
                self.preamp.drag_to(pos)
                self.eq.set_preamp(self.preamp.value)
                self.audio.push_system_eq()
            if self.master and self.master.dragging:
                self.master.drag_to(pos)
                self.eq.set_master_db(self.master.value)
                self.audio.push_system_eq()
            for b in self.buttons:
                b.hot = b.rect.collidepoint(pos)
            for s in self.sliders:
                s.hot = s.hit(pos)
                s.reset_hot = s.hit_reset(pos)
            hover = self._viz_drag_band if self._viz_drag_band is not None else self._viz_hover_band
            if hover is not None and hover < len(self.sliders):
                self.sliders[hover].hot = True
            if self.preamp:
                self.preamp.hot = self.preamp.hit(pos)
                self.preamp.reset_hot = self.preamp.hit_reset(pos)
            if self.master:
                self.master.hot = self.master.hit(pos)
                self.master.reset_hot = self.master.hit_reset(pos)
            for dd in (self.dd_in, self.dd_out):
                if dd:
                    dd.hot = dd.rect.collidepoint(pos)
                    dd.hover = dd.index_at(pos) if dd.open else -1
            self._viz_prev_hot = bool(self._viz_prev and self._viz_prev.collidepoint(pos))
            self._viz_next_hot = bool(self._viz_next and self._viz_next.collidepoint(pos))
            self._hover_pos = pos
            self._hover_tip = self._tip_at(pos)

    def _boot(self, frac: float, message: str) -> None:
        if self.splash:
            self.splash.step(frac, message)

    def _refresh_caption(self) -> None:
        name = VIZ_STYLES[self.viz_style % len(VIZ_STYLES)]
        pygame.display.set_caption(f"SPECTRA EQ {APP_VERSION} — {name}")

    def _cycle_viz(self, step: int) -> None:
        self.viz_style = (int(self.viz_style) + step) % len(VIZ_STYLES)
        self._refresh_caption()
        self._save_settings()

    def _tip_at(self, pos: Tuple[int, int]) -> str:
        name = VIZ_STYLES[self.viz_style % len(VIZ_STYLES)]
        if self._viz_prev and self._viz_prev.collidepoint(pos):
            return f"Previous spectrum style. Now showing {name}."
        if self._viz_next and self._viz_next.collidepoint(pos):
            return f"Next spectrum style. Now showing {name}."
        for dd, what in (
            (self.dd_in, "Capture device. The mesh listens here. System EQ still changes the speakers."),
            (self.dd_out, "Play device. Leave this on your speakers. System EQ does not play a second copy."),
        ):
            if dd and dd.open:
                i = dd.index_at(pos)
                if i >= 0 and i < len(dd.items):
                    return f"{dd.items[i]}. {what}"
            if dd and dd.rect.collidepoint(pos):
                return what
        for b in self.buttons:
            if b.rect.collidepoint(pos):
                return TIPS.get(b.label, b.label)
        if self.preamp and self.preamp.hit_reset(pos):
            return "Reset preamp to 0 dB."
        if self.master and self.master.hit_reset(pos):
            return "Reset master to 0 dB."
        if self.preamp and self.preamp.hit(pos):
            return f"Preamp {self.preamp.value:+.0f} dB. Volume before the EQ. Drag, scroll, or press Reset."
        if self.master and self.master.hit(pos):
            return f"Master {self.master.value:+.0f} dB. Volume after the EQ. Drag, scroll, or press Reset."
        for s in self.sliders:
            if s.hit_reset(pos):
                return f"Reset {s.label} to 0 dB."
            if s.hit(pos):
                return (
                    f"{s.label} Hz  {s.value:+.0f} dB. "
                    "Drag the slider or knob. Scroll changes 1 dB. Reset sets it to 0."
                )
        return ""

    def _draw_tooltip(self, surf: pygame.Surface) -> None:
        text = self._hover_tip
        if not text:
            return
        lines = wrap_text(self.font_sm, text, 320)[:4]
        line_h = self.font_sm.get_height() + 2
        pad = 8
        tw = max(self.font_sm.size(ln)[0] for ln in lines) + pad * 2
        th = line_h * len(lines) + pad
        x = self._hover_pos[0] + 16
        y = self._hover_pos[1] + 18
        if x + tw > self.w - 8:
            x = max(8, self._hover_pos[0] - tw - 12)
        if y + th > self.h - 8:
            y = max(8, self._hover_pos[1] - th - 12)
        bg = pygame.Surface((tw, th), pygame.SRCALPHA)
        bg.fill((8, 14, 22, 235))
        surf.blit(bg, (x, y))
        pygame.draw.rect(surf, (0, 210, 220), pygame.Rect(x, y, tw, th), 1, border_radius=6)
        yy = y + 4
        for ln in lines:
            surf.blit(self.font_sm.render(ln, True, (236, 246, 255)), (x + pad, yy))
            yy += line_h

    def _viz_series(self) -> Tuple[np.ndarray, np.ndarray, int]:
        mesh = self.spectrum.mesh()
        front = np.array(mesh[-1], dtype=np.float32)
        if front.size >= 3:
            kernel = np.array([0.22, 0.56, 0.22], dtype=np.float32)
            front = np.convolve(front, kernel, mode="same")
        return mesh, front, int(front.shape[0])

    @staticmethod
    def _viz_tone(x: float, amp: float, sat: float = 0.82, val: float = 1.0) -> Tuple[int, int, int]:
        hue = max(0.0, min(0.78, x * 0.78))
        bright = 0.42 + 0.58 * max(0.0, min(1.0, amp))
        return hsv(hue, sat, min(1.0, val * bright))

    @staticmethod
    def _viz_hz_x(hz: float) -> float:
        span = math.log(F_MAX) - math.log(F_MIN)
        x = (math.log(max(hz, 1.0)) - math.log(F_MIN)) / span
        return max(0.0, min(1.0, x))

    @staticmethod
    def _viz_px(w: int, x: float, inset: int = 72) -> int:
        return int(inset + x * max(1, w - inset * 2))

    def _viz_band_anchor(self, w: int, index: int) -> Tuple[int, int]:
        """Screen point of one EQ band on the current spectrum graphic."""
        x = self._viz_hz_x(BAND_HZ[index])
        viz_h = self.viz_h
        style = VIZ_STYLES[self.viz_style % len(VIZ_STYLES)]
        if style == "Rings":
            cx, cy = w // 2, int(viz_h * 0.50)
            reach = min((w - 160) * 0.46, viz_h * 0.40)
            inner = reach * 0.34
            ang = math.radians(110) + x * math.radians(320)
            radius = inner + reach * 0.46
            return int(cx + math.cos(ang) * radius), int(cy + math.sin(ang) * radius)
        if style == "Bars":
            span = max(80, w - 144)
            px = w * 0.50 + (x - 0.5) * span
            return int(px), int(viz_h * 0.74)
        if style == "Mirror":
            return self._viz_px(w, x), int(viz_h * 0.48)
        return self._viz_px(w, x), int(viz_h * 0.78)

    def _viz_band_at(self, pos: Tuple[int, int]) -> Optional[int]:
        """Band under the pointer, so a drag on 20 Hz turns the 20 Hz knob."""
        if not self.sliders or self._eq_is_blocked():
            return None
        x, y = pos
        if y < 0 or y >= self.viz_h - 8:
            return None
        if (self._viz_prev and self._viz_prev.collidepoint(pos)) or (self._viz_next and self._viz_next.collidepoint(pos)):
            return None
        w = self.screen.get_width() if getattr(self, "screen", None) else self.w
        style = VIZ_STYLES[self.viz_style % len(VIZ_STYLES)]
        best_i = 0
        best = 10 ** 12
        for i in range(len(BAND_HZ)):
            ax, ay = self._viz_band_anchor(w, i)
            dist = (x - ax) ** 2 + (y - ay) ** 2 if style == "Rings" else abs(x - ax)
            if dist < best:
                best, best_i = dist, i
        if style == "Rings" and best > 90 ** 2:
            return None
        return best_i

    def _viz_drag_gain(self, pos: Tuple[int, int]) -> None:
        i = self._viz_drag_band
        if i is None or i >= len(self.sliders) or self._viz_drag_y is None:
            return
        span = GAIN_MAX - GAIN_MIN
        dy = self._viz_drag_y - pos[1]
        self._viz_drag_y = pos[1]
        knob = self.sliders[i]
        knob.value += dy * (span / 56.0)
        knob._clamp()
        knob.dragging = True
        knob.accent = (0, 220, 190) if knob.value >= 0 else (230, 100, 130)
        self.eq.set_band(i, knob.value)
        self.active_preset = "Custom"
        self.audio.push_system_eq()

    def _viz_scale(self, surf: pygame.Surface, viz_h: int, place) -> None:
        for hz in (20, 100, 1000, 10000):
            x = self._viz_hz_x(hz)
            px, py = place(x)
            col = self._viz_tone(x, 0.85, 0.7, 1.0)
            lab = f"{hz // 1000}k" if hz >= 1000 else str(hz)
            txt = self.font_tiny.render(lab, True, col)
            surf.blit(txt, (px - txt.get_width() // 2, min(viz_h - 18, py)))

    def _viz_tags(self, surf: pygame.Surface, w: int, viz_h: int) -> None:
        """Boost labels sit in one row under the spectrum, not on the animation."""
        used: List[pygame.Rect] = []
        row_y = viz_h - 30
        for hz, lab, gain in zip(BAND_HZ, BAND_LABELS, self.eq.gains):
            if float(gain) < 0.5:
                continue
            x = self._viz_hz_x(hz)
            px = self._viz_px(w, x)
            tag = f"{lab} +{float(gain):.0f}"
            col = self._viz_tone(x, 1.0, 0.55, 1.0)
            txt = self.font_tiny.render(tag, True, (255, 255, 255))
            tw, th = txt.get_size()
            box = pygame.Rect(max(8, min(w - tw - 16, px - (tw + 8) // 2)), row_y, tw + 8, th + 4)
            for prev in used:
                if box.colliderect(prev.inflate(6, 2)):
                    box.x = min(w - box.w - 8, prev.right + 4)
            used.append(box)
            pill = pygame.Surface((box.w, box.h), pygame.SRCALPHA)
            pill.fill((*col, 220))
            surf.blit(pill, box.topleft)
            pygame.draw.rect(surf, (255, 255, 255), box, 1, border_radius=4)
            surf.blit(txt, (box.x + 4, box.y + 2))

    def _viz_bars(self, surf, w, viz_h, mesh, front, cols) -> None:
        def cam(x: float, amp: float, z: float) -> Tuple[int, int]:
            depth = 1.05 + (1.0 - z) * 1.85
            span = max(80, w - 144)
            px = w * 0.50 + (x - 0.5) * span * 1.05 / depth
            floor = viz_h * 0.74
            py = floor - max(0.0, amp) * viz_h * 0.62 / depth + (1.0 - z) * viz_h * 0.10
            return int(px), int(py)

        glow = pygame.Surface((w, viz_h), pygame.SRCALPHA)
        for ago, z in ((-18, 0.22), (-8, 0.48)):
            row = np.array(mesh[ago], dtype=np.float32)
            ridge = [cam(i / max(1, cols - 1), float(row[i]) ** 0.85, z) for i in range(0, cols, 2)]
            if len(ridge) > 2:
                pygame.draw.lines(glow, (80, 120, 160, 70), False, ridge, 1)
        gap = 0.72 / max(1, cols)
        for i in range(cols):
            x0 = i / max(1, cols)
            x1 = min(1.0, x0 + gap)
            amp = max(0.03, min(1.0, float(front[i]) ** 0.82))
            mid = (x0 + x1) * 0.5
            front_col = self._viz_tone(mid, amp, 0.78, 0.95)
            top_col = self._viz_tone(mid, amp, 0.45, 1.15)
            side_col = self._viz_tone(mid, amp, 0.9, 0.55)
            bl, br = cam(x0, 0.0, 1.0), cam(x1, 0.0, 1.0)
            tr, tl = cam(x1, amp, 1.0), cam(x0, amp, 1.0)
            tlb, trb, brb = cam(x0, amp, 0.86), cam(x1, amp, 0.86), cam(x1, 0.0, 0.86)
            pygame.draw.polygon(surf, side_col, (br, tr, trb, brb))
            pygame.draw.polygon(surf, front_col, (bl, br, tr, tl))
            pygame.draw.polygon(surf, top_col, (tl, tr, trb, tlb))
            if amp > 0.08:
                pygame.draw.line(glow, (*top_col, 90), tl, tr, 2)
            drop = int(10 + amp * 28)
            pygame.draw.polygon(glow, (*front_col, 38), ((bl[0], bl[1] + 2), (br[0], br[1] + 2), (br[0], br[1] + drop), (bl[0], bl[1] + drop)))
        surf.blit(glow, (0, 0))
        self._viz_scale(surf, viz_h, lambda x: (cam(x, 0.0, 1.0)[0], min(viz_h - 48, cam(x, 0.0, 1.0)[1] + 8)))

    def _viz_wave(self, surf, w, viz_h, mesh, front, cols) -> None:
        floor = int(viz_h * 0.78)
        pts = []
        for i in range(cols):
            x = i / max(1, cols - 1)
            amp = max(0.0, min(1.0, float(front[i]) ** 0.82))
            pts.append((self._viz_px(w, x), int(floor - amp * viz_h * 0.58)))
        if len(pts) < 2:
            return
        for i in range(len(pts) - 1):
            x = i / max(1, cols - 1)
            amp = max(0.15, min(1.0, float(front[i])))
            col = self._viz_tone(x, amp, 0.8, 0.9)
            pygame.draw.polygon(surf, col, (pts[i], pts[i + 1], (pts[i + 1][0], floor), (pts[i][0], floor)))
        pygame.draw.lines(surf, (245, 255, 255), False, pts, 2)
        glow = pygame.Surface((w, viz_h), pygame.SRCALPHA)
        for ago, lift in ((-16, 18), (-6, 8)):
            row = np.array(mesh[ago], dtype=np.float32)
            ridge = []
            for i in range(0, cols, 2):
                x = i / max(1, cols - 1)
                amp = max(0.0, min(1.0, float(row[i]) ** 0.85))
                ridge.append((self._viz_px(w, x), int(floor - amp * viz_h * 0.58 - lift)))
            if len(ridge) > 2:
                pygame.draw.lines(glow, (120, 200, 220, 80), False, ridge, 1)
        surf.blit(glow, (0, 0))
        self._viz_scale(surf, viz_h, lambda x: (self._viz_px(w, x), floor + 6))

    def _viz_mirror(self, surf, w, viz_h, mesh, front, cols) -> None:
        cy = int(viz_h * 0.48)
        span = max(1, w - 144)
        bar_w = max(3, int(span / max(1, cols)) - 2)
        pygame.draw.line(surf, (0, 120, 140), (72, cy), (w - 72, cy), 1)
        for i in range(cols):
            x = i / max(1, cols - 1)
            amp = max(0.02, min(1.0, float(front[i]) ** 0.8))
            hgt = int(amp * viz_h * 0.40)
            px = self._viz_px(w, x) - bar_w // 2
            col = self._viz_tone(x, amp, 0.82, 1.0)
            dim = tuple(max(0, c // 3) for c in col)
            pygame.draw.rect(surf, col, pygame.Rect(px, cy - hgt, bar_w, hgt), border_radius=2)
            pygame.draw.rect(surf, dim, pygame.Rect(px, cy + 2, bar_w, max(2, int(hgt * 0.72))), border_radius=2)
        self._viz_scale(surf, viz_h, lambda x: (self._viz_px(w, x), viz_h - 52))

    def _viz_rings(self, surf, w, viz_h, mesh, front, cols) -> None:
        cx, cy = w // 2, int(viz_h * 0.50)
        reach = min((w - 160) * 0.46, viz_h * 0.40)
        inner = reach * 0.34
        start, sweep = math.radians(110), math.radians(320)
        for i in range(cols):
            x = i / max(1, cols)
            amp = max(0.04, min(1.0, float(front[i]) ** 0.8))
            ang = start + (i / max(1, cols)) * sweep
            outer = inner + amp * reach * 0.92
            x0, y0 = cx + math.cos(ang) * inner, cy + math.sin(ang) * inner
            x1, y1 = cx + math.cos(ang) * outer, cy + math.sin(ang) * outer
            pygame.draw.line(surf, self._viz_tone(x, amp, 0.85, 1.0), (x0, y0), (x1, y1), 3)
        pygame.draw.circle(surf, (0, 180, 200), (cx, cy), int(inner * 0.72), 1)

        def place_hz(x: float) -> Tuple[int, int]:
            ang = start + x * sweep
            r = inner * 0.55
            return int(cx + math.cos(ang) * r), int(cy + math.sin(ang) * r)

        self._viz_scale(surf, viz_h, place_hz)

    def _viz_needles(self, surf, w, viz_h, mesh, front, cols) -> None:
        floor = int(viz_h * 0.78)
        glow = pygame.Surface((w, viz_h), pygame.SRCALPHA)
        older = np.array(mesh[-10], dtype=np.float32) if mesh.shape[0] > 10 else front
        for i in range(cols):
            x = i / max(1, cols - 1)
            px = self._viz_px(w, x)
            amp = max(0.0, min(1.0, float(front[i]) ** 0.82))
            tip = int(floor - amp * viz_h * 0.58)
            col = self._viz_tone(x, max(0.2, amp), 0.86, 1.0)
            pygame.draw.line(surf, col, (px, floor), (px, tip), 2)
            ghost = max(0.0, min(1.0, float(older[i]) ** 0.85))
            gy = int(floor - ghost * viz_h * 0.58)
            pygame.draw.circle(glow, (*col, 70), (px, gy), 5)
            pygame.draw.circle(surf, (255, 255, 255), (px, tip), 3)
            pygame.draw.circle(surf, col, (px, tip), 6, 2)
        surf.blit(glow, (0, 0))
        pygame.draw.line(surf, (0, 120, 140), (72, floor), (w - 72, floor), 1)
        self._viz_scale(surf, viz_h, lambda x: (self._viz_px(w, x), floor + 6))

    def _draw_viz_grab(self, surf: pygame.Surface, w: int, viz_h: int) -> None:
        band = self._viz_drag_band if self._viz_drag_band is not None else self._viz_hover_band
        if band is None or band >= len(BAND_HZ):
            return
        ax, ay = self._viz_band_anchor(w, band)
        style = VIZ_STYLES[self.viz_style % len(VIZ_STYLES)]
        col = (255, 255, 255) if self._viz_drag_band is not None else (180, 230, 240)
        if style == "Rings":
            pygame.draw.circle(surf, col, (ax, ay), 10, 2)
        else:
            pygame.draw.line(surf, col, (ax, 34), (ax, viz_h - 36), 1)
        gain = float(self.eq.gains[band])
        tag = f"{BAND_LABELS[band]}  {gain:+.1f} dB"
        txt = self.font_sm.render(tag, True, (255, 255, 255))
        box = pygame.Rect(0, 0, txt.get_width() + 12, txt.get_height() + 6)
        box.centerx = max(box.w // 2 + 8, min(w - box.w // 2 - 8, ax))
        box.bottom = max(28, ay - 8) if style == "Rings" else 32
        pill = pygame.Surface((box.w, box.h), pygame.SRCALPHA)
        pill.fill((0, 18, 28, 210))
        surf.blit(pill, box.topleft)
        pygame.draw.rect(surf, col, box, 1, border_radius=6)
        surf.blit(txt, (box.x + 6, box.y + 3))

    def _draw_viz_arrows(self, surf: pygame.Surface, w: int, viz_h: int) -> None:
        size = 46
        cy = viz_h // 2
        self._viz_prev = pygame.Rect(12, cy - size // 2, size, size)
        self._viz_next = pygame.Rect(w - 12 - size, cy - size // 2, size, size)
        for rect, hot, direction in (
            (self._viz_prev, self._viz_prev_hot, -1),
            (self._viz_next, self._viz_next_hot, 1),
        ):
            bg = pygame.Surface((rect.w, rect.h), pygame.SRCALPHA)
            bg.fill((0, 18, 28, 220 if hot else 150))
            surf.blit(bg, rect.topleft)
            pygame.draw.rect(surf, (0, 235, 245) if hot else (0, 160, 180), rect, 2, border_radius=10)
            mx, my = rect.center
            if direction < 0:
                pts = [(mx + 8, my - 12), (mx - 10, my), (mx + 8, my + 12)]
            else:
                pts = [(mx - 8, my - 12), (mx + 10, my), (mx - 8, my + 12)]
            pygame.draw.polygon(surf, (236, 255, 255), pts)

    def draw_viz(self, surf: pygame.Surface) -> None:
        w, h = surf.get_size()
        viz_h = self.viz_h
        for i in range(16):
            y0 = int(viz_h * i / 16)
            y1 = int(viz_h * (i + 1) / 16)
            shade = 5 + int(i * 0.7)
            pygame.draw.rect(surf, (shade, shade + 1, shade + 8), (0, y0, w, max(1, y1 - y0)))

        mesh, front, cols = self._viz_series()
        style = VIZ_STYLES[self.viz_style % len(VIZ_STYLES)]
        draw = {
            "Bars": self._viz_bars,
            "Wave": self._viz_wave,
            "Mirror": self._viz_mirror,
            "Rings": self._viz_rings,
            "Needles": self._viz_needles,
        }[style]
        draw(surf, w, viz_h, mesh, front, cols)
        self._draw_viz_grab(surf, w, viz_h)
        self._viz_tags(surf, w, viz_h)
        self._draw_viz_arrows(surf, w, viz_h)

        pygame.draw.line(surf, (0, 180, 196), (0, viz_h - 1), (w, viz_h - 1), 2)

        title_f = self.font_lg
        title = title_f.render("SPECTRA EQ", True, (0, 245, 255))
        title_bg = pygame.Surface((title.get_width() + 16, title.get_height() + 6), pygame.SRCALPHA)
        title_bg.fill((0, 0, 0, 160))
        tx = (w - title.get_width()) // 2
        surf.blit(title_bg, (tx - 8, 8))
        surf.blit(title, (tx, 10))

        if self.audio.muted:
            mute = self.font.render("MUTED", True, (255, 90, 110))
            bg = pygame.Surface((mute.get_width() + 16, mute.get_height() + 8), pygame.SRCALPHA)
            bg.fill((40, 0, 0, 220))
            surf.blit(bg, (w - mute.get_width() - 40, 12))
            surf.blit(mute, (w - mute.get_width() - 32, 16))

        if self._eq_is_blocked():
            self._draw_eq_setup_overlay(surf, w, viz_h)

    def _draw_eq_setup_overlay(self, surf: pygame.Surface, w: int, viz_h: int) -> None:
        """Shown only when the Windows audio effect is not installed yet."""
        panel_w = min(760, w - 48)
        panel_h = min(300, viz_h - 16)
        px = (w - panel_w) // 2
        py = max(8, (viz_h - panel_h) // 2)
        card = pygame.Rect(px, py, panel_w, panel_h)
        shade = pygame.Surface((w, viz_h), pygame.SRCALPHA)
        shade.fill((0, 0, 0, 150))
        surf.blit(shade, (0, 0))
        pygame.draw.rect(surf, (14, 22, 36), card, border_radius=10)
        pygame.draw.rect(surf, (255, 170, 80), card, 2, border_radius=10)

        title = self.font.render("One-time setup, then this is a normal EQ", True, (255, 236, 210))
        surf.blit(title, (px + 18, py + 12))

        lines = [
            "Spectra cannot EQ the speakers by listening to them — that would feed back.",
            "Install the free Windows audio effect (Equalizer APO). Tick Speakers. Reboot.",
            "Then press EQ ON. The knobs change YouTube, games, Spotify — all sound.",
        ]
        y = py + 52
        for line in lines:
            for wrapped in wrap_text(self.font_sm, line, panel_w - 36):
                surf.blit(self.font_sm.render(wrapped, True, (230, 240, 255)), (px + 18, y))
                y += 22

        bw, bh = 240, 42
        gap = 16
        bx = px + (panel_w - (bw * 2 + gap)) // 2
        by = py + panel_h - 62
        self._setup_btn_phones = None
        self._setup_btn_cable = pygame.Rect(bx, by, bw, bh)
        self._setup_btn_retry = pygame.Rect(bx + bw + gap, by, bw, bh)
        self._setup_btn_dismiss = pygame.Rect(px + panel_w - 36, py + 8, 28, 28)

        for rect, label, col in (
            (self._setup_btn_cable, "Install system EQ", (40, 90, 150)),
            (self._setup_btn_retry, "I installed it — EQ ON", (20, 120, 70)),
        ):
            pygame.draw.rect(surf, col, rect, border_radius=6)
            pygame.draw.rect(surf, (180, 220, 255), rect, 1, border_radius=6)
            t = self.font_sm.render(label, True, (255, 255, 255))
            surf.blit(t, (rect.centerx - t.get_width() // 2, rect.centery - t.get_height() // 2))

        pygame.draw.rect(surf, (60, 30, 30), self._setup_btn_dismiss, border_radius=4)
        xmark = self.font_sm.render("X", True, (255, 180, 180))
        surf.blit(
            xmark,
            (
                self._setup_btn_dismiss.centerx - xmark.get_width() // 2,
                self._setup_btn_dismiss.centery - xmark.get_height() // 2,
            ),
        )

    def _draw_preset_ui(self, surf: pygame.Surface) -> None:
        self._load_hits = []
        if self._load_open:
            btn = next((b for b in self.buttons if b.label == "Load"), None)
            names = list(self.user_presets.keys())
            if btn and names:
                mw, row_h = 240, 28
                total = row_h * len(names) + 8
                x = min(btn.rect.x, self.w - mw - 8)
                y = btn.rect.bottom + 4
                if y + total > self.h - 40:
                    y = max(8, btn.rect.y - total - 4)
                panel = pygame.Rect(x, y, mw, total)
                bg = pygame.Surface((panel.w, panel.h), pygame.SRCALPHA)
                bg.fill((8, 16, 24, 240))
                surf.blit(bg, panel.topleft)
                pygame.draw.rect(surf, (120, 220, 170), panel, 1, border_radius=6)
                for i, name in enumerate(names):
                    r = pygame.Rect(x + 4, y + 4 + i * row_h, mw - 8, row_h - 4)
                    self._load_hits.append((r, name))
                    if r.collidepoint(self._hover_pos) or name == self.active_preset:
                        pygame.draw.rect(surf, (28, 78, 64), r, border_radius=4)
                    txt = self.font_sm.render(name, True, (236, 248, 255))
                    surf.blit(txt, (r.x + 8, r.centery - txt.get_height() // 2))
        if self._naming:
            bar = pygame.Rect(16, self.h - 48, min(560, self.w - 32), 36)
            pygame.draw.rect(surf, (10, 22, 28), bar, border_radius=8)
            pygame.draw.rect(surf, (120, 220, 170), bar, 1, border_radius=8)
            shown = self._name_buf or ""
            msg = f"Preset name: {shown}|    Enter saves, Esc cancels"
            txt = self.font_sm.render(msg, True, (230, 248, 255))
            surf.blit(txt, (bar.x + 10, bar.centery - txt.get_height() // 2))

    def draw_panel(self, surf: pygame.Surface) -> None:
        w, h = self.w, self.h
        viz_h = self.viz_h
        panel = pygame.Rect(0, viz_h, w, h - viz_h)
        pygame.draw.rect(surf, (8, 12, 20), panel)
        pygame.draw.rect(surf, (14, 22, 34), pygame.Rect(0, viz_h, w, 70))
        pygame.draw.line(surf, (0, 210, 220), (0, viz_h), (w, viz_h), 2)

        # L/R level meters, short, in the header strip
        mx = w - 22
        my = viz_h + 74
        mh = 36
        for i, (lvl, col) in enumerate(
            (
                (self.audio.level_l, (80, 220, 160)),
                (self.audio.level_r, (120, 170, 255)),
            )
        ):
            bx = mx - 16 + i * 10
            pygame.draw.rect(surf, (16, 24, 36), (bx, my, 6, mh), border_radius=2)
            hgt = int(min(1.0, max(0.0, lvl)) * (mh - 2))
            if hgt > 0:
                pygame.draw.rect(surf, col, (bx + 1, my + mh - 1 - hgt, 4, hgt), border_radius=2)

        for b in self.buttons:
            on = self.audio.affect_sound
            show = b.label
            if b.label in ("EQ ON", "EQ OFF"):
                show = "EQ ON" if on else "EQ OFF"
                bg = (12, 150, 96) if on else (120, 42, 58)
                border = (140, 255, 200) if on else (255, 120, 140)
            elif b.label == "Mute" and self.audio.muted:
                bg = (150, 36, 62)
                border = (255, 140, 160)
            elif b.label == self.active_preset:
                bg = (0, 110, 128)
                border = (0, 230, 240)
            elif b.hot:
                bg = (36, 58, 82)
                border = (90, 170, 200)
            elif b.kind == "adj":
                bg = (22, 40, 36)
                border = (50, 90, 80)
            elif b.kind == "dev":
                bg = (28, 36, 58)
                border = (70, 90, 130)
            elif b.kind == "file":
                bg = (28, 72, 58) if b.hot or (b.label == "Load" and self._load_open) or (b.label == "Save" and self._naming) else (18, 48, 40)
                border = (120, 220, 170)
            elif b.kind == "sys":
                bg = (48, 32, 40)
                border = (120, 70, 80)
            else:
                bg = (18, 28, 42)
                border = (48, 72, 96)
            pygame.draw.rect(surf, bg, b.rect, border_radius=10)
            pygame.draw.rect(surf, border, b.rect, 2, border_radius=10)
            fnt = self.font if b.label in ("EQ ON", "EQ OFF", "Refresh") else self.font_sm
            txt = fnt.render(show, True, (245, 250, 255))
            surf.blit(txt, (b.rect.centerx - txt.get_width() // 2, b.rect.centery - txt.get_height() // 2))

        if self.dd_in:
            self.dd_in.draw(surf, self.font_sm, self.font_tiny)
        if self.dd_out:
            self.dd_out.draw(surf, self.font_sm, self.font_tiny)

        for s in self.sliders:
            s.draw(surf, self.font_sm, self.font_tiny)
        if self.preamp:
            self.preamp.draw(surf, self.font_sm, self.font_tiny)
        if self.master:
            self.master.draw(surf, self.font_sm, self.font_tiny)

        # Dropdown menus on top of everything
        if self.dd_in:
            self.dd_in.draw_menu(surf, self.font_sm)
        if self.dd_out:
            self.dd_out.draw_menu(surf, self.font_sm)
        self._draw_preset_ui(surf)
        self._draw_tooltip(surf)

    def run(self) -> None:
        err_frames = 0
        while self.running:
            try:
                # Auto-reconnect dropped capture streams
                if int(time.time() * 2) % 4 == 0:
                    try:
                        self.audio.watchdog()
                    except Exception:
                        pass
                for event in pygame.event.get():
                    try:
                        self.handle(event)
                    except Exception as exc:
                        log_exception("handle", exc)
                        self.audio.status = f"UI error (logged): {exc}"[:140]
                try:
                    self.draw_viz(self.screen)
                    self.draw_panel(self.screen)
                except Exception as exc:
                    err_frames += 1
                    log_exception("draw", exc)
                    try:
                        self.screen.fill((10, 10, 20))
                        msg = self.font.render("Draw error — see logs/spectra_eq_crash.log", True, (255, 100, 100))
                        self.screen.blit(msg, (40, 40))
                    except Exception:
                        pass
                    if err_frames > 120:
                        # rebuild layout once after repeated draw failures
                        try:
                            self._relayout_keep_gains()
                        except Exception:
                            pass
                        err_frames = 0
                else:
                    err_frames = 0
                pygame.display.flip()
                self.clock.tick(60)
            except Exception as exc:
                log_exception("main_loop", exc)
                time.sleep(0.05)
        try:
            self._save_settings()
        except Exception:
            pass
        try:
            self.audio.stop()
        except Exception as exc:
            log_exception("audio.stop", exc)
        try:
            pygame.quit()
        except Exception:
            pass


class Splash:
    """Startup card. Stays at least 3 seconds, and longer if loading is still going."""

    MIN_SECONDS = 3.0
    W, H = 720, 400

    def __init__(self) -> None:
        os.environ.setdefault("SDL_VIDEO_CENTERED", "1")
        if not pygame.get_init():
            pygame.init()
        apply_window_icon()
        self.screen = pygame.display.set_mode((self.W, self.H), pygame.NOFRAME)
        pygame.display.set_caption("SPECTRA EQ")
        self.clock = pygame.time.Clock()
        self.t0 = time.time()
        self.progress = 0.02
        self.message = "Starting"
        self.font_lg = SpectraEQ._font(46, bold=True)
        self.font = SpectraEQ._font(18)
        self.font_sm = SpectraEQ._font(15)
        self.step(0.06, "Starting")

    def step(self, frac: float, message: str) -> None:
        self.progress = max(self.progress, min(0.97, float(frac)))
        self.message = message
        self._paint()
        self._pump()

    def finish(self) -> None:
        """Loading is done. Hold the card until the 3 second mark, then fill the bar."""
        start_p = self.progress
        while True:
            elapsed = time.time() - self.t0
            if elapsed >= self.MIN_SECONDS:
                self.progress = 1.0
                self.message = "Ready"
                self._paint()
                pygame.time.wait(160)
                return
            self.progress = start_p + (1.0 - start_p) * (elapsed / self.MIN_SECONDS)
            self.message = "Ready"
            self._paint()
            self._pump()
            self.clock.tick(60)

    def _pump(self) -> None:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                pygame.quit()
                sys.exit(0)

    def _paint(self) -> None:
        surf = self.screen
        w, h = self.W, self.H
        surf.fill((6, 10, 18))
        pygame.draw.rect(surf, (0, 210, 220), (0, 0, w, 3))
        pygame.draw.rect(surf, (0, 90, 110), (0, h - 3, w, 3))

        base_y = 118
        for i in range(28):
            x = 70 + i * 21
            t = i / 27.0
            amp = 0.25 + 0.75 * abs(math.sin(t * math.pi))
            bh = int(18 + amp * 78)
            col = hsv(t * 0.78, 0.82, 0.35 + 0.55 * amp)
            pygame.draw.rect(surf, col, pygame.Rect(x, base_y - bh, 14, bh), border_radius=3)

        title = self.font_lg.render("SPECTRA EQ", True, (0, 245, 255))
        surf.blit(title, ((w - title.get_width()) // 2, 168))
        sub = self.font.render("20 Hz  →  10 kHz", True, (170, 210, 225))
        surf.blit(sub, ((w - sub.get_width()) // 2, 224))

        track = pygame.Rect((w - 460) // 2, 286, 460, 12)
        pygame.draw.rect(surf, (16, 28, 40), track, border_radius=6)
        pygame.draw.rect(surf, (40, 80, 100), track, 1, border_radius=6)
        fill_w = max(8, int(track.w * max(0.0, min(1.0, self.progress))))
        fill = pygame.Rect(track.x, track.y, fill_w, track.h)
        pygame.draw.rect(surf, (0, 210, 220), fill, border_radius=6)

        msg = self.font_sm.render(self.message, True, (200, 230, 240))
        surf.blit(msg, ((w - msg.get_width()) // 2, 312))
        ver = self.font_sm.render(APP_VERSION, True, (90, 130, 150))
        surf.blit(ver, ((w - ver.get_width()) // 2, 352))
        pygame.display.flip()


def main() -> None:
    install_crash_handlers()
    multiprocessing.freeze_support()
    print("=" * 56)
    print(f"  SPECTRA EQ  {APP_VERSION}   -   20 Hz → 10 kHz")
    print("=" * 56)
    print(f"Crash log: {CRASH_LOG}")
    print(f"sounddevice: {'OK ' + getattr(sd, '__version__', '') if sd else 'MISSING'}")
    if sc is not None:
        print(f"soundcard:   OK ({getattr(sc, '__file__', '?')})")
    else:
        print(f"soundcard:   optional (missing) — using built-in ctypes WASAPI")
    print(f"ctypes WASAPI: YES (Windows built-in)")
    print(f"WASAPI loopback ready: {'YES' if WASAPI_NATIVE else 'NO'}")
    if sd is None:
        print("Note: sounddevice not installed — viz-only / limited EQ ON.")
        print("      pip install sounddevice numpy pygame")
    splash = Splash()
    try:
        splash.step(0.18, "Finding audio devices")
        ins, outs = list_audio_devices(probe=True)
        print(f"Devices: {len(ins)} capture (speakers loopback), {len(outs)} play")
        for i, d in enumerate(ins[:8]):
            print(f"  CAPTURE[{i}] {d.kind}: {d.name}")
        if len(ins) > 8:
            print(f"  ... +{len(ins) - 8} more")
        for i, d in enumerate(outs[:6]):
            print(f"  OUT[{i}] {d.name}")
    except Exception as exc:
        log_exception("startup_device_probe", exc)

    try:
        app = SpectraEQ(splash)
        app.run()
    except Exception as exc:
        log_exception("fatal_main", exc)
        print(f"\nFatal error — details in:\n  {CRASH_LOG}\n")
        try:
            input("Press Enter to close...")
        except Exception:
            time.sleep(3)
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log_exception("entrypoint", exc)
        sys.exit(1)
