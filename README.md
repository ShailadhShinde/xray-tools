# X-Ray: plan a small, correct Docker image for a Python project

**Any project, step by step: open [XRAY_WALKTHROUGH.html](XRAY_WALKTHROUGH.html) in a browser** - what to run, what each
report line means, what you decide; your folders fill the commands. The short version, with a map of the steps and
where the space is saved, as a Word file: [X-Ray_How_It_Works.docx](X-Ray_How_It_Works.docx).

Worked examples with real projects' results: [ZIP_WALKTHROUGH.html](ZIP_WALKTHROUGH.html) (a video program) and
[NORWAY_RUNBOOK.html](NORWAY_RUNBOOK.html) (a multi-camera GPU worker: licence, database, load tests, compose).

**A .NET app (a dashboard, a web API), step by step: open [DOTNET_WALKTHROUGH.html](DOTNET_WALKTHROUGH.html)** - from
the published folder to a running container: `dotnet_check.py`, a form that writes the Dockerfile, `.dockerignore`,
`compose.yml` and `.env` from what it finds, what to check after every command, new versions and the way back.

**The dashboard and your ML programs as one system: open [SYSTEM_WALKTHROUGH.html](SYSTEM_WALKTHROUGH.html)** - started
one by one with `docker run` (the naive images) or with their compose files: one network, every program called by its
name, the commands in the right order with a check after each, the logs - then uploads, HTTPS and keys, one at a time.

**How one program's address reaches another: [CONNECTIONS_GUIDE.html](CONNECTIONS_GUIDE.html)** (CONNECTIONS_GUIDE.md) -
`os.getenv("NAME", default)` in the code, what X-Ray and run.env do with it, the Flask "Running on" lines and which
address matters, the port and the route to write down, the docker run line piece by piece, env-file rules (no quotes),
ports inside the network and from the PC, and what to check once the network exists.

**The database has the new rows, the dashboard shows nothing: [EMPTY_DASHBOARD.html](EMPTY_DASHBOARD.html)**
(EMPTY_DASHBOARD.md) - the checks in order: the same database, an old row next to a new one, the containers' clock
(UTC: `TZ`), picture paths, calls to the dashboard, the dashboard's own query (MySQL's general log), and the dashboard
in Docker next to IIS.

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
| combine | `xray_combine.py` | your machine | several programs planned one by one, in ONE image: the shared packages once, each program's code from its verified slim image into /app/NAME, a verify run and a compose file per program (COMBINE_GUIDE) |
| system | `xray_system.py` | your machine | several projects that call each other (an API that calls another API ...): from their Step 1 maps, who calls whom (matched by the port), the start order, what must change first (a server on 127.0.0.1, an address or a PC folder written in the code), and a compose.yml that runs them together - by name, no IPs (SERVICES_GUIDE section 9) |

See results in the browser: `python3 xray_view.py app.json` (writes and opens `app.view.html` with the file built in).

What X-Ray reads from your files (most of it) and what it knows from lists - and which list to extend for a library it does not know yet: GUIDE.md section 15.

**The rest of the system** - a .NET dashboard / web API, a PostgreSQL or MySQL database and your programs as ONE system, started together and connected (Dockerfiles for .NET, the database images, compose, the network, moving your data in, projects that call each other, the .NET app in IIS on the PC itself): **[SERVICES_GUIDE.html](SERVICES_GUIDE.html)** (the same text: SERVICES_GUIDE.md), with a working example in `example-stack/`.

**A .NET app next to your ML images** - what is the same and what differs: the Dockerfiles line by line, requirements, compilers, secrets, `.dockerignore`, slow internet, and ports - who can reach what (a program listening on 127.0.0.1 or 0.0.0.0, `ports:`, service names, client PCs): **[DOTNET_VS_ML.html](DOTNET_VS_ML.html)** (the same text: DOTNET_VS_ML.md).

**Several programs in ONE image** (they share most packages: stored once, each program in its own folder and container): **[COMBINE_GUIDE.html](COMBINE_GUIDE.html)** (COMBINE_GUIDE.md), with `xray_combine.py`.

**Many clients on one GPU server** - a copy of a solution per client, separate networks and data, one proxy with an API key or a password per client, sharing the GPU between the clients' models: **[MULTI_CLIENT_HOSTING.html](MULTI_CLIENT_HOSTING.html)** (MULTI_CLIENT_HOSTING.md), templates in `multi-client/`.

**A program that saves videos** (ffmpeg; OpenCV cannot write H.264 in a container): GUIDE.md section 19. **Paths written with `\`** (a folder named `out\vehicle\bus` in the container) and PC folders the program saves into: GUIDE.md section 17c. **Slow or unstable internet** (builds that time out): GUIDE.md section 20. Before you put a .NET app in an image: `python dotnet_check.py "C:\path\to\app-folder" > dotnet-check.txt` (the published folder or the project folder) - its .NET version and base image, port, HTTPS, the database (wherever the connection string is: appsettings, web.config, the code), Windows-only parts and native libraries, and what goes into the image (with the .dockerignore lines), each with the fix.

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
