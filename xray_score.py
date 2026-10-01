#!/usr/bin/env python3
"""
xray_score.py - grade an X-Ray output file against a fixture answer_key.json.

  python xray_score.py --key F1_import_maze/answer_key.json --out xray_static.json

What it reports
  in-scope recall   key facts this layer SHOULD see (visible_to contains the layer) that it found
  bonus             key facts it found although the key says only another layer can see them
  missed            in-scope key facts not found  -> bugs / limitations to fix
  negatives         decoys (negative: true) wrongly reported as runtime needs -> false positives
  required          agreement of required_at_runtime (yes / conditional / no) on found facts
  extras            output facts matching no key fact (either key gaps or real false positives)
Writes a JSON report next to the output (or --report).
"""
import argparse
import json
import re
from collections import Counter, defaultdict

FAMILY = {"local_module": "import", "third_party_import": "import", "dynamic_import": "import",
          "stdlib_import": "import", "config_file": "file_read", "data_file_read": "file_read",
          "model_file": "file_read"}
LAYER_SEES = {"static": {"static", "static_partial"},
              "runtime": {"py_hook", "py_wrapper", "os_trace"},
              "package": {"pkg_analyzer"}}
LAYER_SEES["merged"] = LAYER_SEES["static"] | LAYER_SEES["runtime"]   # xray_trace.py merge output
FOUND = ("found", "found_line_off", "found_other_site")
# key categories the key is expected to list exhaustively -> unmatched output facts are "possible FP"
EXHAUSTIVE = {"third_party_import", "env_var", "secret", "listen_port", "network_endpoint", "file_write"}


def fam(c):
    return FAMILY.get(c, c)


PLACEHOLDER = re.compile(r"<[^<>]*>")
SUFFIX_CATS = {"gpu_usage", "os_data", "hardcoded_config", "thread_config", "hardware_identity", "device",
               "multiprocessing"}
ASSIGN = re.compile(r"^([a-z_][a-z0-9_]*)\s*=")


def norm(s):
    """Compare names loosely: forward slashes, no ./ prefix or trailing /, any <placeholder> == <*>,
    a trailing call "()" dropped (uuid.getnode() == uuid.getnode)."""
    s = str(s).strip().replace("\\", "/")
    while s.startswith("./"):
        s = s[2:]
    if len(s) > 1:
        s = s.rstrip("/")
    if s.endswith("()"):
        s = s[:-2]
    return PLACEHOLDER.sub("<*>", s).lower()


def alts(v):
    """Key subjects may list alternatives: "DejaVuSans.ttf / fonts-dejavu-core"."""
    return [x for x in str(v).split(" / ") if x.strip()] if " / " in str(v) else [v]


def idents(f):
    out = set()
    for k in ("subject", "resolved_path", "import_name", "value"):
        v = f.get(k)
        if v not in (None, ""):
            for a in alts(v):
                out.add(norm(a))
    for v in f.get("aliases") or []:   # runtime: "MODEL_CACHE/x.onnx" for /root/.cache/xray_models/x.onnx
        out.add(norm(v))
    for v in list(out):   # 'DB_PASSWORD = "FAKE..."' / 'GPU_ID = 0' name the variable
        m = ASSIGN.match(v)
        if m:
            out.add(m.group(1))
    cat = f.get("category")
    if cat == "listen_port":  # "5004" == "0.0.0.0:5004"
        out |= {v.rsplit(":", 1)[-1] for v in out}
    if cat in SUFFIX_CATS:    # "zoneinfo:Asia/Kolkata" == "tzdata:Asia/Kolkata"
        out |= {v.rsplit(":", 1)[-1] for v in out if ":" in v and "://" not in v}
    return out


def strength(k, o):
    """3 = same subject, 2 = same resolved path, 1 = same import name / bare filename, 0 = no match."""
    if norm(k.get("subject", "")) == norm(o.get("subject", "")):
        return 3
    kr, orr = norm(k.get("resolved_path") or ""), norm(o.get("resolved_path") or "")
    ks, os_ = norm(k.get("subject", "")), norm(o.get("subject", ""))
    if (kr and kr in (orr, os_)) or (orr and orr == ks):
        return 2
    if idents(k) & idents(o):
        return 1
    for a in idents(k):  # key gave a bare filename, output a path
        if "/" not in a and "." in a and any(b.rsplit("/", 1)[-1] == a for b in idents(o) if "/" in b):
            return 1
    if k.get("category") == "unused_asset" and o.get("category") == "unused_asset":
        # a file inside a folder the output reports as unused as a whole (it lists the files in `paths`)
        ks = norm(k.get("subject", ""))
        if ks in {norm(p) for p in o.get("paths") or []}:
            return 1
        osub = norm(o.get("subject", ""))
        if str(o.get("subject", "")).endswith("/") and ks.startswith(osub + "/"):
            return 1
    return 0


