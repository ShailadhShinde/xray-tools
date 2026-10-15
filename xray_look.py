#!/usr/bin/env python3
"""
xray_look.py - look inside a container, for ANY project: what is running, which files are open, which libraries are
loaded and where each one comes from (an apt package, a pip package, the GPU driver of your PC), network, GPU.
Runs INSIDE the container (Linux), Python standard library only. It only reads; nothing is changed.

  python xray_look.py              one look at the running program(s)
  python xray_look.py --watch 5    look every 5 seconds and print what CHANGED: files opened / closed, libraries
                                   loaded, connections, RAM - Ctrl+C to stop
  python xray_look.py --image      no program needed: Python, the biggest packages, the system libraries your pip
                                   packages need (MISSING ones + the apt package that has them), CUDA libraries,
                                   does every package import (and why not), GPU test
  python xray_look.py --pid 25     only that process          --all   every library, not only a summary

Get it into the container:
  X-Ray's run files mount the tools at /xray:   docker exec -it NAME python /xray/xray_look.py
  any other container:   docker cp xray_look.py NAME:/tmp/xray_look.py
                         docker exec -it NAME python /tmp/xray_look.py
"""
import argparse
import glob
import os
import re
import shutil
import subprocess
import sys
import time

VERSION = "0.1.1"
CUDA_RE = re.compile(r"lib(cuda|cudart|cublas|cublasLt|cudnn|cufft|curand|cusparse|cusolver|nvrtc|nccl|nvJitLink|"
                     r"nvinfer|onnxruntime_providers_cuda|onnxruntime_providers_tensorrt|torch_cuda|c10_cuda)", re.I)
DRIVER_RE = re.compile(r"/libcuda\.so|/libnvidia-|/libnvcuvid|/libdxcore|/usr/lib/wsl/")
BASE_RE = re.compile(r"/(libc|libm|libdl|libpthread|librt|libutil|ld-linux[^/]*|libgcc_s|libstdc\+\+|libresolv|"
                     r"libnss_[a-z]+)\.so")
# common system libraries -> the Debian / Ubuntu package that has them (for MISSING ones)
APT = {"libGL.so.1": "libgl1", "libEGL.so.1": "libegl1", "libGLX.so.0": "libglx0", "libOpenGL.so.0": "libopengl0",
       "libglib-2.0.so.0": "libglib2.0-0 (Ubuntu 24.04: libglib2.0-0t64)", "libgthread-2.0.so.0": "libglib2.0-0",
       "libgobject-2.0.so.0": "libglib2.0-0", "libX11.so.6": "libx11-6", "libXext.so.6": "libxext6",
       "libXrender.so.1": "libxrender1", "libSM.so.6": "libsm6", "libICE.so.6": "libice6", "libxcb.so.1": "libxcb1",
       "libxkbcommon.so.0": "libxkbcommon0", "libXau.so.6": "libxau6", "libXdmcp.so.6": "libxdmcp6",
       "libfontconfig.so.1": "libfontconfig1", "libfreetype.so.6": "libfreetype6", "libdbus-1.so.3": "libdbus-1-3",
       "libgomp.so.1": "libgomp1", "libgfortran.so.5": "libgfortran5", "libquadmath.so.0": "libquadmath0",
       "libpq.so.5": "libpq5", "libsndfile.so.1": "libsndfile1", "libportaudio.so.2": "libportaudio2",
       "libasound.so.2": "libasound2", "libzbar.so.0": "libzbar0", "libmagic.so.1": "libmagic1",
       "libnuma.so.1": "libnuma1", "libjpeg.so.8": "libjpeg-turbo8", "libjpeg.so.62": "libjpeg62-turbo",
       "libpng16.so.16": "libpng16-16", "libtiff.so.6": "libtiff6", "libwebp.so.7": "libwebp7",
       "libavcodec.so": "ffmpeg", "libssl.so.3": "libssl3", "libcrypto.so.3": "libssl3", "libffi.so.8": "libffi8",
       "libz.so.1": "zlib1g", "libbz2.so.1.0": "libbz2-1.0", "liblzma.so.5": "liblzma5", "libxml2.so.2": "libxml2",
       "libmariadb.so.3": "libmariadb3", "libodbc.so.2": "libodbc2", "libgdal.so": "libgdal-dev",
       "libcairo.so.2": "libcairo2", "libpango-1.0.so.0": "libpango-1.0-0", "libusb-1.0.so.0": "libusb-1.0-0"}


