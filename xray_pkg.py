#!/usr/bin/env python3
"""
xray_pkg.py - X-Ray Phase 3: package analyzer. Stdlib only, Python 3.8+.

Looks at what is INSTALLED in an image (not at the code): Python distributions and their metadata
(Requires-Dist, WHEEL tags, RECORD, sizes), which system libraries their compiled files need (ldd),
which Debian package provides what (dpkg), the files of the project inside the image (duplicates),
and what python:3.x-slim already has (the "baseline"). It then combines that with what the code
imports (static map and/or runtime facts) into facts gradable with xray_score.py --layer package.

  scan      INSIDE the app image:   python /xray/xray_pkg.py scan --root /app --out /xray-out/pkgscan.json
  baseline  INSIDE python:3.x-slim: python /xray/xray_pkg.py baseline --out /xray-out/slim.json
  facts     on the host:
            python3 xray_pkg.py facts --scan pkg-f3/pkgscan.json --baseline pkg-slim/slim.json \\
                --static f3.json --runtime f3-runtime.json --out f3-pkg.json
"""
import argparse
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from collections import defaultdict

VERSION = "0.1.1"
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".mypy_cache", ".pytest_cache"}
DEPLOY_FILES = re.compile(r"(^|/)(Dockerfile[^/]*|docker-compose[^/]*\.ya?ml|compose[^/]*\.ya?ml|\.dockerignore|"
                          r"requirements[^/]*\.txt|README[^/]*|SCENARIO\.md|answer_key\.json|\.gitattributes|"
                          r"\.gitignore)$")


def jdump(obj, p):
    os.makedirs(os.path.dirname(os.path.abspath(p)), exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=1, ensure_ascii=False, default=str)


def jload(p):
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)


def canon(name):
    return re.sub(r"[-_.]+", "-", str(name)).lower()


def run(cmd, timeout=120):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except Exception:
        return ""


def owners(paths):
    """{path: debian package} (tries /usr-merged variants)."""
    if not paths or not shutil.which("dpkg"):
        return {}
    cand = {}
    for p in paths:
        vs = {p, os.path.realpath(p)}
        for v in list(vs):
            if v.startswith(("/usr/lib/", "/usr/bin/", "/usr/sbin/")):
                vs.add(v[4:])
            elif v.startswith(("/lib/", "/bin/", "/sbin/")):
                vs.add("/usr" + v)
        for v in vs:
            cand.setdefault(v, p)
    res, items = {}, sorted(cand)
    for i in range(0, len(items), 200):
        for line in run(["dpkg", "-S", *items[i:i + 200]]).splitlines():
            if ": " in line and not line.startswith("diversion"):
                pk, path = line.split(": ", 1)
                path = path.strip()
                if path in cand and cand[path] not in res:
                    res[cand[path]] = pk.split(",")[0].split(":")[0].strip()
    return res


def dpkg_all():
    out = {}
    txt = run(["dpkg-query", "-W", "-f=${Package}\t${Version}\t${Priority}\t${Essential}\t${Installed-Size}\n"])
    for line in txt.splitlines():
        p = line.split("\t")
        if len(p) == 5:
            out[p[0]] = {"version": p[1], "priority": p[2], "essential": p[3] == "yes",
                         "kb": int(p[4]) if p[4].isdigit() else None}
    return out


def ldconfig_sonames():
    """{soname: path} of every library the dynamic loader can find."""
    out = {}
    for line in run(["ldconfig", "-p"]).splitlines()[1:]:
        m = re.match(r"\s*(\S+)\s+\(([^)]*)\)\s+=>\s+(\S+)", line)
        if m and "64" in m.group(2):
            out.setdefault(m.group(1), m.group(3))
    return out


def os_release():
    try:
        with open("/etc/os-release") as fh:
            return dict(line.rstrip().split("=", 1) for line in fh if "=" in line).get("PRETTY_NAME", "").strip('"')
    except OSError:
        return None


def system_data():
    """OS data the fixtures need: timezone files, fonts, CA certificates."""
    fonts = []
    for d in ("/usr/share/fonts", "/usr/local/share/fonts"):
        for p in glob.glob(d + "/**/*", recursive=True):
            if re.search(r"\.(ttf|otf|ttc)$", p, re.I):
                fonts.append(os.path.basename(p))
    return {"zoneinfo": os.path.isdir("/usr/share/zoneinfo") and os.path.exists("/usr/share/zoneinfo/Asia/Kolkata"),
            "fonts": sorted(set(fonts))[:400], "ca_certificates": os.path.exists("/etc/ssl/certs/ca-certificates.crt")}


# ============================================================================ baseline (in python:3.x-slim)
def cmd_baseline(a):
    import importlib.metadata as md
    out = {"tool": "xray-pkg-baseline", "version": VERSION, "python": sys.version.split()[0], "os": os_release(),
           "image": a.image, "dpkg": dpkg_all(), "sonames": sorted(ldconfig_sonames()), "system_data": system_data(),
           "binaries": sorted({os.path.basename(p) for d in ("/usr/bin", "/bin", "/usr/local/bin", "/usr/sbin", "/sbin")
                               for p in glob.glob(d + "/*") if os.access(p, os.X_OK)}),
           "python_dists": sorted(canon(d.metadata["Name"]) for d in md.distributions() if d.metadata["Name"])}
    jdump(out, a.out)
    print(f"[xray-pkg] baseline of {out['os']} / Python {out['python']}: {len(out['dpkg'])} Debian packages, "
          f"{len(out['sonames'])} shared libraries -> {a.out}")
    return 0