def subject_match(k, o):
    return strength(k, o) > 0


def sites(f):
    return [(norm(s.get("file") or ""), s.get("line")) for s in f.get("sites", [])]


def site_overlap(k, o, tol=2):
    best = None
    for kf, kl in sites(k):
        for of, ol in sites(o):
            if kf and kf == of:
                if kl is None or ol is None or abs(kl - ol) <= tol:
                    return "exact"
                best = "file"
    return best


def req_agreement(kreq, oreq):
    if kreq == oreq:
        return "agree"
    if oreq == "conditional" and kreq in ("yes", "no"):
        return "undecidable_statically"
    return "disagree"


def score(key, out, layer):
    kf = key["facts"]
    of = out["facts"]
    sees = LAYER_SEES[layer]
    used = set()
    rows = []
    for k in kf:
        in_scope = bool(set(k.get("visible_to", [])) & sees)
        cands = [o for o in of if fam(o["category"]) == fam(k["category"])]
        best, status = None, "missed"
        subj = [o for o in cands if subject_match(k, o)]
        if k.get("negative"):
            hit = [o for o in subj if site_overlap(k, o) == "exact"]
            if not hit:
                status = "correctly_ignored"
            else:
                best = hit[0]
                status = ("FALSE_POSITIVE" if best.get("required_at_runtime") in ("yes", "conditional")
                          else "flagged_not_required")
        elif subj:
            by = {"exact": [], "file": [], None: []}
            for o in sorted(subj, key=lambda o: -strength(k, o)):
                by[site_overlap(k, o)].append(o)
            if by["exact"]:
                best, status = by["exact"][0], "found"
            elif by["file"]:
                best, status = by["file"][0], "found_line_off"
            else:
                best, status = subj[0], "found_other_site"
        else:
            site_only = [o for o in cands if site_overlap(k, o) == "exact"]
            if site_only:
                best, status = site_only[0], "site_only"
        if best is not None:
            used.add(best["id"])
        row = {"key_id": k["id"], "category": k["category"], "subject": k["subject"],
               "mechanism": k.get("mechanism"), "visible_to": k.get("visible_to", []),
               "in_scope": in_scope, "negative": bool(k.get("negative")), "status": status,
               "key_required": k.get("required_at_runtime"), "key_label": k.get("expected_label"),
               "key_sites": k.get("sites", []), "trap": k.get("trap", "")}
        if best is not None:
            row.update({"out_id": best["id"], "out_subject": best["subject"],
                        "out_required": best.get("required_at_runtime"), "out_evidence": best.get("evidence"),
                        "out_sites": best.get("sites", [])})
            if status in FOUND:
                row["required"] = req_agreement(k.get("required_at_runtime"), best.get("required_at_runtime"))
                if best.get("label"):
                    row["out_label"] = best["label"]
                    row["label"] = "agree" if best["label"] == k.get("expected_label") else "disagree"
        rows.append(row)
    extras = [o for o in of if o["id"] not in used]
    return rows, extras


def pct(a, b):
    return f"{(100.0 * a / b):.0f}%" if b else "n/a"


