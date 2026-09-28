#!/usr/bin/env python3
"""
xray_compare.py - X-Ray Phase 5: comparator. Stdlib only, Python 3.8+.

Compares two (or more) planned projects and proposes ONE shared base image:
  - which Python distributions both need (directly or as dependencies), in which versions, and which versions
    ALL declared constraints accept (Requires-Dist from the package scans, with markers evaluated for Linux/x86_64
    and the image's Python) -> the pin changes that let them share, and what cannot be shared;
  - byte-identical files in the images (sha256 from the package scans), shared persistent folders, twin entrypoints;
  - a base Dockerfile (python:3.x-slim + shared packages + the app user) and a Dockerfile per project FROM it.

  compare  python3 xray_compare.py compare --project F2_cv_api:pkg-f2/pkgscan.json:plans/F2_cv_api/plan.json \\
               --project F3_stream_worker:pkg-f3/pkgscan.json:plans/F3_stream_worker/plan.json --out plans/shared
  grade    python3 xray_compare.py grade --report plans/shared/compare.json --key F4_cross/answer_key_cross.json
"""
import argparse
import json
import os
import re
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import xray_plan  # noqa: E402

VERSION = "0.1.0"
BASE_TAG = "xray-base:latest"


def jload(p):
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)


canon = xray_plan.canon


# ============================================================================ versions / specifiers / markers
def vkey(v):
    """'2.0.2' < '2.0.10'; pre-releases sort before the release; '+cpu' local part ignored."""
    v = str(v).split("+")[0]
    m = re.match(r"^(\d+(?:\.\d+)*)(?:(a|b|rc)(\d+))?(?:\.post(\d+))?", v)
    if not m:
        return ((0,), 0, 0, 0)
    rel = tuple(int(x) for x in m.group(1).split("."))
    rel = rel + (0,) * (4 - len(rel))
    pre = {"a": -3, "b": -2, "rc": -1}.get(m.group(2), 0)
    return (rel, pre, int(m.group(3) or 0), int(m.group(4) or 0))


def spec_ok(version, spec):
    """'>=1.21.6,<2.0' / '==2.0.*' / '~=1.4' / '!=1.5'"""
    for part in [p.strip() for p in spec.split(",") if p.strip()]:
        m = re.match(r"^(===|==|!=|<=|>=|<|>|~=)\s*(\S+)$", part)
        if not m:
            continue
        op, want = m.groups()
        if want.endswith(".*"):
            pref = want[:-2]
            same = str(version).split("+")[0] == pref or str(version).startswith(pref + ".")
            if (op == "==" and not same) or (op == "!=" and same):
                return False
            continue
        a, b = vkey(version), vkey(want)
        if op == "~=":
            parts = want.split(".")
            upper = ".".join(parts[:-2] + [str(int(parts[-2]) + 1)]) if len(parts) > 1 else str(int(parts[0]) + 1)
            ok = a >= b and a < vkey(upper)
        else:
            ok = {"==": a == b, "===": str(version) == want, "!=": a != b, "<=": a <= b, ">=": a >= b,
                  "<": a < b, ">": a > b}[op]
        if not ok:
            return False
    return True