# ============================================================================ scan (in the app image)
def requirement_files(root):
    """The requirements file(s) the Dockerfile installs (-r X), else requirements.txt, else all requirements*.txt."""
    used = []
    for df in glob.glob(os.path.join(root, "Dockerfile*")):
        try:
            with open(df, encoding="utf-8", errors="replace") as fh:
                used += re.findall(r"-r\s+(\S+\.txt)", fh.read())
        except OSError:
            pass
    used = [os.path.join(root, u) for u in dict.fromkeys(used) if os.path.exists(os.path.join(root, u))]
    if used:
        return used
    if os.path.exists(os.path.join(root, "requirements.txt")):
        return [os.path.join(root, "requirements.txt")]
    return sorted(glob.glob(os.path.join(root, "requirements*.txt")))


def parse_requirements(root):
    reqs = []
    for rf in requirement_files(root):
        with open(rf, encoding="utf-8", errors="replace") as fh:
            for n, line in enumerate(fh, 1):
                body = line.split("#")[0].strip()
                m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[[^\]]*\])?\s*(.*)$", body)
                if m and not body.startswith("-"):
                    reqs.append({"name": canon(m.group(1)), "raw_name": m.group(1), "spec": m.group(3).strip(),
                                 "file": os.path.relpath(rf, root), "line": n, "text": line.rstrip("\n")})
    return reqs


def dist_info(d, site_dirs):
    md = d.metadata
    name = md["Name"]
    files = list(d.files or [])
    size, sos = 0, []
    for f in files:
        try:
            p = str(d.locate_file(f))
        except Exception:
            continue
        if os.path.isfile(p):
            try:
                size += os.path.getsize(p)
            except OSError:
                pass
            if re.search(r"\.so(\.[\d.]+)?$", p):
                sos.append(os.path.realpath(p))
    wheel = d.read_text("WHEEL") or ""
    tags = re.findall(r"^Tag:\s*(\S+)", wheel, re.M)
    gen = (re.findall(r"^Generator:\s*(.+)$", wheel, re.M) or [None])[0]
    top = [x.strip() for x in (d.read_text("top_level.txt") or "").split() if x.strip()]
    if not top:
        tops = set()
        for f in files:
            parts = str(f).split("/")
            if len(parts) > 1 and not parts[0].endswith((".dist-info", ".data", ".libs")) and parts[0] != "..":
                tops.add(parts[0])
            elif len(parts) == 1 and str(f).endswith(".py"):
                tops.add(str(f)[:-3])
        top = sorted(tops)
    return {"name": canon(name), "raw_name": name, "version": md["Version"],
            "requires": md.get_all("Requires-Dist") or [], "summary": md["Summary"],
            "installer": (d.read_text("INSTALLER") or "").strip() or None, "wheel_tags": tags, "generator": gen,
            "direct_url": d.read_text("direct_url.json"), "top_level": top, "size_bytes": size,
            "n_files": len(files), "so_files": sos,
            "record_paths": [str(f) for f in files if not str(f).endswith((".pyc",)) and "dist-info" not in str(f)],
            "record_hash": {str(f): f.hash.value for f in files if f.hash and str(f).endswith(".so")}}


def elf_needed(path):
    """DT_NEEDED entries of an ELF64 little-endian file (what it links DIRECTLY), or None."""
    import struct
    try:
        with open(path, "rb") as fh:
            data = fh.read()
        if data[:4] != b"\x7fELF" or data[4] != 2 or data[5] != 1:
            return None
        shoff, = struct.unpack_from("<Q", data, 0x28)
        shentsize, shnum = struct.unpack_from("<HH", data, 0x3A)
        secs = [struct.unpack_from("<IIQQQQIIQQ", data, shoff + i * shentsize) for i in range(shnum)]
        dyn = next((s for s in secs if s[1] == 6), None)          # SHT_DYNAMIC
        if dyn is None:
            return []
        strtab = secs[dyn[6]]                                      # sh_link -> .dynstr
        out = []
        for off in range(dyn[4], dyn[4] + dyn[5], 16):
            tag, val = struct.unpack_from("<qQ", data, off)
            if tag == 0:
                break
            if tag == 1:                                           # DT_NEEDED
                start = strtab[4] + val
                out.append(data[start:data.index(b"\0", start)].decode("ascii", "replace"))
        return out
    except Exception:
        return None


def ldd(paths):
    """{so: {"needs": {soname: resolved path or None}}} via ldd (DT_NEEDED + loader search)."""
    out = {}
    for p in paths:
        needs = {}
        for line in run(["ldd", p], timeout=30).splitlines():
            m = re.match(r"\s*(\S+)\s+=>\s+(not found|\S+)", line)
            if m:
                needs[m.group(1)] = None if m.group(2) == "not found" else m.group(2)
        out[p] = needs
    return out


