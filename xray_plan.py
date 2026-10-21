#!/usr/bin/env python3
"""
xray_plan.py - X-Ray Phase 4: planner. Stdlib only, Python 3.8+.

Turns what the three layers found into Docker decisions and real files:
  static map   (xray_static.py)  what the code references, env vars, secrets, copy / dockerignore draft
  runtime      (xray_trace.py)   what really ran: files written, ports, shared memory, env set but unread
  packages     (xray_pkg.py)     requirements keep/remove/swap/add, system packages python:3.x-slim lacks

  plan   python3 xray_plan.py plan --fixture F3_stream_worker --static f3.json --runtime f3-runtime.json \\
             --pkg f3-pkg.json --out plans/F3_stream_worker
         writes plans/F3_stream_worker/{plan.json, Dockerfile, Dockerfile.dockerignore, compose.xray.yml}
  grade  python3 xray_plan.py grade --plan plans/F3_stream_worker/plan.json --key F3_stream_worker/answer_key.json

A real project names its files differently: pass them (the service(s) are found from the compose file's build
context when they are not named):
  python3 xray_plan.py plan --project ~/myapp/services/api --dockerfile ~/myapp/services/api/Dockerfile \
      --compose ~/myapp/docker-compose.yml [--service api] --static ... --runtime ... --pkg ... --out plans/api

Build + run the planned image from the fixture folder (the override only changes the app services):
  docker compose -f docker-compose.yml -f ../plans/F3_stream_worker/compose.xray.yml build
"""
import argparse
import json
import os
import re
import shutil
import sys
import textwrap

VERSION = "0.1.16"
APP_USER, APP_UID = "app", 1000
APP_HOME = f"/home/{APP_USER}"
ALWAYS_IGNORE = [".git/", "**/__pycache__/", "**/*.pyc", ".idea/", ".vscode/", "Dockerfile*", "docker-compose*.yml",
                 "*.md", "answer_key.json", "tools/", "docker/", "requirements*.txt", "output/", "logs/"]


def jload(p):
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)


def canon(n):
    return re.sub(r"[-_.]+", "-", str(n)).lower()


# Debian/Ubuntu packages of the CUDA toolkit: an nvidia/cuda base image has them already, pip CUDA wheels replace them
CUDA_DEB = re.compile(r"^(cuda-|libcu|libnpp|libnv|libcudnn|libnccl)")
UBUNTU_PY = {"24.04": "3.12", "22.04": "3.10", "20.04": "3.8"}
# CUDA from NVIDIA's pip wheels: register every folder they put libraries in with the Linux loader, found AFTER the
# install - CUDA 12 wheels use one folder per library (nvidia/cublas/lib ...), CUDA 13 wheels one shared folder
# (nvidia/cu13/lib), cuDNN / NCCL / NVSHMEM keep their own. No fixed list: any layout, any version.
CUDA_REGISTER = ("RUN python3 -c \"import pathlib, site; print('\\n'.join(sorted({str(f.parent) for s in "
                 "site.getsitepackages() for f in pathlib.Path(s, 'nvidia').rglob('*.so*')})))\" \\\n"
                 "    > /etc/ld.so.conf.d/cuda-wheels.conf && ldconfig")


def deb_base(name):
    """libglib2.0-0t64 -> libglib2.0-0 (Debian 13 renamed libraries for the 64-bit time_t transition)"""
    return re.sub(r"t64$", "", name)


# ============================================================================ compose (tiny reader)
def compose_services(path):
    """{service: {"image", "dockerfile", "volumes": [...], "environment": {...}, "command"}} from a simple compose file."""
    out, cur, section = {}, None, None
    in_services = False
    try:
        lines = open(path, encoding="utf-8").read().splitlines()
    except OSError:
        return out
    for raw in lines:
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        ind = len(raw) - len(raw.lstrip())
        line = raw.strip()
        if ind == 0:
            in_services = line.startswith("services:")
            cur = None
            continue
        if not in_services:
            continue
        if ind == 2 and line.endswith(":"):
            cur = line[:-1]
            out[cur] = {"volumes": [], "environment": {}}
            section = None
            continue
        if cur is None:
            continue
        if ind == 4:
            k, _, v = line.partition(":")
            section = k if not v.strip() else None
            v = v.strip().strip('"').strip("'")
            if k in ("image", "container_name", "command", "shm_size", "mac_address") and v:
                out[cur][k] = v
            elif k == "build" and v:
                out[cur]["context"] = v            # build: ./path (short form)
        elif ind >= 6 and section in ("volumes", "environment", "build", "ports"):
            if section == "build":
                k, _, v = line.partition(":")
                if k.strip() in ("dockerfile", "context"):
                    out[cur][k.strip()] = v.strip().strip('"').strip("'")
            elif line.startswith("- "):
                item = line[2:].strip().strip('"').strip("'")
                if section == "environment" and "=" in item:
                    k, v = item.split("=", 1)
                    out[cur]["environment"][k] = v
                else:
                    out[cur].setdefault(section, []).append(item)
            elif section == "environment" and ":" in line:
                k, _, v = line.partition(":")
                out[cur]["environment"][k.strip()] = v.strip().strip('"').strip("'")
    return out


def naive_dockerfile(path):
    info = {"cmd": None, "env": {}, "workdir": "/app", "python": None, "from": None, "found": False}
    try:
        txt = open(path, encoding="utf-8").read()
    except OSError:
        return info
    info["found"] = True
    m = re.search(r"^FROM\s+(\S+)", txt, re.M | re.I)
    if m:
        info["from"] = m.group(1)
    m = re.search(r"^FROM\s+python:(\d+\.\d+)", txt, re.M | re.I)
    if m:
        info["python"] = m.group(1)
    cmds = re.findall(r"^CMD\s+(.+?)\s*$", txt, re.M | re.I)
    if cmds:                                   # the last CMD wins, as in Docker
        c = cmds[-1]
        try:
            info["cmd"] = json.loads(c) if c.startswith("[") else ["sh", "-c", c]
        except ValueError:
            info["cmd"] = ["sh", "-c", c]
        if info["cmd"][:2] == ["sh", "-c"] and re.fullmatch(r"[\w./-]+(\s+[\w./=-]+)*", c):
            info["cmd"] = c.split()            # CMD python api.py: plain words need no shell
    m = re.search(r"^WORKDIR\s+(\S+)", txt, re.M)
    if m:
        info["workdir"] = m.group(1)
    for k, v in re.findall(r"^ENV\s+(\w+)=(\S+)", txt, re.M):
        info["env"][k] = v
    return info


def gpu_used(rt):
    """the traced run really ran on the GPU (torch.cuda True, or onnxruntime with CUDA active)"""
    res = rt.get("resources") or {}
    return any(f.get("result") is True or "GPU used" in str(f.get("docker_implication"))
               for f in rt.get("facts", []) if f["category"] == "gpu_usage") or \
        any("CUDA" in x or "Tensorrt" in x for x in res.get("onnxruntime_ran_on") or [])


