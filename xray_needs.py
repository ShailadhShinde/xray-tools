#!/usr/bin/env python3
"""
X-Ray "needs" report: what an entry point needs to run, in plain words, and what to pass to Docker.

Reads any X-Ray output (static map, merged map, or runtime facts) and prints, per entry point:
  a tree  entry file -> the project files it imports -> what each file does, in bundles
          (cameras, screen, models, data, writes, env vars, network, packages, system, library side effects)
  needs   what YOU have to provide when the app runs in a container, and the docker flags for it.
Nothing is run; this only reads the JSON.

  python3 xray_needs.py benchmark.json                 # after Step 1 (static)
  python3 xray_needs.py benchmark-merged.json          # after Step 2 (static + what really ran)
  python3 xray_needs.py benchmark-merged.json --all    # also show what the entry point never uses
  python3 xray_needs.py X.json --out X.needs.json      # also save the tree + needs as JSON

The bundle rules are shared with xray_viewer.html (Entry tree tab): keep BUNDLES / bundle_of in sync.
"""
import argparse
import json
import posixpath
import re
import sys

VERSION = "0.1.6"

# (key, title, one-line meaning) - display order: what an ML developer cares about first
BUNDLES = [
    ("camera", "Cameras & video streams", "where frames come from"),
    ("screen", "Screen / windows", "cv2.imshow & co. - a container has no screen"),
    ("model", "Model files", "weights the code loads"),
    ("data", "Data files read", "images, json, csv, videos the code reads"),
    ("writes", "Files written", "outputs the code saves"),
    ("env", "Environment variables", "settings read from the environment"),
    ("network", "Network: databases, APIs, URLs", "things it connects to"),
    ("packages", "Python packages", "pip installs"),
    ("system", "System: Linux libraries, programs, GPU, threads, processes", "what the OS must provide"),
    ("config", "Config files", "yml / ini / toml / .env it reads"),
    ("side", "Library side effects", "caches, temp files and env reads done by libraries, not by your code"),
    ("problems", "Check these", "hardcoded values, secrets, missing files, name clashes"),
]
TITLE = {k: t for k, t, _ in BUNDLES}
PRIMARY = ("camera", "screen", "model", "data", "writes", "env", "network")
CONFIG_EXT = (".yml", ".yaml", ".ini", ".toml", ".cfg", ".conf", ".env", ".properties")
SYSTEM_CATS = {"native_library", "system_package_candidate", "subprocess_binary", "os_data", "gpu_usage",
               "thread_config", "multiprocessing", "ipc_shared_memory", "hardware_identity"}
PROBLEM_CATS = {"secret", "hardcoded_config", "dangling_reference", "shadowing", "packaging"}
PROJECT_CATS = {"unused_asset", "duplicate_asset"}
SIDE_PREFIXES = ("/root/", "/home/", "~/", "/tmp", "/var/", "/proc/", "/sys/", "/dev/shm")
OS_ENV = {"HOME", "PATH", "USER", "LOGNAME", "LANG", "LANGUAGE", "PWD", "SHELL", "TERM", "HOSTNAME", "TZ",
          "TMPDIR", "TEMP", "TMP", "PYTHONPATH", "PYTHONHASHSEED", "LD_LIBRARY_PATH"}


def from_static(f):
    """the static map found it in the project's own code (a runtime-only fact is None / has no static origin)"""
    if "required_static" in f:
        return f["required_static"] is not None
    return f.get("evidence") != "confirmed"


def detail_txt(f):
    d = f.get("detail")
    return " ".join(d) if isinstance(d, list) else str(d or "")


def is_camera(f):
    s, c = str(f.get("subject", "")), f["category"]
    if c == "device":
        return s.startswith("/dev/video") or s.startswith("camera:")
    if c in ("network_endpoint", "file_write", "data_file_read"):
        d = " ".join(f.get("detail") if isinstance(f.get("detail"), list) else [str(f.get("detail") or "")])
        return bool(re.match(r"^(rtsp|rtmp|rtp|udp|srt)://", s)) or "VideoCapture" in d
    return False


