# X-Ray - first-time user guide

X-Ray helps you build a **small Docker image that still works** for a Python project.
It does not guess: it **reads** your code, **watches** it run, **looks inside** the image, and then **writes**
a slim Dockerfile and **checks** that the slim image does the same things as the old one.

You run every command yourself. X-Ray never changes your code, never uploads anything, and never starts
Docker on its own.

Read this guide once from top to bottom before you start. Then follow it step by step.

---

## 0. The words used in this guide

| Word | Meaning |
|---|---|
| **project folder** | the folder with your Python code (what `COPY . .` would copy into an image) |
| **entry point** | the `.py` file you start (`python app.py` -> `app.py`). A project can have several. |
| **image** | the packed-up app Docker runs. "Naive image" = the big one you have today. |
| **container** | a running image |
| **output JSON** | the file each X-Ray step writes (you open it in the viewer, or give it to the next step) |
| **terminal** | Linux / WSL: the Ubuntu window. Windows: PowerShell or the PyCharm "Terminal" tab. |

**`python3` or `python`?** On Linux / WSL / Mac type `python3`. On Windows type `python`.
This guide writes `python3`; change it if you are on Windows.
Check it works: `python3 --version` must print 3.8 or newer. X-Ray needs nothing else (no pip install).

**Windows PowerShell:** run this once in each new PowerShell window, so the tree lines (`├─`) show correctly
in the window and in log files:
```powershell
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
```

---

## 1. Get the tools (once per machine)

```bash
git clone https://github.com/shailadhshinde/xray-tools.git
cd xray-tools
```
(If your repo has another name, use that URL.) Later, to get a newer version: `cd xray-tools && git pull`.
**After an update, re-run the steps you want to use** - an old JSON keeps the old results.

Everything below is run from inside the `xray-tools` folder.

---

## 2. Before you run anything: gather these facts (10 minutes, pen and paper)

Write the answers down. Every later command needs them.

### 2.1 Where is the project, and what are its parts?
- The full path of the project folder. Linux example: `/home/me/projects/myapp`. Windows: `C:\Users\me\myapp`
  (in WSL the same folder is `/mnt/c/Users/me/myapp`).
- A project often has **several apps** (for example an API, a background worker, a dashboard). Each app that has
  its own Dockerfile is its own X-Ray run. Only Python apps can be analysed.

### 2.2 Find the entry points
An entry point is the file that is started. Look in this order:

1. **The Dockerfile**: the last `CMD` / `ENTRYPOINT` line, e.g. `CMD ["python", "main.py"]` -> `main.py`.
2. **docker-compose.yml**: a `command:` line under a service overrides the Dockerfile's CMD.
3. **How you start it today**: the PyCharm Run configuration (top right, "Edit Configurations" -> "Script path"),
   a `.sh` / `.bat` start script, a systemd service, or the command you type.
4. **Search for files that can be started** (they contain `if __name__ == "__main__":`):
   ```bash
   grep -rl --include=*.py "__main__" PROJECT_FOLDER | grep -v -e venv -e site-packages
   ```
   Windows PowerShell:
   ```powershell
   Get-ChildItem -Recurse -Filter *.py PROJECT_FOLDER | Select-String "__main__" -List | Select Path
   ```
   You will often find many (tests, old scripts, backups). **Only the ones that are really started count.**
   If you are not sure, ask the person who deploys it, or check which file the Dockerfile/compose runs.

**Watch out:** if the Dockerfile `CMD` runs a file that is not in the folder (e.g. `api.py` but no `api.py`
exists), the image is started some other way (compose `command:`, a script). Find out how.

If two entry points share the same code folder (e.g. two APIs on different ports), give **both** to X-Ray in one
run (`--entry a.py --entry b.py`). Mapping them separately makes each one think the other's code is unused.

### 2.3 Find the requirements file
The pip packages list: `requirements.txt`, or another name. Look at the Dockerfile's `COPY ... requirements` /
`pip install -r ...` line - **that** is the file the image really uses (there may be several old ones).

### 2.4 Find the Python version
- Dockerfile `FROM python:3.9-slim` -> 3.9.
- Dockerfile `FROM nvidia/cuda:...ubuntu24.04` + `apt install python3` -> the Ubuntu one (24.04 = 3.12, 22.04 = 3.10).
- A PyCharm venv: `.venv/pyvenv.cfg`, line `version`.

### 2.5 What does the app need from outside?
You will confirm this with X-Ray, but write down what you already know:
database (which one, where), cameras (webcam / RTSP), GPU, a screen (windows opening), other services,
env variables, model files, folders it writes (images, logs, results), licence checks.