def marker_ok(marker, py="3.11"):
    """Evaluate a PEP 508 marker for Linux / x86_64 / CPython <py>; extras are never requested."""
    if not marker.strip():
        return True
    env = {"python_version": py, "python_full_version": py + ".0", "platform_system": "Linux",
           "sys_platform": "linux", "platform_machine": "x86_64", "os_name": "posix",
           "platform_python_implementation": "CPython", "implementation_name": "cpython", "extra": None}

    def atom(s):
        s = s.strip()
        while s.startswith("(") and s.endswith(")"):
            s = s[1:-1].strip()
        m = re.match(r"""^(\w+)\s*(==|!=|<=|>=|<|>|in|not in)\s*['"]([^'"]*)['"]$""", s)
        if not m:
            m2 = re.match(r"""^['"]([^'"]*)['"]\s*(==|!=|in|not in)\s*(\w+)$""", s)
            if not m2:
                return True
            val, op, var = m2.groups()
            left, right = val, env.get(var)
        else:
            var, op, val = m.groups()
            left, right = env.get(var), val
        if left is None or right is None:
            return False
        if var in ("python_version", "python_full_version") and op in ("<", "<=", ">", ">=", "==", "!="):
            return spec_ok(left, f"{op}{right}")
        return {"==": left == right, "!=": left != right, "in": left in right, "not in": left not in right,
                "<": left < right, ">": left > right, "<=": left <= right, ">=": left >= right}[op]

    def expr(s):
        depth, parts, cur = 0, [], ""
        tokens = re.split(r"(\(|\)|\s+or\s+)", s)
        for t in tokens:
            if t is None:
                continue
            if t == "(":
                depth += 1
            elif t == ")":
                depth -= 1
            if depth == 0 and re.fullmatch(r"\s+or\s+", t or ""):
                parts.append(cur)
                cur = ""
            else:
                cur += t
        parts.append(cur)
        if len(parts) > 1:
            return any(expr(p) for p in parts)
        ands = re.split(r"\s+and\s+", s)
        if len(ands) > 1 and all(a.count("(") == a.count(")") for a in ands):
            return all(expr(a) for a in ands)
        return atom(s)
    try:
        return expr(marker)
    except Exception:
        return True


def parse_req(r):
    """'numpy (<2.0,>=1.21.6) ; python_version >= "3.10"' -> ('numpy', '<2.0,>=1.21.6', marker)"""
    body, _, marker = r.partition(";")
    m = re.match(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[[^\]]*\])?\s*\(?([^)]*)\)?\s*$", body)
    if not m:
        return None, "", marker
    return canon(m.group(1)), m.group(3).strip(), marker


# ============================================================================ compare
class Project:
    def __init__(self, name, scan, plan):
        self.name, self.scan, self.plan = name, scan, plan
        self.py = str(scan.get("python", "3.11"))[:4].rstrip(".")
        self.pins = {}              # dist -> version the plan installs
        self.cpu = set()
        for p in plan.get("pip_install", []):
            n, _, v = p.partition("==")
            n = canon(n)
            if v.endswith("+cpu"):
                self.cpu.add(n)
            self.pins[n] = v

    def version_of(self, name):
        if name in self.pins:
            return self.pins[name].split("+")[0]
        d = self.scan["dists"].get(name)
        return d["version"] if d else None


def build_meta(projects):
    meta = {}          # (name, version) -> requires
    for p in projects:
        for n, d in p.scan["dists"].items():
            meta.setdefault((n, d["version"]), d.get("requires") or [])
    return meta


def closure(p, meta, versions=None):
    """distribution -> version the project ends up with (plan pins + their dependencies, markers evaluated)."""
    versions = versions or {}
    out, stack = {}, list(p.pins)
    while stack:
        n = stack.pop()
        if n in out:
            continue
        v = versions.get(n) or p.version_of(n)
        if v is None:
            continue
        out[n] = v
        for r in meta.get((n, v), []) or meta.get((n, p.version_of(n)), []):
            dep, spec, marker = parse_req(r)
            if not dep or not marker_ok(marker, p.py):
                continue
            if n in p.cpu and dep.startswith(("nvidia-", "triton")):
                continue            # the CPU build of torch has no CUDA dependencies
            stack.append(dep)
    return out


def violations(name, version, closures, meta, py):
    """which installed distributions forbid name==version"""
    bad = []
    for clo in closures:
        for d, dv in clo.items():
            for r in meta.get((d, dv), []):
                dep, spec, marker = parse_req(r)
                if dep == name and spec and marker_ok(marker, py) and not spec_ok(version, spec):
                    bad.append((d, dv, spec))
    return bad