# ============================================================================ plan
class Planner:
    def __init__(self, fixture, static, runtime, pkg, dockerfile=None, compose=None, services=None, gpu_base="auto"):
        self.fx = fixture
        self.gpu_base = gpu_base or "auto"
        self.st, self.rt, self.pk = static or {}, runtime or {}, pkg or {}
        self.rec = self.st.get("recommendations", {})
        self.evidence = []
        self.dockerfile_path = dockerfile or os.path.join(fixture, "Dockerfile.naive")
        self.naive = naive_dockerfile(self.dockerfile_path)
        if not self.naive["found"]:
            self.evidence.append(f"no Dockerfile at {self.dockerfile_path}: python version / CMD / WORKDIR from the "
                                 "runtime trace or defaults (pass --dockerfile)")
        if self.naive["python"] is None:
            rt_py = re.match(r"(\d+\.\d+)", str(self.rt.get("python") or ""))
            self.naive["python"] = rt_py.group(1) if rt_py else "3.11"
            self.evidence.append(f"python {self.naive['python']}: " + (
                f"the base image is {self.naive['from']}, not python:X.Y; version from the runtime trace" if rt_py
                else "default (no python:X.Y base image and no runtime trace)"))
        self.check_cmd()
        self.compose_path = compose if compose is not None else os.path.join(fixture, "docker-compose.yml")
        self.services = compose_services(self.compose_path) if self.compose_path else {}
        self.wanted = list(services or [])

    def check_cmd(self):
        """CMD python api.py where api.py is not in the project: the image cannot start - use what really ran."""
        cmd = self.naive["cmd"]
        scripts = [c for c in (cmd or []) if c.endswith(".py")]
        missing = [c for c in scripts if not os.path.isfile(os.path.join(self.fx, c.lstrip("/").replace(
            self.naive["workdir"].strip("/") + "/", "", 1) if c.startswith("/") else c))]
        if cmd and not missing:
            return
        sc = self.rt.get("scenario") or {}
        runs = [x.get("cmd") for x in (sc if isinstance(sc, list) else [sc]) if isinstance(x, dict) and x.get("cmd")]
        ran = runs[0] if runs else None
        if len(runs) > 1:
            self.evidence.append("cmd: the other traced entry point(s) run from the same image with a different "
                                 "command (compose command:): " + "; ".join(" ".join(r) for r in runs[1:]))
        entries = self.st.get("entrypoints") or []
        new = ran if ran else (["python", entries[0]] if entries else None)
        if new and new != cmd:
            why = (f"the Dockerfile CMD {cmd} runs {', '.join(missing)}, which is not in the project" if missing
                   else "the Dockerfile has no CMD")
            self.evidence.append(f"cmd: {why}; using {new} ({'what the traced run started' if ran else 'the entry point X-Ray mapped'})")
            self.naive["cmd"] = new

    def rfacts(self, cat=None):
        return [f for f in self.rt.get("facts", []) if cat is None or f["category"] == cat]

    def pfacts(self, cat=None):
        return [f for f in self.pk.get("facts", []) if cat is None or f["category"] == cat]

    def sfacts(self, cat=None):
        return [f for f in self.st.get("facts", []) if cat is None or f["category"] == cat]

    # ---------------------------------------------------------------- decisions
    def app_services(self):
        if self.wanted:
            unknown = [s for s in self.wanted if s not in self.services]
            if unknown and self.services:
                self.evidence.append(f"services {unknown} are not in {self.compose_path}")
            return [s for s in self.wanted if s in self.services]
        named = [s for s, v in self.services.items()
                 if "naive" in str(v.get("image", "")) or "naive" in str(v.get("dockerfile", ""))]
        if named:
            return named
        # a real compose file: the services built from this project folder
        base = os.path.dirname(os.path.abspath(self.compose_path)) if self.compose_path else ""
        fx = os.path.abspath(self.fx)
        return [s for s, v in self.services.items()
                if v.get("context") and os.path.abspath(os.path.join(base, v["context"])) == fx]

    def gpu_used(self):
        return gpu_used(self.rt)

    def cuda_extras(self):
        """{package: [extras]}: the extras that install NVIDIA's CUDA wheels, read by xray_pkg from each kept package's
        own metadata (onnxruntime-gpu: cuda, cudnn; tensorflow: and-cuda; cupy: ctk). Older package facts: onnxruntime"""
        obs = self.pk.get("observations") or {}
        if "cuda_extras" in obs:
            return {canon(k): v for k, v in obs["cuda_extras"].items()}
        return {"onnxruntime-gpu": ["cuda", "cudnn"]} if self.pins_have("onnxruntime-gpu") else {}

    def pins_have(self, name):
        return any(canon(re.split(r"[\[=<>!~ ]", k)[0]) == canon(name) for k in (self.pk.get("requirements_plan") or {})
                   .get("keep", []))

    def base_image(self):
        """GPU runs: 'nvidia' keeps the naive nvidia/cuda family (runtime, not devel) + Ubuntu's python3 in a venv;
        'pip' uses python:X-slim and takes the CUDA libraries from pip wheels (onnxruntime-gpu[cuda,cudnn] / torch)"""
        if hasattr(self, "_base"):
            return self._base
        py = self.naive["python"]
        if self.gpu_used():
            frm = self.naive.get("from") or ""
            mode = self.gpu_base
            if mode == "auto":
                mode = "nvidia" if frm.startswith("nvidia/cuda") else "pip"
                if mode == "pip":
                    self.evidence.append("GPU base: auto = pip here - Dockerfile.naive already takes CUDA from pip wheels "
                                         f"({frm or 'not nvidia/cuda'}), so auto and pip build the same image. The other "
                                         "variant: --gpu-base nvidia (GPUBASE=nvidia in run-step3-4), then "
                                         "compare-images.bat")
            if mode == "nvidia":
                # the CUDA major the image's CUDA wheels are built for (xray_pkg: -cu12 / -cu13 names, cuda-runtime's version)
                major = (self.pk.get("observations") or {}).get("cuda_major") or 12
                img = frm if frm.startswith("nvidia/cuda") else (
                    "nvidia/cuda:13.0.3-cudnn-runtime-ubuntu24.04" if major >= 13 else
                    "nvidia/cuda:12.8.1-cudnn-runtime-ubuntu24.04")
                why = ("the run used the GPU; same nvidia/cuda image family as Dockerfile.naive (it worked on your GPU)"
                       if frm.startswith("nvidia/cuda") else
                       f"the run used the GPU; --gpu-base nvidia: NVIDIA's CUDA {major} runtime image (the CUDA the "
                       "packages' own CUDA wheels are built for) instead of pip CUDA wheels"
                       + ("; CUDA 13 needs NVIDIA driver 580 or newer and an RTX 20xx / GTX 16xx or newer"
                          if major >= 13 else "; CUDA 12.8 needs NVIDIA driver 570 or newer"))
                if "-devel" in img:
                    img = img.replace("-devel", "-runtime")
                    why += "; 'runtime' instead of 'devel' (no compilers / headers needed to run)"
                elif "-cudnn" not in img and "cudnn" in str(self.pk).lower():
                    self.evidence.append("GPU base: the run loaded cuDNN but the base is not a -cudnn- image - "
                                         "check the FROM line")
                ub = re.search(r"ubuntu(\d\d\.\d\d)", img)
                ub_py = UBUNTU_PY.get(ub.group(1)) if ub else None
                if ub_py and ub_py != py:
                    self.evidence.append(f"python {py} ran, but Ubuntu {ub.group(1)}'s python3 is {ub_py}: if the naive "
                                         "Dockerfile installs Python another way, copy those lines into the new one")
                self._base = {"choice": img, "kind": "nvidia", "why": why + f"; Python {ub_py or py} from Ubuntu in a venv"}
                return self._base
            why = ("the run used the GPU; python:" + py + "-slim + the CUDA libraries from pip wheels")
            ex = self.cuda_extras()
            by_name = sorted(set((self.pk.get("observations") or {}).get("links_cuda") or []) | set(ex))
            if ex:
                why += " (" + ", ".join(f"{n}[{','.join(e)}]" for n, e in sorted(ex.items())) + " installs them)"
            if by_name:
                why += "; their folders are registered with the Linux loader for " + ", ".join(by_name)
                self.evidence.append("GPU base pip: NOT yet proven on a real GPU for " + ", ".join(by_name) + " - the "
                                     "verify run must show the GPU in use" + (" (onnxruntime: CUDAExecutionProvider "
                                     "active)" if any(n.startswith("onnxruntime") for n in by_name) else ""))
            self._base = {"choice": f"python:{py}-slim", "kind": "pip-cuda", "why": why}
            return self._base
        self._base = {"choice": f"python:{py}-slim", "kind": "python",
                "why": f"Python {py} (from Dockerfile.naive); no GPU was used at runtime"
                       + (" (torch.cuda.is_available() returned False, ORT fell back to CPU)" if self.rfacts("gpu_usage")
                          else "") + "; no compilers needed (no package is built from source after the swaps)"}
        return self._base

    def apt(self):
        out = []
        kind = self.base_image().get("kind")
        for f in self.pfacts("system_package_candidate"):
            d = str(f.get("detail"))
            if kind in ("nvidia", "pip-cuda") and CUDA_DEB.match(str(f["subject"])):
                self.evidence.append(f"apt: {f['subject']} left out - " + ("the nvidia/cuda base image has it"
                                     if kind == "nvidia" else "the pip CUDA wheels bring it"))
                continue
            if "needed ONLY because" in d or "not needed after the swap" in d:
                self.evidence.append(f"apt: {f['subject']} left out - {d[:160]}")
                continue
            out.append({"name": deb_base(f["subject"]), "installed_as": f["subject"],
                        "required": f.get("required_at_runtime"), "why": d[:220]})
        return sorted(out, key=lambda x: x["name"])

    def requirements(self):
        plan = self.pk.get("requirements_plan") or {}
        swaps = list(plan.get("swap", []))
        # second opinion: the static map also knows a torch that has no GPU use should come from the CPU index
        gpu = self.gpu_used()
        kept_gpu = []
        if gpu:      # the run used the GPU: a CPU-only torch would break it (even if torch itself barely used CUDA)
            for sw in [x for x in swaps if "download.pytorch.org/whl/cpu" in str(x.get("to"))]:
                swaps.remove(sw)
                kept_gpu.append(re.split(r"\s", sw["from"])[0])
                self.evidence.append(f"requirements: kept {sw['from']} - the run used the GPU, no swap to the CPU torch")
        for sw in (self.rec.get("requirements") or {}).get("swap", []) if not gpu else []:
            m = re.match(r"^(\S+?)==(\S+) -> .*download\.pytorch\.org/whl/cpu", str(sw))
            if m and not any(canon(re.split(r"[=<>!~ ]", x["from"])[0]) == canon(m.group(1)) for x in swaps):
                swaps.append({"from": f"{m.group(1)}=={m.group(2)}", "to": f"{m.group(1)}=={m.group(2)}+cpu "
                              "--index-url https://download.pytorch.org/whl/cpu", "why": str(sw)})
                self.evidence.append(f"requirements: {m.group(1)} CPU swap taken from the static map (the package "
                                     f"facts did not have it - were they made with an older xray_pkg.py?)")
        swapped = {canon(re.split(r"[=<>!~ ]", s["from"])[0]) for s in swaps}
        keep = [k for k in plan.get("keep", []) if canon(re.split(r"[=<>!~ ]", k)[0]) not in swapped]
        out = {"keep": keep, "remove": plan.get("remove", []), "swap": [], "add": plan.get("add", [])}
        extra_index = None
        out["keep"] = kept_gpu + keep
        pins = kept_gpu + [k.split("  ")[0] for k in keep]
        for s in swaps:
            to = s["to"]
            if "download.pytorch.org/whl/cpu" in to:
                name_ver = re.split(r"\s", s["from"])[0]
                pin = name_ver + "+cpu" if "+" not in name_ver else name_ver
                extra_index = "https://download.pytorch.org/whl/cpu"
                out["swap"].append({"from": s["from"], "to": f"{pin} --extra-index-url {extra_index}", "why": s["why"]})
                pins.insert(0, pin)
            else:
                out["swap"].append(s)
                pins.append(to.split()[0])
        for a in out["add"]:
            pins.append(a["name"])
        cu = next((m.group(1) for k in pins for m in [re.search(r"\+cu(\d+)\b", k)] if m), None)
        if cu and not extra_index:   # torch==2.8.0+cu126 only exists on PyTorch's own index
            extra_index = f"https://download.pytorch.org/whl/cu{cu}"
        if self.base_image().get("kind") == "pip-cuda":
            # python:X-slim has no CUDA: a package with extras that install NVIDIA's CUDA wheels gets them
            # (onnxruntime-gpu[cuda,cudnn], tensorflow[and-cuda] ...; torch wheels depend on them already)
            ex = self.cuda_extras()

            def with_extras(k):
                m = re.match(r"^([A-Za-z0-9._-]+)(\[[^\]]*\])?(.*)$", k, re.S)
                if not m or m.group(2) or canon(m.group(1)) not in ex:
                    return k
                return f"{m.group(1)}[{','.join(ex[canon(m.group(1))])}]{m.group(3)}"
            pins = [with_extras(k) for k in pins]
        pins = list(dict.fromkeys(pins))          # the same pin twice (kept + re-added) once
        return out, pins, extra_index

    def source_builds(self, pins):
        """kept packages pip compiles while building the image (no prebuilt wheel): they need a compiler then"""
        names = {canon(re.split(r"[\[=<>!~ ]", k)[0]) for k in pins}
        out = sorted({str(f["subject"]) for f in self.pfacts("packaging")
                      if f.get("mechanism") == "source_build" and canon(f["subject"]) in names})
        if out:
            self.evidence.append("build: " + ", ".join(out) + " are compiled by pip (no prebuilt wheel): gcc is "
                                 "installed for the pip step and removed in the same step")
        return out

    def copy_and_ignore(self):
        copy = list(self.rec.get("copy") or ["src/"])
        ignore = set()
        for p in self.rec.get("dockerignore") or []:
            ignore.add(p)
        for f in self.pfacts("unused_asset") + self.pfacts("duplicate_asset"):
            if f["category"] == "duplicate_asset" and not f.get("same_as"):
                continue            # the copy we keep
            ignore.add(str(f["subject"]))
        # never ignore something the run really read or imported
        used = set()
        for f in self.rfacts():
            for k in ("subject", "resolved_path"):
                v = f.get(k)
                if isinstance(v, str) and not v.startswith("/") and "<" not in v:
                    used.add(v)
        for u in used:
            for ig in list(ignore):
                if ig.rstrip("/") == u or (ig.endswith("/") and u.startswith(ig)):
                    ignore.discard(ig)
                    self.evidence.append(f"dockerignore: kept {ig} - the run used {u}")
        # a copied folder narrows to the files the code uses when everything else in it is ignored
        copy = [c for c in copy if not any(c.rstrip("/") == ig.rstrip("/") for ig in ignore)]
        return copy, sorted(ignore)

    def env(self):
        out = {}
        for e in self.rec.get("env") or []:
            out[e["name"]] = {"name": e["name"], "default_in_code": e.get("default_in_code"),
                              "secret": bool(e.get("secret")), "scenario_value": None}
        for f in self.rfacts("env_var"):
            if f.get("mechanism") == "env_set":
                continue
            e = out.setdefault(f["subject"], {"name": f["subject"], "default_in_code": None, "secret": False,
                                              "scenario_value": None})
            if f.get("value") not in (None, "***"):
                e["scenario_value"] = f["value"]
        return [out[k] for k in sorted(out) if k not in ("HOME", "PATH", "HOSTNAME", "PWD", "PYTHONPATH")]

    def secrets(self):
        out = []
        for s in self.rec.get("secrets") or []:
            if "never imported" in str(s.get("detail")):
                continue
            out.append({"what": f"{s['what']} ({s['where']['file']}:{s['where']['line']})" if s.get("where") else s["what"],
                        "fix": s.get("detail")})
        unread = (self.rt.get("observations") or {}).get("env_set_but_not_read_by_code") or []
        for n in unread:
            out.append({"what": f"{n} is set for the container but the code never reads it",
                        "fix": "the code uses a hardcoded value instead: code change needed before the env var works"})
        return out

    def user_path(self, p):
        """/root/... -> /home/app/... (the image runs as a non-root user)"""
        if isinstance(p, str) and (p == "/root" or p.startswith("/root/")):
            return APP_HOME + p[5:]
        if isinstance(p, str) and p.startswith("~/"):
            return APP_HOME + p[1:]
        return p

    def volumes_and_dirs(self, env):
        vols, dirs, remap = [], set(), {}
        envval = {e["name"]: e.get("scenario_value") for e in env}
        # a directory the app writes below whose location comes from an env var (OUTPUT_ROOT, MODEL_CACHE)
        roots = [f for f in self.rfacts("file_write") if f.get("mechanism") == "write_root" or
                 any(a in envval for a in (f.get("aliases") or [])) and str(f["subject"]).startswith("/") and
                 "/" not in str((f.get("aliases") or [""])[0])]
        roots = [f for f in roots if not str(f["subject"]).startswith("/tmp")]      # caches in /tmp: no volume
        for f in roots:
            p = str(f["subject"])
            name = (f.get("aliases") or [None])[0]
            newp = self.user_path(p)
            if newp != p:
                remap[name or p] = (p, newp)
            if f.get("mechanism") not in ("write_root", "mkdir"):
                # the env var names a FILE the app writes (CSV_OUT=/xout/results.csv): the volume is its folder
                p, newp = os.path.dirname(p), os.path.dirname(newp)
                if not newp or newp == "/" or any(x["container_path"] == newp for x in vols):
                    continue
            vols.append({"container_path": newp, "was": p if newp != p else None, "for_env": name,
                         "why": f.get("detail")})
        for v in self.rec.get("volumes") or []:
            p = str(v).split(" ")[0]
            if p.startswith("~") or p.startswith("/"):
                newp = self.user_path(p)
                if not any(x["container_path"] == newp or x.get("was") == p for x in vols) and not any(
                        newp.startswith(x["container_path"] + "/") or x["container_path"].startswith(newp + "/")
                        for x in vols):
                    name = next((k for k, val in envval.items() if val and self.user_path(val) == newp), None)
                    vols.append({"container_path": newp, "was": p if newp != p else None, "for_env": name,
                                 "why": "written at runtime (static map)"})
            elif p:
                dirs.add(p.rstrip("/"))          # cwd-relative (logs): writable folder in the app dir
        # writable folders from what the run wrote (outside volumes)
        for f in self.rfacts("file_write"):
            p = str(f["subject"])
            if f in roots or p.startswith("/tmp"):
                continue
            if not p.startswith("/"):
                d = p if f.get("mechanism") == "mkdir" else os.path.dirname(p)
                if d:
                    dirs.add(d.split("/")[0])
                continue
            if p.startswith(("/tmp", "/root", "/home")) or any(p == v["container_path"] or p.startswith(
                    v["container_path"] + "/") or (v.get("was") and p.startswith(v["was"])) for v in vols):
                continue
            d = p if f.get("mechanism") == "mkdir" else os.path.dirname(p)
            dirs.add(re.sub(r"/<var>.*$", "", d))
        for w in self.rec.get("writable_dirs") or []:
            p = str(w).split(" ")[0]
            if p.startswith("/") and not p.startswith("/tmp"):
                dirs.add(p)
        dirs = {d for d in dirs if d and not any(d == v["container_path"] or d.startswith(v["container_path"] + "/")
                                                 for v in vols)}
        return vols, sorted(dirs), remap

    def compose_runtime(self):
        cr = dict(self.rec.get("compose_runtime") or {})
        out = {"shm_size": None, "mac_address": None, "ports": [],
               "gpus": "all (docker run --gpus all)" if self.gpu_used() else "not required"}
        shm = next((f for f in self.rfacts("ipc_shared_memory")), None)
        if shm and shm.get("total_bytes"):
            mb = shm["total_bytes"] / 2 ** 20
            need = 64
            while need < mb * 1.5:
                need *= 2
            out["shm_size"] = f"{need}mb" if mb * 1.25 > 64 else None
            out["shm_measured_mb"] = round(mb, 1)
            if out["shm_size"] is None:
                out["shm_note"] = "fits Docker's 64 MB default"
        mac = re.search(r'"([0-9a-f:]{17})"', str(cr.get("mac_address") or ""))
        if mac:
            out["mac_address"] = mac.group(1)
        ports = set()
        for f in self.rfacts("listen_port"):
            port = f.get("port") or str(f["subject"]).rsplit(":", 1)[-1]
            ports.add(f"{port}:{port}")
        for p in cr.get("ports") or []:
            ports.add(f"{p}:{p}" if ":" not in str(p) else str(p))
        out["ports"] = sorted(ports)
        return out

    def code_changes(self, ignore=()):
        out = list(self.rec.get("code_changes_required") or [])
        # a line in a file that is not copied into the image (unused folder, test script) is not a change for it
        def left_out(item):
            fn = str(item).split(":", 1)[0]
            return any(fn == ig.rstrip("/") or (ig.endswith("/") and fn.startswith(ig)) for ig in ignore)
        dropped = [x for x in out if left_out(x)]
        if dropped:
            out = [x for x in out if x not in dropped]
            self.evidence.append(f"code changes: {len(dropped)} left out - their files are not in the image "
                                 f"({', '.join(sorted({str(x).split(':', 1)[0] for x in dropped})[:6])})")
        for f in self.rfacts("listen_port"):
            if str(f["subject"]).startswith("127.0.0.1:"):
                s = f["sites"][0] if f.get("sites") else {}
                msg = f"{s.get('file')}:{s.get('line')}: listens on {f['subject']} (seen at runtime) - unreachable " \
                      f"through a published port; bind 0.0.0.0"
                if not any("127.0.0.1" in x for x in out):
                    out.append(msg)
        for f in self.rfacts("thread_config"):
            if f.get("oversubscribed"):
                out.append(f"thread oversubscription (seen at runtime): {str(f.get('detail'))[:200]}")
        return out

    def optional_changes(self):
        """Not needed to run - but the image gets smaller: a package the code imports and then never uses."""
        out = []
        for f in self.pfacts("packaging"):
            if f.get("mechanism") != "import_never_used":
                continue
            top, gb = f.get("import_name") or f["subject"], (f.get("size_bytes") or 0) / 1e9
            where = ", ".join(f"{s.get('file')}:{s.get('line')}" for s in f.get("sites") or [])
            msg = (f"{where} imports {top} but never uses it: {top} and the {len(f.get('freed') or []) - 1} "
                   f"package(s) only it needs are {gb:.1f} GB of this image. If only Windows needs the import (e.g. "
                   f"to load DLLs): if os.name == \"nt\": import {top} - else delete it; then rebuild the naive image "
                   "and run steps 2-4 again (verify shows the image still works)")
            out.append(msg)
            self.evidence.append("smaller image: " + msg)
        return out

    def plan(self):
        pv = str(self.pk.get("version", "0"))
        if self.pk and tuple(int(x) for x in pv.split(".")) < (0, 1, 1):
            msg = (f"WARNING: the package facts were made with xray_pkg.py {pv}; re-run `xray_pkg.py facts` with "
                   f"0.1.1+ (older versions could hide the CUDA-torch swap)")
            print(msg)
            self.evidence.append(msg)
        env = self.env()
        reqs, pins, extra_index = self.requirements()
        copy, ignore = self.copy_and_ignore()
        vols, dirs, remap = self.volumes_and_dirs(env)
        apps = self.app_services()
        images = {"count": 1 if apps else 0, "services": apps,
                  "why": "all entrypoints share one code base and one set of packages" if len(apps) > 1 else
                  "one app service"}
        return {"tool": "xray-plan", "version": VERSION, "fixture": os.path.basename(os.path.abspath(self.fx)),
                "inputs": {"project": self.fx, "dockerfile": self.dockerfile_path,
                           "compose": self.compose_path or None},
                "images": images, "base_image": self.base_image(), "apt_packages": self.apt(),
                "requirements": reqs, "pip_install": pins, "pip_extra_index": extra_index,
                "build_from_source": self.source_builds(pins),
                "copy": copy, "dockerignore": ignore, "env": env, "secrets": self.secrets(), "volumes": vols,
                "writable_dirs": dirs, "env_remap": {k: v[1] for k, v in remap.items()},
                "compose_runtime": self.compose_runtime(), "code_changes_required": self.code_changes(ignore),
                "code_changes_optional": self.optional_changes(),
                "user": {"name": APP_USER, "uid": APP_UID, "home": APP_HOME},
                "cmd": self.naive["cmd"], "workdir": self.naive["workdir"], "python": self.naive["python"],
                "image_env": {k: v for k, v in self.naive["env"].items() if k == "PYTHONPATH"},
                "evidence": self.evidence}


