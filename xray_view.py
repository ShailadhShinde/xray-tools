#!/usr/bin/env python3
"""
xray_view.py - open X-Ray results in the viewer WITHOUT the "Open files" button.

Writes a copy of xray_viewer.html with the JSON files built in; open that copy (double-click) and everything is
already loaded. Use it when the browser / PC does not allow picking or dragging files. Standard library only.

  python xray_view.py app.json                                   -> app.view.html (and opens it)
  python xray_view.py app.json app-merged.json app-pkg.json plans/app/plan.json --out app.view.html
  python xray_view.py app.json --no-open                         (only write the file)
"""
import argparse
import json
import os
import sys
import webbrowser

VERSION = "0.1.0"
HERE = os.path.dirname(os.path.abspath(__file__))


def main(argv=None):
    for _s in (sys.stdout, sys.stderr):   # Windows consoles / pipes: never crash on tree characters or paths
        if hasattr(_s, "reconfigure"):
            _s.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="X-Ray viewer with the JSON files built in (no file picker needed)")
    ap.add_argument("json", nargs="+", help="X-Ray outputs: static / merged / runtime map, -pkg.json, plan.json")
    ap.add_argument("--out", help="the HTML file to write (default: FIRST.view.html next to the first JSON)")
    ap.add_argument("--viewer", default=os.path.join(HERE, "xray_viewer.html"), help="the viewer to copy")
    ap.add_argument("--no-open", action="store_true", help="do not open it in the browser")
    a = ap.parse_args(argv)
    files = []
    for p in a.json:
        try:
            with open(p, encoding="utf-8") as fh:
                files.append({"name": os.path.basename(p), "json": json.load(fh)})
        except (OSError, ValueError) as e:
            sys.exit(f"xray_view: cannot read {p}: {e}")
    try:
        with open(a.viewer, encoding="utf-8") as fh:
            html = fh.read()
    except OSError as e:
        sys.exit(f"xray_view: cannot read the viewer {a.viewer}: {e} (keep xray_view.py next to xray_viewer.html)")
    if "XRAY_EMBEDDED" not in html:
        sys.exit("xray_view: this xray_viewer.html is too old (no XRAY_EMBEDDED support) - get the new one")
    data = json.dumps(files, ensure_ascii=False).replace("</", "<\\/")   # a "</script>" inside a string must not end the tag
    html = html.replace("</head>", f"<script>window.XRAY_EMBEDDED = {data};</script>\n</head>", 1)
    out = a.out or os.path.splitext(a.json[0])[0] + ".view.html"
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(html)
    print(f"xray_view {VERSION}: {', '.join(f['name'] for f in files)} -> {out}")
    print("  open it with a double-click (Chrome / Edge); the files are already loaded")
    if not a.no_open:
        try:
            webbrowser.open("file://" + os.path.abspath(out).replace("\\", "/"))
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
