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
| **terminal** | Linux / WSL: the Ubuntu window. Windows: CMD (Command Prompt) or the PyCharm "Terminal" tab. Nothing in this guide needs PowerShell. |

**Where do I type a command?** Two places, and a command typed in the wrong one fails:

| The block is marked | Type it in |
|---|---|
| `bat` / "in CMD" | the Windows CMD window on your PC (prompt `C:\...>`) |
| `bash` / "inside" | the container. First open a shell in it: `docker exec -it NAME bash` (a running container) or `docker run --rm -it --entrypoint bash IMAGE` (an image). The prompt changes to something like `root@1a2b3c4d:/app#`; `exit` goes back to CMD |

`SP=$(python -c ...)`, `ldd`, `ls /proc/...`, `$LD_LIBRARY_PATH` are Linux commands: typed in CMD they fail with
"'SP' is not recognized as an internal or external command". Inside, `python: command not found` = that image only
has `python3`.

**`python3` or `python`?** On Linux / WSL / Mac type `python3`. On Windows type `python`.
This guide writes `python3`; change it if you are on Windows.
Check it works: `python3 --version` must print 3.8 or newer. X-Ray needs nothing else (no pip install).

**Windows CMD:** the X-Ray tools write UTF-8 themselves. If the tree lines (`├─`) look garbled in the CMD window,
run this once in that window (the log files are fine either way - open them in Notepad):
```bat
chcp 65001
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
   Windows CMD:
   ```bat
   findstr /s /m /c:"__main__" PROJECT_FOLDER\*.py
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

### 2.4 Find the Python version (xray_naive.py reads it itself)
The image should run the Python the project already runs on: package versions and wheels depend on it.
`xray_naive.py` looks in the project, in this order, and says in `naive-check.txt` where it found it:
- a virtual environment in the project (`venv/`, `.venv/`, `env/` - PyCharm makes one): `pyvenv.cfg`, line `version`.
  This is the strongest evidence: it is what the code really runs on;
- `.python-version` (pyenv), `runtime.txt`, `Pipfile`, `environment.yml` (conda), the project's own `Dockerfile`
  (`FROM python:3.9-slim` -> 3.9);
- `pyproject.toml` `requires-python`, `setup.py` / `setup.cfg` `python_requires`: the newest Python it allows.
Nothing found: 3.12. Your own choice wins: `--python 3.10`. The venv lives elsewhere (PyCharm: Settings > Project >
Python Interpreter shows its path and version)? Then give it with `--python`.

**Which Linux?** Always Linux: Docker Desktop on Windows runs Linux containers, the same image runs on a Linux server.
What differs between machines is the CPU: `x86_64` (Intel / AMD PCs and servers, the default) or `aarch64` (ARM:
Jetson, Raspberry Pi, ARM cloud servers) - `--arch aarch64` checks the requirements for ARM. A Jetson with its GPU
needs one of NVIDIA's L4T base images; the tool says so.

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
| `screen ...` | opens windows (`cv2.imshow`) | a container has no screen. The line below says how to switch them off: the code's own env var (`in a container: -e SHOW=0`, and the `env` line for it says `MUST be 0`), or - when the switch is a constant written in the code (`SHOW = True`) or there is none - a change in CODE CHANGES NEEDED |
| `system GPU: ...` | "required" = no CPU fallback, "optional" = falls back to CPU | does the machine have an NVIDIA GPU + the NVIDIA container toolkit? |

**c) CODE CHANGES NEEDED** - things Docker settings cannot fix (hardcoded passwords, `127.0.0.1` servers,
hardcoded Windows paths, key files). You may not be allowed to change code: then this is the list you give to
the code owner, with file and line.

**c2) OPTIONAL CODE CHANGES (it runs without them - the image gets smaller)** - a package the code imports and
then never uses (the tree marks it `(imported, never used)`): only the `import` line runs. The image still carries
that package and every package only it needs. The classic case: `import torch` at the top of a program that runs its
model with onnxruntime-gpu. On Windows that line has a job - importing torch loads torch's CUDA DLLs, and
onnxruntime-gpu then finds them. In a Linux image it has none: the loader finds NVIDIA's CUDA wheels (their folders are
registered in the image - section 3b, line 4) or the nvidia/cuda base image has them - and torch with its own CUDA
wheels is 3-4 GB. X-Ray does not decide this from the names "torch" / "onnxruntime": step 3 reads it from the image
(the unused package depends on CUDA wheels; a package the code uses links CUDA libraries by name). The change keeps
your PC as it is:
```python
if os.name == "nt": import torch      # Windows only: loads the CUDA DLLs onnxruntime-gpu uses
```
X-Ray reads code under `if os.name == "nt":` / `if sys.platform == "win32":` / `if platform.system() == "Windows":`
as "never runs in the image" (an image is Linux), so after the change - rebuild the naive image, run Steps 2-4 again -
the plan leaves the package out, and the verify run shows whether the image still works without it.
`code-changes.txt` (from xray_runfile.py) lists the same items under OPTIONAL, with the line as it is now and the line
to write; `step3.txt` and the plan's `NOTE: smaller image:` say how many GB it is.

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
Windows CMD: `findstr /s /n /c:"VideoCapture" *.py`, or PyCharm "Find in Files", Ctrl+Shift+F):

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

## 3b. Before the first build - check the requirements and write the naive Dockerfile

Why: most first builds fail on the same three things. `xray_naive.py` finds them **before** you build, in about a
minute, without installing anything (pip only reads the package index - it needs internet):