### 2.6 How do you know it works?
A URL that answers, a file that appears, a database row, a log line. You need this for testing in step 5.

---

## 3. Step 1 - Read the code (safe: nothing runs, nothing changes)

### 3.1 Run it
```bash
python3 xray_static.py --root PROJECT_FOLDER --entry ENTRY.py --requirements REQUIREMENTS.txt --out app.json 2>&1 | tee step1.log
python3 xray_needs.py app.json 2>&1 | tee step1-needs.log
```
- `--entry` is relative to `--root`. Several: `--entry a.py --entry b.py`.
- `--requirements` is relative to `--root` too (default: `requirements.txt`).
- `2>&1 | tee step1.log` shows the output **and** saves it (with any errors) in `step1.log`.

Optional flags (only if needed):

| Flag | When |
|---|---|
| `--exclude "tests/*"` | skip folders that are not part of the app (tests, helper scripts, backups). Repeatable. |
| `--cwd subfolder` | the Dockerfile `WORKDIR` is a subfolder of the project (decides where `open("config.yml")` points) |
| `--path src` | the container sets `PYTHONPATH` to a subfolder |

A big project may take a minute. Warnings about files it could not parse are normal (old Python 2 files, broken
backups); they are listed in the JSON under `"warnings"`.

### 3.2 Read the summary (the last lines of step1.log)
```
X-Ray static 0.5.4: 100 facts, 18 modules needed (23 parsed)
  by required : yes=59, conditional=18, no=23
```
- `18 modules needed (23 parsed)`: the entry point uses 18 of the 23 Python files it looked at.
- `yes` = always needed; `conditional` = "maybe" (inside an `if`, a `try`, a loop, a function that may not run);
  `no` = not needed (other scripts, dead code, unused files).

### 3.3 Read the needs report (step1-needs.log)
It has four parts. What each one means and **what to check**:

**a) The tree** - starts at your entry file, then every project file it imports, each with what it uses:
cameras, screen, env vars, packages, model files, data files, files written, network, system (GPU, processes).
`[maybe]` = only in a code path that may not run.
- Check: are all the files you know are important in the tree? A missing one = X-Ray could not follow an
  import (tell Claude which one).

**b) TO RUN IT IN A CONTAINER, YOU PROVIDE** - your checklist:
| Line | Means | You check |
|---|---|---|
| `env -e NAME=value` | the code reads this env var; "code default" = value used if you don't set it | is the default right for Docker? (e.g. a DB host `localhost` is wrong inside a container) |
| `network connects to host:port` | database / API it talks to | is that host reachable from a container? `localhost` / `127.0.0.1` never is |
| `network listens on 127.0.0.1:PORT` | a server only reachable from inside its own container | needs a code change or env var to bind `0.0.0.0` |
| `model ...` | model files it loads | are they in the project folder? big ones may be better mounted |
| `writes ...` | folders it writes | these need a volume (`-v`) to keep the files |
| `camera ...` | webcam / RTSP / file | RTSP on the network: fine. Webcam: `--device /dev/videoN` (Linux). `<unresolved>` = the address comes from a DB/config at runtime |
| `screen ...` | opens windows (`cv2.imshow`) | a server has no screen: is it switched off by a setting? |
| `system GPU: ...` | "required" = no CPU fallback, "optional" = falls back to CPU | does the machine have an NVIDIA GPU + the NVIDIA container toolkit? |

**c) CODE CHANGES NEEDED** - things Docker settings cannot fix (hardcoded passwords, `127.0.0.1` servers,
hardcoded Windows paths, key files). You may not be allowed to change code: then this is the list you give to
the code owner, with file and line.

**d) NOT USED BY THIS ENTRY POINT** - files, folders, models the entry point never touches. They will be left
out of the slim image. Check: is anything here actually used (e.g. started by another script)? If yes, that
script is another entry point - add it with `--entry`.

### 3.4 Look at it in the viewer (optional but easier)
```bash
python3 xray_view.py app.json
```
This writes `app.view.html` (a copy of the viewer with `app.json` built in) and opens it in the browser.
Nothing to upload or pick: the page opens already loaded. If it does not open by itself, double-click
`app.view.html`. Later, put all of a project's files in one page:
`python3 xray_view.py app.json app-merged.json app-pkg.json plans/app/plan.json`.
(You can also double-click `xray_viewer.html` and use **Open files...**, if your browser allows it.)
- **Entry tree**: the same tree, clickable, colour groups per file, and the "to run it in a container" box.
- **Map**: the whole project as a graph. Hollow shapes = not needed.
- **Facts**: every finding in a table; click one to see the file and line.
- **Docker plan**: X-Ray's first suggestions (base image, apt packages, requirements changes, what to copy).

