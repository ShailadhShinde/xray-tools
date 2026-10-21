#!/usr/bin/env python3
"""
xray_runfile.py - write the Step 2 run files for you (no Docker command typing).

Reads the Step 1 map (xray_static.py output) and writes, into runs/NAME/:
  build-naive.bat      docker build of the project's Dockerfile.naive (if it exists)
  run-naive.bat        the naive image ON ITS OWN, no X-Ray: does it start and do its work? (RUN_SECONDS, then Ctrl+C)
  run-step2.bat        the traced run: mounts, -e env vars, --gpus, --gui off, --shm-size, ports ... then
                       facts + merge + the needs report (step2-needs.txt)
  run-step2-cpu.bat    the same run without the GPU, to compare (only for projects that use a GPU)
  run-step3-4.bat      Step 3 (package scan of the naive image) + Step 4 (plan: plan/Dockerfile + .dockerignore)
  build-slim.bat       docker build of plan/Dockerfile -> image NAME-slim, then both image sizes
  run-slim.bat         the Step 2 run again on NAME-slim, then verify (verify.txt: PASS = same behaviour)
  run-explore.bat      runs the app with NO time limit (video files loop), container named NAME-explore: look
                       inside with  docker exec -it NAME-explore bash ; stop with  docker stop -t 60 NAME-explore ;
                       X-Ray's report of the same run is then in runs/NAME/explore/ to compare with what you saw
  run-load.bat A       a load test (no line counting, GPU logged every 2 s, the app's own log folders kept) into
                       runs/NAME/load-A/; run it again with B, C ... after changing cameras / solutions
  compare-images.bat   two images side by side (default NAME-naive against NAME-slim, or any two): sizes, the apt /
                       pip packages only one has, where each gets its CUDA libraries, what each X-Ray run used
  videos.bat [FOLDER]  can the image read your videos? each one is opened with the image's own OpenCV; the ones it
                       cannot read (AV1 ...) are converted to H.264 into FOLDER\\h264 (xray_videos.py)
Every line is commented. Open them in Notepad, change what you want, then run them. Docker is never started by
this script itself. Standard library only.

Settings at the top of every X-Ray run file (change them in Notepad):
  RUN_SECONDS  how long the program runs, then it gets Ctrl+C (0 = until it ends / you stop it); load 300, explore 0
  LOOP         1 = a video FILE the program reads starts again when it ends (the program keeps working)
  VIDEOS       a folder of videos: the program plays them one after the other instead of its own video file
               (with LOOP=1 the list starts again). Cameras and streams are never touched.

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
import posixpath
import os
import re
import sys

try:                                  # same folder: the folder constants (NAME = "/abs/path") of the project
    from xray_needs import path_constants, const_of
except ImportError:                   # pragma: no cover - xray_needs.py missing next to this file
    def path_constants(root):
        return []

    def const_of(path, consts, outer=False):
        return None

VERSION = "0.2.9"
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


def image_pythonpath(dockerfile):
    """the PYTHONPATH the image sets itself (ENV in its Dockerfile): kept when a run puts a folder in front"""
    try:
        txt = open(dockerfile, encoding="utf-8", errors="replace").read() if dockerfile else ""
    except OSError:
        return ""
    m = re.findall(r"^\s*ENV\s+(?:.*\s)?PYTHONPATH[=\s]\s*\"?([^\"\s]+)", txt, re.M | re.I)
    return re.sub(r":?\$\{?PYTHONPATH\}?", "", m[-1]) if m else ""


NW_DIR = "/opt/xray-no-windows"       # where the tools' xray_no_windows folder is mounted


def no_windows(w):
    """OpenCV windows, and no env var of the program switches them: X-Ray's no_windows turns them off without a code
    change (NO_WINDOWS=1 + the folder on PYTHONPATH). Only for cv2 - other GUI libraries need the code's own switch"""
    calls = (w or {}).get("calls") or []
    return bool(calls) and not w.get("env") and (w.get("no_screen") or no_screen(calls))[1] and \
        all(str(c[2]).startswith("cv2.") for c in calls)


IN_DIR = "/opt/xray-inputs"           # where the tools' xray_inputs folder is mounted


def adapt_flags(p, X, sep, windows):
    """the tools' sitecustomize folders a run needs, in one PYTHONPATH: xray_no_windows (OpenCV windows off) and
    xray_inputs (SOURCE at the top of the run file, and files the code names by their PC path) - always there, so
    SOURCE works whenever you set it; with nothing to do it changes nothing"""
    dirs = ([("xray_no_windows", NW_DIR)] if windows else []) + [("xray_inputs", IN_DIR)]
    pp = ":".join(c for _h, c in dirs) + (":" + p["pythonpath"] if p.get("pythonpath") else "")
    return ["  " + " ".join('-v "' + X + sep + h + ":" + c + ':ro"' for h, c in dirs) + " -e PYTHONPATH=" + pp
            + (" -e NO_WINDOWS=1" if windows else "")]


# --------------------------------------------------------------------------- windows (cv2.imshow) and their switch
OFF_TRY = ["0", "1", "false", "true", "no", "yes", "off", "on", ""]
# what a program does when it opens a window in a container (no screen), per GUI library: matched against the window
# calls / GUI imports X-Ray found
NO_SCREEN = [
    (r"^cv2\.", "OpenCV: 'Could not load the Qt platform plugin \"xcb\"', then it aborts (exit code 134 or 139)"),
    (r"^(PyQt\d|PySide\d)", "Qt: 'Could not load the Qt platform plugin \"xcb\"', then it aborts (exit code 134)"),
    (r"^(tkinter|Tkinter|customtkinter)", "tkinter: '_tkinter.TclError: no display name and no $DISPLAY environment "
                                           "variable'"),
    (r"^pygame", "pygame: 'pygame.error: No available video device'"),
    (r"^wx", "wxPython: 'Unable to access the X Display, is $DISPLAY set properly?'"),
    (r"^(kivy|pyglet)", "an error at start: there is no display to open a window on"),
    (r"^matplotlib", "matplotlib: no error - plt.show() shows nothing (at most a 'non-interactive' warning)"),
]


def no_screen(calls):
    """(what each GUI library does without a screen, does any of them stop the program?) for these window calls"""
    out = []
    for c in calls or []:
        what = c[2] if isinstance(c, (list, tuple)) else str(c)
        for rx, msg in NO_SCREEN:
            if re.match(rx, what) and msg not in out:
                out.append(msg)
    out = out or ["an error: a container has no screen to open a window on"]
    return "; ".join(out), not all(m.startswith("matplotlib") for m in out)


class _Env:
    """a stand-in for os / os.environ while trying values: every env var NAME reads `value`, the others their default"""
    def __init__(self, name, value):
        self.name, self.value = name, value
        self.environ = self

    def getenv(self, key, default=None):
        return self.value if key == self.name else default

    def get(self, key, default=None):
        return self.getenv(key, default)

    def __getitem__(self, key):
        if key == self.name:
            return self.value
        raise KeyError(key)

    def __contains__(self, key):
        return key == self.name


def _env_reads(node):
    """env var names read in an expression: os.getenv("X"), os.environ.get("X"), os.environ["X"], getenv("X")"""
    import ast
    out = []
    for n in ast.walk(node):
        f = getattr(n, "func", None)
        environ_get = isinstance(f, ast.Attribute) and f.attr == "get" and (
            (isinstance(f.value, ast.Attribute) and f.value.attr == "environ") or
            (isinstance(f.value, ast.Name) and f.value.id == "environ"))
        if isinstance(n, ast.Call) and n.args and isinstance(n.args[0], ast.Constant) and \
                isinstance(n.args[0].value, str) and (
                environ_get or (isinstance(f, ast.Attribute) and f.attr == "getenv") or
                (isinstance(f, ast.Name) and f.id == "getenv")):
            out.append(n.args[0].value)
        elif isinstance(n, ast.Subscript) and isinstance(n.value, ast.Attribute) and n.value.attr == "environ":
            k = n.slice if not isinstance(n.slice, ast.Index) else n.slice.value     # Python 3.8: ast.Index
            if isinstance(k, ast.Constant) and isinstance(k.value, str):
                out.append(k.value)
    return out


def _truth(node, known, env):
    """True / False / None (unknown) for an if-condition, with the names in `known` and one env var set"""
    import ast
    if isinstance(node, ast.BoolOp):
        vals = [_truth(v, known, env) for v in node.values]
        if isinstance(node.op, ast.And):
            return False if False in vals else True if all(v is True for v in vals) else None
        return True if True in vals else False if all(v is False for v in vals) else None
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        v = _truth(node.operand, known, env)
        return None if v is None else not v
    try:
        code = compile(ast.Expression(body=node), "<if>", "eval")
        g = {"__builtins__": {"int": int, "bool": bool, "str": str, "float": float, "len": len}, "os": env,
             "getenv": env.getenv, "environ": env}
        return bool(eval(code, g, dict(known)))      # only expressions from the project's own if-conditions
    except Exception:
        return None


