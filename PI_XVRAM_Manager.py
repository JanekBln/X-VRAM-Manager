# X-VRAM Manager v0.3
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

from XPPython3 import xp


PLUGIN_NAME = "X-VRAM Manager"
PLUGIN_SIG = "xvram.manager"
PLUGIN_DESC = "Standalone X-Plane 12 texture-pager tuning"
PLUGIN_VERSION = "0.3.1"


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
        if self._kernel32 and self._base:
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
        # manager.start() already applies the configuration during initial load.
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