| It reports | Meaning | What the tool does |
|---|---|---|
| COMPILED | no ready-made Linux package ("wheel") for that version: pip compiles it in the build (`gcc not found`, `pg_config not found` ...). Also when only the source exists and pip could not even PREPARE it on your PC (psycopg2 asks for PostgreSQL's `pg_config`, which a Windows PC does not have): that part runs again inside the image, where the build installs what it needs | the Dockerfile gets a compiler + the right `-dev` library (psycopg2: `libpq-dev`; or use `psycopg2-binary`) |
| MISSING | that version does not exist for Linux / this Python (`No matching distribution`) | lists the nearest versions that do: change the pin |
| WINDOWS | a Windows-only package (`pyreadline3`, `pywin32` ...) - a `pip freeze` from a Windows PC | left out |
| INDEX | `torch==2.8.0+cu126`: exists only on PyTorch's own index | the index line is added |
| INSTALLED IN A SECOND STEP | a line pip refuses to install together with another one: torch `+cu126` pins its own `nvidia-cublas-cu12` version (12.6.x) but your file pins `nvidia-cublas-cu12==12.9.2.10`; `torchaudio 2.11.0 depends on torch==2.11.0` but your file pins torch 2.12.1 | installed **exactly as pinned**, after everything else, with `pip install --no-deps` (Dockerfile.naive section 3c). Nothing is left out, no version is changed |
| ADDED | what a second-step package needs that is not in your file and nothing else installs (`--no-deps` would skip it) | installed right after it; `-c` keeps every version installed before exactly as it is |
| LISTED TWICE | the same package on two lines | the line that installs (the newer one if both) is kept, the other is reported |
| SAME FILES | two DIFFERENT packages that install the same files: `opencv-python` + `opencv-python-headless` (both write `cv2/`), `PyJWT` + `jwt`, `pymongo` + `bson` | found from each wheel's file list (read from the package index, not downloaded): both in one image = the one installed last overwrites the other's files. One is kept - the one with the most files there, or the one you name with `--keep NAME`; the report says which and why |
| a pin with no Linux file / a local path | `pkg @ file:///C:/...` or a version that has no Linux file at all | installed by name / the nearest version that has a Linux file (the only way it can work on Linux) |
| CANNOT BE INSTALLED TOGETHER | a clash that none of the above solves | shows pip's reason (the whole file installed at once, like the build) |

**Why a second step - the error you had.** A `pip freeze` comes from a PC where packages were installed at different
times: torch first (with the nvidia-* versions it asks for), later newer nvidia-* packages or a torchaudio of another
torch. Each time pip only **warned** ("pip's dependency resolver does not currently take into account ..."), so that
PC has exactly the versions in the file. A Docker build installs the whole file in **one** `pip install`, and in one
install pip refuses the same combination (`ResolutionImpossible`). The second step repeats what happened on the PC:
first everything else, then those lines with `--no-deps`. `naive-check.txt` lists each one with pip's own reason.
In the build log you then see the same warning the PC saw, plus `pip check` lines like `torch 2.12.1+cu126 has
requirement nvidia-cublas-cu12==12.6..., but you have nvidia-cublas-cu12 12.9.2.10` - information, the build goes on.
Does the program work with those versions (the GPU too)? That is what the runs check: `run-naive`, the X-Ray Step 2
run, and `xray_look.py --image` (section 10a: every package is imported, and the GPU frameworks are tested).
Windows-only packages (`pywin32`, `pyreadline3` ...) are the only lines left out: no Linux version of them exists.

```bat
python xray_naive.py --project "PROJECT_FOLDER" --requirements requirements.txt --entry main.py --name app --gpu
```
(`--cpu` for a CPU image; `--python 3.10` to choose the Python yourself - section 2.4; `--arch aarch64` for ARM.)
If `naive-check.txt` starts with `WARNING: your pip (...) checks 'Requires-Python' ...`: run `python -m pip install -U pip`
and the check again. pip 24.1 and older check a package's Python limits against the PC's own Python instead of the
image's, so a package made only for some Python versions shows up as MISSING or with another version.

**OTHER PYTHON VERSIONS.** For every line with no Linux wheel for the chosen Python, `naive-check.txt` lists the
Pythons (3.8 to 3.13) that DO have one for exactly that pin - e.g. `numpy==1.21.6  Linux wheels for Python 3.8, 3.9,
3.10`. Often that is the real answer: the file comes from a PC with Python 3.10, and the image should be 3.10 too.
When one Python has a wheel for all of them, it says so (`-> Python 3.10 ...`); run the check again with that
`--python` to see every other line for it. It is a choice, not a fix: the project must run on that Python.

`naive-check.txt` also has two tables:
- **WHAT KIND OF PACKAGE EACH ONE IS** - for every line, the file pip would really download for Linux:
  `pure wheel` (Python only, `py3-none-any`), `binary wheel` (compiled `.so` inside, prebuilt), `HEAVY binary wheel`
  (over 100 MB), `sdist (source)` - for a source package the tool downloads it and looks inside: C / C++ / Cython /
  Rust files = **compiler YES**, Python only = no compiler. With the download size of each, and for torch the size
  of the `nvidia-*` CUDA packages it pulls in (they are separate pip packages, about 2.7 GB).
- **GPU - WHERE EACH GPU PACKAGE GETS ITS CUDA LIBRARIES FROM** - torch brings its own (nvidia-* pip packages);
  onnxruntime-gpu needs them from the system (base image), or from the `[cuda,cudnn]` extra (1.21 and newer only).
  From that it says which base fits: if torch is there, `python:3.12-slim` is enough and an nvidia/cuda base would
  be a second copy of the same libraries.
It writes `runs\app\naive\`: `naive-check.txt` (read it), `Dockerfile.naive`, `Dockerfile.naive.dockerignore`,
`requirements.docker.txt`. Copy the three Docker files into the project folder - the tool never overwrites the
project's own files. Then `xray_runfile.py` writes `build-naive.bat` as usual.

**Files your code reads from your PC** (a model on the Desktop, a video on D:): run the check with
`--static runs\app\app.json` (the Step 1 map) and `naive-check.txt` ends with FILES OUTSIDE THE PROJECT:
- **every env var your code reads**: its value in your code (`C:\Users\me\Desktop\...\best_blend.onnx`) and what it
  becomes in the container (`/xin/model_onnx/best_blend.onnx` - Dockerfile.naive sets it with `ENV`), outputs
  (`/xout/...`), and the ones used as they are;
- **CODE CHANGES NEEDED**: a Windows path written in the code WITHOUT an env var (`VIDEO_IN = r"D:\..."`) cannot be
  fixed by any Docker setting. It shows WHERE (file:line), NOW, and CHANGE TO
  (`VIDEO_IN = os.getenv("VIDEO_IN", r"D:\...")` - the old value stays the default, your PC runs as before);
- **copy or keep outside**, per file: a file up to 500 MB that exists on this PC is COPIED into the image (it runs
  anywhere on its own); bigger ones, or ones not found, are KEPT OUTSIDE and mounted with `-v` at run time. Choose
  yourself with `--copy MODEL_ONNX` / `--mount MODEL_ONNX` (or `--copy all` / `--mount all`). A copied file is taken
  from its folder at build time with `--build-context` (build-naive.bat adds it);
- the exact **build** and **run** commands, and how to pass env vars: `docker run ... -e NAME=value ...` or
  `--env-file run.env` (one `NAME=value` per line). xray_runfile.py writes `runs\app\run.env` with all of them.

`Dockerfile.naive` is written in 5 sections, each decided from the tables above, with the reason as a comment
above every line (also good to know when you write a Dockerfile by hand):

| Section | Decided from | Rule |
|---|---|---|
| 1. base image | the GPU table | CPU: `python:3.X-slim`. GPU + a package that brings CUDA as pip wheels (torch; or `onnxruntime-gpu[cuda,cudnn]`, `tensorflow[and-cuda]` - an extra whose dependencies are NVIDIA's CUDA wheels, read from the package's own metadata): `python:3.X-slim` too (an nvidia/cuda base would be a second copy). GPU with a package that needs CUDA from the system: `nvidia/cuda:<CUDA>-cudnn-runtime` (+ Python in a venv) - CUDA 12.8.1, or 13.0.3 when the package's own requirements ask for CUDA 13 (`nvidia-cuda-runtime~=13.0`, `-cu13` names); onnxruntime-gpu < 1.19: CUDA 11.8 + cuDNN 8. A source package with CUDA code (`.cu`) needs nvcc: the `-devel` image. |
| 2. system libraries (apt) | the package names | the libraries a package loads when it RUNS: opencv-python -> `libgl1` glib `libx11-6` `libxcb1`, lightgbm -> `libgomp1`, psycopg2 -> `libpq5` ... |
| 3a. big frameworks | kind = HEAVY / torch, onnxruntime ... | their own `pip install` first: Docker reuses that layer while the rest changes |
| 3b. the other packages | kind | if any is an sdist with C / C++ / Cython code: `gcc libc6-dev` (+ `g++`, + the `-dev` library) installed and purged **in the same RUN**, so the compiler is not in the image |
| 4. the CUDA library folders | GPU table | only when a package that loads CUDA by name (onnxruntime-gpu, tensorflow, cupy ...) sits on a slim base with NVIDIA's CUDA wheels: one `RUN` finds, AFTER the install, every folder under `site-packages/nvidia` that holds libraries and registers it with the Linux loader (`/etc/ld.so.conf.d` + `ldconfig` = a permanent `LD_LIBRARY_PATH`). No fixed list, because the layout changes: CUDA 12 wheels use one folder per library (`nvidia/cublas/lib` ...), CUDA 13 wheels one shared folder (`nvidia/cu13/lib`), cuDNN / NCCL their own. With an nvidia/cuda base the file says why nothing is added: the base sets `LD_LIBRARY_PATH=/usr/local/cuda/lib64` and keeps cuDNN in a standard folder |
| 5. code | - | `COPY . .` - everything; X-Ray finds out what is used |

The registration, seen with your own eyes (in a container of such an image):
```bash
cat /etc/ld.so.conf.d/cuda-wheels.conf               # the folders the Dockerfile registered
P=$(python3 -c "import onnxruntime,os;print(os.path.dirname(onnxruntime.__file__))")/capi/libonnxruntime_providers_cuda.so
ldd $P | grep -E "nvidia|not found"                  # found: .../site-packages/nvidia/cudnn/lib/libcudnn.so.9 ...
```
Tested both layouts (python:3.12-slim, no GPU needed for this check): onnxruntime-gpu 1.23.2 (CUDA 12 wheels) - without
the registration 6 libraries "not found" (libcudart, libcublas, libcublasLt, libcufft, libcurand, libcudnn), with it
none; onnxruntime-gpu 1.31.0 (CUDA 13 wheels in `nvidia/cu13/lib`) - the same. What stays "not found" is `libcuda.so.1`,
the driver, which `--gpus all` brings from the host.
The only GPU part that comes from the host is the driver (`libcuda.so`): `--gpus all` mounts it in.

## 4. Step 2 - Watch it run (needs Docker and an image that runs the app)

**Shortcut:** `python xray_runfile.py --static runs/NAME/NAME.json --project PROJECT --name NAME` writes these files into
`runs/NAME/` (`.bat` on Windows, `.sh` on Linux): `build-naive`, `run-naive` (the image ON ITS OWN, no X-Ray: does it
start and do its work?), `run-step2` (+ `run-step2-cpu` for GPU apps), `run-step3-4` (package scan + plan),
`build-slim` and `run-slim` (the same run on the new image, then `verify.txt`), `run-load`, `run-explore`,
`compare-images` (two images side by side) and `videos` (can the image read your videos - converts the ones it cannot).
Run them in that order. The sections below explain what they do.

**How long it runs, loop, several videos** - the top of every run file (open it in Notepad, change, save, run):

| Setting | Meaning |
|---|---|
| `RUN_SECONDS` | the program is stopped after this many seconds (it gets Ctrl+C, like on your PC). `0` = until it ends by itself / you stop it. `600` = 10 minutes. Defaults: run-step2 / run-slim 180, run-load 300, run-explore 0, run-naive 120 |
| `LOOP` | `1` = a video FILE the program reads starts again from the first frame when it ends, so a short video keeps it working for all of `RUN_SECONDS` |
| `VIDEOS` | a folder with videos (`.mp4 .avi .mov .mkv ...`), e.g. `set "VIDEOS=D:\test videos"`: the program plays them one after the other (sorted by name) instead of its own video file; with `LOOP=1` the list starts again. A video OpenCV cannot read here (AV1) is skipped with a message. Empty = its own video |

`LOOP` and `VIDEOS` change only video FILES the program opens with `cv2.VideoCapture` (cameras and `rtsp://` streams
are never touched), and only in the X-Ray runs (run-naive has no X-Ray: it has `RUN_SECONDS` only). A program that
writes its results only at its very end (a CSV after the last frame) writes nothing when `RUN_SECONDS` stops it
first - for those, `LOOP=0` and `RUN_SECONDS=0`.
Example - the worker on the slim image for 10 minutes on a 2-minute video: in `run-slim.bat` set
`RUN_SECONDS=600` and `LOOP=1`.

