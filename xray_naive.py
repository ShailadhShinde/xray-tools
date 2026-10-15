#!/usr/bin/env python3
"""
xray_naive.py - check a requirements file for Linux BEFORE building, and write a naive Dockerfile that builds.

For every line of the requirements file it asks pip (dry run: nothing is installed, only the package index is read)
whether a ready-made Linux package (a "wheel") exists for the Python version of the image:
  OK          a prebuilt Linux wheel exists                       -> nothing to do
  COMPILED    only the source exists: pip compiles it in the build -> gcc + Python headers (+ a -dev library)
  MISSING     no file at all for Linux / this Python               -> change the pin (versions that do exist are listed)
  WINDOWS     a Windows-only package (pyreadline3, pywin32 ...)    -> left out of the Linux requirements
  INDEX       torch==2.8.0+cu126 style pins                        -> PyTorch's index is added
  SOURCE      only the source exists, but it is pure Python        -> builds without a compiler

and WHAT KIND each package is (from the file pip picks): pure wheel / binary wheel / HEAVY binary wheel (>100 MB) /
sdist (source: it downloads the source and looks for C / C++ / Cython / Rust files = compiler needed), the download
size, and for GPU packages where their CUDA libraries come from (own nvidia-* pip packages, or the system / base
image) - which decides python-slim vs an nvidia/cuda base.

Then it writes into runs/NAME/naive/:
  naive-check.txt              the table above, with the reason for every line, + the KIND and GPU tables
  naive-check.json             the same as data
  requirements.docker.txt      the requirements as the build installs them (Windows-only out, PyTorch index in,
                               each handled line with a comment)
  Dockerfile.naive             sections (base, apt libraries, frameworks, other packages + a compiler only where
                               needed, LD_LIBRARY_PATH, code), each line with its reason - decided from the tables
  Dockerfile.naive.dockerignore

The whole file is also checked TOGETHER, the way the build installs it (one pip install). A pip freeze comes from a PC
where packages were installed in several steps, so it can hold lines pip refuses to install together (torch +cu126 and
newer nvidia-* pins, a torchaudio of another torch). Those lines are installed in section 3c, AFTER the others, exactly
as pinned, with --no-deps - the way that PC got them; nothing is left out, no pin is changed. What they need that is not
in the file is added right after, without changing any version installed before (-c).

With --static (the Step 1 map), section 6 handles the files the code reads from your PC (a model on the Desktop):
copied into the image (up to 500 MB, or --copy ENV_NAME) or mounted at run time (--mount ENV_NAME), with the env var
set to the path inside the container. naive-check.txt then lists every env var (value in your code -> value in the
container), the code changes needed (a Windows path written without an env var), and the build and run commands.
Copy the three Docker files into the project folder (the tool never overwrites the project's own files).

  python xray_naive.py --project "C:\\...\\Solution" --requirements requirement.txt --entry main.py --name worker
       [--python 3.12] [--gpu | --cpu] [--static runs\\worker\\worker.json] [--copy ENV_NAME | --mount ENV_NAME]

Needs internet access to the package index (pip reads it). Standard library only.
"""
import argparse
import concurrent.futures
import json
import os
import re
import subprocess
import sys
import tempfile

VERSION = "0.4.1"
HERE = os.path.dirname(os.path.abspath(__file__))

# Linux x86_64 platform tags (pip matches the listed tags exactly; pure-Python "any" wheels always match)
PLATFORMS = ["linux_x86_64", "manylinux1_x86_64", "manylinux2010_x86_64", "manylinux2014_x86_64"] + \
    [f"manylinux_2_{n}_x86_64" for n in range(5, 40)]
WINDOWS_ONLY = {"pywin32", "pypiwin32", "pywinpty", "pyreadline3", "pyreadline", "wmi", "comtypes",
                "windows-curses", "pywinauto", "winshell", "pywin32-ctypes", "win10toast", "pythonnet-win"}
# big C/Fortran packages: if only their source exists for this Python, compiling usually fails (or takes an hour)
HEAVY = {"numpy", "scipy", "pandas", "scikit-learn", "scikit-image", "matplotlib", "torch", "torchvision",
         "torchaudio", "tensorflow", "opencv-python", "opencv-contrib-python", "opencv-python-headless",
         "onnxruntime", "onnxruntime-gpu", "numba", "llvmlite", "pyarrow", "h5py", "shapely", "grpcio", "tokenizers"}
NET_ERR = r"ProxyError|NewConnectionError|Max retries|403 Forbidden|SSLError|Read timed out|ConnectionError|" \
          r"Temporary failure in name resolution|Could not fetch URL"
# compiled packages that also need a system library (Debian / Ubuntu package names)
DEV_LIBS = {
    "psycopg2": ("libpq-dev", "or use psycopg2-binary (prebuilt, no compiler)"),
    "mysqlclient": ("default-libmysqlclient-dev pkg-config", ""),
    "pycairo": ("libcairo2-dev pkg-config", ""),
    "pygobject": ("libgirepository1.0-dev libcairo2-dev pkg-config", ""),
    "python-ldap": ("libldap2-dev libsasl2-dev", ""),
    "pyaudio": ("portaudio19-dev", ""),
    "dlib": ("cmake", "takes a long time to compile"),
    "h5py": ("libhdf5-dev pkg-config", ""),
    "lxml": ("libxml2-dev libxslt1-dev", ""),
    "pillow": ("libjpeg-dev zlib1g-dev", ""),
    "gssapi": ("libkrb5-dev", ""),
    "pykerberos": ("libkrb5-dev", ""),
    "pyodbc": ("unixodbc-dev", ""),
    "pycurl": ("libcurl4-openssl-dev libssl-dev", ""),
    "python-snappy": ("libsnappy-dev", ""),
    "cryptography": ("libssl-dev libffi-dev cargo", "an old version: newer ones have wheels"),
    "cffi": ("libffi-dev", ""),
}


def canon(n):
    return re.sub(r"[-_.]+", "-", str(n)).lower()


def parse_requirements(path):
    """[(line_no, raw, name, spec)] + index options (-i / --extra-index-url / -f) + notes"""
    reqs, opts, notes = [], [], []
    with open(path, encoding="utf-8-sig", errors="replace") as fh:
        lines = fh.read().splitlines()
    for no, raw in enumerate(lines, 1):
        line = raw.split(" #", 1)[0].strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("-"):
            m = re.match(r"^(--extra-index-url|--index-url|-i|--find-links|-f)[ =](\S+)", line)
            if m:
                opts.append(("--extra-index-url" if m.group(1) in ("--extra-index-url",) else
                             "--index-url" if m.group(1) in ("--index-url", "-i") else "--find-links", m.group(2)))
            else:
                notes.append(f"line {no}: '{line}' - not checked (pip option / other file)")
            continue
        if "@" in line and ("file:" in line or "://" in line):
            reqs.append((no, line, canon(line.split("@")[0].strip()), "LOCAL"))
            continue
        m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(\[[^\]]*\])?\s*(.*)$", line)
        if not m:
            notes.append(f"line {no}: '{line}' - not understood, not checked")
            continue
        reqs.append((no, line, canon(m.group(1)), m.group(3).strip()))
    return reqs, opts, notes


REPORT = {"ok": True}       # pip older than 22.2 has no --report: then the KIND table is left out


