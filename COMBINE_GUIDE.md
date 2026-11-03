# One image for several programs - xray_combine.py

When several programs share most of their Python packages, one image can hold all of them. The shared packages are
stored once, every program keeps its own folder, and each still runs in its own container. `xray_combine.py` builds
that image from what X-Ray already made for each program: its plan (`plan.json`) and its verified slim image. It writes
a verify run per program, and a compose file per program that runs it from the combined image.

*Tested* with three small programs (two Flask APIs and a worker, numpy + OpenCV + Pillow + requests), each taken
through X-Ray to `VERDICT: PASS` first. Their three slim images took 1,004 MB on disk together; the combined image
568 MB, 43% less. All three programs passed their verify run in the combined image, and all three started from their
`compose.combined.yml`. Not tested here: a GPU program (the test machine has no GPU).

---

## 0. Combine, or keep separate images?

| | One combined image | Separate slim images |
|---|---|---|
| Disk | each shared package once | each image has its own copy of the shared packages - only the base image (python:3.11-slim ...) is stored once anyway |
| Download to a new machine | one image | three |
| A new release of ONE program | the image is rebuilt: every program restarts and is verified again | only that program |
| Package versions | one version of each package for all programs | each program its own |
| Good when | the programs ship together, run on the same Python, share most packages | they update at different times, or need different versions of a package |

Docker already shares the base image between separate images. What the combined image saves is the packages that two
or three programs install: the more they share (numpy, OpenCV, torch, onnxruntime ...), the bigger the saving.

---

## 1. Before you start

1. **Every program done with X-Ray up to `run-slim`**, and its `runs\NAME\verify.txt` ends with `VERDICT: PASS`.
   The tool reads `runs\NAME\plan\plan.json` and `runs\NAME\pkg\pkgscan.json`, and copies the code out of the image
   `NAME-slim`.
2. **The same Python for every program.** The first lines of each `runs\NAME\plan\Dockerfile` show it
   (`FROM python:3.11-slim`). One image has one Python - section 8 says what to do when they differ.
3. **GPU programs: the same `GPUBASE`** (the top of `run-step3-4.bat`) for all of them - otherwise CUDA would be in
   the image twice.
4. **The slim images are there:**
   ```bat
   docker images
   ```
   lists `anpr-slim`, `middle-slim`, `toll-slim` (your names). Missing one: run that program's `build-slim.bat`.

---

## 2. Run the tool

```bat
cd /d "C:\...\xray-tools"
python xray_combine.py --run runs\anpr --run runs\middle --run runs\toll --image mep-combined --out combined
notepad combined\combine.txt
```

| You give | What it is |
|---|---|
| `--run runs\NAME` | one per program: its X-Ray folder (2 or more) |
| `--image mep-combined` | the name the combined image gets |
| `--out combined` | the folder for the Dockerfile and the report |
| `--compose NAME=D:\...\compose.yml` | only if a program's compose file is NOT `runs\NAME\plan\compose.yml` |
| `--base IMAGE` | only when the plans disagree on the base (section 8) |

It writes:

| File | What it is |
|---|---|
| `combined\Dockerfile` | the combined image |
| `combined\combine.txt` | what is shared, what changed, what could not be combined, the sizes, the next steps |
| `runs\NAME\run-combined.bat` | the verify run of that program in the combined image - a copy of its run-slim.bat |
| `compose.combined.yml` | next to each program's compose file: the same service, switched to the combined image |

Nothing in your project folders changes. The combined image is made from what the plans and slim images already
have, so run the tool again after every new plan.

---

## 3. Read combine.txt

From the test (shortened):

```text
PYTHON PACKAGES THE PLANS INSTALL (pip)   - = that program does not install it
  package                         anpr          middle        toll          in the image
  flask                           3.0.3         3.0.3         -             3.0.3
  numpy                           1.26.4        2.0.2         1.26.4        2.0.2   <- changed
  opencv-contrib-python-headless  -             -             4.10.0.84     4.10.0.84
  opencv-python-headless          4.10.0.84     -             -             -  (see CHANGED)   <- changed
  pillow                          10.4.0        10.4.0        -             10.4.0
  requests                        -             2.32.3        2.32.3        2.32.3
  4 of 6 are installed by two or more programs: once in the combined image

SIZE OF THE PYTHON PACKAGES (from the package scans, unpacked on disk)
  3 separate images: 597 MB  (254 + 107 + 236)
  one combined image:    262 MB   -> 335 MB less

CHANGED - a program gets another package or version than its slim image had (verify proves it still works)
  anpr         opencv-python-headless==4.10.0.84  ->  opencv-contrib-python-headless==4.10.0.84
  anpr         numpy==1.26.4  ->  numpy==2.0.2   (one version for every program)
  toll         numpy==1.26.4  ->  numpy==2.0.2   (one version for every program)
```