**Videos: can the image read them?** `runs\NAME\videos.bat` (no folder = the folder of the program's own video, or
`videos.bat "D:\other videos"`) opens every video in the folder with the OpenCV INSIDE the image - the same library
your program uses - and reads a few frames. The ones it cannot read are converted to H.264 into the subfolder `h264`
(the originals stay), checked again, and it prints the `run.env` line for each copy; for a run file, `VIDEOS=` that
`h264` folder. It works for any format because it asks the real library, not a list. A second run skips what is
already converted. The ffmpeg it uses comes from an image it builds the first time (`xray-ffmpeg:2`, about 1 GB:
linuxserver's ffmpeg + Python). **The GPU converts when it can:** with `GPU=1` (the default, at the top of
videos.bat) the converter gets the GPU with its video part (`--gpus all -e NVIDIA_DRIVER_CAPABILITIES=compute,video,
utility`; plain `--gpus all` leaves the encoder out), encodes a one-second test video on NVIDIA's encoder
(`h264_nvenc`), and if that works converts on the GPU - much faster for big videos. Otherwise, and for a file the GPU
encoder refuses (H.264 on NVENC takes at most 4096 pixels wide), the CPU (`libx264`) does it; each line says which
(`, GPU)` / `, CPU)`), and the first line says why the GPU was not used. Docker cannot give the GPU at all (exit code
125): videos.bat runs it again on the CPU. Tested here without a GPU (the fallback); the GPU path needs your card.
What OpenCV's Linux wheel read in a test (4.13 and 5.0 the same): H.264 (8-bit, 10-bit, 4:2:2), H.265 (8-bit,
10-bit), VP8, VP9, MPEG-4, MPEG-2, MJPEG, ProRes, DNxHD, FFV1, Theora, WMV, H.263, GIF - all fine; AV1 - opens, but
no frame.

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
- the CPU run of a GPU app (`run-step2-cpu`) sets env vars whose default is `cuda` / `gpu` to `cpu` on purpose:
  onnxruntime-gpu crashes (exit code -11 or 139) when it is asked for CUDA in a container without a GPU, even with
  the CPU listed as a fallback.
Send `step2-needs.log` + the end of `step2.log` to Claude.

---


**Env vars: one file.** `xray_runfile.py` writes every env var the code reads into `runs\NAME\run.env`; all run
files read it (`--env-file`). To change or add a value, edit `run.env` once - one `NAME=value` per line, no quotes,
no comment after a value. For production, Step 4 writes `compose.yml` + `.env.example` (when the project has no
compose file): copy `.env.example` to `.env`, fill it in per machine / site (folders, MAC, passwords), and run
`docker compose up -d`. Never commit `.env`.

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
  `NOTE: smaller image: live_video.py:4 imports torch but never uses it: torch and the 16 package(s) only it needs are
  3.9 GB of this image` = an optional code change (section 3.3 c2); the Dockerfile repeats it above the pip line.
- **GPU apps** (the run really used the GPU), `--gpu-base`:
  - `auto` (default): the same `nvidia/cuda` image family as your naive Dockerfile (`-runtime`, never `-devel`),
    with Ubuntu's python3 in a venv. It is the family that already worked on your GPU.
  - `pip`: `python:X.Y-slim`, with the CUDA libraries coming from NVIDIA's pip wheels: a package with an extra that
    installs them gets it (read from its own metadata: `onnxruntime-gpu[cuda,cudnn]`, `tensorflow[and-cuda]` ...), and
    their folders are registered with the loader (section 3b, line 4). It is about 1.5-2 GB smaller. Use it only if the
    verify run says PASS: verify checks that the GPU was really used.
  - `nvidia`: the nvidia/cuda runtime image even when the naive image was python-slim (torch brought CUDA as pip
    wheels: then `auto` = `pip`, and step4.txt says so) - CUDA 12.8.1, or 13.0.3 when the image's CUDA wheels are CUDA
    13 ones (CUDA 13 needs driver 580+). Build the other variant and compare the two with `compare-images.bat`
    (section 6.2).

You get `plans/app/Dockerfile` (every line has a comment why), `Dockerfile.dockerignore`, `compose.xray.yml`,
`plan.json`. Read the Dockerfile before building.

No compose file in the project: you get `compose.yml` + `.env.example` instead. `.env.example` mounts the same input
folders as the run files (read-only, taken from the code defaults, e.g. `MODEL_ONNX_DIR=C:/.../models`) and points the
env vars at the paths inside the container. If the code opens windows (`cv2.imshow`), a container has no screen: switch
them off the way your code allows before a run without X-Ray. `compose.yml` says `restart: unless-stopped`: right for
a service that should run forever (cameras, an API).
A program that works through one file and ends is started with `docker compose run --rm SERVICE`; with
`docker compose up -d` Docker would start it again every time it finishes.

### 6.2 Build it and compare sizes
```bash
docker build -f plans/app/Dockerfile -t app-xray PROJECT_FOLDER
docker images | grep -e app-xray -e NAIVE_IMAGE_NAME
```
With compose: `docker compose -f docker-compose.yml -f PATH/TO/plans/app/compose.xray.yml build SERVICE`
(the exact command is written at the top of `compose.xray.yml`).

**What makes the difference - two images side by side.** `runs\NAME\compare-images.bat` (written by xray_runfile.py)
looks inside both images and writes `runs\NAME\compare\compare.txt`: Linux and Python of each, the sizes of apt and
pip packages, the folders that differ most, every package only one image has (biggest first) - with "used in the run" /
"NOT used in the run" from each image's X-Ray run - versions that differ, and where each image gets each CUDA library
(apt package of the base image, or a pip wheel). Default: NAME-naive against NAME-slim. Any two images:
```bat
runs\NAME\compare-images.bat NAME-slim-auto NAME-slim
```
That is how you see why one base is smaller for one project and bigger for another: e.g. an nvidia/cuda base brings
cuSOLVER, cuSPARSE, NCCL, NPP (about 1.4 GB) that onnxruntime never loads; a python base with torch brings torch's own
CUDA wheels, so adding an nvidia/cuda base under it would be a second copy.

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
| `VIDEOIO(FFMPEG): backend is generally available but can't be used to capture by name`, then the program's own "camera not connected" again and again | OpenCV cannot open that name: in a container, usually a file of your PC (`C:\...\video.mp4` in the code's config). Step 1 lists it as `pcfile`; run Step 1 and xray_runfile.py again on the PC where the file is, and the run files mount it (section 17b) - or set SOURCE at the top of the run file to a video of this PC (17a) |
| a run ends with exit code 137 | the program did not end within 30 s of Ctrl+C, so Docker killed it. Normal for a program with child processes (each camera / worker in its own process) - not an error. Or out of memory: `docker stats` while it runs |
| `could not select device driver "" with capabilities: [[gpu]]` | `--gpus all` without the NVIDIA container toolkit on that machine |
| `timed out waiting for /mnt/wslg` | Docker Desktop cannot show windows: use `--gui off` |
| verify says `CHECK` | send `step4-verify.log` |
| `'SP' is not recognized as an internal or external command` (or `ldd`, `grep`, `$PID` ...) | a command for INSIDE the container typed in CMD: first `docker exec -it NAME bash`, then type it there (section 0) |
| inside: `python: command not found` | that image has `python3` only: type `python3` |
| inside: `ls: cannot access '/proc//fd'` | `PID` is empty: run the `PID=...` line of section 10b first, or type the number (`PID=25`) |
| build log: `ERROR: pip's dependency resolver does not currently take into account ...` / `pip check` lines | expected when naive-check.txt lists INSTALLED IN A SECOND STEP lines: the same warning your PC had, the build goes on |
| the image builds, the program says `No module named ...` / `undefined symbol` / `numpy.core.multiarray failed to import` | `xray_look.py --image` (section 10a): every package is imported and each failure gets its reason |
| `docker compose run`: lines you pasted after it appear in the program's output | `compose run` connects your keyboard to the program while it runs: paste the next commands only after it ended |
| `docker compose run` (no X-Ray): `Could not load the Qt platform plugin "xcb"` | the window: `.env.example` from xray_plan sets the code's own window switch off (e.g. `SHOW=0`); a `.env` copied before that needs the line added |
| onnxruntime runs on the CPU in a python-slim GPU image (`CUDAExecutionProvider` missing, `Failed to load library libonnxruntime_providers_cuda.so ... cannot open shared object file`) | the CUDA wheels' folders are not known to the loader. `xray_look.py --image`: "registered with the loader" must list the `site-packages/nvidia/...` folders; a note names the folders nobody searches. The X-Ray Dockerfiles register them (section 3b, line 4); a hand-written one needs the same `RUN` (or `LD_LIBRARY_PATH` with exactly those folders - CUDA 13 wheels use `nvidia/cu13/lib`) |
| the slim image is still big, and most of it is torch (or another big package) the code hardly touches | the needs report's OPTIONAL CODE CHANGES / the plan's `NOTE: smaller image:`: a package imported and never used (section 3.3 c2). `xray_look.py --image` lists the biggest packages of any image |
| `ResolutionImpossible` in a naive build | the requirements file changed after the check: run xray_naive.py again and use the new Dockerfile.naive |
| `Could not load the Qt platform plugin "xcb" ... even though it was found`, exit code 134 or 139 | the program opened a window (`cv2.imshow`) and a container has no screen. `run-naive` finds the code's own switch (e.g. `if SHOW:` with `SHOW = os.getenv("SHOW", "1") == "1"`) and sets it off - its WINDOWS note at the top says which. A switch written as a constant (`SHOW = True`) or no switch at all: `code-changes.txt` has the line to change (`SHOW = os.getenv("SHOW", "1") == "1"`); until then use run-explore / run-load (X-Ray switches windows off) |
| `[av1 @ ...] Your platform doesn't support hardware accelerated AV1 decoding`, `Failed to get pixel format`, no frames | the video is AV1. OpenCV's Linux wheel has no software AV1 decoder (its own AV1 decoder needs hardware decoding, which a container does not have) - same in OpenCV 4.13 and 5.0. Run `runs\NAME\videos.bat "D:\your folder"`: it checks every video with the image's own OpenCV and converts the ones it cannot read to H.264 (section 4, "Videos"). `naive-check.txt` and `run.env` show each video's codec; with `VIDEOS` an unreadable video is skipped with a message |
| run-naive: `The program ended with exit code N` | the line below it says what N means (1 = a Python error, 134 / 139 = usually a window without a screen, 137 = stopped after 30 s or out of memory) |
| `cannot open video: /xin/...` after `VIDEOIO(CV_IMAGES) ... icvExtractPattern` | the video is not at that path INSIDE the container (the OpenCV message is misleading: when FFmpeg cannot open the file, OpenCV tries the name as an image-sequence pattern). Check: `docker run --rm -v "D:\your folder:/xin/video_in" --entrypoint "" IMAGE ls -la /xin/video_in` - is the file listed, with exactly that name? Nothing listed: Docker Desktop cannot see that drive / folder (Settings -> Resources -> File sharing), or the folder path is wrong |
| build log: `debconf: unable to initialize frontend: Dialog` / `delaying package configuration, since apt-utils is not installed` | harmless notes from apt (no screen to ask questions on). The Dockerfiles set `ARG DEBIAN_FRONTEND=noninteractive`, which removes the first one |
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