def pip_version():
    """(major, minor) of the pip that does the checks, or None"""
    try:
        out = subprocess.run([sys.executable, "-m", "pip", "--version"], capture_output=True, text=True,
                             timeout=60).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.match(r"pip (\d+)\.(\d+)", out or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


def pip_dry(req, py, only_binary, opts, cu_index, want=None):
    """run pip install --dry-run for one requirement on Linux / py: (ok, text); the file pip picked + its metadata
    go into want["pick"] when a dict is given"""
    with tempfile.TemporaryDirectory() as tmp:
        cmd = [sys.executable, "-m", "pip", "install", "--dry-run", "--quiet", "--no-deps", "--ignore-installed",
               "--disable-pip-version-check", "--no-input", "--target", os.path.join(tmp, "t"),
               "--python-version", py, "--implementation", "cp", "--abi", "cp" + py.replace(".", ""),
               "--abi", "abi3", "--abi", "none"]
        for p in PLATFORMS:
            cmd += ["--platform", p]
        if only_binary:
            cmd += ["--only-binary=:all:"]
        for k, v in opts:
            cmd += [k, v]
        if cu_index:
            cmd += ["--extra-index-url", cu_index]
        rep = os.path.join(tmp, "report.json")
        if want is not None and REPORT["ok"]:
            cmd += ["--report", rep]
        cmd += req if isinstance(req, list) else [req]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
        except subprocess.TimeoutExpired:
            return None, "pip timed out"
        out = (r.stdout + r.stderr).strip()
        if r.returncode != 0 and "no such option: --report" in out:
            REPORT["ok"] = False
            return pip_dry(req, py, only_binary, opts, cu_index, want)
        if r.returncode == 0:
            try:
                with open(rep, encoding="utf-8") as fh:
                    want["all"] = json.load(fh)["install"]
                    want["pick"] = want["all"][0]
            except (OSError, ValueError, KeyError, IndexError, TypeError):
                pass
            return True, out
        if re.search(NET_ERR, out):     # an index could not be read: "not found" would be a guess
            bad = re.findall(r"(https?://[^\s'\"]+|/whl/\S+)", out)
            return None, "could not read the package index" + (f" ({bad[0]})" if bad else "")
        return False, out


def resolve_together(results, py, opts, got=None):
    """install the whole file at once (dry run, WITH dependencies), like the build does: two lines that cannot be
    installed together (torch pins its own nvidia-* versions, torchaudio must match torch ...) only show up here.
    -> (ok True / False / None = not checked, pip's explanation lines); got["names"] = every package it installs"""
    reqs = [r["raw"].split(" #")[0].strip() for r in results          # every line the build installs first
            if r["status"] in ("OK", "UNCHECKED") and not r.get("later")]
    if len(reqs) < 2 and got is None:
        return True, []
    idx = sorted({r["index"] for r in results if r.get("index")})
    with tempfile.TemporaryDirectory() as tmp:
        cmd = [sys.executable, "-m", "pip", "install", "--dry-run", "--ignore-installed",
               "--disable-pip-version-check", "--no-input", "--target", os.path.join(tmp, "t"),
               "--python-version", py, "--implementation", "cp", "--abi", "cp" + py.replace(".", ""),
               "--abi", "abi3", "--abi", "none", "--only-binary=:all:"]
        for p in PLATFORMS:
            cmd += ["--platform", p]
        for k, v in opts:
            cmd += [k, v]
        for u in idx:
            if ("--extra-index-url", u) not in opts:
                cmd += ["--extra-index-url", u]
        rep = os.path.join(tmp, "report.json")
        if got is not None and REPORT["ok"]:
            cmd += ["--report", rep]
        try:
            r = subprocess.run(cmd + reqs, capture_output=True, text=True, timeout=900)
        except subprocess.TimeoutExpired:
            return None, ["pip timed out"]
        if r.returncode == 0 and got is not None:
            try:
                with open(rep, encoding="utf-8") as fh:
                    got["names"] = {canon(i["metadata"]["name"]) for i in json.load(fh)["install"]}
            except (OSError, ValueError, KeyError, TypeError):
                pass
    if r.returncode == 0:
        return True, []
    out = (r.stdout + r.stderr).strip()
    if re.search(NET_ERR, out) and "conflict" not in out.lower():
        return None, ["the package index could not be read"]
    keep = [ln.rstrip() for ln in out.splitlines()
            if re.search(r"Cannot install|conflict is caused by|depends on|requested|No matching distribution|"
                         r"ResolutionImpossible|Could not find a version", ln)]
    return False, keep[:30] or out.splitlines()[-15:]


SKIP = ("WINDOWS", "LOCAL", "MISSING", "DROPPED")


def pin_of(raw):
    m = re.search(r"==\s*([\w.+*]+)", raw or "")
    return m.group(1) if m else None


def set_req(r, raw, why, py, opts, index=None):
    """replace one requirement line by a fixed one and check it again"""
    old = r["raw"]
    m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(\[[^\]]*\])?\s*(.*)$", raw)
    new = check_one((r["line"], raw, r["name"], m.group(3).strip() if m else ""), py, opts, index)
    if index and not new.get("index"):
        new["index"] = index
    r.clear()
    r.update(new)
    r["fixed"] = f"was {old}: {why}"


def autofix(results, py, opts):
    """lines that cannot be built as they are, and have only one way to work: fixed, and listed in the report"""
    fixes = []
    # a package listed twice: pip cannot install two versions - keep the line that installs (the newer one if both)
    by = {}
    for r in results:
        by.setdefault(r["name"], []).append(r)
    for name, rs in by.items():
        if len(rs) > 1:
            good = [r for r in rs if r["status"] not in SKIP] or rs
            keep = max(good, key=lambda r: vkey(pin_of(r["raw"]) or "0"))
            for r in rs:
                if r is not keep:
                    r.update(status="DROPPED", why=f"{name} is listed twice, line {keep['line']} ({keep['raw']}) is kept")
                    fixes.append(f"line {r['line']}: {r['raw']}  LEFT OUT - {r['why']}")
    for r in results:
        pin = pin_of(r["raw"])
        # a pin that has no file for Linux / this Python at all: the nearest version that has one
        if r["status"] == "MISSING" and r.get("alts") and pin and "*" not in pin:
            above = [v for v in r["alts"] if vkey(v) >= vkey(pin)]
            v = above[0] if above else r["alts"][-1]
            old, raw = r["raw"], re.sub(r"==\s*[\w.+*]+", "==" + v, r["raw"].split(" #")[0].strip(), count=1)
            set_req(r, raw, f"no file for Linux / Python {py}", py, opts)
            fixes.append(f"line {r['line']}: {old} -> {raw} (that version has no file for Linux / Python {py}; "
                         "the nearest version that has one)")
        # a path on the PC the file was made on (pip freeze of a conda / local install): install it by name
        elif r["status"] == "LOCAL":
            old, name = r["raw"], r["raw"].split("@")[0].strip()
            set_req(r, name, "a path on the PC the file comes from", py, opts)
            fixes.append(f"line {r['line']}: {old} -> {name} (a path on the PC the file was made on; installed by "
                         "name, newest version)")
    return fixes


def resolve_and_split(results, py, opts, fixes, got=None):
    """install the whole file together, like the build does. When pip refuses because one line pins a version that
    another line (or its dependency) does not accept - typical for a pip freeze from a PC where packages were
    installed in several steps, e.g. torch first and newer nvidia-* versions later - the line with FEWER dependencies
    moves to a second pip step with --no-deps: installed exactly as the file pins it, after the others, the same way
    that PC got it. Nothing is left out and no pin is changed."""
    for _ in range(15):
        ok, why = resolve_together(results, py, opts, got)
        if ok is not False:
            return ok, why
        text = "\n".join(why)
        req = {canon(n) for n in re.findall(r"The user requested ([A-Za-z0-9._-]+)", text)}
        live = {r["name"]: r for r in results if r["status"] in ("OK", "UNCHECKED") and not r.get("later")}
        moved = False
        for line in why:
            m = re.match(r"\s*([A-Za-z0-9._-]+) ([\w.+!-]+) depends on ([A-Za-z0-9._-]+)(.*)$", line.strip())
            if not m:
                continue
            dep, x = canon(m.group(1)), canon(m.group(3))
            if x not in req or x not in live:
                continue
            r = live[x]
            if dep in live and live[dep].get("ndeps") is not None and r.get("ndeps") is not None and \
                    live[dep]["ndeps"] < r["ndeps"]:
                r = live[dep]            # e.g. torchvision (3 dependencies) instead of torch (15)
            if r.get("later"):
                continue
            r["later"] = line.strip()
            fixes.append(f"line {r['line']}: {r['raw']}  INSTALLED IN A SECOND STEP, exactly this version - pip says: "
                         f"{line.strip()}")
            moved = True
        if not moved:
            return ok, why
    return resolve_together(results, py, opts, got)


def dep_applies(dep, py, extras=()):
    """does one Requires-Dist line apply on Linux x86_64 / Python py (with these extras asked for)?"""
    if ";" not in dep:
        return True
    marker = dep.split(";", 1)[1].strip()
    try:
        from pip._vendor.packaging.markers import Marker     # pip's own copy of 'packaging': pip is needed anyway
        m = Marker(marker)
    except Exception:
        return "extra" not in marker
    env = {"python_version": py, "python_full_version": py + ".0", "implementation_version": py + ".0",
           "sys_platform": "linux", "platform_system": "Linux", "platform_machine": "x86_64", "os_name": "posix",
           "implementation_name": "cpython", "platform_python_implementation": "CPython"}
    try:
        return any(m.evaluate(dict(env, extra=e)) for e in [""] + list(extras))
    except Exception:
        return "extra" not in marker


def later_needs(results, py, step1):
    """what the second-step lines need that nothing else installs (--no-deps skips it): {name: entry}.
    step1 = every package the first pip steps install (pip's report), or an empty set when unknown"""
    infile = {r["name"] for r in results}
    need = {}
    for r in results:
        if not r.get("later"):
            continue
        m = re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*\[([^\]]*)\]", r["raw"])
        extras = [canon(x) for x in m.group(1).split(",")] if m else []
        for dep in r.get("requires") or []:
            if not dep_applies(dep, py, extras):
                continue
            mm = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[[^\]]*\])?\s*\(?([^);]*)\)?", dep.split(";")[0].strip())
            if not mm:
                continue
            n = canon(mm.group(1))
            if n in infile or n in step1:
                continue
            e = need.setdefault(n, {"name": mm.group(1), "extras": "", "specs": [], "who": []})
            e["extras"] = e["extras"] or (mm.group(2) or "")
            for sp in mm.group(3).replace(" ", "").split(","):
                if sp and sp not in e["specs"]:
                    e["specs"].append(sp)
            if r["name"] not in e["who"]:
                e["who"].append(r["name"])
    for e in need.values():
        e["req"] = e["name"] + e["extras"] + ",".join(e["specs"])
    return need