def unify(projects, meta):
    """Pick one version per shared distribution that every declared constraint accepts; change pins if needed."""
    choice = {}
    changes = defaultdict(list)       # project -> [(dist, old, new, why)]
    notes = []
    for _ in range(6):
        clos = [closure(p, meta, choice) for p in projects]
        shared = sorted(set(clos[0]).intersection(*clos[1:]))
        changed = False
        for n in shared:
            # a version a project pins on purpose beats one that only arrived as a dependency; then newest first
            pinned = {p.pins[n].split("+")[0] for p in projects if n in p.pins}
            cands = sorted({c[n] for c in clos}, key=lambda v: (v in pinned, vkey(v)), reverse=True)
            if len(cands) == 1:
                if choice.get(n) != cands[0]:
                    choice[n] = cands[0]
                    changed = True
                continue
            picked = None
            for v in cands:
                bad = violations(n, v, clos, meta, projects[0].py)
                fixable = True
                for d, dv, spec in bad:
                    # the forbidding package has another version in some project that allows v: move to it
                    alts = [c[d] for c in clos if d in c and c[d] != dv]
                    ok_alt = [a for a in alts if not any(
                        dep == n and spec2 and marker_ok(mk, projects[0].py) and not spec_ok(v, spec2)
                        for dep, spec2, mk in (parse_req(r) for r in meta.get((d, a), [])))]
                    if ok_alt:
                        choice[d] = sorted(ok_alt, key=vkey, reverse=True)[0]
                        notes.append(f"{d} {dv} forbids {n} {v} ({spec}); {d} {choice[d]} allows it")
                    else:
                        fixable = False
                if fixable:
                    picked = v
                    break
            if picked is None:
                notes.append(f"{n}: no single version satisfies both projects ({', '.join(cands)})")
                choice.pop(n, None)
                continue
            if choice.get(n) != picked:
                choice[n] = picked
                changed = True
        if not changed:
            break
    clos = [closure(p, meta, choice) for p in projects]
    shared = sorted(n for n in set(clos[0]).intersection(*clos[1:]) if n in choice)
    for p, base in zip(projects, [closure(p, meta) for p in projects]):
        for n in shared:
            if base.get(n) and base[n] != choice[n]:
                why = "pinned by the plan" if n in p.pins else "installed as a dependency"
                changes[p.name].append({"dist": n, "from": base[n], "to": choice[n], "kind": why})
    return choice, shared, dict(changes), notes, clos


def size_of(n, v, projects):
    for p in projects:
        d = p.scan["dists"].get(n)
        if d and d["version"] == v:
            return d["size_bytes"]
    for p in projects:
        d = p.scan["dists"].get(n)
        if d:
            return d["size_bytes"]
    return 0


