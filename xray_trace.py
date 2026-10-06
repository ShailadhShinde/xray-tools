#!/usr/bin/env python3
"""
xray_trace.py - X-Ray Phase 2: runtime tracer. Stdlib only, Python 3.8+.

Three steps:

  run    INSIDE the app's container: runs the app with the tracer hook (xray_hook/sitecustomize.py)
         loaded into every Python process, then collects system facts (dpkg owners of loaded
         libraries / fonts / binaries, package versions, DNS names, /dev/shm size).
           python /xray/xray_trace.py run --out /xray-out -- python src/main.py
           (add --strace to also record every file open / connect / exec with strace -f)

  facts  on the host: turns the raw trace folder into an X-Ray fact file (same format as
         xray_static.py, evidence "confirmed"), gradable with xray_score.py --layer runtime.
           python3 xray_trace.py facts --trace trace-f3 --out f3-runtime.json

  merge  on the host: combines the static map and the runtime facts. Every static fact gets a
         runtime verdict (seen / its line ran / did not run in the scenario) and a label
         (confirmed / static_only / unresolved / derived); runtime-only facts are added.
           python3 xray_trace.py merge --static f3.json --runtime f3-runtime.json \\
               --root F3_stream_worker --out f3-merged.json
"""
import argparse
import ast
import glob
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from collections import defaultdict

VERSION = "0.2.4"
HERE = os.path.dirname(os.path.abspath(__file__))
HOOK_DIR = os.path.join(HERE, "xray_hook")

MODEL_EXT = (".onnx", ".pt", ".pth", ".bin", ".pkl", ".pickle", ".joblib", ".h5", ".hdf5", ".tflite", ".task", ".pb",
             ".safetensors", ".engine", ".trt", ".caffemodel", ".weights", ".ckpt", ".npz", ".param")
CONFIG_EXT = (".yaml", ".yml", ".json", ".toml", ".ini", ".cfg", ".conf", ".env", ".xml", ".properties")
DOWNLOAD_EXT = MODEL_EXT + (".zip", ".tar", ".gz", ".tgz", ".npy", ".csv", ".txt", ".jpg", ".png", ".mp4")
ENV_BORING = {"PATH", "HOME", "HOSTNAME", "LANG", "LC_ALL", "TERM", "PWD", "SHLVL", "GPG_KEY", "PYTHON_VERSION",
              "PYTHON_SHA256", "PYTHON_PIP_VERSION", "PYTHON_SETUPTOOLS_VERSION", "PYTHON_GET_PIP_URL",
              "PYTHON_GET_PIP_SHA256", "PYTHONPATH", "_", "OLDPWD", "NO_PROXY", "no_proxy"}
# set by base images (nvidia/cuda, python, Debian), not by the person running the app: never "set but not read"
IMAGE_ENV_PREFIXES = ("XRAY_", "NV_", "NVIDIA_", "CUDA_", "NCCL_", "CUDNN_", "LC_", "LD_LIBRARY_PATH", "DEBIAN_FRONTEND",
                      "PYTHONUNBUFFERED", "PYTHONDONTWRITEBYTECODE", "VIRTUAL_ENV", "PIP_", "LIBRARY_PATH", "NVARCH")
SECRETISH = ("PASS", "SECRET", "TOKEN", "KEY", "PWD", "CREDENTIAL", "AUTH")
# Debian packages python:3.x-slim already has (base system + CPython's runtime libraries). An assumption until
# Phase 3 checks the real slim image; only used to keep them out of system_package_candidate facts.
PY_SLIM_BASE = {"libc6", "libc-bin", "libgcc-s1", "libstdc++6", "zlib1g", "libzstd1", "liblzma5", "libbz2-1.0",
                "libssl3", "libssl3t64", "libffi8", "libuuid1", "libcrypt1", "libtinfo6", "libncursesw6",
                "libsqlite3-0", "libexpat1", "libreadline8", "libreadline8t64", "libgdbm6", "libgdbm6t64",
                "libdb5.3", "libdb5.3t64", "tzdata", "base-files", "ca-certificates", "netbase"}
IMPORT_CATS = {"local_module", "third_party_import", "dynamic_import"}
NOT_ROOT_ENV = {"HOME", "PWD", "OLDPWD", "TMPDIR", "TMP", "TEMP", "PATH", "SHELL", "VIRTUAL_ENV", "PYTHONPATH"}
STRACE_CALLS = "open,openat,creat,mkdir,mkdirat,rename,renameat,renameat2,connect,bind,execve"


def jload(p):
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)


def jdump(obj, p):
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=1, ensure_ascii=False, default=str)


def read_events(trace_dir):
    """-> {pid: [records]} (a torn last line from a killed process is skipped)."""
    out = {}
    for path in sorted(glob.glob(os.path.join(trace_dir, "events-*.jsonl"))):
        pid = int(re.findall(r"events-(\d+)\.jsonl", path)[0])
        recs = []
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    recs.append(json.loads(line))
                except ValueError:
                    pass
        out[pid] = recs
    return out


# ============================================================================ strace (os_trace)
STRACE_LINE = re.compile(r"^(\d+)\s+(\w+)\((.*)\)\s+=\s+(-?\d+|\?)(?:\s+(\w+))?")
STR = re.compile(r'"((?:[^"\\]|\\.)*)"')