def read(path, mode="r"):
    try:
        with open(path, mode) as fh:
            return fh.read()
    except (OSError, UnicodeDecodeError):
        return None


def mb(n):
    return f"{n / 1048576:,.0f} MB" if n >= 1048576 else f"{max(1, n // 1024)} KB"


# --------------------------------------------------------------------------- where a file comes from
class Sources:
    """file path -> 'apt:PKG' / 'pip:DIST' / 'driver' / 'python' / 'base' / 'image'"""

    def __init__(self):
        self.dpkg = None
        self.pip = {}
        self.sites = set()
        self.mounts = mounts()

    def load_dpkg(self):
        self.dpkg = {}
        for lst in glob.glob("/var/lib/dpkg/info/*.list"):
            pkg = os.path.basename(lst)[:-5].split(":")[0]
            for p in (read(lst) or "").splitlines():
                self.dpkg[p] = pkg
                if p.startswith("/lib"):
                    self.dpkg["/usr" + p] = pkg
                elif p.startswith("/usr/lib"):
                    self.dpkg[p[4:]] = pkg

    def add_site(self, d):
        if d in self.sites or not os.path.isdir(d):
            return
        self.sites.add(d)
        for rec in glob.glob(os.path.join(d, "*.dist-info", "RECORD")):
            dist = re.sub(r"-[^-]+\.dist-info$", "", os.path.basename(os.path.dirname(rec)))
            for line in (read(rec) or "").splitlines():
                rel = line.split(",")[0]
                if rel:
                    self.pip[os.path.normpath(os.path.join(d, rel))] = dist

    def of(self, path):
        m = re.match(r"(.*?/(?:site|dist)-packages)/", path)
        if m:
            self.add_site(m.group(1))
            return "pip:" + self.pip.get(os.path.normpath(path), "?")
        if self.dpkg is None:
            self.load_dpkg()
        if path in self.dpkg:
            return "apt:" + self.dpkg[path]
        if DRIVER_RE.search(path) or any(path == m or path.startswith(m.rstrip("/") + "/") for m in self.mounts):
            return "driver" if DRIVER_RE.search(path) else "mounted"
        if "/lib-dynload/" in path or re.search(r"/libpython3", path):
            return "python"
        if BASE_RE.search(path):
            return "base"
        return "image"


def mounts():
    """mount points that came from outside the image (docker -v, --gpus all), not /proc, /sys, /dev ..."""
    out = []
    for line in (read("/proc/self/mountinfo") or "").splitlines():
        f = line.split()
        if len(f) < 5:
            continue
        mp = f[4].replace("\\040", " ")
        if mp == "/" or re.match(r"^/(proc|sys|dev)(/|$)", mp) or mp in ("/etc/hostname", "/etc/hosts",
                                                                       "/etc/resolv.conf"):
            continue
        out.append(mp)
    return out


# --------------------------------------------------------------------------- processes
def procs():
    me, out = os.getpid(), {}
    for d in glob.glob("/proc/[0-9]*"):
        pid = int(d[6:])
        cmd = (read(f"{d}/cmdline") or "").replace("\0", " ").strip()
        st = read(f"{d}/stat")
        if not cmd or not st or pid == me:
            continue
        rest = st.rsplit(")", 1)[1].split()
        status = read(f"{d}/status") or ""
        val = lambda k: int(re.search(rf"^{k}:\s+(\d+)", status, re.M).group(1)) if re.search(
            rf"^{k}:\s+(\d+)", status, re.M) else 0
        out[pid] = {"pid": pid, "ppid": int(rest[1]), "cmd": cmd, "cpu": int(rest[11]) + int(rest[12]),
                    "rss": val("VmRSS") * 1024, "peak": val("VmHWM") * 1024, "threads": val("Threads")}
    return out