# ============================================================================ files
def dockerfile(p):
    kind = p["base_image"].get("kind")
    other = {"nvidia": "xray_plan.py --gpu-base pip = python:X-slim + NVIDIA's pip wheels for CUDA (smaller; the verify "
                       "run proves it works) - compare both with compare-images.bat",
             "pip-cuda": "xray_plan.py --gpu-base nvidia = the nvidia/cuda runtime image (CUDA from NVIDIA's apt "
                         "packages; bigger) - compare both with compare-images.bat",
             "python": "none needed: the run used no GPU, so there is no CUDA in this image"}.get(kind, "")
    L = [f"# Generated by xray_plan.py {VERSION} from the static map, the runtime trace and the package scan.",
         "# Every line below has a reason in plan.json. Unlike Dockerfile.naive (decided from the requirements), each",
         "# choice here comes from what the X-Ray run really used. Edit freely.",
         f"# Base: {p['base_image'].get('why', '')}", f"# Other choice: {other}", f"FROM {p['base_image']['choice']}", ""]
    env = {"PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1", **p.get("image_env", {})}
    L.append("ENV " + " ".join(f"{k}={v}" for k, v in env.items()))
    if kind == "nvidia" or p["apt_packages"] or p.get("build_from_source"):
        L.append("# apt asks no questions while building; ARG = only during the build, not in the running container")
        L.append("ARG DEBIAN_FRONTEND=noninteractive")
    if kind == "nvidia":
        # the nvidia/cuda image has CUDA but no Python: Ubuntu's python3 + a venv (Ubuntu blocks pip into its python)
        L += ["", "# Python (not in the nvidia/cuda image) + system packages the run really loaded a library of:"]
        L += [f"#   {a['installed_as']}: {a['why'][:110]}" for a in p["apt_packages"]]
        L.append("RUN apt-get update \\\n && apt-get install -y --no-install-recommends python3 python3-venv"
                 + "".join(" " + a["installed_as"] for a in p["apt_packages"]) + " \\\n && rm -rf /var/lib/apt/lists/*")
        L += ["RUN python3 -m venv /opt/venv", "ENV PATH=/opt/venv/bin:$PATH"]
    elif p["apt_packages"]:
        L += ["", "# system packages python:3.x-slim lacks and the run really loaded a library of:"]
        L += [f"#   {a['installed_as']}: {a['why'][:110]}" for a in p["apt_packages"]]
        L.append("RUN apt-get update \\\n && apt-get install -y --no-install-recommends "
                 + " ".join(a["name"] if kind == "pip-cuda" else a["installed_as"] for a in p["apt_packages"])
                 + " \\\n && rm -rf /var/lib/apt/lists/*")
    L += ["", f"WORKDIR {p['workdir']}", "", "# Python packages: keep / swap / add from the package analyzer "
          "(removed: " + (", ".join(r["name"] for r in p["requirements"]["remove"]) or "none") + ")",
          "# kept = imported in the run, or needed by one that was; pinned to the version that ran (==)"]
    for x in p.get("code_changes_optional") or []:
        L += ["# OPTIONAL (a smaller image):"] + ["#   " + w for w in textwrap.wrap(x, 108)]
    idx = f"--extra-index-url {p['pip_extra_index']} " if p.get("pip_extra_index") else ""
    src = p.get("build_from_source") or []
    tools = ("gcc libc6-dev python3-dev" if kind == "nvidia" else "gcc libc6-dev")
    if src:      # compiled by pip: a compiler only for this step, removed in the same layer (not in the image)
        L.append(f"# {', '.join(src)}: no prebuilt wheel, pip compiles them - gcc only during this step")
        L.append(f"RUN apt-get update && apt-get install -y --no-install-recommends {tools} \\\n"
                 f" && pip install --no-cache-dir {idx}\\\n    " + " \\\n    ".join(p["pip_install"])
                 + f" \\\n && apt-get purge -y --auto-remove {tools} && rm -rf /var/lib/apt/lists/*")
    else:
        L.append(f"RUN pip install --no-cache-dir {idx}\\\n    " + " \\\n    ".join(p["pip_install"]))
    if kind == "pip-cuda":
        L += ["", "# The CUDA libraries come from NVIDIA's pip wheels. A library that opens them by name (onnxruntime,",
              "# tensorflow, cupy ...) needs the Linux loader to know their folders. Found here after the install,",
              "# whatever the layout (CUDA 12 wheels: one folder per library, nvidia/cublas/lib ...; CUDA 13 wheels: one",
              "# shared folder, nvidia/cu13/lib), written to /etc/ld.so.conf.d and registered with ldconfig = a",
              "# permanent LD_LIBRARY_PATH for every program in the image. (torch finds its own wheels without it.)",
              CUDA_REGISTER]
    dirs = [d if d.startswith("/") else f"{p['workdir'].rstrip('/')}/{d}" for d in p["writable_dirs"]]
    vols = [v["container_path"] for v in p["volumes"]]
    mk = sorted(set(dirs + vols))
    L += ["", f"# non-root user; folders the app writes (volumes: {', '.join(vols) or 'none'})",
          ("RUN if id -u ubuntu >/dev/null 2>&1; then userdel -r ubuntu 2>/dev/null; fi \\\n && " if kind == "nvidia"
           else "RUN ")   # Ubuntu 24.04 images already have user 'ubuntu' with uid 1000
          + f"useradd --create-home --uid {p['user']['uid']} {p['user']['name']}"
          + (f" \\\n && mkdir -p {' '.join(mk)} \\\n && chown -R {p['user']['name']}:{p['user']['name']} {' '.join(mk)}"
             if mk else "")]
    L += ["", "# code and data the program uses (the rest is excluded by Dockerfile.dockerignore)"]
    for c in p["copy"]:
        L.append(f"COPY --chown={p['user']['name']}:{p['user']['name']} {c} {c}")
    L += ["", f"USER {p['user']['name']}"]
    L += check_by_hand(p, kind, dirs, vols)
    if p.get("cmd"):
        L.append("CMD " + json.dumps(p["cmd"]))
    return "\n".join(L) + "\n"


