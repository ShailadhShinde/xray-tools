#!/usr/bin/env python3
"""
xray_combine.py - ONE image for several programs that X-Ray planned one by one. Stdlib only, Python 3.8+.

Each program keeps what its own X-Ray run proved it needs. The packages two or three programs share are installed
once, in one version every program's packages accept; each program's code comes from its own verified slim image
(COPY --from), into a folder of its own: /app/NAME. Nothing is read from the project folders again.

Reads, per program, its X-Ray folder runs/NAME (after run-step3-4 and build-slim):
  plan/plan.json        what the slim image has: base, apt and pip packages, folders, the command
  pkg/pkgscan.json      the packages' own requirements (which versions they accept) and sizes
  run-slim.bat / .sh    the verify run of the slim image
Writes:
  OUT/Dockerfile                      the combined image
  OUT/combine.txt                     what is shared, what changed, what could not be combined, sizes, next steps
  runs/NAME/run-combined.bat / .sh    the verify run of each program in the combined image (a copy of run-slim)
  compose.combined.yml                next to each program's compose file: the same service, the combined image

  python xray_combine.py --run runs\\anpr --run runs\\middle --run runs\\toll --image mep-combined --out combined
  python xray_combine.py ... --compose middle=D:\\docker\\middle\\compose.yml      (default: runs\\NAME\\plan\\compose.yml)
"""
import argparse
import json
import os
import re
import subprocess
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import xray_plan  # noqa: E402
from xray_compare import build_meta, closure, parse_req, marker_ok, spec_ok, vkey, Project  # noqa: E402

VERSION = "0.1.0"
canon = xray_plan.canon

# packages that install the same folder: one of them per image. First = kept (it can do what the others do).
SAME_FILES = {
    "onnxruntime": ["onnxruntime-gpu", "onnxruntime"],          # the GPU build runs on the CPU too
    "psycopg2": ["psycopg2-binary", "psycopg2"],                # the same library, prebuilt: no compiler
    "tensorflow": ["tensorflow", "tensorflow-cpu"],
}
OPENCV = {"opencv-python", "opencv-python-headless", "opencv-contrib-python", "opencv-contrib-python-headless"}


def jload(p):
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)


def split_pin(pin):
    """'onnxruntime-gpu[cuda,cudnn]==1.19.2' -> ('onnxruntime-gpu', '[cuda,cudnn]', '1.19.2', '')
    'torch==2.4.1+cpu' -> ('torch', '', '2.4.1', '+cpu')"""
    m = re.match(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[[^\]]*\])?\s*(?:==\s*([^\s;+]+)(\+\S+)?)?", pin)
    if not m:
        return pin.strip(), "", "", ""
    return m.group(1), m.group(2) or "", m.group(3) or "", m.group(4) or ""