def app_pids(ps, want):
    if want:
        return [want] if want in ps else []
    py = [p for p, x in ps.items() if re.search(r"python", x["cmd"].split()[0] if x["cmd"] else "")
          and "xray_look" not in x["cmd"] and "xray_trace.py run" not in x["cmd"]]
    return sorted(py) or sorted(p for p, x in ps.items() if not re.match(r"^(/bin/|/usr/bin/)?(ba)?sh\b", x["cmd"]))


def net_table():
    """socket inode -> (proto, local, remote, state)"""
    t, states = {}, {"01": "ESTABLISHED", "0A": "LISTEN", "02": "SYN_SENT", "06": "TIME_WAIT", "08": "CLOSE_WAIT"}

    def addr(h):
        ip, port = h.split(":")
        if len(ip) == 8:
            ip = ".".join(str(int(ip[i:i + 2], 16)) for i in (6, 4, 2, 0))
        else:
            ip = "[" + ":".join(ip[i:i + 4] for i in range(0, 32, 4)) + "]"
        return f"{ip}:{int(port, 16)}"
    for proto in ("tcp", "tcp6", "udp", "udp6"):
        for line in (read(f"/proc/net/{proto}") or "").splitlines()[1:]:
            f = line.split()
            if len(f) > 9:
                t[f[9]] = (proto, addr(f[1]), addr(f[2]), states.get(f[3], f[3]) if proto.startswith("tcp") else "")
    return t


def look_pid(pid, src):
    """files open now, libraries loaded, sockets - for one process"""
    files, socks = {}, []
    for fd in glob.glob(f"/proc/{pid}/fd/*"):
        try:
            tgt = os.readlink(fd)
        except OSError:
            continue
        if tgt.startswith("socket:["):
            socks.append(tgt[8:-1])
            continue
        if not tgt.startswith("/") or tgt.startswith(("/dev/null", "/dev/pts", "/dev/urandom", "/dev/random",
                                                      "/proc/")):
            continue
        flags = re.search(r"flags:\s+(\d+)", read(f"/proc/{pid}/fdinfo/{os.path.basename(fd)}") or "")
        files[tgt] = bool(flags and int(flags.group(1), 8) & 3)          # O_WRONLY / O_RDWR
    libs = set()
    for line in (read(f"/proc/{pid}/maps") or "").splitlines():
        p = line.split(None, 5)[-1] if len(line.split(None, 5)) == 6 else ""
        if ".so" in p and p.startswith("/"):
            libs.add(p.replace(" (deleted)", ""))
    return files, libs, socks


def file_note(path, writing, src):
    tags = []
    if path.startswith("/dev/nvidia"):
        tags.append("GPU device")
    elif path.startswith("/dev/video"):
        tags.append("camera")
    elif path.startswith("/dev/shm/"):
        tags.append("shared memory")
    elif any(path == m or path.startswith(m.rstrip("/") + "/") for m in src.mounts):
        tags.append("mounted from your PC")
    if writing:
        tags.append("WRITING")
    try:
        if os.path.isfile(path):
            tags.append(mb(os.path.getsize(path)))
    except OSError:
        pass
    return f"{path}" + (f"   [{', '.join(tags)}]" if tags else "")