def available_versions(name, py, opts, cu_index):
    """the versions that DO have a Linux wheel for this Python (pip lists them when asked for an impossible one)"""
    ok, out = pip_dry(f"{name}==0.0.0.dev0", py, True, opts, cu_index)
    m = re.search(r"\(from versions: ([^)]*)\)", out or "")
    if not m or m.group(1).strip() == "none":
        return []
    return [v.strip() for v in m.group(1).split(",")]


def vkey(v):
    """'2.0.2' -> (2, 0, 2) for sorting (pre-releases sort before the release)"""
    parts = []
    for x in re.split(r"[.+]", v):
        m = re.match(r"(\d+)(.*)", x)
        parts.append((int(m.group(1)), 0 if m.group(2) else 1) if m else (0, 0))
    return parts


def nearest(vs, spec):
    """the versions closest above the pinned one (and the newest)"""
    m = re.search(r"==\s*([\w.+]+)", spec or "")
    final = [v for v in vs if not re.search(r"(a|b|rc|dev)\d*$", v)] or vs
    if not m:
        return final[-4:]
    pin = vkey(m.group(1))
    above = [v for v in sorted(final, key=vkey) if vkey(v) >= pin]
    pick = above[:3] + ([final[-1]] if final and final[-1] not in above[:3] else [])
    return pick or final[-4:]


HEAVY_MB = 100     # a wheel bigger than this (download size) is listed as HEAVY
# source files in an sdist that need a build tool, and which one
SRC_LANG = [(r"\.pyx$|\.pxd$", "Cython -> C"), (r"\.(c|h)$", "C"), (r"\.(cpp|cc|cxx|hpp|hh)$", "C++"),
            (r"(^|/)Cargo\.toml$", "Rust (needs cargo)"), (r"\.(f|f90|f95|for)$", "Fortran (needs gfortran)"),
            (r"(^|/)CMakeLists\.txt$", "CMake"), (r"\.(cu|cuh)$", "CUDA (needs nvcc)")]
# GPU packages that load CUDA / cuDNN from the SYSTEM (base image or LD_LIBRARY_PATH), not from their own pip deps
SYSTEM_CUDA = {"onnxruntime-gpu", "cupy-cuda11x", "cupy-cuda12x", "tensorrt", "paddlepaddle-gpu", "faiss-gpu"}


def url_size(url):
    """download size in bytes (HTTP HEAD), or None"""
    try:
        import urllib.request
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "xray-naive"})
        with urllib.request.urlopen(req, timeout=20) as r:
            n = r.headers.get("Content-Length")
            return int(n) if n else None
    except Exception:
        return None


def sdist_sources(url, size):
    """which compiled languages an sdist contains: [] = pure Python, None = could not look"""
    if not url or (size or 0) > 60 * 1024 * 1024:
        return None
    try:
        import io
        import tarfile
        import urllib.request
        import zipfile
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "xray-naive"}),
                                    timeout=60) as r:
            data = r.read()
        if url.split("#")[0].endswith(".zip"):
            names = zipfile.ZipFile(io.BytesIO(data)).namelist()
        else:
            names = tarfile.open(fileobj=io.BytesIO(data)).getnames()
    except Exception:
        return None
    names = [n for n in names if not re.search(r"(^|/)(tests?|docs?|examples?|benchmarks?)/", n)]
    return sorted({lang for pat, lang in SRC_LANG for n in names if re.search(pat, n, re.I)})


def classify(pick, name, raw):
    """what kind of file pip picked: pure wheel / binary wheel / heavy / sdist (+ what building it needs),
    the download size, and where a GPU package gets CUDA from"""
    info = pick.get("download_info") or {}
    url = info.get("url", "")
    fname = url.split("#")[0].rstrip("/").split("/")[-1]
    meta = pick.get("metadata") or {}
    size = url_size(url) if url.startswith("http") else None
    k = {"file": fname, "version": meta.get("version", ""), "size": size}
    if fname.endswith(".whl"):
        tags = fname[:-4].split("-")[-3:]          # python tag, abi tag, platform tag
        if tags[-1] == "any":
            k["kind"], k["compiler"] = "pure wheel", "no"
            k["what"] = "Python code only: the same file works on any OS / CPU"
        else:
            heavy = size and size > HEAVY_MB * 1024 * 1024
            k["kind"] = ("HEAVY " if heavy else "") + "binary wheel" + (" (abi3)" if tags[1] == "abi3" else "")
            k["compiler"] = "no"
            k["what"] = "compiled C/C++ code (.so files) already built for Linux" + \
                (" - one file for every Python 3 version" if tags[1] == "abi3" else f" - for {tags[0]} only")
    else:
        langs = sdist_sources(url, size)
        k["kind"], k["langs"] = "sdist (source)", langs
        if langs == []:
            k["compiler"], k["what"] = "no", "source, but Python only: pip builds it without a compiler"
        elif langs:
            k["compiler"] = "YES"
            k["what"] = "source with " + ", ".join(langs) + " code: pip compiles it during the build"
        else:
            k["compiler"], k["what"] = "probably", "source: could not look inside - assume it needs a compiler"
    # GPU: does it bring CUDA itself (nvidia-* pip packages), offer it as an extra, or need it from the system?
    deps = meta.get("requires_dist") or []
    nv = [d for d in deps if canon(d).startswith("nvidia-")]
    hard = [re.split(r"[ ;<>=~!\[]", d)[0] for d in nv if "extra" not in d]
    k["nv_reqs"] = [d.split(";")[0].strip() for d in nv if "extra" not in d]
    k["nv_pkgs"] = sorted({canon(h) for h in hard})
    rt = " ".join(d.lower().replace("_", "-") for d in nv if "extra" not in d)
    m = re.search(r"nvidia-cuda-runtime-cu1\d\s*==\s*(\d+)\.(\d+)", rt)
    if m:
        k["cuda_ver"] = f"{m.group(1)}.{m.group(2)}"
    m = re.search(r"\+cu(\d+)(\d)$", meta.get("version", ""))
    if m and not k.get("cuda_ver"):
        k["cuda_ver"] = f"{m.group(1)}.{m.group(2)}"
    extra = sorted({m for d in nv for m in re.findall(r'extra\s*==\s*["\']([^"\']+)', d)})
    asked = re.match(r"^[A-Za-z0-9._-]+\[([^\]]*)\]", raw)
    asked = [canon(e.strip()) for e in asked.group(1).split(",") if e.strip()] if asked else []
    have = {canon(e) for e in (meta.get("provides_extra") or [])}
    if asked and [e for e in asked if e not in have]:
        k["extra_missing"] = [e for e in asked if e not in have]
    if hard:
        k["cuda"] = "own"
        k["gpu"] = f"brings its own CUDA libraries as pip packages ({len(hard)}: " + \
            ", ".join(sorted({canon(h).replace("nvidia-", "").replace("-cu12", "").replace("-cu11", "")
                              for h in hard})[:6]) + ")"
    elif extra and set(extra) & set(asked):
        k["cuda"] = "own"
        k["nv_pkgs"] = sorted({canon(re.split(r"[ ;<>=~!\[]", d)[0]) for d in nv
                               if any(re.search(r'extra\s*==\s*["\']' + e + '["\']', d) for e in asked)})
        k["gpu"] = "brings its own CUDA libraries as pip packages (through the extra [" + ",".join(
            sorted(set(extra) & set(asked))) + "])"
    elif extra:
        k["cuda"] = "system"
        k["gpu"] = "needs CUDA from the system - or write it as " + name + "[" + ",".join(extra) + \
            "] and pip installs the CUDA libraries too"
    elif name in SYSTEM_CUDA:
        k["cuda"] = "system"
        k["gpu"] = "needs CUDA + cuDNN from the SYSTEM: the base image, or nvidia-* pip packages via LD_LIBRARY_PATH" + \
            (" (this version has no [cuda,cudnn] extra)" if name == "onnxruntime-gpu" else "")
    return k


def cuda_size(k, py, opts, cu_index):
    """download size of the nvidia-* pip packages a GPU package pulls in (they are not in its own wheel)"""
    reqs = k.pop("nv_reqs", None)
    if not reqs:
        return
    want = {}
    ok, _ = pip_dry(reqs, py, True, opts, cu_index, want)
    if ok and want.get("all"):
        sizes = [url_size((i.get("download_info") or {}).get("url", "")) for i in want["all"]]
        k["cuda_size"] = sum(n or 0 for n in sizes)


