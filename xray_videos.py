#!/usr/bin/env python3
"""
xray_videos.py - can the IMAGE read your videos? And if not, make copies it can read.

  check  (runs INSIDE the app's image): every video in a folder is opened with the image's own OpenCV - the same
         library your program uses - and a few frames are read. OK / CANNOT READ, with the codec from the file
         header. The ones it cannot read are listed in unreadable.txt.
  convert (runs in the ffmpeg image): those files are converted to H.264 (the format every OpenCV build reads)
         into a subfolder; the originals stay as they are. On NVIDIA's GPU encoder (h264_nvenc) when the container
         has the GPU - much faster for big videos - else on the CPU (libx264); a file the GPU encoder refuses is done
         on the CPU.

Why: OpenCV's Linux wheel (pip install opencv-python) reads almost every video format, but not all - AV1 opens and then
gives no frame (no software AV1 decoder inside, and a container has no hardware video decoding). A check with the real
library finds such files whatever their format, instead of a list of known bad formats.

You do not type these: xray_runfile.py writes runs\\NAME\\videos.bat, which runs both.
  docker run --rm --entrypoint "" -v "TOOLS:/xray:ro" -v "FOLDER:/v:ro" -v "OUT:/out" IMAGE python3 /xray/xray_videos.py check /v --out /out
  docker run --rm --gpus all -e NVIDIA_DRIVER_CAPABILITIES=compute,video,utility -v "TOOLS:/xray:ro" -v "FOLDER:/v" -v "OUT:/out" xray-ffmpeg:2 python /xray/xray_videos.py convert /v --list /out/unreadable.txt
Standard library only (plus the image's own cv2 for check, the ffmpeg image's ffmpeg for convert).
"""
import argparse
import os
import subprocess
import sys
import time

VERSION = "0.2.0"
HERE = os.path.dirname(os.path.abspath(__file__))
EXT = (".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi", ".mpg", ".mpeg", ".ts", ".m2ts", ".mts", ".wmv", ".flv",
       ".3gp", ".ogv", ".mxf")
SUBDIR = "h264"


def codec_of(path):
    try:
        sys.path.insert(0, HERE)
        import xray_runfile
        return xray_runfile.video_codec(path) or "?"
    except Exception:
        return "?"


def videos(folder):
    if os.path.isfile(folder):
        return [folder]
    out = []
    for root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if d != SUBDIR]          # not the converted copies
        out += [os.path.join(root, f) for f in sorted(files) if f.lower().endswith(EXT)]
    return out


def check(a):
    try:
        import cv2
    except ImportError:
        print("This image has no OpenCV (cv2): your program reads videos with another library, so this check "
              "(which uses OpenCV) does not apply to it.")
        return 2
    files = videos(a.folder)
    print(f"xray_videos {VERSION} - the videos in {a.folder}, read with the OpenCV inside this image (cv2 "
          f"{cv2.__version__}), {a.frames} frames each")
    if not files:
        print("  no video files there (" + " ".join(EXT) + ")")
        return 0
    w = min(max(len(os.path.relpath(f, a.folder)) for f in files) + 2, 60)
    bad = []
    for f in files:
        rel = os.path.relpath(f, a.folder) if os.path.isdir(a.folder) else os.path.basename(f)
        codec = codec_of(f)
        c = cv2.VideoCapture(f)
        opened = c.isOpened()
        n = 0
        while opened and n < a.frames:
            ok, _frame = c.read()
            if not ok:
                break
            n += 1
        if n:
            wd, ht = int(c.get(cv2.CAP_PROP_FRAME_WIDTH)), int(c.get(cv2.CAP_PROP_FRAME_HEIGHT))
            res = f"OK  {wd}x{ht}  {c.get(cv2.CAP_PROP_FPS):.1f} fps  {int(c.get(cv2.CAP_PROP_FRAME_COUNT))} frames"
        else:
            bad.append(rel)
            res = ("CANNOT READ: it opens, but no frame comes out - OpenCV here has no decoder for " + codec
                   if opened else "CANNOT READ: OpenCV cannot open it (broken file, or a format it does not know)")
        c.release()
        print(f"  {rel:<{w}} {codec:<16} {res}", flush=True)
    if a.out:
        os.makedirs(a.out, exist_ok=True)
        lst = os.path.join(a.out, "unreadable.txt")
        if bad:
            with open(lst, "w", encoding="utf-8") as fh:
                fh.write("\n".join(bad) + "\n")
        elif os.path.exists(lst):
            os.remove(lst)
    print(f"{len(files) - len(bad)} of {len(files)} videos can be read here." +
          (f" {len(bad)} cannot: listed in unreadable.txt" + (" - videos.bat converts them to H.264 now."
                                                               if a.out else ".") if bad else ""))
    return 1 if bad else 0


def ffmpeg_exe():
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


def gpu_encoder(exe):
    """can this ffmpeg encode on the GPU here? -> (True, '') or (False, why). A one-second test video is encoded:
    the encoder must be in ffmpeg AND the container must have the GPU with its video libraries"""
    r = subprocess.run([exe, "-hide_banner", "-encoders"], capture_output=True, text=True)
    if "h264_nvenc" not in (r.stdout or ""):
        return False, "this ffmpeg has no NVIDIA encoder (rebuild the image: docker build -t xray-ffmpeg:2 ...)"
    r = subprocess.run([exe, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                        "color=c=black:s=256x256:r=25:d=1", "-c:v", "h264_nvenc", "-f", "null", "-"],
                       capture_output=True, text=True)
    if r.returncode == 0:
        return True, ""
    lines = (r.stderr or "").strip().splitlines() or ["?"]
    last = next((x for x in lines if "nvenc" in x.lower()), lines[-1])[:160]   # the encoder's own reason first
    return False, ("the container has no GPU (run it with --gpus all -e NVIDIA_DRIVER_CAPABILITIES=compute,video,"
                   f"utility) or the driver has no video encoder here - ffmpeg: {last}")