def lib_summary(libs, src, show_all):
    groups = {}
    for p in sorted(libs):
        groups.setdefault(src.of(p), []).append(p)
    L = [f"  LIBRARIES LOADED ({len(libs)}), by where they come from:"]
    order = sorted(groups, key=lambda k: ({"pip": 0, "apt": 1, "driver": 2, "mounted": 3, "image": 4, "python": 5,
                                           "base": 6}.get(k.split(":")[0], 7), k))
    label = {"driver": "the GPU driver of your PC (mounted by --gpus all, never in the image)",
             "mounted": "mounted from outside the image", "image": "in the image, not from apt or pip (copied in / built)",
             "python": "Python itself", "base": "C / C++ runtime (in every image)"}
    for k in order:
        names = [os.path.basename(p) + (" [CUDA]" if CUDA_RE.search(os.path.basename(p)) else "") for p in groups[k]]
        kind, _, name = k.partition(":")
        head = (f"pip package {name}" if kind == "pip" else f"apt package {name}" if kind == "apt" else label[kind])
        if kind in ("python", "base") and not show_all:
            L.append(f"    {head}: {len(names)}")
            continue
        shown = names if show_all or len(names) <= 6 else names[:5] + [f"... {len(names) - 5} more (--all)"]
        L.append(f"    {head}: " + ", ".join(shown))
    cuda = [os.path.basename(p) for p in libs if CUDA_RE.search(os.path.basename(p))]
    L.append("  GPU: " + (f"{len(cuda)} CUDA libraries loaded - the program is on the GPU path" if cuda else
                          "no CUDA library loaded - the program runs on the CPU (or has not reached its GPU code yet)"))
    return L


def gpu_lines():
    if not shutil.which("nvidia-smi"):
        return ["  GPU: no nvidia-smi here (the container was started without --gpus all, or it is a CPU image)"]
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=name,utilization.gpu,memory.used,memory.total",
                              "--format=csv,noheader"], capture_output=True, text=True, timeout=20).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        out = ""
    return ["  GPU (whole card): " + (out or "nvidia-smi gave no answer")]


def one_look(a):
    src, ps = Sources(), procs()
    pids = app_pids(ps, a.pid)
    time.sleep(1.0)
    ps2 = procs()
    net = net_table()
    L = [f"xray_look {VERSION} - {time.strftime('%H:%M:%S')} - {len(ps)} processes in this container", "",
         "  PID   PPID  CPU%   RAM now  RAM peak  threads  command"]
    for p in sorted(ps):
        x = ps[p]
        cpu = (ps2.get(p, x)["cpu"] - x["cpu"]) / os.sysconf("SC_CLK_TCK") * 100
        L.append(f"  {p:<5} {x['ppid']:<5} {cpu:4.0f}  {mb(x['rss']):>8}  {mb(x['peak']):>8}  {x['threads']:>7}  "
                 f"{x['cmd'][:110]}")
    for p in pids:
        files, libs, socks = look_pid(p, src)
        L += ["", f"=== PID {p}: {ps[p]['cmd'][:120]}", f"  FILES OPEN NOW ({len(files)}):"]
        L += ["    " + file_note(f, w, src) for f, w in sorted(files.items())] or ["    (none)"]
        conns = [net[i] for i in socks if i in net]
        L.append(f"  NETWORK ({len(conns)}):")
        L += [f"    {pr:<5} {('listening on ' + lo) if st == 'LISTEN' else (lo + '  ->  ' + re_)}  {st}"
              for pr, lo, re_, st in conns] or ["    (none)"]
        L += lib_summary(libs, src, a.all)
    L += ["", *gpu_lines(),
          "  Which Python code runs right now: pip install py-spy  then  py-spy dump --pid PID   (needs --cap-add "
          "SYS_PTRACE, X-Ray's explore run has it)"]
    print("\n".join(L), flush=True)