| Section | What it tells you | What you do |
|---|---|---|
| PYTHON PACKAGES | each program's packages side by side, and the one version in the image | nothing - read it |
| SIZE | the packages of the separate images against the combined one (the measure after the build: section 7) | nothing |
| CHANGED | a program gets another version, or another package that installs the same folder (one `cv2` per image) | nothing now: its verify run (section 5) proves it still works |
| CHECK | two programs need versions that cannot be installed together - the build will stop | section 8 |
| HAND EDITS | lines you added to a program's `plan\Dockerfile` by hand: they are not in plan.json, so not in the combined image | copy the ones that matter into `combined\Dockerfile` |
| ... names paths of the slim image | a value in `run.env` or `.env` points into `/app/...` | nothing: the verify run and the compose file give the program the new place |

How one version is chosen: the version every program's packages accept (their own requirements, read from the package
scans); among those, a version a plan pins on purpose, then the newest. Packages that install the same folder keep the
one that can do what the others do: `opencv-contrib-python` over `opencv-python`, the window build over `-headless`
only when a program needs windows, `onnxruntime-gpu` over `onnxruntime` (it runs on the CPU too), `psycopg2-binary`
over `psycopg2`, the CUDA build of torch over `+cpu`.

---

## 4. Build

```bat
cd /d "C:\...\xray-tools\combined"
docker build -t mep-combined -f Dockerfile .
```

The folder holds only the Dockerfile: the code comes from the slim images (`COPY --from=anpr-slim /app/ /app/anpr/`),
the packages from pip. Inside the image:

```text
/app/anpr/      the anpr code, exactly as its slim image had it
/app/middle/
/app/toll/
```

---

## 5. Verify every program in the combined image

```bat
runs\anpr\run-combined.bat
runs\middle\run-combined.bat
runs\toll\run-combined.bat
```

Each is that program's `run-slim.bat` with four changes: the combined image, `-w /app/NAME` (the folder it runs in),
X-Ray's `--root /app/NAME`, and its own output names (`trace-combined`, `step2-combined.log`, `verify-combined.txt`).
Every `runs\NAME\verify-combined.txt` must end with:

```text
VERDICT: PASS - the planned image did everything the scenario needs
```

`CHECK` with problems: read them (a missing import, a file not found), and send `verify-combined.txt` to Claude. A
program that only works on its own versions stays in its own image - combine the others.

---

## 6. Start them from the combined image

Each program's compose file got a twin, `compose.combined.yml`, in the same folder - so its `.env`, `env_file` and
`./folders` stay where they are. What changed in it (from the test):

```text
    image: anpr-slim                                    ->    image: mep-combined
    build:                                                    (removed: nothing to build per program now)
      context: ...                                            working_dir: /app/anpr
      dockerfile: ...\runs\anpr\plan\Dockerfile               command: ["python", "main.py"]
      - ${OUTPUT_DIR:-./output}:/app/output             ->    - ${OUTPUT_DIR:-./output}:/app/anpr/output
```

Start each one in its folder:

```bat
cd /d "C:\...\xray-tools\runs\anpr\plan"
docker compose -f compose.combined.yml up -d
```