def cmd_scan(a):
    import importlib.metadata as md
    import site
    root = os.path.realpath(a.root)
    print(f"[xray-pkg] scanning installed packages and {root} ...", flush=True)
    site_dirs = [p for p in site.getsitepackages() + [site.getusersitepackages()] if os.path.isdir(p)]
    dists = {}
    for d in md.distributions():
        try:
            if not d.metadata["Name"]:
                continue
            info = dist_info(d, site_dirs)
            dists.setdefault(info["name"], info)
        except Exception as e:
            print(f"[xray-pkg] skipped a distribution: {e!r}", flush=True)
    # files written by two distributions (dual installs like opencv-python + opencv-python-headless)
    by_path = defaultdict(list)
    for n, info in dists.items():
        for p in info.pop("record_paths"):
            by_path[p].append(n)
    overlaps = defaultdict(int)
    winners = defaultdict(lambda: defaultdict(int))   # which distribution wrote the shared files LAST
    for p, ns in by_path.items():
        if len(ns) > 1:
            key = "|".join(sorted(ns))
            overlaps[key] += 1
            hashes = {n: dists[n]["record_hash"].get(p) for n in ns}
            if any(hashes.values()):
                try:
                    real = str(md.distribution(dists[ns[0]]["raw_name"]).locate_file(p))
                    import base64
                    with open(real, "rb") as fh:
                        h = base64.urlsafe_b64encode(hashlib.sha256(fh.read()).digest()).rstrip(b"=").decode()
                    for n, hv in hashes.items():
                        if hv == h:
                            winners[key][n] += 1
                except OSError:
                    pass
    for info in dists.values():
        info.pop("record_hash", None)
    print(f"[xray-pkg] {len(dists)} distributions; running ldd on their compiled files ...", flush=True)
    all_so = sorted({s for i in dists.values() for s in i["so_files"]})
    needs = ldd(all_so)
    direct = {p: elf_needed(p) for p in all_so}
    sysl = sorted({p for n in needs.values() for p in n.values() if p and "-packages/" not in p})
    sonames = ldconfig_sonames()
    bins = sorted({p for d in ("/usr/bin", "/bin", "/usr/local/bin", "/usr/sbin", "/sbin")
                   for p in glob.glob(d + "/*") if os.access(p, os.X_OK) and os.path.isfile(p)})
    fonts = [p for p in glob.glob("/usr/share/fonts/**/*", recursive=True) if os.path.isfile(p)]
    own = owners(sysl + list(sonames.values()) + bins + fonts[:500] + ["/usr/share/zoneinfo/Asia/Kolkata"])
    files = []
    for dp, dn, fn in os.walk(root):
        dn[:] = [x for x in dn if x not in SKIP_DIRS]
        for f in fn:
            p = os.path.join(dp, f)
            if os.path.islink(p) or not os.path.isfile(p):
                continue
            h = hashlib.sha256()
            try:
                with open(p, "rb") as fh:
                    for chunk in iter(lambda: fh.read(1 << 20), b""):
                        h.update(chunk)
                files.append({"path": os.path.relpath(p, root), "size": os.path.getsize(p), "sha256": h.hexdigest()})
            except OSError:
                pass
    pipcheck = run([sys.executable, "-m", "pip", "check"], timeout=120).strip()
    out = {"tool": "xray-pkg-scan", "version": VERSION, "python": sys.version.split()[0], "os": os_release(),
           "root": root, "site_dirs": site_dirs, "requirements": parse_requirements(root), "dists": dists,
           "overlaps": dict(overlaps), "overlap_winners": {k: dict(v) for k, v in winners.items()}, "ldd": {k: v for k, v in needs.items() if v},
           "needed_direct": {k: v for k, v in direct.items() if v}, "owners": own,
           "sonames": sonames, "dpkg": dpkg_all(), "binaries": [os.path.basename(b) for b in bins],
           "binary_paths": {os.path.basename(b): b for b in reversed(bins)}, "system_data": system_data(),
           "files": files, "pip_check": pipcheck}
    jdump(out, a.out)
    give_back(os.path.dirname(os.path.abspath(a.out)))
    tot = sum(i["size_bytes"] for i in dists.values())
    print(f"[xray-pkg] done: {len(dists)} Python distributions ({tot / 1e9:.2f} GB), {len(all_so)} compiled files, "
          f"{len(out['dpkg'])} Debian packages, {len(files)} project files -> {a.out}", flush=True)
    return 0


def give_back(d):
    try:
        st = os.stat(d)
        if os.getuid() == 0 and st.st_uid != 0:
            for p in glob.glob(os.path.join(d, "*")):
                os.chown(p, st.st_uid, st.st_gid)
    except OSError:
        pass


# ============================================================================ facts (host)
def req_names(spec):
    """'nvidia-cudnn-cu12 (==9.1.0.70) ; platform_system == "Linux"' -> ('nvidia-cudnn-cu12', marker text)"""
    m = re.match(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)", spec)
    return (canon(m.group(1)) if m else None), (spec.split(";", 1)[1] if ";" in spec else "")


def extra_only(marker):
    return "extra ==" in marker.replace(" ", " ") or "extra==" in marker.replace(" ", "")