def cmd_compare(a):
    projects = []
    for spec in a.project:
        name, scan, plan = spec.split(":")
        projects.append(Project(name, jload(scan), jload(plan)))
    meta = build_meta(projects)
    choice, shared, changes, notes, clos = unify(projects, meta)
    naive = [closure(p, meta) for p in projects]
    # packages the projects' plans pin, side by side
    pinned = sorted(set().union(*(p.pins for p in projects)))
    table = []
    for n in pinned:
        row = {"package": n}
        for p, c in zip(projects, naive):
            row[p.name] = (p.pins.get(n) or c.get(n))
        vs = {v.split("+")[0] for k, v in row.items() if k != "package" and v}
        row["conflict"] = len(vs) > 1
        row["shared_after_unify"] = n in shared
        if n in choice:
            row["unified_version"] = choice[n]
        table.append(row)
    # the requirements files as written (psycopg2 vs psycopg2-binary, numpy 1 vs 2)
    declared = defaultdict(dict)
    for p in projects:
        for r in p.scan.get("requirements", []):
            m = re.match(r"==\s*(\S+)", r.get("spec") or "")
            declared[r["name"]][p.name] = m.group(1) if m else (r.get("spec") or "*")
    declared_rows = []
    for n, by in sorted(declared.items()):
        vs = set(by.values())
        declared_rows.append({"package": n, **by, "in_all": len(by) == len(projects),
                              "conflict": len(by) == len(projects) and len(vs) > 1})
    # coupling: what forbids the other project's version
    coupling = []
    for n in {r["package"] for r in declared_rows if r["conflict"]} | {r["package"] for r in table if r["conflict"]}:
        for p in projects:
            other = [q for q in projects if q is not p]
            for q in other:
                qv = q.version_of(n)
                if not qv:
                    continue
                for d, dv, spec in violations(n, qv, [naive[projects.index(p)]], meta, p.py):
                    coupling.append(f"{p.name}: {d} {dv} requires {n} {spec} -> {q.name}'s {n} {qv} is not allowed")
    # identical files across the projects
    by_hash = defaultdict(list)
    for p in projects:
        for f in p.scan.get("files", []):
            if f["size"] > 0 and not f["path"].endswith((".py", ".pyc", ".md", ".txt", ".yml", ".sh", ".json")):
                by_hash[f["sha256"]].append({"project": p.name, "path": f["path"], "size": f["size"]})
    identical = [{"sha256": h[:16], "size": v[0]["size"], "copies": v} for h, v in by_hash.items()
                 if len({x["project"] for x in v}) > 1]
    # persistent folders and twin entrypoints
    vols = {p.name: p.plan.get("volumes", []) for p in projects}
    twins = {p.name: p.plan.get("images", {}).get("services", []) for p in projects
             if len(p.plan.get("images", {}).get("services", [])) > 1}
    folders = []
    for p in projects:
        for v in vols[p.name]:
            folders.append({"project": p.name, "container_path": v["container_path"], "env": v.get("for_env"),
                            "why": v.get("why")})
    # base image contents
    py = projects[0].py
    base_pkgs = [n for n in shared]
    base_pins = [f"{n}=={choice[n]}" for n in shared]     # every shared distribution, pinned: stored once
    extra_index = next((p.plan.get("pip_extra_index") for p in projects
                        if p.plan.get("pip_extra_index") and any(n in p.cpu for n in shared)), None)
    shared_bytes = sum(size_of(n, choice[n], projects) for n in base_pkgs)
    apt_sets = [set(x["installed_as"] for x in p.plan.get("apt_packages", [])) for p in projects]
    apt_shared = sorted(set.intersection(*apt_sets)) if apt_sets else []
    report = {"tool": "xray-compare", "version": VERSION, "projects": [p.name for p in projects],
              "python": py, "base_image": f"python:{py}-slim",
              "declared_requirements": declared_rows, "planned_packages": table,
              "version_coupling": sorted(set(coupling)),
              "unified": {"shared_distributions": {n: choice[n] for n in shared}, "pin_changes": changes,
                          "notes": notes, "shared_bytes": shared_bytes},
              "identical_files": identical, "persistent_folders": folders, "twin_entrypoints": twins,
              "base": {"tag": BASE_TAG, "from": f"python:{py}-slim", "apt": apt_shared, "pip": base_pins,
                       "extra_index": extra_index, "user": xray_plan.APP_USER, "uid": xray_plan.APP_UID},
              "layer_order": ["python:slim", "shared apt (if any)", "shared pip packages", "app user",
                              "project apt", "project pip packages", "project folders", "project code"],
              "measure": ["docker system df -v", "docker image inspect --format '{{json .RootFS.Layers}}' IMAGE",
                          "docker images"]}
    report["shared_folder_hint"] = shared_folder_hint(projects, folders)
    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "compare.json"), "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=1)
    write_dockerfiles(a.out, report, projects, choice, shared)
    print(f"xray-compare {VERSION}: {', '.join(report['projects'])} -> {a.out}/compare.json")
    print("  requirements as written, in both: " + ", ".join(
        f"{r['package']} ({' vs '.join(str(r[p.name]) for p in projects)})" + (" CONFLICT" if r["conflict"] else "")
        for r in declared_rows if r["in_all"]))
    for c in report["version_coupling"]:
        print(f"  coupling: {c}")
    print(f"  shared after unifying ({len(shared)} distributions, {shared_bytes / 1e6:.0f} MB stored once): "
          + ", ".join(f"{n}=={choice[n]}" for n in shared))
    for pn, ch in changes.items():
        print(f"  pin changes for {pn}: " + ", ".join(f"{c['dist']} {c['from']} -> {c['to']}" for c in ch))
    for x in identical:
        print("  identical file in several projects: " + ", ".join(f"{c['project']}:{c['path']}" for c in x["copies"]))
    print(f"  twin entrypoints: {twins or 'none'}")
    print(f"  {report['shared_folder_hint']}")
    print(f"  wrote {a.out}/Dockerfile.base and plans/<project>/Dockerfile.shared + compose.shared.yml")
    return 0


