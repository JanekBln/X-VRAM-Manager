# X-VRAM Manager v0.4.1
# Standalone XPPython3 texture-pager tuning for X-Plane 12.
# GPL-3.0-or-later.
#
# This plugin does NOT install a Vulkan layer and does NOT modify X-Plane.exe
# on disk. Optional binary patches are applied in process memory only and are
# guarded by exact-signature checks.

import os
import struct
import ctypes
import configparser
import traceback
import uuid

from XPPython3 import xp


PLUGIN_NAME = "X-VRAM Manager"
PLUGIN_SIG = "xvram.manager"
PLUGIN_DESC = "Standalone X-Plane 12 texture-pager tuning with VRAM monitor"
PLUGIN_VERSION = "0.4.1"


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class _LUID(ctypes.Structure):
    _fields_ = [
        ("LowPart", ctypes.c_uint32),
        ("HighPart", ctypes.c_int32),
    ]


class _DXGI_ADAPTER_DESC1(ctypes.Structure):
    _fields_ = [
        ("Description", ctypes.c_wchar * 128),
        ("VendorId", ctypes.c_uint32),
        ("DeviceId", ctypes.c_uint32),
        ("SubSysId", ctypes.c_uint32),
        ("Revision", ctypes.c_uint32),
        ("DedicatedVideoMemory", ctypes.c_size_t),
        ("DedicatedSystemMemory", ctypes.c_size_t),
        ("SharedSystemMemory", ctypes.c_size_t),
        ("AdapterLuid", _LUID),
        ("Flags", ctypes.c_uint32),
    ]


class _DXGI_QUERY_VIDEO_MEMORY_INFO(ctypes.Structure):
    _fields_ = [
        ("Budget", ctypes.c_uint64),
        ("CurrentUsage", ctypes.c_uint64),
        ("AvailableForReservation", ctypes.c_uint64),
        ("CurrentReservation", ctypes.c_uint64),
    ]


def _guid(text):
    return _GUID.from_buffer_copy(uuid.UUID(text).bytes_le)


def _com_method(ptr, index, restype, *argtypes):
    """Return a callable COM vtable method for a c_void_p interface pointer."""
    table = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    address = ctypes.cast(table[index], ctypes.c_void_p).value
    return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(address)