def watch(a):
    src, prev = Sources(), {}
    print(f"xray_look {VERSION} - watching every {a.watch:g} s, printing what changes (Ctrl+C to stop)", flush=True)
    try:
        while True:
            ps, net = procs(), net_table()
            now, out = {}, []
            for p in app_pids(ps, a.pid):
                files, libs, socks = look_pid(p, src)
                conns = {f"{net[i][1]} -> {net[i][2]} {net[i][3]}" for i in socks if i in net}
                now[p] = (files, libs, conns, ps[p]["rss"])
                f0, l0, c0, r0 = prev.get(p, ({}, set(), set(), None))
                if p not in prev:
                    out.append(f"  + process {p}: {ps[p]['cmd'][:100]}  (RAM {mb(ps[p]['rss'])})")
                out += [f"  + opened  {file_note(f, w, src)}" for f, w in files.items() if f not in f0]
                out += [f"  - closed  {f}" for f in f0 if f not in files]
                out += [f"  + loaded  {os.path.basename(x)}  ({src.of(x).replace(':', ' ', 1)})" +
                        ("  [CUDA]" if CUDA_RE.search(os.path.basename(x)) else "") for x in sorted(libs - l0)]
                out += [f"  + network {c}" for c in sorted(conns - c0)]
                if r0 is not None and abs(ps[p]["rss"] - r0) > 50 * 1048576:
                    out.append(f"  ~ RAM of {p}: {mb(r0)} -> {mb(ps[p]['rss'])}")
            out += [f"  - process {p} ended" for p in prev if p not in now]
            if out:
                print(f"[{time.strftime('%H:%M:%S')}]", *out, sep="\n", flush=True)
            prev = now
            time.sleep(a.watch)
    except KeyboardInterrupt:
        print("stopped")


