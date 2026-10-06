#!/usr/bin/env python3
"""
xray_runfile.py - write the Step 2 run files for you (no Docker command typing).

Reads the Step 1 map (xray_static.py output) and writes, into runs/NAME/:
  build-naive.bat      docker build of the project's Dockerfile.naive (if it exists)
  run-step2.bat        the traced run: mounts, -e env vars, --gpus, --gui off, --shm-size, ports ... then
                       facts + merge + the needs report (step2-needs.txt)
  run-step2-cpu.bat    the same run without the GPU, to compare (only for projects that use a GPU)
  run-step3-4.bat      Step 3 (package scan of the naive image) + Step 4 (plan: plan/Dockerfile + .dockerignore)
  build-slim.bat       docker build of plan/Dockerfile -> image NAME-slim, then both image sizes
  run-slim.bat         the Step 2 run again on NAME-slim, then verify (verify.txt: PASS = same behaviour)
Every line is commented. Open them in Notepad, change what you want, then run them. Docker is never started by
this script itself. Standard library only.

  python xray_runfile.py --static runs\\zip\\zip.json --project "C:\\...\\ZIP-main" --name zip
  python xray_runfile.py --static runs/zip/zip.json --project ~/ZIP-main --name zip --shell sh     (Linux / WSL)

How env vars are handled (from the code's os.getenv / os.environ reads):
  default is a file/folder on your PC (C:\\..., D:\\..., /home/me/...) -> its folder is mounted read-only and the
      env var points at the file inside the container
  default is a relative output file the code writes (results.csv)  -> written to runs/NAME/out/
  default "cuda" / "gpu"                                           -> "cpu" in the CPU run
  anything else                                                    -> a commented line you can enable
"""
import argparse
import json
import ntpath
import os
import re
import sys

VERSION = "0.1.4"
HERE = os.path.dirname(os.path.abspath(__file__))
PC_PATH = re.compile(r"^([A-Za-z]:[\\/]|/(home|Users)/)")
GPU_WORDS = {"cuda", "gpu", "cuda:0", "cuda:1"}


def jload(p):
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)


def live(f):
    return f.get("required_at_runtime") != "no"


def safe(name):
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "x"


def split_path(p):
    """dirname / basename for both C:\\a\\b.onnx and /home/a/b.onnx"""
    return (ntpath if re.match(r"^[A-Za-z]:", p) else os.path).split(p)


def python_of(dockerfile):
    """the Python version of the naive image (for the python:X-slim baseline of Step 3)"""
    try:
        txt = open(dockerfile, encoding="utf-8", errors="replace").read() if dockerfile else ""
    except OSError:
        txt = ""
    m = re.search(r"^FROM\s+python:(\d+\.\d+)", txt, re.M | re.I)
    if m:
        return m.group(1)
    m = re.search(r"^FROM\s+\S*ubuntu(\d\d\.\d\d)", txt, re.M | re.I)
    return {"24.04": "3.12", "22.04": "3.10", "20.04": "3.8"}.get(m.group(1) if m else "", "3.12")


def workdir_and_cmd(project, dockerfile):
    wd, cmd = "/app", None
    for cand in ([dockerfile] if dockerfile else []) + [os.path.join(project, "Dockerfile.naive"),
                                                        os.path.join(project, "Dockerfile")]:
        if cand and os.path.isfile(cand):
            txt = open(cand, encoding="utf-8", errors="replace").read()
            m = re.findall(r"^WORKDIR\s+(\S+)", txt, re.M | re.I)
            if m:
                wd = m[-1]
            return wd, cand
    return wd, None