def side_effect(f):
    """done by a library (matplotlib cache, tempfile reading TMPDIR ...), not by the project's own code"""
    d = detail_txt(f)
    if f["category"] == "env_var":
        return "(read by " in d or (f.get("subject") in OS_ENV and not from_static(f))
    if f["category"] == "file_write":
        s = str(f.get("subject", ""))
        return not from_static(f) and (s.startswith(SIDE_PREFIXES) or s == "/tmp")
    return False


def set_by_code(f):
    return f.get("mechanism") == "env_set" or "set by the code at runtime" in detail_txt(f)


def bundle_of(f):
    c = f["category"]
    if c in ("local_module", "stdlib_import"):
        return None
    if c == "dynamic_import":
        return "problems" if str(f.get("subject", "")).startswith("<") else None
    if is_camera(f):
        return "camera"
    if side_effect(f):
        return "side"
    if c == "gui_usage":
        return "screen"
    if c == "model_file":
        return "model"
    if c in ("data_file_read", "config_file"):
        return "config" if str(f.get("subject", "")).lower().endswith(CONFIG_EXT) else "data"
    if c == "file_write":
        return "writes"
    if c == "env_var":
        return "env"
    if c in ("network_endpoint", "listen_port", "runtime_download"):
        return "network"
    if c == "third_party_import":
        return "packages"
    if c == "device":
        return "system"
    if c in SYSTEM_CATS:
        return "system"
    if c in PROBLEM_CATS:
        return "problems"
    if c in PROJECT_CATS:
        return None
    return "system"


def run_state(f):
    """seen / ran / not_run / runtime_only / None (no runtime data)"""
    r = f.get("runtime")
    if r:
        if f.get("required_static") is None and r.get("seen_by"):
            return "runtime_only"
        if f.get("label") == "derived":
            return None
        if r.get("seen_by") and f.get("label") == "confirmed":
            return "seen"
        if f.get("label") == "confirmed":
            return "ran"
        return "not_run"
    return "seen" if f.get("evidence") == "confirmed" else None


RUN_TXT = {"seen": "seen running", "ran": "its line ran", "not_run": "did not run",
           "runtime_only": "found only by running"}


def used(f):
    return f.get("required_at_runtime") in ("yes", "conditional") or run_state(f) in ("seen", "runtime_only")


def entries_of(d):
    out = []
    for e in d.get("entrypoints") or []:
        out.append(e if isinstance(e, str) else e.get("path"))
    return [e for e in out if e]


def build(d, show_all=False):
    facts = d.get("facts") or []
    graph = d.get("graph") or {}
    mods = {n["label"]: n for n in graph.get("nodes", []) if n.get("type") == "module"}
    kids = {}
    for e in graph.get("edges", []):
        if e.get("type") != "import":
            continue
        s, t = e["source"][4:], e["target"][4:]
        if show_all or e.get("required") != "no":
            kids.setdefault(s, []).append(t)
    per_file, project = {}, []
    for f in facts:
        if f.get("negative"):
            continue
        if f["category"] in PROJECT_CATS:
            project.append(f)
            continue
        b = bundle_of(f)
        if b is None or (not show_all and not used(f)):
            continue
        files = sorted({s["file"] for s in f.get("sites") or [] if s.get("file", "").endswith(".py")})
        if not files:
            project.append(dict(f, _bundle=b))
            continue
        for fn in files:
            per_file.setdefault(fn, {}).setdefault(b, []).append(f)

    shown = set()

    def node(path, seen):
        if path in shown:
            return {"file": path, "shown_above": True}
        shown.add(path)
        n = {"file": path, "required": (mods.get(path) or {}).get("required"),
             "bundles": {b: [slim(f, path) for f in per_file.get(path, {}).get(b, [])]
                         for b, _, _ in BUNDLES if per_file.get(path, {}).get(b)},
             "imports": []}
        seen = seen | {path}
        for k in sorted(set(kids.get(path, []))):
            if k.endswith("__init__.py") and not per_file.get(k) and not kids.get(k):
                continue          # empty package __init__: noise in the tree
            if k in seen:
                n["imports"].append({"file": k, "cycle": True})
            else:
                n["imports"].append(node(k, seen))
        return n

    roots = [node(e, set()) for e in entries_of(d)]
    return {"tool": "xray-needs", "version": VERSION, "source": d.get("tool"), "entrypoints": entries_of(d),
            "tree": roots, "project": [slim(f, None) for f in project], "needs": needs(d, facts, show_all)}