def check_by_hand(p, kind, dirs, vols):
    """comments: each choice above, checked by hand inside the built image (Python is asked where things are, so
    the lines fit the layout: venv or not)"""
    gpu = kind in ("nvidia", "pip-cuda")
    L = ["", "# CHECK BY HAND - inside the built image:  docker run --rm -it --entrypoint bash IMAGE" +
         ("   (+ --gpus all)" if gpu else ""),
         '#   python -c "import sys, site; print(sys.executable, sys.version.split()[0], site.getsitepackages())"',
         "#   pip list                                    only the kept packages; the removed ones are not there"]
    if p["apt_packages"]:
        L.append("#   dpkg -l " + " ".join(a["name"] if kind == "pip-cuda" else a["installed_as"] for a in p["apt_packages"]) +
                 "    the system libraries (ii = installed)")
    if p.get("build_from_source"):
        L.append("#   which gcc || echo no compiler               removed again in the same RUN as pip")
    if kind == "pip-cuda":
        L += ["#   cat /etc/ld.so.conf.d/cuda-wheels.conf     the CUDA wheel folders registered above",
              "#   ldconfig -p | grep -E 'libcudnn|libcublas'  the loader finds them by name"]
    elif kind == "nvidia":
        L.append("#   echo $LD_LIBRARY_PATH; ldconfig -p | grep -E 'libcudnn|libcublas'    CUDA from the base image")
    if gpu:
        L.append("#   nvidia-smi                                  the GPU (only with docker run --gpus all)")
    L += [f"#   id                                          the user: {p['user']['name']} (uid {p['user']['uid']}), not root",
          f"#   ls -la {p['workdir']}                                 only the copied code"]
    if dirs or vols:
        d0 = (dirs + vols)[0]
        L.append(f"#   touch {d0.rstrip('/')}/x && rm {d0.rstrip('/')}/x && echo writable    a folder the app writes")
    L.append('#   all at once, from your PC:  docker run --rm' + (" --gpus all" if gpu else "") +
             ' -v "TOOLS_FOLDER:/x:ro" --entrypoint "" IMAGE python /x/xray_look.py --image')
    return L