### 3.5 Things to investigate yourself (search the code)
X-Ray finds these, but when something looks wrong, these searches show you the lines (run in the project folder;
PowerShell: `Select-String -Path *.py -Pattern "..." -Recurse` style, or PyCharm "Find in Files", Ctrl+Shift+F):

| Search for | Why |
|---|---|
| `VideoCapture` | where cameras / video files are opened, and where the address comes from |
| `os.environ`, `os.getenv` | env variables it reads (and their defaults) |
| `InferenceSession`, `torch.load`, `.onnx`, `.pt` | model files and GPU/CPU choice (`providers=`) |
| `open(`, `imwrite`, `makedirs` | files read and written |
| `connect(`, `psycopg2`, `mysql` | databases and their host/port |
| `app.run`, `host=` | servers and the address they listen on |
| `subprocess`, `os.system` | external programs it needs (ffmpeg, ...) |
| `imshow`, `waitKey` | windows (need a screen) |
| `sys.path` | import tricks (which copy of a folder is really imported) |
| `getnode`, `mac`, `serial`, `/sys/` | licence / machine checks (may fail in a container) |
| `multiprocessing`, `SharedMemory`, `Process` | many processes + shared memory (Docker's `shm_size`) |

Also look at the files:
```bash
# the biggest files in the project (models, videos, backups that bloat the image)
find PROJECT_FOLDER -type f -size +20M -not -path "*/.git/*" -exec ls -lh {} \; | sort -k5 -h
# folders that must never go into an image
find PROJECT_FOLDER -maxdepth 3 -type d \( -name .git -o -name .venv -o -name venv -o -name __pycache__ -o -name .idea \)
```

### 3.6 Send to Claude
Paste `step1-needs.log` (and the last 10 lines of `step1.log`) into the chat. The needs report contains file
paths, env var **names**, hosts and ports, but secrets are masked (`***`).
**Never** upload your company's code or these outputs to a public place.

---

## 4. Step 2 - Watch it run (needs Docker and an image that runs the app)

**Shortcut:** `python xray_runfile.py --static runs/NAME/NAME.json --project PROJECT --name NAME` writes these files into
`runs/NAME/` (`.bat` on Windows, `.sh` on Linux): `build-naive`, `run-step2` (+ `run-step2-cpu` for GPU apps),
`run-step3-4` (package scan + plan), `build-slim` and `run-slim` (the same run on the new image, then `verify.txt`).
Run them in that order. The sections below explain what they do.

Why: reading code cannot tell which `if` really runs, which of two installed packages is loaded, whether the GPU is
really used, what a library writes. Step 2 runs the app **inside its current image** with X-Ray watching.

### 4.1 Check what you have
```bash
docker images                 # is the app's image there? note its name
docker compose ps             # (in the compose folder) which services are running
```
If there is no image yet, build it the way it is built today (`docker compose build SERVICE` or
`docker build -t NAME .`). That is the **naive image**; note its size in `docker images` - your "before".

### 4.2 The command
The idea: start the app's image, but put X-Ray inside (`-v`), and let X-Ray start the app (everything after `--`).
```bash
mkdir -p trace-app && chmod 777 trace-app
docker run --rm \
  -v "$PWD":/xray:ro \
  -v "$PWD/trace-app":/xray-out \
  IMAGE \
  python /xray/xray_trace.py run --out /xray-out --root /app --stop-after 120 -- python ENTRY.py \
  2>&1 | tee step2.log
```
- `-v "$PWD":/xray:ro`: the X-Ray folder, read-only, inside the container.
- `-v "$PWD/trace-app":/xray-out`: where the recording is written.
- `--root /app`: the project folder **inside the image** (the Dockerfile's `WORKDIR`).
- After `--`: exactly how the app is started (the Dockerfile CMD).
- If the image has an `ENTRYPOINT` script that must not run, add `--entrypoint ""` before `IMAGE`.

### 4.3 Add what the app needs (from the Step 1 checklist)
| The app needs | Add |
|---|---|
| env variables | `-e NAME=value` before `IMAGE` |
| a database / other compose services | run through compose so it is on the same network: start them (`docker compose up -d db`), then replace `docker run --rm ... IMAGE` by `docker compose run --rm -v ... SERVICE` |
| a GPU | `--gpus all` before `IMAGE` (needs the NVIDIA container toolkit) |
| a webcam, but you want to test with a video | `-v /path/clip.mp4:/data/clip.mp4:ro` and `--camera 0=/data/clip.mp4` (after `run`) |
| an RTSP camera address in code / DB | nothing, if the container can reach it. To replace one: `--camera "rtsp://...=/data/clip.mp4"` |
| windows (`imshow`) | `--gui off` (frames it would show are saved in `trace-app/gui/`) |
| a server (Flask ...) | it never ends: `--stop-after 120` and send it real requests in those 120 s (browser, Postman, curl) |
| a script that ends by itself | nothing |
| endless loops / workers | `--stop-after 60` (or longer: long enough to do its real work once) |

**The test is what matters:** X-Ray only records what the app did during this run. If no face is detected, the
"face found" code never runs, and the slim image is only checked for what ran. Make the run do the real work.

### 4.4 Check it worked
The last lines of `step2.log` must say
`[xray-trace] app exited ... collecting system info...` and `[xray-trace] done: N Python processes traced, M events`.
A `KeyboardInterrupt` traceback just before is normal (that is the stop).
If the app crashed at once, the reason is above `[xray-trace] app exited` - usually a missing env var, file, or a
service it could not reach. Fix that and run again.

### 4.5 Turn the recording into facts (on your machine)
```bash
python3 xray_trace.py facts --trace trace-app --out app-runtime.json 2>&1 | tee step2-facts.log
python3 xray_trace.py merge --static app.json --runtime app-runtime.json --root PROJECT_FOLDER --out app-merged.json
python3 xray_needs.py app-merged.json 2>&1 | tee step2-needs.log
```
Several entry points: one trace folder each, then `facts --trace trace-a --trace trace-b`.

### 4.6 What to look for
In `step2-needs.log` every item now says **seen running**, **its line ran** or **did not run**:
- important things that **did not run** -> your test did not reach them. Improve the test (4.3) and repeat.
- **found only by running** -> things reading the code missed (a library writing a cache, a model download...).
- GPU lines show what really happened: `torch.cuda.is_available() returned ...`, ONNX providers **requested vs
  active** (asked for the GPU but ran on the CPU = silent slowdown).
- threads: `oversubscribed` = more busy threads than CPUs (each process uses all CPUs).
Send `step2-needs.log` + the end of `step2.log` to Claude.

---

## 5. Step 3 - What is inside the image

```bash
mkdir -p pkg-slim pkg-app && chmod 777 pkg-slim pkg-app
# once per Python version: what a clean python:X.Y-slim has (use YOUR version)
docker run --rm -v "$PWD":/xray:ro -v "$PWD/pkg-slim":/xray-out python:3.11-slim \
  python /xray/xray_pkg.py baseline --out /xray-out/slim.json
# the app's image
docker run --rm --entrypoint "" -v "$PWD":/xray:ro -v "$PWD/pkg-app":/xray-out IMAGE \
  python /xray/xray_pkg.py scan --root /app --out /xray-out/pkgscan.json
python3 xray_pkg.py facts --scan pkg-app/pkgscan.json --baseline pkg-slim/slim.json \
  --static app.json --runtime app-runtime.json --out app-pkg.json 2>&1 | tee step3.log
```
(No Step 2? Leave out `--runtime app-runtime.json`; results are more cautious.)

What to look for in the last lines of `step3.log`:
- `requirements: keep [...], remove [...], swap [...], add [...]` - pip changes for the slim image
  (remove = installed but never used; swap = e.g. a GPU build of torch on a CPU-only app, `psycopg2` -> `psycopg2-binary`).
- `Python packages installed: X GB; largest: ...` - where the size is.
- `Debian packages not in python:3.x-slim: N MB` - what the base image adds.
Send `step3.log` to Claude.

---

## 6. Step 4 - The slim Dockerfile, build, verify

### 6.1 Write the plan
The project has its own Dockerfile / compose file: tell X-Ray where they are.
```bash
python3 xray_plan.py plan --project PROJECT_FOLDER --dockerfile PROJECT_FOLDER/Dockerfile \
  --compose PATH/docker-compose.yml --static app.json --runtime app-runtime.json --pkg app-pkg.json \
  --out plans/app 2>&1 | tee step4.log
```
- The compose services that build this folder are found automatically; `--service NAME` picks one.
- No compose file: `--compose none`.
- Read every `NOTE:` line. Example: "the Dockerfile CMD runs api.py, which is not in the project".
- **GPU apps** (the run really used the GPU), `--gpu-base`:
  - `auto` (default): the same `nvidia/cuda` image family as your naive Dockerfile (`-runtime`, never `-devel`),
    with Ubuntu's python3 in a venv. It is the family that already worked on your GPU.
  - `pip`: `python:X.Y-slim`, with the CUDA libraries coming from pip (`onnxruntime-gpu[cuda,cudnn]`, plus an
    `LD_LIBRARY_PATH` pointing at them). It is about 1.5-2 GB smaller. Use it only if the verify run says PASS: verify
    checks that the GPU was really used.

You get `plans/app/Dockerfile` (every line has a comment why), `Dockerfile.dockerignore`, `compose.xray.yml`,
`plan.json`. Read the Dockerfile before building.

### 6.2 Build it and compare sizes
```bash
docker build -f plans/app/Dockerfile -t app-xray PROJECT_FOLDER
docker images | grep -e app-xray -e NAIVE_IMAGE_NAME
```
With compose: `docker compose -f docker-compose.yml -f PATH/TO/plans/app/compose.xray.yml build SERVICE`
(the exact command is written at the top of `compose.xray.yml`).

### 6.3 Verify: same test, new image
Repeat **exactly** the Step 2 run with `app-xray` instead of the old image and a new folder `trace-app-xray`
(the slim image runs as a normal user, so `chmod 777 trace-app-xray` first). Then:
```bash
python3 xray_trace.py facts --trace trace-app-xray --out app-xray-runtime.json
python3 xray_plan.py verify --naive app-runtime.json --planned app-xray-runtime.json 2>&1 | tee step4-verify.log
```
- `VERDICT: PASS` - the slim image did everything the old one did in the same test.
- `CHECK` + problems - something is missing (a Linux library, a file, a permission). Send `step4-verify.log` to
  Claude; the fix goes into the plan, not your code.

---

## 7. When something goes wrong

**Always:** keep the `| tee stepN.log` at the end of every command; send the last 20-30 lines of that log.

| You see | Meaning / what to do |
|---|---|
| `python3: command not found` | Windows: type `python` |
| `No such file or directory: xray_static.py` | you are not in the `xray-tools` folder (`cd xray-tools`) |
| `error: the following arguments are required: --entry` | the command is missing a flag; check spelling (two dashes `--`) |
| an entry point "not found" | `--entry` is relative to `--root`; check the exact name and capital letters |
| `permission denied` writing `trace-...` / `pkg-...` | `chmod 777 thatfolder` |
| `docker: command not found` / cannot connect | Docker is not running (start Docker Desktop / `sudo systemctl start docker`) |
| the app exits at once in Step 2 | read the error above `[xray-trace] app exited`: missing env var, file, or unreachable DB/camera |
| `could not select device driver "" with capabilities: [[gpu]]` | `--gpus all` without the NVIDIA container toolkit on that machine |
| `timed out waiting for /mnt/wslg` | Docker Desktop cannot show windows: use `--gui off` |
| verify says `CHECK` | send `step4-verify.log` |
| anything else | send the log; say which step and the exact command you typed |

## 8. What to send to Claude, and what never to send
**Send (in the chat):** the `.log` files, the `xray_needs.py` output, the JSON outputs when asked.
**Never:** company source code, passwords, private keys, certificates; never put them in a public GitHub repo.
X-Ray outputs mask secret values, but they do contain file paths, host names and env var names - share them in the
chat, not publicly.

## 9. Quick reference
| Step | Command | Output | Look at |
|---|---|---|---|
| 1 read | `xray_static.py --root --entry --requirements --out app.json` | app.json | `xray_needs.py app.json` |
| 2 watch | `docker run ... IMAGE python /xray/xray_trace.py run --out /xray-out --root /app -- python ENTRY.py` | trace-app/ | end of log: `done: N processes` |
| 2 facts | `xray_trace.py facts --trace trace-app --out app-runtime.json` + `merge` | app-runtime.json, app-merged.json | `xray_needs.py app-merged.json` |
| 3 packages | `xray_pkg.py baseline` / `scan` (in containers), `facts` | app-pkg.json | the `requirements:` line |
| 4 plan | `xray_plan.py plan --project --dockerfile --compose ...` | plans/app/ | `NOTE:` lines, the Dockerfile |
| 4 verify | same run on the new image, `facts`, `verify` | verdict | `VERDICT: PASS` |