def slim(f, path):
    lines = [s.get("line") for s in f.get("sites") or [] if s.get("file") == path and s.get("line")]
    d = f.get("detail")
    return {"id": f.get("id"), "category": f["category"], "subject": f.get("subject"),
            "required": f.get("required_at_runtime"), "run": run_state(f), "lines": lines,
            "detail": " ".join(d) if isinstance(d, list) else d, "docker": f.get("docker_implication"),
            "default": f.get("default"), "bundle": f.get("_bundle")}


def needs(d, facts, show_all):
    """What a person must provide to run it in a container, with the docker flags."""
    runtime_only = d.get("tool") == "xray-trace"
    use = [f for f in facts if not f.get("negative") and used(f)]
    out = {"cameras": [], "screen": None, "env": [], "network": [], "models": [], "data": [], "writes": [],
           "system": [], "code_changes": []}
    cams = {}
    for f in use:
        if not is_camera(f):
            continue
        s = str(f["subject"])
        site = ", ".join(f"{x['file']}:{x['line']}" for x in (f.get("sites") or [])[:3] if x.get("line"))
        if s.startswith("/dev/video") or s.startswith("camera:"):
            n = re.sub(r"\D", "", s) or "0"
            cams.setdefault(("webcam", n), {"kind": "webcam", "index": n, "where": site,
                                            "linux": f"--device /dev/video{n}",
                                            "windows": f"no webcam in Docker Desktop: trace with --camera {n}=/data/your_video.mp4",
                                            "run": run_state(f)})
        elif s.startswith("<") or "<" in s or f.get("mechanism") == "data_driven":
            m = re.search(r"seed data lists (.*?)(;|$)", detail_txt(f))
            seen = list(dict.fromkeys(re.findall(r"[a-z]+://[^\s,;()]+", m.group(1)))) if m else []
            cams.setdefault(("computed", site), {
                "kind": "computed", "pattern": s, "where": site, "run": run_state(f), "urls": seen,
                "note": (f"{len(seen)} camera URLs found in the project's data files (e.g. DB seed): "
                         + ", ".join(seen[:4]) + (" ..." if len(seen) > 4 else "")) if seen else
                        "the source is a variable (a list, a config, a DB row...): look at that line"})
        else:
            host = re.sub(r"^[a-z]+://([^/@]*@)?", "", s).split("/")[0].split(":")[0]
            local = host in ("localhost", "127.0.0.1", "0.0.0.0")
            cams.setdefault(("stream", s), {"kind": "stream", "url": s, "where": site, "run": run_state(f),
                                            "note": ("localhost inside a container is the container itself: "
                                                     "use the host's IP / host.docker.internal (code or env change)")
                                            if local else "nothing to pass - the container reaches it over the "
                                                          "network like any program"}
                            if re.match(r"^[a-z]+://", s) else
                            {"kind": "file", "url": s, "where": site, "run": run_state(f),
                             "note": "a video FILE, not a stream: mount its folder with -v and pass the path "
                                     "(env var / argument)"})
    out["cameras"] = list(cams.values())
    gui = [f for f in use if f["category"] == "gui_usage"]
    if gui:
        maybe = all(f.get("required_at_runtime") != "yes" for f in gui) and not any(
            run_state(f) == "seen" for f in gui)
        out["screen"] = {"calls": sorted({str(f["subject"]) for f in gui}), "maybe": maybe,
                         "trace": "--gui off (X-Ray saves the shown frames every 5 s to <trace>/gui/)",
                         "linux_window": "-e DISPLAY -v /tmp/.X11-unix:/tmp/.X11-unix  (needs a desktop)"}
    envs = {}
    code_sets = sorted({f["subject"] for f in use if f["category"] == "env_var" and set_by_code(f)})
    for f in use:
        if f["category"] == "env_var" and not side_effect(f) and not set_by_code(f):
            e = envs.setdefault(f["subject"], {"name": f["subject"], "default": f.get("default"),
                                               "required": f.get("required_at_runtime"), "where": [],
                                               "deploy": f.get("values_in_deploy_files")})
            e["where"] += [f"{x['file']}:{x['line']}" for x in f.get("sites") or [] if x.get("line")]
    for e in envs.values():
        has_def = e["default"] not in (None, "")
        pc_path = has_def and bool(re.match(r"^([A-Za-z]:[\\/]|/(home|Users)/)", str(e["default"])))
        e["flag"] = f"-e {e['name']}=" + ("<path inside the container>" if pc_path else
                                          str(e["default"]) if has_def else "<value>")
        e["why"] = (f"MUST be set in Docker: the default {e['default']!r} is a path on your PC - mount the file's "
                    "folder with -v and pass the path inside the container" if pc_path else
                    f"code default {e['default']!r} - pass it only to change it" if has_def else
                    ("read by the code (its default is in the Step 1 report)" if runtime_only else
                     "no default in the code - you must set it") if e["required"] == "yes" else "optional")
    out["env_set_by_code"] = code_sets
    out["_pc_defaults"] = [str(e["default"]) for e in envs.values()
                           if e["default"] and re.match(r"^([A-Za-z]:[\\/]|/(home|Users)/)", str(e["default"]))]
    out["env"] = list(envs.values())
    for f in use:
        c, s = f["category"], str(f.get("subject"))
        if c in ("network_endpoint", "listen_port", "runtime_download") and not is_camera(f):
            out["network"].append({"what": CAT.get(c, c), "subject": s, "docker": f.get("docker_implication")})
            if c == "listen_port" and s.startswith(("127.0.0.1:", "localhost:")):
                site = ", ".join(f"{x['file']}:{x['line']}" for x in (f.get("sites") or [])[:2] if x.get("line"))
                out["code_changes"].append(f"{s} ({site}): the server only accepts connections from inside its own "
                                           "container - bind 0.0.0.0 (or read the host from an env var)")
        elif c == "model_file":
            out["models"].append(s)
        elif c in ("data_file_read", "config_file") and not s.startswith("<"):
            out["data"].append(s)
        elif c == "file_write" and not side_effect(f):
            out["writes"].append(s)
        elif c == "gpu_usage":
            out.setdefault("_gpu", []).append(f)
        elif c in ("subprocess_binary", "system_package_candidate", "ipc_shared_memory"):
            out["system"].append(f"{CAT.get(c, c)}: {s}")
        elif c in ("secret", "hardcoded_config") and f.get("docker_implication"):
            out["code_changes"].append(f"{s}: {f['docker_implication']}")
    gpu = out.pop("_gpu", [])
    if gpu:
        need = any(f.get("required_at_runtime") == "yes" and f.get("mechanism") not in ("gpu_probe", "env_default",
                                                                                          "provider_string") for f in gpu)
        out["system"].append(("GPU: wanted, no CPU fallback in the code - run with --gpus all (+ onnxruntime-gpu / CUDA torch); "
                               "without a GPU onnxruntime usually falls back to the CPU with a warning (Step 2 shows which ran) - "
                               if need else "GPU: used when available (the code falls back to the CPU without one) - "
                              "on a GPU machine build a GPU image and run with --gpus all. ") + f"({len(gpu)} places, e.g. {gpu[0]['subject']})")
    for k in ("models", "data", "writes", "system", "code_changes"):
        out[k] = sorted(set(out[k]))
    out["data"] = [x for x in out["data"] if x not in out["models"]]
    roots = []
    for w in out["writes"]:                       # /data/output + 5 files below it -> one line
        if not any(w == r or w.startswith(r.rstrip("/") + "/") for r in roots):
            roots.append(w)
    out["writes"] = [{"path": r, "inside": sum(1 for w in out["writes"] if w != r and w.startswith(r.rstrip("/") + "/"))}
                     for r in roots]
    return out