# --------------------------------------------------------------------------- the image, no program needed
def look_image(a):
    src = Sources()
    sites = [p for p in sys.path if p.endswith(("site-packages", "dist-packages")) and os.path.isdir(p)]
    L = [f"xray_look {VERSION} --image", "", f"  Python: {sys.executable}  {sys.version.split()[0]}  (prefix {sys.prefix})",
         f"  packages folder: {', '.join(sites) or '?'}",
         f"  PATH: {os.environ.get('PATH', '')}",
         "  LD_LIBRARY_PATH (extra folders searched for libraries): " + (os.environ.get("LD_LIBRARY_PATH") or "(not set)")]
    for d in (os.environ.get("LD_LIBRARY_PATH") or "").split(":"):
        if d:
            L.append(f"    {d}  {'ok' if os.path.isdir(d) else 'MISSING - this folder does not exist'}")
    # biggest pip packages (sizes from RECORD)
    sizes = {}
    for d in sites:
        for rec in glob.glob(os.path.join(d, "*.dist-info", "RECORD")):
            dist = re.sub(r"-[^-]+\.dist-info$", "", os.path.basename(os.path.dirname(rec)))
            sizes[dist] = sum(int(x.split(",")[2]) for x in (read(rec) or "").splitlines()
                              if x.count(",") >= 2 and x.split(",")[2].isdigit())
    top = sorted(sizes.items(), key=lambda kv: -kv[1])
    L += ["", f"  PIP PACKAGES: {len(sizes)}, {mb(sum(sizes.values()))} in all; the biggest:"]
    L += [f"    {n:<34} {mb(s):>9}" for n, s in top[:15]]
    # system libraries the compiled parts of the pip packages need
    sos = []
    for d in sites:
        for root, _dirs, fs in os.walk(d):
            sos += [os.path.join(root, f) for f in fs if re.search(r"\.so(\.\d+)*$", f)]
    need, missing = {}, {}
    # ldd on a library a wheel bundles (numpy.libs/libopenblas...) says "not found" for its bundled neighbours: the
    # package's module finds them through its own search path when it loads them - they are not missing
    bundled = {os.path.basename(f) for f in sos}
    if shutil.which("ldd"):
        for i in range(0, min(len(sos), 6000), 200):
            try:
                out = subprocess.run(["ldd"] + sos[i:i + 200], capture_output=True, text=True, timeout=300).stdout
            except (OSError, subprocess.SubprocessError):
                continue
            cur = None
            for line in out.splitlines():
                if not line.startswith(("\t", " ")) and line.endswith(":"):
                    cur = line[:-1]
                    continue
                m = re.match(r"\s+(\S+) => (not found|(\S+))", line)
                if not m or cur is None:
                    continue
                owner = src.of(cur).split(":", 1)[-1]
                if m.group(2) == "not found":
                    if m.group(1) in bundled:     # a pip package's own copy (numpy.libs/...): found at run time
                        continue
                    missing.setdefault(m.group(1), set()).add(f"{owner} ({os.path.basename(cur)})")
                elif "site-packages" not in m.group(3) and "dist-packages" not in m.group(3):
                    k = src.of(os.path.realpath(m.group(3)))
                    if k.startswith("apt:"):
                        need.setdefault(k[4:], set()).add(owner)
    L += ["", f"  SYSTEM LIBRARIES YOUR PIP PACKAGES NEED (ldd on {len(sos)} compiled files):"]
    L += [f"    apt package {k:<28} used by {', '.join(sorted(v))}" for k, v in sorted(need.items())
          if not re.match(r"^(libc6|libgcc-s1|libstdc\+\+6|zlib1g)$", k)] or ["    (only the C / C++ runtime)"]
    drv = {k: v for k, v in missing.items() if re.match(r"lib(cuda|nvidia-)", k)}
    real = {k: v for k, v in missing.items() if k not in drv}
    if drv:
        L.append("    GPU driver libraries (" + ", ".join(sorted(drv)) + "): not in the image on purpose - "
                 "docker run --gpus all mounts them from your PC")
    if real:
        L.append("    MISSING (an import that needs one of these fails with 'cannot open shared object file'):")
        for k, v in sorted(real.items()):
            fix = next((p for so, p in APT.items() if k.startswith(so)), None)
            optional = all(re.search(r"plugin|_tensorrt|tensorrt|openvino|_rocm|migraphx", x, re.I) for x in v)
            windows = all(re.search(r"qxcb|XcbQpa|libQt5Gui|libQt5Widgets", x) for x in v)
            L.append(f"      {k:<26} needed by {', '.join(sorted(v))[:110]}" +
                     ("\n        (a plugin / optional part: matters only if the program uses it)" if optional else
                      "\n        (only for windows on a screen: cv2.imshow with a display)" if windows else "") +
                     "\n        -> " +
                     (f"apt-get install -y {fix}" if fix else "find it: apt-get install -y apt-file && apt-file "
                      f"update && apt-file search {k}") +
                     ("  (or a CUDA library: check LD_LIBRARY_PATH above)" if CUDA_RE.search(k) else ""))
    else:
        L.append("    MISSING: none - every system library the pip packages ask for is in the image")
    # CUDA libraries in the image, and where they are
    cud = []
    lc = subprocess.run(["ldconfig", "-p"], capture_output=True, text=True).stdout if shutil.which("ldconfig") else ""
    for line in lc.splitlines():
        m = re.search(r"(lib(cudart|cublas|cublasLt|cudnn|cufft|curand|nvrtc)\.so\.\d+) .* => (\S+)", line)
        if m:
            cud.append((m.group(1), os.path.realpath(m.group(3))))
    for d in sites:
        for f in glob.glob(os.path.join(d, "nvidia", "*", "lib", "lib*.so.*")):
            if re.search(r"lib(cudart|cublas|cublasLt|cudnn|cufft|curand|nvrtc)\.so\.\d+$", f):
                cud.append((os.path.basename(f), f))
    L += ["", "  CUDA LIBRARIES IN THE IMAGE:"]
    L += [f"    {n:<22} {mb(os.path.getsize(p)) if os.path.exists(p) else '?':>8}  {src.of(p).replace(':', ' ', 1):<28} {p}"
          for n, p in sorted(set(cud))] or ["    none (a CPU image, or the CUDA libraries come with a package not listed)"]
    if any("site-packages" in p for _n, p in cud) and not os.environ.get("LD_LIBRARY_PATH"):
        L.append("    note: CUDA libraries from pip (site-packages/nvidia) but no LD_LIBRARY_PATH - torch finds them "
                 "itself; onnxruntime and others that open them by name need LD_LIBRARY_PATH")
    if not a.no_imports:
        L += [""] + import_test(sites)
    L += ["", *gpu_test()]
    print("\n".join(L), flush=True)