def dockerignore(p):
    L = ["# Generated by xray_plan.py: files the image does not need (unused, duplicates, junk, deploy files)"]
    L += ALWAYS_IGNORE
    L += ["# found by X-Ray:"] + [x for x in p["dockerignore"] if x not in ALWAYS_IGNORE]
    return "\n".join(L) + "\n"


def slug(fixture):
    """F3_stream_worker -> f3 (the fixtures' image prefix); My_Service -> my-service"""
    if re.match(r"^F\d+_", fixture):
        return fixture.split("_")[0].lower()
    return re.sub(r"[^a-z0-9]+", "-", fixture.lower()).strip("-") or "app"


def compose_override(p, services, out_dir, compose_path, window=None):
    img = slug(p["fixture"]) + "-xray"
    cdir = os.path.dirname(os.path.abspath(compose_path))
    rel = lambda x: os.path.relpath(os.path.abspath(x), cdir).replace(os.sep, "/")
    context = rel(p["inputs"]["project"])
    dockerfile_in_ctx = os.path.relpath(os.path.abspath(os.path.join(out_dir, "Dockerfile")),
                                        p["inputs"]["project"]).replace(os.sep, "/")
    L = ["# Generated by xray_plan.py. Use it ON TOP of the project's compose file, from the compose file's folder:",
         f"#   docker compose -f {os.path.basename(compose_path)} -f {rel(os.path.join(out_dir, 'compose.xray.yml'))} build",
         "# Only the app service(s) change: the planned image, non-root paths, named volumes.", "services:"]
    for s in p["images"]["services"]:
        svc = services.get(s, {})
        L += [f"  {s}:", f"    image: {img}", "    build:", f"      context: {context}",
              f"      dockerfile: {dockerfile_in_ctx}"]
        env = dict(p.get("env_remap") or {})
        if window and window.get("env"):
            env[window["env"]] = window["value"]          # the code's own window switch: a container has no screen
        nwpp = nw_setup(p, out_dir) if no_windows(window) else None
        if nwpp:                                        # no switch of its own: X-Ray's no_windows (cv2 windows off)
            env["NO_WINDOWS"], env["PYTHONPATH"] = "1", nwpp
        if env:
            L.append("    environment:")
            L += [f"      {k}: {v}" for k, v in env.items()]
        vols = []
        changed = False
        for v in svc.get("volumes", []):
            parts = v.split(":")
            if len(parts) >= 2:
                src, tgt = parts[0], parts[1]
                new_t = next((x["container_path"] for x in p["volumes"] if x.get("was") == tgt), tgt)
                if src.startswith("./") and any(x["container_path"] == new_t for x in p["volumes"]):
                    src, changed = f"{slug(p['fixture'])}_{os.path.basename(tgt)}_data", True
                if new_t != tgt:
                    changed = True
                if src == "./src" and tgt.endswith("/src"):
                    changed = True
                    continue      # test the code baked into the image, not a bind mount of the source
                vols.append(f"{src}:{new_t}")
        nwv = f"{rel(os.path.join(out_dir, 'no_windows'))}:{NW_DIR}:ro" if nwpp else None
        if changed:
            L.append("    volumes: !override")
            L += [f"      - {v}" for v in vols + ([nwv] if nwv else [])]
        elif nwv:
            L += ["    volumes:", f"      - {nwv}"]
    new_named = []
    for s in p["images"]["services"]:
        for v in services.get(s, {}).get("volumes", []):
            src, tgt = v.split(":")[0], v.split(":")[1] if ":" in v else ""
            new_t = next((x["container_path"] for x in p["volumes"] if x.get("was") == tgt), tgt)
            if src.startswith("./") and any(x["container_path"] == new_t for x in p["volumes"]):
                new_named.append(f"{slug(p['fixture'])}_{os.path.basename(tgt)}_data")
    if new_named:
        L.append("volumes:")
        L += [f"  {n}:" for n in sorted(set(new_named))]
    return "\n".join(L) + "\n"