CAT = {"network_endpoint": "connects to", "listen_port": "listens on", "runtime_download": "downloads",
       "subprocess_binary": "runs program", "system_package_candidate": "Linux package",
       "gpu_usage": "GPU", "ipc_shared_memory": "shared memory"}


# ------------------------------------------------------------------ text report
def tag(x):
    bits = []
    if x.get("run"):
        bits.append(RUN_TXT[x["run"]])
    if x.get("required") == "conditional":
        bits.append("maybe")
    elif x.get("required") == "no":
        bits.append("not needed")
    return f"  [{', '.join(bits)}]" if bits else ""


def fmt_item(x):
    ln = f" (line {', '.join(map(str, x['lines'][:4]))})" if x.get("lines") else ""
    return f"{x['subject']}{ln}{tag(x)}"


def print_tree(n, pre="", out=print):
    out(f"{pre}{n['file']}" + ("   (imports back - see above)" if n.get("cycle") else
                               "   (see above)" if n.get("shown_above") else ""))
    if n.get("cycle") or n.get("shown_above"):
        return
    ind = pre.replace("└─ ", "   ").replace("├─ ", "│  ")
    items = [("b", b) for b, _, _ in BUNDLES if b in n["bundles"]] + [("i", k) for k in n["imports"]]
    for i, (kind, v) in enumerate(items):
        last = i == len(items) - 1
        br = "└─ " if last else "├─ "
        if kind == "i":
            print_tree(v, ind + br, out)
            continue
        xs = n["bundles"][v]
        if v in ("packages", "side") and len(xs) > 3:
            out(f"{ind}{br}{TITLE[v]} ({len(xs)}): " + ", ".join(sorted({str(x['subject']) for x in xs})))
            continue
        out(f"{ind}{br}{TITLE[v]}")
        sub = ind + ("   " if last else "│  ")
        for j, x in enumerate(xs):
            out(f"{sub}{'└─ ' if j == len(xs) - 1 else '├─ '}{fmt_item(x)}")