def plan(st, project, name, image, dockerfile, stop_after):
    facts = [f for f in st.get("facts", []) if live(f)]
    by = {}
    for f in facts:
        by.setdefault(f["category"], []).append(f)
    entries = st.get("entrypoints") or []
    wd, dfile = workdir_and_cmd(project, dockerfile)
    p = {"name": name, "image": image or f"{name}-naive", "project": project, "workdir": wd, "dockerfile": dfile,
         "entries": entries, "mounts": [], "env": [], "env_cpu": [], "env_optional": [], "notes": [], "ports": [],
         "gpu": False, "gui": False, "shm": False, "stop_after": stop_after, "out_mount": False,
         "file_mounts": [], "mac": None}
    # files reached through env vars: inputs (read) vs outputs (written)
    via_env = {}
    for c in ("model_file", "data_file_read", "config_file", "file_write"):
        for f in by.get(c, []):
            for e in f.get("path_envs") or []:
                via_env.setdefault(e, set()).add("w" if c == "file_write" else "r")
    writes = {str(f["subject"]) for f in by.get("file_write", [])}
    mounted = {}
    seen = set()
    for f in by.get("env_var", []):
        n = f["subject"]
        if n in seen or n.startswith("<") or f.get("mechanism") in ("env_set", "env_set_default"):
            continue
        seen.add(n)
        d = f.get("default")
        site = next((f"{x.get('file')}:{x.get('line')}" for x in f.get("sites") or [] if x.get("line")), "")
        if d and PC_PATH.match(str(d)) and "w" not in via_env.get(n, set()):
            folder, base = split_path(str(d))
            looks_file = "." in base
            host = folder if looks_file else str(d)
            if host not in mounted:
                mounted[host] = f"/xin/{safe(n)}"
                p["mounts"].append((host, mounted[host], "ro", f"{n} default is on your PC ({site})"))
            p["env"].append((n, mounted[host] + ("/" + base if looks_file else ""), f"was {d}"))
        elif d and (n in via_env and "w" in via_env[n] or str(d) in writes) and not os.path.isabs(str(d)) \
                and not PC_PATH.match(str(d)):
            p["out_mount"] = True
            p["env"].append((n, "/xout/" + split_path(str(d))[1], f"output file; was {d} - kept in runs/{name}/out (out-cpu, out-slim for the other runs)"))
        elif d and PC_PATH.match(str(d)):
            p["out_mount"] = True
            p["env"].append((n, "/xout/" + split_path(str(d))[1], f"output; was {d} - kept in runs/{name}/out"))
        elif d and str(d).lower() in GPU_WORDS:
            p["env_cpu"].append((n, "cpu", f"was {d}: ask for the CPU in the CPU run"))
        elif not d and f.get("required_at_runtime") == "yes" and f.get("mechanism") == "env_required":
            p["env"].append((n, "CHANGE_ME", f"no default in the code, must be set ({site})"))
        else:
            p["env_optional"].append((n, d if d not in (None, "") else "", site))
    # hardcoded paths of one machine: env vars cannot fix them
    for f in by.get("hardcoded_config", []):
        if f.get("mechanism") in ("windows_path", "machine_path"):
            s = next((f"{x.get('file')}:{x.get('line')}" for x in f.get("sites") or [] if x.get("line")), "")
            p["notes"].append(f"code change needed: {f['subject']} is hardcoded ({s}) - the run cannot find it")
    p["gpu"] = any(f.get("mechanism") not in ("env_default",) for f in by.get("gpu_usage", []))
    p["gui"] = bool(by.get("gui_usage"))
    p["shm"] = bool(by.get("ipc_shared_memory"))
    for f in by.get("listen_port", []):
        port = f.get("port") or str(f["subject"]).rsplit(":", 1)[-1]
        if str(port).isdigit() and port not in p["ports"]:
            p["ports"].append(port)
        if str(f["subject"]).startswith(("127.0.0.1:", "localhost:")):
            p["notes"].append(f"{f['subject']}: the server only listens inside its container (code change) - "
                              "requests from your PC will not reach it")
    if p["ports"]:
        p["notes"].append("a server: it runs until --stop-after; send it real requests while it runs")
    for f in by.get("device", []):
        if str(f["subject"]).startswith("/dev/video") or str(f["subject"]).startswith("camera:"):
            p["notes"].append("webcam: on Windows mount a test video and add --camera 0=/data/clip.mp4 "
                              "after 'run' (see the guide)")
    # folders the code writes to that are hardcoded paths of one machine (/home/alice/app/images/...): inside the
    # container they do not exist - mount a TEST folder at exactly that path, so the run can write and you see it
    prefixes = []
    for f in by.get("file_write", []):
        subj = str(f["subject"])
        if f.get("path_envs") or not re.match(r"^/(home|Users)/[^/<]+/", subj):
            continue
        pre = subj.split("<")[0].rstrip("/")
        if "/" in pre and "." in pre.rsplit("/", 1)[1]:
            pre = pre.rsplit("/", 1)[0]
        if pre and pre not in prefixes:
            prefixes.append(pre)
    prefixes = [x for x in prefixes if not any(x != y and x.startswith(y + "/") for y in prefixes)]
    used = set()
    for pre in sorted(prefixes):
        sub = safe(pre.rsplit("/", 1)[-1])
        while sub in used:
            sub += "_"
        used.add(sub)
        p["file_mounts"].append((sub, pre, "the code writes here (hardcoded path) - a test folder is mounted at it"))
    if p["file_mounts"]:   # those hardcoded write paths are handled by the test folders: one note instead of many
        conts = [c for _s, c, _w in p["file_mounts"]]
        p["notes"] = [n for n in p["notes"] if not any(c in n for c in conts)]
        p["notes"].append("the code writes to hardcoded folders (" + ", ".join(conts) + "): for this run a test folder is "
                          "mounted at each one (runs/NAME/files/); in production mount the real folder at the same "
                          "path, or change the code (code-changes.txt)")
    # licence / MAC check: pin the MAC the licence expects
    hw = by.get("hardware_identity", [])
    if hw:
        macs = sorted({str(f["value"]) for f in hw if re.fullmatch(r"(?i)[0-9a-f]{2}(:[0-9a-f]{2}){5}", str(f.get("value") or ""))})
        p["mac"] = macs[0] if macs else compose_mac(project)
        if not p["mac"]:
            p["notes"].append("licence / machine check found but no MAC value in the code or a compose file: "
                              "add --mac-address XX:XX:XX:XX:XX:XX to the docker run line if the licence needs one")
    return p