def report(key, out, layer, rows, extras):
    pos = [r for r in rows if not r["negative"]]
    neg = [r for r in rows if r["negative"]]
    scope = [r for r in pos if r["in_scope"]]
    found_scope = [r for r in scope if r["status"] in FOUND]
    found_all = [r for r in pos if r["status"] in FOUND]
    bonus = [r for r in pos if not r["in_scope"] and r["status"] in FOUND]
    missed = [r for r in scope if r["status"] not in FOUND]
    fps = [r for r in neg if r["status"] == "FALSE_POSITIVE"]
    req = Counter(r.get("required") for r in found_all if r.get("required"))
    L = []
    L.append(f"# X-Ray score - fixture {key.get('fixture_id')} - layer '{layer}'")
    L.append(f"output: {out.get('tool')} {out.get('version')} ({len(out['facts'])} facts); "
             f"key: {len(key['facts'])} facts ({len(neg)} decoys)")
    L.append("")
    L.append(f"IN-SCOPE RECALL : {len(found_scope)}/{len(scope)} = {pct(len(found_scope), len(scope))}"
             f"   (key facts the '{layer}' layer should see)")
    L.append(f"TOTAL RECALL    : {len(found_all)}/{len(pos)} = {pct(len(found_all), len(pos))}"
             f"   (incl. {len(bonus)} bonus facts the key says need another layer)")
    L.append(f"DECOYS          : {len(neg) - len(fps)}/{len(neg)} handled, {len(fps)} false positive(s)")
    L.append(f"REQUIRED        : agree {req['agree']}, undecidable-statically {req['undecidable_statically']}, "
             f"disagree {req['disagree']}   (on {len(found_all)} found facts)")
    lab = Counter(r.get("label") for r in found_all if r.get("label"))
    if lab:
        L.append(f"LABEL           : agree {lab['agree']}, disagree {lab['disagree']}   "
                 f"(expected_label vs merged label: confirmed / static_only / unresolved / derived)")
    L.append("")
    L.append("## Per category (in scope)")
    L.append(f"{'category':<22}{'expected':>9}{'found':>7}{'missed':>8}{'bonus':>7}")
    cats = sorted({r["category"] for r in pos})
    for c in cats:
        e = [r for r in scope if r["category"] == c]
        fnd = [r for r in e if r["status"] in FOUND]
        b = [r for r in bonus if r["category"] == c]
        L.append(f"{c:<22}{len(e):>9}{len(fnd):>7}{len(e) - len(fnd):>8}{len(b):>7}")
    L.append("")

    def site(r, key="key_sites"):
        s = r.get(key) or []
        return ", ".join(f"{x.get('file')}:{x.get('line')}" for x in s[:2]) or "-"

    if missed:
        L.append("## MISSED (in scope) - fix these")
        for r in missed:
            extra = f" [site found, subject differs: {r.get('out_subject')}]" if r["status"] == "site_only" else ""
            L.append(f"- {r['key_id']} {r['category']} `{r['subject']}` ({r['mechanism']}) at {site(r)}{extra}")
            if r["trap"]:
                L.append(f"    trap: {r['trap'][:140]}")
        L.append("")
    if fps:
        L.append("## FALSE POSITIVES on decoys")
        for r in fps:
            L.append(f"- {r['key_id']} `{r['subject']}` at {site(r)} reported by {r['out_id']} as {r['out_required']}")
        L.append("")
    if bonus:
        L.append("## BONUS - found although the key says only another layer sees it")
        for r in bonus:
            L.append(f"- {r['key_id']} {r['category']} `{r['subject']}` (key: {', '.join(r['visible_to']) or '-'})")
        L.append("")
    dis = [r for r in found_all if r.get("required") == "disagree"]
    if dis:
        L.append("## REQUIRED disagreements")
        for r in dis:
            L.append(f"- {r['key_id']} `{r['subject']}` key={r['key_required']} output={r['out_required']}")
        L.append("")
    ldis = [r for r in found_all if r.get("label") == "disagree"]
    if ldis:
        L.append("## LABEL disagreements")
        for r in ldis:
            L.append(f"- {r['key_id']} `{r['subject']}` key={r['key_label']} output={r['out_label']}")
        L.append("")
    oos = [r for r in pos if not r["in_scope"] and r["status"] not in FOUND]
    if oos:
        L.append(f"## Not visible to '{layer}' and not found (expected - for the next layer)")
        for r in oos:
            L.append(f"- {r['key_id']} {r['category']} `{r['subject']}` (visible_to: {', '.join(r['visible_to']) or '-'})")
        L.append("")
    if extras:
        L.append("## EXTRAS - output facts matching no key fact (review: key gap or false positive?)")
        grp = defaultdict(list)
        for o in extras:
            grp[o["category"]].append(o)
        for c in sorted(grp):
            tag = "  <- key lists these exhaustively: check for false positives" if c in EXHAUSTIVE else ""
            L.append(f"- {c} ({len(grp[c])}){tag}")
            for o in grp[c][:8]:
                s = o["sites"][0] if o.get("sites") else {}
                L.append(f"    {o['id']} `{o['subject']}` {o.get('mechanism')} req={o.get('required_at_runtime')} "
                         f"at {s.get('file')}:{s.get('line')}")
            if len(grp[c]) > 8:
                L.append(f"    ... {len(grp[c]) - 8} more")
    summary = {"in_scope_recall": [len(found_scope), len(scope)], "total_recall": [len(found_all), len(pos)],
               "false_positives": len(fps), "required": dict(req)}
    if lab:
        summary["label"] = dict(lab)
    return "\n".join(L), summary


def main(argv=None):
    ap = argparse.ArgumentParser(description="Grade an X-Ray output against a fixture answer key.")
    ap.add_argument("--key", required=True, help="answer_key.json")
    ap.add_argument("--out", required=True, help="X-Ray output JSON (e.g. xray_static.json)")
    ap.add_argument("--layer", default="static", choices=sorted(LAYER_SEES), help="evidence layer being graded")
    ap.add_argument("--report", default=None, help="write the detailed JSON report here")
    args = ap.parse_args(argv)
    with open(args.key, encoding="utf-8") as fh:
        key = json.load(fh)
    with open(args.out, encoding="utf-8") as fh:
        out = json.load(fh)
    rows, extras = score(key, out, args.layer)
    text, summary = report(key, out, args.layer, rows, extras)
    print(text)
    path = args.report or args.out.rsplit(".", 1)[0] + ".score.json"
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"summary": summary, "rows": rows, "extras": [e["id"] for e in extras]}, fh, indent=1)
    print(f"\n-> detailed report: {path}")


if __name__ == "__main__":
    main()