class XVRAMManager:
    def __init__(self):
        self.plugin_dir = os.path.dirname(os.path.abspath(__file__))
        self.ini_path = os.path.join(self.plugin_dir, "XVRAM_Manager.ini")

        self.cfg = {}
        self.enabled = True

        self._cmd_apply = None
        self._cmd_reload = None
        self._cmd_restore = None

        self._flightloop_registered = False
        self._cycle = 0
        self._frames_in_flight = 0
        self._fallback_ready_logged = False

        self._control_refs = {}
        self._control_orig = {}
        self._control_missing_logged = set()

        self._patches = {}
        self._base = 0
        self._kernel32 = None
        self._process = None

        # Lightweight monitor UI. Hidden by default and opened from the
        # X-Plane Plugins menu. The tuning code itself is unchanged.
        self._window = None
        self._menu = None
        self._dragging = False
        self._drag_mouse = (0, 0)
        self._drag_geometry = None

        # DXGI local-video-memory telemetry for the current X-Plane process.
        self._dxgi_adapter3 = None
        self._dxgi_adapter_name = ""
        self._dxgi_total_bytes = 0
        self._vram_snapshot = None
        self._vram_query_failed_logged = False

        self.load_config()

    # ------------------------------------------------------------------ logging

    def log(self, msg):
        try:
            xp.debugString(f"[X-VRAM] {msg}\n")
        except Exception:
            pass

    # --------------------------------------------------------------- configuration

    def load_config(self):
        cp = configparser.ConfigParser()
        cp.read(self.ini_path, encoding="utf-8")

        def get_bool(sec, key, default):
            try:
                return cp.getboolean(sec, key)
            except Exception:
                return default

        def get_int(sec, key, default):
            try:
                return cp.getint(sec, key)
            except Exception:
                return default

        def get_float(sec, key, default):
            try:
                return cp.getfloat(sec, key)
            except Exception:
                return default

        self.cfg = {
            "enabled": get_bool("general", "enabled", True),
            "restore_on_disable": get_bool("general", "restore_on_disable", True),

            "budget_reserve_mb": get_int("pager", "budget_reserve_mb", 512),
            "fallback_controls": get_bool("pager", "fallback_controls", True),
            "max_overdrive": get_float("pager", "max_overdrive", 64.0),
            "size_fudge_factor": get_float("pager", "size_fudge_factor", 0.75),
            "downscale_cooldown": get_float("pager", "downscale_cooldown", 0.0),
            "reassert_every_frames": max(1, get_int("pager", "reassert_every_frames", 60)),

            "scale_floor": get_float("advanced", "scale_floor", 0.0),
            "scale_floor_delay_frames": max(0, get_int("advanced", "scale_floor_delay_frames", 900)),
        }
        self.enabled = bool(self.cfg["enabled"])

        self.log(
            "config: enabled={} reserve={}MB fallback_controls={} "
            "max_overdrive={} size_fudge={} cooldown={} scale_floor={}".format(
                int(self.enabled),
                self.cfg["budget_reserve_mb"],
                int(self.cfg["fallback_controls"]),
                self.cfg["max_overdrive"],
                self.cfg["size_fudge_factor"],
                self.cfg["downscale_cooldown"],
                self.cfg["scale_floor"],
            )
        )

    # ------------------------------------------------------------ private datarefs

    def _resolve_control(self, path):
        # Do not cache missing refs. X-Plane 12.4.x can register private art
        # controls after XPPython3 plugins have already started.
        ref = self._control_refs.get(path)
        if ref:
            return ref

        try:
            ref = xp.findDataRef(path)
        except Exception:
            ref = None

        if ref:
            self._control_refs[path] = ref
            self._control_missing_logged.discard(path)
            if path not in self._control_orig:
                try:
                    self._control_orig[path] = float(xp.getDataf(ref))
                    self.log(f"control: {path} original={self._control_orig[path]:g}")
                except Exception:
                    pass
        else:
            if path not in self._control_missing_logged:
                self._control_missing_logged.add(path)
                self.log(f"control: {path} not available yet - will retry in flight loop")
        return ref

    def _hold_control(self, path, value):
        # A value of 0 means "leave this optional control alone" for controls
        # where zero is used as an OFF switch in the supplied INI.
        if path.endswith("/downscale_cooldown") and not value:
            return False

        ref = self._resolve_control(path)
        if not ref:
            return False

        try:
            cur = float(xp.getDataf(ref))
            value = float(value)
            if abs(cur - value) > 1e-6:
                xp.setDataf(ref, value)
                self.log(f"control: {path} -> {value:g} (was {cur:g})")
            return True
        except Exception as exc:
            self.log(f"control write failed: {path}: {exc}")
            return False

    def restore_controls(self):
        for path, original in list(self._control_orig.items()):
            ref = self._control_refs.get(path)
            if not ref:
                try:
                    ref = xp.findDataRef(path)
                except Exception:
                    ref = None
            if not ref:
                continue
            try:
                xp.setDataf(ref, float(original))
                self.log(f"control: restored {path}={original:g}")
            except Exception as exc:
                self.log(f"control restore failed: {path}: {exc}")

        self._control_refs.clear()
        self._control_orig.clear()
        self._control_missing_logged.clear()
        self._fallback_ready_logged = False

    # -------------------------------------------------------------- win32 memory

    def _init_win32(self):
        if os.name != "nt":
            self.log("binary patching is Windows-only - skipped")
            return False
        if self._kernel32 && self._base:
            return True

        try:
            self._kernel32 = ctypes.windll.kernel32
            self._process = self._kernel32.GetCurrentProcess()
            self._kernel32.GetModuleHandleW.restype = ctypes.c_void_p
            self._base = int(self._kernel32.GetModuleHandleW(None) or 0)
            if not self._base:
                self.log("could not obtain X-Plane module base")
                return False
            return True
        except Exception as exc:
            self.log(f"Win32 init failed: {exc}")
            return False

    def _read_bytes(self, addr, size):
        return ctypes.string_at(addr, size)

    def _write_bytes(self, addr, data):
        PAGE_EXECUTE_READWRITE = 0x40
        old = ctypes.c_ulong(0)

        if not self._kernel32.VirtualProtect(
            ctypes.c_void_p(addr),
            ctypes.c_size_t(len(data)),
            PAGE_EXECUTE_READWRITE,
            ctypes.byref(old),
        ):
            raise OSError("VirtualProtect failed")

        try:
            ctypes.memmove(ctypes.c_void_p(addr), data, len(data))
            try:
                self._kernel32.FlushInstructionCache(
                    self._process, ctypes.c_void_p(addr), ctypes.c_size_t(len(data))
                )
            except Exception:
                pass
        finally:
            tmp = ctypes.c_ulong(0)
            self._kernel32.VirtualProtect(
                ctypes.c_void_p(addr),
                ctypes.c_size_t(len(data)),
                old.value,
                ctypes.byref(tmp),
            )

    def _parse_sections(self):
        if not self._init_win32():
            return {}

        base = self._base
        if self._read_bytes(base, 2) != b"MZ":
            raise RuntimeError("host module has no MZ header")

        e_lfanew = struct.unpack("<I", self._read_bytes(base + 0x3C, 4))[0]
        nt = base + e_lfanew

        if self._read_bytes(nt, 4) != b"PE\x00\x00":
            raise RuntimeError("host module has no PE header")

        file_header = nt + 4
        num_sections = struct.unpack("<H", self._read_bytes(file_header + 2, 2))[0]
        size_opt = struct.unpack("<H", self._read_bytes(file_header + 16, 2))[0]
        section_table = file_header + 20 + size_opt

        sections = {}
        for i in range(num_sections):
            sh = section_table + i * 40
            raw_name = self._read_bytes(sh, 8).split(b"\x00", 1)[0]
            name = raw_name.decode("ascii", errors="ignore")
            virtual_size = struct.unpack("<I", self._read_bytes(sh + 8, 4))[0]
            virtual_addr = struct.unpack("<I", self._read_bytes(sh + 12, 4))[0]
            if name:
                sections[name] = (base + virtual_addr, max(virtual_size, 1))
        return sections

    def _find_all(self, section_name, pattern, max_hits=10):
        sections = self._parse_sections()
        if section_name not in sections:
            return []

        start, size = sections[section_name]
        hits = []
        chunk_size = 8 * 1024 * 1024
        overlap = max(32, len(pattern) + 8)
        pos = 0

        while pos < size:
            n = min(chunk_size, size - pos)
            data = self._read_bytes(start + pos, n)

            idx = 0
            while True:
                idx = data.find(pattern, idx)
                if idx < 0:
                    break
                addr = start + pos + idx
                if addr not in hits:
                    hits.append(addr)
                    if len(hits) >= max_hits:
                        return hits
                idx += 1

            if pos + n >= size:
                break
            pos += max(1, n - overlap)

        return hits

    def _find_stack_float4_candidates(self):
        # Diagnostic-only scan. Broad matches are NEVER patched.
        sections = self._parse_sections()
        if ".text" not in sections:
            return []

        start, size = sections[".text"]
        hits = []
        chunk_size = 8 * 1024 * 1024
        overlap = 16
        pos = 0

        while pos < size:
            n = min(chunk_size, size - pos)
            data = self._read_bytes(start + pos, n)

            for i in range(0, max(0, len(data) - 8)):
                # C7 44 24 XX 00 00 80 40
                # mov dword ptr [rsp+disp8], 4.0f
                if (
                    data[i:i+3] == b"\xC7\x44\x24"
                    and data[i+4:i+8] == b"\x00\x00\x80\x40"
                ):
                    addr = start + pos + i
                    if addr not in hits:
                        hits.append(addr)
                        if len(hits) >= 32:
                            return hits

            if pos + n >= size:
                break
            pos += max(1, n - overlap)

        return hits

    def _record_and_patch(self, name, addr, patched):
        if name in self._patches:
            return True

        original = self._read_bytes(addr, len(patched))
        self._write_bytes(addr, patched)

        verify = self._read_bytes(addr, len(patched))
        if verify != patched:
            raise RuntimeError(f"{name}: verification failed")

        self._patches[name] = {
            "addr": addr,
            "original": original,
            "patched": patched,
        }
        return True

    def restore_patches(self):
        for name, rec in list(self._patches.items())[::-1]:
            addr = rec["addr"]
            original = rec["original"]
            patched = rec["patched"]

            try:
                current = self._read_bytes(addr, len(patched))
                if current != patched:
                    self.log(
                        f"{name}: current bytes no longer match our patch - "
                        "refusing to overwrite another change"
                    )
                    continue
                self._write_bytes(addr, original)
                self.log(f"{name}: restored original bytes")
            except Exception as exc:
                self.log(f"{name}: restore failed: {exc}")

        self._patches.clear()

    # ---------------------------------------------------------- reserve patch

    def patch_budget_reserve(self):
        if not self.enabled:
            return False

        want_mb = int(self.cfg.get("budget_reserve_mb", 0))
        if want_mb == 0:
            self.log("budget reserve: binary patch disabled in config")
            return False

        if want_mb < 256 or want_mb > 1024:
            self.log(f"budget reserve: {want_mb} MB outside safe 256..1024 range - refusing")
            return False

        if not self._init_win32():
            return False

        # Known signature:
        # C7 44 24 40 00 00 80 40
        # mov dword ptr [rsp+0x40], 4.0f
        # 4.0 * 256 MB = stock 1024 MB reserve.
        sig = bytes([0xC7, 0x44, 0x24, 0x40, 0x00, 0x00, 0x80, 0x40])
        hits = self._find_all(".text", sig, max_hits=3)

        if len(hits) == 1:
            addr = hits[0]
            want_factor = float(want_mb) / 256.0
            patched = sig[:4] + struct.pack("<f", want_factor)
            self._record_and_patch("budget_reserve", addr, patched)

            self.log(
                f"budget reserve: {want_mb} MB instead of 1024 MB; "
                f"+{1024 - want_mb} MB texture budget "
                f"(RAM patch only, RVA +0x{addr - self._base:X})"
            )
            return True

        candidates = self._find_stack_float4_candidates()
        preview = ", ".join(f"+0x{x-self._base:X}" for x in candidates[:8])

        self.log(
            f"budget reserve: known signature hits={len(hits)}; "
            "binary reserve patch skipped safely. "
            f"4.0f stack candidates={len(candidates)}"
            + (f" ({preview})" if preview else "")
        )
        return False

    # ----------------------------------------------------------- optional scale floor

    def patch_scale_floor(self):
        want = float(self.cfg.get("scale_floor", 0.0))
        if want <= 0.0:
            return False

        allowed = [1.0, 0.5, 0.25, 0.125, 0.0625]
        if not any(abs(want - x) < 1e-8 for x in allowed):
            self.log(
                f"scale floor: {want:g} unsupported; use one of "
                "1.0, 0.5, 0.25, 0.125, 0.0625 or 0 to disable"
            )
            return False

        if not self._init_win32():
            return False

        sections = self._parse_sections()
        if ".rdata" not in sections:
            self.log("scale floor: .rdata section missing")
            return False

        # Current known XP12 signature. Requires exact single match.
        floor_sig = bytes.fromhex("F3 44 0F 10 0D 6B 17 F5 00")
        hits = self._find_all(".text", floor_sig, max_hits=3)

        if len(hits) != 1:
            self.log(f"scale floor: signature hits={len(hits)} - refusing")
            return False

        insn = hits[0]
        fallback = insn + 0xEB
        fallback_expected = bytes.fromhex("C7 44 24 48 00 00 80 3D")

        if self._read_bytes(fallback, 8) != fallback_expected:
            self.log("scale floor: fallback signature mismatch - refusing")
            return False

        rstart, rsize = sections[".rdata"]
        target_bytes = struct.pack("<f", want)
        target_addr = None

        # Find an aligned constant with the requested float.
        data = self._read_bytes(rstart, rsize)
        idx = 0
        while True:
            idx = data.find(target_bytes, idx)
            if idx < 0:
                break
            addr = rstart + idx
            if addr % 4 == 0:
                target_addr = addr
                break
            idx += 1

        if not target_addr:
            self.log(f"scale floor: float constant {want:g} not found in .rdata")
            return False

        # MOVSS xmm9, dword ptr [rip+disp32]
        next_ip = insn + 9
        disp = target_addr - next_ip
        if disp < -0x80000000 or disp > 0x7FFFFFFF:
            self.log("scale floor: RIP displacement out of range")
            return False

        patched_floor = floor_sig[:5] + struct.pack("<i", disp)
        patched_fallback = fallback_expected[:4] + struct.pack("<f", want)

        self._record_and_patch("scale_floor_load", insn, patched_floor)
        self._record_and_patch("scale_floor_fallback", fallback, patched_fallback)

        self.log(
            f"scale floor: patched to {want:g} "
            "(warning: restricting emergency downscaling can increase OOM risk)"
        )
        return True


    # -------------------------------------------------------------- VRAM monitor

    def _release_com(self, ptr):
        if not ptr:
            return
        try:
            release = _com_method(ptr, 2, ctypes.c_ulong)
            release(ptr)
        except Exception:
            pass

    def _release_dxgi(self):
        if self._dxgi_adapter3:
            self._release_com(self._dxgi_adapter3)
        self._dxgi_adapter3 = None
        self._dxgi_adapter_name = ""
        self._dxgi_total_bytes = 0
        self._vram_snapshot = None

    def _init_dxgi(self):
        """Resolve the discrete GPU and keep an IDXGIAdapter3 interface."""
        if self._dxgi_adapter3:
            return True
        if os.name != "nt":
            return False

        factory = ctypes.c_void_p()
        try:
            dxgi = ctypes.WinDLL("dxgi.dll")
            create_factory = dxgi.CreateDXGIFactory1
            create_factory.argtypes = [ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p)]
            create_factory.restype = ctypes.c_long

            iid_factory1 = _guid("770aae78-f26f-4dba-a829-253c83d1b387")
            iid_adapter3 = _guid("645967a4-1392-4310-a798-8053ce3e93fd")

            hr = int(create_factory(ctypes.byref(iid_factory1), ctypes.byref(factory)))
            if hr < 0 or not factory:
                return False

            # IDXGIFactory1::EnumAdapters1 is vtable slot 12.
            enum_adapters1 = _com_method(
                factory, 12, ctypes.c_long, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)
            )

            best = None
            best_total = -1
            index = 0
            while index < 32:
                adapter1 = ctypes.c_void_p()
                hr = int(enum_adapters1(factory, index, ctypes.byref(adapter1)))
                if hr < 0 or not adapter1:
                    break

                try:
                    # IDXGIAdapter1::GetDesc1 is vtable slot 10.
                    get_desc1 = _com_method(
                        adapter1, 10, ctypes.c_long, ctypes.POINTER(_DXGI_ADAPTER_DESC1)
                    )
                    desc = _DXGI_ADAPTER_DESC1()
                    if int(get_desc1(adapter1, ctypes.byref(desc))) >= 0:
                        # DXGI_ADAPTER_FLAG_SOFTWARE == 2. Prefer the physical
                        # adapter with the largest dedicated VRAM (RTX 3090 on
                        # the tested machine rather than the integrated GPU).
                        if not (int(desc.Flags) & 2):
                            adapter3 = ctypes.c_void_p()
                            query_interface = _com_method(
                                adapter1,
                                0,
                                ctypes.c_long,
                                ctypes.POINTER(_GUID),
                                ctypes.POINTER(ctypes.c_void_p),
                            )
                            qhr = int(
                                query_interface(
                                    adapter1, ctypes.byref(iid_adapter3), ctypes.byref(adapter3)
                                )
                            )
                            if qhr >= 0 and adapter3:
                                dedicated = int(desc.DedicatedVideoMemory)
                                if dedicated > best_total:
                                    if best:
                                        self._release_com(best[0])
                                    best = (adapter3, str(desc.Description).strip(), dedicated)
                                    best_total = dedicated
                                else:
                                    self._release_com(adapter3)
                finally:
                    self._release_com(adapter1)
                index += 1

            if not best:
                return False

            self._dxgi_adapter3 = best[0]
            self._dxgi_adapter_name = best[1]
            self._dxgi_total_bytes = best[2]
            self.log(
                "VRAM monitor: DXGI adapter={} dedicated={:.2f}GB".format(
                    self._dxgi_adapter_name or "unknown",
                    self._dxgi_total_bytes / (1024.0 ** 3),
                )
            )
            return True
        except Exception as exc:
            if not self._vram_query_failed_logged:
                self._vram_query_failed_logged = True
                self.log(f"VRAM monitor: DXGI init unavailable: {exc}")
            self._release_dxgi()
            return False
        finally:
            if factory:
                self._release_com(factory)

    def _update_vram_snapshot(self):
        if not self._init_dxgi():
            return False
        try:
            info = _DXGI_QUERY_VIDEO_MEMORY_INFO()
            # IDXGIAdapter3::QueryVideoMemoryInfo is vtable slot 14.
            query = _com_method(
                self._dxgi_adapter3,
                14,
                ctypes.c_long,
                ctypes.c_uint32,
                ctypes.c_int,
                ctypes.POINTER(_DXGI_QUERY_VIDEO_MEMORY_INFO),
            )
            # Node 0, DXGI_MEMORY_SEGMENT_GROUP_LOCAL (dedicated/local VRAM).
            hr = int(query(self._dxgi_adapter3, 0, 0, ctypes.byref(info)))
            if hr < 0:
                raise OSError(f"QueryVideoMemoryInfo HRESULT=0x{hr & 0xffffffff:08X}")

            used = int(info.CurrentUsage)
            budget = int(info.Budget)
            self._vram_snapshot = {
                "used": used,
                "budget": budget,
                "headroom": max(0, budget - used),
                "total": int(self._dxgi_total_bytes),
            }
            self._vram_query_failed_logged = False
            return True
        except Exception as exc:
            if not self._vram_query_failed_logged:
                self._vram_query_failed_logged = True
                self.log(f"VRAM monitor: query failed: {exc}")
            return False

    @staticmethod
    def _fmt_gb(value):
        if value is None:
            return "---"
        return f"{float(value) / (1024.0 ** 3):.2f} GB"

    def _tool_is_active(self):
        if not self.enabled:
            return False
        if self.cfg.get("fallback_controls", True):
            return bool(self._fallback_ready_logged)
        return bool(self._patches) or self.enabled

    def _create_monitor_ui(self):
        try:
            self._menu = xp.createMenu(
                name="X-VRAM Manager", parentMenuID=None, parentItem=0,
                handler=self._menu_handler, refCon=None
            )
            if self._menu:
                xp.appendMenuItem(self._menu, "VRAM Monitor", refCon="toggle_monitor")
                xp.appendMenuSeparator(self._menu)
                xp.appendMenuItem(self._menu, "Apply", refCon="apply")
                xp.appendMenuItem(self._menu, "Reload Config", refCon="reload")
                xp.appendMenuItem(self._menu, "Restore Stock", refCon="restore")
        except Exception as exc:
            self.log(f"VRAM monitor: menu creation failed: {exc}")
            self._menu = None

        try:
            try:
                screen_w, screen_h = xp.getScreenSize()
            except Exception:
                screen_w, screen_h = (1920, 1080)

            # Fixed-size panel: movable, intentionally not resizable.
            width, height = 360, 220
            left = 40
            top = int(screen_h) - 80

            decoration = getattr(
                xp,
                "WindowDecorationSelfDecorated",
                getattr(xp, "WindowDecorationNone", 0),
            )
            self._window = xp.createWindowEx(
                left=left,
                top=top,
                right=left + width,
                bottom=top - height,
                visible=0,
                draw=self._draw_monitor,
                click=self._monitor_click,
                key=self._monitor_key,
                cursor=self._monitor_cursor,
                wheel=self._monitor_wheel,
                refCon=None,
                decoration=decoration,
                layer=xp.WindowLayerFloatingWindows,
                rightClick=self._monitor_right_click,
            )
            if self._window:
                try:
                    xp.setWindowPositioningMode(self._window, xp.WindowPositionFree, -1)
                except Exception:
                    pass
        except Exception as exc:
            self.log(f"VRAM monitor: window creation failed: {exc}")
            self._window = None

    def _destroy_monitor_ui(self):
        if self._window:
            try:
                xp.destroyWindow(self._window)
            except Exception:
                pass
            self._window = None

        if self._menu:
            try:
                xp.destroyMenu(self._menu)
            except Exception:
                pass
            self._menu = None

        self._dragging = False
        self._drag_geometry = None

    def _menu_handler(self, menuRefCon, itemRefCon):
        if itemRefCon == "toggle_monitor":
            self.toggle_monitor()
        elif itemRefCon == "apply":
            self._ui_apply()
        elif itemRefCon == "reload":
            self._ui_reload()
        elif itemRefCon == "restore":
            self._ui_restore()

    def _ui_apply(self):
        self.log("ui: apply")
        self.apply_all()

    def _ui_reload(self):
        self.log("ui: reload_config")
        self.restore_all()
        self.apply_all()

    def _ui_restore(self):
        self.log("ui: restore_stock")
        self.restore_all()

    def toggle_monitor(self):
        if not self._window:
            return
        try:
            visible = bool(xp.getWindowIsVisible(self._window))
            if not visible:
                self._update_vram_snapshot()
                xp.setWindowIsVisible(self._window, 1)
                try:
                    xp.bringWindowToFront(self._window)
                except Exception:
                    pass
            else:
                xp.setWindowIsVisible(self._window, 0)
        except Exception as exc:
            self.log(f"VRAM monitor: toggle failed: {exc}")

    def _draw_monitor(self, windowID, refCon):
        left, top, right, bottom = xp.getWindowGeometry(windowID)

        xp.drawTranslucentDarkBox(left, top, right, bottom)

        white = (1.0, 1.0, 1.0)
        green = (0.15, 1.0, 0.15)
        red = (1.0, 0.18, 0.18)
        dim = (0.72, 0.72, 0.72)
        font = xp.Font_Basic

        pad = 12
        header_y = top - 22
        xp.drawString(white, left + pad, header_y, f"X-VRAM  v{PLUGIN_VERSION}", None, font)
        xp.drawString(white, right - 20, header_y, "X", None, font)

        active = self._tool_is_active()
        lamp_color = green if active else red
        status_text = "ACTIVE" if active else "OFF"
        # Solid dot glyph used as the LED instead of the previous O/0.
        xp.drawString(lamp_color, left + pad, top - 45, "●", None, font)
        xp.drawString(white, left + pad + 18, top - 45, status_text, None, font)

        snap = self._vram_snapshot or {}
        rows = [
            ("VRAM USED", self._fmt_gb(snap.get("used"))),
            ("VRAM BUDGET", self._fmt_gb(snap.get("budget"))),
            ("HEADROOM", self._fmt_gb(snap.get("headroom"))),
            ("GPU VRAM", self._fmt_gb(snap.get("total"))),
        ]

        y = top - 69
        value_x = left + 170
        for label, value in rows:
            xp.drawString(white, left + pad, y, label, None, font)
            xp.drawString(white, value_x, y, value, None, font)
            y -= 20

        pager = self.cfg.get("max_overdrive", 64.0)
        fudge = self.cfg.get("size_fudge_factor", 0.75)
        xp.drawString(dim, left + pad, bottom + 55, f"PAGER {pager:g}   FUDGE {fudge:g}", None, font)

        # Bottom action row. Black/white styling, matching the monitor.
        xp.drawString(white, left + 18, bottom + 27, "[ APPLY ]", None, font)
        xp.drawString(white, left + 118, bottom + 27, "[ RELOAD ]", None, font)
        xp.drawString(white, left + 232, bottom + 27, "[ RESTORE ]", None, font)

    def _monitor_click(self, windowID, x, y, mouseStatus, refCon):
        try:
            left, top, right, bottom = xp.getWindowGeometry(windowID)

            if mouseStatus == xp.MouseDown:
                # Small custom close box in the upper-right corner.
                if (right - 34) <= x <= right and (top - 30) <= y <= top:
                    xp.setWindowIsVisible(windowID, 0)
                    self._dragging = False
                    return 1

                # Action buttons.
                if (bottom + 12) <= y <= (bottom + 43):
                    if (left + 8) <= x <= (left + 102):
                        self._ui_apply()
                        return 1
                    if (left + 105) <= x <= (left + 224):
                        self._ui_reload()
                        return 1
                    if (left + 226) <= x <= (right - 8):
                        self._ui_restore()
                        return 1

                # Drag anywhere in the title strip except the close box.
                if (top - 30) <= y <= top and x < (right - 34):
                    self._dragging = True
                    self._drag_mouse = (x, y)
                    self._drag_geometry = (left, top, right, bottom)
                    return 1

            elif mouseStatus == xp.MouseDrag and self._dragging and self._drag_geometry:
                sx, sy = self._drag_mouse
                gl, gt, gr, gb = self._drag_geometry
                dx = x - sx
                dy = y - sy
                xp.setWindowGeometry(windowID, gl + dx, gt + dy, gr + dx, gb + dy)
                return 1

            elif mouseStatus == xp.MouseUp:
                self._dragging = False
                self._drag_geometry = None
                return 1
        except Exception:
            self._dragging = False
            self._drag_geometry = None
        return 1

    def _monitor_key(self, windowID, key, flags, vKey, refCon, losingFocus):
        return None

    def _monitor_cursor(self, windowID, x, y, refCon):
        return xp.CursorDefault

    def _monitor_wheel(self, windowID, x, y, wheel, clicks, refCon):
        return 1

    def _monitor_right_click(self, windowID, x, y, mouseStatus, refCon):
        return 1

    # ------------------------------------------------------------ application logic

    def apply_all(self):
        self.load_config()
        if not self.enabled:
            self.log("disabled by config")
            return

        reserve_ok = False
        try:
            reserve_ok = self.patch_budget_reserve()
        except Exception as exc:
            self.log(f"budget reserve patch exception: {exc}")
            self.log(traceback.format_exc().replace("\n", " | "))

        self._fallback_ready_logged = False
        self._frames_in_flight = 0
        self._cycle = 0

        if self.cfg.get("fallback_controls", True):
            self.log("pager fallback controls: waiting for X-Plane to register private controls")

        # scale_floor is deliberately delayed and OFF by default.
        if reserve_ok:
            pass

    def restore_all(self):
        if self.cfg.get("restore_on_disable", True):
            self.restore_controls()
            self.restore_patches()

    # -------------------------------------------------------------- flight loop

    def flight_loop(self, sinceLast, elapsedTime, counter, refcon):
        self._cycle += 1
        self._frames_in_flight += 1

        if self._window and self._cycle % 30 == 0:
            try:
                if xp.getWindowIsVisible(self._window):
                    self._update_vram_snapshot()
            except Exception:
                pass

        if self.enabled and self.cfg.get("fallback_controls", True):
            # Retry more often during the first 600 callbacks, then use the
            # configured reassert interval.
            interval = 15 if self._cycle < 600 else int(self.cfg.get("reassert_every_frames", 60))

            if self._cycle % max(1, interval) == 0:
                self._hold_control(
                    "sim/private/controls/tex/paging/max_overdrive",
                    self.cfg.get("max_overdrive", 64.0),
                )
                self._hold_control(
                    "sim/private/controls/tex/paging/size_fudge_factor",
                    self.cfg.get("size_fudge_factor", 0.75),
                )
                self._hold_control(
                    "sim/private/controls/tex/paging/downscale_cooldown",
                    self.cfg.get("downscale_cooldown", 0.0),
                )

                wanted = [
                    "sim/private/controls/tex/paging/max_overdrive",
                    "sim/private/controls/tex/paging/size_fudge_factor",
                ]
                ready = all(self._control_refs.get(p) for p in wanted)

                if ready and not self._fallback_ready_logged:
                    self._fallback_ready_logged = True
                    self.log("pager fallback controls: ACTIVE (late-resolved)")

        want_floor = float(self.cfg.get("scale_floor", 0.0))
        delay = int(self.cfg.get("scale_floor_delay_frames", 900))
        if (
            self.enabled
            and want_floor > 0.0
            and self._frames_in_flight >= delay
            and "scale_floor_load" not in self._patches
        ):
            try:
                self.patch_scale_floor()
            except Exception as exc:
                self.log(f"scale floor patch exception: {exc}")

        return -1.0

    # -------------------------------------------------------------- commands

    def command_apply(self, cmdRef, phase, refCon):
        if phase == 0:
            self.log("command: apply")
            self.apply_all()
        return 1

    def command_reload(self, cmdRef, phase, refCon):
        if phase == 0:
            self.log("command: reload_config")
            self.restore_all()
            self.apply_all()
        return 1

    def command_restore(self, cmdRef, phase, refCon):
        if phase == 0:
            self.log("command: restore_stock")
            self.restore_all()
        return 1

    # -------------------------------------------------------------- lifecycle

    def start(self):
        self.log(f"v{PLUGIN_VERSION} starting; X-Plane/XPLM/host={self._versions()}")

        self._cmd_apply = xp.createCommand("xvram/apply", "X-VRAM: apply current configuration")
        self._cmd_reload = xp.createCommand("xvram/reload_config", "X-VRAM: reload INI and apply")
        self._cmd_restore = xp.createCommand("xvram/restore_stock", "X-VRAM: restore captured stock values")

        xp.registerCommandHandler(self._cmd_apply, self.command_apply, 1, None)
        xp.registerCommandHandler(self._cmd_reload, self.command_reload, 1, None)
        xp.registerCommandHandler(self._cmd_restore, self.command_restore, 1, None)

        try:
            xp.registerFlightLoopCallback(self.flight_loop, -1.0, None)
            self._flightloop_registered = True
        except Exception as exc:
            self.log(f"flight loop registration failed: {exc}")

        self._create_monitor_ui()
        self.apply_all()

    def stop(self):
        try:
            self.restore_all()
        except Exception:
            pass

        if self._flightloop_registered:
            try:
                xp.unregisterFlightLoopCallback(self.flight_loop, None)
            except Exception:
                pass
            self._flightloop_registered = False

        self._destroy_monitor_ui()
        self._release_dxgi()

        for cmd, handler in [
            (self._cmd_apply, self.command_apply),
            (self._cmd_reload, self.command_reload),
            (self._cmd_restore, self.command_restore),
        ]:
            if cmd:
                try:
                    xp.unregisterCommandHandler(cmd, handler, 1, None)
                except Exception:
                    pass

    def enable(self):
        self.apply_all()
        return 1

    def disable(self):
        self.restore_all()

    def receive_message(self, fromWho, message, param):
        pass

    def _versions(self):
        try:
            # xp.getVersions() normally returns X-Plane version, XPLM version, host ID.
            return xp.getVersions()
        except Exception:
            return "unknown"


class PythonInterface:
    """XPPython3 plugin entry point."""

    def __init__(self):
        self.manager = None
        self._initial_enable_pending = True

    def XPluginStart(self):
        self.manager = XVRAMManager()
        self.manager.start()
        return PLUGIN_NAME, PLUGIN_SIG, PLUGIN_DESC

    def XPluginStop(self):
        if self.manager:
            self.manager.stop()
        self.manager = None

    def XPluginEnable(self):
        # start() already applies the configuration during initial load.
        # Avoid applying it twice on XPPython3's immediate first Enable call.
        if self._initial_enable_pending:
            self._initial_enable_pending = False
            return 1

        if self.manager:
            return self.manager.enable()
        return 1

    def XPluginDisable(self):
        if self.manager:
            self.manager.disable()

    def XPluginReceiveMessage(self, inFromWho, inMessage, inParam):
        if self.manager:
            self.manager.receive_message(inFromWho, inMessage, inParam)