def print_needs(nd, out=print):
    out("\nTO RUN IT IN A CONTAINER, YOU PROVIDE")
    none = True
    for c in nd["cameras"]:
        none = False
        if c["kind"] == "webcam":
            out(f"  camera   webcam {c['index']} ({c['where']})")
            out(f"           Linux: {c['linux']}   |   Windows: {c['windows']}")
        elif c["kind"] == "stream":
            out(f"  camera   {c['url']} ({c['where']}) - {c['note']}")
        elif c["kind"] == "file":
            out(f"  video    {c['url']} ({c['where']}) - {c['note']}")
        else:
            out(f"  camera   {c['pattern']} ({c['where']}): URL computed at runtime")
            out(f"           {c['note']}")
    if nd["screen"]:
        none = False
        out(f"  screen   {', '.join(nd['screen']['calls'])}"
            + ("   (maybe: only in a code path that did not run / depends on a setting)" if nd["screen"]["maybe"] else ""))
        out(f"           trace: {nd['screen']['trace']}   |   real window on Linux: {nd['screen']['linux_window']}")
    for e in nd["env"]:
        none = False
        out(f"  env      {e['flag']:<40} {e['why']} ({', '.join(e['where'][:2])})")
    if nd.get("env_set_by_code"):
        out(f"  (env the code sets itself - nothing to pass: {', '.join(nd['env_set_by_code'])})")
    for n in nd["network"]:
        none = False
        out(f"  network  {n['what']} {n['subject']}" + (f" - {n['docker']}" if n.get("docker") else ""))
    # a PC path that is only an env var's default (C:/.../a.onnx) is not what the container uses: hide it when
    # the run saw the file under its container path
    pcs = [x.replace("\\", "/") for x in nd.get("_pc_defaults", [])]
    def overridden(m):
        m2 = str(m).replace("\\", "/")
        if not any(m2 == p or m2.startswith(p) for p in pcs):
            return False
        base = m2.rsplit("/", 1)[-1]
        return any(str(x).endswith("/" + base) and x != m for x in nd["models"] + nd["data"])
    for m in nd["models"]:
        if overridden(m):
            continue
        none = False
        out(f"  model    {m}   (must be inside the image or mounted)")
    for m in nd["data"]:
        if overridden(m):
            continue
        none = False
        out(f"  data     {m}   (must be inside the image or mounted)")
    for w in nd["writes"]:
        none = False
        out(f"  writes   {w['path']}" + (f" (+{w['inside']} paths inside)" if w["inside"] else "")
            + "   (mount it with -v to keep the files; the folder must be writable)")
    for s in nd["system"]:
        none = False
        out(f"  system   {s}")
    if none:
        out("  nothing - it runs with no inputs")
    out("\nCODE CHANGES NEEDED")
    out("  " + ("\n  ".join(nd["code_changes"]) if nd["code_changes"] else "none found"))


