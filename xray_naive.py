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

Then it writes into runs/NAME/naive/:
  naive-check.txt              the table above, with the reason for every line
  requirements.docker.txt      the requirements with the safe fixes (Windows-only out, PyTorch index in)
  Dockerfile.naive             a standard Dockerfile that installs the compiler / -dev libraries the check found
  Dockerfile.naive.dockerignore
Copy the three Docker files into the project folder (the tool never overwrites the project's own files).

  python xray_naive.py --project "C:\\...\\Solution" --requirements requirement.txt --entry main.py --name worker
       [--python 3.12] [--gpu | --cpu] [--static runs\\worker\\worker.json]

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

VERSION = "0.1.0"
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


def pip_dry(req, py, only_binary, opts, cu_index):
    """run pip install --dry-run for one requirement on Linux / py: (ok, text)"""
    with tempfile.TemporaryDirectory() as tmp:
        cmd = [sys.executable, "-m", "pip", "install", "--dry-run", "--quiet", "--no-deps", "--ignore-installed",
               "--disable-pip-version-check", "--no-input", "--target", tmp,
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
        cmd.append(req)
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        except subprocess.TimeoutExpired:
            return None, "pip timed out"
        out = (r.stdout + r.stderr).strip()
        if r.returncode == 0:
            return True, out
        if re.search(NET_ERR, out):     # an index could not be read: "not found" would be a guess
            bad = re.findall(r"(https?://[^\s'\"]+|/whl/\S+)", out)
            return None, "could not read the package index" + (f" ({bad[0]})" if bad else "")
        return False, out


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


def check_one(item, py, opts):
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
    cu_index = f"https://download.pytorch.org/whl/cu{m.group(1)}" if m else None
    res["index"] = cu_index
    req = raw.split(";")[0].strip()
    ok, out = pip_dry(req, py, True, opts, cu_index)
    if ok:
        res.update(status="OK", why="prebuilt Linux wheel")
        return res
    if ok is None:
        res.update(status="UNCHECKED", why=f"could not reach the package index: {out[-160:]}")
        return res
    ok2, out2 = pip_dry(req, py, False, opts, cu_index)
    if ok2 and name in HEAVY:
        vs = available_versions(name, py, opts, cu_index)
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
    res.update(status="MISSING", why=f"no file for Linux / Python {py} at all" +
               (f"; versions that exist: {', '.join(nearest(vs, spec))} - change the pin" if vs else
                " - check the name, or the package is not on this index"))
    return res


def gpu_from_static(path):
    try:
        with open(path, encoding="utf-8") as fh:
            st = json.load(fh)
    except (OSError, ValueError):
        return None
    return any(f.get("category") == "gpu_usage" for f in st.get("facts", []))


def write_files(out_dir, results, opts, py, gpu, entry, reqname):
    os.makedirs(out_dir, exist_ok=True)
    comp = [r for r in results if r["status"] == "COMPILED"]
    names = {r["name"] for r in results if r["status"] != "WINDOWS"}
    dev = sorted({d for r in comp for d in r.get("dev", "").split() if d})
    idx = sorted({r["index"] for r in results if r.get("index")})
    ort = next((r for r in results if r["name"] == "onnxruntime-gpu"), None)
    ort_old = False
    if ort:
        mv = re.search(r"==\s*(\d+)\.(\d+)", ort["spec"])
        ort_old = bool(mv and (int(mv.group(1)), int(mv.group(2))) < (1, 19))
    # requirements.docker.txt: the same lines, Windows-only and local paths out, PyTorch index in
    L = [f"# written by xray_naive.py {VERSION} from {reqname} - see naive-check.txt"]
    L += [f"{k} {v}" for k, v in opts]
    L += [f"--extra-index-url {u}" for u in idx if ("--extra-index-url", u) not in opts]
    for r in results:
        if r["status"] in ("WINDOWS", "LOCAL"):
            L.append(f"# {r['raw']}    <- {r['status'].lower()}: left out")
        elif r["status"] == "MISSING":
            L.append(f"{r['raw']}    # <- MISSING for Linux/Python {py}: change this pin (naive-check.txt)")
        else:
            L.append(r["raw"])
    with open(os.path.join(out_dir, "requirements.docker.txt"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(L) + "\n")
    # Dockerfile.naive
    opencv = bool(names & {"opencv-python", "opencv-contrib-python"})
    D = [f"# Naive Dockerfile written by xray_naive.py {VERSION}: it only has to BUILD and RUN (X-Ray makes it small "
         "later)."]
    if gpu:
        if ort_old:
            base, ub, pyv = "nvidia/cuda:11.8.0-cudnn8-runtime-ubuntu22.04", "22.04", "3.10"
            D.append("# onnxruntime-gpu < 1.19 is built for CUDA 11.8 + cuDNN 8: this base matches it (Python 3.10)")
        elif py == "3.10":
            base, ub, pyv = "nvidia/cuda:12.8.1-cudnn-runtime-ubuntu22.04", "22.04", "3.10"
        else:
            base, ub, pyv = "nvidia/cuda:12.8.1-cudnn-runtime-ubuntu24.04", "24.04", "3.12"
        if pyv != py:
            D.append(f"# NOTE: Ubuntu {ub} has Python {pyv}, the check ran for {py}: run xray_naive.py again with "
                     f"--python {pyv} so the versions are checked for the Python that is really in the image")
        glib = "libglib2.0-0t64" if ub == "24.04" else "libglib2.0-0"
        apt = ["python3", "python3-venv"] + (["python3-dev", "build-essential"] if comp else []) + dev + \
            (["libgl1", glib] if opencv else [])
        D += [f"FROM {base}", "ENV DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1",
              "# Python is not in the nvidia/cuda image; " + ("compiler + headers: " + ", ".join(
                  r["name"] for r in comp) + " are compiled by pip; " if comp else "") +
              ("libgl1 / glib: opencv" if opencv else ""),
              "RUN apt-get update && apt-get install -y --no-install-recommends \\",
              "        " + " ".join(apt) + " \\", "    && rm -rf /var/lib/apt/lists/*",
              "RUN python3 -m venv /opt/venv", "ENV PATH=/opt/venv/bin:$PATH"]
    else:
        D += [f"# python:{py} (the full Debian image, not -slim): gcc and the common -dev libraries are already in it",
              f"FROM python:{py}", "ENV PYTHONUNBUFFERED=1"]
        apt = dev + (["libgl1"] if opencv else [])
        if apt:
            D += ["RUN apt-get update && apt-get install -y --no-install-recommends " + " ".join(apt) +
                  " \\", "    && rm -rf /var/lib/apt/lists/*"]
    D += ["WORKDIR /app", "COPY requirements.docker.txt .",
          "RUN pip install --no-cache-dir -r requirements.docker.txt", "COPY . .",
          f'CMD ["python", "{entry}"]']
    with open(os.path.join(out_dir, "Dockerfile.naive"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(D) + "\n")
    with open(os.path.join(out_dir, "Dockerfile.naive.dockerignore"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join([".git/", ".venv/", "venv/", "env/", ".idea/", ".vscode/", "**/__pycache__/",
                            "**/*.pyc", "logs/", "runs/", "*.log"]) + "\n")
    return base if gpu else f"python:{py}"


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
            notes.append(f"line {no}: {name} is listed twice (also line {seen[name]}) - pip cannot install both: "
                         "keep one")
        seen.setdefault(name, no)
    print(f"xray_naive {VERSION}: checking {len(reqs)} requirements for Linux x86_64, Python {a.python} "
          f"(pip dry run, nothing is installed) ...", flush=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, a.jobs)) as ex:
        results = list(ex.map(lambda it: check_one(it, a.python, opts), reqs))
    out_dir = os.path.join(HERE, "runs", a.name, "naive")
    base = write_files(out_dir, results, opts, a.python, gpu, a.entry, os.path.basename(req_path))
    groups = {}
    for r in results:
        groups.setdefault(r["status"], []).append(r)
    T = [f"X-Ray naive check {VERSION} - {req_path}", f"for: Linux x86_64, Python {a.python}, "
         f"{'GPU' if gpu else 'CPU'} image (base {base})", ""]
    titles = [("MISSING", "NOT AVAILABLE for Linux / this Python - change these pins (the build fails otherwise)"),
              ("COMPILED", "COMPILED during the build - the Dockerfile installs a compiler (+ the -dev libraries)"),
              ("WINDOWS", "WINDOWS-ONLY - left out of requirements.docker.txt"),
              ("LOCAL", "LOCAL PATHS - left out (write name==version instead)"),
              ("UNCHECKED", "NOT CHECKED - the package index could not be reached"),
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
    idx = sorted({r["index"] for r in results if r.get("index")})
    if idx:
        T.append("EXTRA INDEX added to requirements.docker.txt: " + ", ".join(idx) + " (+cuNNN pins exist only there)")
    T += notes
    T += ["", f"Files written to {out_dir}:", "  Dockerfile.naive, Dockerfile.naive.dockerignore, requirements.docker.txt",
          "Copy them into the project folder, then build (build-naive.bat from xray_runfile.py)."]
    with open(os.path.join(out_dir, "naive-check.txt"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(T) + "\n")
    print("\n".join(T))
    return 1 if groups.get("MISSING") else 0


if __name__ == "__main__":
    sys.exit(main())