def window_switch(st, project):
    """the env var setting that turns the program's windows (cv2.imshow ...) off, found in its code: the `if` around
    each window call, and the env var that condition comes from (SHOW = os.getenv("SHOW", "1") == "1" ... if SHOW:).
    -> {"env", "value", "where", "calls"}; "env" None = a window call with no such switch"""
    import ast
    gui = [f for f in st.get("facts", []) if f.get("category") == "gui_usage" and live(f)]
    gui = [f for f in gui if f.get("mechanism") != "gui_import"] or gui     # the calls, not the `import tkinter` line
    calls = [(x.get("file"), x.get("line"), str(f["subject"])) for f in gui for x in (f.get("sites") or [])[:1]
             if x.get("line")]
    # the calls that OPEN a window (cv2.imshow, plt.show, tkinter.Tk, pygame.display.set_mode, QApplication ...)
    calls = [c for c in calls if re.search(r"imshow|namedWindow|\.show$|show\(|\.Tk$|set_mode|QApplication|\.App$|"
                                           r"mainloop|MainLoop", c[2])] or calls
    if not calls:
        return None
    files = {c[0] for c in calls} | {x.get("file") for f in st.get("facts", []) if f.get("category") == "env_var"
                                      for x in (f.get("sites") or []) if x.get("file")}
    trees, assigns = {}, {}            # variable name -> (env var, expression of its value)
    for rel in files:
        try:
            with open(os.path.join(project, rel), encoding="utf-8", errors="replace") as fh:
                trees[rel] = ast.parse(fh.read())
        except (OSError, SyntaxError, ValueError, TypeError):
            continue
        for n in ast.walk(trees[rel]):
            if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
                e = _env_reads(n.value)
                if len(e) == 1:
                    assigns[n.targets[0].id] = (e[0], n.value)
    guards = []                        # per window call: the if-conditions it sits in (test, in the if-branch?)
    iftests = []                       # per window call: the tests of the ifs around it (not loops, not `if True`)
    for rel, line, what in calls:
        tree = trees.get(rel)
        if tree is None:
            return {"env": None, "calls": calls, "no_screen": no_screen(calls)}
        parents = {c: n for n in ast.walk(tree) for c in ast.iter_child_nodes(n)}
        call = next((n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n, "lineno", 0) == line), None)
        g, ifs, node = [], [], call
        while node in parents:
            par = parents[node]
            if isinstance(par, (ast.If, ast.While)) and node in par.body:
                g.append((par.test, True))
            elif isinstance(par, ast.If) and node in par.orelse:
                g.append((par.test, False))
            if isinstance(par, ast.If) and not isinstance(par.test, ast.Constant) and node in par.body + par.orelse:
                ifs.append((rel, par.test))
            node = par
        guards.append(g)
        iftests.append(ifs)
    names = set()
    for g in guards:
        for test, _b in g:
            names |= {n.id for n in ast.walk(test) if isinstance(n, ast.Name) and n.id in assigns}
            names |= {"env:" + e for e in _env_reads(test)}
    for key in sorted(names):
        env = key[4:] if key.startswith("env:") else assigns[key][0]
        for value in OFF_TRY:
            fake = _Env(env, value)
            known = {}
            for var, (e, expr) in assigns.items():
                if e == env:
                    try:
                        known[var] = eval(compile(ast.Expression(body=expr), "<v>", "eval"),
                                          {"__builtins__": {"int": int, "bool": bool, "str": str, "float": float},
                                           "os": fake, "getenv": fake.getenv, "environ": fake})
                    except Exception:
                        pass
            ok = True
            for g in guards:           # every window call must be skipped: some if around it decides "no"
                if not any(_truth(test, known, fake) is (not branch) for test, branch in g):
                    ok = False
                    break
            if ok:
                return {"env": env, "value": value, "calls": calls, "no_screen": no_screen(calls)}
    # no env var switches them: inside an if all the same? A constant in the code (SHOW = True) decides it, or
    # something X-Ray cannot follow (a command-line flag, a config value)
    guarded = all(iftests)
    const = None
    for ifs in iftests if guarded else []:
        for rel, test in ifs:
            for st_ in trees[rel].body:
                if (isinstance(st_, ast.Assign) and len(st_.targets) == 1 and isinstance(st_.targets[0], ast.Name)
                        and isinstance(st_.value, ast.Constant) and isinstance(st_.value.value, (bool, int, str))
                        and st_.targets[0].id in {n.id for n in ast.walk(test) if isinstance(n, ast.Name)}):
                    const = const or {"name": st_.targets[0].id, "value": st_.value.value, "file": rel,
                                      "line": st_.lineno}
    return {"env": None, "calls": calls, "guarded": guarded, "const": const, "no_screen": no_screen(calls)}


def video_codec(path):
    """the video codec of a file, read from its header (MP4 / MOV, MKV / WebM, AVI) - None when unknown"""
    import struct
    names = {b"avc1": "H.264", b"avc3": "H.264", b"hvc1": "H.265", b"hev1": "H.265", b"av01": "AV1", b"vp09": "VP9",
             b"vp08": "VP8", b"mp4v": "MPEG-4 part 2", b"mjpa": "MJPEG", b"jpeg": "MJPEG", b"apch": "ProRes",
             b"apcn": "ProRes", b"apcs": "ProRes", b"ap4h": "ProRes", b"dvh1": "Dolby Vision (H.265)",
             b"dvhe": "Dolby Vision (H.265)", b"H264": "H.264", b"h264": "H.264", b"HEVC": "H.265", b"XVID": "MPEG-4 part 2",
             b"DIVX": "MPEG-4 part 2", b"MJPG": "MJPEG", b"AV01": "AV1", b"FMP4": "MPEG-4 part 2", b"DX50": "MPEG-4 part 2",
             b"AVdn": "DNxHD", b"AVdh": "DNxHR", b"s263": "H.263", b"H263": "H.263", b"mp2v": "MPEG-2", b"xvid": "MPEG-4 part 2",
             b"FFV1": "FFV1", b"ap4x": "ProRes", b"apco": "ProRes", b"vp80": "VP8", b"VP80": "VP8", b"VP90": "VP9"}
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            head = fh.read(12)
            if head[4:8] in (b"ftyp", b"moov", b"mdat", b"free", b"wide", b"skip"):        # MP4 / MOV

                def boxes(start, end):
                    pos = start
                    while pos + 8 <= end:
                        fh.seek(pos)
                        sz, typ = struct.unpack(">I4s", fh.read(8))
                        hdr = 8
                        if sz == 1:
                            sz, hdr = struct.unpack(">Q", fh.read(8))[0], 16
                        elif sz == 0:
                            sz = end - pos
                        if sz < hdr:
                            return
                        yield typ, pos + hdr, pos + sz
                        pos += sz

                def read_at(pos, n):
                    fh.seek(pos)
                    return fh.read(n)

                def find(start, end, path_):
                    for typ, a, b in boxes(start, end):
                        if typ == path_[0]:
                            if len(path_) == 1:
                                yield a, b
                            else:
                                yield from find(a, b, path_[1:])
                for a, b in find(0, size, [b"moov"]):
                    for ta, tb in find(a, b, [b"trak"]):
                        kind = next((read_at(ha + 8, 4) for ha, hb in find(ta, tb, [b"mdia", b"hdlr"])), b"")
                        if kind != b"vide":
                            continue
                        for sa, sb in find(ta, tb, [b"mdia", b"minf", b"stbl", b"stsd"]):
                            fourcc = read_at(sa + 12, 4)
                            return names.get(fourcc, fourcc.decode("latin-1").strip() or None)
                return None
            fh.seek(0)
            data = fh.read(8 << 20)
            if head[:4] == b"\x1aE\xdf\xa3":                                       # Matroska / WebM
                for cid, nm in ((b"V_AV1", "AV1"), (b"V_MPEGH/ISO/HEVC", "H.265"), (b"V_MPEG4/ISO/AVC", "H.264"),
                                (b"V_VP9", "VP9"), (b"V_VP8", "VP8"), (b"V_FFV1", "FFV1"), (b"V_THEORA", "Theora"),
                                (b"V_MPEG2", "MPEG-2"), (b"V_MPEG4/ISO/ASP", "MPEG-4 part 2"), (b"V_MJPEG", "MJPEG"),
                                (b"V_PRORES", "ProRes"), (b"FFV1", "FFV1")):        # FFV1: a fourcc in V_MS/VFW/FOURCC
                    if cid in data:
                        return nm
            if head[:4] == b"\x00\x00\x01\xba":                                   # MPEG program stream (.mpg)
                return "MPEG-2" if b"\x00\x00\x01\xb5" in data else "MPEG-1/2"
            if head[:4] == b"OggS":
                return "Theora" if b"\x80theora" in data else "Ogg"
            if head[:4] == b"\x30\x26\xb2\x75":                                   # ASF (.wmv)
                for cid in (b"WMV1", b"WMV2", b"WMV3", b"WVC1", b"WMVA"):
                    if cid in data:
                        return cid.decode()
                return "WMV"
            if head[:4] == b"GIF8":
                return "GIF"
            if head[:4] == b"RIFF" and head[8:12] == b"AVI ":                     # AVI: strh 'vids' + fourcc
                i = data.find(b"vids")
                if i >= 0:
                    fourcc = data[i + 4:i + 8]
                    return names.get(fourcc, fourcc.decode("latin-1").strip() or None)
    except (OSError, struct.error, ValueError):
        return None
    return None


VIDEO_EXT = (".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi")
CODEC_NOTE = {"AV1": "OpenCV's Linux wheel cannot read AV1 (no software AV1 decoder inside; its own AV1 decoder needs "
                     "hardware decoding, which a container does not have): runs\\NAME\\videos.bat converts it to H.264",
              "VP9": "OpenCV's Linux wheel reads VP9 in software - fine"}