def encode_args(gpu, a):
    """H.264, 8-bit 4:2:0 (every decoder reads it), visually the same quality, audio as AAC (if there is any)"""
    if gpu:      # NVENC: constant quality (-cq ~ x264's -crf), p5 = a slower, better preset of its p1..p7
        return ["-c:v", "h264_nvenc", "-preset", "p5", "-tune", "hq", "-rc", "vbr", "-cq", str(a.crf), "-b:v", "0",
                "-profile:v", "high", "-pix_fmt", "yuv420p"]
    return ["-c:v", "libx264", "-preset", a.preset, "-crf", str(a.crf), "-pix_fmt", "yuv420p"]


def convert(a):
    with open(a.list, encoding="utf-8") as fh:
        todo = [x.strip() for x in fh if x.strip()]
    exe = ffmpeg_exe()
    out_dir = os.path.join(a.folder, a.to)
    os.makedirs(out_dir, exist_ok=True)
    gpu, why = (False, "--gpu no") if a.gpu == "no" else gpu_encoder(exe)
    if a.gpu == "yes" and not gpu:
        print(f"xray_videos {VERSION}: --gpu yes, but the GPU encoder does not work here: {why}")
        return 1
    print(f"xray_videos {VERSION} - converting {len(todo)} video(s) to H.264 into {a.to}/ (the originals stay as "
          "they are). " + ("Encoder: h264_nvenc, the NVIDIA GPU." if gpu else
                           f"Encoder: libx264, the CPU - a long 4K video takes minutes. GPU not used: {why}"),
          flush=True)
    done = []
    for rel in todo:
        src = os.path.join(a.folder, rel)
        dst = os.path.join(out_dir, os.path.splitext(rel)[0] + ".mp4")       # same subfolders: no name clashes
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if os.path.exists(dst) and os.path.getsize(dst) > 0 and os.path.getmtime(dst) >= os.path.getmtime(src):
            done.append(dst)                    # converted before (and the original has not changed since)
            print(f"  {rel}: already converted -> {a.to}/{os.path.relpath(dst, out_dir).replace(os.sep, '/')}")
            continue
        t0 = time.time()
        base = [exe, "-hide_banner", "-loglevel", "error", "-y", "-i", src, "-map", "0:v:0", "-map", "0:a?"]
        tail = ["-c:a", "aac", "-movflags", "+faststart", dst]
        r = subprocess.run(base + encode_args(gpu, a) + tail, capture_output=True, text=True)
        on = "GPU" if gpu else "CPU"
        if gpu and r.returncode != 0:       # e.g. wider than the encoder takes (H.264 on NVENC: 4096 px): the CPU
            print(f"  {rel}: the GPU encoder refused it ({(r.stderr or '').strip()[-160:]}) - on the CPU instead",
                  flush=True)
            r = subprocess.run(base + encode_args(False, a) + tail, capture_output=True, text=True)
            on = "CPU"
        if r.returncode == 0 and os.path.exists(dst):
            done.append(dst)
            new = a.to + "/" + os.path.relpath(dst, out_dir).replace(os.sep, "/")
            print(f"  {rel} -> {new}  ({os.path.getsize(dst) / 1048576:,.1f} MB, {time.time() - t0:.0f} s, {on})"
                  + (f"\n      in run.env, for the program's own video:  {a.hint}/{new}" if a.hint else ""), flush=True)
        else:
            print(f"  {rel}: conversion FAILED - {(r.stderr or '').strip()[-300:]}", flush=True)
    print(f"Done: {len(done)} of {len(todo)} converted, in the subfolder {a.to} of the same folder.")
    return 0 if len(done) == len(todo) else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="can the image read these videos? convert the ones it cannot")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="(inside the app image) open every video with its OpenCV and read some frames")
    c.add_argument("folder", help="a folder (subfolders too) or one video file")
    c.add_argument("--frames", type=int, default=30, help="frames to read from each video (default 30)")
    c.add_argument("--out", help="folder for unreadable.txt (the list for convert)")
    v = sub.add_parser("convert", help="(in the ffmpeg image) convert the listed videos to H.264")
    v.add_argument("folder", help="the folder the list is relative to")
    v.add_argument("--list", required=True, help="unreadable.txt from check")
    v.add_argument("--to", default=SUBDIR, help=f"subfolder for the copies (default {SUBDIR})")
    v.add_argument("--crf", type=int, default=18, help="H.264 quality, lower = better and bigger (default 18; the "
                                                         "GPU encoder takes it as -cq)")
    v.add_argument("--preset", default="fast", help="x264 speed: ultrafast ... veryslow (default fast)")
    v.add_argument("--hint", help="ENV_NAME=/container/folder: print the run.env line for each converted file")
    v.add_argument("--gpu", choices=("auto", "yes", "no"), default="auto",
                   help="NVIDIA GPU encoder: auto = when it works here (default), yes = fail without it, no = CPU only")
    a = ap.parse_args(argv)
    return check(a) if a.cmd == "check" else convert(a)


if __name__ == "__main__":
    sys.exit(main())