def check_one(item, py, opts, index=None):
    no, raw, name, spec = item
    res = {"line": no, "raw": raw, "name": name, "spec": spec}
    if spec == "LOCAL":
        res.update(status="LOCAL", why="a local file path (pip freeze of a conda / local install): remove the line "
                                       "or write name==version")
        return res
    if name in WINDOWS_ONLY or re.search(r"sys_platform\s*==\s*['\"]win32", raw):
        res.update(status="WINDOWS", why="Windows-only: not needed on Linux, left out")
        return res
    m = re.search(r"\+cu(\d+)", raw)
    cu_index = f"https://download.pytorch.org/whl/cu{m.group(1)}" if m else index
    res["index"] = cu_index
    req = raw.split(";")[0].strip()
    want = {}
    ok, out = pip_dry(req, py, True, opts, cu_index, want)
    if ok:
        res.update(status="OK", why="prebuilt Linux wheel")
        md = (want.get("pick") or {}).get("metadata") or {}
        res["ndeps"] = len([x for x in md.get("requires_dist") or [] if "extra ==" not in x])
        res["requires"] = list(md.get("requires_dist") or [])
        if want.get("pick"):
            res["k"] = classify(want["pick"], name, raw)
            cuda_size(res["k"], py, opts, cu_index)
        return res
    if ok is None:
        res.update(status="UNCHECKED", why=f"could not reach the package index: {out[-160:]}")
        return res
    ok2, out2 = pip_dry(req, py, False, opts, cu_index, want)
    if ok2 and want.get("pick"):
        res["k"] = classify(want["pick"], name, raw)
        if res["k"]["compiler"] == "no" and name not in HEAVY:
            res.update(status="SOURCE", why="no wheel, only the source - but it is Python only: pip builds it "
                                            "without a compiler")
            return res
    if ok2 and name in HEAVY:
        vs = available_versions(name, py, opts, cu_index)
        res["alts"] = nearest(vs, spec) if vs else []
        res.update(status="MISSING", why=f"no prebuilt Linux wheel for Python {py}, only the source - compiling "
                                         f"{name} usually fails or takes very long" +
                   (f"; versions WITH a wheel: {', '.join(nearest(vs, spec))} - change the pin" if vs else ""))
        return res
    if ok2 is None:
        res.update(status="UNCHECKED", why=out2)
        return res
    if ok2:
        lib = DEV_LIBS.get(name)
        res.update(status="COMPILED", dev=(lib[0] if lib else ""),
                   why="no prebuilt Linux wheel for Python " + py + ": pip compiles it during the build (gcc + "
                       "Python headers" + (f" + {lib[0]}" if lib else "") + ")" + (f"; {lib[1]}" if lib and lib[1]
                                                                                    else ""))
        vs = available_versions(name, py, opts, cu_index)
        if vs:
            res["why"] += f"; versions WITH a wheel (no compiler needed): {', '.join(nearest(vs, spec))}"
        return res
    vs = available_versions(name, py, opts, cu_index)
    res["alts"] = nearest(vs, spec) if vs else []
    res.update(status="MISSING", why=f"no file for Linux / Python {py} at all" +
               (f"; versions that exist: {', '.join(nearest(vs, spec))} - change the pin" if vs else
                " - check the name, or the package is not on this index"))
    return res


def mb(n):
    return "?" if not n else f"{n / 1048576:,.0f} MB" if n >= 1048576 else f"{max(1, n // 1024)} KB"


def kind_table(results, py, d=None):
    """the KIND section of naive-check.txt: what each package is, its size, compiler yes/no, GPU / CUDA source"""
    rows = [r for r in results if r.get("k")]
    if not rows:
        return (["(KIND table left out: pip older than 22.2 cannot report what it picked - "
                 "'python -m pip install -U pip' and run again)", ""] if not REPORT["ok"] else []) + \
            (["WHICH BASE IMAGE, AND WHY"] + ["  " + x for x in d["why_base"]] + [""] if d else [])
    rows.sort(key=lambda r: -(r["k"].get("size") or 0))
    w = max(len(r["name"]) for r in rows) + 2
    T = [f"WHAT KIND OF PACKAGE EACH ONE IS (the file pip picks for Linux, Python {py}; size = download, installed "
         "is about 2x)", f"  {'package':<{w}}{'version':<16}{'kind':<26}{'size':>9}  compiler",
         "  " + "-" * (w + 60)]
    for r in rows:
        k = r["k"]
        T.append(f"  {r['name']:<{w}}{k.get('version', '')[:15]:<16}{k['kind']:<26}{mb(k.get('size')):>9}  "
                 f"{k['compiler']}")
        if k.get("cuda_size"):
            T.append(f"  {'  + its nvidia-* CUDA packages':<{w + 42}}{mb(k['cuda_size']):>9}  no   (pulled in by "
                     f"{r['name']}, not in its own file)")
    total = sum((r["k"].get("size") or 0) + (r["k"].get("cuda_size") or 0) for r in rows)
    T.append(f"  {'total download':<{w + 42}}{mb(total):>9}   (other dependencies of these packages are not "
             "counted)")
    T += ["", "  pure wheel        Python code only - works on any OS / CPU, nothing to compile",
          "  binary wheel      compiled C/C++ (.so files), already built for Linux + this Python - nothing to compile",
          "  (abi3)            one binary file that works for every Python 3 version",
          f"  HEAVY             a binary wheel over {HEAVY_MB} MB (usually it carries big C++ / CUDA libraries)",
          "  sdist (source)    no wheel for Linux + this Python: pip builds it in the image - compiler YES if it "
          "has C / C++ / Cython / Rust code"]
    odd = [r for r in rows if r["k"].get("compiler") != "no"]
    for r in odd:
        T.append(f"     {r['name']}: {r['k']['what']}")
    for r in rows:
        if r["k"].get("extra_missing"):
            T.append(f"  NOTE {r['raw']}: this version has no extra [{','.join(r['k']['extra_missing'])}] - pip "
                     "ignores it (installs nothing more)")
    gpu = [r for r in rows if r["k"].get("gpu")]
    if gpu:
        T += ["", "GPU - WHERE EACH GPU PACKAGE GETS ITS CUDA LIBRARIES FROM (the driver always comes from the host)"]
        T += [f"  {r['name']}: {r['k']['gpu']}" for r in gpu]
    if d and d.get("why_base"):
        T += ["", "WHICH BASE IMAGE, AND WHY (Dockerfile.naive section 1)"] + ["  " + x for x in d["why_base"]]
    if d and d.get("notes"):
        T += [""] + ["  NOTE " + x for x in d["notes"]]
    return T + [""]


def gpu_from_static(path):
    try:
        with open(path, encoding="utf-8") as fh:
            st = json.load(fh)
    except (OSError, ValueError):
        return None
    return any(f.get("category") == "gpu_usage" for f in st.get("facts", []))


# system libraries a pip package loads when it RUNS (Debian / Ubuntu names; GLIB = the right glib for the OS)
RUNTIME_APT = {
    "opencv-python": (["libgl1", "GLIB", "libx11-6", "libxcb1"], "cv2 loads OpenGL (libGL.so.1), glib, and X11 for "
                                                                  "its windows (imshow)"),
    "opencv-contrib-python": (["libgl1", "GLIB", "libx11-6", "libxcb1"], "cv2 loads OpenGL, glib and X11 (windows)"),
    "opencv-python-headless": (["GLIB"], "cv2 (headless: no windows) still loads glib"),
    "lightgbm": (["libgomp1"], "OpenMP runtime"),
    "pyzbar": (["libzbar0"], "the zbar barcode library"),
    "python-magic": (["libmagic1"], "the libmagic file-type library"),
    "pyaudio": (["libportaudio2"], "the PortAudio library"),
    "pytesseract": (["tesseract-ocr"], "calls the tesseract program"),
    "ffmpeg-python": (["ffmpeg"], "calls the ffmpeg program"),
    "pdf2image": (["poppler-utils"], "calls pdftoppm (poppler)"),
    "psycopg2": (["libpq5"], "the PostgreSQL client library (psycopg2-binary brings its own)"),
    "mysqlclient": (["libmariadb3"], "the MySQL / MariaDB client library"),
    "pycairo": (["libcairo2"], "the cairo library"),
}
# packages that go into the first pip layer: big, change rarely, must match each other
FRAMEWORKS = {"torch", "torchvision", "torchaudio", "tensorflow", "tensorflow-cpu", "onnxruntime", "onnxruntime-gpu",
              "jax", "jaxlib", "paddlepaddle", "paddlepaddle-gpu", "tensorrt"}
