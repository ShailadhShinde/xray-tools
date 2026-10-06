# X-Ray: plan a small, correct Docker image for a Python project

**First time? Read [GUIDE.md](GUIDE.md)** - every step, what to look for, what to do when something fails.

Python standard library only (Python 3.8+). Nothing to install.

| Step | Tool | Runs where | What it does |
|---|---|---|---|
| 1 | `xray_static.py` + `xray_needs.py` | your machine | reads the code (runs nothing): packages, files, models, env vars, network, GPU, cameras |
| 2 | `xray_runfile.py` writes the run files; they run `xray_trace.py run` / `facts` / `merge` | inside the app's container / your machine | runs the app and records what it really did |
| 3 | `xray_pkg.py baseline` / `scan` / `facts` | inside containers / your machine | what is installed in the image and what is really needed |
| 4 | `xray_plan.py plan` / `verify` | your machine | writes a slim Dockerfile, .dockerignore and compose override; checks the new image behaves the same |
| 5 | `xray_compare.py` | your machine | several projects: one shared base image |

See results in the browser: `python3 xray_view.py app.json` (writes and opens `app.view.html` with the file built in).

## Step 1 (safe: reads files only)
```
python3 xray_static.py --root PROJECT_FOLDER --entry main.py [--entry other_entry.py] [--requirements requirements.txt] --out app.json
python3 xray_needs.py app.json
```
On Windows use `python` instead of `python3`.

## Keep a log of every command
Add `2>&1 | tee step1.log` to the end of a command: the screen output (including errors) is also saved in
`step1.log`, which you can share.