SKIP_DISTS = {"pip", "setuptools", "wheel", "distribute"}
HINTS = [(r"numpy\.core\.multiarray failed to import|_ARRAY_API|compiled using NumPy 1|numpy\.dtype size changed",
          "built for NumPy 1.x, but NumPy 2.x is installed: install the numpy version your PC has (numpy<2)"),
         (r"No module named '([^'.]+)", "not installed: '{0}' is missing - a dependency nothing installed (pip check "
                                         "lists them)"),
         (r"cannot open shared object file", "a system library is missing: see MISSING above (apt-get install ...)"),
         (r"undefined symbol", "compiled for another version of a package it links to (e.g. torchaudio / torchvision "
                               "built for another torch): use the matching versions"),
         (r"DISPLAY|cannot connect to X server|qt\.qpa", "needs a screen: normal in a container"),
         (r"GLIBC_[0-9.]+' not found", "built for a newer Linux than this image: newer base image, or an older version")]


def top_modules(site, distinfo):
    """the modules 'import X' can load from one pip package: top_level.txt, else its RECORD"""
    t = read(os.path.join(distinfo, "top_level.txt"))
    if t:
        mods = [x.strip() for x in t.splitlines() if x.strip()]
    else:
        mods = []
        for line in (read(os.path.join(distinfo, "RECORD")) or "").splitlines():
            first = line.split(",")[0].split("/")[0]
            if first.endswith((".dist-info", ".data")) or first in ("..", "bin", "__pycache__") or not first:
                continue
            if "/" in line.split(",")[0]:
                if os.path.isfile(os.path.join(site, first, "__init__.py")):
                    mods.append(first)
            elif first.endswith(".py"):
                mods.append(first[:-3])
            elif re.search(r"\.so$|\.pyd$", first):
                mods.append(first.split(".")[0])
    return sorted({m.replace("/", ".") for m in mods if m and not m.startswith(("_", "test")) and
                   re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", m)})


CHILD = r"""
import importlib, sys, time
for m in sys.argv[1:]:
    print("@@xray START " + m, flush=True)
    t = time.time()
    try:
        importlib.import_module(m)
        print("@@xray OK %s %.1f" % (m, time.time() - t), flush=True)
    except BaseException as e:
        msg = ("%s: %s" % (type(e).__name__, e)).strip().splitlines()
        print("@@xray FAIL %s %s" % (m, msg[0][:220] if msg else type(e).__name__), flush=True)
"""


def import_test(sites):
    """import every top-level module of every pip package, in a separate Python: what the program would hit"""
    mods = {}
    for d in sites:
        for di in sorted(glob.glob(os.path.join(d, "*.dist-info"))):
            dist = os.path.basename(di)[:-10]
            name = re.sub(r"-[^-]+$", "", dist)
            if name.lower().replace("_", "-") in SKIP_DISTS:
                continue
            for m in top_modules(d, di):
                mods.setdefault(m, dist.replace("-", " ", 1) if "-" in dist else dist)
    todo, res, t0 = sorted(mods, key=str.lower), {}, time.time()
    while todo:
        try:
            r = subprocess.run([sys.executable, "-c", CHILD] + todo, capture_output=True, text=True, timeout=900,
                               cwd="/")
            out, code = r.stdout, r.returncode
        except subprocess.TimeoutExpired as e:
            out, code = (e.stdout.decode() if isinstance(e.stdout, bytes) else e.stdout) or "", "timeout"
        started = None
        for line in out.splitlines():
            m = re.match(r"@@xray (START|OK|FAIL) (\S+) ?(.*)$", line)
            if not m:
                continue
            if m.group(1) == "START":
                started = m.group(2)
            else:
                res[m.group(2)] = (m.group(1), m.group(3))
                started = None
        if started:      # the import itself crashed the process (segfault) or hung
            res[started] = ("FAIL", f"the import crashed Python (exit code {code})" if code != "timeout" else
                            "no answer within 900 s")
        done = set(res)
        rest = [m for m in todo if m not in done]
        if len(rest) == len(todo):        # nothing ran at all
            for m in rest:
                res[m] = ("FAIL", f"could not run Python (exit code {code})")
            break
        todo = rest
    bad = sorted((m for m in res if res[m][0] == "FAIL"), key=str.lower)
    good = sorted((m for m in res if res[m][0] == "OK"), key=str.lower)
    L = [f"  DOES EVERY PACKAGE IMPORT? ({len(res)} modules of {len(set(mods.values()))} pip packages, each imported "
         f"the way your code does - {time.time() - t0:.0f} s)"]
    if bad:
        L.append(f"    FAILED ({len(bad)}) - the program fails only if it imports one of these:")
        for m in bad:
            msg = res[m][1]
            L.append(f"      {m:<24} ({mods[m]})  {msg}")
            for pat, hint in HINTS:
                h = re.search(pat, msg)
                if h:
                    L.append("        -> " + hint.format(*(h.groups() or ("",))))
                    break
    else:
        L.append("    FAILED: none")
    slow = sorted(((float(res[m][1] or 0), m) for m in good if re.fullmatch(r"[0-9.]+", res[m][1] or "")), reverse=True)
    L.append(f"    OK ({len(good)}): " + ", ".join(good))
    if slow and slow[0][0] >= 2:
        L.append("    slowest imports: " + ", ".join(f"{m} {t:.0f} s" for t, m in slow[:4] if t >= 1))
    return L


def gpu_test():
    tests = {"torch": "import torch; ok = torch.cuda.is_available(); print('torch', torch.__version__, '- GPU:', ok, "
                      "torch.cuda.get_device_name(0) if ok else '', '- CUDA build', torch.version.cuda)",
             "onnxruntime": "import onnxruntime as o; print('onnxruntime', o.__version__, '- providers:', "
                            "o.get_available_providers(), '(available = built in; a session shows if CUDA really works)')",
             "tensorflow": "import tensorflow as tf; print('tensorflow', tf.__version__, '- GPUs:', "
                           "tf.config.list_physical_devices('GPU'))",
             "cupy": "import cupy; print('cupy', cupy.__version__, '- GPUs:', cupy.cuda.runtime.getDeviceCount())",
             "paddle": "import paddle; print('paddle', paddle.__version__, '- GPU:', paddle.device.is_compiled_with_cuda())"}
    L = ["  GPU TEST (each framework that is installed, in its own process):"]
    if not shutil.which("nvidia-smi"):
        L.append("    no nvidia-smi: this container has no GPU (start it with --gpus all to test the GPU)")
    import importlib.util
    found = False
    for mod, code in tests.items():
        if importlib.util.find_spec(mod) is None:
            continue
        found = True
        try:
            r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=180)
            out = (r.stdout.strip() or r.stderr.strip().splitlines()[-1:] or ["no output"])
            out = out if isinstance(out, str) else out[0]
            L.append(f"    {out[:200]}" + (f"   (exit code {r.returncode}: crashed)" if r.returncode < 0 else ""))
        except subprocess.TimeoutExpired:
            L.append(f"    {mod}: no answer within 180 s")
    if not found:
        L.append("    no GPU framework installed (torch, onnxruntime, tensorflow, cupy, paddle)")
    return L


def main(argv=None):
    ap = argparse.ArgumentParser(description="look inside a container: files, libraries and their packages, network, GPU")
    ap.add_argument("--watch", type=float, help="look every N seconds and print what changes")
    ap.add_argument("--image", action="store_true", help="look at the image: packages, missing libraries, CUDA, GPU test")
    ap.add_argument("--pid", type=int, help="only this process")
    ap.add_argument("--all", action="store_true", help="list every library")
    ap.add_argument("--no-imports", action="store_true", help="--image without the import test (it takes a while)")
    a = ap.parse_args(argv)
    if not os.path.isdir("/proc/self"):
        sys.exit("xray_look runs inside a Linux container: docker exec -it NAME python /xray/xray_look.py")
    if a.image:
        look_image(a)
    elif a.watch:
        watch(a)
    else:
        one_look(a)


if __name__ == "__main__":
    main()