# nvidia-* pip package -> its folder in site-packages/nvidia/ (what onnxruntime & co. load by name)
NV_DIR = {"cudnn": "cudnn", "cublas": "cublas", "cuda-runtime": "cuda_runtime", "cuda-nvrtc": "cuda_nvrtc",
          "cufft": "cufft", "curand": "curand"}


def vtuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", v or "")[:2]) or (0,)


def decide(results, py, gpu):
    """every Dockerfile.naive decision, with its reason: base image, apt libraries, compilers, pip groups,
    LD_LIBRARY_PATH"""
    live = [r for r in results if r["status"] not in SKIP]
    names = {r["name"] for r in live}
    known = [r for r in live if r.get("k")]
    own = [r for r in known if r["k"].get("cuda") == "own"]
    system = [r for r in known if r["k"].get("cuda") == "system"]
    loads_system = [r for r in known if r["name"] in SYSTEM_CUDA]     # load CUDA by name (LD_LIBRARY_PATH)
    comp = [r for r in live if r["status"] == "COMPILED"]
    langs = set()
    for r in comp:
        langs |= set((r.get("k") or {}).get("langs") or ["C"])
    nvcc = "CUDA (needs nvcc)" in langs
    ort = next((r for r in live if r["name"] == "onnxruntime-gpu"), None)
    ort_v = vtuple((ort.get("k") or {}).get("version") or ort["spec"]) if ort else None
    torch_cuda = next((r["k"]["cuda_ver"] for r in own if r["k"].get("cuda_ver")), None)
    d = {"why_base": [], "notes": [], "apt": [], "ld": [], "venv": False}
    if not gpu:
        d.update(base=f"python:{py}-slim", os="debian", pyv=py)
        d["why_base"].append(f"CPU image: python:{py}-slim = Debian + Python {py} only (about 120 MB). Every library "
                             "the packages need is added below, each with its reason.")
    elif nvcc:
        d.update(base="nvidia/cuda:12.8.1-cudnn-devel-ubuntu24.04", os="ubuntu24.04", pyv="3.12", venv=True)
        d["why_base"] += [", ".join(r["name"] for r in comp if "CUDA (needs nvcc)" in ((r.get("k") or {}).get("langs")
                                                                                       or []))
                          + " contains CUDA source code (.cu): compiling it needs nvcc, the GPU compiler. Only the "
                          "-devel images have it (about 9 GB). Look for a version with a prebuilt wheel first."]
    elif own:
        d.update(base=f"python:{py}-slim", os="debian", pyv=py)
        d["why_base"] += [", ".join(r["name"] for r in own) + " bring" + ("s" if len(own) == 1 else "") +
                          " the CUDA libraries as pip packages (nvidia-*). The driver (libcuda.so) comes from the "
                          "host: --gpus all mounts it in.",
                          f"So the base needs no CUDA: python:{py}-slim. An nvidia/cuda base would be a SECOND copy "
                          "of the same libraries (about 3 GB more, no speed gain)."]
    elif system or not known:
        if ort_v and ort_v < (1, 19):
            d.update(base="nvidia/cuda:11.8.0-cudnn8-runtime-ubuntu22.04", os="ubuntu22.04", pyv="3.10", venv=True)
        elif py == "3.10":
            d.update(base="nvidia/cuda:12.8.1-cudnn-runtime-ubuntu22.04", os="ubuntu22.04", pyv="3.10", venv=True)
        else:
            d.update(base="nvidia/cuda:12.8.1-cudnn-runtime-ubuntu24.04", os="ubuntu24.04", pyv="3.12", venv=True)
        if known:
            d["why_base"] += [", ".join(r["name"] for r in system) + (" needs" if len(system) == 1 else " need") +
                              " CUDA + cuDNN from the system, and no "
                              "package here brings them: the nvidia/cuda cudnn-runtime image has them (" +
                              d["base"] + ")." + (" onnxruntime-gpu < 1.19 is built for CUDA 11.8 + cuDNN 8." if
                                                  d["pyv"] == "3.10" and ort_v and ort_v < (1, 19) else ""),
                              "The tag, part by part: cudnn = cuDNN included (libcudnn.so.9 / .so.8); runtime = the "
                              "libraries only (-devel adds the compiler nvcc + headers, about 5 GB more, only for "
                              "compiling CUDA code; -base has only libcudart); " + d["os"].replace("ubuntu", "Ubuntu ") +
                              f" = its Python is {d['pyv']}." + (" CUDA 12.8 runs on every current RTX card, RTX 50xx "
                                                                 "too; it needs NVIDIA driver 570 or newer." if
                                                                 d["base"].startswith("nvidia/cuda:12.8") else "")]
            if ort_v and ort_v >= (1, 21):
                d["why_base"].append("Smaller alternative: python-slim + onnxruntime-gpu[cuda,cudnn] (pip installs "
                                     "only the CUDA parts onnxruntime needs) + LD_LIBRARY_PATH.")
        else:
            d["why_base"].append("GPU image, package kinds unknown (pip could not report them): nvidia/cuda "
                                 "cudnn-runtime is the safe base.")
    else:
        d.update(base=f"python:{py}-slim", os="debian", pyv=py)
        d["why_base"].append("--gpu was given, but no package here uses CUDA (torch, onnxruntime-gpu, ...): "
                             f"python:{py}-slim. If the app reaches the GPU some other way, tell X-Ray (Step 2 shows "
                             "GPU use).")
    if d["os"].startswith("ubuntu") and d["pyv"] != py:
        d["notes"].append(f"the base has Python {d['pyv']}, the check ran for {py}: run xray_naive.py again with "
                          f"--python {d['pyv']} so the versions are checked for the Python that is really in the image")
    if gpu and torch_cuda and vtuple(torch_cuda) < (12, 8):
        d["notes"].append(f"torch here is built for CUDA {torch_cuda}: it does not run on RTX 50xx (Blackwell, "
                          "sm_120) cards - those need a cu128 or newer build")
    if ort_v and ort_v < (1, 19) and torch_cuda and vtuple(torch_cuda) >= (12, 0):
        d["notes"].append("onnxruntime-gpu < 1.19 is built for CUDA 11.8 + cuDNN 8, torch brings CUDA 12 + cuDNN 9: "
                          "onnxruntime falls back to the CPU - use onnxruntime-gpu 1.19 or newer")
    # where Python packages live in this base
    d["site"] = (f"/opt/venv/lib/python{d['pyv']}/site-packages" if d["venv"] else
                 f"/usr/local/lib/python{d['pyv']}/site-packages")
    glib = "libglib2.0-0t64" if d["os"] == "ubuntu24.04" else "libglib2.0-0"
    if d["venv"]:
        d["apt"].append((["python3", "python3-venv"], "the nvidia/cuda image has no Python"))
    for n in sorted(names):
        if n in RUNTIME_APT:
            pk, why = RUNTIME_APT[n]
            d["apt"].append(([glib if x == "GLIB" else x for x in pk], f"{n}: {why}"))
    # compilers: only for the packages pip builds from source, removed again in the same RUN
    if comp:
        tools = (["build-essential", "python3-dev"] if d["os"].startswith("ubuntu") else
                 ["gcc", "libc6-dev"] + (["g++"] if langs & {"C++", "CMake"} else []))
        keep = []
        if "CMake" in langs:
            tools.append("cmake")
        if "Rust (needs cargo)" in langs:
            tools.append("cargo")
        if "Fortran (needs gfortran)" in langs:
            keep.append("gfortran")
        dev = sorted({x for r in comp for x in r.get("dev", "").split() if x})
        d["compile"] = {"tools": tools, "keep": keep + dev, "langs": sorted(langs),
                        "who": [(r["name"], ", ".join((r.get("k") or {}).get("langs") or ["C (could not look inside)"]))
                                for r in comp]}
    # LD_LIBRARY_PATH: a package that loads CUDA by name + nvidia-* pip packages present + no CUDA in the base
    nv = sorted({p for r in own for p in r["k"].get("nv_pkgs", [])})
    if loads_system and nv and not d["base"].startswith("nvidia/"):
        for p in nv:
            key = re.sub(r"^nvidia-|-cu1\d$", "", p)
            if key in NV_DIR:
                d["ld"].append(f"{d['site']}/nvidia/{NV_DIR[key]}/lib")
        d["ld_why"] = (", ".join(r["name"] for r in loads_system) + " opens libcudart / libcublas / libcudnn by NAME: "
                       "Linux looks in the system folders and in LD_LIBRARY_PATH. These folders are INSIDE the image - "
                       "the nvidia-* pip packages (from " + ", ".join(r["name"] for r in own) + ") put the libraries "
                       "there. Nothing comes from the host except the driver (libcuda.so), which --gpus all mounts in.")
    # pip groups
    first = [r for r in live if not r.get("later")]
    d["frameworks"] = [r for r in first if r["name"] in FRAMEWORKS or
                       (r.get("k") or {}).get("kind", "").startswith("HEAVY")]
    d["rest"] = [r for r in first if r not in d["frameworks"]]
    d["later"] = [r for r in live if r.get("later")]
    return d