def plan(st, project, name, image, dockerfile, stop_after):
    facts = [f for f in st.get("facts", []) if live(f)]
    by = {}
    for f in facts:
        by.setdefault(f["category"], []).append(f)
    entries = st.get("entrypoints") or []
    wd, dfile = workdir_and_cmd(project, dockerfile)
    p = {"name": name, "image": image or f"{name}-naive", "project": project, "workdir": wd, "dockerfile": dfile,
         "pythonpath": image_pythonpath(dfile),
         "entries": entries, "mounts": [], "env": [], "env_cpu": [], "env_optional": [], "notes": [], "ports": [],
         "gpu": False, "gui": False, "shm": False, "stop_after": stop_after, "out_mount": False,
         "file_mounts": [], "mac": None, "app_dirs": []}
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
        if n in seen or n.startswith("<") or f.get("mechanism") in ("env_set", "env_set_default") or \
                "set by the code at runtime" in str(f.get("detail")):     # the code sets it itself when missing
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
    # files of one PC named in the code (C:\...\lane.mp4 in a config list): each one that is on this PC is mounted,
    # and X-Ray's xray_inputs gives the code the container path (PC_PATHS) - no code change
    p["pc_files"] = []
    for f in by.get("hardcoded_config", []):
        subj = str(f["subject"])
        s = next((f"{x.get('file')}:{x.get('line')}" for x in f.get("sites") or [] if x.get("line")), "")
        if f.get("mechanism") in ("pc_file", "windows_path") and "<" not in subj and posixpath.splitext(subj)[1] \
                and subj.lower() not in {x["key"] for x in p["pc_files"]}:
            host = subj.replace("/", "\\") if re.match(r"^[A-Za-z]:/", subj) else subj
            here = os.path.isfile(subj)
            n = sum(1 for x in p["pc_files"] if x["here"]) + 1
            cont = f"/xin/pc_{n}/" + subj.rsplit("/", 1)[-1]
            p["pc_files"].append({"key": subj.lower(), "pc": host, "cont": cont, "where": s, "here": here})
            if here:
                p["mounts"].append((host, cont, "ro", f"a file of this PC named in the code ({s}) - PC_PATHS maps "
                                                      "the code's path to it"))
            else:
                p["notes"].append(f"the code names {host} ({s}) - that file is not on this PC: copy it to exactly "
                                  "that path and run xray_runfile.py again (then it is mounted)"
                                  + (", or set SOURCE below to a video of this PC (or a folder of videos, or an "
                                     "rtsp:// camera)" if subj.lower().endswith(VIDEO_EXT) else "")
                                  + " - else the program cannot open it")
        elif f.get("mechanism") == "machine_path":
            p["notes"].append(f"code change needed: {f['subject']} is hardcoded ({s}) - the run cannot find it")
    here = [x for x in p["pc_files"] if x["here"]]
    if here:
        p["env"].append(("PC_PATHS", "|".join(f"{x['pc']}={x['cont']}" for x in here),
                         "files the code names by their PC path -> where the run mounts them; X-Ray's xray_inputs "
                         "(on PYTHONPATH) gives the code that path - no code change"))
    p["gpu"] = any(f.get("mechanism") not in ("env_default",) for f in by.get("gpu_usage", []))
    p["gui"] = bool(by.get("gui_usage"))
    p["window"] = window_switch(st, project) if p["gui"] else None
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
    consts = path_constants(project)
    for f in by.get("file_write", []):
        subj = str(f["subject"])
        if f.get("path_envs") or not re.match(r"^/(home|Users)/[^/<]+/", subj):
            continue
        k = const_of(subj, consts, outer=True)
        pre = k[1] if k else subj.split("<")[0].rstrip("/")
        if not k and "/" in pre and "." in pre.rsplit("/", 1)[1]:
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
    # folders the app writes below its own folder (logs/...): kept from the load runs (they hold the app's own timings)
    for f in by.get("file_write", []):
        subj = str(f["subject"]).replace("\\", "/")
        top = subj.split("/")[0]
        if "/" in subj and not os.path.isabs(subj) and not PC_PATH.match(subj) and top and "<" not in top \
                and top not in (".", "..", "~") and top not in p["app_dirs"]:
            p["app_dirs"].append(top)
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
    consts = path_constants(project)
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
                    k = const_of(subj, consts) if new is None and env is None else None
                    if k:             # the folder comes from a constant in another file (constants.py)
                        cf, cl = k[2].rsplit(":", 1)
                        if (cf, int(cl)) in seen:
                            continue
                        seen.add((cf, int(cl)))
                        lines, text = read_line(project, cf, int(cl))
                        x = {"file": cf, "line": int(cl)}
                        new, env, note = suggest(text, c, subj)
                        note += "; every path built from " + k[0] + " follows it"
                need_import = new is not None and lines is not None and not any(
                    re.match(r"^\s*(import os\b|from os import)", ln) for ln in lines)
                items.append({"where": f"{x['file']}:{x['line']}", "now": text.strip(), "new": new and new.strip(),
                              "env": env, "note": note + ("; add  import os  at the top of the file" if need_import
                                                          else "")})
    return items


def window_change(window, project):
    """SHOW = True written in the code switches the windows: in a container it must be off -> read it from an env var"""
    k = (window or {}).get("const")
    if not window or not (window.get("no_screen") or ("", True))[1]:
        return []                       # no window, or only plots that do not stop the program (matplotlib)
    if window.get("calls") and window.get("env") is None and window.get("guarded") is False:
        c = window["calls"][0]
        lines, text = read_line(project, c[0], c[1])
        return [{"where": f"{c[0]}:{c[1]}", "now": (text or "").strip() or None, "new": None, "env": "SHOW", "value": "0",
                 "note": "the code always opens windows here, and a container has no screen (" + window["no_screen"][0]
                         + "): add a switch -  SHOW = os.getenv(\"SHOW\", \"1\") == \"1\"  at the top of the file, "
                         "then  if SHOW:  around the window calls (" + ", ".join(x[2] for x in window["calls"][:3])
                         + "). Your PC keeps showing them; the run files and the compose files set SHOW=0"}]
    if not k:
        return []
    lines, text = read_line(project, k["file"], k["line"])
    on = "1" if k["value"] not in (False, 0, "0", "") else "0"
    c = window["calls"][0]
    return [{"where": f"{k['file']}:{k['line']}", "now": (text or "").strip() or None,
             "new": f"{k['name']} = os.getenv(\"{k['name']}\", \"{on}\") == \"1\"", "env": k["name"], "value": "0",
             "note": f"switches the windows ({c[2]} at {c[0]}:{c[1]}); a container has no screen, so it must be off "
                     f"there ({window['no_screen'][0]}) - the run files and the compose files then set it to 0"
                     + ("; add  import os  at the top of the file" if lines and not any(
                         re.match(r"^\s*(import os\b|from os import)", ln) for ln in lines) else "")}]


def optional_changes(st, project):
    """Not needed to run, but the image gets smaller: a package the code imports and then never uses."""
    by = {}
    for f in st.get("facts", []):
        if f.get("category") == "third_party_import" and live(f):
            by.setdefault(f.get("import_name") or f["subject"], []).append(f)
    items = []
    for top, fs in sorted(by.items()):
        if not all(f.get("never_used") for f in fs):
            continue
        for f in fs:
            for x in (f.get("sites") or [])[:1]:
                lines, text = read_line(project, x.get("file"), x.get("line") or 0)
                stmt = re.sub(r"\s*#.*$", "", text or "").strip()
                simple = re.match(r"^(import|from)\s+" + re.escape(top) + r"\b[^;]*$", stmt)
                why = ("maybe kept for what importing does: a plugin that registers itself, a patch - or, on Windows, "
                       "a package that ships CUDA DLLs imported so that another library finds them (e.g. torch before "
                       "onnxruntime); in the Linux image the loader finds NVIDIA's CUDA wheels without it")
                items.append({"where": f"{x.get('file')}:{x.get('line')}", "now": (text or "").strip() or None,
                              "new": (f"if os.name == \"nt\": {stmt}" if simple else None), "env": None,
                              "note": f"the code never uses {top} - only the import runs ({why}). The image carries "
                                      f"{top} and every package only it needs for this line (step3.txt / the plan say how "
                                      "many GB). Windows-only as above keeps your PC as it is; if nothing needs it, delete "
                                      "the line. Then rebuild the naive image and run steps 2-4 again"})
    return items