ENV_NOISE = {"TEMP", "TMP", "TMPDIR", "HOME", "PATH", "PWD", "HOSTNAME", "PYTHONPATH", "OMP_NUM_THREADS",
             "MKL_NUM_THREADS", "PYTHONUNBUFFERED", "PYTHONDONTWRITEBYTECODE"}


def env_key(path):
    """/home/x/.../recognized_people -> RECOGNIZED_PEOPLE_DIR (the .env name for a host folder)"""
    return re.sub(r"[^A-Z0-9]+", "_", os.path.basename(str(path).rstrip("/")).upper()).strip("_") + "_DIR"


PC_PATH = re.compile(r"^([A-Za-z]:[\\/]|/(home|Users)/)")       # a file / folder of one PC (code default)


def window_of(static, project):
    """the code's own switch for its windows (cv2.imshow): {"env", "value"} from xray_runfile, or None"""
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import xray_runfile
        return xray_runfile.window_switch(static or {}, project)
    except Exception:
        return None


def no_windows(window):
    """OpenCV windows with no env var of the program to switch them: X-Ray's no_windows does it (NO_WINDOWS=1)"""
    try:
        import xray_runfile
        return xray_runfile.no_windows(window)
    except Exception:
        return False


NW_DIR = "/opt/xray-no-windows"


def nw_setup(p, out_dir):
    """copy the tools' no_windows (a sitecustomize.py) next to the compose file; -> the PYTHONPATH to set"""
    src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "xray_no_windows", "sitecustomize.py")
    os.makedirs(os.path.join(out_dir, "no_windows"), exist_ok=True)
    shutil.copyfile(src, os.path.join(out_dir, "no_windows", "sitecustomize.py"))
    pp = (p.get("image_env") or {}).get("PYTHONPATH")
    return NW_DIR + (":" + re.sub(r":?\$\{?PYTHONPATH\}?", "", pp) if pp else "")


def nw_env_lines(window):
    where = ", ".join(f"{c[2]} at {c[0]}:{c[1]}" for c in (window.get("calls") or [])[:2])
    return ["# windows: the code opens OpenCV windows (" + where + ") and a container has no screen.",
            "# NO_WINDOWS=1: X-Ray's no_windows (the folder no_windows, mounted by compose.yml) makes cv2's window calls",
            "# do nothing - no code change, the rest runs as it is. 0 = the code's own behaviour (it needs a screen then).",
            "NO_WINDOWS=1"]


def pc_files_of(static):
    """files of one PC the code names in its config (xray_static: hardcoded_config / pc_file) -> 'path (file:line)'"""
    out = []
    for f in (static or {}).get("facts", []):
        if f.get("category") == "hardcoded_config" and f.get("mechanism") == "pc_file":
            site = next((f"{x.get('file')}:{x.get('line')}" for x in f.get("sites") or [] if x.get("line")), "")
            out.append(f"{str(f['subject']).replace('/', chr(92)) if re.match(r'^[A-Za-z]:/', str(f['subject'])) else f['subject']} ({site})")
    return out


def compose_new(p, out_dir, mac_needed, window=None, pc_files=()):
    """no compose file in the project: a compose.yml for the planned image + .env.example (one place for every
    value that changes per site: env vars, host folders, the licence MAC)"""
    name = slug(p["fixture"])
    svc = re.sub(r"[^a-z0-9_-]+", "-", name) or "app"
    env = [e for e in p["env"] if e["name"] not in ENV_NOISE]
    vols = [(env_key(v["container_path"]), "./" + os.path.basename(v["container_path"].rstrip("/")),
             v["container_path"]) for v in p["volumes"]]
    # inputs the run mounted from this PC (xray_runfile: a code default C:\...\model.onnx -> /xin/model_onnx/...):
    # the same read-only mount here, and the env var gets the path INSIDE the container
    ins, value = [], {}
    for e in env:
        dv, sv = str(e.get("default_in_code") or ""), str(e.get("scenario_value") or "")
        if PC_PATH.match(dv) and sv.startswith("/xin/"):
            host = dv.replace("\\", "/")
            looks_file = "." in host.rsplit("/", 1)[-1]
            hdir, cdir = (host.rsplit("/", 1)[0], sv.rsplit("/", 1)[0]) if looks_file else (host, sv)
            if not any(c == cdir for _k, _h, c in ins):
                ins.append((e["name"] + "_DIR", hdir, cdir))
            value[e["name"]] = sv
        elif sv and any(sv == c or sv.startswith(c.rstrip("/") + "/") for _k, _d, c in vols):
            value[e["name"]] = sv                                   # an output inside a mounted folder
    wd = p["workdir"].rstrip("/")
    dirs = [(env_key(d), "./" + d, wd + "/" + d) for d in p["writable_dirs"] if d not in (".", "") and
            not d.startswith("/")]
    cr = p["compose_runtime"]
    shm = cr.get("shm_size")
    if shm and cr.get("shm_measured_mb"):        # frames between processes grow with cameras / resolution: room x4
        shm = f"{max(256, int(cr['shm_measured_mb'] * 4 // 64 + 1) * 64)}m"
    L = [f"# Generated by xray_plan.py {VERSION}: runs the planned image. Everything that changes per machine / site",
         "# is in .env (copy .env.example to .env and fill it in). Keep .env out of git: it holds passwords.",
         "#   docker compose build      docker compose up -d      docker compose logs -f      docker compose down",
         "services:", f"  {svc}:", f"    image: {name}-slim", "    build:",
         f"      context: {p['inputs']['project']}", f"      dockerfile: {os.path.abspath(os.path.join(out_dir, 'Dockerfile'))}",
         "    restart: unless-stopped", "    env_file: .env"]
    nw = no_windows(window)
    if nw:
        L += ["    environment:", "      # X-Ray's no_windows: its sitecustomize.py runs at Python start (NO_WINDOWS in .env)",
              f"      PYTHONPATH: {nw_setup(p, out_dir)}"]
        dirs = dirs + [("", "./no_windows", NW_DIR + ":ro")]
    if mac_needed:
        L.append('    mac_address: "${MAC_ADDRESS:?set MAC_ADDRESS in .env (the MAC the licence expects)}"')
    if shm:
        L.append(f'    shm_size: "{shm}"')
    if cr.get("ports"):
        L.append("    ports:")
        L += [f'      - "{x}"' for x in cr["ports"]]
    if vols or dirs or ins:
        L.append("    volumes:")
        L += [f"      - ${{{k}:-./{c.rstrip('/').rsplit('/', 1)[-1]}}}:{c}:ro" for k, _h, c in ins]
        L += [f"      - ${{{k}:-{d}}}:{c}" if k else f"      - {d}:{c}" for k, d, c in vols + dirs]
    if str(cr.get("gpus", "")).startswith("all"):
        L += ["    deploy:", "      resources:", "        reservations:", "          devices:",
              "            - driver: nvidia", "              count: all", "              capabilities: [gpu]"]
    E = [f"# .env for {svc} - copy to .env next to compose.yml and fill in. Never commit .env (passwords).", ""]
    if mac_needed:
        E += ["# the licence check reads the network card: the MAC the licence was issued for", "MAC_ADDRESS=", ""]
    if ins:
        E.append("# input folders on this machine, mounted read-only (from the code defaults; / works on Windows too)")
        E += [f"{k}={h}" for k, h, c in ins] + [""]
    if vols or any(k for k, _d, _c in dirs):
        E.append("# folders on this machine (left: host, mounted into the container)")
        E += [f"{k}={d}" for k, d, c in vols + dirs if k] + [""]
    if nw:
        E += nw_env_lines(window) + [""]
    if env:
        E.append("# env vars the code reads")
        for e in env:          # comments on their own line: in an env file "X=1  # c" makes the value "1  # c"
            dv = e.get("default_in_code")
            if window and window.get("env") == e["name"]:
                E += [f"# windows: the code opens them (cv2.imshow) only when {e['name']} is on, and a container has no "
                      f"screen - so off: {window['value']}", f"# (the code's default is {dv}: windows on, only where there "
                      "is a screen)", f"{e['name']}={window['value']}"]
                continue
            if e.get("secret"):
                E += ["# secret: fill it in here only - never in a Dockerfile, compose.yml or a chat", f"{e['name']}="]
            elif e["name"] in value:
                E += [f"# the path inside the container (the code default was {dv})", f"{e['name']}={value[e['name']]}"]
            elif dv is None:
                E += ["# no default in the code: must be set", f"{e['name']}="]
            else:
                E.append(f"{e['name']}={dv}")
    else:
        E.append("# (the code reads no env vars yet: its settings are written in the code - see code_changes_required)")
    if pc_files:
        E += ["", "# the code's config names file(s) of one PC: " + "; ".join(pc_files[:3]),
              "# A container cannot see them. In production the config holds what the server really has (a camera",
              "# address, a path there). To run this compose with such a test file: mount it like the run files do",
              "# (volumes: the file at /xin/pc_1/file.mp4:ro, and the tools' xray_inputs folder at /opt/xray-inputs:ro),",
              "# add /opt/xray-inputs to PYTHONPATH, and here PC_PATHS=C:\\...\\file.mp4=/xin/pc_1/file.mp4 - GUIDE 17b."]
    if window and window.get("calls") and not window.get("env") and (window.get("no_screen") or ("", True))[1] \
            and not nw:
        where = ", ".join(f"{c[2]} at {c[0]}:{c[1]}" for c in window["calls"][:2])
        k = window.get("const")
        if k:
            E += ["", f"# NOTE: the code opens windows ({where}) when {k['name']} is on, and {k['name']} = {k['value']!r} is",
                  f"# written in the code ({k['file']}:{k['line']}) - a container has no screen: read it from an env var",
                  f"# ({k['name']} = os.getenv(\"{k['name']}\", \"1\") == \"1\"), then {k['name']}=0 here."]
        elif window.get("guarded"):
            E += ["", f"# NOTE: the code opens windows ({where}) inside an if that no env var decides (a",
                  "# command-line flag, a config value?) - a container has no screen: switch that setting off."]
        else:
            E += ["", "# NOTE: the code always opens windows (" + where + ") - a container has no screen. Make the",
                  "# window optional in the code (if os.getenv(\"SHOW\", \"1\") == \"1\":)."]
        E += ["#   without a screen: " + x.strip() for x in re.findall(r".{1,100}(?:\s|$)",
                                                                       (window.get("no_screen") or ("?",))[0])]
    return "\n".join(L) + "\n", "\n".join(E) + "\n"