def main():
    for _s in (sys.stdout, sys.stderr):   # Windows consoles / pipes: never crash on tree characters or paths
        if hasattr(_s, "reconfigure"):
            _s.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("json", help="an X-Ray output: static map, merged map or runtime facts")
    ap.add_argument("--all", action="store_true", help="also show what the entry point never uses")
    ap.add_argument("--out", help="also save the tree + needs as JSON")
    a = ap.parse_args()
    d = json.load(open(a.json))
    if "facts" not in d:
        sys.exit(f"{a.json} has no facts - give it the output of xray_static.py, xray_trace.py facts or merge")
    r = build(d, a.all)
    step = {"xray-static": "Step 1 (static: read the code, nothing ran)",
            "xray-merged": "Steps 1+2 (the code + what really ran)",
            "xray-trace": "Step 2 only (what really ran)"}.get(d.get("tool"), d.get("tool"))
    print(f"X-Ray needs {VERSION} - {a.json}: {step}")
    print("Showing only what the entry point uses" + ("" if a.all else " (add --all for everything)") + "\n")
    for t in r["tree"]:
        print_tree(t)
        print()
    if not r["tree"]:
        print("(no entry points in this file)")
    print_needs(r["needs"])
    res = d.get("resources")
    if res:
        print("\nWHAT THE RUN USED (whole run, all processes)")
        print(f"  RAM      {res['peak_ram_mb_sum']:.0f} MB peak (sum of {res['processes']} process(es); largest one "
              f"{res['peak_ram_mb_max']:.0f} MB)")
        if (res.get("wall_s") or 0) >= 2:
            print(f"  CPU      {res['cpu_s']} CPU-seconds in {res['wall_s']} s = {res.get('avg_cpu_cores_busy')} cores busy on average")
        else:
            print(f"  CPU      {res['cpu_s']} CPU-seconds (the run was too short to say how many cores were busy)")
        if res.get("onnxruntime_ran_on"):
            on = res["onnxruntime_ran_on"]
            gpu = any("CUDA" in x or "Tensorrt" in x for x in on)
            print(f"  GPU      onnxruntime ran on {', '.join(on)} ({'the GPU' if gpu else 'the CPU - no GPU used'})"
                  + ("; its GPU memory is not measured here: see nvidia-smi or the app's own log" if gpu else ""))
        if res.get("gpu_peak_mb_torch"):
            print(f"  GPU      {res['gpu_peak_mb_torch']:.0f} MB reserved by torch")
        elif not res.get("onnxruntime_ran_on"):
            print("  GPU      no GPU use seen (torch / onnxruntime); other libraries: watch nvidia-smi")
    unused = [x for x in r["project"] if x["category"] == "unused_asset"]
    if unused:
        print(f"\nNOT USED BY THIS ENTRY POINT ({len(unused)}): " + ", ".join(str(x["subject"]) for x in unused[:12])
              + (" ..." if len(unused) > 12 else "") + "   -> left out of the image")
    if a.out:
        json.dump(r, open(a.out, "w", encoding="utf-8"), indent=1)
        print(f"\n-> {a.out}")


if __name__ == "__main__":
    main()