def write_code_changes(items, name, office_hint=True, optional=()):
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
            L += [f"   IN DOCKER: -e {it['env']}={it.get('value') or '<value>'}"]
        L += [f"   NOTE:      {it['note']}", ""]
    if optional:
        L += ["", "OPTIONAL - it runs without these (each NOTE says what it gains):", ""]
        for i, it in enumerate(optional, 1):
            L += [f"{i}. WHERE:     {it['where']}"]
            if it["now"]:
                L += [f"   NOW:       {it['now']}"]
            L += [f"   CHANGE TO: {it['new'] or '(by hand - see the note)'}", f"   NOTE:      {it['note']}", ""]
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
        L += ["REM GPUBASE (GPU projects only): auto = the same kind of base as the naive image (it worked on your GPU);",
              "REM nvidia = the nvidia/cuda runtime image (CUDA from NVIDIA's apt packages); pip = python:X-slim + the CUDA",
              "REM libraries from pip wheels (usually smaller). Build both and see the difference with compare-images.",
              'set "GPUBASE=auto"',
              'if not exist "%RUN%\\' + name + '-runtime.json" (echo Run run-step2 first: ' + name + '-runtime.json is missing & exit /b 1)',
              'mkdir "%RUN%\\pkg" 2>nul']
    else:
        L += ['GPUBASE=auto   # GPU projects: auto = the naive image\'s kind of base; nvidia = nvidia/cuda runtime; pip = python:X-slim + pip CUDA',
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


def source_lines(sh):
    """SOURCE at the top of a run file: what every video source of the program gets instead (xray_inputs)"""
    if sh.bat:
        return ["REM SOURCE: what the program reads INSTEAD of what its code opens (a camera, an rtsp:// address, a",
                "REM         video file) - no code change. Empty = what the code says. A % in it: write %%.",
                'REM           a video file on this PC:  set "SOURCE=D:\\videos\\lane 3.mp4"   every source gets it',
                'REM           a folder of videos:       set "SOURCE=D:\\videos"    each source its own video from it',
                'REM           a camera stream:          set "SOURCE=rtsp://user:password@192.168.1.20:554/stream1"',
                'set "SOURCE="',
                'set "XSRC="', 'if "%SOURCE%"=="" goto src_done',
                'if /i "%SOURCE:~0,4%"=="rtsp" goto src_url', 'if /i "%SOURCE:~0,4%"=="rtmp" goto src_url',
                'if /i "%SOURCE:~0,4%"=="http" goto src_url', 'if /i "%SOURCE:~0,4%"=="udp:" goto src_url',
                'for %%F in ("%SOURCE%") do set "SOURCE=%%~fF"',
                'if "%SOURCE:~-1%"=="\\" set "SOURCE=%SOURCE:~0,-1%"',
                'if exist "%SOURCE%\\*" goto src_dir',
                'if not exist "%SOURCE%" goto src_missing',
                'for %%F in ("%SOURCE%") do set "SRCNAME=%%~nxF"',
                'set XSRC=-v "%SOURCE%:/xsource/%SRCNAME%:ro" -e "XRAY_SOURCE=/xsource/%SRCNAME%"', "goto src_done",
                ":src_missing", 'echo SOURCE "%SOURCE%": no such file or folder on this PC - fix SOURCE at the top '
                'of this file.', "exit /b 1",
                ":src_dir", 'set XSRC=-v "%SOURCE%:/xsource:ro" -e XRAY_SOURCE=/xsource', "goto src_done",
                ":src_url", 'set XSRC=-e "XRAY_SOURCE=%SOURCE%"', ":src_done", ""]
    return ['SOURCE=""      # what the program reads INSTEAD of what its code opens: a video file, a folder of videos '
            '(each source its own), rtsp://... - empty = what the code says',
            'XSRC=()',
            'case "$SOURCE" in',
            '  "") ;;',
            '  rtsp*|rtmp*|http*|udp:*) XSRC=(-e "XRAY_SOURCE=$SOURCE");;',
            '  *) [ -e "$SOURCE" ] || { echo "SOURCE $SOURCE: no such file or folder - fix SOURCE at the top of this file"; exit 1; }',
            '     SOURCE="$(cd "$(dirname "$SOURCE")" && pwd)/$(basename "$SOURCE")"     # docker needs the full path',
            '     if [ -d "$SOURCE" ]; then XSRC=(-v "$SOURCE:/xsource:ro" -e XRAY_SOURCE=/xsource); '
            'else XSRC=(-v "$SOURCE:/xsource/$(basename "$SOURCE"):ro" -e "XRAY_SOURCE=/xsource/$(basename "$SOURCE")"); fi;;',
            'esac', ""]


def write_run(p, sh, xdir, out_dir, cpu, slim=False, load=False, explore=False):
    name = p["name"]
    X, R, PY = ("%X%", "%RUN%", "%PY%") if sh.bat else ("$X", "$RUN", "$PY")
    sep = "\\" if sh.bat else "/"
    trace_names = []
    folder = load or explore            # results in their own folder (load-A, explore)
    L = head(sh, xdir, out_dir, "EXPLORE: the app runs until you stop it (video files loop), so you can look inside "
             "the container by hand" if explore else
             "a LOAD TEST, RUN_SECONDS long (usage: run-load A, then B, C ... for the next setups)" if load else
             "the traced run on the " + ("planned image " + name + "-slim + verify" if slim else
                                         "naive image" + (" without the GPU" if cpu else "")))
    if p["notes"]:
        L += [sh.rem + "CHECK BEFORE RUNNING:"] + [sh.rem + "  - " + n for n in p["notes"]] + [""]
    # settings at the top of the file: how long, loop, which videos (change them in Notepad)
    secs = 300 if load else 0 if explore else p["stop_after"]
    loop = 1 if (load or explore) else 0
    if sh.bat:
        L += ["REM ---- settings: change them here, then run this file ----",
              "REM RUN_SECONDS: how long the program runs, then it is stopped (0 = until it ends by itself or you stop it)",
              f'set "RUN_SECONDS={secs}"',
              "REM LOOP: 1 = a video FILE starts again from the beginning when it ends (the program keeps working)",
              f'set "LOOP={loop}"',
              "REM VIDEOS: a folder with videos (.mp4 .avi .mov ...): the program plays them one after the other instead",
              "REM         of its own video (with LOOP=1 the list starts again). Empty = the program's own video.",
              "REM         (both only matter for programs that read video FILES; cameras / streams are not touched)",
              'set "VIDEOS="',
              "REM " + "-" * 60,
              'set "XSTOP="', 'if not "%RUN_SECONDS%"=="0" set "XSTOP=--stop-after %RUN_SECONDS%"',
              'set "XVID="', 'if not "%VIDEOS%"=="" set XVID=-v "%VIDEOS%:/xvideos:ro" -e XRAY_PLAYLIST=/xvideos', ""]
        L += source_lines(sh)
    else:
        L += ["# ---- settings: change them here ----",
              f"RUN_SECONDS={secs}".ljust(16) + "# how long the program runs, then it is stopped (0 = until it ends / you stop it)",
              f"LOOP={loop}".ljust(16) + "# 1 = a video FILE starts again when it ends (the program keeps working)",
              'VIDEOS=""'.ljust(16) + "# a folder of videos: played one after the other instead of the program's own video",
              'XSTOP=""; [ "$RUN_SECONDS" != "0" ] && XSTOP="--stop-after $RUN_SECONDS"',
              'XVID=(); [ -n "$VIDEOS" ] && XVID=(-v "$VIDEOS:/xvideos:ro" -e XRAY_PLAYLIST=/xvideos)', ""]
        L += source_lines(sh)
    suffix = "-cpu" if cpu else "-slim" if slim else ""
    image = name + "-slim" if slim else p["image"]
    if load:
        LB = "load-%L%" if sh.bat else "load-$L"
        if sh.bat:
            L += ['set "L=%~1"', 'if "%L%"=="" set "L=A"',
                  sh.rem + "the small image if it was built, else the naive one",
                  'set "IMG=' + name + '-slim"', "docker image inspect %IMG% >nul 2>&1 || set \"IMG=" + p["image"] + '"',
                  'echo Load test %L% on image %IMG% - %RUN_SECONDS% s. Results: runs\\' + name + '\\load-%L%\\', ""]
        else:
            L += ['L="${1:-A}"', 'IMG=' + name + '-slim',
                  'docker image inspect "$IMG" >/dev/null 2>&1 || IMG=' + p["image"],
                  'echo "Load test $L on image $IMG - $RUN_SECONDS s. Results: runs/' + name + '/load-$L/"', ""]
        image = "%IMG%" if sh.bat else "$IMG"
    if explore:
        LB = "explore"
        cn = name + "-explore"
        L += ["docker rm -f " + cn + (" >nul 2>&1" if sh.bat else " >/dev/null 2>&1")]
        for t in ["The app starts now and keeps running (LOOP=1: a video file starts again when it ends).",
                  "In a SECOND window:   docker exec -it " + cn + " python /xray/xray_look.py            (what it uses now)",
                  "                      docker exec -it " + cn + " python /xray/xray_look.py --watch 5  (live changes)",
                  "                      docker exec -it " + cn + " bash      (look around by hand, see GUIDE.md)",
                  "                      docker stats   /   docker top " + cn,
                  "To stop:              docker stop -t 60 " + cn + "      (then this window writes the X-Ray report)"]:
            L.append("echo " + t if sh.bat else "echo '" + t + "'")
        L.append('if not "%RUN_SECONDS%"=="0" echo It also stops by itself after %RUN_SECONDS% s - RUN_SECONDS above.'
                 if sh.bat else
                 '[ "$RUN_SECONDS" != "0" ] && echo "It also stops by itself after $RUN_SECONDS s - RUN_SECONDS above."')
        L.append("")
    entries = p["entries"] or ["main.py"]
    envs = list(p["env"]) + (p["env_cpu"] if cpu else [])
    for e in entries:
        stem = safe(os.path.splitext(os.path.basename(e))[0]) if len(entries) > 1 else ""
        if folder:
            tdir = LB + sep + "trace" + ("-" + stem if stem else "")
            log = LB + sep + "run" + ("-" + stem if stem else "") + ".log"
            outd, filesd = LB + sep + "out", LB + sep + "files"
        else:
            tdir = "trace" + suffix + ("-" + stem if stem else "")
            log = "step2" + suffix + ("-" + stem if stem else "") + ".log"
            # every run gets its own fresh output folders (out, out-cpu, out-slim ...): the outputs can be compared,
            # and the planned image (a non-root user) cannot overwrite files an earlier run wrote as root
            outd, filesd = "out" + suffix, "files" + suffix
        trace_names.append(tdir)
        fdirs = [R + sep + filesd + sep + sub for sub, cont, why in p["file_mounts"]]
        adirs = [R + sep + LB + sep + "applog" + sep + d for d in p["app_dirs"]] if folder else []
        fresh = ([LB] if e == entries[0] else []) if folder else \
            [tdir] + ([outd] if p["out_mount"] else []) + ([filesd] if fdirs else [])
        if sh.bat:
            L += ['if exist "%RUN%\\' + d + '" rmdir /s /q "%RUN%\\' + d + '"' for d in fresh]
            L += ['mkdir "%RUN%\\' + tdir + '" ' + ('"%RUN%\\' + outd + '" ' if p["out_mount"] else "")
                  + " ".join('"' + d + '"' for d in fdirs + adirs) + ' 2>nul']
        else:
            mk = '"$RUN/' + tdir + '" ' + ('"$RUN/' + outd + '" ' if p["out_mount"] else "") + " ".join('"' + d + '"' for d in fdirs + adirs)
            L += (["rm -rf " + " ".join('"$RUN/' + d + '"' for d in fresh) + "; "] if fresh else [""])
            L[-1] += "mkdir -p " + mk + "; chmod 777 " + mk
        if load and p["gpu"] and e == entries[0]:
            gq = "nvidia-smi --query-gpu=timestamp,utilization.gpu,memory.used,memory.total --format=csv -l 2 -f "
            L += [sh.rem + "GPU use every 2 seconds -> gpu.csv (stopped when the run ends)",
                  ('start "gpu-log" /min ' + gq + '"%RUN%\\' + LB + '\\gpu.csv"') if sh.bat else
                  (gq + '"$RUN/' + LB + '/gpu.csv" & GPULOG=$!')]
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
        a = ["docker run --rm" + (" --name " + name + "-explore" + ("-" + stem if stem else "") +
                                  " --cap-add SYS_PTRACE --security-opt seccomp=unconfined" if explore else "")
             + (" --gpus all" if p["gpu"] and not cpu else "") + (" --shm-size 2g" if p["shm"] else "")
             + (" --mac-address " + p["mac"] if p["mac"] else "")
             + "".join(" -p " + str(pt) + ":" + str(pt) for pt in p["ports"])]
        a.append('  -v "' + X + ':/xray:ro" -v "' + R + sep + tdir + ':/xray-out"')
        for host, cont, mode, why in p["mounts"]:
            a.append('  -v "' + host + ":" + cont + ":" + mode + '"')
        a += adapt_flags(p, X, sep, False)       # X-Ray's own --gui off handles windows here
        if p["out_mount"]:
            a.append('  -v "' + R + sep + outd + ':/xout"')
        for sub, cont, why in p["file_mounts"]:
            a.append('  -v "' + R + sep + filesd + sep + sub + ":" + cont + '"')
        for d in (p["app_dirs"] if folder else []):
            a.append('  -v "' + R + sep + LB + sep + "applog" + sep + d + ":" + p["workdir"].rstrip("/") + "/" + d + '"')
        if p["env"] or p["env_optional"]:
            # every env var in ONE file (runs/NAME/run.env): add or change values there, not in each run file
            a.append('  --env-file "' + R + sep + 'run.env"')
        for n, v, why in (p["env_cpu"] if cpu else []):
            a.append("  -e " + n + "=" + v)
        if load:
            a.append("  -e XRAY_COVERAGE=0")       # no line counting: it would slow the app and skew the numbers
        a.append("  -e XRAY_LOOP_VIDEO=%LOOP% %XVID% %XSRC%" if sh.bat else
                 '  -e XRAY_LOOP_VIDEO="$LOOP" ${XVID[@]+"${XVID[@]}"} ${XSRC[@]+"${XSRC[@]}"}')
        a.append("  " + image)
        flags = (" --gui off" if p["gui"] else "") + (" %XSTOP%" if sh.bat else " $XSTOP")
        a.append("  python /xray/xray_trace.py run --out /xray-out --root " + p["workdir"] + flags + " -- python " + e)
        msg = ("Explore run" if explore else "Load test" if load else "Step 2" + (" (CPU)" if cpu else " (planned image)" if slim else "")) + (": " + e
               + " - the window stays quiet until it ends. Log: " + log)
        L += ["echo " + msg if sh.bat else "echo '" + msg + "'"]
        # one command over several lines: each line but the last ends with the continuation mark; the log
        # redirect sits on the LAST line (in CMD a ^ also swallows the first character of the next line)
        L += [x + sh.cont for x in a[:-1]] + [a[-1] + ' > "' + R + sep + log + '" 2>&1', ""]
    traces = " ".join('--trace "' + R + sep + t + '"' for t in trace_names)
    pyv = '"' + PY + '"'
    if folder:
        if p["gpu"] and load:
            L += ["taskkill /im nvidia-smi.exe /f >nul 2>&1" if sh.bat else "kill $GPULOG 2>/dev/null"]
        L += [pyv + ' "' + X + sep + 'xray_trace.py" facts ' + traces + ' --out "' + R + sep + LB + sep + 'runtime.json"',
              pyv + ' "' + X + sep + 'xray_needs.py" "' + R + sep + LB + sep + 'runtime.json" > "' + R + sep + LB + sep
              + 'needs.txt"']
        return finish(sh, L, LB + sep + "needs.txt", "Done. In the " + ("explore" if explore else "load") +
                      " folder: needs.txt - WHAT THE RUN USED "
                      "is at the end -, gpu.csv, run.log, applog = the logs the app wrote.")
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


EXIT_CODES = [("0", "normal end"), ("1", "a Python error: read the Traceback above"),
              ("125", "docker could not start the container: read docker's message above"),
              ("130", "stopped with Ctrl+C (RUN_SECONDS)"),
              ("134", "aborted - in a container usually a window (no screen: 'Could not load the Qt platform plugin xcb')"),
              ("137", "killed: it did not end within 30 s of the stop (Ctrl+C) - a program with child processes "
                      "often keeps running - or out of memory (docker stats shows the RAM)"),
              ("139", "crashed (segmentation fault) - in a container usually a window (no screen), else a native library")]


def window_lines(p, rem):
    """what run-naive says about the program's windows"""
    w = p.get("window") or {}
    where = ", ".join(f"{c[2]} at {c[0]}:{c[1]}" for c in (w.get("calls") or [])[:3])
    if not p["gui"]:
        return []
    if w.get("env"):
        return [rem + f"WINDOWS: the code opens windows ({where}) only when {w['env']} is on. A container has no screen,",
                rem + f"so this run sets {w['env']}={w['value']} (found in the code's own if-condition). X-Ray's runs switch",
                rem + "windows off themselves and keep the code as it is."]
    ns, stops = w.get("no_screen") or no_screen(w.get("calls"))
    err = [rem + "  without a screen: " + x for x in re.findall(r".{1,100}(?:\s|$)", ns)]
    if not stops and not w.get("env"):
        return [rem + f"WINDOWS: the code shows plots ({where}). A container has no screen - nothing to do:"] + err
    if no_windows(w):
        how = (f"when {w['const']['name']} = {w['const']['value']!r} is on ({w['const']['file']}:{w['const']['line']})"
               if w.get("const") else "inside an if no env var decides" if w.get("guarded") else "always")
        return [rem + f"WINDOWS: the code opens OpenCV windows ({where}) - {how}. A container has no screen:"] + err + [
                rem + "So this run switches them off WITHOUT a code change: NO_WINDOWS=1, with the tools' xray_no_windows",
                rem + "folder on PYTHONPATH - cv2.imshow / waitKey / namedWindow ... then do nothing, the rest runs as is.",
                rem + "The same is in the compose file of the plan. To SEE the windows on your screen: run-window."]
    if w.get("const"):
        k = w["const"]
        return [rem + f"WINDOWS: the code opens windows ({where}) when {k['name']} is on - and {k['name']} = {k['value']!r} is",
                rem + f"written in the code ({k['file']}:{k['line']}). A container has no screen:"] + err + [
                rem + f"Read it from an env var:  {k['name']} = os.getenv(\"{k['name']}\", \"1\") == \"1\"  - then rebuild: "
                      "run-naive sets it to 0 then.",
                rem + "X-Ray's runs (run-step2, run-explore, run-load) switch windows off themselves."]
    if w.get("guarded"):
        return [rem + f"WINDOWS: the code opens windows ({where}) inside an if that no env var decides (a constant, a",
                rem + "flag or a config value?). A container has no screen - if that setting is on:"] + err + [
                rem + "Switch it off. X-Ray's runs (run-step2, run-explore, run-load) switch windows off themselves."]
    return [rem + f"WINDOWS: the code always opens windows ({where}) - no setting switches them off. A container has",
            rem + "no screen:"] + err + [
            rem + "Watch it with run-explore / run-load instead (X-Ray switches windows off), or make the window optional",
            rem + "in the code:  SHOW = os.getenv(\"SHOW\", \"1\") == \"1\"  and  if SHOW:  around the window calls."]


def write_naive_run(p, sh, out_dir, xdir, window=False):
    """the naive image ON ITS OWN, without X-Ray: does it start and do its work? Stopped after RUN_SECONDS.
    window=True: the same with the program's windows on this PC's screen (run-window)"""
    name, R, X = p["name"], ("%RUN%" if sh.bat else "$RUN"), ("%X%" if sh.bat else "$X")
    sep = "\\" if sh.bat else "/"
    cn = name + ("-window" if window else "-run")
    w = p.get("window") or {}
    a = ["docker run --rm --name " + cn + (" --gpus all" if p["gpu"] else "") + (" --shm-size 2g" if p["shm"] else "")
         + (" --mac-address " + p["mac"] if p["mac"] else "") + "".join(" -p " + str(x) + ":" + str(x) for x in p["ports"])]
    a += ['  -v "' + host + ":" + cont + ":" + mode + '"' for host, cont, mode, why in p["mounts"]]
    if p["out_mount"]:
        a.append('  -v "' + R + sep + 'out-naive:/xout"')
    if p["env"] or p["env_optional"]:
        a.append('  --env-file "' + R + sep + 'run.env"')
    if p["gui"] and w.get("env") and not window:
        a.append("  -e " + w["env"] + "=" + w["value"])        # after --env-file: this value wins
    a += adapt_flags(p, X, sep, bool(p["gui"] and no_windows(w) and not window))
    if window:
        a += (["  -e DISPLAY=:0 -e WAYLAND_DISPLAY=wayland-0 -e XDG_RUNTIME_DIR=/mnt/wslg/runtime-dir "
               "-e PULSE_SERVER=/mnt/wslg/PulseServer -e QT_X11_NO_MITSHM=1",
               "  -v /run/desktop/mnt/host/wslg/.X11-unix:/tmp/.X11-unix -v /run/desktop/mnt/host/wslg:/mnt/wslg"]
              if sh.bat else ['  -e DISPLAY="$DISPLAY" -e QT_X11_NO_MITSHM=1 -v /tmp/.X11-unix:/tmp/.X11-unix'])
    img = p["image"] + ("-window" if window else "")
    a.append("  %XSRC%" if sh.bat else '  ${XSRC[@]+"${XSRC[@]}"}')
    a.append("  " + (("%WIMG%" if sh.bat else '"$WIMG"') if window else p["image"]))
    if sh.bat:
        L = ["@echo off", "REM Generated by xray_runfile.py " + VERSION + ": run the naive image ON ITS OWN (no X-Ray) - " +
             ("its windows on THIS PC's screen" if window else "does it start and do its work?"), "setlocal",
             'set "RUN=' + out_dir + '"', 'set "X=' + xdir + '"', "",
             "REM RUN_SECONDS: stop it after this many seconds (0 = until it ends by itself or you press Ctrl+C)",
             'set "RUN_SECONDS=120"', ""]
        L += source_lines(sh)
        L += window_run_lines(p, sh, img) if window else window_lines(p, "REM ")
        L += [sh.rem + "  " + host + "  mounted as  " + cont + "   (" + why + ")" for host, cont, mode, why in p["mounts"]]
        L += ["docker rm -f " + cn + " >nul 2>&1", 'mkdir "%RUN%\\out-naive" 2>nul' if p["out_mount"] else "",
              "REM the stop: Ctrl+C for the program (SIGINT, as on your PC), then docker stop (30 s to end, then killed)",
              'if not "%RUN_SECONDS%"=="0" start "stop ' + cn + '" /min cmd /c "timeout /t %RUN_SECONDS% /nobreak >nul '
              '& docker kill -s INT ' + cn + ' >nul 2>&1 & docker stop -t 30 ' + cn + ' >nul 2>&1"',
              'if "%RUN_SECONDS%"=="0" echo The program\'s own output follows. It runs until it ends by itself - RUN_SECONDS is 0.',
              'if not "%RUN_SECONDS%"=="0" echo The program\'s own output follows. It is stopped after %RUN_SECONDS% seconds.',
              ""]
        L += [x + " ^" for x in a[:-1]] + [a[-1], 'set "RC=%ERRORLEVEL%"', "echo.",
                                            "echo The program ended with exit code %RC%:"]
        L += [f'if "%RC%"=="{c}" echo   {c} = {t}' for c, t in EXIT_CODES]
        if p["out_mount"]:
            L.append("echo What it wrote is in runs\\" + name + "\\out-naive")
        L += ["endlocal"]
        return "\r\n".join(L) + "\r\n"
    L = ["#!/usr/bin/env bash", "# Generated by xray_runfile.py " + VERSION + ": run the naive image on its own (no X-Ray) - " +
         ("its windows on this machine's screen" if window else "does it start and do its work?"),
         'RUN="' + out_dir + '"', 'X="' + xdir + '"',
         "RUN_SECONDS=120   # stop it after this many seconds (0 = until it ends / Ctrl+C)", ""]
    L += source_lines(sh)
    L += window_run_lines(p, sh, img) if window else window_lines(p, "# ")
    L += ["#   " + host + "  mounted as  " + cont + "   (" + why + ")" for host, cont, mode, why in p["mounts"]]
    L += ["docker rm -f " + cn + " >/dev/null 2>&1"]
    if p["out_mount"]:
        L += ['mkdir -p "$RUN/out-naive"; chmod 777 "$RUN/out-naive"']
    L += ["# the stop: Ctrl+C for the program (SIGINT, as on your PC), then docker stop (30 s to end, then killed)",
          '[ "$RUN_SECONDS" != "0" ] && (sleep "$RUN_SECONDS"; docker kill -s INT ' + cn + '; docker stop -t 30 ' + cn
          + ') >/dev/null 2>&1 &',
          'if [ "$RUN_SECONDS" = "0" ]; then echo "The program\'s own output follows. It runs until it ends by itself."; '
          'else echo "The program\'s own output follows. It is stopped after $RUN_SECONDS seconds."; fi', ""]
    L += [x + " \\" for x in a[:-1]] + [a[-1], "RC=$?", "echo", 'echo "The program ended with exit code $RC:"',
                                         "case $RC in"]
    L += [f'  {c}) echo "  {c} = {t}";;' for c, t in EXIT_CODES] + ["esac"]
    if p["out_mount"]:
        L.append("echo 'What it wrote is in runs/" + name + "/out-naive'")
    return "\n".join(L) + "\n"


def window_run_lines(p, sh, img):
    """run-window: what it needs (a display from the PC, the window plugin's libraries in the image) + the test image"""
    r = sh.rem
    w = p.get("window") or {}
    where = ", ".join(f"{c[2]} at {c[0]}:{c[1]}" for c in (w.get("calls") or [])[:3])
    cv2 = all(str(c[2]).startswith("cv2.") for c in (w.get("calls") or [])) and w.get("calls")
    L = [r + f"WINDOWS ON YOUR SCREEN: the program's own windows ({where}), no X-Ray, nothing switched off.",
         r + "Two things are needed:",
         r + "1. A screen the container can reach." + (" Windows 11: Docker Desktop shares WSLg (Windows' own Linux display) -" if sh.bat else
                                                      " Linux desktop: the X server's socket (/tmp/.X11-unix) and DISPLAY."),
         ]
    if sh.bat:
        L += [r + "   the -v /run/desktop/mnt/host/wslg lines below. Windows 10 (no WSLg): start an X server (VcXsrv, tick",
              r + "   'Disable access control'), then replace the two display lines by:  -e DISPLAY=host.docker.internal:0.0"]
    L += [r + "2. The window libraries in the image. A server image usually lacks them, and then even WITH a screen it",
          r + "   prints 'Could not load the Qt platform plugin \"xcb\" even though it was found' (the same words as without",
          r + "   a screen). xray_look.py --image names them: 'the Qt window plugin ... -> apt-get install -y ...'.",
          r + "   WINDOW_APT: those packages - they are added in a small test image " + img + " (FROM the naive image,",
          r + "   one extra layer, built once; the naive image itself stays as it is). Empty = run the naive image as it is."]
    if cv2:
        L += [r + "   opencv-python's window plugin needed libsm6 libice6 in X-Ray's test (Ubuntu 24.04) - the default below.",
              r + "   opencv-python-HEADLESS has no windows at all: then no screen helps (it has no window code)."]
    if sh.bat:
        L += ['set "WINDOW_APT=' + ("libsm6 libice6" if cv2 else "") + '"',
              'set "WIMG=' + p["image"] + '"',
              'if "%WINDOW_APT%"=="" goto window_run',
              'set "WIMG=' + img + '"',
              '> "%RUN%\\Dockerfile.window" echo FROM ' + p["image"],
              '>> "%RUN%\\Dockerfile.window" echo USER root',
              '>> "%RUN%\\Dockerfile.window" echo RUN apt-get update ^&^& apt-get install -y --no-install-recommends '
              '%WINDOW_APT% ^&^& rm -rf /var/lib/apt/lists/*',
              'docker build -t ' + img + ' - < "%RUN%\\Dockerfile.window"', ":window_run", ""]
    else:
        L += ['WINDOW_APT="' + ("libsm6 libice6" if cv2 else "") + '"', 'WIMG="' + p["image"] + '"',
              'if [ -n "$WINDOW_APT" ]; then WIMG="' + img + '"; printf \'FROM %s\\nUSER root\\nRUN apt-get update && '
              'apt-get install -y --no-install-recommends %s && rm -rf /var/lib/apt/lists/*\\n\' "' + p["image"] +
              '" "$WINDOW_APT" | docker build -t "$WIMG" -; fi',
              "xhost +local: >/dev/null 2>&1 || true   # let containers use this X server (if xhost is installed)", ""]
    return L


def write_videos(p, sh, xdir, out_dir):
    """videos.bat FOLDER: every video opened with the image's own OpenCV; the ones it cannot read -> H.264 copies"""
    name = p["name"]
    vid = next(((n, v, re.sub(r"^was ", "", why).split(" - ")[0]) for n, v, why in p["env"]
                if re.search(r"\.(" + "|".join(x[1:] for x in VIDEO_EXT) + r")\b", why, re.I)), None)
    host = split_path(vid[2])[0] if vid else ""
    cont_dir = vid[1].rsplit("/", 1)[0] if vid else ""
    if sh.bat:
        L = ["@echo off", "REM Generated by xray_runfile.py " + VERSION + ": can the image read your videos? Every video in a "
             "folder is opened", "REM with the OpenCV inside the image (the library your program uses) and a few frames "
             "are read. The ones it", "REM cannot read (AV1 ...) are converted to H.264 into the subfolder h264 of that "
             "folder; the originals stay.",
             "REM Usage: videos                      " + ("the folder of your program's video (" + vid[0] + ")" if vid else
                                                         "(give a folder)"),
             'REM        videos "D:\\other videos"    any folder (its subfolders too)',
             "setlocal", 'set "X=' + xdir + '"', 'set "RUN=' + out_dir + '"',
             'set "DIR=%~1"', 'if "%DIR%"=="" set "DIR=' + host + '"',
             'if "%DIR%"=="" echo Give a folder: videos "D:\\your videos"', 'if "%DIR%"=="" exit /b 1',
             'if "%DIR:~-1%"=="\\" set "DIR=%DIR:~0,-1%"',
             "REM IMG: the image whose OpenCV decides (the slim image has the same OpenCV version)",
             'set "IMG=' + p["image"] + '"',
             "REM CONVERT: 1 = convert the videos the image cannot read, 0 = only check",
             'set "CONVERT=1"',
             "REM GPU: 1 = convert on NVIDIA's GPU encoder when Docker can give the container the GPU (much faster for",
             "REM      big videos; without it the CPU does it), 0 = CPU only",
             'set "GPU=1"',
             'mkdir "%RUN%\\videos" 2>nul', 'del "%RUN%\\videos\\unreadable.txt" 2>nul',
             "echo Checking the videos in %DIR% with the OpenCV inside %IMG% ...",
             'docker run --rm --entrypoint "" -v "%X%:/xray:ro" -v "%DIR%:/v:ro" -v "%RUN%\\videos:/out" %IMG% python3 '
             "/xray/xray_videos.py check /v --out /out",
             'if not exist "%RUN%\\videos\\unreadable.txt" goto done', 'if "%CONVERT%"=="0" goto done', "echo.",
             "REM ffmpeg comes from an image built the first time only (xray_ffmpeg.Dockerfile): ffmpeg with the CPU",
             "REM encoder libx264 and NVIDIA's GPU encoder h264_nvenc, + Python for xray_videos.py",
             'docker image inspect xray-ffmpeg:2 >nul 2>&1 || docker build -t xray-ffmpeg:2 - < "%X%\\xray_ffmpeg.Dockerfile"',
             "REM the GPU with its video part (the driver's encoder): --gpus all alone gives only compute + utility",
             'set "GPUFLAGS="',
             'if "%GPU%"=="1" set "GPUFLAGS=--gpus all -e NVIDIA_DRIVER_CAPABILITIES=compute,video,utility"',
             'docker run --rm %GPUFLAGS% -v "%X%:/xray:ro" -v "%DIR%:/v" -v "%RUN%\\videos:/out" xray-ffmpeg:2 python '
             "/xray/xray_videos.py convert /v --list /out/unreadable.txt" +
             (' --hint "' + vid[0] + "=" + cont_dir + '"' if vid else ""),
             "REM exit code 125 = Docker itself could not start the container - with GPU=1: no GPU for Docker here",
             "if not errorlevel 125 goto converted", 'if "%GPUFLAGS%"=="" goto converted',
             "echo Docker cannot give this container the GPU - converting on the CPU instead",
             'docker run --rm -v "%X%:/xray:ro" -v "%DIR%:/v" -v "%RUN%\\videos:/out" xray-ffmpeg:2 python '
             "/xray/xray_videos.py convert /v --list /out/unreadable.txt --gpu no" +
             (' --hint "' + vid[0] + "=" + cont_dir + '"' if vid else ""),
             ":converted", "echo.",
             "echo Checking the copies with the same OpenCV ...",
             'docker run --rm --entrypoint "" -v "%X%:/xray:ro" -v "%DIR%\\h264:/v:ro" %IMG% python3 '
             "/xray/xray_videos.py check /v", "echo.",
             "echo Use the copies: in a run file  VIDEOS=%DIR%\\h264  - or the run.env line shown above",
             ":done", "endlocal"]
        return "\r\n".join(L) + "\r\n"
    L = ["#!/usr/bin/env bash", "# Generated by xray_runfile.py " + VERSION + ": can the image read your videos? Each video "
         "is opened with the image's own OpenCV;", "# the ones it cannot read are converted to H.264 into FOLDER/h264. "
         "Usage: bash videos.sh [FOLDER]", 'X="' + xdir + '"', 'RUN="' + out_dir + '"',
         'DIR="${1:-' + host + '}"', '[ -n "$DIR" ] || { echo "Give a folder: bash videos.sh /path/to/videos"; exit 1; }',
         'IMG="' + p["image"] + '"   # the image whose OpenCV decides', "CONVERT=1          # 0 = only check",
         "GPU=1              # 1 = NVIDIA's GPU encoder when Docker can give the container the GPU, 0 = CPU only",
         'mkdir -p "$RUN/videos"; chmod 777 "$RUN/videos"; rm -f "$RUN/videos/unreadable.txt"',
         'docker run --rm --entrypoint "" -v "$X:/xray:ro" -v "$DIR:/v:ro" -v "$RUN/videos:/out" "$IMG" python3 '
         "/xray/xray_videos.py check /v --out /out",
         '[ -f "$RUN/videos/unreadable.txt" ] && [ "$CONVERT" = "1" ] || exit 0',
         'docker image inspect xray-ffmpeg:2 >/dev/null 2>&1 || docker build -t xray-ffmpeg:2 - < "$X/xray_ffmpeg.Dockerfile"',
         '# the GPU with its video part (the driver\'s encoder): --gpus all alone gives only compute + utility',
         'GPUFLAGS=(); [ "$GPU" = "1" ] && GPUFLAGS=(--gpus all -e NVIDIA_DRIVER_CAPABILITIES=compute,video,utility)',
         'docker run --rm "${GPUFLAGS[@]}" -v "$X:/xray:ro" -v "$DIR:/v" -v "$RUN/videos:/out" xray-ffmpeg:2 python '
         "/xray/xray_videos.py convert /v --list /out/unreadable.txt" + (' --hint "' + vid[0] + "=" + cont_dir + '"' if vid else "")
         + "; rc=$?",
         '# exit code 125 = Docker itself could not start the container - with GPU=1: no GPU for Docker here',
         'if [ "$rc" -ge 125 ] && [ "${#GPUFLAGS[@]}" -gt 0 ]; then echo "Docker cannot give this container the GPU - '
         'converting on the CPU instead"; docker run --rm -v "$X:/xray:ro" -v "$DIR:/v" -v "$RUN/videos:/out" '
         'xray-ffmpeg:2 python /xray/xray_videos.py convert /v --list /out/unreadable.txt --gpu no' +
         (' --hint "' + vid[0] + "=" + cont_dir + '"' if vid else "") + '; fi',
         'echo "Checking the copies with the same OpenCV ..."',
         'docker run --rm --entrypoint "" -v "$X:/xray:ro" -v "$DIR/h264:/v:ro" "$IMG" python3 /xray/xray_videos.py check /v',
         'echo "Use the copies: VIDEOS=$DIR/h264 - or the run.env line shown above"']
    return "\n".join(L) + "\n"


def write_compare(p, sh, xdir, out_dir):
    """two images side by side (xray_look.py --inventory in each, then --compare): sizes, what only one has, CUDA"""
    name = p["name"]
    a_img, b_img = p["image"], name + "-slim"
    if sh.bat:
        L = ["@echo off", "REM Generated by xray_runfile.py " + VERSION + ": two images side by side - sizes, apt / pip "
             "packages only one has (biggest first),", "REM where each gets its CUDA libraries, and what the X-Ray run "
             "of each image used. Usage:", "REM   compare-images                    " + a_img + " against " + b_img,
             "REM   compare-images IMAGE_A IMAGE_B    any two images (e.g. " + name + "-slim-auto " + name + "-slim)",
             "setlocal", 'set "X=' + xdir + '"', 'set "RUN=' + out_dir + '"', 'set "PY=' + sys.executable + '"',
             'set "A=%~1"', 'if "%A%"=="" set "A=' + a_img + '"', 'set "B=%~2"', 'if "%B%"=="" set "B=' + b_img + '"',
             "REM the X-Ray run of each image marks 'used in the run' (run-step2 = the naive image, run-slim = the slim one);",
             "REM for other images put their run files here, or leave them empty",
             'set "RA=%RUN%\\' + name + '-runtime.json"', 'set "RB=%RUN%\\' + name + '-runtime-slim.json"',
             'if not "%~1"=="" set "RA="', 'if not "%~2"=="" set "RB="',
             'mkdir "%RUN%\\compare" 2>nul',
             "echo What is in %A% ...",
             'docker run --rm -u 0 --entrypoint "" -v "%X%:/xray:ro" -v "%RUN%\\compare:/out" %A% python3 '
             '/xray/xray_look.py --inventory /out/image-a.json --label %A%',
             "echo What is in %B% ...",
             'docker run --rm -u 0 --entrypoint "" -v "%X%:/xray:ro" -v "%RUN%\\compare:/out" %B% python3 '
             '/xray/xray_look.py --inventory /out/image-b.json --label %B%',
             'set "RTA="', 'if exist "%RA%" set RTA=--runtime-a "%RA%"',
             'set "RTB="', 'if exist "%RB%" set RTB=--runtime-b "%RB%"',
             '"%PY%" "%X%\\xray_look.py" --compare "%RUN%\\compare\\image-a.json" "%RUN%\\compare\\image-b.json" '
             '%RTA% %RTB% > "%RUN%\\compare\\compare.txt"',
             'notepad "%RUN%\\compare\\compare.txt"', "endlocal"]
        return "\r\n".join(L) + "\r\n"
    L = ["#!/usr/bin/env bash", "# Generated by xray_runfile.py " + VERSION + ": two images side by side. Usage: "
         "bash compare-images.sh [IMAGE_A IMAGE_B]", 'X="' + xdir + '"', 'RUN="' + out_dir + '"', 'PY="' + sys.executable + '"',
         'A="${1:-' + a_img + '}"', 'B="${2:-' + b_img + '}"',
         '# the X-Ray run of each image marks "used in the run" (only for the default pair)',
         'RA="$RUN/' + name + '-runtime.json"; RB="$RUN/' + name + '-runtime-slim.json"',
         '[ -n "${1:-}" ] && RA=""; [ -n "${2:-}" ] && RB=""',
         'mkdir -p "$RUN/compare"; chmod 777 "$RUN/compare"',
         'docker run --rm -u 0 --entrypoint "" -v "$X:/xray:ro" -v "$RUN/compare:/out" "$A" python3 /xray/xray_look.py '
         '--inventory /out/image-a.json --label "$A"',
         'docker run --rm -u 0 --entrypoint "" -v "$X:/xray:ro" -v "$RUN/compare:/out" "$B" python3 /xray/xray_look.py '
         '--inventory /out/image-b.json --label "$B"',
         'RT=(); [ -n "$RA" ] && [ -f "$RA" ] && RT+=(--runtime-a "$RA"); [ -n "$RB" ] && [ -f "$RB" ] && RT+=(--runtime-b "$RB")',
         '"$PY" "$X/xray_look.py" --compare "$RUN/compare/image-a.json" "$RUN/compare/image-b.json" ${RT[@]+"${RT[@]}"} '
         '| tee "$RUN/compare/compare.txt"']
    return "\n".join(L) + "\n"


def write_build(p, sh, out_dir):
    d = p["dockerfile"] if p["dockerfile"] and os.path.basename(p["dockerfile"]) == "Dockerfile.naive" else None
    if not d:
        return None
    try:     # files from outside the project that xray_naive.py --copy puts into the image
        ctx = "".join(f' --build-context "{m.group(1).strip()}"' for m in re.finditer(
            r"^# xray-build-context: (.+)$", open(d, encoding="utf-8", errors="replace").read(), re.M))
    except OSError:
        ctx = ""
    if sh.bat:
        return ("@echo off\r\nREM Generated by xray_runfile.py: build the naive image (several minutes the first time)\r\n"
                f'docker build -f "{d}"{ctx} -t {p["image"]} "{p["project"]}" > "{out_dir}\\build.log" 2>&1\r\n'
                f'docker images {p["image"]}\r\nREM nothing listed above = the build failed: read build.log\r\n')
    return ("#!/usr/bin/env bash\n# Generated by xray_runfile.py: build the naive image\n"
            f'docker build -f "{d}"{ctx} -t {p["image"]} "{p["project"]}" > "{out_dir}/build.log" 2>&1\n'
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
    ap.add_argument("--mac", help="the MAC address a licence check expects (written into every run file as "
                    "--mac-address), e.g. f0:2f:74:dd:7f:09")
    ap.add_argument("--stop-after", type=int, default=180, help="stop the app after this many seconds (default 180; "
                    "0 = never - only for scripts that end by themselves)")
    ap.add_argument("--shell", choices=("bat", "sh"), default="bat" if os.name == "nt" else "sh",
                    help="bat = Windows CMD (default on Windows), sh = Linux / WSL")
    a = ap.parse_args(argv)
    st = jload(a.static)
    out_dir = os.path.abspath(os.path.join(HERE, "runs", a.name))
    os.makedirs(out_dir, exist_ok=True)
    p = plan(st, os.path.abspath(a.project), a.name, a.image, a.dockerfile, a.stop_after or None)
    if a.mac:
        m = re.sub(r"[^0-9a-fA-F]", "", a.mac).lower()
        if len(m) != 12:
            sys.exit(f"--mac {a.mac}: expected 12 hex digits, e.g. f0:2f:74:dd:7f:09")
        p["mac"] = ":".join(m[i:i + 2] for i in range(0, 12, 2))
        p["notes"] = [n for n in p["notes"] if not n.startswith("licence / machine check found")]
    sh = Shell(a.shell)
    ext = ".bat" if sh.bat else ".sh"
    files = {}
    b = write_build(p, sh, out_dir)
    if b:
        files["build-naive" + ext] = b
    files["run-naive" + ext] = write_naive_run(p, sh, out_dir, HERE)
    if p["gui"] and (p["window"] or {}).get("calls") and ((p["window"] or {}).get("no_screen") or
                                                         no_screen(p["window"]["calls"]))[1]:
        files["run-window" + ext] = write_naive_run(p, sh, out_dir, HERE, window=True)
    files["run-step2" + ext] = write_run(p, sh, HERE, out_dir, cpu=False)
    if p["gpu"]:
        files["run-step2-cpu" + ext] = write_run(p, sh, HERE, out_dir, cpu=True)
    files["run-step3-4" + ext] = write_step34(p, sh, HERE, out_dir)
    files["build-slim" + ext] = write_build_slim(p, sh, out_dir)
    files["run-slim" + ext] = write_run(p, sh, HERE, out_dir, cpu=False, slim=True)
    files["run-load" + ext] = write_run(p, sh, HERE, out_dir, cpu=False, load=True)
    files["run-explore" + ext] = write_run(p, sh, HERE, out_dir, cpu=False, explore=True)
    files["compare-images" + ext] = write_compare(p, sh, HERE, out_dir)
    files["videos" + ext] = write_videos(p, sh, HERE, out_dir)
    if p["env"] or p["env_optional"]:
        E = ["# Env vars for the runs of " + a.name + " (written by xray_runfile.py " + VERSION + "). Every run file",
             "# reads this file (--env-file): change a value here once. One NAME=value per line, no quotes,",
             "# no comment after a value. Lines starting with # are off: remove the # to switch one on."]
        for n, v, why in p["env"]:
            m = re.search(r"was (.+)$", why)
            c = video_codec(m.group(1)) if m and m.group(1).lower().endswith(VIDEO_EXT) else None
            if c:
                why += f" - video {c}" + (f": {CODEC_NOTE[c]}" if c in CODEC_NOTE else "")
            E += ["# " + why, n + "=" + v]
        if p["env_optional"]:
            E += ["", "# other env vars the code reads (its default is used while the line stays off):"]
            sw = (p.get("window") or {}).get("env")
            for n, d, site in p["env_optional"]:
                E += ["#   " + n + ": read at " + site + (f" - switches the program's windows: {n}="
                                                          f"{p['window']['value']} = off (run-naive sets it; a container has "
                                                          "no screen)" if n == sw else ""), "# " + n + "=" + str(d)]
        files["run.env"] = "\n".join(E) + "\n"
    wch = window_change(p.get("window"), os.path.abspath(a.project))
    optional = optional_changes(st, os.path.abspath(a.project))
    if no_windows(p.get("window")):       # NO_WINDOWS=1 runs it as it is: the code's switch is nicer, not needed
        for it in wch:
            it["note"] = ("NOT NEEDED TO RUN: NO_WINDOWS=1 (the tools' xray_no_windows, used by run-naive and the plan's "
                          "compose file) switches OpenCV's windows off without a code change. Cleaner in the code: "
                          + it["note"])
        optional, wch = wch + optional, []
    changes = code_changes(st, os.path.abspath(a.project)) + wch
    files["code-changes.txt"] = write_code_changes(changes, a.name, optional=optional).replace(
        "\n", "\r\n" if sh.bat else "\n")
    for fn, txt in files.items():
        with open(os.path.join(out_dir, fn), "w", encoding="utf-8", newline="") as fh:
            fh.write(txt)
    print(f"xray_runfile {VERSION}: {a.name} -> {out_dir}")
    for fn in files:
        print(f"  {fn}")
    print(f"  image {p['image']}; entry {', '.join(p['entries']) or '?'}; GPU {'yes' if p['gpu'] else 'no'}; "
          f"window {'yes (--gui off)' if p['gui'] else 'no'}; ports {', '.join(p['ports']) or 'none'}")
    if p["gui"]:
        w = p.get("window") or {}
        print("  windows: " + (f"{w['env']}={w['value']} switches them off (run-naive sets it)" if w.get("env") else
                               "OpenCV windows - run-naive and the plan's compose switch them off with NO_WINDOWS=1 (no "
                               "code change); run-window shows them on your screen" if no_windows(w) else
                               "plots only - nothing to do in a container (" + w["no_screen"][0] + ")"
                               if not (w.get("no_screen") or ("", True))[1] else
                               f"{w['const']['name']} = {w['const']['value']!r} in the code opens them - see run-naive's "
                               "WINDOWS note" if w.get("const") else
                               "inside an if no env var decides (a flag?) - see run-naive's WINDOWS note"
                               if w.get("guarded") else
                               "always opened, no setting switches them off - see run-naive's WINDOWS note"))
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
    print(f"  code changes suggested: {len(changes)}" + (f", optional: {len(optional)}" if optional
                                                         else "") + " (see code-changes.txt)")
    if not p["dockerfile"]:
        print("  NOTE: no Dockerfile.naive / Dockerfile in the project - build the image first (see the guide)")
    print("First read code-changes.txt. Then open the run files in Notepad if you want, and run them in this order: "
          + ", ".join(fn for fn in files if not fn.endswith((".txt", ".env"))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