The compose project name stays (`name: anpr`), so `up -d` replaces the slim container with one from the combined
image. Networks, ports, the env file: as before. Your own compose file (not X-Ray's): give it with
`--compose NAME=PATH`. The tool switches every service that uses `NAME-slim` or builds the plan's Dockerfile. Look at
the new file once: `docker compose -f compose.combined.yml config` prints it as compose reads it.

The programs talk to each other exactly as before (CONNECTIONS_GUIDE): the image changed, not the network.

---

## 7. The sizes, measured

```bat
docker system df -v
```

| Column | Meaning |
|---|---|
| SIZE | everything the image needs |
| SHARED SIZE | layers other images use too (the base image) - on disk once |
| UNIQUE SIZE | what only this image has |

The disk the separate images take = SHARED once + each one's UNIQUE. In the test: 210 + 327 + 145 + 323 = 1,004 MB
for the three slim images; the combined image 568 MB. Once every program runs from the combined image, the slim images
can go (`docker image rm anpr-slim middle-slim toll-slim`) - `build-slim.bat` makes them again when you need them.

---

## 8. When the tool stops, or a step fails

| Message | Meaning | Do this |
|---|---|---|
| `cannot combine: the programs run on different Pythons` | e.g. anpr on 3.12 (an nvidia/cuda base), middle on 3.11 | make them the same: run xray_naive.py for one of them with `--python 3.12`, then its steps again. Or `--base IMAGE` and verify every program on it |
| `... in one image CUDA would be there twice` | one GPU program gets CUDA from the nvidia/cuda base, another from pip wheels | the same `GPUBASE` at the top of each `run-step3-4.bat`, then run-step3-4, build-slim, run-slim, combine again |
| `the GPU programs use different nvidia/cuda images` | two CUDA versions | `--base` the newer one (`nvidia-smi` shows the driver supports it), verify every program on it |
| `plan.json is missing` | that program has no plan yet | its `run-step3-4.bat` |
| `NOT FOUND - run build-slim` | no slim image to copy the code from | its `build-slim.bat` |
| CHECK `no version every program's packages accept` | e.g. anpr's package wants numpy below 2, middle's package needs 2 or newer | one program on a version the other's packages accept: change the pin, its X-Ray steps again - or that program keeps its own image |
| the build stops with `ResolutionImpossible` | pip found what the CHECK warned about | as above |
| `no compose file at ...` | its compose file is elsewhere | `--compose NAME=PATH` |
| `PermissionError` writing a mounted folder (Linux server) | Docker created the host folder as root; the programs run as the user app (uid 1000) | `sudo chown -R 1000:1000 FOLDER` once - the same as for the slim images |

---

## 9. A new release of one program

1. That program's X-Ray steps (step 1, build-naive, run-step2, run-step3-4, build-slim, run-slim -> PASS).
2. `xray_combine.py` again (the same command), then build (section 4).
3. Verify every program (section 5), not only the new one: a shared package may have moved.
4. `docker compose -f compose.combined.yml up -d` for each: all programs get the new image.

---

## 10. How it works - for anyone who changes the tool

- **Inputs, per program** (`runs\NAME`): `plan\plan.json` (base, apt and pip packages, writable folders, volumes,
  the command, ENV), `pkg\pkgscan.json` (every installed package with its own requirements and size), `run-slim.bat`,
  the compose file. Nothing is read from the project folders.
- **Base:** the plans' base when they agree; one Python for all; a GPU program's base when the others are CPU programs
  on the same Python; it stops when CUDA would come from two places.
- **Packages:** apt = all of them. pip = one version per package: from the package scans, the version every program's
  packages accept (pinned on purpose first, then the newest); packages that install the same folder are reduced to one
  (the list above). A dependency whose version moved is pinned too.
- **Code:** `COPY --from=NAME-slim /app/ /app/NAME/` - the files the verify run of the slim image proved, nothing
  re-read from the project. Writable folders are made per program (`/app/NAME/logs`), volumes as the plans have them.
- **No program-specific ENV in the image:** `PYTHONPATH=/app/src` of two programs would let one import the other's
  modules (both have a `constants.py`). It goes per program: into `run-combined.bat` (`-e`) and
  `compose.combined.yml` (`environment:`), with `/app` replaced by `/app/NAME`; the same for env-file values that
  point into `/app`.
- **The Dockerfile** is written by X-Ray's own plan renderer (`xray_plan.dockerfile`), so it has the same parts as the
  slim ones: slow-internet settings, the CUDA wheel registration, the non-root user, CHECK BY HAND.
- **Limits:** the compose twin is made by editing the text of X-Ray-style compose files (2-space indents, a `services:`
  block) - check it with `docker compose config`. Lines added by hand to a slim Dockerfile are reported, not copied.