def docker_has(image):
    try:
        r = subprocess.run(["docker", "image", "inspect", "--format", "{{.Size}}", image],
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None                 # no docker here: cannot check
    return int(r.stdout.strip() or 0) if r.returncode == 0 else False


# ============================================================================ one program
class Program:
    def __init__(self, run_dir, compose=None):
        self.dir = os.path.abspath(run_dir)
        self.name = os.path.basename(self.dir.rstrip("\\/"))
        if not re.match(r"^[a-z0-9][a-z0-9_.-]*$", self.name):
            sys.exit(f"--run {run_dir}: the folder name {self.name!r} is the program's short name - small letters, "
                     "digits, - and _ only (the name you gave xray_runfile.py --name)")
        pf = os.path.join(self.dir, "plan", "plan.json")
        if not os.path.isfile(pf):
            sys.exit(f"{self.name}: {pf} is missing - run run-step3-4 for {self.name} first (its plan)")
        self.plan = jload(pf)
        sf = os.path.join(self.dir, "pkg", "pkgscan.json")
        self.scan = jload(sf) if os.path.isfile(sf) else None
        self.workdir = (self.plan.get("workdir") or "/app").rstrip("/") or "/app"
        self.cmd = self.plan.get("cmd") or []
        self.dest = "/app/" + self.name
        # the slim image: build-slim tags NAME-slim; the plan's compose.yml calls it after the project folder
        cands = [self.name + "-slim"]
        alt = xray_plan.slug(self.plan.get("fixture") or self.name) + "-slim"
        if alt not in cands:
            cands.append(alt)
        self.image, self.image_size, self.image_note = cands[0], None, ""
        for c in cands:
            s = docker_has(c)
            if s:
                self.image, self.image_size = c, s
                break
            if s is None:
                self.image_note = "docker not reachable here: not checked"
                break
        else:
            self.image_note = "NOT FOUND - run build-slim for " + self.name + " first (" + " or ".join(cands) + ")"
        self.compose = os.path.abspath(compose) if compose else os.path.join(self.dir, "plan", "compose.yml")
        self.moves = {}                 # env file -> {KEY: new value}: paths of the slim image, now /app/NAME/...
        self.pins = {}                  # canonical name -> (name as written, extras, version, local)
        for pin in self.plan.get("pip_install", []):
            n, ex, v, loc = split_pin(pin)
            self.pins[canon(n)] = (n, ex, v, loc)

    @property
    def base(self):
        return self.plan["base_image"]["choice"]

    @property
    def kind(self):
        return self.plan["base_image"].get("kind") or ("nvidia" if "nvidia/cuda" in self.base else "python")

    @property
    def python(self):
        m = re.match(r"^python:(\d+\.\d+)", self.base)
        return m.group(1) if m else str(self.plan.get("python") or "")[:4].rstrip(".")

    def hand_edits(self):
        """lines of plan/Dockerfile that the plan does not make: edits by hand (the combined image has only the plan)"""
        path = os.path.join(self.dir, "plan", "Dockerfile")
        if not os.path.isfile(path):
            return [], []
        with open(path, encoding="utf-8", errors="replace") as fh:
            have = fh.read()
        norm = lambda t: [re.sub(r"\s+", " ", x.strip()) for x in t.splitlines() if x.strip() and not x.strip().startswith("#")]
        h, w = norm(have), norm(xray_plan.dockerfile(self.plan))
        return [x for x in h if x not in w], [x for x in w if x not in h]

    def to_app(self, value):
        """a path of the slim image (/app/...) -> where it is in the combined image (/app/NAME/...)"""
        w = re.escape(self.workdir)
        return re.sub(r"(?<![\w.-])" + w + r"(?=/|$|[\s\"':;,])", self.dest, str(value))


# ============================================================================ base image
def choose_base(progs, forced):
    kinds = {p.kind for p in progs}
    if forced:
        kind = "nvidia" if "nvidia/cuda" in forced else "pip-cuda" if "pip-cuda" in kinds else "python"
        return forced, kind, f"--base {forced} (given): every program must PASS its verify run on it"
    bases = sorted({p.base for p in progs})
    pys = sorted({p.python for p in progs if p.python})
    if "nvidia" in kinds and "pip-cuda" in kinds:
        sys.exit("cannot combine: " + ", ".join(f"{p.name} gets CUDA from {'the nvidia/cuda base' if p.kind == 'nvidia' else 'pip wheels'}"
                 for p in progs if p.kind in ("nvidia", "pip-cuda")) + " - in one image CUDA would be there twice. "
                 "Plan them the same way: GPUBASE=nvidia (or pip) at the top of run-step3-4, run it, build-slim, "
                 "run-slim, then combine again")
    nv = sorted({p.base for p in progs if p.kind == "nvidia"})
    if len(nv) > 1:
        sys.exit("cannot combine: the GPU programs use different nvidia/cuda images (" + ", ".join(nv) + ") - give "
                 "one with --base IMAGE (the newer one, if the driver is new enough) and verify every program on it")
    if len(pys) > 1:
        sys.exit("cannot combine: the programs run on different Pythons (" + ", ".join(
            f"{p.name} {p.python}" for p in progs) + ") - one image has one Python. Make them the same (xray_naive.py "
            "--python X.Y, then the steps again) or give --base python:X.Y-slim and verify every program on it")
    if nv:
        return nv[0], "nvidia", ("the GPU program's base (" + ", ".join(p.name for p in progs if p.kind == "nvidia")
                                 + "); the others run on it too - without --gpus they use the CPU as before")
    if len(bases) == 1:
        return bases[0], ("pip-cuda" if "pip-cuda" in kinds else progs[0].kind), "every program's plan uses it"
    return f"python:{pys[0]}-slim", ("pip-cuda" if "pip-cuda" in kinds else "python"), f"Python {pys[0]} for all"


# ============================================================================ packages
def family_merge(progs, log):
    """packages that write the same folder (cv2/, onnxruntime/ ...): one per image -> rename pins in place"""
    have = defaultdict(dict)        # family -> {canonical name: [programs]}
    for p in progs:
        for c in p.pins:
            if c in OPENCV:
                have["cv2"].setdefault(c, []).append(p)
            for fam, members in SAME_FILES.items():
                if c in members:
                    have[fam].setdefault(c, []).append(p)
    for fam, found in have.items():
        if len(found) < 2:
            continue
        if fam == "cv2":
            gui = any("headless" not in c for c in found)
            contrib = any("contrib" in c for c in found)
            keep = "opencv-" + ("contrib-" if contrib else "") + "python" + ("" if gui else "-headless")
            why = ("a program uses windows (the non-headless build)" if gui else "no program needs windows") + (
                "; one needs the contrib modules" if contrib else "")
        else:
            keep = next(m for m in SAME_FILES[fam] if m in found)
            why = {"onnxruntime": "the GPU build, it runs on the CPU too", "psycopg2": "prebuilt, no compiler needed",
                   "tensorflow": "the full build"}[fam]
        # the version: the kept package's own pin, else the newest of the family
        own = [p.pins[keep][2] for p in found.get(keep, [])]
        ver = sorted(own or [p.pins[c][2] for c, ps in found.items() for p in ps], key=vkey)[-1]
        ex = next((p.pins[keep][1] for p in found.get(keep, []) if p.pins[keep][1]), "")
        for c, ps in found.items():
            for p in ps:
                if c != keep or p.pins[c][2] != ver:
                    log.append((p.name, f"{p.pins[c][0]}=={p.pins[c][2]}", f"{keep}=={ver}",
                                f"one {fam} folder per image: {keep} ({why})"))
                old = p.pins.pop(c)
                p.pins[keep] = (keep, ex or old[1], ver, "")
    return log


def torch_merge(progs, log):
    """torch +cpu in one program, the CUDA build in another: the CUDA build for all (it runs on the CPU too)"""
    for fam in ("torch", "torchvision", "torchaudio"):
        ps = [p for p in progs if fam in p.pins]
        gpu = [p for p in ps if not p.pins[fam][3]]
        if gpu and len(gpu) < len(ps):
            ver = sorted((p.pins[fam][2] for p in gpu), key=vkey)[-1]
            for p in ps:
                if p.pins[fam][3] or p.pins[fam][2] != ver:
                    log.append((p.name, f"{fam}=={p.pins[fam][2]}{p.pins[fam][3]}", f"{fam}=={ver}",
                                "the CUDA build of " + fam + " (another program needs the GPU); it runs on the CPU too"))
                p.pins[fam] = (p.pins[fam][0], p.pins[fam][1], ver, "")
    return log


def unify_all(progs):
    """one version per distribution that every program's packages accept (their Requires-Dist, from the scans)"""
    projects = []
    for p in progs:
        plan = dict(p.plan)
        plan["pip_install"] = [f"{n}=={v}{loc}" for n, _ex, v, loc in p.pins.values() if v]
        projects.append(Project(p.name, p.scan, plan))
    meta = build_meta(projects)
    py = projects[0].py
    choice, notes = {}, {}
    first = [closure(q, meta) for q in projects]
    for _ in range(6):
        clos = [closure(q, meta, choice) for q in projects]
        changed = False
        for n in sorted({d for c in clos for d in c}):
            have = [c[n] for c in clos if n in c]
            if len(set(have)) == 1 and n not in choice:
                choice[n] = have[0]
                changed = True
                continue
            pinned = {q.pins[n].split("+")[0] for q in projects if n in q.pins}
            have += [c[n] for c in first if n in c]          # the versions the programs came with count too
            cands = sorted(set(have), key=lambda v: (v in pinned, vkey(v)), reverse=True)
            picked = None
            for v in cands:
                bad = [b for b in violations_all(n, v, clos, meta, py)]
                if not bad:
                    picked = v
                    break
            if picked is None:
                picked = cands[0]
                who = lambda v: ", ".join(q.name for q, c in zip(projects, first) if c.get(n) == v)
                notes[n] = f"{n}: no version every program's packages accept - " + "; ".join(
                    f"{v} ({who(v)}) is refused by " + ", ".join(sorted({f"{d} {dv} ({n} {spec})" for d, dv, spec in
                                                                     violations_all(n, v, first, meta, py)}))
                    for v in cands)
            if choice.get(n) != picked:
                choice[n] = picked
                changed = True
        if not changed:
            break
    return choice, list(notes.values()), first, [closure(q, meta, choice) for q in projects], projects, meta


def violations_all(name, version, clos, meta, py):
    bad = []
    for c in clos:
        for d, dv in c.items():
            for r in meta.get((d, dv), []):
                dep, spec, marker = parse_req(r)
                if dep == name and spec and marker_ok(marker, py) and not spec_ok(version, spec):
                    bad.append((d, dv, spec))
    return bad


def size_of(n, v, progs):
    sizes = [p.scan["dists"][n].get("size_bytes", 0) for p in progs if p.scan and n in p.scan.get("dists", {})
             and p.scan["dists"][n].get("version") == v]
    if not sizes:
        sizes = [p.scan["dists"][n].get("size_bytes", 0) for p in progs if p.scan and n in p.scan.get("dists", {})]
    return max(sizes) if sizes else 0


# ============================================================================ the combined plan -> Dockerfile
def combined_plan(progs, base, kind, pins, extra_index, base_why=""):
    first = progs[0].plan
    apt, seen = [], set()
    for p in progs:
        for a in p.plan.get("apt_packages", []):
            k = a.get("installed_as") or a.get("name")
            if k not in seen:
                seen.add(k)
                a = dict(a)
                a["why"] = f"{p.name}: " + a.get("why", "")
                apt.append(a)
    vols, vseen = [], set()
    for p in progs:
        for v in p.plan.get("volumes", []):
            if v["container_path"] not in vseen:
                vseen.add(v["container_path"])
                vols.append(v)
    dirs = []
    for p in progs:
        for d in p.plan.get("writable_dirs", []):
            dirs.append(p.to_app(d) if d.startswith("/") else p.name + "/" + d.strip("/"))
    removed = sorted({r["name"] for p in progs for r in p.plan.get("requirements", {}).get("remove", [])})
    return {
        "base_image": {"choice": base, "kind": kind, "why": base_why},
        "apt_packages": apt, "pip_install": pins, "pip_extra_index": extra_index,
        "build_from_source": sorted({s for p in progs for s in (p.plan.get("build_from_source") or [])}),
        "python": first.get("python") or progs[0].python,
        "requirements": {"remove": [{"name": n} for n in removed]},
        "writable_dirs": dirs, "volumes": vols, "user": first.get("user") or {"name": "app", "uid": 1000},
        "workdir": "/app", "copy": [], "image_env": {}, "cmd": None,
    }


def render(progs, plan, image):
    txt = xray_plan.dockerfile(plan)
    u = plan["user"]["name"]
    head = [f"# Generated by xray_combine.py {VERSION}: ONE image for " + ", ".join(p.name for p in progs) + ".",
            "# Each program's packages are installed once, its code comes from its own verified slim image.",
            f"# Build (needs the slim images: " + ", ".join(p.image for p in progs) + "):",
            f"#   docker build -t {image} -f Dockerfile .        (in this folder: nothing in it is copied)",
            "# combine.txt says what is shared and what changed. Edit freely."]
    txt = re.sub(r"^# Generated by xray_plan\.py[^\n]*\n# Every line below[^\n]*\n# choice here[^\n]*\n", "", txt)
    txt = "\n".join(head) + "\n" + txt
    cp = ["# each program's code and data, exactly as its slim image has it (its verify run proved it), in a folder",
          "# of its own. compose sets which program a container runs: working_dir /app/NAME + its command"]
    cp += [f"COPY --from={p.image} --chown={u}:{u} {p.workdir}/ {p.dest}/" for p in progs]
    txt = txt.replace("# code and data the program uses (the rest is excluded by Dockerfile.dockerignore)", "\n".join(cp))
    txt = txt.replace("# Python packages: keep / swap / add from the package analyzer",
                      "# Python packages of all the programs, one version each (combine.txt: which program needs which)")
    txt = re.sub(r"#   ls -la /app\s+only the copied code", "#   ls -la /app    one folder per program: " +
                 " ".join(p.name for p in progs), txt)
    txt += ("# the CMD only says what is inside: compose (or docker run -w /app/NAME IMAGE python ...) says which program runs\n"
            "CMD [\"python\", \"-c\", \"print('this image holds " + ", ".join(p.name for p in progs)
            + " - run one with working_dir /app/NAME and its command (combine.txt)')\"]\n")
    return txt


def env_file_moves(p, path):
    """KEY -> new value, for the KEY=value lines of an env file that name a path of the slim image (/app/...)"""
    out = {}
    if not os.path.isfile(path):
        return out
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", k.strip()) and p.to_app(v) != v:
                out[k.strip()] = p.to_app(v)
    return out


# ============================================================================ run-combined (verify) from run-slim
def run_combined(p, image, ext):
    src = os.path.join(p.dir, "run-slim" + ext)
    if not os.path.isfile(src):
        return None, f"{os.path.basename(src)} not found (xray_runfile.py writes it): verify {p.name} by hand"
    with open(src, encoding="utf-8", errors="replace") as fh:
        t = fh.read()
    bat = ext == ".bat"
    t = t.replace("trace-slim", "trace-combined").replace("out-slim", "out-combined") \
         .replace("step2-slim.log", "step2-combined.log").replace(f"{p.name}-runtime-slim.json", f"{p.name}-runtime-combined.json") \
         .replace("verify.txt", "verify-combined.txt").replace("(planned image)", "(combined image " + image + ")")
    t = t.replace("the traced run on the planned image " + p.name + "-slim + verify",
                  "the traced run of " + p.name + " in the combined image " + image + " + verify")
    # the image, the folder it runs in, X-Ray's root
    lines = t.split("\n")
    out, done_img, done_w = [], False, False
    env_needed = {k: p.to_app(v) for k, v in (p.plan.get("image_env") or {}).items()}
    moved = env_file_moves(p, os.path.join(p.dir, "run.env"))      # -e wins over --env-file
    p.moves["run.env"] = moved
    for k, v in moved.items():
        env_needed.setdefault(k, v)
    for ln in lines:
        s = ln.strip().rstrip("^\\").strip()
        if not done_w and re.match(r"^docker run --rm\b", ln.strip()):
            ln = ln.replace("docker run --rm", f"docker run --rm -w {p.dest}", 1)
            done_w = True
        if not done_img and s == p.image or (not done_img and s in (p.name + "-slim",)):
            ln = ln.replace(s, image, 1)
            done_img = True
            for k, v in list(env_needed.items()):          # the slim image's own ENV: now per program
                pad = re.match(r"^\s*", ln).group(0)
                out.append(f'{pad}-e "{k}={v}"' + (" ^" if bat else " \\"))
                env_needed.pop(k)
        if "xray_trace.py run" in ln:
            ln = re.sub(r"--root\s+" + re.escape(p.workdir) + r"(?=\s)", "--root " + p.dest, ln)
        # a mount into the program's folder: only the container side of each -v
        ln = re.sub(r'(-v\s+"[^"]*?:)' + re.escape(p.workdir) + r'(?=[/":])', lambda m: m.group(1) + p.dest, ln)
        ln = re.sub(r'(-v\s+[^\s"]*?:)' + re.escape(p.workdir) + r'(?=[/:\s]|$)', lambda m: m.group(1) + p.dest, ln)
        if re.search(r"-e\s+\"?PYTHONPATH=", ln):
            ln = re.sub(r"(-e\s+\"?PYTHONPATH=)([^\s\"]*)", lambda m: m.group(1) + p.to_app(m.group(2)), ln)
            img_pp = p.to_app((p.plan.get("image_env") or {}).get("PYTHONPATH", ""))
            if img_pp and img_pp not in ln:
                ln = re.sub(r"(-e\s+\"?PYTHONPATH=[^\s\"]*)", lambda m: m.group(1) + ":" + img_pp, ln)
            env_needed.pop("PYTHONPATH", None)
        out.append(ln)
    if not (done_img and done_w):
        return None, f"run-slim{ext} of {p.name} does not look like X-Ray wrote it: verify {p.name} by hand"
    dst = os.path.join(p.dir, "run-combined" + ext)
    with open(dst, "w", encoding="utf-8", newline="") as fh:
        fh.write(("\r\n" if bat else "\n").join(out))
    return dst, ""


# ============================================================================ compose.combined.yml next to a compose file
def compose_combined(p, image):
    """the program's compose file, its service(s) switched to the combined image - a NEW file next to it, so its
    .env, env_file and ./folders stay where they are"""
    src = p.compose
    if not os.path.isfile(src):
        return None, f"no compose file at {src} - add --compose {p.name}=PATH, or set it by hand (combine.txt)"
    with open(src, encoding="utf-8") as fh:
        lines = fh.read().split("\n")
    names = {p.name + "-slim", p.image, xray_plan.slug(p.plan.get("fixture") or p.name) + "-slim"}
    out, i, changed = [], 0, []
    in_services = False
    while i < len(lines):
        ln = lines[i]
        if re.match(r"^services:\s*$", ln):
            in_services = True
        elif re.match(r"^\S", ln):
            in_services = False
        m = re.match(r"^(  )([A-Za-z0-9_.-]+):\s*$", ln) if in_services else None
        if not m:
            out.append(ln)
            i += 1
            continue
        # one service block: its lines until the next key at the same depth
        j = i + 1
        while j < len(lines) and (lines[j].strip() == "" or lines[j].startswith("   ") or lines[j].startswith("    ")
                                  or lines[j].lstrip().startswith("#") and not re.match(r"^  \S", lines[j])):
            j += 1
        block = lines[i:j]
        img = next((re.match(r"^\s+image:\s*(\S+)", b).group(1).strip("'\"") for b in block if re.match(r"^\s+image:", b)), "")
        builds_plan = any("plan" in b and "Dockerfile" in b for b in block)
        if img.split(":")[0] not in names and not builds_plan:
            out += block
            i = j
            continue
        svc = m.group(2)
        new, k = [ln], 1
        have_wd = any(re.match(r"^    working_dir:", b) for b in block)
        have_cmd = any(re.match(r"^    (command|entrypoint):", b) for b in block)
        env_keys = {re.match(r"^      (?:- )?([A-Za-z_][A-Za-z0-9_]*)[:=]", b).group(1) for b in block
                    if re.match(r"^      (?:- )?([A-Za-z_][A-Za-z0-9_]*)[:=]", b)}
        env_list = any(re.match(r"^      - [A-Za-z_][A-Za-z0-9_]*=", b) for b in block)
        files = [m2.group(1) for b in block for m2 in [re.match(r"^    env_file:\s*['\"]?([^'\"\s#]+)", b)] if m2]
        in_ef = False
        for b in block:
            if re.match(r"^    env_file:\s*$", b):
                in_ef = True
                continue
            if in_ef:
                m2 = re.match(r"^      -\s*(?:path:\s*)?['\"]?([^'\"\s#]+)", b)
                if m2:
                    files.append(m2.group(1))
                elif not re.match(r"^\s*(#|$|        )", b):
                    in_ef = False
        moved = {}
        for f in files:
            moved.update(env_file_moves(p, os.path.join(os.path.dirname(src), f)))
        p.moves.update({f"env_file {f}": env_file_moves(p, os.path.join(os.path.dirname(src), f)) for f in files})
        extra_env = {key: p.to_app(val) for key, val in (p.plan.get("image_env") or {}).items()}
        for key, val in moved.items():
            extra_env.setdefault(key, val)
        if not any(re.match(r"^    image:", b) for b in block):
            block = [block[0], f"    image: {p.name}-slim"] + block[1:]
        while k < len(block):
            b = block[k]
            if re.match(r"^    build:\s*$", b):            # the build of the slim image: not needed any more
                k += 1
                while k < len(block) and (block[k].startswith("      ") or block[k].strip() == ""):
                    k += 1
                continue
            if re.match(r"^    build:\s*\S", b):
                k += 1
                continue
            if re.match(r"^    image:", b):
                new.append(f"    image: {image}")
                if not have_wd:
                    new.append(f"    working_dir: {p.dest}            # this program's folder in the combined image")
                if not have_cmd and p.cmd:
                    new.append(f"    command: {json.dumps(p.cmd)}")
                k += 1
                continue
            b2 = p.to_app(b) if not re.match(r"^\s*#", b) else b
            if re.match(r"^      (?:- )?PYTHONPATH[:=]", b2):
                pp = p.to_app((p.plan.get("image_env") or {}).get("PYTHONPATH", ""))
                if pp and pp not in b2:
                    b2 = b2.rstrip().rstrip("'\"") + ":" + pp + ("\"" if b2.rstrip().endswith("\"") else "")
            new.append(b2)
            if re.match(r"^    environment:\s*$", b):
                for key, val in extra_env.items():
                    if key not in env_keys:
                        new.append(f"      - {key}={val}" if env_list else f"      {key}: {val}          # " + (
                            "was ENV in the slim image" if key in (p.plan.get("image_env") or {}) else
                            "the env file says the slim image's path"))
            k += 1
        if not any(re.match(r"^    environment:", b) for b in block) and extra_env:
            tail = []
            while new and new[-1].strip() == "":       # before the blank lines that end the service
                tail.append(new.pop())
            new.append("    environment:")
            new += [f"      {key}: {val}          # " + ("was ENV in the slim image" if key in (p.plan.get("image_env") or {})
                                                    else "the env file says the slim image's path")
                    for key, val in extra_env.items()]
            new += tail
        out += new
        changed.append(svc)
        i = j
    if not changed:
        return None, (f"{src}: no service uses {' / '.join(sorted(names))} or builds the plan's Dockerfile - set it by "
                      "hand (combine.txt)")
    dst = os.path.join(os.path.dirname(src), "compose.combined.yml")
    head = [f"# Generated by xray_combine.py {VERSION} from {os.path.basename(src)}: service(s) {', '.join(changed)} run "
            f"the combined image {image}", "# (build it first: combine.txt). Start:  docker compose -f compose.combined.yml up -d"]
    with open(dst, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(head + out).rstrip("\n") + "\n")
    return dst, ", ".join(changed)


# ============================================================================ main
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", action="append", required=True, help="a program's X-Ray folder runs/NAME (2 or more)")
    ap.add_argument("--image", default="combined", help="the combined image's name (default: combined)")
    ap.add_argument("--out", default="combined", help="folder for the Dockerfile and combine.txt (default: combined)")
    ap.add_argument("--compose", action="append", default=[], help="NAME=PATH: that program's compose file "
                                                                   "(default: runs/NAME/plan/compose.yml)")
    ap.add_argument("--base", help="the base image, when the plans disagree (every program must PASS on it)")
    a = ap.parse_args(argv)
    if len(a.run) < 2:
        sys.exit("give two or more --run folders")
    comp = {}
    for c in a.compose:
        n, _, path = c.partition("=")
        comp[n.strip()] = path.strip()
    progs = [Program(r, comp.get(os.path.basename(os.path.abspath(r).rstrip("\\/")))) for r in a.run]
    if len({p.name for p in progs}) < len(progs):
        sys.exit("two --run folders have the same name")
    base, kind, base_why = choose_base(progs, a.base)
    before = {p.name: {c: f"{v[2]}{v[3]}" for c, v in p.pins.items()} for p in progs}
    log = family_merge(progs, [])
    log = torch_merge(progs, log)
    notes, clos_b, clos_a = [], None, None
    if all(p.scan for p in progs):
        choice, notes, clos_b, clos_a, projects, meta = unify_all(progs)
        for p, cb in zip(progs, clos_b):
            for n, v in cb.items():
                if choice.get(n) and choice[n] != v:
                    if n in p.pins:
                        log.append((p.name, f"{p.pins[n][0]}=={v}", f"{p.pins[n][0]}=={choice[n]}",
                                    "one version for every program"))
                        p.pins[n] = (p.pins[n][0], p.pins[n][1], choice[n], p.pins[n][3])
                    else:
                        log.append((p.name, f"{n}=={v} (a dependency)", f"{n}=={choice[n]}", "one version for every program"))
        dep_pins = {n: choice[n] for p, cb in zip(progs, clos_b) for n, v in cb.items()
                    if n not in p.pins and choice.get(n) and choice[n] != v}
    else:
        dep_pins = {}
        missing = [p.name for p in progs if not p.scan]
        notes.append("no package scan for " + ", ".join(missing) + " (runs/NAME/pkg/pkgscan.json): versions were "
                     "not checked against what the packages accept - the newest pin is used, pip checks the rest")
        for c in sorted({c for p in progs for c in p.pins}):
            vs = sorted({p.pins[c][2] for p in progs if c in p.pins and p.pins[c][2]}, key=vkey)
            for p in progs:
                if c in p.pins and vs and p.pins[c][2] != vs[-1]:
                    log.append((p.name, f"{p.pins[c][0]}=={p.pins[c][2]}", f"{p.pins[c][0]}=={vs[-1]}", "the newest pin"))
                    p.pins[c] = (p.pins[c][0], p.pins[c][1], vs[-1], p.pins[c][3])
    # the combined pins: every program's own pins (now one version each) + dependencies whose version moved
    merged = {}
    for p in progs:
        for c, (n, ex, v, loc) in p.pins.items():
            old = merged.get(c)
            if old and (old[2], old[3]) != (v, loc):
                notes.append(f"{c}: {old[2]}{old[3]} and {v}{loc} - kept {old[2]}{old[3]}")
                continue
            merged.setdefault(c, (n, ex if not old else (old[1] or ex), v, loc))
    for n, v in sorted(dep_pins.items()):
        merged.setdefault(n, (n, "", v, ""))
    pins = [f"{n}{ex}=={v}{loc}" if v else f"{n}{ex}" for _c, (n, ex, v, loc) in sorted(merged.items())]
    idx = sorted({p.plan.get("pip_extra_index") for p in progs if p.plan.get("pip_extra_index")})
    need_local = any(v[3] for v in merged.values())
    extra = None
    if idx and need_local:
        extra = " --extra-index-url ".join(idx)
    plan = combined_plan(progs, base, kind, pins, extra, base_why)
    os.makedirs(a.out, exist_ok=True)
    df = os.path.join(a.out, "Dockerfile")
    with open(df, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(render(progs, plan, a.image))

    # ---------------------------------------------------------------- the report
    R = [f"X-Ray combine {VERSION} - ONE image for: " + ", ".join(p.name for p in progs), "",
         f"image   {a.image}      Dockerfile: {os.path.abspath(df)}",
         f"base    {base}  ({base_why})", ""]
    R.append("THE PROGRAMS' SLIM IMAGES (each one's code is copied from it)")
    for p in progs:
        R.append(f"  {p.name:<12} {p.image:<28} " + (f"{p.image_size / 1e6:,.0f} MB" if p.image_size else p.image_note)
                 + f"   -> {p.dest}")
    R.append("")
    for p in progs:
        added, gone = p.hand_edits()
        if added or gone:
            R.append(f"HAND EDITS in {p.name}'s plan/Dockerfile - the combined Dockerfile is made from plan.json, so these")
            R.append("  are NOT in it: copy the ones that matter into combined/Dockerfile by hand"
                     + (f" (the plan was made by xray_plan {p.plan.get('version')}, this is {xray_plan.VERSION}: some lines"
                        " may differ only because the tool changed)" if p.plan.get("version") != xray_plan.VERSION else ""))
            R += [f"    + {x}" for x in added[:15]] + [f"    - {x}" for x in gone[:15]]
            R.append("")
    # packages side by side
    allc = sorted({c for p in progs for c in before[p.name]} | set(merged))
    w = max([len(c) for c in allc] + [10]) + 2
    R.append("PYTHON PACKAGES THE PLANS INSTALL (pip)   - = that program does not install it")
    R.append("  " + "package".ljust(w) + "".join(p.name[:12].ljust(14) for p in progs) + "in the image")
    shared = 0
    for c in allc:
        row = [before[p.name].get(c, "-") for p in progs]
        inimg = (merged[c][2] + merged[c][3]) if c in merged else "-  (see CHANGED)"
        users = sum(1 for x in row if x != "-")
        shared += users >= 2
        mark = "" if all(x in ("-", inimg) for x in row) else "   <- changed"
        R.append("  " + c.ljust(w) + "".join(x[:12].ljust(14) for x in row) + inimg + mark)
    R.append(f"  {shared} of {len(allc)} are installed by two or more programs: once in the combined image")
    R.append("")
    if clos_b:
        sep_b = sum(size_of(n, v, progs) for cb in clos_b for n, v in cb.items())
        uni = {}
        for ca in clos_a:
            uni.update(ca)
        com_b = sum(size_of(n, v, progs) for n, v in uni.items())
        R += ["SIZE OF THE PYTHON PACKAGES (from the package scans, unpacked on disk)",
              f"  {len(progs)} separate images: {sep_b / 1e6:,.0f} MB  ({' + '.join(f'{sum(size_of(n, v, progs) for n, v in cb.items()) / 1e6:,.0f}' for cb in clos_b)})",
              f"  one combined image:    {com_b / 1e6:,.0f} MB   -> {max(0, sep_b - com_b) / 1e6:,.0f} MB less",
              "  (the base image is stored once either way: Docker shares identical layers between images)", ""]
    if log:
        R.append("CHANGED - a program gets another package or version than its slim image had (verify proves it still works)")
        for name, frm, to, why in log:
            R.append(f"  {name:<12} {frm}  ->  {to}   ({why})")
        R.append("")
    apts = [(a2.get("installed_as") or a2.get("name"), a2["why"].split(":")[0]) for a2 in plan["apt_packages"]]
    if apts:
        R.append("SYSTEM PACKAGES (apt), for: " + ", ".join(f"{n} ({who})" for n, who in apts))
        R.append("")
    if notes:
        R.append("CHECK - could not be combined cleanly")
        R += ["  " + n for n in notes]
        R += ["  pip stops the build with 'ResolutionImpossible' when two programs need versions that cannot be",
              "  installed together. Then: one of them on a version the other's packages accept (its own X-Ray steps",
              "  again with that pin), or that program keeps its own image.", ""]
    R.append("EACH PROGRAM IN THE COMBINED IMAGE (compose sets these; the slim image had them built in)")
    for p in progs:
        env = {k: p.to_app(v) for k, v in (p.plan.get("image_env") or {}).items()}
        R.append(f"  {p.name:<12} working_dir: {p.dest}   command: {json.dumps(p.cmd)}"
                 + (f"   environment: {', '.join(f'{k}={v}' for k, v in env.items())}" if env else ""))
    R.append("  Every /app path of the slim image is now /app/NAME (volumes, env values): the files below do it.")
    R.append("")
    R.append("VERIFY EACH PROGRAM IN THE COMBINED IMAGE (written next to run-slim: the same run, the combined image)")
    for p in progs:
        for ext in (".bat", ".sh"):
            if os.path.isfile(os.path.join(p.dir, "run-slim" + ext)):
                dst, err = run_combined(p, a.image, ext)
                R.append(f"  {p.name:<12} " + (dst + "  -> verify-combined.txt: VERDICT: PASS" if dst else err))
    R.append("")
    R.append("COMPOSE: each program's compose file, its service switched to the combined image (a new file next to it)")
    for p in progs:
        dst, info = compose_combined(p, a.image)
        R.append(f"  {p.name:<12} " + (f"{dst}  (service: {info})" if dst else info))
    for p in progs:
        for f, mv in p.moves.items():
            if mv:
                R.append(f"  {p.name}: {f} names paths of the slim image - set to the new place (the file stays as it is): "
                         + ", ".join(f"{k}={v}" for k, v in mv.items()))
    R += ["", "NEXT",
          f"  1. build:   cd {'/d ' if os.name == 'nt' else ''}\"{os.path.abspath(a.out)}\"   then   docker build -t {a.image} -f Dockerfile .",
          "  2. verify:  each program's run-combined file (above); every verify-combined.txt must end with VERDICT: PASS",
          "  3. run:     in each program's compose folder:  docker compose -f compose.combined.yml up -d",
          "  4. sizes:   docker system df -v   (SHARED / UNIQUE SIZE per image). The slim images can go once every program",
          "              runs from the combined one:",
          "              docker image rm " + " ".join(p.image for p in progs) + "  - build-slim makes them again)",
          "  A new release of one program: its X-Ray steps (step 1, build-naive, step 2, step 3-4, build-slim,",
          "  run-slim), then this tool again, build, verify. All programs restart when the image changes."]
    rep = os.path.join(a.out, "combine.txt")
    with open(rep, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(R) + "\n")
    print("\n".join(R))
    return 0


if __name__ == "__main__":
    sys.exit(main())