class PkgFacts:
    def __init__(self, scan, base=None, static=None, runtime=None):
        self.s, self.b, self.st, self.rt = scan, base or {}, static or {}, runtime or {}
        self.d = scan["dists"]
        self.facts = {}
        self.notes = []
        self.top2dists = defaultdict(list)
        for n, i in self.d.items():
            for t in i["top_level"]:
                self.top2dists[t].append(n)
        self.reqs = {r["name"]: r for r in scan["requirements"]}
        self.used, self.used_by = self.code_imports()
        self.graph = {n: [x for x in (req_names(r)[0] for r in i["requires"] if not extra_only(req_names(r)[1]))
                          if x in self.d] for n, i in self.d.items()}

    # ---------------------------------------------------------------- inputs from the other layers
    def code_imports(self):
        """import names the code needs at runtime (static: required yes/conditional; runtime: loaded)."""
        used, by = {}, defaultdict(list)
        for src, facts in (("static", self.st.get("facts", [])), ("runtime", self.rt.get("facts", []))):
            for f in facts:
                if f.get("category") not in ("third_party_import", "dynamic_import"):
                    continue
                name = f.get("import_name") or f.get("subject")
                top = str(name).split(".")[0]
                req = f.get("required_at_runtime")
                if src == "runtime" and f.get("import_failed"):
                    continue
                if req in ("yes", "conditional") or src == "runtime":
                    rank = {"yes": 2, "conditional": 1}.get(req, 2)
                    used[top] = max(used.get(top, 0), rank)
                    by[top].append(src)
        if self.rt.get("observations"):     # everything any traced process really loaded
            for p in self.rt["observations"].get("packages_loaded", []):
                by[p["import_name"]].append("runtime:loaded")
        return used, by

    def dual_losers(self):
        out = {}
        for key, over in self.s.get("overlaps", {}).items():
            ns = sorted(key.split("|"))
            if over < 3 or any(n not in self.d for n in ns):
                continue
            tops = sorted(set.intersection(*(set(self.d[n]["top_level"]) for n in ns))) or ["?"]
            won = self.s.get("overlap_winners", {}).get(key) or {}
            loaded = max(won, key=won.get) if won else self.rt_dist_loaded(tops[0])
            # keep the headless build unless the code really opens windows at runtime; else keep what loads today
            headless = [n for n in ns if n.endswith("-headless")]
            gui = any(f.get("category") == "gui_usage" for f in self.rt.get("facts", []))
            keep = headless[0] if headless and not gui else (loaded if loaded in ns else ns[0])
            losers = [n for n in ns if n != keep]
            winner = keep
            for n in losers:
                out[n] = (tops[0], ns, over, loaded, winner)
        return out

    def needed_dists(self):
        """distributions the code needs: those providing used import names + their dependency closure."""
        roots = set()
        losers = self.dual_losers()
        for top in self.used:
            for n in self.top2dists.get(top, []):
                if n not in losers:
                    roots.add(n)
        seen, stack = set(), list(roots)
        while stack:
            n = stack.pop()
            if n in seen:
                continue
            seen.add(n)
            stack.extend(self.graph.get(n, []))
        return roots, seen

    def req_site(self, name):
        r = self.reqs.get(name)
        return [{"file": r["file"], "line": r["line"]}] if r else []

    def add(self, cat, subject, mech, required, detail, sites=None, **extra):
        k = (cat, subject, mech) if cat == "packaging" else (cat, subject)
        f = self.facts.get(k)
        if f is None:
            f = self.facts[k] = {"category": cat, "subject": subject, "mechanism": mech, "sites": sites or [],
                                 "evidence": "derived", "confidence": "high", "layers": ["pkg_analyzer"],
                                 "required_at_runtime": required, "detail": detail}
            f.update({k2: v for k2, v in extra.items() if v is not None})
        return f

    # ---------------------------------------------------------------- facts
    def build(self):
        roots, needed = self.needed_dists()
        mb = lambda b: f"{b / 1e6:.0f} MB" if b < 1e9 else f"{b / 1e9:.2f} GB"
        # 1. requirements: kept / orphan / missing / undeclared
        for name, r in self.reqs.items():
            i = self.d.get(name)
            if i is None:
                self.add("packaging", name, "not_installed", "no", f"{r['text'].strip()} is in {r['file']} but is not "
                         "installed in the image", self.req_site(name))
                continue
            tops = i["top_level"]
            if name in self.dual_losers():
                continue            # reported once, as dual_distribution
            if name in roots:
                self.add("third_party_import", name, "installed_dist", "yes",
                         f"{i['raw_name']}=={i['version']} installed ({mb(i['size_bytes'])}); import name(s) "
                         f"{', '.join(tops) or '?'} used by the code ({', '.join(sorted(set(sum((self.used_by[t] for t in tops), []))))})",
                         self.req_site(name), import_name=(tops or [None])[0], version=i["version"],
                         size_bytes=i["size_bytes"])
            elif name in needed:
                parents = sorted(n for n in needed if name in self.graph.get(n, []))
                self.add("packaging", name, "transitive_declared", "yes",
                         f"not imported by the code, but {', '.join(parents)} depend(s) on it: keep (or let pip pull "
                         f"it in)", self.req_site(name), version=i["version"])
            else:
                self.add("packaging", name, "orphan_requirement", "no",
                         f"{i['raw_name']}=={i['version']} ({mb(i['size_bytes'])}) is in {r['file']} but no code "
                         f"that runs imports {', '.join(tops) or name} and no needed package depends on it: remove it",
                         self.req_site(name), version=i["version"], size_bytes=i["size_bytes"])
        for n in sorted(roots):
            if n not in self.reqs:
                parents = sorted(p for p in self.d if n in self.graph.get(p, []))
                i = self.d[n]
                self.add("packaging", n, "undeclared_dependency", "yes",
                         f"imported by the code ({', '.join(i['top_level'])}) but not in requirements; installed only "
                         f"because {', '.join(parents) or 'something'} depends on it: add {i['raw_name']}=={i['version']}",
                         [{"file": (self.s["requirements"] or [{"file": "requirements.txt"}])[0]["file"], "line": None}],
                         version=i["version"])
        # 2. two distributions writing the same files (opencv-python + opencv-python-headless both own cv2/*)
        #    (shared namespace folders like nvidia/ are fine: different files)
        for n, (top, ns, over, loaded, winner) in self.dual_losers().items():
            self.add("packaging", n, "dual_distribution", "no",
                     f"'{top}' is installed by {len(ns)} distributions ({', '.join(ns)}): {over} files overwrite "
                     f"each other, so whichever pip installed last wins" +
                     (f"; the files on disk are {loaded}'s (installed last), so that build is what loads today"
                      if loaded else "") + f": keep only {winner}",
                     self.req_site(n))
        # 3. built from source in this image: a platform wheel without a manylinux tag whose WHEEL "Generator" is the
        #    exact setuptools / wheel version installed here (upstream wheels, e.g. torch's, name other versions)
        local_tools = {f"{t} ({self.d[t]['version']})" for t in ("setuptools", "wheel") if t in self.d}
        local_tools |= {f"bdist_wheel ({self.d['wheel']['version']})"} if "wheel" in self.d else set()
        for n, i in self.d.items():
            plat = [t for t in i["wheel_tags"] if not t.endswith("-any")]
            if plat and all("manylinux" not in t and "musllinux" not in t for t in plat) and i["so_files"] and \
                    (i.get("generator") or "").strip() in local_tools:
                binary = f"{n}-binary" if n == "psycopg2" else None
                self.add("packaging", n, "source_build", "yes" if n in needed else "no",
                         f"built from source inside the image (wheel tag {plat[0]}, built by the {i['generator']} "
                         f"installed here): needs a "
                         f"compiler and -dev headers at build time" + (f"; swap to {binary}" if binary else
                                                                       "; use a prebuilt wheel or a build stage"),
                         self.req_site(n), version=i["version"])
        # 4. torch with CUDA libraries on a CPU run
        for n in ("torch", "tensorflow", "jax", "onnxruntime-gpu"):
            i = self.d.get(n)
            if not i:
                continue
            cuda = sorted(x for x in self.graph.get(n, []) if x.startswith(("nvidia-", "triton")))
            if cuda or "+cu" in i["version"]:
                size = i["size_bytes"] + sum(self.d[x]["size_bytes"] for x in cuda)
                gpu_used = self.gpu_used()
                self.add("packaging", n, "cuda_build", "yes",
                         f"{n}=={i['version']} is the CUDA build: it pulls {len(cuda)} NVIDIA/triton packages; together "
                         f"{mb(size)} installed" + ("" if gpu_used else "; this run never used a GPU (CPU fallback) -> "
                                                    "install the CPU build: --index-url https://download.pytorch.org/whl/cpu"),
                         self.req_site(n), cuda_packages=cuda, size_bytes=size, version=i["version"])
        # 5. numpy ABI pin
        np_ = self.d.get("numpy")
        if np_ and ("numpy" in needed or "numpy" in self.reqs):
            dep = sorted(n for n in needed if "numpy" in self.graph.get(n, []))
            self.add("packaging", f"numpy=={np_['version']}", "abi_pin", "yes",
                     f"numpy {np_['version'].split('.')[0]}.x - compiled packages must be built for the same numpy major "
                     f"({', '.join(dep) or 'none'} depend on it): images sharing a base must agree on it",
                     self.req_site("numpy"), version=np_["version"], dependents=dep)
        # 6. pip check
        for line in (self.s.get("pip_check") or "").splitlines():
            if line.strip() and "No broken requirements" not in line:
                self.add("packaging", line.split()[0].lower(), "pip_check", "yes", line.strip())
        self.system_packages(needed)
        self.image_files()
        return self

    def rt_dist_loaded(self, top):
        for f in self.rt.get("facts", []):
            if f.get("category") == "third_party_import" and f.get("import_name") == top and "loaded files belong" in \
                    str(f.get("detail")):
                return canon(f["subject"])
        return None

    def gpu_used(self):
        for f in self.rt.get("facts", []):
            if f.get("category") == "gpu_usage" and (f.get("result") is True or "active ['CUDA" in str(f.get("detail"))):
                return True
        return False

    # ---------------------------------------------------------------- system packages
    def system_packages(self, needed):
        own, dp = self.s.get("owners", {}), self.s.get("dpkg", {})
        base_pk = set((self.b.get("dpkg") or {}).keys())
        base_so = set(self.b.get("sonames") or [])
        base_bin = set(self.b.get("binaries") or [])
        have_base = bool(self.b)

        def in_base(pkg, soname=None):
            return (pkg in base_pk) or (soname in base_so if soname else False)

        # (a0) system libraries that really loaded at runtime (the definitive list when a trace exists)
        rt_libs = (self.rt.get("observations") or {}).get("system_libraries") or []
        if rt_libs:
            losers = set(self.dual_losers())
            so2dist = {so: n for n, i in self.d.items() for so in i["so_files"]}
            linkers = defaultdict(set)      # soname -> distributions whose compiled files link it directly
            for so, need in self.s.get("needed_direct", {}).items():
                n = so2dist.get(so)
                if n in needed or n in losers:
                    for x in need:
                        linkers[x].add(n)
            for f in self.rt.get("facts", []):
                if f.get("category") == "native_library":
                    linkers[os.path.basename(str(f.get("subject")))].add("the project (ctypes)")
            pk = defaultdict(lambda: {"libs": set(), "by": set()})
            for lib in rt_libs:
                pkg = lib.get("package")
                if not pkg or (have_base and in_base(pkg)) or lib.get("in_python_slim"):
                    continue
                base = os.path.basename(lib["path"])
                pk[pkg]["libs"].add(base)
                for x, ns in linkers.items():
                    if base == x or base.startswith(x + "."):
                        pk[pkg]["by"] |= ns
            for pkg, x in sorted(pk.items()):
                by = sorted(x["by"])
                if not by:
                    self.notes.append(f"{pkg} ({', '.join(sorted(x['libs']))}) loaded as a dependency of another "
                                      f"library: apt installs it automatically")
                    continue
                swapped = {n for n in by if n == "psycopg2" and self.d.get(n, {}).get("so_files") and
                           ("psycopg2", "packaging") in {(k[1], k[0]) for k in self.facts if k[1] == "psycopg2"}}
                if swapped and all(n in swapped for n in by):
                    self.add("system_package_candidate", pkg, "loaded_at_runtime", "conditional",
                             f"{', '.join(sorted(x['libs']))} loaded, linked by psycopg2 built from source - not needed "
                             f"after the swap to psycopg2-binary (it bundles libpq)", kb=dp.get(pkg, {}).get("kb"),
                             linked_by=by)
                    continue
                only_losers = all(n in losers for n in by)
                self.add("system_package_candidate", pkg, "loaded_at_runtime", "conditional" if only_losers else "yes",
                         f"{', '.join(sorted(x['libs']))} loaded during the scenario, linked by {', '.join(by)}"
                         + (" - not in python:3.x-slim" if have_base else "")
                         + (f"; needed ONLY because {', '.join(by)} is installed: not needed once it is removed"
                            if only_losers else ""), kb=dp.get(pkg, {}).get("kb"), linked_by=by)
            libs_from_ldd = False
        else:
            libs_from_ldd = True
        # (a) libraries compiled Python packages link against (no runtime trace)
        libs = defaultdict(lambda: {"dists": set(), "path": None})
        so2dist = {s: n for n, i in self.d.items() for s in i["so_files"]}
        bundled = {os.path.basename(s) for s in so2dist}
        for so, needs in (self.s.get("ldd", {}) if libs_from_ldd else {}).items():
            n = so2dist.get(so)
            if n not in needed:
                continue
            direct = self.s.get("needed_direct", {}).get(so)
            for soname, path in needs.items():
                if direct is not None and soname not in direct:
                    continue            # pulled in by another library: apt installs it as a dependency
                if path is None or "-packages/" in path:
                    if path is None and soname not in bundled:
                        self.notes.append(f"{os.path.basename(so)} ({n}) needs {soname}: NOT FOUND in this image")
                    continue
                libs[soname]["dists"].add(n)
                libs[soname]["path"] = path
        by_pkg = defaultdict(lambda: {"sonames": set(), "dists": set()})
        for soname, x in libs.items():
            pkg = own.get(x["path"])
            if not pkg:
                continue
            by_pkg[pkg]["sonames"].add(soname)
            by_pkg[pkg]["dists"] |= x["dists"]
        for pkg, x in sorted(by_pkg.items()):
            if have_base and in_base(pkg):
                continue
            if not have_base and (dp.get(pkg, {}).get("essential") or dp.get(pkg, {}).get("priority") == "required"):
                continue
            self.add("system_package_candidate", pkg, "ldd", "yes",
                     f"{', '.join(sorted(x['dists']))} link(s) {', '.join(sorted(x['sonames']))}"
                     + (" - not in python:3.x-slim" if have_base else ""), sonames=sorted(x["sonames"]),
                     dists=sorted(x["dists"]), kb=dp.get(pkg, {}).get("kb"))
        # (b) native libraries / binaries / OS data the code uses (from the static map and the runtime trace)
        sonames = self.s.get("sonames", {})
        for f in self.st.get("facts", []) + self.rt.get("facts", []):
            c = f.get("category")
            if c == "native_library":
                so = os.path.basename(str(f.get("subject")))
                path = sonames.get(so)
                pkg = own.get(path) if path else None
                if not pkg:
                    continue
                if have_base and in_base(pkg, so):
                    continue
                req = f.get("required_at_runtime") if f.get("required_at_runtime") != "no" else "conditional"
                bundled = " (the runtime saw an already-loaded copy from a pip wheel answer the call)" if \
                    "bundled" in str(f.get("detail")) or "already-loaded" in str(f.get("detail")) else ""
                self.add("system_package_candidate", pkg, "native_library", req,
                         f"the code loads {so}; in this image it comes from Debian package {pkg}"
                         + (" - not in python:3.x-slim" if have_base else "") + bundled, f.get("sites"),
                         sonames=[so])
            elif c == "subprocess_binary":
                b = str(f.get("subject"))
                path = self.s.get("binary_paths", {}).get(b)
                pkg = own.get(path) if path else None
                if not pkg or (have_base and (in_base(pkg) or b in base_bin)):
                    continue
                self.add("system_package_candidate", pkg, "binary", f.get("required_at_runtime") or "yes",
                         f"the code runs {b}; in this image it is {path} from Debian package {pkg}"
                         + (" - not in python:3.x-slim" if have_base else ""), f.get("sites"))
            elif c == "os_data":
                subj = str(f.get("subject"))
                sd = self.b.get("system_data") or {}
                if "tzdata" in subj or "zoneinfo" in subj:
                    if have_base and not sd.get("zoneinfo"):
                        self.add("system_package_candidate", "tzdata", "os_data", f.get("required_at_runtime") or "yes",
                                 f"the code needs timezone data ({subj}); python:3.x-slim has no "
                                 f"/usr/share/zoneinfo/Asia/Kolkata (or pip install tzdata)", f.get("sites"))
                elif subj.startswith("font:"):
                    font = subj.split(":", 1)[1]
                    pkg = f.get("package") or next((own[p] for p in own if os.path.basename(p) == font), None)
                    if have_base and font in (sd.get("fonts") or []):
                        continue
                    if pkg:
                        self.add("system_package_candidate", pkg, "os_data", f.get("required_at_runtime") or "conditional",
                                 f"the code loads font {font}; provided by {pkg}" +
                                 (" - python:3.x-slim has no such font" if have_base else ""), f.get("sites"))
        # (c) big build-only packages in the image
        for pkg in ("build-essential", "gcc", "g++", "libpq-dev", "python3-dev", "cmake"):
            if pkg in dp and (not have_base or pkg not in base_pk):
                self.notes.append(f"Debian package {pkg} is installed ({dp[pkg].get('kb') or '?'} KB): build-time only")

    # ---------------------------------------------------------------- files in the image
    def image_files(self):
        files = self.s.get("files", [])
        self.unused_folders = set()
        # files baked into the image that nothing uses (needs the static map and/or the runtime trace)
        if self.st or self.rt:
            self.unused_files(files)
        self.duplicates(files)
        self.shadowing(files)

    def shadowing(self, files):
        """Module names in the image that collide: a local file named like an installed package (skimage.py vs
        scikit-image), or two local packages/modules with the same name in different folders."""
        installed = {t: ns for t, ns in self.top2dists.items() if not t.startswith("_")}
        local = defaultdict(set)
        for f in files:
            p = f["path"]
            if not p.endswith(".py") or p.startswith(("tools/", "docker/")):
                continue
            base = os.path.basename(p)
            if base == "__init__.py":
                name, where = os.path.basename(os.path.dirname(p)), os.path.dirname(p)
            else:
                name, where = base[:-3], p
            if name and name not in ("__main__", "setup", "conftest", "src"):
                local[name].add(where)
        for name, where in sorted(local.items()):
            if name in installed:
                ds = sorted(set(installed[name]))
                self.add("shadowing", name, "name_collision", "no",
                         f"local {', '.join(sorted(where))} has the same import name as the installed "
                         f"{', '.join(ds)}: which one 'import {name}' gets depends on sys.path (tools like pipreqs "
                         f"drop {ds[0]} because of it)", [{"file": sorted(where)[0], "line": None}], paths=sorted(where))
            elif len(where) > 1:
                self.add("shadowing", name, "name_collision", "no",
                         f"{len(where)} local modules/packages named '{name}': {', '.join(sorted(where))}; only the first "
                         f"on sys.path is ever imported", [{"file": sorted(where)[0], "line": None}], paths=sorted(where))

    def duplicates(self, files):
        by = defaultdict(list)
        for f in files:
            if f["size"] > 0 and not f["path"].endswith((".py", ".pyc")) and "/__pycache__/" not in f["path"]:
                by[f["sha256"]].append(f)
        for h, fs in by.items():
            if len(fs) < 2 or all(any(x["path"].startswith(d + "/") for d in self.unused_folders) for x in fs):
                continue            # single file / junk inside a folder that is unused as a whole
            fs = sorted(fs, key=lambda x: (x["path"].count("/"), x["path"]))
            dirs = {os.path.dirname(x["path"]) for x in fs}
            if len(dirs) < 2 and all(x["size"] < 1024 for x in fs):
                continue
            keep, rest = fs[0], fs[1:]
            for x in rest:
                self.add("duplicate_asset", x["path"], "sha256", "no",
                         f"byte-identical to {keep['path']} ({x['size']} bytes, sha256 {h[:12]})",
                         [{"file": x["path"], "line": None}], same_as=keep["path"], size_bytes=x["size"])
            self.add("duplicate_asset", keep["path"], "sha256", "no",
                     f"{len(rest)} byte-identical cop{'y' if len(rest) == 1 else 'ies'}: "
                     + ", ".join(x["path"] for x in rest), [{"file": keep["path"], "line": None}],
                     copies=[x["path"] for x in rest])

    def unused_files(self, files):
        used = set()
        for src in (self.st, self.rt):
            for f in src.get("facts", []):
                if f.get("required_at_runtime") == "no" and src is self.st:
                    continue
                if f.get("category") in ("unused_asset", "duplicate_asset", "packaging"):
                    continue
                for k in ("resolved_path", "subject"):
                    v = f.get(k)
                    if isinstance(v, str) and not v.startswith(("/", "<")):
                        used.add(v.rstrip("/"))
                for p in f.get("paths") or []:
                    if isinstance(p, str):
                        used.add(p)
                for s in f.get("sites") or []:
                    if s.get("file"):
                        used.add(s["file"])
        for m in self.st.get("modules", []):
            if m.get("required") in ("yes", "conditional"):
                used.add(m["path"])
        for fn in (self.rt.get("coverage") or {}):
            used.add(fn)
        unused = []
        for f in files:
            p = f["path"]
            if p in used or DEPLOY_FILES.search(p) or p.startswith(("docker/", "tools/")) or p.endswith("__init__.py"):
                continue
            unused.append(f)
        # collapse whole folders
        ufiles = {f["path"] for f in unused}
        allfiles = defaultdict(set)
        for f in files:
            parts = f["path"].split("/")
            for i in range(1, len(parts)):
                allfiles["/".join(parts[:i])].add(f["path"])
        done = set()
        for d in sorted(allfiles, key=lambda x: x.count("/")):
            members = allfiles[d]
            if any(d.startswith(x + "/") or d == x for x in done):
                continue
            real = {m for m in members if not m.endswith("__init__.py")}
            if real and real <= ufiles and len(members) > 1:
                size = sum(f["size"] for f in files if f["path"] in members)
                self.unused_folders.add(d)
                self.add("unused_asset", d + "/", "image_folder", "no",
                         f"whole folder unused ({len(members)} files, {size / 1e6:.1f} MB) - .dockerignore it",
                         [{"file": d + "/", "line": None}], paths=sorted(members)[:50])
                done.add(d)
        for f in unused:
            if any(f["path"].startswith(x + "/") for x in done):
                continue
            self.add("unused_asset", f["path"], "image_file", "no",
                     f"in the image ({f['size'] / 1e6:.2f} MB) but never imported, opened or referenced by code that "
                     f"runs", [{"file": f["path"], "line": None}], size_bytes=f["size"])

    # ---------------------------------------------------------------- output
    def output(self):
        facts = sorted(self.facts.values(), key=lambda f: (f["category"], str(f["subject"])))
        for n, f in enumerate(facts, 1):
            f["id"] = f"P-{n:04d}"
            f["docker_implication"] = implication(f)
        sizes = sorted(((n, i["size_bytes"]) for n, i in self.d.items()), key=lambda x: -x[1])
        dp = self.s.get("dpkg", {})
        base = set((self.b.get("dpkg") or {}).keys())
        extra_apt = sorted(((p, v.get("kb") or 0) for p, v in dp.items() if base and p not in base), key=lambda x: -x[1])
        by = defaultdict(int)
        for f in facts:
            by[f["category"]] += 1
        return {"tool": "xray-pkg", "version": VERSION, "python": self.s.get("python"), "os": self.s.get("os"),
                "root": self.s.get("root"), "entrypoints": self.st.get("entrypoints") or self.rt.get("entrypoints") or [],
                "baseline": {"os": self.b.get("os"), "python": self.b.get("python")} if self.b else None,
                "summary": {"facts": len(facts), "by_category": dict(by),
                            "python_packages_bytes": sum(s for _, s in sizes),
                            "apt_packages_not_in_slim_kb": sum(k for _, k in extra_apt) if base else None},
                "facts": facts, "requirements_plan": self.req_plan(),
                "observations": {"largest_python_packages": [{"name": n, "mb": round(s / 1e6, 1)} for n, s in sizes[:15]],
                                 "apt_packages_not_in_slim": [{"name": p, "mb": round(k / 1e3, 1)} for p, k in extra_apt[:40]],
                                 "notes": list(dict.fromkeys(self.notes))[:60]}}

    def req_plan(self):
        plan = {"keep": [], "remove": [], "swap": [], "add": []}
        for f in self.facts.values():
            m, s = f["mechanism"], f["subject"]
            v = f.get("version")
            if m == "installed_dist":
                plan["keep"].append(f"{s}=={v}" if v else s)
            elif m == "transitive_declared":
                plan["keep"].append(f"{s}=={v}  (dependency)" if v else s)
            elif m in ("orphan_requirement", "dual_distribution"):
                plan["remove"].append({"name": s, "why": f["detail"]})
            elif m == "undeclared_dependency":
                plan["add"].append({"name": f"{s}=={v}" if v else s, "why": f["detail"]})
            elif m == "source_build" and s == "psycopg2":
                plan["swap"].append({"from": f"psycopg2=={v}", "to": f"psycopg2-binary=={v}", "why": f["detail"]})
            elif m == "cuda_build":
                plan["swap"].append({"from": f"{s}=={v}", "to": f"{s}=={str(v).split('+')[0]}+cpu "
                                     "--index-url https://download.pytorch.org/whl/cpu", "why": f["detail"]})
        return plan