COPY_MAX_MB = 500      # a file from the PC up to this size is copied into the image by default; bigger: mounted


def outside_files(static_path, project, name, copy, mount=()):
    """files the code reads from the PC, outside the project (from the Step 1 map, with xray_runfile's rules): where
    each one is inside the container, and whether it is COPIED into the image or KEPT OUTSIDE (mounted with -v)"""
    try:
        sys.path.insert(0, HERE)
        import xray_runfile as rf
        with open(static_path, encoding="utf-8") as fh:
            st = json.load(fh)
    except (ImportError, OSError, ValueError):
        return None
    p = rf.plan(st, project, name, None, None, 180)
    items = []
    for host, cont, _mode, _why in p["mounts"]:
        envs = [(n, v) for n, v, _w in p["env"] if v == cont or v.startswith(cont + "/")]
        hn = host.replace("\\", "/").rstrip("/")
        used = set()
        for f in st.get("facts", []):        # every file the code reads in that folder (model.onnx, model.onnx.block ...)
            sub = str(f.get("subject", "")).replace("\\", "/")
            if f.get("category") in ("model_file", "data_file_read", "config_file") and \
                    f.get("required_at_runtime") != "no" and sub.lower().startswith(hn.lower() + "/") and "<" not in sub:
                rel = sub[len(hn) + 1:]
                if "/" not in rel:
                    used.add(rel)
        used |= {v[len(cont):].lstrip("/") for _n, v in envs if v != cont}
        here = sorted(x for x in used if os.path.isfile(os.path.join(host, x)))   # only files that exist on this PC
        size = sum(os.path.getsize(os.path.join(host, x)) for x in here)
        names = {n for n, _v in envs}
        if "all" in mount or names & set(mount):
            cp, why = False, "you chose --mount"
        elif "all" in copy or names & set(copy):
            cp, why = True, "you chose --copy"
        elif not here:
            cp, why = False, "not found on this PC, so it cannot be copied"
        elif size <= COPY_MAX_MB * 1048576:
            cp, why = True, f"small ({mb(size)}): copied, so the image runs on its own"
        else:
            cp, why = False, f"big ({mb(size)}): kept outside so the image stays small"
        items.append({"host": host, "cont": cont, "ctx": cont.rsplit("/", 1)[-1], "env": envs, "copy": cp,
                      "why": why, "files": here, "missing": sorted(used - set(here))})
    return {"items": items, "outs": [(n, v) for n, v, _w in p["env"] if v.startswith("/xout/")],
            "env": p["env"], "cpu": p["env_cpu"], "other": p["env_optional"], "gui": p["gui"], "gpu": p["gpu"],
            "changes": rf.code_changes(st, project)}