## 10. Look inside a container - for ANY project, without knowing its file or package names

Two ways, both read-only (nothing is changed): **10a** X-Ray's look tool, **10b** the same by hand with standard
commands, for a PC without X-Ray. `NAME` is the container's name (`docker ps`, last column); X-Ray's explore run
names it `APP-explore`, run-naive `APP-run`. Start the program first (run-explore / run-naive / your own
`docker run`), then use a SECOND CMD window.

### 10a. With X-Ray: xray_look.py

In CMD (X-Ray's run files mount the tools at `/xray` inside the container):
```bat
docker exec -it NAME python /xray/xray_look.py              & REM one look: what runs, what is open, what is loaded
docker exec -it NAME python /xray/xray_look.py --watch 5    & REM every 5 s: what CHANGED (Ctrl+C to stop)
docker exec -it NAME python /xray/xray_look.py --all        & REM every library, not only the summary
```
Any other container (no `/xray` in it): copy the one file in first.
```bat
docker cp xray_look.py NAME:/tmp/xray_look.py
docker exec -it NAME python /tmp/xray_look.py
```
It shows, per process: the files open right now (models, videos, logs - `WRITING` = being written; `mounted from
your PC` = it came with `-v`), network connections, RAM now / peak, threads, and every loaded library grouped by
where it comes from: `pip package X`, `apt package Y`, `GPU driver from your PC`, the base image. `[CUDA]` marks the
GPU libraries, and the GPU line says whether the program really runs on the GPU. `--watch` prints `+opened`,
`-closed`, `+loaded`, `+network` as they happen.

**An image, nothing running** (does everything in it work?):
```bat
docker run --rm --gpus all -v "%CD%:/x:ro" --entrypoint "" IMAGE python /x/xray_look.py --image
```
(run it in the xray-tools folder; leave out `--gpus all` on a PC without an NVIDIA GPU.) It shows: which Python and
`LD_LIBRARY_PATH` (each folder checked), the biggest pip packages, the system libraries the pip packages need and
the apt package each comes from, MISSING ones with the `apt-get install` that adds them, the CUDA libraries in the
image, then **DOES EVERY PACKAGE IMPORT?** - every pip package is imported in a separate Python, the way your code
does, and each failure gets its reason (`built for NumPy 1.x, but NumPy 2.x is installed`, `'six' is missing`,
`compiled for another version of torch`, `a system library is missing`), packages that installed the same files
(two OpenCVs), and last a GPU test per framework (torch: `GPU: True` + the card; onnxruntime: its providers).
Under every section of its report a `by hand:` line gives the standard command that shows the same thing (section
10b), with the real PID filled in. Those commands ask Python where its packages are
(`python3 -c "import site; print(site.getsitepackages()[0])"`), so they fit any image: system Python
(`/usr/local/lib/python3.12/site-packages`) or a venv (`/opt/venv/lib/python3.12/site-packages`).

**Two images side by side** (the same project on two bases, naive against slim ...): `compare-images.bat`
(section 6.2). By hand it is `xray_look.py --inventory` inside each image, then `xray_look.py --compare a.json b.json`.

### 10b. By hand - the same, with standard commands (no X-Ray needed)

**From CMD** (no shell inside needed):
```bat
docker ps                                       & REM running containers: NAME is the last column
docker top NAME                                 & REM its processes (PID as seen from the PC)
docker stats NAME                               & REM live CPU / RAM / network (Ctrl+C to stop)
docker logs -f NAME                             & REM what the program prints
docker diff NAME                                & REM every file it ADDED (A) / CHANGED (C) / DELETED (D) since start - outside mounts
docker inspect -f "{{range .Mounts}}{{.Source}} -> {{.Destination}}{{println}}{{end}}" NAME   & REM what is mounted from your PC
docker inspect -f "{{range .Config.Env}}{{println .}}{{end}}" NAME                             & REM the env vars it got
docker exec -it NAME bash                       & REM a shell INSIDE (sh if there is no bash; -u 0 = as root)
```
**Inside** (after `docker exec -it NAME bash`) - these need no names and no extra tools, so they work in any
Debian / Ubuntu based image, slim ones too (`ps`, `lsof`, `ss` are often not installed; `/proc` always is):
```bash
# 1. every process and its command line (PID first)
for p in /proc/[0-9]*; do echo "${p#/proc/}  $(tr '\0' ' ' < $p/cmdline 2>/dev/null)"; done
# 2. pick the program: the first Python process that is not X-Ray (or type it: PID=25)
PID=$(for p in /proc/[0-9]*; do c=$(tr '\0' ' ' < $p/cmdline 2>/dev/null); case "$c" in *xray_*) ;; *python*) echo ${p#/proc/};; esac; done | sort -n | head -1); echo "PID=$PID: $(tr '\0' ' ' < /proc/$PID/cmdline)"
# 3. files it has open RIGHT NOW (models, videos, logs; sockets = network)
ls -l /proc/$PID/fd | grep -v -e 'pipe:' -e 'anon_inode:' -e ' /dev/'
ls -l /proc/$PID/cwd                                                     # the folder it runs in
# 4. libraries it has loaded now, and how many
grep -o '/[^ ]*\.so[^ ]*' /proc/$PID/maps | sort -u
grep -o '/[^ ]*\.so[^ ]*' /proc/$PID/maps | sort -u | wc -l
# 5. the apt package of each system library (a library with no package = a pip wheel, Python itself, or the driver)
grep -o '/[^ ]*\.so[^ ]*' /proc/$PID/maps | sort -u | grep -v -e site-packages -e dist-packages | xargs dpkg -S 2>/dev/null | cut -d: -f1 | sort -u
# 6. the pip packages whose compiled parts are loaded
grep -o '/[^ ]*-packages/[^/]*' /proc/$PID/maps | sort -u | sed 's|.*-packages/||'
# 7. GPU: CUDA libraries loaded (none = it runs on the CPU, or has not reached its GPU code yet) + GPU memory
grep -o '/[^ ]*\.so[^ ]*' /proc/$PID/maps | sort -u | grep -iE 'cuda|cudnn|cublas|cufft|curand|nvrtc|nvjit|libnv' || echo "no CUDA library loaded"
nvidia-smi --query-compute-apps=pid,used_memory --format=csv             # only with --gpus all
# 8. RAM now / peak, threads
grep -E 'VmRSS|VmHWM|Threads' /proc/$PID/status
# 9. the env vars the program really got
tr '\0' '\n' < /proc/$PID/environ | sort | cut -c1-150
# 10. watch the open files change, every 2 s (Ctrl+C stops the watching, not the program)
while sleep 2; do clear; date; ls -l /proc/$PID/fd | grep -v -e 'pipe:' -e 'anon_inode:' -e ' /dev/'; done
# 11. where pip puts packages, and which Python runs
SP=$(python -c "import site;print(site.getsitepackages()[0])"); echo $SP; which python; python --version
```
**Which Python code runs right now** (the function / line, not only the file): `py-spy` reads the program's stack
from outside. It needs internet in the container and `--cap-add SYS_PTRACE` (run-explore has it):
```bash
pip install py-spy && py-spy dump --pid $PID          # every thread: file, function, line
```
**Network** (slim images have no `ss`): `apt-get update && apt-get install -y iproute2 && ss -tnp` (as root:
`docker exec -u 0 -it NAME bash`), or `xray_look.py`, which reads `/proc/net/tcp` itself.

What a look by hand cannot see, and X-Ray can: a file the program read for one second at its start (gone from
`/proc/PID/fd` a moment later), which lines of your code ran, an env var read with a computed name. A look is one
moment; X-Ray's report covers the whole run.

**Look inside an image (nothing of the app runs):**
```bat
docker run --rm -it --entrypoint bash IMAGE
```
Then, inside (type `exit` to leave):
```bash
id                                    # which user the app runs as (root or app)
ls -la /app                           # the files that are in the image
du -sh /app/* | sort -h               # their sizes
pip list                              # installed packages
du -sh $(python -c "import site;print(site.getsitepackages()[0])")/* | sort -h | tail -15   # biggest packages
pip check                             # versions that do not match what another package asks for
cat /etc/os-release                   # which Linux
```
**The GPU from inside a container** (in CMD):
```bat
docker run --rm --gpus all --entrypoint "" IMAGE nvidia-smi
docker run --rm --gpus all --entrypoint "" IMAGE python -c "import torch; print(torch.cuda.is_available())"
docker run --rm --gpus all --entrypoint "" IMAGE python -c "import onnxruntime as o; print(o.get_available_providers())"
```
**Compare two images** (in CMD):
```bat
docker images
docker history IMAGE                  & REM which Dockerfile line added how many MB (--no-trunc: full commands)
REM docker image ls on newer Docker: CONTENT SIZE = the compressed download, DISK USAGE = download + unpacked files;
REM older Docker shows one SIZE column = the unpacked size
docker run --rm --entrypoint "" app-naive ls /app
docker run --rm --entrypoint "" app-slim ls /app
```
**Check one X-Ray claim by hand**, e.g. "folder `old_models/` is not used": while the app runs,
`ls -l /proc/*/fd 2>/dev/null | grep old_models` shows nothing -> nothing has it open. (A short look can miss a
file that is read once at start; X-Ray watches the whole run, and verify proves it: the small image works without it.)


## 11. Watch a whole run by hand - what X-Ray does, with standard Linux tools

The same questions X-Ray answers, answered with `strace` (every file / network call), `/proc`, `lsof`, `ss`.
Use the NAIVE image (it runs as root, so tools can be installed inside; `--rm` throws them away afterwards).

**Window 1 - start a container that may be watched** (`SYS_PTRACE` lets strace / py-spy attach; `/out` is a
folder on your PC for the results):
```bat
mkdir manual
docker run --rm -it --gpus all --shm-size 2g --cap-add SYS_PTRACE --security-opt seccomp=unconfined ^
  -v "%CD%\manual:/out" --entrypoint bash IMAGE
```
inside (Ubuntu / Debian based image):
```bash
apt-get update && apt-get install -y strace lsof procps iproute2
touch /tmp/start                                     # marks the start: "files written after this"
strace -f -e trace=openat,connect -o /out/trace.txt python -X importtime main.py 2> /out/imports.txt
```
(`-f` follows every child process; `-X importtime` lists every import. Let it run a few minutes, then Ctrl+C.)

**Window 2 - look while it runs:**
```bat
docker ps
docker exec -it CONTAINER_ID bash
```
```bash
ps -ef                                               # all processes (PID column 2)
lsof -p PID | grep -E '\.onnx|\.pt|\.so'             # files + libraries that process has open now
ss -tnp                                              # network connections now (cameras :554, database :5432)
nvidia-smi --query-compute-apps=pid,used_memory --format=csv     # GPU memory per process
df -h /dev/shm; ls -la /dev/shm                      # shared memory
grep -E 'Threads|VmRSS|VmHWM' /proc/PID/status       # threads, RAM now, RAM peak
pip install py-spy && py-spy dump --pid PID          # what that Python process is doing right now
```

**After Ctrl+C (still in window 1) - read the recording** (the files are also in `manual\` on your PC):
```bash
# models / data files the run opened
grep -E '\.onnx|\.pt"|\.pth|\.names|\.json"|\.yaml' /out/trace.txt | grep -v ENOENT | awk -F'"' '{print $2}' | sort -u
# files it wrote (+ everything changed since the start)
grep -E 'O_WRONLY|O_RDWR' /out/trace.txt | grep -v ENOENT | awk -F'"' '{print $2}' | sort -u
find / -xdev -newer /tmp/start -type f 2>/dev/null | grep -vE '^/(proc|sys|out)/'
# network: which IP:port it connected to
grep 'connect(' /out/trace.txt | grep -oE 'sin_port=htons\([0-9]+\), sin_addr=inet_addr\("[0-9.]+"\)' \
  | sed -E 's/sin_port=htons\(([0-9]+)\), sin_addr=inet_addr\("([0-9.]+)"\)/\2:\1/' | sort -u
# system libraries loaded, and which apt package each comes from
grep '\.so' /out/trace.txt | grep -v ENOENT | awk -F'"' '{print $2}' | grep -v site-packages | sort -u > /out/libs.txt
xargs -a /out/libs.txt dpkg -S 2>/dev/null | cut -d: -f1 | sort -u
# Python modules imported (top level names)
awk -F'|' '{print $3}' /out/imports.txt | sed 's/^ *//' | cut -d. -f1 | sort -u
# project files that were NEVER opened (candidates for .dockerignore - check them before deleting anything)
grep -v ENOENT /out/trace.txt | awk -F'"' 'NF>1 {p=$2; if (p !~ /^\//) p="/app/" p; print p}' | grep '^/app/' | sort -u > /out/opened.txt
find /app -type f | sort > /out/present.txt
comm -23 /out/present.txt /out/opened.txt
# env vars the code reads (from the code itself)
grep -rn --include=*.py -E 'getenv|environ' /app
```
What this does not show, and X-Ray does: which lines ran, files only some code paths use (a short run can miss
them - run longer, or start from the code), env vars read with a computed name, and the before/after check
(verify) that the small image still does the same.


**Windows CMD equivalents** (no PowerShell needed anywhere):

| Want | CMD |
|---|---|
| the end of a log | `notepad runs\NAME\step2.log`, then Ctrl+End |
| watch a log while it grows | open it again in Notepad (it shows the current state), or `type runs\NAME\build.log` |
| search in files | `findstr /s /n /c:"text" *.py` |
| can this PC reach a host:port? | `docker run --rm busybox nc -zv -w 3 192.168.1.75 5432` ("open" = yes) |
| ping a camera | `ping 10.10.10.17` |
| folder size | Explorer: right-click the folder -> Properties |


## 12. Explore mode - the app keeps running, you look inside, then compare with X-Ray

`run-explore.bat` (made by `xray_runfile.py`) starts the NAIVE image with X-Ray watching, with `RUN_SECONDS=0` (no
time limit) and `LOOP=1` (a video FILE starts again from the beginning when it ends; camera streams are not touched).
Change both at the top of the file (section 4: e.g. `RUN_SECONDS=600` stops it by itself after 10 minutes; `VIDEOS`
plays a folder of videos). The container has a fixed name, `NAME-explore`, so you never need to look up its ID, and
it can be watched (`--cap-add SYS_PTRACE`: py-spy, strace).

**Window 1:**
```bat
runs\NAME\run-explore.bat
```
**Window 2** (as long as you like):
```bat
docker exec -it NAME-explore python /xray/xray_look.py --watch 5
docker top NAME-explore
docker stats NAME-explore
docker exec -it NAME-explore bash
```
then everything from sections 10b and 11 works inside (the image runs as root, so `apt-get install -y strace lsof
procps iproute2` works if the PC has internet). `strace` can also attach to the RUNNING process:
`strace -f -p PID -e trace=openat,connect -o /tmp/t.txt` (Ctrl+C to stop it; the app keeps running).

**Stop** (in any window): `docker stop -t 60 NAME-explore` - the app gets Ctrl+C, X-Ray saves its recording and
window 1 writes `runs\NAME\explore\needs.txt`. With `RUN_SECONDS` set it stops by itself.

**Compare** - what you saw by hand, and where X-Ray has the same thing:

| By hand | In `explore\needs.txt` / `explore\runtime.json` |
|---|---|
| `xray_look.py`, `ls -l /proc/PID/fd` - video / model files open | YOU PROVIDE: video / data / model lines |
| `xray_look.py` NETWORK, `ss -tnp` - camera :554, database :5432 | YOU PROVIDE: network lines (one per host) |
| `tr '\0' '\n' < /proc/PID/environ`, `grep -rn getenv` | YOU PROVIDE: env lines |
| `xray_look.py` LIBRARIES, `/proc/PID/maps` + `dpkg -S` | Step 3 (`xray_pkg.py`) turns these into apt packages |
| `docker stats`, `VmHWM` in `/proc/PID/status` | WHAT THE RUN USED: RAM peak, cores |
| `nvidia-smi` | WHAT THE RUN USED: GPU line |
| `docker diff NAME-explore`, `find / -newer /tmp/start` | files written (logs / output folders) |

Differences are normal in one direction: a look by hand sees one moment, X-Ray sees the whole run (a file read
once at start is gone from `/proc/PID/fd` a second later, but X-Ray saw it open).

Files a program writes only at its end (a CSV after the last frame) never appear in explore mode: the video loops,
so the end never comes. The Step 2 run covers them.


## 13. Prove each Dockerfile line yourself

Every line of a Dockerfile has a reason you can see inside the image. Use a throwaway container (`--rm` deletes it
on `exit`, so you can break things freely). In CMD:
```bat
docker run --rm -it --entrypoint bash IMAGE
```
then type these INSIDE (the prompt is `root@...:/app#`, not `C:\...>`). `xray_look.py --image` (section 10a) does
most of it in one go; this is the same by hand. Each generated Dockerfile (Dockerfile.naive and the plan's) also ends
with a **CHECK BY HAND** block: one command per choice it made - its packages, its apt libraries, the CUDA folders it
registered, the user, the copied code - written for THAT Dockerfile's layout:
```bash
which python; python --version; echo $PATH          # which Python, and is the venv first on PATH
SP=$(python -c "import site;print(site.getsitepackages()[0])"); echo $SP     # where pip put the packages
pip check                                           # versions that do not match what another package asks for
# every system library a compiled file of a pip package needs and that is NOT in the image (empty = none missing)
find $SP -name '*.so*' -type f -exec ldd {} + 2>/dev/null | awk '/:$/{f=substr($0,1,length($0)-1)} /not found/{print $1, f}' | sort -u | while read lib f; do [ -z "$(find $SP -name "$lib" -print -quit)" ] && echo "MISSING $lib   needed by $f"; done
# the GPU part of every package, and the CUDA libraries it opens ("not found" = it falls back to the CPU)
for f in $(find $SP -name '*cuda*.so*' -type f); do echo "== $f"; ldd $f | grep -E 'libcu|libnv|not found'; done
echo $LD_LIBRARY_PATH                               # extra folders Linux searches for libraries
dpkg -S $(readlink -f /usr/lib/x86_64-linux-gnu/libz.so.1)   # which apt package brought a file (any path; no answer = a pip wheel, Python itself or the driver)
dpkg-query -W -f='${Installed-Size}\t${Package}\n' | sort -rn | head -15    # the biggest apt packages (KB)
apt-cache rdepends --installed PACKAGE              # who pulled a big apt package in (a name from the line above)
# break one on purpose and read the error a too-small image would give (here: a library cv2 needs)
mkdir -p /tmp/hidden && mv /usr/lib/x86_64-linux-gnu/libGL.so.1* /tmp/hidden/ && python -c "import cv2"
```
With the GPU (`docker run --rm -it --gpus all ...`): `ldconfig -p | grep libcuda` shows the driver library, and
`dpkg -S` on it finds no package. It is not in the image: `--gpus all` mounts it in from the host (on Windows from
a folder under `/usr/lib/wsl`). Hide `libcudnn.so.9` the same way and ask onnxruntime for CUDA: it prints
`Failed to load library ... libcudnn.so.9` and runs on the CPU. That is the silent slowdown X-Ray's
"requested vs active" line catches.


## 14. How it fits together - the words behind the Dockerfile lines

**Image, container, layer.** An image is a set of folders (a small Linux + your program). Every `RUN` / `COPY` line
adds one layer on top. Docker keeps the layers and reuses one when nothing above it changed - that is why the big,
rarely changing packages (torch, onnxruntime) get their own `RUN` first: change a small package later and only the
lines from there on are rebuilt. A container is an image that runs.

**Folders like `/usr/local/cuda/lib64`.** Every path in a Dockerfile or in the run files is a folder INSIDE the
image / container, never on your PC (except the left side of a `-v` mount). Linux keeps compiled libraries (`.so`
files, like `.dll` on Windows) in "lib" folders: `/usr/lib/x86_64-linux-gnu` (Debian / Ubuntu's standard place),
`/usr/local/cuda/lib64` (where the nvidia/cuda base puts CUDA; `lib64` = the 64-bit libraries), `site-packages/`
(where pip puts Python packages, some with their own `.so` files inside).

**How a library is found.** A program asks for a library by NAME (`libcudnn.so.9`). Linux looks in: the folders
written into the library that asks (RPATH - torch uses this for its own nvidia-* wheels, so torch needs no setting),
then `LD_LIBRARY_PATH`, then the folders registered in `/etc/ld.so.conf.d` (`ldconfig`; the X-Ray Dockerfiles register
the CUDA wheels' folders there), then the standard folders. Nothing is copied from the PC and nothing is compiled when the
program runs: not found = `ImportError: ... cannot open shared object file` (cv2), or a silent switch to the CPU
(onnxruntime). The compiler (`gcc`) is only ever used while BUILDING, by pip, for a package that has no ready-made
Linux wheel. Examples of system libraries packages expect: `libGL.so.1` (opencv-python, apt `libgl1`),
`libgomp.so.1` (lightgbm, `libgomp1`), `libpq.so.5` (psycopg2, `libpq5`), `libportaudio.so.2` (pyaudio).

**apt and pip are different installers.** apt installs Linux packages from Debian / Ubuntu: libraries (`libgl1`),
programs (`ffmpeg`), Python itself (`python3`). pip installs Python packages from PyPI (`opencv-python`,
`onnxruntime-gpu`). `apt-get update` downloads apt's package list (about 40 MB); `rm -rf /var/lib/apt/lists/*` deletes
that list again in the same `RUN`, so it does not stay in the image. Installed packages are not touched.
"System libraries" = the `.so` files that came with apt packages (the base image's own, plus the apt line).

**CUDA libraries** (libcudart, cuBLAS, cuDNN, cuFFT ...) are made by NVIDIA. Every GPU framework (torch, onnxruntime,
tensorflow ...) calls them, and each framework version is built for certain versions of them. They reach an image in
one of two ways - both INSIDE the image, so it runs on any PC with an NVIDIA GPU: NVIDIA's apt packages (the
`nvidia/cuda:...-runtime` base image has them in `/usr/local/cuda`), or NVIDIA's pip wheels (`nvidia-cublas-cu12` ...,
which torch depends on, and onnxruntime-gpu gets with `[cuda,cudnn]`). The same files, packed two ways. The only part
from your PC is the DRIVER (`libcuda.so`, `nvidia-smi`): `docker run --gpus all` mounts it in.

**Will the GPU work on another PC?** Three things must fit:
1. the driver on that PC: `nvidia-smi` (top right, "CUDA Version") must be at least the CUDA of the image
   (CUDA 12.8 needs driver 570 or newer, CUDA 13 driver 580 or newer);
2. the CUDA libraries must know the card: CUDA 12.8 is the first that knows RTX 50xx (Blackwell); CUDA 13 no longer
   supports cards older than RTX 20xx / GTX 16xx;
3. the framework's own GPU code must be built for the card: torch `+cu126` stops at RTX 40xx (so it does not run on
   an RTX 50xx even on a CUDA 12.8 base); `+cu128` includes RTX 50xx. An RTX 3060 works with all of them.
`docker run --rm --gpus all nvidia/cuda:12.8.1-base-ubuntu24.04 nvidia-smi` checks the first one on any PC: an
NVIDIA GPU, a driver for CUDA 12.8, and Docker that can pass the GPU (Windows: Docker Desktop with WSL 2; Linux: the
NVIDIA Container Toolkit). Errors: `could not select device driver` = no GPU support in Docker; `unsatisfied
condition: cuda>=12.8` = driver too old (use an older tag like `12.4.1-base-ubuntu22.04` to test, and an image with
that CUDA).

**Python in an image.** `python:3.12-slim` = Debian + Python 3.12 + pip. `nvidia/cuda` images have no Python at all
(a program like any other: it has to be installed). Ubuntu's `python3` (apt) comes without pip, and since Ubuntu 23.04
pip refuses to install into it (`externally-managed-environment`: apt owns those files). So the Dockerfile makes a
virtual environment - a folder (`/opt/venv`) with its own `python`, `pip` and `site-packages`, created by Python's
own `venv` module (like the `.venv` PyCharm makes). `ENV PATH=/opt/venv/bin:$PATH` puts it first, so `python` and
`pip` mean the venv's in every later line and in the running container; nothing is copied. Other tools do the same
job (virtualenv, conda, uv); `venv` comes with Python.

**Two packages, the same files.** pip installs every package into one folder. `opencv-python` and
`opencv-python-headless` both put their files into `cv2/`; with both in the requirements the one installed last
overwrites the other's files. `xray_naive.py` keeps one (section 3b, SAME FILES); `xray_look.py --image` shows it in
a built image ("PACKAGES THAT INSTALLED THE SAME FILES"). Note: opencv-python's wheel brings its own FFmpeg
(`opencv_python.libs/libavcodec...`) - no system FFmpeg is needed to read videos; the non-headless one needs the X11 /
OpenGL libraries from apt (`libgl1`, `libxcb1` ...) even when no window is ever opened.

## 15. What X-Ray reads, and what it knows - so anyone can extend it

**Read from your files, for any project.** Almost every decision comes from data: the code (imports and whether their
names are ever used, env vars and their defaults, the `if` around a window call, `if os.name == "nt":` and variables
that hold such a test, paths, hosts), the run (what really loaded, which GPU provider ran, which files were opened),
the image (the installed files, which libraries each compiled file needs - `ldd`, where the CUDA wheels put their
libraries), and each package's own metadata (dependencies, extras that install NVIDIA's CUDA wheels, the CUDA major a
wheel is built for). No project's paths, names or packages are written into the tools.

**Known from lists.** Some facts are not in any file - e.g. which call opens a window, or what tkinter prints without
a screen. They sit in a few lists; a library that is in none of them is not recognized by that rule, and one added line
extends the rule for every project:

| Knowledge | List | File |
|---|---|---|
| calls / imports that open a window | `GUI_CALLS`, `GUI_IMPORTS` | xray_static.py |
| what each GUI library does without a screen | `NO_SCREEN` | xray_runfile.py |
| system libraries a package needs when it runs, before any image exists | `RUNTIME_APT` | xray_naive.py |
| ... and from a library / program name to its apt package | `LIB_TO_APT`, `BIN_TO_APT`, `APT_BY_DIST` | xray_static.py |
| packages that load CUDA by name although their metadata does not say so | `SYSTEM_CUDA` | xray_naive.py |
| CPU / GPU / headless variants, binary swaps | `VARIANTS`, `GUI_TO_HEADLESS`, `BINARY_SWAP` | xray_static.py |
| Windows-only packages | `WINDOWS_ONLY` | xray_naive.py |
| headers a compiled package needs (`-dev`) | `DEV_LIBS` | xray_naive.py |
| model / config / key file types | `MODEL_EXT`, `CONFIG_EXT`, `KEY_EXT` | xray_static.py |
| file, network, process calls it follows | `IO_CALLS`, `NET_CALLS`, `SUBPROC` ... | xray_static.py |
| import errors explained | `HINTS` | xray_look.py |
| video formats OpenCV cannot read | `CODEC_NOTE` | xray_runfile.py |
| the apt package of a MISSING library (a hint only: the library is found missing by `ldd`, whatever it is; one not in the list gets `apt-file search NAME`) | `APT` | xray_look.py |
| where a project names its Python version | `project_python` | xray_naive.py |
| which OpenCV calls open windows and are switched off by NO_WINDOWS | `_windows_off` | xray_no_windows/sitecustomize.py |
| what a path of one PC looks like in code (`C:\`, `D:/`, `/home/NAME/`), and video extensions | `PC_FILE_RE`, `VIDEO_EXT` | xray_static.py |
| which calls get the container path for a PC path (VideoCapture, imread, open, os.path, onnxruntime), and which one SOURCE replaces (VideoCapture) | `PATCH`, the `builtins` / `posixpath` wraps, `_patch_cv2` | xray_inputs/sitecustomize.py |
| which addresses SOURCE passes on as a stream; video files it takes from a folder | the `rtsp` / `rtmp` / `http` / `udp:` lines of `source_lines`; `VIDEO_EXT` | xray_runfile.py; xray_inputs/sitecustomize.py |

**The safety net for anything a list does not know.** Once an image exists X-Ray stops guessing: the traced run
records what really loaded, `xray_look.py --image` imports every package and lists every library that is missing
(whatever the library), and verify compares the slim image's run with the naive one's. A new library the lists do not
know still shows up there - as a failed import, a missing `.so`, or a difference in verify.


## 16. Windows (cv2.imshow) in a container: switch them off, or see them on your screen

A container has no screen. A program that opens a window there stops (OpenCV: `Could not load the Qt platform plugin
"xcb"`, exit code 134 or 139). X-Ray handles that in three places, none of them needs the name of a variable:

| Where | What happens to the windows | How |
|---|---|---|
| X-Ray's runs (run-step2, run-explore, run-load, run-slim) | not opened; what each window WOULD show is saved | `--gui off`: X-Ray replaces OpenCV's window calls (`imshow`, `waitKey`, `namedWindow` ...) inside the library itself, for every window the program opens. Every 5 s one picture per window name goes to `runs\NAME\trace\gui\` (`load-A\trace\gui\` ...): open them to see what the windows showed |
| run-naive and the plan's compose (no X-Ray in them) | not opened | the program's OWN switch when it has one in an env var (`-e SHOW=0`, read from the `if` around the window call); otherwise **NO_WINDOWS=1**: the tools' folder `xray_no_windows` goes on `PYTHONPATH`, and its `sitecustomize.py` (Python runs it at start) makes OpenCV's window calls do nothing. No code change, any number of windows, any window names. `NO_WINDOWS=0` = the program as it is |
| run-window (new) | opened on YOUR screen | see below |

`NO_WINDOWS` covers OpenCV (`cv2`) - the window calls of almost every vision project. Another GUI library (tkinter,
pygame, Qt widgets) has no such switch: there the code needs its own (`code-changes.txt` says where). The code's own
switch is still the cleaner way for production: `code-changes.txt` lists it under OPTIONAL when NO_WINDOWS already
makes it run.

**Seeing the windows on your screen (run-window.bat).** Two things are needed:
1. **A screen the container can reach.** Windows 11 has one built in: WSLg (Windows' own Linux display), which Docker
   Desktop shares. run-window.bat passes it:
   ```bat
   docker run --rm -e DISPLAY=:0 -e WAYLAND_DISPLAY=wayland-0 -e XDG_RUNTIME_DIR=/mnt/wslg/runtime-dir ^
     -e PULSE_SERVER=/mnt/wslg/PulseServer -e QT_X11_NO_MITSHM=1 ^
     -v /run/desktop/mnt/host/wslg/.X11-unix:/tmp/.X11-unix -v /run/desktop/mnt/host/wslg:/mnt/wslg IMAGE
   ```
   Windows 10 has no WSLg: install an X server (VcXsrv; tick "Disable access control" when it starts) and use
   `-e DISPLAY=host.docker.internal:0.0` instead of those display lines. A Linux desktop: `-e DISPLAY=$DISPLAY
   -v /tmp/.X11-unix:/tmp/.X11-unix` (run-window.sh), after `xhost +local:`.
2. **The window libraries in the image.** A server image lacks them, and then even WITH a screen OpenCV prints the
   same `Could not load the Qt platform plugin "xcb" even though it was found`. `xray_look.py --image` names them:
   `... the Qt window plugin (cv2.imshow on a screen) -> apt-get install -y libsm6` and so on. run-window adds them
   in a small test image (`IMAGE-window`: FROM the naive image + one apt layer, built once - the naive image itself
   is not changed) from `WINDOW_APT` at the top of the file. Tested here with opencv-python 5.0 on Ubuntu 24.04: the
   window plugin lacked `libSM.so.6` and `libICE.so.6` (packages `libsm6 libice6`, the default), and with them the
   window opened on an X display. `opencv-python-headless` has no window code at all: no screen helps there.

What was tested here: the window on an X display (Xvfb) from a container, NO_WINDOWS in run-naive and in compose
(on: the program runs to its end; off: it stops, no screen). Not tested here: WSLg itself (no Windows machine) -
the Docker Desktop paths above are the ones its users use for WSLg (not tested by me); if no window opens, send what run-window prints.


## 17. Feed the program any video, folder of videos or camera - SOURCE (and files named by a PC path)

### 17a. SOURCE - what the program reads, whatever its code opens

The code decides what a program opens: a camera address from a config, a video path in a list, a camera number,
`if TEST: video else: rtsp`. To give it something else you would normally edit the code. Every run file has a
setting at its top instead:

```bat
REM SOURCE: what the program reads INSTEAD of what its code opens ...
set "SOURCE="
```

| What you write | What the program gets |
|---|---|
| `set "SOURCE="` (empty, the default) | what its code says - nothing is changed |
| `set "SOURCE=D:\videos\lane 3.mp4"` | this video, for EVERY source it opens (one camera or ten) |
| `set "SOURCE=D:\videos"` | a folder: source 1 gets the 1st video in it (by name), source 2 the 2nd, ... (more sources than videos: it starts again from the 1st). A source keeps its video when the program reconnects |
| `set "SOURCE=rtsp://user:password@192.168.1.20:554/stream1"` | this camera stream, for every source (`rtsp`, `rtmp`, `http`, `udp:` addresses) |

A relative path works (`set "SOURCE=videos\test.mp4"`, from the folder you run the file in). A path that does not
exist stops the run file at once: `SOURCE "D:\videos\x.mp4": no such file or folder on this PC`. A `%` in an address
(a password with `%`) must be written `%%` in a .bat file. The .sh files: `SOURCE="..."`, the same values.

**How, without a code change.** The run file mounts the file or folder (`-v "D:\videos:/xsource:ro"`) and passes
`-e XRAY_SOURCE=/xsource`. The tools' folder `xray_inputs` is on `PYTHONPATH` in every run file; its
`sitecustomize.py` runs when Python starts - in every process the program starts, too - and gives
`cv2.VideoCapture(...)` (and `.open(...)`) the SOURCE instead of what the code passed: a camera number, an rtsp
address, a file path. Each swap is printed once per process, at the start of the program's output:

```text
[xray] video source: C:\Users\me\Videos\Lane-12 down side.mp4 -> /xsource/a lane 1.mp4
```

No `[xray] video source` line = the program did not open a source through `cv2.VideoCapture` (another library -
PyAV, GStreamer, imageio - or it never got that far): read the program's own output above.

It is in every run file: run-naive, run-window, run-step2, run-step3-4, run-slim, run-load, run-explore. In X-Ray's
runs the needs report shows the swapped video as `the test video the run file gave in place of the code's own
source (its SOURCE setting)`, so it is not mistaken for a file the image needs.

**SOURCE and VIDEOS.** VIDEOS (X-Ray's runs only) plays a folder of videos ONE AFTER THE OTHER for a program that
reads a video FILE, and leaves cameras alone. SOURCE replaces every source - cameras and streams too - and gives each
source its own video. To test a multi-camera program with recorded videos: SOURCE = a folder with one video per
camera. To loop a single video file forever: SOURCE = the file, LOOP=1.

Tested here: the toll-style program from section 17b (a spawned process, `VideoCapture(path, cv2.CAP_FFMPEG)`, its
config naming a video of another PC): with SOURCE empty `Cam ... Not Connected`; with SOURCE = a file, a folder, or
an rtsp stream (a test RTSP server) `Cam connected`, in run-naive and in the traced run-step2. Three sources (two
rtsp addresses and camera 0, in spawned processes) with SOURCE = a folder of two videos: video 1, video 2, video 1,
and each source got the same video again when it reconnected. Not tested here: Windows CMD itself (no Windows
machine) - if a .bat line fails, send what it printed.

### 17b. Files the code names by their PC path (a video in a config list, a model on D:)

A program written on Windows often names a file by where it is on that PC - in a config list, a dict, a constant:
```python
CAMERA_CONFIG = [{'name': 'Lane_3', 'rtsp_url': r"C:\Users\me\Videos\Lane-12 down side.mp4", ...}]
...
cap = cv2.VideoCapture(cam_config['rtsp_url'], cv2.CAP_FFMPEG)
```
A Linux container has no `C:\`. OpenCV prints `can't be used to capture by name` and the program waits for its
"camera" forever. No env var to set, no `-v` that helps: the code asks for `C:\Users\...`, a name no container has.

**What X-Ray does, without a code change:**
1. Step 1 finds every path of a PC written in the code (spaces allowed, any depth: a list, a dict, a constant) and
   lists it in the needs report: `pcfile  C:/Users/me/Videos/Lane-12 down side.mp4 (constants.py:5) - a video of one
   PC, named in the code`. Near an unresolved camera it adds: the video file(s) named in the code may be what it opens.
2. xray_runfile.py mounts each such file that is on THIS PC into every run: `-v "C:\...\Lane-12 down side.mp4:/xin/pc_1/
   Lane-12 down side.mp4:ro"`, and writes `PC_PATHS=C:\...\Lane-12 down side.mp4=/xin/pc_1/Lane-12 down side.mp4` into
   `run.env`.
3. The same `xray_inputs` folder as SOURCE (on `PYTHONPATH`: `-v "%X%\xray_inputs:/opt/xray-inputs:ro"`) gives
   `cv2.VideoCapture`, `cv2.imread` / `imwrite`, `open()`, `os.path.exists` / `isfile` / `isdir` / `getsize` and
   `onnxruntime.InferenceSession` the container path whenever the code names a path in `PC_PATHS` (upper / lower
   case and `/` against `\` do not matter, as on Windows). Every other path is passed on as it is.

It works in X-Ray's runs (together with X-Ray's own trace hook) and in run-naive / run-window. Tested here with the
same pattern as a real multi-camera worker: `VideoCapture(path, cv2.CAP_FFMPEG)` in a spawned child process opened
the mounted video; without it, nothing.

**The file is not on this PC** (the config names another developer's PC): xray_runfile prints `CHECK the code names
C:\... - that file is not on this PC`. Either copy the video to exactly that path and run xray_runfile.py again, or -
quicker - set SOURCE (17a) to any video of this PC. SOURCE wins when both are set: it replaces the source before the
path is looked up.

**Production is different:** a server has neither your PC's file nor X-Ray's run files. There the config holds what
the server has - a camera address (`rtsp://...`) or a path on the server - and nothing needs mapping. The plan's
`.env.example` names the PC files it found and says how to test compose with one, if you want to.

The slim image never contains such a test file or SOURCE video: they are mounted at run time only.


## 18. Edit the run files and Dockerfile.naive yourself

The files X-Ray writes are plain text: open them in Notepad (right click > Edit, or `notepad run-naive.bat`). Every
line is a command you could type yourself. This section shows what each part does, so you can change it.

### 18.1 Before you edit: which file, and what survives

| You want to change | Change it in | Kept when xray_runfile.py runs again? |
|---|---|---|
| how long it runs, loop, videos, SOURCE | the settings at the top of that run file | no - set it again |
| an env var the program reads (for every run) | `runs\NAME\run.env` | no - written again too |
| a `-v` / `-e` / `-p` for one run | the `docker run` line of that run file | no |
| what is INSTALLED in the image | `Dockerfile.naive` in the project, then build-naive again | yes - xray_runfile.py never writes it (xray_naive.py does, when you run it again) |

xray_runfile.py writes ALL its files again each time it runs (run.env included). To keep an edited run file, save it
under your own name: `copy run-naive.bat my-run-naive.bat` - X-Ray never touches a file it did not write. Edit the
copy, run the copy.

### 18.2 The settings at the top of a run file

```bat
set "RUN_SECONDS=180"
set "LOOP=0"
set "VIDEOS="
set "SOURCE="
```
- Write the value between `=` and the closing `"`: `set "SOURCE=D:\videos"`. Keep both quotes - they let a path
  have spaces. No spaces around `=` (`set "SOURCE = D:\x"` makes a variable called `SOURCE `, with a space).
- `REM` at the start of a line = a comment, CMD skips it. In .sh files the comment sign is `#` and a setting is
  `SOURCE="D:/videos"` (no `set`, no spaces around `=`).
- `%NAME%` later in the file = the value of that setting. `%%` = one `%` character.
- What each setting does is written in the `REM` lines above it.

### 18.3 The docker run line, part by part

A run file is mostly one `docker run` command. CMD reads a line ending in `^` and the next line as ONE line (in .sh
files: `\`). A typical one (run-naive of a GPU project):

```bat
docker run --rm --name proj-run --gpus all --shm-size 2g ^
  -v "D:\models\best.onnx:/data/best.onnx:ro" ^
  --env-file "%RUN%\run.env" ^
  -v "%X%\xray_no_windows:/opt/xray-no-windows:ro" -v "%X%\xray_inputs:/opt/xray-inputs:ro" -e PYTHONPATH=/opt/xray-no-windows:/opt/xray-inputs -e NO_WINDOWS=1 ^
  %XSRC% ^
  proj-naive
```

| Part | What it does | When to change it |
|---|---|---|
| `docker run` | start a NEW container from an image | - |
| `--rm` | delete the container when it ends (its files are gone; the image stays) | remove it to look at a stopped container (`docker ps -a`, `docker logs NAME`) |
| `--name proj-run` | a fixed name: `docker stop proj-run`, `docker exec -it proj-run bash` | - |
| `--gpus all` | give the container the GPU (the driver comes from the PC) | remove it to test the CPU fallback |
| `--shm-size 2g` | shared memory for programs that pass frames between processes (`multiprocessing`, PyTorch DataLoader); Docker's default is 64 MB | raise it on `Bus error` / `No space left on device` in /dev/shm |
| `--mac-address ...` | the network card address a licence check expects | when the licence is for another PC |
| `-p 8000:8000` | PC port : container port - a web API the program serves | when the program listens on a port |
| `-v "PC_PATH:CONTAINER_PATH:ro"` | show a PC file or folder inside the container. `:ro` = read only; leave it out to let the program write there | add one for every file the program needs that is not in the image |
| `--env-file "...\run.env"` | every `NAME=value` line of run.env becomes an env var | change values in run.env |
| `-e NAME=value` | one env var; written AFTER `--env-file` it wins over run.env | a quick change for one run |
| `PYTHONPATH=...` + the two `-v ...\xray_...` | X-Ray's two helper folders: NO_WINDOWS (16) and SOURCE / PC_PATHS (17) | leave them - empty settings change nothing |
| `%XSRC%` | the `-v` / `-e` the SOURCE setting made (empty when SOURCE is empty) | change SOURCE instead |
| `proj-naive` | the IMAGE. Anything AFTER the image name replaces the image's CMD | add `python other_entry.py` after it to start another file |

**Add a mount or an env var:** put a new line before the image line, ending in ` ^`:
```bat
  -v "D:\data\lanes.json:/app/config/lanes.json:ro" ^
  -e LOG_LEVEL=DEBUG ^
  proj-naive
```
The container path must be what the code opens (a relative path in the code is relative to `/app`, the WORKDIR of
the Dockerfile). Paths with spaces: keep the whole `PC:container:ro` inside one pair of quotes.

**Common mistakes:** a space after `^` (CMD then ends the command there: the next line runs as its own command -
`'-v' is not recognized`); a line without `^` before the image line (same); a Windows path on the container side
(`-v "D:\x:D:\x"` - the container side is always Linux: `/something`).

In run-step2 (and the other X-Ray runs) the image name is followed by `python /xray/xray_trace.py run ... -- python
main.py`: X-Ray starts the program itself to watch it. The part after `--` is the program's own command.

### 18.4 Dockerfile.naive, part by part

xray_naive.py writes it into `runs\NAME\naive\`; you copy it into the project and build it with build-naive.bat
(`docker build -f Dockerfile.naive -t NAME-naive .`). Every `RUN`, `COPY`, `ENV` line makes a layer; `#` lines are
comments. Its sections:

| Section | Lines | Change it when |
|---|---|---|
| 1. BASE IMAGE | `FROM nvidia/cuda:...-cudnn-runtime-ubuntu24.04` or `FROM python:3.12-slim` | another Python version (`python:3.10-slim`), CPU only (`python:X-slim` instead of nvidia/cuda) - or run xray_naive.py again with `--python 3.10` / `--cpu` |
| `ENV` / `ARG` | `PYTHONUNBUFFERED=1` (prints reach the log at once), `DEBIAN_FRONTEND` (apt asks nothing) | rarely |
| 2. SYSTEM LIBRARIES | `RUN apt-get update && apt-get install -y --no-install-recommends ... && rm -rf /var/lib/apt/lists/*` | `xray_look.py --image` or an error names a missing library: add its package to that list |
| 3a / 3b / 3c. PACKAGES | `RUN pip install --no-cache-dir 'name==version' ...` - 3a the big frameworks (own layer), 3b the rest, 3c (only if present) pins pip will not combine, installed after with `--no-deps` | change a version, add a package the program imports but the requirements forgot |
| 4. LD_LIBRARY_PATH | where the CUDA wheels put their libraries | only when xray_naive.py wrote one and a package moved them |
| 5. THE CODE | `COPY . .` (everything in the project, minus `Dockerfile.naive.dockerignore`) | add a folder to the .dockerignore to keep it out |
| `CMD` | `CMD ["python", "main.py"]` = what the container starts | another entry file |

**Add a system library** - e.g. `xray_look.py --image` says `libsm6`: add it to the apt line, with a space, before the
`\` (the `\` at a line end joins the next line, like `^` in CMD):
```dockerfile
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-venv libgl1 libglib2.0-0t64 libx11-6 libxcb1 libsm6 \
 && rm -rf /var/lib/apt/lists/*
```
Find the package of a library you only know by its file name: `apt-file search libSM.so.6` (inside a container of the
same base: `apt-get update && apt-get install -y apt-file && apt-file update` first).

**Change a version** - one line in the pip list. Each package is in single quotes (`'numpy==1.26.4'`): keep them -
the shell that runs a RUN line would otherwise read `<` / `>` in `numpy<2` as a redirect. Also change it in your requirements file,
or the next xray_naive.py run brings back the old one.

**Rebuild:** run build-naive.bat again. Docker reuses every layer ABOVE the first changed line, so a change in 3b does
not download 3a (torch, onnxruntime) again. Then run run-naive.bat to see it start.

**Test a change before you write it down:** `docker run --rm -it --entrypoint bash NAME-naive` opens a shell in the
image (as root): `apt-get update && apt-get install -y libsm6`, `pip install 'numpy==1.26.4'`, then `python main.py`
there. What worked goes into Dockerfile.naive; the shell's changes are gone when you exit. The `CHECK BY HAND` lines
at the end of Dockerfile.naive show how each section can be checked in the built image.