def implication(f):
    c, m, s = f["category"], f["mechanism"], f["subject"]
    if c == "third_party_import":
        return f"requirements: keep {s}"
    if c == "system_package_candidate":
        return f"apt: install {s}"
    if c == "duplicate_asset":
        return "keep one copy; .dockerignore the rest"
    if c == "unused_asset":
        return f".dockerignore {s}"
    return {"orphan_requirement": f"requirements: remove {s}", "dual_distribution": f"requirements: remove {s}",
            "undeclared_dependency": f"requirements: add {s}", "not_installed": f"requirements: {s} is not installed",
            "transitive_declared": f"requirements: keep {s} (a needed package depends on it)",
            "cuda_build": f"requirements: {s} CPU build (--index-url https://download.pytorch.org/whl/cpu)",
            "source_build": f"requirements: prebuilt wheel for {s}" if s != "psycopg2" else "requirements: swap psycopg2 -> psycopg2-binary",
            "abi_pin": f"keep {s} consistent across images that share a base", "pip_check": "fix the version conflict"}.get(m, "")


def cmd_facts(a):
    scan = jload(a.scan)
    pf = PkgFacts(scan, jload(a.baseline) if a.baseline else None, jload(a.static) if a.static else None,
                  jload(a.runtime) if a.runtime else None).build()
    out = pf.output()
    jdump(out, a.out)
    s = out["summary"]
    print(f"xray-pkg {VERSION}: {s['facts']} package facts -> {a.out}")
    print("  " + ", ".join(f"{k} {v}" for k, v in sorted(s["by_category"].items())))
    rp = out["requirements_plan"]
    print(f"  requirements: keep {len(rp['keep'])}, remove {[x['name'] for x in rp['remove']]}, "
          f"swap {[x['from'] for x in rp['swap']]}, add {[x['name'] for x in rp['add']]}")
    print(f"  Python packages installed: {s['python_packages_bytes'] / 1e9:.2f} GB; largest: " +
          ", ".join(f"{x['name']} {x['mb']:.0f} MB" for x in out["observations"]["largest_python_packages"][:5]))
    if s.get("apt_packages_not_in_slim_kb") is not None:
        print(f"  Debian packages not in python:3.x-slim: {s['apt_packages_not_in_slim_kb'] / 1e3:.0f} MB")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="X-Ray Phase 3 package analyzer")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan", help="(inside the app image) inventory of installed packages and project files")
    s.add_argument("--root", default=".", help="project folder inside the image (e.g. /app)")
    s.add_argument("--out", required=True)
    b = sub.add_parser("baseline", help="(inside python:3.x-slim) what the slim image already has")
    b.add_argument("--out", required=True)
    b.add_argument("--image", default="python:3.11-slim", help="label only")
    f = sub.add_parser("facts", help="(host) scan (+ baseline + static map + runtime facts) -> package facts")
    f.add_argument("--scan", required=True)
    f.add_argument("--baseline", default=None)
    f.add_argument("--static", default=None, help="xray_static.py output (what the code imports)")
    f.add_argument("--runtime", default=None, help="xray_trace.py facts output (what really loaded)")
    f.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    return {"scan": cmd_scan, "baseline": cmd_baseline, "facts": cmd_facts}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