def write_files(out_dir, results, opts, py, gpu, entry, reqname, d, o=None):
    os.makedirs(out_dir, exist_ok=True)
    idx = sorted({r["index"] for r in results if r.get("index")})
    # requirements.docker.txt: the same lines, Windows-only and local paths out, PyTorch index in
    L = [f"# written by xray_naive.py {VERSION} from {reqname} - see naive-check.txt"]
    L += [f"{k} {v}" for k, v in opts]
    L += [f"--extra-index-url {u}" for u in idx if ("--extra-index-url", u) not in opts]
    for r in results:
        if r["status"] in ("WINDOWS", "LOCAL"):
            L.append(f"# {r['raw']}    <- {r['status'].lower()}: left out")
        elif r["status"] == "DROPPED":
            L.append(f"# {r['raw']}    <- left out: {r['why']}")
        elif r.get("later"):
            L.append(f"{r['raw']}    # second pip step, --no-deps (Dockerfile.naive 3c): {r['later']}")
        elif r.get("fixed"):
            L.append(f"{r['raw']}    # fixed by xray_naive, {r['fixed']}")
        elif r["status"] == "MISSING":
            L.append(f"{r['raw']}    # <- MISSING for Linux/Python {py}: change this pin (naive-check.txt)")
        else:
            L.append(r["raw"])
    for e in d.get("later_needs") or []:
        L.append(f"# + {e['req']}    <- not in your file: needed by {', '.join(e['who'])} (second step), added in "
                 "Dockerfile.naive 3c")
    with open(os.path.join(out_dir, "requirements.docker.txt"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(L) + "\n")

    def bar(title):
        return ["", "# " + "=" * 100, "# " + title, "# " + "=" * 100]

    def desc(r):
        k = r.get("k") or {}
        if not k:
            return r["status"].lower()
        t = f"{k['kind']}, {mb(k.get('size'))}"
        if k.get("cuda_size"):
            t += f" + {mb(k['cuda_size'])} nvidia-* CUDA packages"
        if k.get("extra_missing"):
            t += f"  (the extra [{','.join(k['extra_missing'])}] does not exist in this version: pip ignores it)"
        return t

    def pip_run(rs, pre="RUN ", post=None):
        ix = [f"--extra-index-url {u}" for u in sorted({r["index"] for r in rs if r.get("index")})
              if ("--extra-index-url", u) not in opts]
        head = " ".join(["pip install --no-cache-dir"] + [f"{k} {v}" for k, v in opts] + ix)
        lines = [pre + head + " \\"] + [f"        '{r['raw'].split(' #')[0]}' \\" for r in rs]
        if post:
            lines += post
        else:
            lines[-1] = lines[-1][:-2]
        return lines

    w = max([len(r["raw"]) for r in results] + [10]) + 2
    D = [f"# Naive Dockerfile written by xray_naive.py {VERSION} from {reqname}.",
         "# It only has to BUILD and RUN; every line says why it is there (details: naive-check.txt).",
         "# X-Ray Steps 2-4 then watch the real run and make it small. It runs as root, so you can apt-get inside "
         "when you explore."]
    D += bar("1. BASE IMAGE")
    D += ["# " + x for x in d["why_base"]] + ["# NOTE: " + x for x in d["notes"]]
    D += [f"FROM {d['base']}", "ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1" +
          (" DEBIAN_FRONTEND=noninteractive" if d["os"].startswith("ubuntu") else "")]
    if d["apt"]:
        D += bar("2. SYSTEM LIBRARIES the packages load when they RUN (apt)")
        D += [f"#   {' '.join(pk):<40} <- {why}" for pk, why in d["apt"]]
        D += ["RUN apt-get update && apt-get install -y --no-install-recommends \\",
              "        " + " ".join(dict.fromkeys(x for pk, _ in d["apt"] for x in pk)) + " \\",
              " && rm -rf /var/lib/apt/lists/*"]
    if d["venv"]:
        D += ["# Ubuntu does not allow pip into its own Python: a virtual environment, first on PATH",
              "RUN python3 -m venv /opt/venv", "ENV PATH=/opt/venv/bin:$PATH"]
    D += ["", "WORKDIR /app"]
    if d["frameworks"]:
        D += bar("3a. BIG FRAMEWORKS - their own layer, first: Docker reuses it while the lines below change")
        D += [f"#   {r['raw']:<{w}} {desc(r)}" for r in d["frameworks"]]
        D += pip_run(d["frameworks"])
    rest = d["rest"]
    if rest or results:
        D += bar("3b. THE OTHER PACKAGES")
        c = d.get("compile")
        if c:
            D += ["#   compiled from source by pip (no wheel for Linux + this Python) - the compiler is installed for "
                  "this RUN only:"]
            D += [f"#     {n:<{w - 2}} {lang}" for n, lang in c["who"]]
            D += [f"#   compiler: {' '.join(c['tools'])}" +
                  (f";  kept (their runtime part is needed): {' '.join(c['keep'])}" if c["keep"] else "")]
        elif rest:
            D.append(f"#   nothing to compile: every package has a prebuilt wheel for Linux + Python {d['pyv']} - "
                     "no compiler in the image")
        groups = [("prebuilt binary wheels (compiled code, nothing to compile)", lambda k: "binary" in k.get("kind", "")),
                  ("pure Python", lambda k: k.get("kind") == "pure wheel" or (k.get("kind", "").startswith("sdist")
                                                                              and k.get("compiler") == "no")),
                  ("not classified", lambda k: not k)]
        shown = {r["name"] for r in rest if r["status"] == "COMPILED"}
        for title, f in groups:
            rs = [r for r in rest if r["name"] not in shown and f(r.get("k") or {})]
            shown |= {r["name"] for r in rs}
            if rs:
                D.append(f"#   {title}: " + ", ".join(r["name"] for r in rs))
        out = [r for r in results if r["status"] in SKIP]
        D += [f"#   FIXED: {r['raw']}  ({r['fixed']})" for r in results if r.get("fixed") and r["status"] not in SKIP]
        for r in out:
            D.append(f"#   LEFT OUT ({r['status']}): {r['raw']}" +
                     ("   <- change the pin, see naive-check.txt" if r["status"] == "MISSING" else ""))
        if rest:
            if c:
                pre = ["RUN apt-get update && apt-get install -y --no-install-recommends " +
                       " ".join(c["tools"] + c["keep"]) + " \\"]
                post = [" && apt-get purge -y --auto-remove " + " ".join(c["tools"]) +
                        " && rm -rf /var/lib/apt/lists/*"]
                D += pre + pip_run(rest, pre=" && ", post=post)
            else:
                D += pip_run(rest)
    if d["later"]:
        D += bar("3c. LINES PIP WILL NOT INSTALL TOGETHER WITH THE OTHERS - installed second, exactly as pinned")
        D += ["# Each of these pins a version another package does not accept (pip's reason on the right). Your file",
              "# comes from a PC where packages were installed in several steps, so it has these exact versions working",
              "# together. Installing them AFTER the others with --no-deps gives the same: nothing left out, no pin changed.",
              "# pip check below prints the mismatch in the build log - information only, the build goes on."]
        D += [f"#   {r['raw']:<{w}} {r['later']}" for r in d["later"]]
        post = []
        if d.get("later_needs"):
            ns = d["later_needs"]
            D += ["# --no-deps also skips what these packages need. Already installed above: everything else they need.",
                  "# Not in your file and not installed above - added right after, with their own dependencies; -c (the",
                  "# versions installed so far) keeps every version above exactly as it is:"]
            D += [f"#   {e['req']:<{w}} needed by {', '.join(e['who'])}" for e in ns]
            add = pip_run([{"raw": e["req"], "index": None} for e in ns], pre="")
            add[0] = add[0].replace("pip install --no-cache-dir", "pip install --no-cache-dir -c /tmp/xray-installed.txt", 1)
            for u in sorted({r["index"] for r in d["later"] if r.get("index")}):
                if ("--extra-index-url", u) not in opts and f"--extra-index-url {u}" not in add[0]:
                    add[0] = add[0].replace(" \\", f" --extra-index-url {u} \\", 1)
            add[-1] += " \\"
            post = [" && pip freeze > /tmp/xray-installed.txt \\", " && (" + add[0]] + add[1:] + \
                ['     || echo "xray_naive: they could not be added without changing a version installed above - see pip"'
                 ") \\"]
        second = pip_run(d["later"], post=post + [" && (pip check || true)"])
        second[0] = second[0].replace("pip install --no-cache-dir", "pip install --no-cache-dir --no-deps", 1)
        D += second
    if d["ld"]:
        D += bar("4. LD_LIBRARY_PATH - where to find the CUDA libraries")
        D += ["# " + x.strip() for x in re.findall(r".{1,110}(?:\s|$)", d["ld_why"])]
        D += ["ENV LD_LIBRARY_PATH=" + ":\\\n".join(d["ld"])]
    elif gpu and d["base"].startswith("nvidia/"):
        D += bar("4. LD_LIBRARY_PATH - nothing to add")
        D += ["# The nvidia/cuda base already sets LD_LIBRARY_PATH=/usr/local/cuda/lib64 (libcudart, libcublas, libcufft ...)",
              "# and has cuDNN in /usr/lib/x86_64-linux-gnu, a folder Linux always searches. The driver (libcuda.so)",
              "# comes from the host: --gpus all mounts it in."]
    D += bar("5. THE CODE (all of it - X-Ray finds out what is really used)")
    D += ["COPY . ."]
    if o and (o["items"] or o["outs"]):
        D += bar("6. FILES OUTSIDE THE PROJECT - the code reads them from your PC (found in the Step 1 map)")
        D += ["# A container cannot see C:\\ or D:\\. Each file is either COPIED into the image (the image grows by its size",
              "# and runs anywhere on its own) or KEPT OUTSIDE and mounted with -v when the container starts (the image stays",
              "# small, the file can change without a rebuild). Choose: xray_naive.py ... --copy ENV_NAME (or --copy all).",
              "# Either way the env var below points at the same path inside the container."]
        was = {n: w for n, _v, w in o["env"]}
        for it in o["items"]:
            names = ", ".join(n for n, _v in it["env"]) or "(no env var)"
            D.append(f"#   {names}: your code's default {code_default(was.get(it['env'][0][0], '')) if it['env'] else ''}"
                     " is a path on your PC - in the container it is the path below")
            D.append(f"#     {it['host']}  ->  {it['cont']}   " +
                     (f"COPIED into the image ({it['why']})" if it["copy"] else
                      f"KEPT OUTSIDE ({it['why']}): docker run -v \"{it['host']}:{it['cont']}:ro\""))
            if it["copy"]:
                D.append(f"# xray-build-context: {it['ctx']}={it['host']}")
                D.append(f"COPY --from={it['ctx']} " + json.dumps((it["files"] or ["."]) + [it["cont"] + "/"]))
            D += [f"ENV {n}={json.dumps(v)}" for n, v in it["env"]]
        if o["outs"]:
            D.append("# files the program writes: /xout (mount a folder there to keep them: -v \"%CD%\\out:/xout\")")
            D += [f"ENV {n}={json.dumps(v)}" for n, v in o["outs"]] + ["RUN mkdir -p /xout"]
        if o["other"]:
            D.append("# other settings the code reads keep its defaults (change one at run time: -e NAME=value): " +
                     ", ".join(f"{n}={d}" for n, d, _s in o["other"]))
    if o and o["changes"]:
        D += bar("CODE CHANGES NEEDED - a Windows / PC path written in the code: no Docker setting can fix it")
        for i, c in enumerate(o["changes"], 1):
            D += [f"#   {i}. {c['where']}", f"#      now:       {c['now'] or ''}",
                  f"#      change to: {c['new'] or '(by hand: ' + c['note'] + ')'}"]
        D.append("#   then run xray_static.py + xray_naive.py again: the new env var gets its container path here")
    D += [f'CMD ["python", "{entry}"]']
    with open(os.path.join(out_dir, "Dockerfile.naive"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(D) + "\n")
    with open(os.path.join(out_dir, "Dockerfile.naive.dockerignore"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join([".git/", ".venv/", "venv/", "env/", ".idea/", ".vscode/", "**/__pycache__/",
                            "**/*.pyc", "logs/", "runs/", "*.log"]) + "\n")
    return d["base"]


def code_default(why):
    """xray_runfile's note 'was C:\\x.onnx' / 'output file; was out.csv - kept in ...' -> the code's default value"""
    m = re.search(r"was (.*?)(?: - kept in .*|: ask for .*)?$", why or "")
    return m.group(1) if m else (why or "")


def run_help(o, a, gpu):
    """naive-check.txt: the files outside the project (copied or mounted), and the exact build and run commands"""
    img, T = f"{a.name}-naive", ["", "FILES OUTSIDE THE PROJECT, AND HOW TO BUILD + RUN THIS IMAGE"]
    if o is None:
        return T + ["  (run with --static runs\\NAME\\NAME.json: then the files your code reads from your PC are handled "
                    "here - copied into the image or mounted - with their env vars)"]
    T += ["  A container is Linux: C:\\... and D:\\... do not exist in it. Every env var your code reads:"]
    for n, v, w in o["env"]:
        T += [f"    {n:<16} your code: {code_default(w)}", f"    {'':<16} -> in the container: {v}" +
              ("  (an output: mount a folder at /xout to keep it)" if v.startswith("/xout/") else
               "  (Dockerfile.naive sets it)")]
    for n, v, w in o["cpu"]:
        T.append(f"    {n:<16} your code: {code_default(w)}  -> used as it is (X-Ray's CPU run sets {v})")
    for n, d, _site in o["other"]:
        T.append(f"    {n:<16} your code: {d or '(no default)'}  -> used as it is")
    T.append("    to change one when you run the image: docker run ... -e NAME=value ...   (or --env-file FILE with "
             "NAME=value lines)")
    if o["changes"]:
        T += ["  CODE CHANGES NEEDED - a Windows / PC path written in the code without an env var (no Docker setting "
              "can fix it):"]
        for i, c in enumerate(o["changes"], 1):
            T += [f"    {i}. WHERE:     {c['where']}", f"       NOW:       {c['now'] or ''}",
                  f"       CHANGE TO: {c['new'] or '(by hand: ' + c['note'] + ')'}"]
        T.append("    The old value stays the default, so it still runs the same on your PC. Then run xray_static.py "
                 "and xray_naive.py again.")
    else:
        T.append("  CODE CHANGES NEEDED: none - every path from your PC is read through an env var")
    T.append("  Files your code reads from your PC:")
    for it in o["items"]:
        names = ", ".join(n for n, _v in it["env"]) or "(no env var)"
        T += [f"    {names}: {it['host']}  ->  {it['cont']}  " + ("COPIED into the image: " + ", ".join(
            it["files"] or ["the whole folder"]) if it["copy"] else "KEPT OUTSIDE, mounted at run time") +
              f"  ({it['why']})"]
        if it["missing"]:
            T.append(f"      not on this PC, so not copied: {', '.join(it['missing'])}")
    if o["items"]:
        T.append(f"    To switch: run xray_naive.py again with --copy ENV_NAME / --mount ENV_NAME (or all). Without "
                 f"them: files up to {COPY_MAX_MB} MB are copied, bigger ones stay outside.")
    ctx = "".join(f' --build-context "{it["ctx"]}={it["host"]}"' for it in o["items"] if it["copy"])
    mounts = "".join(f' -v "{it["host"]}:{it["cont"]}:ro"' for it in o["items"] if not it["copy"])
    T += ["  build (in the project folder; build-naive.bat from xray_runfile.py does the same):",
          f"    docker build -f Dockerfile.naive{ctx} -t {img} .",
          "  run it on its own (X-Ray's run files add the tracing on top of the same mounts):",
          f"    docker run --rm{' --gpus all' if gpu else ''}{mounts}" + (' -v "%CD%\\out:/xout"' if o["outs"] else "")
          + f" {img}"]
    if o["gui"]:
        T.append("  the code opens windows (cv2.imshow): a container has no screen - X-Ray's runs switch them off; "
                 "on its own, switch them off the way your code allows")
    return T


def main(argv=None):
    for _s in (sys.stdout, sys.stderr):
        if hasattr(_s, "reconfigure"):
            _s.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="check requirements for Linux before building; write a naive Dockerfile")
    ap.add_argument("--project", required=True, help="the project folder")
    ap.add_argument("--requirements", required=True, help="requirements file (name inside the project, or a path)")
    ap.add_argument("--entry", default="main.py", help="the file the container starts (CMD)")
    ap.add_argument("--name", required=True, help="short name: files go to runs/NAME/naive/")
    ap.add_argument("--python", default="3.12", help="Python version of the image (default 3.12)")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--gpu", action="store_true", help="GPU image (nvidia/cuda base)")
    g.add_argument("--cpu", action="store_true", help="CPU image (python:X base)")
    ap.add_argument("--static", help="the Step 1 map: GPU yes/no is taken from it when --gpu/--cpu is not given")
    ap.add_argument("--jobs", type=int, default=8, help="parallel pip checks (default 8)")
    ap.add_argument("--copy", action="append", default=[], metavar="ENV_NAME",
                    help="a file the code reads from your PC (needs --static): COPY it into the image (repeatable; "
                         "all = every one). Default: copied when up to 500 MB, else mounted at run time")
    ap.add_argument("--mount", action="append", default=[], metavar="ENV_NAME",
                    help="keep this file outside the image: mounted at run time (repeatable; all = every one)")
    a = ap.parse_args(argv)
    req_path = a.requirements if os.path.isfile(a.requirements) else os.path.join(a.project, a.requirements)
    if not os.path.isfile(req_path):
        sys.exit(f"requirements file not found: {req_path}")
    gpu = True if a.gpu else False if a.cpu else bool(gpu_from_static(a.static)) if a.static else None
    if gpu is None:
        gpu = False
        print("  (no --gpu / --cpu / --static: writing a CPU Dockerfile; add --gpu for a GPU image)")
    reqs, opts, notes = parse_requirements(req_path)
    seen = {}
    for no, raw, name, spec in reqs:
        if name in seen:
            pass        # handled by autofix: one line is kept, the other left out (listed in the report)
        seen.setdefault(name, no)
    print(f"xray_naive {VERSION}: checking {len(reqs)} requirements for Linux x86_64, Python {a.python} "
          f"(pip dry run, nothing is installed) ...", flush=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, a.jobs)) as ex:
        results = list(ex.map(lambda it: check_one(it, a.python, opts), reqs))
    fixes = autofix(results, a.python, opts)
    print("  ... and the whole file together, with dependencies, as the build installs it", flush=True)
    got = {}
    together, why_not = resolve_and_split(results, a.python, opts, fixes, got)
    needs = later_needs(results, a.python, got.get("names") or set())
    if needs:
        fixes.append("ADDED after the second step (--no-deps skips what those lines need; these are not in your file and "
                     "nothing else installs them): " + "; ".join(f"{e['req']} (needed by {', '.join(e['who'])})"
                                                                for e in needs.values()))
    out_dir = os.path.join(HERE, "runs", a.name, "naive")
    d = decide(results, a.python, gpu)
    d["later_needs"] = list(needs.values())
    o = outside_files(a.static, a.project, a.name, a.copy, a.mount) if a.static else None
    base = write_files(out_dir, results, opts, a.python, gpu, a.entry, os.path.basename(req_path), d, o)
    groups = {}
    for r in results:
        groups.setdefault(r["status"], []).append(r)
    T = [f"X-Ray naive check {VERSION} - {req_path}", f"for: Linux x86_64, Python {a.python}, "
         f"{'GPU' if gpu else 'CPU'} image (base {base})", ""]
    pv = pip_version()
    if pv and pv < (24, 2) and sys.version_info[:2] < vtuple(a.python):
        T += [f"WARNING: your pip ({'.'.join(map(str, pv))}, running on Python {sys.version_info[0]}.{sys.version_info[1]}) "
              f"checks 'Requires-Python' against Python {sys.version_info[0]}.{sys.version_info[1]}, not {a.python}:",
              f"  packages that need Python {a.python} may show up as MISSING, or with an older version than the build "
              "installs.", "  Fix: python -m pip install -U pip   then run xray_naive.py again.", ""]
    if fixes:
        T += ["HANDLED SO THE BUILD WORKS (requirements.docker.txt and Dockerfile.naive; your own file is unchanged) - "
              "read them once:"]
        T += ["  " + x for x in fixes]
        if any(r.get("later") for r in results):
            T += ["  Why a second step: a Docker build installs the whole file with ONE pip install, and in one install",
                  "  pip will not put a package next to a version another package rejects. Your file comes from a PC",
                  "  where packages were installed in several steps (pip only warns there), so that PC has exactly these",
                  "  versions. Dockerfile.naive does the same: first everything else, then these lines with --no-deps.",
                  "  Whether the program works with them (the GPU too): the X-Ray Step 2 run, or xray_look.py --image."]
        T.append("")
    if together is False:
        T += ["CANNOT BE INSTALLED TOGETHER, and there is no single right fix - pip's reason (Linux / Python " +
              a.python + "):"]
        T += ["    " + x.strip() for x in why_not] + [""]
    elif together is None:
        T += ["NOT CHECKED: the whole file together (" + "; ".join(why_not) + ") - single lines were checked", ""]
    titles = [("MISSING", "NOT AVAILABLE for Linux / this Python - change these pins (the build fails otherwise)"),
              ("COMPILED", "COMPILED during the build - the Dockerfile installs a compiler (+ the -dev libraries)"),
              ("WINDOWS", "WINDOWS-ONLY - left out of requirements.docker.txt"),
              ("LOCAL", "LOCAL PATHS - left out (write name==version instead)"),
              ("UNCHECKED", "NOT CHECKED - the package index could not be reached"),
              ("SOURCE", "SOURCE ONLY, but pure Python - builds without a compiler"),
              ("OK", "OK - a prebuilt Linux wheel exists")]
    for st, title in titles:
        rs = groups.get(st, [])
        if not rs:
            continue
        T.append(f"{title} ({len(rs)}):")
        if st == "OK":
            T.append("  " + ", ".join(r["raw"] for r in rs))
        else:
            T += [f"  line {r['line']}: {r['raw']}\n      {r['why']}" for r in rs]
        T.append("")
    T += kind_table(results, a.python, d)
    idx = sorted({r["index"] for r in results if r.get("index")})
    if idx:
        T.append("EXTRA INDEX added to requirements.docker.txt: " + ", ".join(idx) + " (+cuNNN pins exist only there)")
    T += notes
    T += run_help(o, a, gpu)
    T += ["", f"Files written to {out_dir}:", "  Dockerfile.naive, Dockerfile.naive.dockerignore, requirements.docker.txt",
          "Copy them into the project folder, then build (build-naive.bat from xray_runfile.py)."]
    with open(os.path.join(out_dir, "naive-check.txt"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(T) + "\n")
    with open(os.path.join(out_dir, "naive-check.json"), "w", encoding="utf-8") as fh:
        json.dump({"tool": "xray-naive", "version": VERSION, "python": a.python, "gpu": gpu, "base": base,
                   "requirements": results}, fh, indent=1)
    print("\n".join(T))
    return 1 if groups.get("MISSING") or together is False else 0


if __name__ == "__main__":
    sys.exit(main())
