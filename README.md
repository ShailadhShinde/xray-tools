# X-Ray: plan a small, correct Docker image for a Python project

**Any project, step by step: open [XRAY_WALKTHROUGH.html](XRAY_WALKTHROUGH.html) in a browser** - what to run, what each
report line means, what you decide; your folders fill the commands.

Worked examples with real projects' results: [ZIP_WALKTHROUGH.html](ZIP_WALKTHROUGH.html) (a video program) and
[NORWAY_RUNBOOK.html](NORWAY_RUNBOOK.html) (a multi-camera GPU worker: licence, database, load tests, compose).

**First time? Read [GUIDE.md](GUIDE.md)** - every step, what to look for, what to do when something fails.

**Reference: [GPU_DOCKER_HANDBOOK.html](GPU_DOCKER_HANDBOOK.html)** - GPU base images and what they contain, CUDA libraries,
which CUDA each framework needs, when pip compiles and what it needs (gcc, -dev libraries), runtime system libraries,
sizes, errors and fixes. Open it in any browser (works offline; download it from GitHub with the "Download raw file" button).

Python standard library only (Python 3.8+). Nothing to install.

| Step | Tool | Runs where | What it does |
|---|---|---|---|
| 0 | `xray_naive.py` | your machine (needs internet) | before the first build: checks every requirement for Linux (prebuilt / compiled / missing / Windows-only), checks the whole file together the way the build installs it (lines pip will not install together go into a second `--no-deps` step, exactly as pinned - nothing left out; two packages that install the same files are found and one is kept), and writes a naive Dockerfile that builds. The Python version comes from the project itself (its venv, `.python-version`, `pyproject.toml` ...; `--python` to choose), the CPU from `--arch` (x86_64 / aarch64); for a pin with no Linux wheel it lists the Python versions that have one. With `--static`: every env var with its value in your code and its path in the container, the code changes needed for Windows paths, files from your PC copied into the image or mounted (`--copy` / `--mount`) |
| 1 | `xray_static.py` + `xray_needs.py` | your machine | reads the code (runs nothing): packages, files, models, env vars, network, GPU, cameras; the env var that switches the windows off in a container; packages imported and never used (OPTIONAL CODE CHANGES: a smaller image) |
| 2 | `xray_runfile.py` writes the run files (+ `run.env`: every env var in one file); they run `xray_trace.py run` / `facts` / `merge` | inside the app's container / your machine | runs the app and records what it really did |
|   | `run-naive.bat`: the naive image on its own, no X-Ray - does it start and do its work? OpenCV windows are switched off with `NO_WINDOWS=1` (`xray_no_windows/`, no code change) | | |
|   | `run-window.bat`: the same with the program's windows on your screen (Windows 11: WSLg) - GUIDE section 16 | | |
|   | a file of your PC named in the code (`C:\\...\\video.mp4` in a config): every run mounts it, and `xray_inputs/` gives the code the container path (`PC_PATHS`, no code change) - GUIDE section 17b | | |
|   | top of every run file: `RUN_SECONDS` (how long, then Ctrl+C), `LOOP=1` (a video file starts again), `VIDEOS` (a folder of videos played one after the other) - GUIDE section 4; `SOURCE` = what the program reads instead of what its code opens: a video file, a folder (each camera its own video) or an `rtsp://` camera, no code change (`xray_inputs/`) - GUIDE section 17a | | |
|   | edit a run file or Dockerfile.naive yourself: what each line does, what survives a new xray_runfile.py run - GUIDE section 18 | | |
|   | `run-explore.bat`: same naive image, no time limit, video files loop; look inside, stop with `docker stop -t 60 NAME-explore` (GUIDE section 12) | | compare your own look with the report |
| look | `xray_look.py` | inside any container | what runs, the files open now, the libraries loaded and the apt / pip package or driver each comes from, network, RAM, GPU; `--watch 5` = what changes; `--image` = missing system libraries, CUDA libraries, packages that installed the same files, does every package import, GPU test; a `by hand:` line under every section (GUIDE section 10) |
| compare | `compare-images.bat` (xray_look `--inventory` + `--compare`) | containers + your machine | two images side by side: sizes, the apt / pip packages only one has (used in the run or not), where each gets its CUDA libraries (GUIDE section 6.2) |
| videos | `videos.bat [FOLDER]` (`xray_videos.py check` / `convert`, `xray_ffmpeg.Dockerfile`) | containers | can the image read your videos? each is opened with the image's own OpenCV; the ones it cannot read (AV1 ...) are converted to H.264 into FOLDER\h264, on NVIDIA's GPU encoder when Docker gives the GPU, else on the CPU (GUIDE section 4) |
| 3 | `xray_pkg.py baseline` / `scan` / `facts` | inside containers / your machine | what is installed in the image and what is really needed |
| 4 | `xray_plan.py plan` / `verify` | your machine | writes a slim Dockerfile, .dockerignore and compose override (or compose.yml + .env.example); checks the new image behaves the same |
| 5 | `xray_compare.py` | your machine | several projects: one shared base image |

See results in the browser: `python3 xray_view.py app.json` (writes and opens `app.view.html` with the file built in).

What X-Ray reads from your files (most of it) and what it knows from lists - and which list to extend for a library it does not know yet: GUIDE.md section 15.

## Step 1 (safe: reads files only)
```
python3 xray_static.py --root PROJECT_FOLDER --entry main.py [--entry other_entry.py] [--requirements requirements.txt] --out app.json
python3 xray_needs.py app.json
```
On Windows use `python` instead of `python3`.

## Keep a log of every command
Add `2>&1 | tee step1.log` to the end of a command: the screen output (including errors) is also saved in
`step1.log`, which you can share.

## Windows
Everything works from CMD (no PowerShell needed). The run files are `.bat` files; open results in Notepad.