def cmd_plan(a):
    pl = Planner(a.fixture, jload(a.static) if a.static else None, jload(a.runtime) if a.runtime else None,
                 jload(a.pkg) if a.pkg else None, dockerfile=a.dockerfile,
                 compose="" if (a.compose or "").lower() == "none" else a.compose, services=a.service,
                 gpu_base=a.gpu_base)
    p = pl.plan()
    os.makedirs(a.out, exist_ok=True)
    name = os.path.basename(os.path.abspath(a.fixture))
    with open(os.path.join(a.out, "plan.json"), "w", encoding="utf-8") as fh:
        json.dump(p, fh, indent=1, ensure_ascii=False)
    with open(os.path.join(a.out, "Dockerfile"), "w", encoding="utf-8") as fh:
        fh.write(dockerfile(p))
    with open(os.path.join(a.out, "Dockerfile.dockerignore"), "w", encoding="utf-8") as fh:
        fh.write(dockerignore(p))
    window = window_of(jload(a.static) if a.static else None, a.fixture)
    if p["images"]["services"]:
        with open(os.path.join(a.out, "compose.xray.yml"), "w", encoding="utf-8") as fh:
            fh.write(compose_override(p, pl.services, a.out, pl.compose_path, window))
    else:
        mac = bool(pl.sfacts("hardware_identity"))
        comp, envx = compose_new(p, a.out, mac, window, pc_files_of(jload(a.static) if a.static else None))
        with open(os.path.join(a.out, "compose.yml"), "w", encoding="utf-8", newline="\n") as fh:
            fh.write(comp)
        with open(os.path.join(a.out, ".env.example"), "w", encoding="utf-8", newline="\n") as fh:
            fh.write(envx)
        print("  no compose file built this folder: wrote compose.yml + .env.example (production: one .env per site)")
    print(f"xray-plan {VERSION}: {name} -> {a.out}/ (plan.json, Dockerfile, Dockerfile.dockerignore, compose.xray.yml)")
    print(f"  base {p['base_image']['choice']} ({p['base_image']['why']})")
    if window and window.get("env"):
        print(f"  windows: {window['env']}={window['value']} in the compose files (the code's own switch; a container has "
              "no screen)")
    print(f"  GPU: {p['compose_runtime']['gpus']}")
    print(f"  apt: {', '.join(x['installed_as'] for x in p['apt_packages']) or 'none'}")
    print(f"  pip: {' '.join(p['pip_install'])}")
    print(f"  copy: {', '.join(p['copy'])}; volumes: {', '.join(v['container_path'] for v in p['volumes']) or 'none'}; "
          f"writable: {', '.join(p['writable_dirs']) or 'none'}")
    print(f"  code changes needed: {len(p['code_changes_required'])}")
    for e in p["evidence"]:
        if e.startswith(("cmd:", "python ", "GPU base", "no Dockerfile", "services ", "WARNING", "apt: ", "requirements: kept",
                         "build: ", "smaller image: ")):
            print(f"  NOTE: {e}")
    return 0


# ============================================================================ grade
def norm_path(x):
    return str(x).strip().rstrip("/").replace("/root/", APP_HOME + "/").replace("~/", APP_HOME + "/")


def covered(entry, ignore):
    e = entry.rstrip("/")
    for ig in ignore:
        g = ig.rstrip("/")
        if e == g or e.startswith(g + "/") or (g.startswith(e + "/") and entry.endswith("/")):
            return True
    return False