def parse_strace(path, cwd="/"):
    """-> list of {pid, call, path|argv|host+port, ok, write}. Joins <unfinished ...> / <... resumed>."""
    out, pending = [], {}
    if not path or not os.path.exists(path):
        return out
    with open(path, encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            raw = raw.rstrip("\n")
            m = re.match(r"^(\d+)\s+(.*)$", raw)
            if not m:
                continue
            pid, rest = int(m.group(1)), m.group(2)
            if rest.endswith("<unfinished ...>"):
                pending[pid] = rest[: -len("<unfinished ...>")].rstrip()
                continue
            r = re.match(r"^<\.\.\. (\w+) resumed>(.*)$", rest)
            if r:
                head = pending.pop(pid, None)
                if head is None:
                    continue
                rest = head + r.group(2)
            m = STRACE_LINE.match(f"{pid} {rest}")
            if not m:
                continue
            call, args, ret, errno = m.group(2), m.group(3), m.group(4), m.group(5)
            ok = ret != "?" and not ret.startswith("-") or errno == "EINPROGRESS"
            ev = {"pid": pid, "call": call, "ok": ok, "errno": errno}
            strs = [bytes(s, "utf-8").decode("unicode_escape", "replace") for s in STR.findall(args)]
            if call in ("open", "openat", "creat"):
                if not strs:
                    continue
                p = strs[0]
                ev["path"] = os.path.normpath(p if p.startswith("/") else os.path.join(cwd, p))
                ev["write"] = call == "creat" or bool(re.search(r"O_WRONLY|O_RDWR|O_CREAT|O_APPEND|O_TRUNC", args))
                ev["dir"] = "O_DIRECTORY" in args
            elif call in ("mkdir", "mkdirat"):
                if not strs:
                    continue
                ev["path"] = strs[0] if strs[0].startswith("/") else os.path.normpath(os.path.join(cwd, strs[0]))
                ev["write"] = True
            elif call.startswith("rename"):
                if len(strs) < 2:
                    continue
                ev["path"] = strs[1] if strs[1].startswith("/") else os.path.normpath(os.path.join(cwd, strs[1]))
                ev["write"] = True
            elif call in ("connect", "bind"):
                pm = re.search(r"sin6?_port=htons\((\d+)\)", args)
                am = re.search(r'inet_addr\("([^"]+)"\)|inet_pton\(AF_INET6, "([^"]+)"', args)
                if not pm or not am:
                    um = re.search(r'sun_path="([^"]*)"', args)
                    if um:
                        ev["unix"] = um.group(1)
                    else:
                        continue
                else:
                    ev["host"], ev["port"] = am.group(1) or am.group(2), int(pm.group(1))
            elif call == "execve":
                if not strs:
                    continue
                ev["path"] = strs[0]
                am = re.search(r"\[(.*?)\]", args)
                ev["argv"] = STR.findall(am.group(1))[:12] if am else [strs[0]]
            else:
                continue
            out.append(ev)
    return out


# ============================================================================ run (in container)
def cmd_run(a):
    cmd = a.cmd[1:] if a.cmd and a.cmd[0] == "--" else a.cmd
    if not cmd:
        sys.exit("usage: xray_trace.py run --out DIR [--root DIR] [--strace] -- python src/main.py")
    if not os.path.exists(os.path.join(HOOK_DIR, "sitecustomize.py")):
        sys.exit(f"missing {HOOK_DIR}/sitecustomize.py (copy the whole xray-fixtures folder)")
    out = os.path.abspath(a.out)
    os.makedirs(out, exist_ok=True)
    for pat in ("events-*.jsonl", "collect.json", "run.json", "strace.log"):
        for p in glob.glob(os.path.join(out, pat)):
            os.remove(p)
    root = os.path.realpath(a.root or os.getcwd())
    env = dict(os.environ)
    env["XRAY_TRACE_DIR"] = out
    env["XRAY_ROOT"] = root
    env["XRAY_COVERAGE"] = "0" if a.no_coverage else "1"
    env["PYTHONPATH"] = HOOK_DIR + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    for c in a.camera or []:
        if "=" not in c:
            sys.exit(f"--camera wants INDEX=SOURCE, e.g. --camera 0=/data/clip.mp4 (got {c!r})")
    if a.camera:
        env["XRAY_CAMERA"] = "\n".join(a.camera)
        print(f"[xray-trace] camera swap (app code unchanged): {', '.join(a.camera)}", flush=True)
    if a.gui == "off":
        env["XRAY_GUI"] = "off"
        env["XRAY_GUI_SAVE"] = str(a.gui_save)
        print("[xray-trace] cv2 windows off" + (f"; a shown frame is saved every {a.gui_save:g}s to {out}/gui/"
                                               if a.gui_save else ""), flush=True)
    full = list(cmd)
    if a.strace:
        st = shutil.which("strace")
        if not st:
            sys.exit("[xray-trace] strace is not installed in this container. Install it first, e.g.:\n"
                     "  apt-get update && apt-get install -y strace\n(or run without --strace)")
        extra = ["--seccomp-bpf"] if subprocess.call([st, "--seccomp-bpf", "-f", "-qq", "-e", "trace=openat",
                                                       "-o", os.devnull, "true"],
                                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0 else []
        full = [st, "-f", "-qq", "-s", "512", *extra, "-e", "trace=" + STRACE_CALLS,
                "-o", os.path.join(out, "strace.log")] + full
    print(f"[xray-trace] root={root} out={out}", flush=True)
    print(f"[xray-trace] running: {' '.join(full)}", flush=True)
    t0 = time.time()
    proc = subprocess.Popen(full, env=env)
    stopped_by, drc = None, None

    def stop(why):
        """Ctrl+C for the app (not for strace): Python raises KeyboardInterrupt, so atexit runs and nothing is lost."""
        nonlocal stopped_by
        if proc.poll() is not None:
            return
        stopped_by = stopped_by or why
        for pid in app_pids(proc.pid, bool(a.strace)):
            try:
                os.kill(pid, signal.SIGINT)
            except OSError:
                pass
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.terminate()
    signal.signal(signal.SIGTERM, lambda *_: stop("SIGTERM to the runner"))
    try:
        if a.driver:
            print(f"[xray-trace] driver: {a.driver}", flush=True)
            drv = subprocess.Popen(["sh", "-c", a.driver], env=dict(os.environ))
            while drv.poll() is None:
                if proc.poll() is not None:       # the app died: no point waiting for the driver
                    print(f"[xray-trace] the app exited (code {proc.returncode}) while the driver was running",
                          flush=True)
                    drv.terminate()
                    break
                time.sleep(0.5)
            drc = drv.wait()
            print(f"[xray-trace] driver exited with code {drc}; stopping the app with Ctrl+C", flush=True)
            time.sleep(1)
            stop("driver finished")
        elif a.stop_after:
            try:
                proc.wait(timeout=a.stop_after)
            except subprocess.TimeoutExpired:
                stop(f"--stop-after {a.stop_after}s")
        rc = proc.wait()
    except KeyboardInterrupt:
        stop("Ctrl+C")
        rc = proc.wait()
    dur = round(time.time() - t0, 1)
    print(f"[xray-trace] app exited with code {rc} after {dur}s"
          + (f" (stopped: {stopped_by})" if stopped_by else "") + " - collecting system info...", flush=True)
    jdump({"cmd": cmd, "out": out, "strace": bool(a.strace), "exit_code": rc, "duration_s": dur, "root": root,
           "driver": a.driver, "driver_exit_code": drc, "stopped_by": stopped_by,
           "camera_swap": a.camera or [], "gui": a.gui,
           "cwd": os.getcwd(), "started": t0, "tool": "xray-trace", "version": VERSION}, os.path.join(out, "run.json"))
    try:
        collect(out, root)
    except Exception as e:  # the trace itself is still usable
        print(f"[xray-trace] collect step failed: {e!r}", flush=True)
    give_back(out)
    n = sum(len(v) for v in read_events(out).values())
    print(f"[xray-trace] done: {len(glob.glob(os.path.join(out, 'events-*.jsonl')))} Python processes traced, "
          f"{n} events -> {out}", flush=True)
    if a.driver:
        return 0 if drc == 0 else 1
    return 0 if rc == 0 or stopped_by else 1


def app_pids(pid, under_strace):
    """The app's process: strace's direct children when tracing, else the child itself."""
    if not under_strace:
        return [pid]
    kids = []
    for t in glob.glob(f"/proc/{pid}/task/*/children"):
        try:
            with open(t) as fh:
                kids += [int(x) for x in fh.read().split()]
        except OSError:
            pass
    return kids or [pid]


def give_back(out):
    """Files written by root inside the container: hand them to the owner of the output folder."""
    try:
        st = os.stat(out)
        if os.getuid() == 0 and st.st_uid != 0:
            for p in glob.glob(os.path.join(out, "*")):
                os.chown(p, st.st_uid, st.st_gid)
    except OSError:
        pass


def dpkg_owners(paths):
    """{path: package} for files owned by a Debian package (tries /usr-merged variants)."""
    if not shutil.which("dpkg"):
        return {}
    cands = {}
    for p in paths:
        vs = [p]
        rp = os.path.realpath(p)
        if rp != p:
            vs.append(rp)
        for v in list(vs):
            if v.startswith("/usr/lib/") or v.startswith("/usr/bin/") or v.startswith("/usr/sbin/"):
                vs.append(v[4:])
            elif v.startswith(("/lib/", "/bin/", "/sbin/")):
                vs.append("/usr" + v)
        for v in vs:
            cands.setdefault(v, p)
    res = {}
    items = list(cands)
    for i in range(0, len(items), 150):
        chunk = items[i:i + 150]
        try:
            txt = subprocess.run(["dpkg", "-S", *chunk], capture_output=True, text=True, timeout=60).stdout
        except Exception:
            continue
        for line in txt.splitlines():
            if ": " not in line or line.startswith("diversion"):
                continue
            pk, path = line.split(": ", 1)
            path = path.strip()
            if path in cands and cands[path] not in res:
                res[cands[path]] = pk.split(",")[0].split(":")[0].strip()
    return res


def dpkg_info(pkgs):
    if not pkgs or not shutil.which("dpkg-query"):
        return {}
    try:
        txt = subprocess.run(["dpkg-query", "-W", "-f=${Package}\t${Priority}\t${Essential}\t${Version}\n",
                              *sorted(pkgs)], capture_output=True, text=True, timeout=60).stdout
    except Exception:
        return {}
    out = {}
    for line in txt.splitlines():
        p = line.split("\t")
        if len(p) == 4:
            out[p[0]] = {"priority": p[1], "essential": p[2] == "yes", "version": p[3]}
    return out


def file_b64sha256(path):
    import base64
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return base64.urlsafe_b64encode(h.digest()).rstrip(b"=").decode()


def packages_distributions(md):
    """importlib.metadata.packages_distributions() - added in Python 3.10; rebuilt here for 3.8 / 3.9 images."""
    if hasattr(md, "packages_distributions"):
        return md.packages_distributions()
    out = {}
    for dist in md.distributions():
        name = dist.metadata["Name"]
        tops = (dist.read_text("top_level.txt") or "").split()
        if not tops:  # no top_level.txt: infer from the files, like the stdlib does
            tops = {f.parts[0] if len(f.parts) > 1 else f.name.rsplit(".", 1)[0]
                    for f in (dist.files or ()) if f.suffix == ".py"}
        for t in tops:
            if name not in out.setdefault(t, []):
                out[t].append(name)
    return out


def record_owner(md, dists, some_file):
    """Two distributions own the same files (opencv-python + -headless): the one whose RECORD hashes match the files
    on disk wrote them last - that is the build that really loads. Checks the package's compiled files."""
    folder = os.path.dirname(os.path.realpath(some_file))
    score = {}
    for d in dists:
        try:
            dist = md.distribution(d)
            n = 0
            for rf in dist.files or []:
                p = os.path.realpath(str(dist.locate_file(rf)))
                if rf.hash and p.startswith(folder + "/") and p.endswith(".so") and os.path.exists(p):
                    n += 1 if file_b64sha256(p) == rf.hash.value else -1
            score[d] = n
        except Exception:
            pass
    best = max(score, key=score.get) if score else None
    return best if best and score[best] > 0 else None


def collect(out, root):
    import socket
    ev = read_events(out)
    run = jload(os.path.join(out, "run.json"))
    st = parse_strace(os.path.join(out, "strace.log"), run.get("cwd", "/"))
    paths, hosts, ips, tops, bins = set(), set(), set(), set(), set()
    for recs in ev.values():
        for r in recs:
            k = r.get("k")
            if k == "exit":
                paths.update(p for p in r.get("libs", []) if p.startswith("/") and "-packages/" not in p)
                tops.update(r.get("packages", {}))
            elif k == "package":
                tops.add(r["top"])
            elif k == "dlopen_resolved" and r.get("path"):
                paths.add(r["path"])
            elif k == "font" and r.get("resolved"):
                paths.add(r["resolved"])
            elif k == "open" and not r.get("w") and not r["path"].startswith(root + "/"):
                paths.add(r["path"])
            elif k == "exec_process":
                exe = r.get("executable") or (r.get("argv") or [None])[0]
                if exe and not r.get("shell"):
                    w = exe if exe.startswith("/") else shutil.which(exe)
                    if w:
                        bins.add(w)
                        paths.add(w)
            elif k in ("resolve",) and r.get("host"):
                hosts.add(str(r["host"]))
            elif k == "db_connect" and r.get("host"):
                hosts.add(r["host"])
            elif k in ("http", "video_open"):
                u = str(r.get("url") or r.get("source") or "")
                m = re.match(r"^\w+://(?:[^@/]*@)?([^:/?#]+)", u)
                if m:
                    hosts.add(m.group(1))
            elif k == "connect" and r.get("host"):
                ips.add(str(r["host"]))
    for e in st:
        if e["call"] == "execve" and e.get("ok"):
            paths.add(e["path"])
        elif e.get("path") and e.get("ok") and not e.get("write") and e["path"].startswith("/") \
                and not e["path"].startswith((root + "/", "/proc", "/dev", "/sys")) and "-packages/" not in e["path"]:
            if re.search(r"\.so(\.\d+)*$", e["path"]) or "/share/" in e["path"] or e["path"].startswith("/etc/"):
                paths.add(e["path"])
        if e.get("host"):
            ips.add(e["host"])
    paths = {p for p in paths if not p.startswith((root + "/", "/proc/", "/dev/", "/tmp/"))}
    owners = dpkg_owners(sorted(paths))
    info = dpkg_info(set(owners.values()))
    dns = {}
    for h in sorted(hosts):
        if re.match(r"^[\d.]+$", h) or ":" in h:
            continue
        try:
            name, aliases, addrs = socket.gethostbyname_ex(h)
            dns[h] = addrs
        except OSError as e:
            dns[h] = f"unresolved: {e}"
    rev = {}
    known = {ip: h for h, addrs in dns.items() if isinstance(addrs, list) for ip in addrs}
    for ip in sorted(ips)[:40]:
        if ip in known:
            rev[ip] = known[ip]
            continue
        try:
            socket.setdefaulttimeout(2)
            rev[ip] = socket.gethostbyaddr(ip)[0]
        except OSError:
            pass
    dists, versions, dist_files = {}, {}, {}
    try:
        import importlib.metadata as md
        pd = packages_distributions(md)
        for t in sorted(tops):
            if t in pd:
                dists[t] = sorted(set(pd[t]))
                for d in dists[t]:
                    try:
                        versions[d] = md.version(d)
                    except Exception:
                        pass
        # which distribution really provided a top-level name that two distributions install
        loaded = {}
        for recs in ev.values():
            for r in recs:
                if r.get("k") == "exit":
                    loaded.update(r.get("packages", {}))
        for t, ds in dists.items():
            if len(ds) > 1 and t in loaded:
                dist_files[t] = record_owner(md, ds, loaded[t])
    except Exception as e:
        dists = {"error": repr(e)}
    sysinfo = {"python": sys.version.split()[0], "uid": os.getuid(), "cpu_count": os.cpu_count()}
    try:
        with open("/etc/os-release") as fh:
            sysinfo["os"] = dict(line.rstrip().split("=", 1) for line in fh if "=" in line).get("PRETTY_NAME",
                                                                                                "").strip('"')
    except OSError:
        pass
    try:
        s = os.statvfs("/dev/shm")
        sysinfo["dev_shm_bytes"] = s.f_blocks * s.f_frsize
    except OSError:
        pass
    for p in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        try:
            with open(p) as fh:
                sysinfo["memory_limit"] = fh.read().strip()
            break
        except OSError:
            pass
    macs = {}
    for p in glob.glob("/sys/class/net/*/address"):
        try:
            with open(p) as fh:
                macs[p.split("/")[4]] = fh.read().strip()
        except OSError:
            pass
    sysinfo["mac"] = macs
    sysinfo["hostname"] = socket.gethostname()
    reqs = []
    for rf in sorted(glob.glob(os.path.join(root, "requirements*.txt"))):
        try:
            with open(rf, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    m = re.match(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)", line.split("#")[0])
                    if m:
                        reqs.append(m.group(1).lower().replace("_", "-"))
        except OSError:
            pass
    jdump({"requirements": sorted(set(reqs)), "owners": owners, "packages": info, "dns": dns, "reverse_dns": rev, "dists": dists,
           "dist_versions": versions, "dist_loaded": dist_files, "binaries": sorted(bins), "system": sysinfo},
          os.path.join(out, "collect.json"))


# ============================================================================ facts (host)
def is_secret(name):
    return any(s in str(name).upper() for s in SECRETISH)


def split_ext(seg):
    i = seg.rfind(".")
    return (seg[:i], seg[i:]) if i > 0 else (seg, "")


def generalize(vals, is_file):
    """Template for one path segment that differs between observed values."""
    if is_file:
        parts = [split_ext(v) for v in vals]
        exts = {e for _, e in parts}
        stems = [s for s, _ in parts]
        ext = exts.pop() if len(exts) == 1 else ".<var>"
    else:
        stems, ext = vals, ""
    pre = os.path.commonprefix(stems)
    suf = os.path.commonprefix([s[::-1] for s in stems])[::-1]
    pre = pre[: max(pre.rfind(c) for c in "_-.") + 1] if any(c in pre for c in "_-.") else ""
    k = min((suf.find(c) for c in "_-." if c in suf), default=-1)
    suf = suf[k:] if k >= 0 else ""
    if len(pre) + len(suf) >= min(len(s) for s in stems):
        pre, suf = "", ""
    return f"{pre}<var>{suf}{ext}"


VOLATILE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|(?<![0-9a-z])(\d{8,}|[0-9a-f]{16,})"
                      r"(?![0-9a-z])", re.I)    # uuids, timestamps, hashes inside a path segment


def template(paths):
    """['/o/r/cam1/17.jpg', '/o/r/cam2/18.jpg'] -> ['/o/r/<var>/<var>.jpg'] (one per segment-count group)."""
    groups = defaultdict(list)
    for p in sorted(set(paths)):
        pre = ""
        m = re.match(r"^(\w+://[^/]*)(/.*)?$", p)
        if m:
            pre, p = m.group(1), m.group(2) or ""
        segs = p.split("/")
        # never merge different top folders (/root/.config vs /tmp/x) or different file types
        groups[(pre, len(segs), "/".join(segs[:min(3, len(segs) - 1)]), split_ext(segs[-1])[1])].append(p)
    out = []
    for (pre, _, _, _), ps in groups.items():
        segs = [[VOLATILE.sub("<var>", x) for x in p.split("/")] for p in ps]
        res = []
        for i, col in enumerate(zip(*segs)):
            last = i == len(segs[0]) - 1
            vals = sorted(set(col))
            res.append(vals[0] if len(vals) == 1 else generalize(vals, last and "." in "".join(vals)))
        out.append(pre + "/".join(res))
    return out


PID_STRIDE = 10_000_000     # several trace folders (one per entrypoint/container): pids are made unique


def merge_collect(cols):
    out = {}
    for c in cols:
        for k, v in c.items():
            if isinstance(v, dict):
                out.setdefault(k, {})
                if isinstance(out[k], dict):
                    for kk, vv in v.items():
                        out[k].setdefault(kk, vv)
            elif isinstance(v, list):
                cur = out.setdefault(k, [])
                cur.extend(x for x in v if x not in cur)
            else:
                out.setdefault(k, v)
    return out


class Builder:
    def __init__(self, trace_dirs, host_root=None):
        if isinstance(trace_dirs, str):
            trace_dirs = [trace_dirs]
        self.dirs = list(trace_dirs)
        self.ev, self.runs, cols = {}, [], []
        for i, d in enumerate(self.dirs):
            off = i * PID_STRIDE if len(self.dirs) > 1 else 0
            for pid, recs in read_events(d).items():
                for r in recs:
                    for k in ("pid", "ppid", "child_pid"):
                        if isinstance(r.get(k), int):
                            r[k] += off
                self.ev[pid + off] = recs
            run = jload(os.path.join(d, "run.json")) if os.path.exists(os.path.join(d, "run.json")) else {}
            run["_offset"], run["_dir"] = off, d
            self.runs.append(run)
            if os.path.exists(os.path.join(d, "collect.json")):
                cols.append(jload(os.path.join(d, "collect.json")))
        if not self.ev:
            sys.exit(f"no events-*.jsonl in {', '.join(self.dirs)}")
        self.run = self.runs[0]
        self.col = merge_collect(cols)
        self.host_root = host_root
        starts = [r for recs in self.ev.values() for r in recs if r.get("k") == "start"]
        self.root = self.run.get("root") or (starts[0]["root"] if starts else "/")
        self.cwd = self.run.get("cwd") or (starts[0].get("cwd") if starts else self.root)
        self.facts = {}
        self.warnings = []
        self.procs = self.roles()
        self.env_dirs = {}    # abs dir -> env var name (values of env vars the project read or set)
        self.lib_exec = {}    # binaries run by library code
        self.lib_dlopen = {}  # shared libraries opened by library code
        self.dl_only = {}     # endpoint -> True while every request to it was a file download

    # ------------------------------------------------------------ processes
    def roles(self):
        info = {}
        targets = {}
        for pid, recs in self.ev.items():
            for r in recs:
                if r.get("k") == "mp_process" and r.get("child_pid"):
                    targets[r["child_pid"]] = r.get("target")
        pids = set(self.ev)
        for pid, recs in self.ev.items():
            st = next((r for r in recs if r.get("k") == "start"), {})
            ex = next((r for r in recs if r.get("k") == "exit"), None)
            argv = st.get("argv") or []
            if pid in targets:
                role = f"child:{(targets[pid] or '?').replace('__mp_main__.', '').replace('__main__.', '')}"
            elif any("resource_tracker" in str(x) for x in argv):
                role = "resource_tracker"
            elif st.get("spawn_child"):
                role = "child"
            elif st.get("ppid") not in pids:
                role = "main"
                if len(self.dirs) > 1:
                    script = next((x for x in argv[1:] if str(x).endswith(".py")), None)
                    role = f"main:{os.path.basename(script) if script else pid // PID_STRIDE}"
            else:
                role = "subprocess"
            info[pid] = {"pid": pid, "ppid": st.get("ppid"), "role": role, "argv": argv[:6],
                         "clean_exit": ex is not None, "uid": st.get("uid"),
                         "torch_threads": (ex or {}).get("torch_threads"), "cv2_threads": (ex or {}).get("cv2_threads"),
                         "mp_start_method": (ex or {}).get("mp_start_method"),
                         "peak_rss_mb": (ex or {}).get("peak_rss_mb"), "cpu_s": (ex or {}).get("cpu_s"),
                         "wall_s": (ex or {}).get("wall_s"), "gpu_peak_mb": (ex or {}).get("gpu_peak_mb")}
        return info

    def resources(self):
        """What the run cost: RAM / CPU / torch GPU memory per process and in total (compare a GPU and a CPU run)."""
        ps = [p for p in self.procs.values() if p.get("peak_rss_mb") is not None]
        if not ps:
            return None
        wall = max((p.get("wall_s") or 0) for p in ps)
        cpu = round(sum(p.get("cpu_s") or 0 for p in ps), 1)
        out = {"processes": len(ps), "peak_ram_mb_sum": round(sum(p["peak_rss_mb"] for p in ps), 1),
               "peak_ram_mb_max": max(p["peak_rss_mb"] for p in ps), "cpu_s": cpu, "wall_s": wall,
               "avg_cpu_cores_busy": round(cpu / wall, 2) if wall else None,
               "gpu_peak_mb_torch": round(sum(p.get("gpu_peak_mb") or 0 for p in ps), 1) or None,
               "per_process": [{k: p.get(k) for k in ("pid", "role", "peak_rss_mb", "cpu_s", "wall_s", "gpu_peak_mb")}
                               for p in sorted(ps, key=lambda p: p["pid"])]}
        return out

    def role(self, pid):
        return self.procs.get(pid, {}).get("role", "?")

    # ------------------------------------------------------------ fact store
    def add(self, cat, subject, mech, layer, site=None, pid=None, required="yes", detail=None, key=None, **extra):
        k = key or (cat, str(subject))
        f = self.facts.get(k)
        if f is None:
            f = self.facts[k] = {"category": cat, "subject": subject, "mechanism": mech, "sites": [],
                                 "evidence": "confirmed", "confidence": "high", "layers": [], "detail": [],
                                 "required_at_runtime": required, "processes": [], "count": 0}
        f["count"] += 1
        if layer and layer not in f["layers"]:
            f["layers"].append(layer)
        if site and {"file": site[0], "line": site[1]} not in f["sites"]:
            f["sites"].append({"file": site[0], "line": site[1]})
        if pid is not None and self.role(pid) not in f["processes"]:
            f["processes"].append(self.role(pid))
        if detail and detail not in f["detail"]:
            f["detail"].append(detail)
        if required == "yes":
            f["required_at_runtime"] = "yes"
        for kk, v in extra.items():
            if isinstance(v, list):
                cur = f.setdefault(kk, [])
                cur.extend(x for x in v if x not in cur)
            elif v is not None:
                f.setdefault(kk, v)
        return f

    def rel(self, p):
        if p and p.startswith(self.root + "/"):
            return p[len(self.root) + 1:]
        return p

    def alias(self, p):
        """/root/.cache/xray_models/x.onnx -> MODEL_CACHE/x.onnx when MODEL_CACHE was read by the code."""
        best = None
        for d, name in self.env_dirs.items():
            if (p == d or p.startswith(d + "/")) and (best is None or len(d) > len(best[0])):
                best = (d, name)
        if best:
            return best[1] + p[len(best[0]):]
        return None

    def file_cat(self, p):
        low = p.lower()
        if low.endswith(MODEL_EXT):
            return "model_file"
        if low.endswith(CONFIG_EXT):
            return "config_file"
        return "data_file_read"

    def os_data(self, p):
        """-> (subject, mechanism) for OS-provided data files, else None."""
        m = re.search(r"/zoneinfo/(.+)$", p)
        if m and "/usr/share/zoneinfo" in p or m and "/tzdata/zoneinfo/" in p:
            return f"tzdata:{m.group(1)}", "timezone"
        if p in ("/etc/localtime", "/etc/timezone"):
            return f"timezone:{p}", "timezone"
        if re.search(r"\.(ttf|otf|ttc|pcf|pfb)(\.gz)?$", p, re.I) or p.startswith("/usr/share/fonts/"):
            return f"font:{os.path.basename(p)}", "font"
        if p.startswith(("/etc/ssl/certs", "/usr/share/ca-certificates/", "/etc/pki/")) or p.endswith("/certifi/cacert.pem"):
            return "ca-certificates", "certificates"
        if p.startswith("/usr/share/") or p.startswith("/etc/"):
            return None
        return None

    # ------------------------------------------------------------ build
    def build(self):
        ev = self.ev
        # pass 0: env values that are directories (for aliases / write roots)
        for pid, recs in ev.items():
            for r in recs:
                if r.get("k") == "env" and r.get("value") and r["value"] != "***":
                    v = str(r["value"])
                    if v.startswith("/") and len(v) > 1 and r["name"] not in NOT_ROOT_ENV:
                        self.env_dirs.setdefault(os.path.normpath(v), r["name"])
        direct_tops = {r["mod"].split(".")[0] for recs in ev.values() for r in recs
                       if r.get("k") == "import" and r.get("kind") == "third_party" and r.get("direct")}
        local_mods, imported_files = {}, set()
        for recs in ev.values():
            for r in recs:
                if r.get("k") == "import" and r.get("kind") == "local":
                    local_mods[r["mod"]] = r.get("file")
                    imported_files.add(r.get("file"))
        missing = []                     # (path_check record, pid) of files the code looked for and did not find
        bound = defaultdict(set)         # pid -> ports the process itself listens on
        for pid, recs in ev.items():
            for r in recs:
                if r.get("k") == "bind" and r.get("port"):
                    bound[pid].add(str(r["port"]))
        writes = defaultdict(list)       # (site, mech, layer) -> [(path, pid)]
        dirs = defaultdict(list)
        endpoints_by_pid = defaultdict(set)
        for pid, recs in ev.items():
            local_groups = defaultdict(list)
            for r in recs:
                k = r.get("k")
                site = r.get("site")
                if k == "import":
                    self.fact_import(r, pid, local_groups)
                elif k == "package" and site and r["top"] not in direct_tops:
                    self.fact_declared_package(r, pid)
                elif k == "import_failed":
                    top = r["mod"].split(".")[0]
                    if r.get("site") and top not in local_mods:
                        self.add("third_party_import", top, "try_except_optional", "py_hook", site, pid,
                                 required="conditional", import_name=top, import_failed=True,
                                 detail="import attempted and failed (ImportError was handled): not installed")
                elif k == "unpickle":
                    mod = r.get("mod")
                    if mod in local_mods and site:
                        self.add("dynamic_import", mod, "pickle_class_import", "py_hook", site, pid,
                                 resolved_path=local_mods.get(mod), detail=f"class {mod}.{r.get('cls')} unpickled")
                elif k == "exec_module":
                    if site and r.get("file") not in imported_files and not str(r.get("caller")).endswith("runpy.py"):
                        mech = "spec_from_file_location" if str(r.get("inner")).startswith("<frozen") else "exec_file"
                        self.add("dynamic_import", r["file"], mech, "py_hook", site, pid, resolved_path=r["file"],
                                 detail="module code executed outside the import system")
                elif k == "open":
                    self.fact_open(r, pid, writes, imported_files)
                elif k in ("mkdir", "rename"):
                    if r.get("path"):
                        (dirs if k == "mkdir" else writes)[(tuple(site or ()), "mkdir" if k == "mkdir" else "python_io",
                                                            "py_hook")].append((r["path"], pid))
                elif k == "env":
                    self.fact_env(r, pid)
                elif k == "resolve":
                    host, port = r.get("host"), r.get("port")
                    if str(host) in ("0.0.0.0", "::", "") or str(port) in bound[pid]:
                        continue    # the server resolving its own listen address
                    if host and site and port not in (None, 0, "0"):
                        self.add("network_endpoint", f"{host}:{port}", "python_io", "py_hook", site, pid,
                                 host=str(host), port=str(port), detail=f"name lookup from {r.get('caller')}")
                        endpoints_by_pid[pid].add(str(port))
                elif k == "connect":
                    if r.get("type") == 2 or str(r.get("port")) in bound[pid]:
                        continue    # UDP "connect" (a local-IP lookup trick, no traffic) / own listen port
                    if r.get("family") in (2, 10) and site and str(r.get("port")) not in endpoints_by_pid[pid]:
                        ip = str(r["host"])
                        name = self.col.get("reverse_dns", {}).get(ip)
                        self.add("network_endpoint", f"{ip}:{r['port']}", "python_io", "py_hook", site, pid,
                                 host=ip, port=str(r["port"]),
                                 detail=f"connect to {ip}" + (f" ({name})" if name else ""))
                elif k == "bind":
                    if r.get("port") and site:
                        self.add("listen_port", f"{r.get('host') or '0.0.0.0'}:{r['port']}", "socket_bind", "py_hook",
                                 site, pid, port=str(r["port"]))
                elif k == "db_connect":
                    host, port = r.get("host") or "localhost", r.get("port") or "5432"
                    self.add("network_endpoint", f"{host}:{port}", "c_level_io", "py_wrapper", site, pid,
                             host=host, port=str(port),
                             detail=f"{r.get('api')} dbname={r.get('dbname') or r.get('database')} user={r.get('user')}"
                             + (f" -> {r['error']}" if r.get("error") else ""))
                elif k == "http":
                    self.fact_http(r, pid)
                elif k == "hub_load":
                    self.add("runtime_download", f"{r.get('repo')}:{r.get('model')}", "hub_download", "py_wrapper",
                             site, pid, detail="torch.hub.load")
                elif k == "video_open":
                    src = r.get("source")
                    if isinstance(src, str) and "://" in src:
                        writes[(tuple(site or ()), "video", "py_wrapper")].append((src, pid))
                    elif isinstance(src, int) or (isinstance(src, str) and src.startswith("/dev/video")):
                        sub = r.get("substituted_by")
                        dev = src if isinstance(src, str) else f"/dev/video{src}"   # same name as the static map
                        self.add("device", dev, "c_level_io", "py_wrapper", site, pid,
                                 aliases=[f"camera:{src}"],
                                 detail="cv2.VideoCapture of a local camera: needs --device"
                                 + (f" (replaced by {sub} during the trace)" if sub else ""))
                    elif isinstance(src, str):
                        self.add(self.file_cat(src), self.rel(src), "c_level_io", "py_wrapper", site, pid,
                                 detail="cv2.VideoCapture")
                elif k == "cv_write":
                    if r.get("path"):
                        writes[(tuple(site or ()), "c_level_io", "py_wrapper")].append((r["path"], pid))
                elif k == "cv_read":
                    if r.get("path"):
                        self.fact_read(r["path"], "c_level_io", "py_wrapper", site, pid, "cv2.imread")
                elif k == "model_load":
                    p = r.get("path")
                    if isinstance(p, str) and not p.startswith("<"):
                        mech = "python_io" if r.get("api") == "torch.load" else "c_level_io"
                        self.fact_read(p, mech, "py_wrapper", site, pid, r.get("api"), force_cat="model_file")
                    req = r.get("providers_requested") or []
                    if any("CUDA" in x or "Tensorrt" in x for x in req):
                        self.add("gpu_usage", "CUDAExecutionProvider", "provider_string", "py_wrapper", site, pid,
                                 required="conditional",
                                 detail=f"requested {req}, active {r.get('providers_active')}")
                elif k == "gpu_probe" and r.get("direct"):
                    self.add("gpu_usage", "torch.cuda.is_available()", "gpu_probe", "py_wrapper", site, pid,
                             result=r.get("result"),
                             detail=f"returned {r.get('result')}" + (" - CPU fallback used" if r.get("result") is False
                                                                      else ""))
                elif k == "thread_call" and r.get("direct"):
                    self.add("thread_config", r["api"], "thread_call", "py_wrapper", site, pid, value=r.get("value"),
                             detail=f"called with {r.get('value')}")
                elif k == "exec_process":
                    self.fact_exec(r, pid)
                elif k in ("dlopen", "dlopen_resolved"):
                    if k == "dlopen" and any(x.get("k") == "dlopen_resolved" and x.get("name") == r["name"]
                                             for x in recs):
                        continue    # the wrapper saw the same call, with the real file
                    self.fact_dlopen(r, pid, {})
                elif k == "find_library" and r.get("direct") and not r.get("result"):
                    self.add("native_library", f"lib{r.get('name')}", "ctypes", "py_wrapper", site, pid,
                             required="conditional", detail="ctypes.util.find_library returned None (not found)")
                elif k == "mp_start_method" and r.get("direct"):
                    self.add("multiprocessing", f"start_method:{r.get('method')}", "mp_call", "py_wrapper", site, pid,
                             detail=f"multiprocessing start method {r.get('method')}")
                elif k == "mp_process" and site:
                    t = (r.get("target") or "?").replace("__mp_main__.", "").replace("__main__.", "")
                    self.add("multiprocessing", f"process:{t}", "mp_call", "py_wrapper", site, pid,
                             detail="child processes started with this target")
                elif k == "shm" and r.get("create"):
                    f = self.add("ipc_shared_memory", "multiprocessing.shared_memory.SharedMemory", "shm_call",
                                 "py_wrapper", site, pid)
                    f.setdefault("segments", []).append({"name": r.get("name"), "bytes": r.get("size")})
                elif k == "font":
                    self.fact_font(r, pid)
                elif k == "hwid" and r.get("direct"):
                    self.add("hardware_identity", "uuid.getnode()", "hwid_call", "py_wrapper", site, pid,
                             value=r.get("value"), detail=f"returned MAC {r.get('value')}")
                elif k == "gui":
                    self.add("gui_usage", r.get("api"), "gui_call", "py_wrapper", site, pid,
                             detail=r.get("error") or ("GUI call made (windows were off during the trace)"
                                                       if r.get("gui_off") else "GUI call made"))
                elif k == "sqlite":
                    if r.get("db") and r["db"] != ":memory:":
                        self.add("data_file_read", self.rel(str(r["db"])), "sqlite", "py_hook", site, pid,
                                 detail="sqlite3 database (also written)")
                elif k == "path_check" and not r.get("exists"):
                    missing.append((r, pid))
                elif k == "warning":
                    self.warnings.append(f"pid {pid}: {r.get('what')}: {r.get('error')}")
            # (a parent package imported on the way to a submodule is kept: its __init__ ran too)
            for (s, kind), names in local_groups.items():
                for n, r in names:
                    cat, mech = kind
                    self.add(cat, n, mech, "py_hook", list(s) if s else None, pid, resolved_path=r.get("file"),
                             detail=r.get("_detail"))
        self.fact_writes(writes, dirs)
        self.fact_missing(missing)
        for f in [f for f in self.facts.values() if f["category"] == "dynamic_import" and
                  f["mechanism"] in ("exec_file", "spec_from_file_location")]:
            self.facts.pop(("data_file_read", f["subject"]), None)    # the source read of an exec'd file
        for ep, only in self.dl_only.items():
            f = self.facts.get(("network_endpoint", ep))
            if only and f:
                f["required_at_runtime"] = "conditional"
                f["detail"].append("only used to download files: not needed once they are baked in / cached")
        self.fact_shm()
        self.fact_threads()
        self.fact_strace()
        self.write_roots()

    def fact_import(self, r, pid, local_groups):
        kind, ck, site = r.get("kind"), r.get("caller_kind"), r.get("site")
        if r.get("sibling"):
            won = r.get("file") if kind == "local" else r["mod"]
            subj = won if kind == "local" else r["sibling"]
            self.add("shadowing", subj, "import_stmt", "py_hook", site, pid,
                     aliases=[x for x in (r["sibling"], won) if x != subj],
                     detail=f"'import {r['mod']}' in {site[0]} loaded {won}, not the file next to it: {r['sibling']}")
        if kind == "local":
            if ck == "pickle":
                key = ("dynamic_import", "pickle_class_import")
                r["_detail"] = f"imported while unpickling (via {r.get('caller')})"
            elif ck == "importlib":
                key = ("dynamic_import", "importlib")
                r["_detail"] = "importlib.import_module"
            elif ck == "dunder_import":
                key = ("dynamic_import", "dunder_import")
                r["_detail"] = "__import__ / C-level import"
            else:
                key = ("local_module", "import_stmt")
            local_groups[(tuple(site or ()), key)].append((r["mod"], r))
        elif kind == "third_party" and site and r.get("direct"):
            top = r["mod"].split(".")[0]
            ds = self.col.get("dists", {}).get(top) if isinstance(self.col.get("dists"), dict) else None
            loaded = self.col.get("dist_loaded", {}).get(top)
            subj = loaded or (ds[0] if ds else top)
            mech = {"pickle": "pickle_class_import", "importlib": "importlib", "dunder_import": "dunder_import"}.get(
                ck, "import_stmt")
            det = None
            if ds and len(ds) > 1:
                det = f"'{top}' is installed by {len(ds)} distributions: {', '.join(ds)}" + (
                    f"; the loaded files belong to {loaded}" if loaded else "")
            self.add("third_party_import", subj.lower().replace("_", "-"), mech, "py_hook", site, pid,
                     import_name=top, detail=det, version=self.col.get("dist_versions", {}).get(subj))

    def fact_missing(self, missing):
        """Checked with os.path.exists/isfile and absent - unless the run created it later (downloads, caches)."""
        made = {p for f in self.facts.values() if f["category"] in ("file_write", "model_file", "config_file",
                                                                     "data_file_read")
                for p in (f.get("paths") or []) + [f["subject"] if str(f["subject"]).startswith("/")
                                                    else self.root + "/" + str(f["subject"])]}
        for r, pid in missing:
            p = r["path"]
            if p in made or any(str(m).startswith(p + "/") for m in made):
                continue
            sub = self.rel(p)
            self.add("dangling_reference", sub, "existence_check", "py_wrapper", r.get("site"), pid, required="no",
                     detail=f"os.path.{r.get('fn')}() found it missing; the code went on without it (fallback)")

    def fact_declared_package(self, r, pid):
        """A package the requirements declare, first loaded by another library (numpy via cv2): the project's own
        later `import numpy` finds it already loaded, so no import event exists for it."""
        top = r["top"]
        ds = (self.col.get("dists") or {}).get(top) or []
        reqs = set(self.col.get("requirements") or [])
        hit = [d for d in ds if d.lower().replace("_", "-") in reqs]
        if not hit:
            return
        subj = (self.col.get("dist_loaded") or {}).get(top) or hit[0]
        self.add("third_party_import", subj.lower().replace("_", "-"), "loaded_by_dependency", "py_hook",
                 r.get("site"), pid, import_name=top, version=self.col.get("dist_versions", {}).get(subj),
                 detail=f"declared in requirements; first loaded by {r.get('caller')} (the project's own import "
                        f"of it, if any, found it already loaded)")

    def fact_read(self, p, mech, layer, site, pid, what, force_cat=None):
        od = self.os_data(p)
        if od and not force_cat:
            subj, m = od
            pkg = self.col.get("owners", {}).get(p)
            self.add("os_data", subj, m, layer, site, pid, paths=[p], package=pkg,
                     detail=f"read {p}" + (f" (Debian package {pkg})" if pkg else ""))
            if pkg:
                self.syspkg(pkg, m, layer, site, pid, f"provides {p}")
            return
        if p.startswith(self.root + "/"):
            rp = self.rel(p)
            self.add(force_cat or self.file_cat(rp), rp, mech, layer, site, pid, detail=what)
        else:
            al = self.alias(p)
            self.add(force_cat or self.file_cat(p), p, mech, layer, site, pid, detail=what,
                     aliases=[al] if al else None)

    def fact_open(self, r, pid, writes, imported_files):
        p, site = r["path"], r.get("site")
        if r.get("w"):
            writes[(tuple(site or ()), "python_io", "py_hook")].append((p, pid))
            return
        if p.startswith(("/sys/class/net/", "/etc/machine-id", "/var/lib/dbus/machine-id")):
            self.add("hardware_identity", p, "python_io", "py_hook", site, pid, detail="machine identity read")
            return
        od = self.os_data(p)
        if od:
            if od[1] == "font" and not r.get("direct"):
                return      # a library scanning the system fonts (matplotlib's font cache)
            return self.fact_read(p, "python_io", "py_hook", site, pid, f"open() by {r.get('caller')}")
        if p.startswith(self.root + "/"):
            rp = self.rel(p)
            if rp.endswith(".py") and rp in imported_files:
                return
            return self.fact_read(p, "python_io", "py_hook", site, pid, f"open() by {r.get('caller')}")
        caller = r.get("caller") or ""
        if site and (is_proj_caller(caller, site) or p.lower().endswith(MODEL_EXT)):
            self.fact_read(p, "python_io", "py_hook", site, pid, f"open() by {caller}")

    def fact_env(self, r, pid):
        name, site = r["name"], r.get("site")
        if r["op"] == "set":
            mech, det = "env_set", f"set by the code to {r.get('value')!r}"
        elif r.get("missing"):
            mech = "env_default" if r.get("how") == "get" else "env_required"
            det = (f"not set - default {r.get('default')!r} used" if r.get("default") is not None
                   else "not set - read returned nothing" if r.get("how") == "get"
                   else "not set - os.environ[...] raised KeyError")
        else:
            mech = "env_default" if r.get("how") == "get" else "env_required"
            det = f"set in scenario: {r.get('value')!r}"
        if r.get("via"):
            det += f" (read by {r['via']})"
        f = self.add("env_var", name, mech, "py_hook", site, pid, detail=det)
        if r["op"] != "set" and not r.get("missing"):
            f["value"] = r.get("value")
        if is_secret(name) and r["op"] != "set":
            self.add("secret", name, "env_var", "py_hook", site, pid,
                     detail="secret read from the environment: pass it at runtime (env / Docker secret)")

    def fact_http(self, r, pid):
        url, site = str(r.get("url") or ""), r.get("site")
        m = re.match(r"^(\w+)://(?:[^@/]*@)?([^:/?#]+)(?::(\d+))?([^?#]*)", url)
        if not m or not site:
            return
        scheme, host, port, path = m.group(1), m.group(2), m.group(3), m.group(4)
        port = port or {"http": "80", "https": "443"}.get(scheme, "?")
        layer = "py_wrapper" if r.get("via") == "requests" else "py_hook"
        self.add("network_endpoint", f"{host}:{port}", "python_io", layer, site, pid, host=host, port=str(port),
                 detail=f"{r.get('method') or 'GET'} {url}")
        dl = (r.get("method") or "GET").upper() == "GET" and path.lower().endswith(DOWNLOAD_EXT)
        self.dl_only[f"{host}:{port}"] = self.dl_only.get(f"{host}:{port}", True) and dl
        if dl:
            self.add("runtime_download", url.split("?")[0], "python_io", layer, site, pid, required="conditional",
                     detail=f"downloaded at runtime via {r.get('via')}"
                     + (f" (HTTP {r['status']})" if r.get("status") else ""))

    def fact_exec(self, r, pid):
        argv = r.get("argv") or []
        site = r.get("site")
        if not site:
            return
        if not r.get("direct") and not r.get("shell"):
            self.lib_exec.setdefault(os.path.basename(str(argv[0] if argv else r.get("executable"))),
                                     set()).add(f"{r.get('caller')} (from {site[0]}:{site[1]})")
            return
        if r.get("shell"):
            b = (argv[0].split() or ["sh"])[0]
            det = f"os.system: {argv[0][:120]}"
        else:
            b = os.path.basename(str(argv[0] if argv else r.get("executable")))
            det = f"{r.get('caller')}: {' '.join(map(str, argv))[:160]}"
        req = "yes"
        if (r.get("caller") or "").endswith("ctypes/util.py"):
            req, det = "conditional", det + " (run by ctypes.util.find_library)"
        path = r.get("executable") if str(r.get("executable") or "").startswith("/") else None
        path = path or next((x for x in self.col.get("binaries", []) if os.path.basename(x) == b), None)
        pkg = self.col.get("owners", {}).get(path) if path else None
        self.add("subprocess_binary", b, "subprocess", "py_hook", site, pid, required=req, detail=det,
                 path=path, package=pkg)
        if pkg:
            self.syspkg(pkg, "binary", "py_hook", site, pid, f"provides {path}", required=req)

    def fact_dlopen(self, r, pid, resolved):
        name, site = r["name"], r.get("site")
        if not site or not r.get("direct"):
            self.lib_dlopen.setdefault(os.path.basename(name), set()).add(str(r.get("caller")))
            return
        layer = "py_wrapper" if r.get("k") == "dlopen_resolved" else "py_hook"
        path = r.get("path") if r.get("k") == "dlopen_resolved" else resolved.get((pid, name))
        if r.get("error"):
            self.add("native_library", os.path.basename(name), "ctypes", layer, site, pid, required="conditional",
                     detail=f"ctypes.CDLL failed: {r['error']}")
            return
        if path is None and not name.startswith("/"):
            libs = [lib for x in self.ev.get(pid, []) if x.get("k") == "exit" for lib in x.get("libs", [])]
            path = next((lib for lib in libs if os.path.basename(lib) == name), None)
        elif path is None:
            path = name
        pkg = self.col.get("owners", {}).get(path) if path else None
        bundled = bool(path and "-packages/" in path)
        det = f"ctypes.CDLL('{name}') -> {path or '?'}"
        if bundled:
            det += (" - an already-loaded copy bundled in a pip wheel answered the call: the system package is "
                    "not what gets used here")
        self.add("native_library", os.path.basename(name), "ctypes", layer, site, pid, path=path, package=pkg,
                 detail=det)
        if pkg and not bundled:
            self.syspkg(pkg, "dlopen", layer, site, pid, f"provides {path}")

    def fact_font(self, r, pid):
        site = r.get("site")
        if str(r.get("requested", "")).startswith("<"):
            return      # a font object built from memory (PIL's load_default fallback), not a file
        name = os.path.basename(str(r.get("requested")))
        if r.get("ok"):
            res = r.get("resolved")
            pkg = self.col.get("owners", {}).get(res) if res else None
            self.add("os_data", f"font:{name}", "font", "py_wrapper", site, pid, paths=[res] if res else None,
                     package=pkg, required="conditional",
                     detail=f"ImageFont.truetype found {res}" + (f" (Debian package {pkg})" if pkg else ""))
            if pkg:
                self.syspkg(pkg, "font", "py_wrapper", site, pid, f"provides {res}", required="conditional")
        else:
            self.add("os_data", f"font:{name}", "font", "py_wrapper", site, pid, required="conditional",
                     detail=f"font NOT found ({r.get('error')}) - the code's fallback was used")

    def syspkg(self, pkg, mech, layer, site, pid, why, required="yes"):
        info = self.col.get("packages", {}).get(pkg, {})
        if info.get("essential") or info.get("priority") == "required" or pkg in PY_SLIM_BASE:
            return      # already in python:3.x-slim
        self.add("system_package_candidate", pkg, mech, layer, site, pid, required=required,
                 detail=f"{why}; priority {info.get('priority', '?')}", version=info.get("version"))

    def fact_writes(self, writes, dirs):
        for (site, mech, layer), items in list(writes.items()) + [((s, "mkdir", lay), it)
                                                                   for (s, _, lay), it in dirs.items()]:
            paths = [p for p, _ in items]
            pids = {pid for _, pid in items}
            if mech == "video":
                for t in template(paths):
                    host = re.match(r"^\w+://(?:[^@/]*@)?([^:/?#]+)(?::(\d+))?", t)
                    for pid in pids:
                        self.add("network_endpoint", t, "c_level_io", layer, list(site) or None, pid,
                                 host=host.group(1) if host else None, port=host.group(2) if host else None,
                                 paths=sorted(set(paths))[:20],
                                 detail=f"cv2.VideoCapture of {len(set(paths))} stream URL(s)")
                continue
            for t in template(paths):
                sub = self.rel(t) if t.startswith(self.root + "/") else t
                al = self.alias(t)
                mine = sorted({p for p in paths if len(p.split("/")) == len(t.split("/"))})
                for pid in pids:
                    what = "directory created (os.mkdir/makedirs)" if mech == "mkdir" else \
                        ("cv2.imwrite" if mech == "c_level_io" else "opened for writing")
                    f = self.add("file_write", sub, "mkdir" if mech == "mkdir" else mech, layer,
                                 list(site) or None, pid, paths=mine[:20], aliases=[al] if al else None,
                                 detail=what, key=("file_write", sub))
                    f["files_seen"] = max(f.get("files_seen", 0), len(mine))

    def write_roots(self):
        """An env var that points at a directory the app writes below -> the directory to mount."""
        for d, name in self.env_dirs.items():
            below = [f for f in self.facts.values() if f["category"] == "file_write" and
                     any(str(p).startswith(d + "/") for p in f.get("paths", []))]
            if not below:
                continue
            envf = self.facts.get(("env_var", name))
            n = sum(x.get("files_seen", 1) for x in below)
            f = self.add("file_write", d, "write_root", "py_hook", None, None,
                         detail=f"{n} path(s) written below it; location set by env {name}",
                         aliases=[name], key=("file_write", d))
            if envf:
                for s in envf["sites"]:
                    if s not in f["sites"]:
                        f["sites"].append(s)
                f["processes"] = sorted({p for x in below for p in x["processes"]})

    def fact_threads(self):
        """Thread pools per process (read at exit): N processes x T threads > CPUs = oversubscription."""
        cpus = self.col.get("system", {}).get("cpu_count")
        for lib, key, api in (("OpenCV", "cv2_threads", "cv2.setNumThreads"), ("torch", "torch_threads", "torch.set_num_threads")):
            per = [(p["role"], p[key]) for p in self.procs.values() if p.get(key)]
            if not per:
                continue
            by_trace = defaultdict(int)       # separate containers do not share CPUs
            for p in self.procs.values():
                if p.get(key):
                    by_trace[p["pid"] // PID_STRIDE] += p[key]
            total = max(by_trace.values())
            groups = defaultdict(list)
            for role, n in per:
                groups[(role, n)].append(role)
            parts = ", ".join(f"{len(v)} x {r} = {n}" for (r, n), v in sorted(groups.items()))
            over = bool(cpus) and total > cpus
            set_by = self.facts.get(("thread_config", api))
            unset = sorted({r for r, _ in per if set_by is None or r not in set_by["processes"]})
            det = f"{lib} threads per process: {parts}; {total} in total on {cpus or '?'} CPUs" + (
                " (largest container)" if len(by_trace) > 1 else "")
            if over:
                det += " - oversubscribed"
            if set_by is not None and unset:
                det += f"; {api} only runs in {', '.join(set_by['processes'])}, not in {', '.join(unset)}"
            f = self.add("thread_config", f"threads:{lib.lower()}", "exit_snapshot", "py_hook",
                         None, None, detail=det, total_threads=total, cpu_count=cpus, oversubscribed=over,
                         per_process=[{"role": r, "threads": n} for r, n in per])
            if set_by is not None:
                f["sites"] = list(set_by["sites"])
            f["processes"] = sorted({r for r, _ in per})

    def fact_shm(self):
        f = self.facts.get(("ipc_shared_memory", "multiprocessing.shared_memory.SharedMemory"))
        if not f:
            return
        segs = {s["name"]: s["bytes"] or 0 for s in f.get("segments", [])}
        total = sum(segs.values())
        cap = self.col.get("system", {}).get("dev_shm_bytes")
        mb = lambda b: f"{b / 1e6:.1f} MB"
        f["total_bytes"] = total
        f["segments"] = [{"name": k, "bytes": v} for k, v in segs.items()]
        f["detail"].append(f"{len(segs)} segment(s) created, {mb(total)} in total"
                           + (f"; /dev/shm in this container: {mb(cap)}" if cap else "")
                           + ("; Docker's default /dev/shm is 64 MB" if total > 64e6 else ""))
        need = int(total * 1.5 / 2 ** 20) + 1
        f["shm_size_min"] = f"{need}m"

    def fact_strace(self):
        st = []
        for run in self.runs:
            for e in parse_strace(os.path.join(run["_dir"], "strace.log"), run.get("cwd") or self.cwd):
                e["pid"] += run["_offset"]
                e["_out"] = run.get("out")
                st.append(e)
        if not st:
            return
        rev = self.col.get("reverse_dns", {})
        dnsmap = {ip: h for h, ips in self.col.get("dns", {}).items() if isinstance(ips, list) for ip in ips}
        idx_paths = defaultdict(list)
        tmpl = []       # (regex of a templated subject, fact): /data/output/recognized/<var>/<var>.jpg
        for f in self.facts.values():
            for p in f.get("paths", []) or []:
                idx_paths[p].append(f)
            subj = str(f["subject"])
            full = subj if subj.startswith("/") else self.root + "/" + subj
            if f["category"] in ("file_write", "model_file", "config_file", "data_file_read") and "<var>" in subj:
                tmpl.append((re.compile("^" + re.escape(full).replace(re.escape("<var>"), "[^/]+") + "$"), f))
        new_writes = []
        exe = {}            # pid -> binary it runs (from execve)
        for e in st:
            if e["call"] == "execve" and e.get("ok"):
                exe[e["pid"]] = os.path.basename(e["path"])
        ends = defaultdict(list)
        for f in self.facts.values():
            if f["category"] == "network_endpoint" and f.get("host") and f.get("port"):
                ends[(str(f["host"]), str(f["port"]))].append(f)
        for e in st:
            pid = e["pid"]
            if not e.get("ok"):
                continue
            if e["call"] == "execve":
                b = os.path.basename(e["path"])
                if re.match(r"^python[\d.]*$", b) or b == "strace" or b in self.lib_exec:
                    continue
                pkg = self.col.get("owners", {}).get(e["path"])
                f = self.facts.get(("subprocess_binary", b))
                if f:
                    f["layers"] = sorted(set(f["layers"]) | {"os_trace"})
                else:
                    self.add("subprocess_binary", b, "execve", "os_trace", None, None, path=e["path"], package=pkg,
                             detail=f"executed: {' '.join(e.get('argv', []))[:160]}")
                continue
            if e["call"] in ("connect", "bind") and e.get("host"):
                if e["host"].startswith("127.") or e["host"] in ("::1",):
                    continue
                host = dnsmap.get(e["host"]) or e["host"]
                if e["call"] == "bind":
                    if e["port"]:
                        self.add("listen_port", f"{e['host']}:{e['port']}", "socket_bind", "os_trace", None, None,
                                 port=str(e["port"]))
                    continue
                hit = ends.get((host, str(e["port"]))) or ends.get((e["host"], str(e["port"])))
                if hit:
                    for f in hit:
                        if "os_trace" not in f["layers"]:
                            f["layers"].append("os_trace")
                elif e["port"] != 53:
                    self.add("network_endpoint", f"{host}:{e['port']}", "os_level", "os_trace", None, None,
                             host=host, port=str(e["port"]),
                             detail=f"connect() to {e['host']}" + (f" ({rev[e['host']]})" if e["host"] in rev else "")
                             + f" by pid {pid} (seen only by strace)")
                continue
            p = e.get("path")
            if not p or e.get("dir") or (e.get("_out") and p.startswith(e["_out"] + "/")):
                continue
            if exe.get(pid) in self.lib_exec:      # a helper binary a library started (fc-list, ldconfig)
                if e.get("write"):
                    self.lib_exec[exe[pid]].add(f"writes {os.path.dirname(p)}/ (strace)")
                continue
            hit = idx_paths.get(p) or [f for rx, f in tmpl if rx.match(p) and (f["category"] == "file_write") ==
                                       bool(e.get("write"))]
            if hit:
                for f in hit:
                    if "os_trace" not in f["layers"]:
                        f["layers"].append("os_trace")
                continue
            if e.get("write"):
                if not p.startswith(("/dev/", "/proc/")) and "__pycache__" not in p:
                    new_writes.append(p)
            elif p.startswith(self.root + "/") and not p.endswith((".py", ".pyc")) and not os.path.isdir(p):
                rp = self.rel(p)
                hit = self.facts.get((self.file_cat(rp), rp)) or self.facts.get(("model_file", rp))
                if hit:
                    if "os_trace" not in hit["layers"]:
                        hit["layers"].append("os_trace")
                elif "." in os.path.basename(rp):
                    self.add(self.file_cat(rp), rp, "os_level", "os_trace", None, None,
                             detail="opened (seen only by strace)")
            elif self.os_data(p):
                subj, m = self.os_data(p)
                hit = self.facts.get(("os_data", subj))
                if hit:
                    if "os_trace" not in hit["layers"]:
                        hit["layers"].append("os_trace")
                elif m != "font":        # font files opened by library font scans (matplotlib) are not app needs
                    pkg = self.col.get("owners", {}).get(p)
                    self.add("os_data", subj, m, "os_trace", None, None, paths=[p], package=pkg,
                             detail=f"opened {p}" + (f" (Debian package {pkg})" if pkg else ""))
            elif p.lower().endswith(MODEL_EXT) and "-packages/" not in p:
                hit = self.facts.get(("model_file", p))
                if hit:
                    if "os_trace" not in hit["layers"]:
                        hit["layers"].append("os_trace")
                else:
                    al = self.alias(p)
                    self.add("model_file", p, "os_level", "os_trace", None, None, aliases=[al] if al else None,
                             detail="opened (seen only by strace)")
        for t in template(new_writes):
            sub = self.rel(t) if t.startswith(self.root + "/") else t
            al = self.alias(t)
            self.add("file_write", sub, "os_level", "os_trace", None, None, aliases=[al] if al else None,
                     paths=sorted({p for p in new_writes if len(p.split("/")) == len(t.split("/"))})[:20],
                     detail="written - seen only by strace (C code or a non-Python process)")
        for f in self.facts.values():     # native libraries / system packages seen loading
            if f["category"] in ("native_library",) and f.get("path"):
                if any(e.get("path") == f["path"] and e.get("ok") for e in st):
                    if "os_trace" not in f["layers"]:
                        f["layers"].append("os_trace")

    # ------------------------------------------------------------ output
    def observations(self):
        tops, libs = {}, {}
        env_read, env_lib, set_names = set(), defaultdict(set), set()
        for pid, recs in self.ev.items():
            for r in recs:
                k = r.get("k")
                if k == "exit":
                    for t, fp in (r.get("packages") or {}).items():
                        tops.setdefault(t, fp)
                    for lib in r.get("libs", []):
                        if "-packages/" not in lib and "/lib-dynload/" not in lib:
                            libs.setdefault(lib, set()).add(self.role(pid))
                elif k == "env":
                    env_read.add(r["name"])
                elif k == "env_lib":
                    env_lib[r["name"]].add(r.get("caller"))
                elif k == "start" and self.role(pid).startswith("main"):
                    set_names.update(r.get("env_names", []))
        dists = self.col.get("dists", {}) if isinstance(self.col.get("dists"), dict) else {}
        vers = self.col.get("dist_versions", {})
        pk = []
        for t in sorted(tops):
            ds = dists.get(t) or []
            pk.append({"import_name": t, "distributions": ds, "versions": {d: vers.get(d) for d in ds}})
        owners, info = self.col.get("owners", {}), self.col.get("packages", {})
        sl = []
        for lib in sorted(libs):
            pkg = owners.get(lib)
            sl.append({"path": lib, "package": pkg, "priority": info.get(pkg, {}).get("priority") if pkg else None,
                       "in_python_slim": (pkg in PY_SLIM_BASE) if pkg else None, "processes": sorted(libs[lib])})
        unread = sorted(n for n in set_names - env_read if n not in ENV_BORING and not n.startswith(IMAGE_ENV_PREFIXES))
        return {"packages_loaded": pk, "system_libraries": sl, "env_set_but_not_read_by_code": unread,
                "binaries_run_by_libraries": {k: sorted(v) for k, v in sorted(self.lib_exec.items())},
                "libraries_dlopened_by_libraries": {k: sorted(v) for k, v in sorted(self.lib_dlopen.items())},
                "env_read_by_libraries": {k: sorted(x for x in v if x) for k, v in sorted(env_lib.items())},
                "system": self.col.get("system", {}), "dns": self.col.get("dns", {})}

    def coverage(self):
        cov = defaultdict(set)
        for recs in self.ev.values():
            for r in recs:
                if r.get("k") == "cov":
                    for fn, lines in r["lines"].items():
                        cov[fn].update(lines)
        return {fn: ranges(sorted(v)) for fn, v in sorted(cov.items()) if not fn.startswith("/")}

    def output(self):
        self.build()
        facts = []
        for f in self.facts.values():
            f["detail"] = "; ".join(d for d in f["detail"] if d)
            f["layers"] = sorted(f["layers"])
            for k in ("paths", "aliases"):
                if k in f and not f[k]:
                    del f[k]
            facts.append(f)
        order = lambda f: ((f["sites"][0]["file"] if f["sites"] else "~"), (f["sites"][0]["line"] or 0)
                           if f["sites"] else 0, f["category"], str(f["subject"]))
        facts.sort(key=order)
        for i, f in enumerate(facts, 1):
            f["id"] = f"R-{i:04d}"
            f["docker_implication"] = implication(f)
        by = defaultdict(int)
        for f in facts:
            by[f["category"]] += 1
        byl = defaultdict(int)
        for f in facts:
            for lay in f["layers"]:
                byl[lay] += 1
        scen = [{k: r.get(k) for k in ("cmd", "exit_code", "duration_s", "strace", "driver", "driver_exit_code",
                                        "stopped_by")} for r in self.runs]
        return {"tool": "xray-trace", "version": VERSION, "python": self.col.get("system", {}).get("python"),
                "root": self.root, "entrypoints": [" ".join(r.get("cmd") or []) for r in self.runs],
                "cwd": self.cwd, "scenario": scen[0] if len(scen) == 1 else scen,
                "summary": {"facts": len(facts), "by_category": dict(sorted(by.items())), "by_layer": dict(byl),
                            "processes": len(self.procs), "exit_code": self.run.get("exit_code"),
                            "traces": len(self.runs)},
                "facts": facts, "processes": sorted(self.procs.values(), key=lambda p: p["pid"]),
                "resources": self.resources(),
                "coverage": self.coverage(), "observations": self.observations(), "warnings": self.warnings}


def is_proj_caller(caller, site):
    return bool(site) and caller == site[0]


def ranges(lines):
    out, start, prev = [], None, None
    for n in lines:
        if start is None:
            start = prev = n
        elif n == prev + 1:
            prev = n
        else:
            out.append([start, prev] if start != prev else start)
            start = prev = n
    if start is not None:
        out.append([start, prev] if start != prev else start)
    return out


def unranges(rs):
    s = set()
    for r in rs:
        if isinstance(r, list):
            s.update(range(r[0], r[1] + 1))
        else:
            s.add(r)
    return s


def implication(f):
    c, s = f["category"], f["subject"]
    if c == "third_party_import":
        if f.get("required_at_runtime") == "conditional":
            return f"optional import ({s}): not installed and the code copes"
        return f"requirements: {s}" + (f"=={f['version']}" if f.get("version") else "")
    if c in ("local_module", "dynamic_import"):
        return f"COPY {f.get('resolved_path') or s}"
    if c in ("model_file", "config_file", "data_file_read"):
        if not str(s).startswith("/"):
            return f"COPY {s} (read at runtime)"
        return f"provide at runtime: bake into the image or mount ({(f.get('aliases') or [s])[0]})"
    if c == "file_write":
        if f.get("mechanism") == "write_root":
            return f"volume: mount {s} (the app writes below it)"
        if str(s).startswith("/tmp"):
            return f"writable: {s} (ephemeral is fine)"
        return f"writable path: {s} (mount a volume if it must survive restarts; chown for a non-root USER)"
    if c == "env_var":
        return f"secret: pass {s} at runtime, never ENV in the image" if is_secret(s) else \
            f"compose environment: {s}" + (f"={f['value']}" if f.get("value") not in (None, "***") else "")
    if c == "secret":
        return "never bake: env var / Docker secret / runtime mount"
    if c == "network_endpoint":
        return f"compose: {f.get('host') or s} must be reachable (depends_on)"
    if c == "runtime_download":
        return "bake into the image at build time or mount a cache volume"
    if c == "listen_port":
        return f"ports / EXPOSE {f.get('port') or s}"
    if c == "subprocess_binary":
        return f"apt: {f['package']} (provides {s})" if f.get("package") else f"binary {s} must be on PATH"
    if c in ("native_library", "system_package_candidate"):
        return f"apt: {f.get('package') or s}" if c == "native_library" else f"apt: {s}"
    if c == "os_data":
        return f"apt: {f['package']}" if f.get("package") else "OS data file must exist in the image"
    if c == "ipc_shared_memory":
        return f"compose: shm_size >= {f.get('shm_size_min', '?')}"
    if c == "multiprocessing":
        return "child processes re-import the main module (spawn): keep it import-safe"
    if c == "gpu_usage":
        return "GPU not used in this run (CPU fallback)" if f.get("result") is False or "CPUExecution" in str(
            f.get("detail")) and "CUDA" not in str(f.get("detail")).split("active")[-1] else "GPU used: needs --gpus"
    if c == "thread_config":
        if f.get("oversubscribed"):
            return ("oversubscribed: limit threads in EVERY process (OMP_NUM_THREADS / setNumThreads in the child "
                    "- a code change for cv2) or give the container more CPUs")
        return "size thread pools per process (N processes x threads <= CPUs)"
    if c == "hardware_identity":
        return "compose: fix mac_address / hostname if the value matters"
    if c == "gui_usage":
        return "needs a display: GUI OpenCV + X11, or keep this path off in containers"
    if c == "device":
        return "docker run --device"
    return ""


def cmd_facts(a):
    b = Builder(a.trace, a.root)
    out = b.output()
    jdump(out, a.out)
    s = out["summary"]
    print(f"xray-trace {VERSION}: {s['facts']} runtime facts from {s['processes']} Python process(es) -> {a.out}")
    print("  by layer: " + ", ".join(f"{k} {v}" for k, v in sorted(s["by_layer"].items())))
    for p in out["processes"]:
        print(f"  pid {p['pid']:>6} {p['role']:<28} clean_exit={p['clean_exit']}")
    if out["warnings"]:
        print(f"  {len(out['warnings'])} tracer warning(s): see 'warnings' in the output")
    return 0


# ============================================================================ merge (host)
class StmtIndex:
    """Map a (file, line) to the line range of the statement that contains it."""

    def __init__(self, root):
        self.root, self.cache = root, {}

    def stmts(self, rel):
        if rel not in self.cache:
            nodes = []
            try:
                with open(os.path.join(self.root, rel), encoding="utf-8", errors="replace") as fh:
                    tree = ast.parse(fh.read())
                for n in ast.walk(tree):
                    if isinstance(n, ast.stmt):
                        a, b = n.lineno, getattr(n, "end_lineno", n.lineno) or n.lineno
                        body = getattr(n, "body", None)
                        if isinstance(body, list) and body and isinstance(body[0], ast.stmt):
                            b = max(a, body[0].lineno - 1)   # header of a compound statement
                            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.decorator_list:
                                a = n.decorator_list[0].lineno
                        nodes.append((a, b))
            except (OSError, SyntaxError, ValueError):
                nodes = None
            self.cache[rel] = nodes
        return self.cache[rel]

    def span(self, rel, line):
        nodes = self.stmts(rel)
        if not nodes:
            return (line, line)
        best = None
        for a, b in nodes:
            if a <= line <= b and (best is None or b - a < best[1] - best[0]):
                best = (a, b)
        return best or (line, line)


def cmd_merge(a):
    sys.path.insert(0, HERE)
    from xray_score import fam, strength, site_overlap
    st, rt = jload(a.static), jload(a.runtime)
    root = a.root or st.get("root")
    idx = StmtIndex(root) if root and os.path.isdir(root) else None
    cov = {fn: unranges(r) for fn, r in (rt.get("coverage") or {}).items()}
    has_cov = bool(cov)

    def executed(f, primary=False):
        """True/False: did a line of this fact's site run in the scenario? None: unknown.
        primary=True looks only at the first site (the operation itself, not the constants it uses)."""
        if not has_cov or not f.get("sites"):
            return None
        seen_any = False
        for s in f["sites"][:1] if primary else f["sites"]:
            fn, ln = s.get("file"), s.get("line")
            if not fn or not ln or not fn.endswith(".py"):
                continue
            seen_any = True
            lines = cov.get(fn)
            if not lines:
                continue
            lo, hi = idx.span(fn, ln) if idx else (ln, ln)
            if any(x in lines for x in range(lo, hi + 1)):
                return True
        return False if seen_any else None

    rfacts = rt["facts"]
    home = next((r.get("value") for r in rfacts if r["category"] == "env_var" and r["subject"] == "HOME"
                 and r.get("value")), None) or "/root"

    def expand(f):
        """static '~/.cache/x' -> '/root/.cache/x' (HOME seen at runtime) for matching only"""
        f2 = dict(f)
        for k in ("subject", "resolved_path"):
            v = f2.get(k)
            if isinstance(v, str) and (v == "~" or v.startswith("~/")):
                f2[k] = home + v[1:]
        return f2

    norm = lambda x: str(x).lower().replace("_", "-")
    loaded = (rt.get("observations") or {}).get("packages_loaded") or []
    loaded_tops = {p.get("import_name") for p in loaded}
    loaded_dists = {norm(d) for p in loaded for d in p.get("distributions") or []}
    used = set()
    merged = []
    for f in st["facts"]:
        g = dict(f)
        fx = expand(f)
        cands = [r for r in rfacts if fam(r["category"]) == fam(f["category"])]
        hits = [r for r in cands if strength(fx, r) > 0 and not str(f["subject"]).startswith("<")]
        if str(f["subject"]).startswith("<unresolved") or f.get("evidence") == "unresolved":
            hits = [r for r in cands if site_overlap(f, r) == "exact"] or hits
            if hits:
                g["subject"] = hits[0]["subject"]
                g["resolved_by_runtime"] = True
        ex = executed(f)
        if hits and ex is False:
            # none of this fact's lines ran: a runtime fact with the same name comes from ANOTHER place
            # (import cv2 in the entrypoint vs import cv2 in an unused script) unless the sites overlap
            hits = [r for r in hits if site_overlap(f, r)]
        for r in hits:
            used.add(r["id"])
        failed = bool(hits) and all(r.get("import_failed") for r in hits)
        ran = seen = bool(hits) and not failed
        if not ran:
            ran = executed(f, primary=True)
        # imports are observed exactly: a line that ran without loading this module means another candidate won
        unseen_import = (not seen and ex and f["category"] in IMPORT_CATS and f.get("evidence") != "derived")
        preloaded, note = False, None
        if unseen_import and f["category"] == "third_party_import":
            top = str(f.get("import_name") or f["subject"]).split(".")[0]
            if top in loaded_tops or norm(f["subject"]) in loaded_dists:
                # `import numpy` after cv2 already loaded numpy: no import event, but the module IS used
                unseen_import, preloaded = False, True
        if f.get("evidence") == "derived" and not seen:
            label = "derived"
        elif failed or unseen_import:
            label = "static_only"
        elif seen or ex:
            label = "confirmed"
            if preloaded:
                note = "its line ran; the module was already loaded by another library (no separate import event)"
        elif f.get("evidence") == "unresolved" or str(f["subject"]).startswith("<"):
            label = "unresolved"
        else:
            label = "static_only"
        req = f.get("required_at_runtime")
        capped = any(s.get("cap") for s in f.get("sites", []))
        if label == "confirmed" and f.get("evidence") != "derived" and ran:
            if req == "no" and seen:
                req, note = "yes", "static analysis said not needed, but it ran in the scenario"
            elif req == "no":
                note = "its line ran, but the runtime never saw this used (e.g. only an existence check)"
            elif req == "conditional":
                if capped:
                    note = "ran in the scenario, but the code has a fallback: stays conditional"
                elif hits and all(r.get("required_at_runtime") == "conditional" for r in hits):
                    note = "ran in the scenario, but the run itself shows it is optional: " + "; ".join(
                        r.get("detail", "")[:120] for r in hits[:1])
                elif f["category"] == "runtime_download" or f["category"] == "network_endpoint" and any(
                        r["category"] == "runtime_download" for r in rfacts
                        if any(s in r.get("sites", []) for s in f.get("sites", []))):
                    note = "happened in this run; skipped when the file is already cached/baked"
                else:
                    req, note = "yes", "conditional statically; ran in the scenario"
        elif failed:
            note = "import attempted in the scenario and failed (not installed); the code handled it"
            req = "no"
        elif unseen_import:
            note = "its line ran, but this module was never imported (another candidate / branch was used)"
            if req == "yes":
                req = "conditional"
        elif label in ("static_only", "unresolved") and ex is False and has_cov:
            note = "did not run in the scenario"
        g["label"] = label
        g["required_static"] = f.get("required_at_runtime")
        g["required_at_runtime"] = req
        g["runtime"] = {"seen_by": [r["id"] for r in hits], "layers": sorted({x for r in hits for x in r["layers"]}),
                        "executed": ex}
        if hits and hits[0]["subject"] != f["subject"]:
            g["runtime"]["observed_subject"] = hits[0]["subject"]
        names = [x for r in hits for x in [r["subject"]] + (r.get("aliases") or []) if x != g["subject"]]
        if names:
            g["aliases"] = list(dict.fromkeys((f.get("aliases") or []) + names))
        if note:
            g["runtime"]["note"] = note
        merged.append(g)
    for r in rfacts:
        if r["id"] in used:
            continue
        g = dict(r)
        g["label"] = "confirmed"
        g["required_static"] = None
        g["runtime"] = {"seen_by": [r["id"]], "layers": r["layers"], "executed": True,
                        "note": "found only at runtime (static analysis missed it)"}
        merged.append(g)
    order = lambda f: ((f["sites"][0]["file"] or "") if f.get("sites") else "~",
                       (f["sites"][0]["line"] or 0) if f.get("sites") else 0, f["category"], str(f["subject"]))
    merged.sort(key=order)
    out = dict(st)
    out["tool"] = "xray-merged"
    out["version"] = VERSION
    out["sources"] = {"static": f"{st.get('tool')} {st.get('version')}", "runtime": f"{rt.get('tool')} {rt.get('version')}",
                      "scenario": rt.get("scenario")}
    out["facts"] = merged
    if rt.get("resources"):
        out["resources"] = rt["resources"]
    lab = defaultdict(int)
    for f in merged:
        lab[f["label"]] += 1
    out["summary"] = dict(st.get("summary", {}))
    req = defaultdict(int)
    for f in merged:
        req[f.get("required_at_runtime")] += 1
    out["summary"].update({"facts": len(merged), "by_label": dict(lab), "by_required": dict(req),
                           "runtime_only": sum(1 for f in merged if f.get("required_static") is None
                                               and f["id"].startswith("R-")),
                           "static_not_run": sum(1 for f in merged if f["runtime"].get("executed") is False)})
    out["runtime_observations"] = rt.get("observations")
    out["runtime_processes"] = rt.get("processes")
    out["coverage"] = rt.get("coverage")
    out["runtime_adjustments"] = adjustments(merged, rt)
    jdump(out, a.out)
    s = out["summary"]
    print(f"merged {len(st['facts'])} static + {len(rfacts)} runtime facts -> {len(merged)} facts -> {a.out}")
    print("  labels: " + ", ".join(f"{k} {v}" for k, v in sorted(s["by_label"].items())))
    print(f"  found only at runtime: {s['runtime_only']}; static facts whose line never ran: {s['static_not_run']}")
    for x in out["runtime_adjustments"]:
        print(f"  - {x}")
    return 0


def adjustments(merged, rt):
    """Plain-language notes the runtime adds to the Docker plan."""
    notes = []
    obs = rt.get("observations") or {}
    for f in merged:
        c = f["category"]
        if c == "ipc_shared_memory" and f.get("total_bytes"):
            notes.append(f"shared memory measured: {f['total_bytes'] / 1e6:.1f} MB -> shm_size >= {f.get('shm_size_min')}")
        if c == "system_package_candidate" and f.get("label") == "confirmed" and f["runtime"]["seen_by"]:
            notes.append(f"apt package used at runtime: {f['subject']} ({f.get('detail', '')[:90]})")
        if c == "thread_config" and f.get("oversubscribed"):
            notes.append(f"thread oversubscription: {f.get('detail', '')}")
        if c == "gpu_usage" and f.get("result") is False:
            notes.append("GPU probe returned False: the run used the CPU fallback (no --gpus needed for this scenario)")
        if f["runtime"].get("note", "").startswith("static analysis said not needed"):
            notes.append(f"static said 'not needed' but it ran: {c} {f['subject']}")
        if f["runtime"].get("note", "").startswith("found only at runtime") and c in (
                "network_endpoint", "file_write", "model_file", "runtime_download", "dynamic_import",
                "subprocess_binary", "os_data"):
            notes.append(f"found only at runtime: {c} {f['subject']}")
    unread = obs.get("env_set_but_not_read_by_code") or []
    if unread:
        notes.append("env vars set for the container but never read by the code: " + ", ".join(unread)
                     + " (a hardcoded value may be used instead - check before relying on them)")
    return list(dict.fromkeys(notes))


# ============================================================================ main
def main(argv=None):
    for _s in (sys.stdout, sys.stderr):   # Windows consoles / pipes: never crash on tree characters or paths
        if hasattr(_s, "reconfigure"):
            _s.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="X-Ray Phase 2 runtime tracer")
    sub = ap.add_subparsers(dest="cmd_name", required=True)
    r = sub.add_parser("run", help="(in the container) run the app under the tracer")
    r.add_argument("--out", required=True, help="folder for the raw trace (mount it from the host)")
    r.add_argument("--root", default=None, help="project root inside the container (default: current dir)")
    r.add_argument("--strace", action="store_true", help="also trace syscalls with strace -f (os_trace layer)")
    r.add_argument("--no-coverage", action="store_true", help="do not record which project lines ran")
    r.add_argument("--driver", default=None,
                   help="shell command run while the app runs (e.g. requests to a server); then the app gets Ctrl+C")
    r.add_argument("--stop-after", type=float, default=None, help="send Ctrl+C to the app after this many seconds")
    r.add_argument("--camera", action="append", metavar="INDEX=SOURCE",
                   help="replace camera INDEX with a video file (replayed in a loop) or a stream URL, without "
                        "changing the app code, e.g. --camera 0=/data/clip.mp4 (repeatable)")
    r.add_argument("--gui", choices=("keep", "off"), default="keep",
                   help="off = cv2 windows are not opened (no screen in a container), waitKey returns no key")
    r.add_argument("--gui-save", type=float, default=5.0,
                   help="with --gui off: save the shown frame every S seconds to <out>/gui/ (0 = never)")
    r.add_argument("cmd", nargs=argparse.REMAINDER, help="-- command to run")
    f = sub.add_parser("facts", help="(host) raw trace folder -> X-Ray runtime fact JSON")
    f.add_argument("--trace", required=True, action="append",
                   help="raw trace folder; repeat it to combine several runs (one per entrypoint)")
    f.add_argument("--root", default=None, help="fixture folder on the host (optional)")
    f.add_argument("--out", required=True)
    m = sub.add_parser("merge", help="(host) static map + runtime facts -> merged map")
    m.add_argument("--static", required=True)
    m.add_argument("--runtime", required=True)
    m.add_argument("--root", default=None, help="fixture folder on the host (to map lines to statements)")
    m.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    return {"run": cmd_run, "facts": cmd_facts, "merge": cmd_merge}[a.cmd_name](a)


if __name__ == "__main__":
    sys.exit(main())