def shared_folder_hint(projects, folders):
    """A folder one project writes images/data into and another project serves files from."""
    by = defaultdict(list)
    for f in folders:
        by[f["project"]].append(f)
    if len(by) >= 2:
        parts = [f"{p}: {', '.join(x['container_path'] + (' ($' + x['env'] + ')' if x.get('env') else '') for x in v)}"
                 for p, v in by.items()]
        return ("persistent folders - " + "; ".join(parts) + ". If one project serves the files another writes "
                "(F2 serves MEDIA_ROOT over GET /media), mount ONE named volume in both services and point the "
                "writer's env var at the reader's folder (e.g. OUTPUT_ROOT=/data/media); both images use uid "
                f"{xray_plan.APP_UID}, so file ownership already matches.")
    return "no shared persistent folder candidates"


def write_dockerfiles(out, report, projects, choice, shared):
    b = report["base"]
    L = [f"# Generated by xray_compare.py {VERSION}: shared base for {', '.join(report['projects'])}.",
         f"# Build:  docker build -t {BASE_TAG} -f plans/shared/Dockerfile.base plans/shared",
         f"FROM {b['from']}", "ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1"]
    if b["apt"]:
        L.append("RUN apt-get update && apt-get install -y --no-install-recommends " + " ".join(b["apt"])
                 + " && rm -rf /var/lib/apt/lists/*")
    idx = f"--extra-index-url {b['extra_index']} " if b.get("extra_index") else ""
    L.append("# packages every project needs, at versions all their declared constraints accept")
    L.append(f"RUN pip install --no-cache-dir {idx}\\\n    " + " \\\n    ".join(b["pip"]))
    L.append(f"RUN useradd --create-home --uid {b['uid']} {b['user']}")
    with open(os.path.join(out, "Dockerfile.base"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(L) + "\n")
    plans_dir = os.path.dirname(os.path.abspath(out))
    for p in projects:
        plan = json.loads(json.dumps(p.plan))
        plan["base_image"] = {"choice": BASE_TAG, "why": "shared base"}
        pins = []
        for pin in plan["pip_install"]:
            n = canon(pin.split("==")[0])
            if n in choice:
                v = choice[n] + ("+cpu" if n in p.cpu else "")
                pins.append(f"{pin.split('==')[0]}=={v}")
            else:
                pins.append(pin)
        # shared dependencies this project used to get in another version: pin them too
        for c in report["unified"]["pin_changes"].get(p.name, []):
            if c["kind"] == "installed as a dependency" and not any(canon(x.split("==")[0]) == c["dist"] for x in pins):
                pins.append(f"{c['dist']}=={c['to']}")
        plan["pip_install"] = pins
        txt = xray_plan.dockerfile(plan)
        txt = re.sub(r"RUN useradd --create-home --uid \d+ \w+ \\\n && ", "RUN ", txt)
        txt = re.sub(r"RUN useradd --create-home --uid \d+ \w+\n", "", txt)
        txt = txt.replace(f"# Generated by xray_plan.py {xray_plan.VERSION}",
                          f"# Generated by xray_compare.py {VERSION} (FROM the shared base) from xray_plan.py")
        pdir = os.path.join(plans_dir, p.name)
        os.makedirs(pdir, exist_ok=True)
        with open(os.path.join(pdir, "Dockerfile.shared"), "w", encoding="utf-8") as fh:
            fh.write(txt)
        src_ign = os.path.join(pdir, "Dockerfile.dockerignore")
        if os.path.exists(src_ign):
            with open(src_ign, encoding="utf-8") as fh, \
                    open(os.path.join(pdir, "Dockerfile.shared.dockerignore"), "w", encoding="utf-8") as g:
                g.write(fh.read())
        comp = os.path.join(pdir, "compose.xray.yml")
        if os.path.exists(comp):
            with open(comp, encoding="utf-8") as fh:
                c = fh.read()
            short = p.name.split("_")[0].lower()
            c = c.replace(f"image: {short}-xray", f"image: {short}-shared").replace(
                f"plans/{p.name}/Dockerfile\n", f"plans/{p.name}/Dockerfile.shared\n").replace(
                "compose.xray.yml", "compose.shared.yml")
            with open(os.path.join(pdir, "compose.shared.yml"), "w", encoding="utf-8") as fh:
                fh.write(c)


# ============================================================================ grade (F4 key)
def cmd_grade(a):
    r, k = jload(a.report), jload(a.key)
    rows = []

    def row(name, ok, detail):
        rows.append((name, ok, detail))
    kp = {canon(x["package"].split(" /")[0]): x for x in k.get("shared_packages", {}).get("packages", [])}
    dec = {x["package"]: x for x in r["declared_requirements"]}
    plan = {x["package"]: x for x in r["planned_packages"]}
    for n, kx in kp.items():
        if n == "pillow":
            ok = not (dec.get("pillow") or {}).get("in_all", False)
            row("pillow F3-only", ok, "not declared by F2 (arrives there only as a dependency of scikit-image)")
            continue
        got = dec.get(n) or dec.get(n + "-binary") or plan.get(n) or plan.get(n + "-binary")
        conflict = bool(got and got.get("conflict"))
        if n == "psycopg2":
            both = "psycopg2" in dec and "psycopg2-binary" in dec
            row("psycopg2 vs psycopg2-binary", both and not kx["conflict"],
                "F2 declares psycopg2 (source), F3 psycopg2-binary, same version 2.9.9: the plan swaps F2 to -binary")
            continue
        row(f"{n} conflict={kx['conflict']}", conflict == bool(kx["conflict"]), f"report conflict={conflict}")
    coup = " ".join(r["version_coupling"])
    row("numpy coupled to onnxruntime", "onnxruntime" in coup and "numpy" in coup, coup[:160])
    ids = [x for x in r["identical_files"] if any("arcface.onnx" in c["path"] for c in x["copies"])]
    row("arcface.onnx byte-identical across projects", bool(ids) and len(ids[0]["copies"]) >= 3,
        f"{len(ids[0]['copies']) if ids else 0} copies: " + (", ".join(c["project"] + ":" + c["path"]
                                                                    for c in ids[0]["copies"]) if ids else ""))
    tw = r.get("twin_entrypoints", {})
    row("F2 twin entrypoints -> one image", any(len(v) == 2 for v in tw.values()), json.dumps(tw))
    fold = r.get("shared_folder_hint", "")
    row("shared media folder (F3 OUTPUT_ROOT / F2 MEDIA_ROOT)", "OUTPUT_ROOT" in fold and "/data/media" in fold,
        fold[:160])
    row("base image python:3.11-slim", r["base"]["from"] == k["proposed_shared_base_image"]["base"],
        f"{r['base']['from']}")
    lo = r.get("layer_order", [])
    row("layer order: shared first, code last", lo and "shared" in lo[1] + lo[2] and lo[-1] == "project code",
        " > ".join(lo))
    row("measuring commands", any("system df" in m for m in r.get("measure", [])), "; ".join(r.get("measure", [])))
    ok = sum(1 for x in rows if x[1])
    print(f"# X-Ray compare grade vs F4_cross - {ok}/{len(rows)} checks agree")
    for n, g, d in rows:
        print(f"  {'OK  ' if g else 'DIFF'} {n:<48} {d}")
    return 0


def main(argv=None):
    for _s in (sys.stdout, sys.stderr):   # Windows consoles / pipes: never crash on tree characters or paths
        if hasattr(_s, "reconfigure"):
            _s.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="X-Ray Phase 5 comparator")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("compare")
    c.add_argument("--project", action="append", required=True, help="NAME:pkgscan.json:plan.json (2 or more)")
    c.add_argument("--out", required=True)
    g = sub.add_parser("grade")
    g.add_argument("--report", required=True)
    g.add_argument("--key", required=True)
    a = ap.parse_args(argv)
    return {"compare": cmd_compare, "grade": cmd_grade}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