def compose_mac(project):
    """mac_address: from a compose file in the project folder or up to 3 folders above it"""
    d = project
    for _ in range(4):
        try:
            names = sorted(os.listdir(d))
        except OSError:
            names = []
        for n in names:
            if re.match(r"(docker-)?compose.*\.ya?ml$", n):
                try:
                    txt = open(os.path.join(d, n), encoding="utf-8", errors="replace").read()
                except OSError:
                    continue
                m = re.search(r"mac_address:\s*[\"']?((?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2})", txt)
                if m:
                    return m.group(1)
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return None


# --------------------------------------------------------------------------- code changes (before -> after)
ASSIGN = re.compile(r"^(\s*)([A-Za-z_][A-Za-z0-9_]*)\s*=\s*([rRbBuU]?(\"[^\"]*\"|'[^']*'))\s*(#.*)?$")


def read_line(project, rel, line):
    try:
        with open(os.path.join(project, rel), encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
        return lines, lines[line - 1] if 0 < line <= len(lines) else None
    except (OSError, TypeError):
        return None, None


def suggest(text, kind, what):
    """(new line, env var name, note) for one flagged line; new line None when it has to be done by hand"""
    if re.search(r"os\.(getenv|environ)", text):
        m = re.search(r"os\.(?:getenv|environ\.get)\(\s*[\"']([A-Za-z_0-9]+)", text)
        return None, m.group(1) if m else None, "already read from an env var - no code change, just pass it with -e"
    m = ASSIGN.match(text)
    if m:
        indent, var, lit, _q, comment = m.groups()
        env = var.upper()
        new = f'{indent}{var} = os.getenv("{env}", {lit})' + (f"  {comment}" if comment else "")
        note = ("secret: pass it with -e at run time; once that works, remove the default from the code"
                if kind == "secret" else "the old value stays the default: the program runs exactly as before")
        return new, env, note
    h = re.search(r"host\s*=\s*([\"'](127\.0\.0\.1|localhost)[\"'])", text)
    if h:
        return text.replace(h.group(0), f'host=os.getenv("HOST", {h.group(1)})'), "HOST", \
            "then run with -e HOST=0.0.0.0 so the server is reachable from outside the container"
    return None, None, f"read this value ({what}) from an env var instead of writing it in the code"


def code_changes(st, project):
    items, seen = [], set()
    for f in st.get("facts", []):
        if not live(f):
            continue
        c, mech, subj = f["category"], f.get("mechanism"), str(f["subject"])
        if c == "hardcoded_config" or (c == "secret" and mech in ("hardcoded_assignment", "hardcoded_dsn",
                                                                  "credentials_in_url")) or \
                (c == "listen_port" and subj.startswith(("127.0.0.1:", "localhost:"))):
            for x in (f.get("sites") or [])[:1]:
                key = (x.get("file"), x.get("line"))
                if not x.get("line") or key in seen:
                    continue
                seen.add(key)
                lines, text = read_line(project, x["file"], x["line"])
                if text is None:
                    items.append({"where": f"{x['file']}:{x['line']}", "now": None, "new": None, "env": None,
                                  "note": f"{subj}: open this line and read the value from an env var"})
                    continue
                new, env, note = suggest(text, c, subj)
                if new is None and env is None and lines:
                    # the value is used here but written elsewhere: find  NAME = "C:\\..."  in the same file
                    for j, ln in enumerate(lines, 1):
                        m = ASSIGN.match(ln)
                        if m:
                            val = re.sub(r"^[rRbBuU]?[\"']|[\"']$", "", m.group(3)).replace("\\", "/")
                            if len(val) > 3 and subj.replace("\\", "/").startswith(val):
                                key2 = (x["file"], j)
                                if key2 in seen:
                                    text = None
                                    break
                                seen.add(key2)
                                x = {"file": x["file"], "line": j}
                                text = ln
                                new, env, note = suggest(ln, c, subj)
                                break
                    if text is None:
                        continue
                need_import = new is not None and lines is not None and not any(
                    re.match(r"^\s*(import os\b|from os import)", ln) for ln in lines)
                items.append({"where": f"{x['file']}:{x['line']}", "now": text.strip(), "new": new and new.strip(),
                              "env": env, "note": note + ("; add  import os  at the top of the file" if need_import
                                                          else "")})
    return items


def write_code_changes(items, name, office_hint=True):
    L = [f"CODE CHANGES for {name} (written by xray_runfile.py {VERSION} from the Step 1 map)", "",
         "Each item: WHERE (file:line), NOW (the line today), CHANGE TO (the suggested line), IN DOCKER (how to set it).",
         "The old value stays the default, so the program keeps running exactly as before on your PC.",
         "After changing code, run Step 1 again (xray_static.py + xray_runfile.py) so the run files use the new env vars."]
    if office_hint:
        L += ["Office projects (no code changes allowed): give this list to the code owner instead."]
    L += [""]
    if not items:
        L += ["Nothing found: no hardcoded paths, hosts or secrets in the code the entry point uses."]
    for i, it in enumerate(items, 1):
        L += [f"{i}. WHERE:     {it['where']}"]
        if it["now"]:
            L += [f"   NOW:       {it['now']}"]
        if it["new"]:
            L += [f"   CHANGE TO: {it['new']}"]
        elif it["note"].startswith("already"):
            L += ["   CHANGE TO: nothing - it is already an env var"]
        else:
            L += ["   CHANGE TO: (by hand - see the note)"]
        if it["env"]:
            L += [f"   IN DOCKER: -e {it['env']}=<value>"]
        L += [f"   NOTE:      {it['note']}", ""]
    return "\n".join(L) + "\n"


# --------------------------------------------------------------------------- writers
class Shell:
    def __init__(self, kind):
        self.bat = kind == "bat"
        self.cont = " ^" if self.bat else " \\"
        self.rem = "REM " if self.bat else "# "

    def var(self, n):
        return f"%{n}%" if self.bat else f'"${n}"'

    def q(self, s):
        return f'"{s}"'


def head(sh, xdir, out_dir, what):
    if sh.bat:
        return ["@echo off", "REM Generated by xray_runfile.py " + VERSION + ": " + what + ". Edit freely, then run this file.",
                "setlocal", 'set "X=' + xdir + '"', 'set "RUN=' + out_dir + '"', 'set "PY=' + sys.executable + '"', ""]
    return ["#!/usr/bin/env bash", "# Generated by xray_runfile.py " + VERSION + ": " + what + ". Then: bash THIS_FILE",
            "set -u", 'X="' + xdir + '"', 'RUN="' + out_dir + '"', 'PY="' + sys.executable + '"', ""]


def finish(sh, L, report, msg):
    if sh.bat:
        L += ["echo.", "echo " + msg, 'notepad "%RUN%\\' + report + '"', "endlocal"]
        return "\r\n".join(L) + "\r\n"
    L += ["echo", "echo '" + msg + "'"]
    return "\n".join(L) + "\n"


def write_step34(p, sh, xdir, out_dir):
    """Step 3 (package scan) + Step 4 (plan) in one file"""
    name, X, R, PY = p["name"], ("%X%" if sh.bat else "$X"), ("%RUN%" if sh.bat else "$RUN"), ("%PY%" if sh.bat else "$PY")
    sep = "\\" if sh.bat else "/"
    py = python_of(p["dockerfile"])
    L = head(sh, xdir, out_dir, "Step 3 (packages) + Step 4 (plan)")
    if sh.bat:
        L += ["REM GPUBASE (GPU projects only): auto = the same nvidia/cuda image family as the naive image (it worked on",
              "REM your GPU); pip = python:X-slim + the CUDA libraries from pip (smaller - check it with run-slim + verify)",
              'set "GPUBASE=auto"',
              'if not exist "%RUN%\\' + name + '-runtime.json" (echo Run run-step2 first: ' + name + '-runtime.json is missing & exit /b 1)',
              'mkdir "%RUN%\\pkg" 2>nul']
    else:
        L += ['GPUBASE=auto   # GPU projects: auto = nvidia/cuda family of the naive image; pip = python:X-slim + pip CUDA',
              '[ -f "$RUN/' + name + '-runtime.json" ] || { echo "Run run-step2 first"; exit 1; }',
              'mkdir -p "$RUN/pkg"; chmod 777 "$RUN/pkg"']
    pk = R + sep + "pkg"
    L += ["echo Step 3: what python:" + py + "-slim already has, then the packages inside " + p["image"] + " - a few minutes",
          'docker run --rm -v "' + X + ':/xray:ro" -v "' + pk + ':/xray-out" python:' + py + "-slim python /xray/xray_pkg.py "
          "baseline --image python:" + py + '-slim --out /xray-out/slim.json > "' + R + sep + 'step3.log" 2>&1',
          'docker run --rm --entrypoint "" -v "' + X + ':/xray:ro" -v "' + pk + ':/xray-out" ' + p["image"]
          + " python /xray/xray_pkg.py scan --root " + p["workdir"] + ' --out /xray-out/pkgscan.json >> "' + R + sep
          + 'step3.log" 2>&1',
          '"' + PY + '" "' + X + sep + 'xray_pkg.py" facts --scan "' + pk + sep + 'pkgscan.json" --baseline "' + pk + sep
          + 'slim.json" --static "' + R + sep + name + '.json" --runtime "' + R + sep + name + '-runtime.json" --out "' + R
          + sep + name + '-pkg.json" > "' + R + sep + 'step3.txt" 2>&1',
          "echo Step 4: the plan",
          '"' + PY + '" "' + X + sep + 'xray_plan.py" plan --project "' + p["project"] + '"'
          + (' --dockerfile "' + p["dockerfile"] + '"' if p["dockerfile"] else "") + ' --static "' + R + sep + name
          + '.json" --runtime "' + R + sep + name + '-runtime.json" --pkg "' + R + sep + name + '-pkg.json" --out "' + R
          + sep + 'plan" --gpu-base ' + ("%GPUBASE%" if sh.bat else "$GPUBASE") + ' > "' + R + sep + 'step4.txt" 2>&1']
    if sh.bat:
        L += ['start "" notepad "%RUN%\\step3.txt"']
    return finish(sh, L, "step4.txt", "Done. step3.txt = packages, step4.txt = the plan; the new Dockerfile is in "
                  + "plan" + sep + "Dockerfile. Next: build-slim, then run-slim.")


def write_build_slim(p, sh, out_dir):
    sep = "\\" if sh.bat else "/"
    df = out_dir + sep + "plan" + sep + "Dockerfile"
    nl = "\r\n" if sh.bat else "\n"
    L = (["@echo off", "REM Generated by xray_runfile.py: build the planned (small) image from plan\\Dockerfile",
          "REM plan\\Dockerfile.dockerignore next to it keeps the unused files out"] if sh.bat else
         ["#!/usr/bin/env bash", "# Generated by xray_runfile.py: build the planned (small) image"])
    L += ['docker build -f "' + df + '" -t ' + p["name"] + '-slim "' + p["project"] + '" > "' + out_dir + sep
          + 'build-slim.log" 2>&1',
          "docker images " + p["image"], "docker images " + p["name"] + "-slim"]
    if sh.bat:
        L += ["REM no " + p["name"] + "-slim listed above = the build failed: read build-slim.log"]
    return nl.join(L) + nl


def write_run(p, sh, xdir, out_dir, cpu, slim=False):
    name = p["name"]
    X, R, PY = ("%X%", "%RUN%", "%PY%") if sh.bat else ("$X", "$RUN", "$PY")
    sep = "\\" if sh.bat else "/"
    trace_names = []
    L = head(sh, xdir, out_dir, "the traced run on the " + ("planned image " + name + "-slim + verify" if slim else
                                                              "naive image" + (" without the GPU" if cpu else "")))
    if p["notes"]:
        L += [sh.rem + "CHECK BEFORE RUNNING:"] + [sh.rem + "  - " + n for n in p["notes"]] + [""]
    suffix = "-cpu" if cpu else "-slim" if slim else ""
    image = name + "-slim" if slim else p["image"]
    entries = p["entries"] or ["main.py"]
    envs = list(p["env"]) + (p["env_cpu"] if cpu else [])
    for e in entries:
        stem = safe(os.path.splitext(os.path.basename(e))[0]) if len(entries) > 1 else ""
        tdir = "trace" + suffix + ("-" + stem if stem else "")
        log = "step2" + suffix + ("-" + stem if stem else "") + ".log"
        trace_names.append(tdir)
        # every run gets its own fresh output folders (out, out-cpu, out-slim ...): the outputs can be compared, and
        # the planned image (a non-root user) cannot overwrite files an earlier run wrote as root
        outd, filesd = "out" + suffix, "files" + suffix
        fdirs = [R + sep + filesd + sep + sub for sub, cont, why in p["file_mounts"]]
        fresh = [tdir] + ([outd] if p["out_mount"] else []) + ([filesd] if fdirs else [])
        if sh.bat:
            L += ['if exist "%RUN%\\' + d + '" rmdir /s /q "%RUN%\\' + d + '"' for d in fresh]
            L += ['mkdir "%RUN%\\' + tdir + '" ' + ('"%RUN%\\' + outd + '" ' if p["out_mount"] else "")
                  + " ".join('"' + d + '"' for d in fdirs) + ' 2>nul']
        else:
            mk = '"$RUN/' + tdir + '" ' + ('"$RUN/' + outd + '" ' if p["out_mount"] else "") + " ".join('"' + d + '"' for d in fdirs)
            L += ["rm -rf " + " ".join('"$RUN/' + d + '"' for d in fresh) + "; mkdir -p " + mk + "; chmod 777 " + mk]
        # explanations go above the command: a CMD ^ block cannot contain comments
        L += [sh.rem + "what goes into the container:"]
        L += [sh.rem + "  " + host + "  mounted as  " + cont + "   (" + why + ")" for host, cont, mode, why in p["mounts"]]
        L += [sh.rem + "  runs" + sep + name + sep + filesd + sep + sub + "  mounted as  " + cont + "   (" + why + ")"
              for sub, cont, why in p["file_mounts"]]
        if p["mac"]:
            L += [sh.rem + "  --mac-address " + p["mac"] + "   (licence / machine check: the MAC it expects)"]
        L += [sh.rem + "  " + n + "=" + v + "   (" + why + ")" for n, v, why in envs]
        if p["env_optional"]:
            L += [sh.rem + "other env vars the code reads (add  -e NAME=value  to the command to change one):"]
            L += [sh.rem + "  " + n + "  (code default: " + str(d) + ")  " + site for n, d, site in p["env_optional"]]
        a = ["docker run --rm" + (" --gpus all" if p["gpu"] and not cpu else "") + (" --shm-size 2g" if p["shm"] else "")
             + (" --mac-address " + p["mac"] if p["mac"] else "")
             + "".join(" -p " + str(pt) + ":" + str(pt) for pt in p["ports"])]
        a.append('  -v "' + X + ':/xray:ro" -v "' + R + sep + tdir + ':/xray-out"')
        for host, cont, mode, why in p["mounts"]:
            a.append('  -v "' + host + ":" + cont + ":" + mode + '"')
        if p["out_mount"]:
            a.append('  -v "' + R + sep + outd + ':/xout"')
        for sub, cont, why in p["file_mounts"]:
            a.append('  -v "' + R + sep + filesd + sep + sub + ":" + cont + '"')
        for n, v, why in envs:
            a.append("  -e " + n + "=" + v)
        a.append("  " + image)
        flags = (" --gui off" if p["gui"] else "") + (" --stop-after " + str(p["stop_after"]) if p["stop_after"] else "")
        a.append("  python /xray/xray_trace.py run --out /xray-out --root " + p["workdir"] + flags + " -- python " + e)
        msg = ("Step 2" + (" (CPU)" if cpu else " (planned image)" if slim else "") + ": " + e
               + " - the window stays quiet until it ends. Log: " + log)
        L += ["echo " + msg if sh.bat else "echo '" + msg + "'"]
        # one command over several lines: each line but the last ends with the continuation mark; the log
        # redirect sits on the LAST line (in CMD a ^ also swallows the first character of the next line)
        L += [x + sh.cont for x in a[:-1]] + [a[-1] + ' > "' + R + sep + log + '" 2>&1', ""]
    traces = " ".join('--trace "' + R + sep + t + '"' for t in trace_names)
    pyv = '"' + PY + '"'
    rt = name + "-runtime" + suffix + ".json"
    L += [pyv + ' "' + X + sep + 'xray_trace.py" facts ' + traces + ' --out "' + R + sep + rt + '"']
    if slim:
        L += [pyv + ' "' + X + sep + 'xray_plan.py" verify --naive "' + R + sep + name + '-runtime.json" --planned "' + R
              + sep + rt + '" > "' + R + sep + 'verify.txt"']
        return finish(sh, L, "verify.txt", "Done. verify.txt must end with VERDICT: PASS. Also compare the end of "
                      "step2-slim.log with step2.log.")
    if not cpu:
        L += [pyv + ' "' + X + sep + 'xray_trace.py" merge --static "' + R + sep + name + '.json" --runtime "' + R + sep
              + rt + '" --root "' + p["project"] + '" --out "' + R + sep + name + '-merged.json"',
              pyv + ' "' + X + sep + 'xray_needs.py" "' + R + sep + name + '-merged.json" > "' + R + sep
              + 'step2-needs.txt"']
        report = "step2-needs.txt"
    else:
        L += [pyv + ' "' + X + sep + 'xray_needs.py" "' + R + sep + rt + '" > "' + R + sep + 'step2-cpu-needs.txt"']
        report = "step2-cpu-needs.txt"
    return finish(sh, L, report, "Done. Read " + report + " - WHAT THE RUN USED is at the end - and the end of the log.")


def write_build(p, sh, out_dir):
    d = p["dockerfile"] if p["dockerfile"] and os.path.basename(p["dockerfile"]) == "Dockerfile.naive" else None
    if not d:
        return None
    if sh.bat:
        return ("@echo off\r\nREM Generated by xray_runfile.py: build the naive image (several minutes the first time)\r\n"
                f'docker build -f "{d}" -t {p["image"]} "{p["project"]}" > "{out_dir}\\build.log" 2>&1\r\n'
                f'docker images {p["image"]}\r\nREM nothing listed above = the build failed: read build.log\r\n')
    return ("#!/usr/bin/env bash\n# Generated by xray_runfile.py: build the naive image\n"
            f'docker build -f "{d}" -t {p["image"]} "{p["project"]}" > "{out_dir}/build.log" 2>&1\n'
            f'docker images {p["image"]}\n')


def main(argv=None):
    for _s in (sys.stdout, sys.stderr):
        if hasattr(_s, "reconfigure"):
            _s.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="write the Step 2 run files (docker run + facts + merge) for a project")
    ap.add_argument("--static", required=True, help="the Step 1 map (xray_static.py output)")
    ap.add_argument("--project", required=True, help="the project folder (the one the image is built from)")
    ap.add_argument("--name", required=True, help="short project name (zip, pet ...): runs/NAME/")
    ap.add_argument("--image", help="image to run (default NAME-naive)")
    ap.add_argument("--dockerfile", help="the naive Dockerfile (default PROJECT/Dockerfile.naive or PROJECT/Dockerfile)")
    ap.add_argument("--stop-after", type=int, default=180, help="stop the app after this many seconds (default 180; "
                    "0 = never - only for scripts that end by themselves)")
    ap.add_argument("--shell", choices=("bat", "sh"), default="bat" if os.name == "nt" else "sh",
                    help="bat = Windows CMD (default on Windows), sh = Linux / WSL")
    a = ap.parse_args(argv)
    st = jload(a.static)
    out_dir = os.path.abspath(os.path.join(HERE, "runs", a.name))
    os.makedirs(out_dir, exist_ok=True)
    p = plan(st, os.path.abspath(a.project), a.name, a.image, a.dockerfile, a.stop_after or None)
    sh = Shell(a.shell)
    ext = ".bat" if sh.bat else ".sh"
    files = {}
    b = write_build(p, sh, out_dir)
    if b:
        files["build-naive" + ext] = b
    files["run-step2" + ext] = write_run(p, sh, HERE, out_dir, cpu=False)
    if p["gpu"]:
        files["run-step2-cpu" + ext] = write_run(p, sh, HERE, out_dir, cpu=True)
    files["run-step3-4" + ext] = write_step34(p, sh, HERE, out_dir)
    files["build-slim" + ext] = write_build_slim(p, sh, out_dir)
    files["run-slim" + ext] = write_run(p, sh, HERE, out_dir, cpu=False, slim=True)
    changes = code_changes(st, os.path.abspath(a.project))
    files["code-changes.txt"] = write_code_changes(changes, a.name).replace("\n", "\r\n" if sh.bat else "\n")
    for fn, txt in files.items():
        with open(os.path.join(out_dir, fn), "w", encoding="utf-8", newline="") as fh:
            fh.write(txt)
    print(f"xray_runfile {VERSION}: {a.name} -> {out_dir}")
    for fn in files:
        print(f"  {fn}")
    print(f"  image {p['image']}; entry {', '.join(p['entries']) or '?'}; GPU {'yes' if p['gpu'] else 'no'}; "
          f"window {'yes (--gui off)' if p['gui'] else 'no'}; ports {', '.join(p['ports']) or 'none'}")
    for host, cont, mode, why in p["mounts"]:
        print(f"  mount {host} -> {cont}")
    for n, v, why in p["env"]:
        print(f"  env   {n}={v}   ({why})")
    if p["gpu"]:
        for n, v, why in p["env_cpu"]:
            print(f"  env   {n}={v}   (CPU run only; {why})")
    for sub, cont, why in p["file_mounts"]:
        print(f"  test folder runs/{a.name}/files/{sub} -> {cont}   (hardcoded write path)")
    if p["mac"]:
        print(f"  --mac-address {p['mac']}   (licence / machine check)")
    for n in p["notes"]:
        print(f"  CHECK {n}")
    print(f"  code changes suggested: {len(changes)} (see code-changes.txt)")
    if not p["dockerfile"]:
        print("  NOTE: no Dockerfile.naive / Dockerfile in the project - build the image first (see the guide)")
    print("First read code-changes.txt. Then open the run files in Notepad if you want, and run them in this order: "
          + ", ".join(fn for fn in files if not fn.endswith(".txt")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