def cmd_grade(a):
    p, key = jload(a.plan), jload(a.key)
    k = key.get("expected_docker_decisions", {})
    rows = []

    def row(section, ok, detail):
        rows.append((section, ok, detail))

    row("base_image", str(k.get("base_image", {}).get("choice", "")).split()[0] == p["base_image"]["choice"].split()[0],
        f"key {k.get('base_image', {}).get('choice')} / plan {p['base_image']['choice']}")
    ka = {deb_base(x["name"]) for x in k.get("apt_packages", [])}
    pa = {x["name"] for x in p["apt_packages"]}
    row("apt_packages", ka == pa, f"both {sorted(ka & pa)}; key only {sorted(ka - pa)}; plan only {sorted(pa - ka)}")
    kr, pr = k.get("requirements", {}), p["requirements"]
    for part in ("remove", "add"):
        kn = {canon(x["name"].split("==")[0]) for x in kr.get(part, [])}
        pn = {canon(x["name"].split("==")[0]) for x in pr.get(part, [])}
        row(f"requirements.{part}", kn == pn, f"key {sorted(kn)} / plan {sorted(pn)}")
    ks = {canon(re.split(r"[=<>! ]", x["from"])[0]) for x in kr.get("swap", [])}
    ps = {canon(re.split(r"[=<>! ]", x["from"])[0]) for x in pr.get("swap", [])}
    kremoved = {canon(x["name"]) for x in kr.get("remove", [])}
    ks_eff = {s for s in ks if s not in kremoved}          # "swap opencv-python -> headless" == "remove opencv-python"
    row("requirements.swap", ks_eff == ps, f"key {sorted(ks)} / plan {sorted(ps)}")
    kk = {canon(re.split(r"[=<>! ]", x)[0]) for x in kr.get("keep", [])}
    pk = {canon(re.split(r"[=<>! ]", x)[0]) for x in pr.get("keep", [])}
    row("requirements.keep", kk <= pk, f"key keeps missing from plan: {sorted(kk - pk)}; plan also keeps "
        f"{sorted(pk - kk)}")
    ign = p["dockerignore"] + ALWAYS_IGNORE
    miss = [x for x in k.get("dockerignore", []) if not covered(x, ign) and not any(
        not covered(x, [c]) for c in [])]
    copied_ok = []
    for x in list(miss):      # not ignored but also not copied into the image = fine
        if not any(x.rstrip("/") == c.rstrip("/") or x.startswith(c.rstrip("/") + "/") for c in p["copy"]):
            miss.remove(x)
            copied_ok.append(x)
    row("dockerignore", not miss, f"key entries still inside the image: {miss}" +
        (f"; not copied at all: {copied_ok}" if copied_ok else ""))
    kc = [c for c in k.get("copy", [])]
    missing_copy = [c for c in kc if not any(c.rstrip("/") == x.rstrip("/") or c.startswith(x.rstrip("/") + "/")
                                             for x in p["copy"])]
    row("copy", not missing_copy, f"key {kc} / plan {p['copy']}")
    ke = {e["name"] for e in k.get("env", [])}
    pe = {e["name"] for e in p["env"]}
    row("env", ke <= pe, f"key vars missing from plan: {sorted(ke - pe)}; plan also has {sorted(pe - ke)}")
    kv = {norm_path(v["container_path"]) for v in k.get("volumes", [])}
    pv = {norm_path(v["container_path"]) for v in p["volumes"]}
    row("volumes", kv == pv, f"key {sorted(kv)} / plan {sorted(pv)} (paths under /root are compared as {APP_HOME})")
    kcr, pcr = k.get("compose_runtime", {}), p["compose_runtime"]
    kshm = kcr.get("shm_size")
    row("shm_size", (kshm is None) == (pcr.get("shm_size") is None),
        f"key {kshm} / plan {pcr.get('shm_size')} (measured {pcr.get('shm_measured_mb')} MB)")
    row("mac_address", kcr.get("mac_address") == pcr.get("mac_address"), f"key {kcr.get('mac_address')} / plan "
        f"{pcr.get('mac_address')}")
    kp = {str(x).split(":")[-1] for x in kcr.get("ports") or []}
    pp = {str(x).split(":")[-1] for x in pcr.get("ports") or []}
    row("ports", kp == pp, f"key {sorted(kp)} / plan {sorted(pp)}")
    row("secrets", len(k.get("secrets", [])) <= len(p["secrets"]), f"key {len(k.get('secrets', []))} / plan "
        f"{len(p['secrets'])}")
    row("code_changes", len(k.get("code_changes_required", [])) <= len(p["code_changes_required"]),
        f"key {len(k.get('code_changes_required', []))} / plan {len(p['code_changes_required'])}")
    ok = sum(1 for r in rows if r[1])
    print(f"# X-Ray plan grade - {key.get('fixture_id')} - {ok}/{len(rows)} decisions agree with the answer key")
    for s, good, d in rows:
        print(f"  {'OK  ' if good else 'DIFF'} {s:<20} {d}")
    rep = a.plan.rsplit(".", 1)[0] + ".grade.json"
    with open(rep, "w", encoding="utf-8") as fh:
        json.dump({"agree": ok, "total": len(rows), "rows": [{"section": s, "agree": g, "detail": d}
                                                              for s, g, d in rows]}, fh, indent=1)
    print(f"-> {rep}")
    return 0


# ============================================================================ verify
VERIFY_CATS = ("local_module", "dynamic_import", "third_party_import", "model_file", "config_file", "data_file_read",
               "file_write", "network_endpoint", "runtime_download", "listen_port", "subprocess_binary",
               "native_library", "os_data", "env_var", "ipc_shared_memory", "multiprocessing")


def cmd_verify(a):
    """Same scenario on the naive image and on the planned image: did the program do the same things?"""
    old, new = jload(a.naive), jload(a.planned)
    # /root -> the app user's home; random temp names (/tmp/tmpab12cd/...) are the same thing in both runs
    home = lambda s: re.sub(r"/tmp/tmp[\w-]{4,}", "/tmp/<tmp>", str(s).replace("/root/", APP_HOME + "/"))

    def index(rt):
        """third-party imports are compared by import name (psycopg2 -> psycopg2-binary is the same import)"""
        out = {}
        for f in rt.get("facts", []):
            if f["category"] in VERIFY_CATS:
                subj = f.get("import_name") or f["subject"] if f["category"] == "third_party_import" else f["subject"]
                out[(f["category"], home(subj))] = f
        for p in (rt.get("observations") or {}).get("packages_loaded", []):
            out.setdefault(("third_party_import", p["import_name"]), {"loaded_only": True})
        return out
    oi, ni = index(old), index(new)
    old_all = {(f["category"], home(f["subject"])) for f in old.get("facts", [])}
    missing = [k for k in oi if k not in ni and not (k[0] == "env_var" and k[1] in ("HOME", "PATH"))
               and not oi[k].get("loaded_only")]
    added = [k for k in ni if k not in oi and not ni[k].get("loaded_only")]
    problems = []
    for f in new.get("facts", []):
        d = str(f.get("detail"))
        if f["category"] == "third_party_import" and f.get("import_failed") and \
                (f["category"], f["subject"]) not in old_all:
            problems.append(f"import failed only in the new image: {f['subject']}")
        if f["category"] == "dangling_reference" and ("dangling_reference", home(f["subject"])) not in old_all:
            problems.append(f"file missing only in the new image: {f['subject']}")
        if f["category"] == "os_data" and "NOT found" in d:
            problems.append(f"{f['subject']}: {d[:120]}")
        if f["category"] == "native_library" and "failed" in d:
            problems.append(f"{f['subject']}: {d[:120]}")
    if gpu_used(old) and not gpu_used(new):
        problems.append("the naive image ran on the GPU, the planned image did NOT (CUDA libraries missing? run "
                        "with --gpus all?)")
    for sc in (new.get("scenario") if isinstance(new.get("scenario"), list) else [new.get("scenario") or {}]):
        if sc.get("driver_exit_code") not in (None, 0):
            problems.append(f"the scenario driver failed (exit {sc.get('driver_exit_code')}) for {sc.get('cmd')}")
        elif sc.get("exit_code") not in (0, None) and not sc.get("stopped_by"):
            problems.append(f"the app exited with code {sc.get('exit_code')} ({sc.get('cmd')})")
    print("# X-Ray verify: planned image vs naive image, same scenario")
    print(f"  facts compared: naive {len(oi)}, planned {len(ni)}")
    print(f"  seen with the naive image but NOT with the planned one ({len(missing)}):")
    for k in sorted(missing):
        print(f"    - {k[0]} {k[1]}")
    lost_imports = [k for k in missing if k[0] in ("local_module", "dynamic_import", "third_party_import")]
    if lost_imports:
        problems.append(f"{len(lost_imports)} import(s) seen only with the naive image: "
                        + ", ".join(k[1] for k in lost_imports))
    print(f"  new with the planned image ({len(added)}):")
    for k in sorted(added):
        print(f"    + {k[0]} {k[1]}")
    print(f"  problems ({len(problems)}):")
    for x in problems:
        print(f"    ! {x}")
    verdict = "PASS" if not problems else "CHECK"
    print(f"VERDICT: {verdict} - " + ("the planned image did everything the scenario needs" if not problems else
                                     "look at the problems above"))
    return 0 if not problems else 1


def main(argv=None):
    for _s in (sys.stdout, sys.stderr):   # Windows consoles / pipes: never crash on tree characters or paths
        if hasattr(_s, "reconfigure"):
            _s.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="X-Ray Phase 4 planner")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--fixture", "--project", dest="fixture", required=True,
                   help="fixture / project folder = the build context (fixtures: has Dockerfile.naive, docker-compose.yml)")
    p.add_argument("--dockerfile", help="the project's current Dockerfile (default: FIXTURE/Dockerfile.naive)")
    p.add_argument("--compose", help="the compose file (default: FIXTURE/docker-compose.yml; 'none' = no compose)")
    p.add_argument("--service", action="append",
                   help="compose service(s) that run this image (default: services built from FIXTURE)")
    p.add_argument("--static")
    p.add_argument("--runtime")
    p.add_argument("--pkg")
    p.add_argument("--out", required=True)
    p.add_argument("--gpu-base", choices=("auto", "nvidia", "pip"), default="auto",
                   help="GPU projects: nvidia = nvidia/cuda runtime image + Python (auto when Dockerfile.naive uses "
                        "nvidia/cuda); pip = python:X-slim + CUDA from pip wheels (smaller, test it with verify)")
    g = sub.add_parser("grade")
    g.add_argument("--plan", required=True)
    g.add_argument("--key", required=True)
    v = sub.add_parser("verify", help="compare the runtime facts of the naive and the planned image")
    v.add_argument("--naive", required=True, help="runtime facts from the naive image (xray_trace.py facts)")
    v.add_argument("--planned", required=True, help="runtime facts from the planned image")
    a = ap.parse_args(argv)
    return {"plan": cmd_plan, "grade": cmd_grade, "verify": cmd_verify}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
