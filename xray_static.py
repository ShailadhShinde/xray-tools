#!/usr/bin/env python3
"""
xray_static.py - X-Ray Phase 1: static mapper (Python standard library only).

Reads a Python project WITHOUT running it and maps everything the program may
need or touch, starting from one or more entrypoints: local modules, packages,
dynamic imports, model/config/data files, writes, env vars, secrets, network
endpoints, listening ports, subprocess binaries, native libraries, GUI/GPU use,
multiprocessing, shared memory, thread config, hardware identity, OS data,
unused/duplicate/dangling files, shadowing and requirements problems.

Beyond a plain AST walk it binds function parameters to the values callers pass (so
`download(url)` called as `download(f"{HUB}/m.onnx")` reports the real URL), follows
Process/Thread targets in the call graph, folds CONFIG["key"] dict lookups, and reads the
class paths stored inside pickles / torch .pt files (pickletools; nothing is executed).

Every fact carries:
  evidence             static | static_partial | unresolved | derived
  required_at_runtime  yes | conditional | no      (best static estimate)

Example:
  python xray_static.py --root F1_import_maze --entry src/main.py --exclude "tools/*"
Then open the JSON in xray_viewer.html, or grade it with xray_score.py.
"""
from __future__ import annotations

import argparse
import ast
import fnmatch
import hashlib
import json
import os
import posixpath
import re
import shlex
import sys
from pathlib import Path
from urllib.parse import urlsplit

VERSION = "0.5.6"
PLACE = "<var>"
REQ = {"no": 0, "conditional": 1, "yes": 2}


def req_min(a, b):
    return a if REQ[a] <= REQ[b] else b


def req_max(a, b):
    return a if REQ[a] >= REQ[b] else b


# =============================================================== knowledge
_FALLBACK_STDLIB = set("""__future__ _thread abc aifc argparse array ast asynchat asyncio asyncore
atexit audioop base64 bdb binascii bisect builtins bz2 calendar cgi cgitb chunk cmath cmd code codecs
codeop collections colorsys compileall concurrent configparser contextlib contextvars copy copyreg
cProfile crypt csv ctypes curses dataclasses datetime dbm decimal difflib dis doctest email encodings
ensurepip enum errno faulthandler fcntl filecmp fileinput fnmatch fractions ftplib functools gc getopt
getpass gettext glob graphlib grp gzip hashlib heapq hmac html http imaplib imghdr imp importlib
inspect io ipaddress itertools json keyword lib2to3 linecache locale logging lzma mailbox mailcap
marshal math mimetypes mmap modulefinder msilib msvcrt multiprocessing netrc nis nntplib nt ntpath
numbers opcode operator optparse os ossaudiodev pathlib pdb pickle pickletools pipes pkgutil platform
plistlib poplib posix posixpath pprint profile pstats pty pwd py_compile pyclbr pydoc queue quopri
random re readline reprlib resource rlcompleter runpy sched secrets select selectors shelve shlex
shutil signal site smtpd smtplib sndhdr socket socketserver spwd sqlite3 sre_compile sre_constants
sre_parse ssl stat statistics string stringprep struct subprocess sunau symtable sys sysconfig syslog
tabnanny tarfile telnetlib tempfile termios textwrap threading time timeit tkinter token tokenize
tomllib trace traceback tracemalloc tty turtle types typing unicodedata unittest urllib uu uuid venv
warnings wave weakref webbrowser winreg winsound wsgiref xdrlib xml xmlrpc zipapp zipfile zipimport
zlib zoneinfo _winapi""".split())
STDLIB = set(getattr(sys, "stdlib_module_names", ())) or _FALLBACK_STDLIB

# import name -> PyPI distribution (only where they differ or are commonly confused)
IMPORT_TO_DIST = {
    "yaml": "pyyaml", "PIL": "pillow", "cv2": "opencv-python", "dateutil": "python-dateutil",
    "skimage": "scikit-image", "sklearn": "scikit-learn", "bs4": "beautifulsoup4",
    "psycopg2": "psycopg2", "jwt": "pyjwt", "dotenv": "python-dotenv", "serial": "pyserial",
    "Crypto": "pycryptodome", "OpenSSL": "pyopenssl", "magic": "python-magic", "attr": "attrs",
    "google.protobuf": "protobuf", "faiss": "faiss-cpu", "docx": "python-docx",
    "pptx": "python-pptx", "fitz": "pymupdf", "zmq": "pyzmq", "gi": "pygobject",
    "win32api": "pywin32", "win32con": "pywin32", "pythoncom": "pywin32",
    "mysql": "mysql-connector-python", "MySQLdb": "mysqlclient",
    "flask_talisman": "flask-talisman", "flask_cors": "flask-cors",
    "flask_sqlalchemy": "flask-sqlalchemy", "paho": "paho-mqtt", "usb": "pyusb", "nacl": "pynacl",
    "mpl_toolkits": "matplotlib", "deep_sort_realtime": "deep-sort-realtime",
    "pytorch_grad_cam": "grad-cam", "huggingface_hub": "huggingface-hub", "grpc": "grpcio",
    "face_recognition": "face-recognition", "yt_dlp": "yt-dlp", "lazy_loader": "lazy-loader",
    "pkg_resources": "setuptools", "jose": "python-jose", "slugify": "python-slugify",
    "multipart": "python-multipart", "socketio": "python-socketio", "engineio": "python-engineio",
    "sqlite_vec": "sqlite-vec", "kafka": "kafka-python", "telegram": "python-telegram-bot",
}
# import name -> distributions that all provide it (variants)
VARIANTS = {
    "cv2": ["opencv-python", "opencv-python-headless", "opencv-contrib-python",
            "opencv-contrib-python-headless"],
    "psycopg2": ["psycopg2", "psycopg2-binary"],
    "onnxruntime": ["onnxruntime", "onnxruntime-gpu", "onnxruntime-directml",
                    "onnxruntime-openvino", "onnxruntime-silicon"],
    "tensorflow": ["tensorflow", "tensorflow-cpu", "tensorflow-gpu"],
    "faiss": ["faiss-cpu", "faiss-gpu"],
    "PIL": ["pillow", "pillow-simd"],
    "mysql": ["mysql-connector-python", "mysql-connector"],
    "yaml": ["pyyaml"],
}
SOURCE_BUILD = {
    "psycopg2": "compiles against libpq: build needs gcc + libpq-dev (prebuilt: psycopg2-binary)",
    "mysqlclient": "compiles: needs gcc + default-libmysqlclient-dev + pkg-config",
    "dlib": "compiles C++ (slow): needs cmake + build-essential",
    "face-recognition": "depends on dlib, which compiles",
    "pyaudio": "compiles: needs gcc + portaudio19-dev",
    "uwsgi": "compiles: needs gcc + python headers",
    "pycairo": "compiles: needs libcairo2-dev + pkg-config",
    "insightface": "may compile Cython extensions: needs gcc/g++",
}
APT_BY_DIST = {
    "opencv-python": ["libgl1", "libglib2.0-0"],
    "opencv-contrib-python": ["libgl1", "libglib2.0-0"],
    "opencv-python-headless": ["libglib2.0-0 (some versions)"],
    "opencv-contrib-python-headless": ["libglib2.0-0 (some versions)"],
    "onnxruntime": ["libgomp1"],
    "onnxruntime-gpu": ["libgomp1", "CUDA + cuDNN runtime (GPU base image)"],
    "psycopg2": ["libpq5 (runtime) + gcc/libpq-dev (build)"],
    "mysqlclient": ["libmariadb3 (runtime)"],
    "pyaudio": ["libportaudio2"], "sounddevice": ["libportaudio2"],
    "pytesseract": ["tesseract-ocr"], "pdf2image": ["poppler-utils"], "pyzbar": ["libzbar0"],
    "python-magic": ["libmagic1"],
    "mediapipe": ["libgl1 + libglib2.0-0 (via opencv-contrib-python)"],
    "ultralytics": ["libgl1 + libglib2.0-0 (via opencv-python)"],
}
GUI_TO_HEADLESS = {"opencv-python": "opencv-python-headless",
                   "opencv-contrib-python": "opencv-contrib-python-headless"}
BINARY_SWAP = {"psycopg2": "psycopg2-binary"}
BIN_TO_APT = {
    "ffmpeg": "ffmpeg", "ffprobe": "ffmpeg", "git": "git", "curl": "curl", "wget": "wget",
    "tesseract": "tesseract-ocr", "pdftotext": "poppler-utils", "pdftoppm": "poppler-utils",
    "convert": "imagemagick", "magick": "imagemagick", "gs": "ghostscript", "openssl": "openssl",
    "ping": "iputils-ping", "ps": "procps", "unzip": "unzip", "zip": "zip", "7z": "p7zip-full",
    "nvidia-smi": "(host NVIDIA driver via container toolkit)",
    "gst-launch-1.0": "gstreamer1.0-tools", "v4l2-ctl": "v4l-utils",
    "psql": "postgresql-client", "pg_dump": "postgresql-client", "mysql": "default-mysql-client",
}
LIB_TO_APT = {
    "gomp": "libgomp1", "GL": "libgl1", "glib-2.0": "libglib2.0-0", "gthread-2.0": "libglib2.0-0",
    "SM": "libsm6", "ICE": "libice6", "Xext": "libxext6", "Xrender": "libxrender1",
    "X11": "libx11-6", "z": "zlib1g", "ssl": "libssl3", "crypto": "libssl3", "pq": "libpq5",
    "jpeg": "libjpeg62-turbo", "turbojpeg": "libturbojpeg0", "png16": "libpng16-16",
    "tiff": "libtiff6", "zbar": "libzbar0", "magic": "libmagic1", "portaudio": "libportaudio2",
    "usb-1.0": "libusb-1.0-0", "v4l2": "libv4l-0", "stdc++": "libstdc++6",
    "m": "(libc, always present)", "c": "(libc, always present)", "dl": "(libc, always present)",
    "rt": "(libc, always present)", "pthread": "(libc, always present)",
    "cuda": "(host NVIDIA driver)", "cudart": "(CUDA runtime: GPU base image)",
    "cudnn": "(cuDNN: GPU base image)", "nvinfer": "(TensorRT)",
}
# ctypes.util.find_library("x") returns the soname on Linux
SONAME = {
    "gomp": "libgomp.so.1", "GL": "libGL.so.1", "glib-2.0": "libglib-2.0.so.0",
    "gthread-2.0": "libgthread-2.0.so.0", "SM": "libSM.so.6", "ICE": "libICE.so.6", "Xext": "libXext.so.6",
    "Xrender": "libXrender.so.1", "X11": "libX11.so.6", "z": "libz.so.1", "ssl": "libssl.so.3",
    "crypto": "libcrypto.so.3", "pq": "libpq.so.5", "zbar": "libzbar.so.0", "magic": "libmagic.so.1",
    "portaudio": "libportaudio.so.2", "usb-1.0": "libusb-1.0.so.0", "stdc++": "libstdc++.so.6",
    "m": "libm.so.6", "c": "libc.so.6", "dl": "libdl.so.2", "rt": "librt.so.1", "pthread": "libpthread.so.0",
    "turbojpeg": "libturbojpeg.so.0", "png16": "libpng16.so.16", "tiff": "libtiff.so.6",
}
NOTABLE_STDLIB = {
    "winreg": "Windows-only module: ImportError inside Linux containers",
    "msvcrt": "Windows-only module", "winsound": "Windows-only module", "_winapi": "Windows-only",
    "tkinter": "needs Tk libraries and a display", "turtle": "needs Tk and a display",
    "curses": "needs ncurses", "zoneinfo": "needs system tzdata or the pip 'tzdata' package",
    "fcntl": "POSIX-only", "termios": "POSIX-only", "grp": "POSIX-only", "pwd": "POSIX-only",
    "resource": "POSIX-only",
}
GUI_IMPORTS = {"tkinter", "PyQt5", "PyQt6", "PySide2", "PySide6", "wx", "kivy", "pygame", "pyglet"}

MODEL_EXT = {".onnx", ".pt", ".pth", ".pb", ".tflite", ".task", ".engine", ".trt", ".plan", ".h5", ".keras",
             ".caffemodel", ".prototxt", ".weights", ".safetensors", ".ckpt", ".model", ".param",
             ".mlmodel", ".joblib", ".pkl"}
CONFIG_EXT = {".yaml", ".yml", ".json", ".toml", ".ini", ".cfg", ".conf", ".env", ".properties"}
KEY_EXT = {".pem", ".key", ".pfx", ".p12", ".crt", ".cer", ".csr", ".jks", ".der"}
SAVE_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp", ".tif", ".tiff", ".pdf"} | MODEL_EXT
JUNK_DIRS = {".git", ".idea", ".vscode", "__pycache__", ".mypy_cache", ".pytest_cache",
             ".ruff_cache", ".venv", "venv", ".tox", "node_modules", ".ipynb_checkpoints", ".eggs"}
JUNK_FILES = {"Thumbs.db", ".DS_Store", "desktop.ini"}
META_RE = re.compile(
    r"(?i)^(readme.*|license.*|changelog.*|requirements.*\.txt|dockerfile.*|.*\.dockerfile|"
    r"docker-compose.*\.ya?ml|compose.*\.ya?ml|\.dockerignore|\.gitignore|\.gitattributes|setup\.py|"
    r"setup\.cfg|pyproject\.toml|pipfile.*|poetry\.lock|makefile|answer_key.*\.json|.*\.md|.*\.rst)$")

STRONG_SECRET = re.compile(r"(?i)(password|passwd|secret|api_?key|private_?key|access_?key|credential)")
WEAK_SECRET = re.compile(r"(?i)(?:^|[_\-])(pass|pwd|pw|token)(?:$|[_\-])")
IPV4_RE = re.compile(r"(?<![\d.])(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)(?![\d.])")
URL_RE = re.compile(r"\b([a-zA-Z][a-zA-Z0-9+.-]{1,15})://([^\s'\"<>]+)")
URL_SCHEMES = {"http", "https", "rtsp", "rtsps", "rtmp", "postgresql", "postgres", "mysql", "redis",
               "mongodb", "amqp", "mqtt", "ws", "wss", "ftp", "sftp", "tcp", "udp"}
DSN_PW_RE = re.compile(r"(?i)\b(password|pwd)\s*=\s*([^;\s]+)")
GPU_PROVIDER_RE = re.compile(r"(CUDA|Tensorrt|TensorRT|ROCM|MIGraphX)ExecutionProvider")
LOOPBACK = {"0.0.0.0", "127.0.0.1", "255.255.255.255", "255.255.255.0"}
SCHEME_PORTS = {"http": 80, "https": 443, "rtsp": 554, "rtsps": 322, "rtmp": 1935, "postgresql": 5432,
                "postgres": 5432, "mysql": 3306, "redis": 6379, "mongodb": 27017, "amqp": 5672,
                "mqtt": 1883, "ftp": 21, "ws": 80, "wss": 443}
THREAD_ENVS = {"OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMEXPR_NUM_THREADS",
               "VECLIB_MAXIMUM_THREADS", "TF_NUM_INTRAOP_THREADS", "TF_NUM_INTEROP_THREADS"}
GPU_ENVS = {"CUDA_VISIBLE_DEVICES", "NVIDIA_VISIBLE_DEVICES", "CUDA_DEVICE_ORDER"}
GPU_FLAGS = {"use_cuda", "use_gpu", "cuda", "gpu", "on_gpu"}
APPISH = {"app", "application", "flask_app", "api", "server", "dash_app", "socketio", "sio"}
PATH_KWS = ("filename", "path", "file", "f", "fname", "filepath", "model_path", "path_or_bytes",
            "src", "dst", "name")

# ---- call tables (canonical dotted names after alias resolution)
ENV_READ = {"os.getenv", "os.environ.get", "os.environ.setdefault", "os.environ.pop"}
DYN_IMPORT = {"importlib.import_module", "__import__", "importlib.__import__"}
SPEC_LOAD = {"importlib.util.spec_from_file_location", "importlib.machinery.SourceFileLoader",
             "imp.load_source", "runpy.run_path"}
DISCOVERY = {"pkgutil.iter_modules", "pkgutil.walk_packages"}
SYSPATH = {"sys.path.insert": "insert", "sys.path.append": "append", "site.addsitedir": "append",
           "sys.path.extend": "extend"}
RESOURCE_FILES = {"importlib.resources.files", "importlib_resources.files"}
RESOURCE_CALLS = {"importlib.resources.read_text", "importlib.resources.read_binary",
                  "importlib.resources.open_text", "importlib.resources.open_binary",
                  "importlib.resources.path", "pkg_resources.resource_filename",
                  "pkg_resources.resource_string", "pkg_resources.resource_stream"} | RESOURCE_FILES
OPEN_CALLS = {"open", "io.open", "builtins.open", "codecs.open", "gzip.open", "bz2.open", "lzma.open"}
IO_CALLS = {  # canonical: (path arg index, op, io kind)
    "cv2.imwrite": (0, "write", "c_level_io"), "cv2.imread": (0, "read", "c_level_io"),
    "cv2.VideoWriter": (0, "write", "c_level_io"), "cv2.dnn.readNet": (0, "read", "c_level_io"),
    "cv2.dnn.readNetFromONNX": (0, "read", "c_level_io"),
    "cv2.dnn.readNetFromCaffe": (1, "read", "c_level_io"),
    "cv2.dnn.readNetFromDarknet": (1, "read", "c_level_io"),
    "cv2.dnn.readNetFromTensorflow": (0, "read", "c_level_io"),
    "cv2.CascadeClassifier": (0, "read", "c_level_io"),
    "cv2.FaceDetectorYN.create": (0, "read", "c_level_io"),
    "cv2.FaceDetectorYN_create": (0, "read", "c_level_io"),
    "cv2.FaceRecognizerSF.create": (0, "read", "c_level_io"),
    "onnxruntime.InferenceSession": (0, "read", "c_level_io"),
    "onnx.load": (0, "read", "python_io"), "onnx.save": (1, "write", "python_io"),
    "torch.load": (0, "read", "python_io"), "torch.jit.load": (0, "read", "python_io"),
    "torch.save": (1, "write", "python_io"), "torch.onnx.export": (2, "write", "python_io"),
    "numpy.save": (0, "write", "python_io"), "numpy.savez": (0, "write", "python_io"),
    "numpy.savez_compressed": (0, "write", "python_io"), "numpy.savetxt": (0, "write", "python_io"),
    "numpy.load": (0, "read", "python_io"), "numpy.loadtxt": (0, "read", "python_io"),
    "numpy.genfromtxt": (0, "read", "python_io"), "numpy.fromfile": (0, "read", "python_io"),
    "joblib.load": (0, "read", "python_io"), "joblib.dump": (1, "write", "python_io"),
    "pandas.read_csv": (0, "read", "python_io"), "pandas.read_json": (0, "read", "python_io"),
    "pandas.read_excel": (0, "read", "python_io"), "pandas.read_parquet": (0, "read", "python_io"),
    "pandas.read_pickle": (0, "read", "python_io"), "PIL.Image.open": (0, "read", "python_io"),
    "matplotlib.pyplot.savefig": (0, "write", "python_io"),
    "logging.FileHandler": (0, "write", "python_io"),
    "logging.handlers.RotatingFileHandler": (0, "write", "python_io"),
    "logging.handlers.TimedRotatingFileHandler": (0, "write", "python_io"),
    "logging.handlers.WatchedFileHandler": (0, "write", "python_io"),
    "os.makedirs": (0, "write", "python_io"), "os.mkdir": (0, "write", "python_io"),
    "shutil.copy": (1, "write", "python_io"), "shutil.copy2": (1, "write", "python_io"),
    "shutil.copyfile": (1, "write", "python_io"), "shutil.move": (1, "write", "python_io"),
    "shutil.copytree": (1, "write", "python_io"), "shutil.rmtree": (0, "write", "python_io"),
    "os.remove": (0, "write", "python_io"), "os.unlink": (0, "write", "python_io"),
    "sqlite3.connect": (0, "write", "python_io"), "ultralytics.YOLO": (0, "read", "python_io"),
    "tensorflow.keras.models.load_model": (0, "read", "python_io"),
    "keras.models.load_model": (0, "read", "python_io"),
    "os.path.isfile": (0, "check", "python_io"), "os.path.exists": (0, "check", "python_io"),
    "os.path.isdir": (0, "check", "python_io"), "os.listdir": (0, "read", "python_io"),
    "glob.glob": (0, "read", "python_io"),
}
METHOD_IO = {"read_text": "read", "read_bytes": "read", "write_text": "write", "write_bytes": "write",
             "mkdir": "write", "touch": "write", "unlink": "write", "savefig": "write",
             "to_csv": "write", "to_json": "write", "to_parquet": "write", "to_pickle": "write",
             "exists": "check", "is_file": "check", "is_dir": "check", "iterdir": "read",
             "glob": "read", "rglob": "read"}
NET_CALLS = {
    "psycopg2.connect": "db", "psycopg.connect": "db", "psycopg2.pool.SimpleConnectionPool": "db",
    "psycopg2.pool.ThreadedConnectionPool": "db", "asyncpg.connect": "db",
    "asyncpg.create_pool": "db", "pymysql.connect": "db", "mysql.connector.connect": "db",
    "MySQLdb.connect": "db", "redis.Redis": "db", "redis.StrictRedis": "db",
    "sqlalchemy.create_engine": "url", "redis.from_url": "url", "pymongo.MongoClient": "url",
    "requests.get": "url", "requests.post": "url", "requests.put": "url", "requests.patch": "url",
    "requests.delete": "url", "requests.head": "url", "requests.request": "url",
    "httpx.get": "url", "httpx.post": "url", "urllib.request.urlopen": "url",
    "urllib.request.Request": "url", "urllib.request.urlretrieve": "download",
    "socket.create_connection": "tuple", "smtplib.SMTP": "hostport", "smtplib.SMTP_SSL": "hostport",
    "ftplib.FTP": "hostport", "cv2.VideoCapture": "capture",
    "huggingface_hub.hf_hub_download": "hub", "huggingface_hub.snapshot_download": "hub",
    "torch.hub.load": "hub", "torch.hub.load_state_dict_from_url": "download",
    "torch.hub.download_url_to_file": "download", "gdown.download": "download",
    "wget.download": "download", "insightface.app.FaceAnalysis": "hub",
}
C_LEVEL_NET = {"psycopg2", "MySQLdb", "cv2"}
DB_DEFAULT_PORTS = {"psycopg2": 5432, "psycopg": 5432, "asyncpg": 5432, "pymysql": 3306,
                    "mysql": 3306, "MySQLdb": 3306, "redis": 6379}
LISTEN_CALLS = {"uvicorn.run": ("127.0.0.1", 8000), "waitress.serve": ("0.0.0.0", 8080),
                "aiohttp.web.run_app": ("0.0.0.0", 8080), "http.server.HTTPServer": "tuple",
                "http.server.ThreadingHTTPServer": "tuple", "socketserver.TCPServer": "tuple",
                "socketserver.ThreadingTCPServer": "tuple"}
SUBPROC = {"subprocess.run", "subprocess.call", "subprocess.check_call", "subprocess.check_output",
           "subprocess.Popen", "subprocess.getoutput", "subprocess.getstatusoutput", "os.system",
           "os.popen", "os.execv", "os.execvp", "os.execvpe", "os.execl", "os.execlp",
           "asyncio.create_subprocess_exec", "asyncio.create_subprocess_shell"}
NATIVE = {"ctypes.CDLL", "ctypes.cdll.LoadLibrary", "ctypes.PyDLL", "ctypes.WinDLL",
          "ctypes.windll.LoadLibrary", "ctypes.util.find_library"}
GUI_CALLS = {"cv2.imshow", "cv2.namedWindow", "cv2.waitKey", "cv2.waitKeyEx", "cv2.destroyAllWindows",
             "cv2.destroyWindow", "cv2.setMouseCallback", "cv2.createTrackbar", "cv2.selectROI",
             "cv2.selectROIs", "cv2.moveWindow", "cv2.resizeWindow", "cv2.startWindowThread",
             "cv2.setWindowProperty", "matplotlib.pyplot.show", "matplotlib.pyplot.ion",
             "tkinter.Tk", "PIL.ImageShow.show"}
MP_CALLS = {"multiprocessing.Process", "multiprocessing.Pool", "multiprocessing.set_start_method",
            "multiprocessing.get_context", "multiprocessing.Manager", "multiprocessing.Queue",
            "multiprocessing.JoinableQueue", "multiprocessing.Pipe",
            "concurrent.futures.ProcessPoolExecutor", "torch.multiprocessing.spawn",
            "torch.multiprocessing.Process", "torch.multiprocessing.set_start_method",
            "torch.multiprocessing.Pool"}
SHM_CALLS = {"multiprocessing.shared_memory.SharedMemory", "multiprocessing.shared_memory.ShareableList",
             "multiprocessing.Array", "multiprocessing.RawArray", "multiprocessing.Value",
             "multiprocessing.RawValue", "multiprocessing.sharedctypes.RawArray",
             "multiprocessing.sharedctypes.Array", "multiprocessing.managers.SharedMemoryManager"}
THREAD_CALLS = {"cv2.setNumThreads", "torch.set_num_threads", "torch.set_num_interop_threads",
                "numexpr.set_num_threads", "threadpoolctl.threadpool_limits", "mkl.set_num_threads"}
HWID_CALLS = {"uuid.getnode", "getmac.get_mac_address", "psutil.net_if_addrs",
              "netifaces.ifaddresses", "netifaces.interfaces", "socket.gethostname",
              "platform.node", "uuid.uuid1"}
MAC_RE = re.compile(r"^[0-9A-Fa-f]{2}([:-])[0-9A-Fa-f]{2}(\1[0-9A-Fa-f]{2}){4}$")
# probes ask whether a GPU exists - code that probes has a CPU path, so they never make a GPU required
GPU_PROBES = {"torch.cuda.is_available", "torch.cuda.device_count", "torch.cuda.is_initialized",
              "torch.backends.cudnn.is_available", "torch.backends.mps.is_available",
              "cv2.cuda.getCudaEnabledDeviceCount", "onnxruntime.get_available_providers",
              "onnxruntime.get_device", "tensorflow.config.list_physical_devices"}
# callables handed to these run in a new process / thread: `target=` is a call edge
TARGET_CALLS = {"multiprocessing.Process", "multiprocessing.context.Process", "torch.multiprocessing.Process",
                "threading.Thread", "threading.Timer"}
# class Worker(mp.Process / Thread): Worker(...).start() runs Worker.run() in the new process / thread
WORKER_BASES = {"multiprocessing.Process", "multiprocessing.context.Process", "multiprocessing.process.BaseProcess",
                "torch.multiprocessing.Process", "multiprocess.Process", "threading.Thread"}
PICKLE_SKIP = {"builtins", "__builtin__", "copyreg", "copy_reg", "_codecs", "collections", "__main__"}
TZ_CALLS = {"zoneinfo.ZoneInfo", "pytz.timezone", "dateutil.tz.gettz", "backports.zoneinfo.ZoneInfo"}
FONT_CALLS = {"PIL.ImageFont.truetype", "PIL.ImageFont.FreeTypeFont"}
TMP_CALLS = {"tempfile.mkdtemp", "tempfile.mkstemp", "tempfile.NamedTemporaryFile",
             "tempfile.TemporaryDirectory", "tempfile.TemporaryFile", "tempfile.gettempdir"}
PICKLE_CALLS = {"pickle.load", "pickle.loads", "joblib.load", "dill.load", "dill.loads",
                "cloudpickle.load", "pandas.read_pickle", "torch.load", "numpy.load"}
MESSAGE_FUNCS = {"print", "logging.debug", "logging.info", "logging.warning", "logging.warn",
                 "logging.error", "logging.exception", "logging.critical", "warnings.warn",
                 "click.echo", "sys.stdout.write", "sys.stderr.write"}
MESSAGE_METHODS = {"debug", "info", "warning", "warn", "error", "exception", "critical"}
PATH_BUILDERS = {"os.path.join", "pathlib.Path", "pathlib.PurePath", "pathlib.PosixPath"}
PATH_FUNCS = {
    "os.path.dirname": lambda t: posixpath.dirname(t.rstrip("/")) if t not in ("/", "") else t,
    "os.path.abspath": posixpath.normpath, "os.path.realpath": posixpath.normpath,
    "os.path.normpath": posixpath.normpath, "os.path.expanduser": lambda t: t,
    "os.path.expandvars": lambda t: t, "os.path.basename": posixpath.basename,
}
PATHLIB_CTORS = {"pathlib.Path", "pathlib.PurePath", "pathlib.PosixPath", "pathlib.PurePosixPath",
                 "pathlib.WindowsPath", "pathlib.PureWindowsPath"}

ALL_KNOWN = (ENV_READ | DYN_IMPORT | SPEC_LOAD | DISCOVERY | set(SYSPATH) | RESOURCE_CALLS | OPEN_CALLS
             | set(IO_CALLS) | set(NET_CALLS) | set(LISTEN_CALLS) | SUBPROC | NATIVE | GUI_CALLS
             | MP_CALLS | SHM_CALLS | THREAD_CALLS | HWID_CALLS | TZ_CALLS | FONT_CALLS | TMP_CALLS
             | PICKLE_CALLS | MESSAGE_FUNCS | PATH_BUILDERS | set(PATH_FUNCS) | {"exec", "execfile"})


# =============================================================== helpers
def norm_dist(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def secretish(name):
    return bool(STRONG_SECRET.search(name) or WEAK_SECRET.search(name))


def mask(s):
    s = str(s)
    return (s[:2] + "***") if len(s) > 4 else "***"


def mask_url(u):
    return re.sub(r"(://[^:/@\s]+):([^@/\s]+)@", r"\1:***@", u)


def dotted(node):
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def kwarg(node, *names):
    for k in node.keywords:
        if k.arg in names:
            return k.value
    return None


def arg_or_kw(node, idx, *names):
    if idx is not None and len(node.args) > idx and not isinstance(node.args[idx], ast.Starred):
        return node.args[idx]
    return kwarg(node, *names)


def unparse(node, limit=80):
    if node is None:
        return ""
    try:
        s = ast.unparse(node)
    except Exception:
        return "<expr>"
    return s if len(s) <= limit else s[: limit - 3] + "..."


def libname(s):
    b = posixpath.basename(s.replace("\\", "/"))
    b = re.sub(r"\.(so(\.\d+)*|dll|dylib)$", "", b)
    return b[3:] if b.startswith("lib") and len(b) > 3 else b


def write_dir(subject):
    s = subject.split("<", 1)[0] if "<" in subject else subject
    if s.endswith("/"):
        return s.rstrip("/") or "/"
    if "." in posixpath.basename(s):
        s = posixpath.dirname(s)
    return s or "."


def sha256(p):
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class FV:
    """Folded value: the best-effort string an expression evaluates to."""
    __slots__ = ("t", "exact", "origin", "envs", "num")

    def __init__(self, t, exact=True, origin=(), envs=(), num=False):
        self.t = t
        self.exact = exact
        self.origin = frozenset(origin)
        self.envs = tuple(envs)
        self.num = num


def fvt(p):
    return p.t if p is not None else PLACE


def merge(parts, text, num=False):
    exact = all(p is not None and p.exact for p in parts)
    origin, envs = set(), []
    for p in parts:
        if p is not None:
            origin |= p.origin
            envs.extend(p.envs)
    return FV(text, exact, origin, envs, num)


def as_int(p):
    if p is None:
        return None
    try:
        return int(float(p.t))
    except (TypeError, ValueError, OverflowError):   # float("inf") / float("nan") are numbers, not ints
        return None


def lit_key(fv):
    """a folded literal as a dict key / argument value: 3.0 -> 3; inf, nan and text stay text"""
    if fv.num:
        i = as_int(fv)
        if i is not None:
            return i
    return fv.t


DOTTED_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")
DUNDER_IMPLICIT = {
    "__call__", "__enter__", "__exit__", "__aenter__", "__aexit__", "__iter__", "__next__", "__aiter__",
    "__anext__", "__getattr__", "__getattribute__", "__setattr__", "__delattr__", "__getitem__", "__setitem__",
    "__delitem__", "__len__", "__contains__", "__del__", "__repr__", "__str__", "__format__", "__eq__", "__ne__",
    "__hash__", "__lt__", "__le__", "__gt__", "__ge__", "__bool__", "__await__", "__post_init__",
    "__init_subclass__", "__set_name__", "__get__", "__set__", "__delete__", "__reduce__", "__reduce_ex__",
    "__getstate__", "__setstate__", "__copy__", "__deepcopy__", "__add__", "__sub__", "__mul__", "__truediv__",
    "__missing__", "__new__", "__dir__", "__index__", "__int__", "__float__", "__fspath__", "__class_getitem__"}
MODULE_DUNDERS = {"__getattr__", "__dir__"}


def tag_kind(t):
    return t.split("@", 1)[0].split(":", 1)[0]


def terminates(stmts):
    return bool(stmts) and isinstance(stmts[-1], (ast.Return, ast.Raise))


STR_TRANSFORMS = {"lower", "upper", "strip", "lstrip", "rstrip", "casefold", "title"}


def apply_tr(v, tr):
    for t in tr:
        if t == "str":
            v = str(v)
        elif isinstance(v, str):
            v = getattr(v, t)()
    return v


def unwrap_tr(expr):
    """name.strip().lower() / str(name) -> (Name node or other, transforms in application order)."""
    tr, e = [], expr
    while True:
        if (isinstance(e, ast.Call) and not e.args and not e.keywords and isinstance(e.func, ast.Attribute)
                and e.func.attr in STR_TRANSFORMS):
            tr.append(e.func.attr)
            e = e.func.value
        elif isinstance(e, ast.Call) and len(e.args) == 1 and isinstance(e.func, ast.Name) and e.func.id == "str":
            tr.append("str")
            e = e.args[0]
        else:
            return e, tuple(reversed(tr))


def registry_dict(value):
    """{"scrfd": ScrfdDetector, ...} - string/int keys mapping to names."""
    return (isinstance(value, ast.Dict) and value.keys
            and all(isinstance(k, ast.Constant) and isinstance(k.value, (str, int)) for k in value.keys)
            and all(isinstance(v, (ast.Name, ast.Attribute)) for v in value.values))


def assigned_names(stmts):
    out = set()
    for st in stmts:
        for n in ast.walk(st):
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
                out.add(n.id)
    return out


def fn_param_names(fn, is_method):
    ps = [a.arg for a in fn.args.posonlyargs + fn.args.args]
    if is_method and "staticmethod" not in {dotted(d) for d in fn.decorator_list} and ps:
        ps = ps[1:]
    return ps


SPECIAL_CONST = {"os.sep": FV("/"), "os.path.sep": FV("/"), "os.curdir": FV("."), "os.pardir": FV("..")}


# =============================================================== modules
class Mod:
    def __init__(self, path, rel, name, base, kind, is_entry=False):
        self.path = path
        self.rel = rel
        self.name = name
        self.base = base
        self.kind = kind            # entry | local | exec | spec | unreached
        self.is_entry = is_entry
        self.is_init = path.name == "__init__.py"
        if is_entry or kind in ("exec", "spec"):
            self.package = ""
        else:
            self.package = name if self.is_init else name.rpartition(".")[0]
        self.abs_posix = path.resolve().as_posix()
        self.tree = None
        self.aliases = {}
        self.consts = {}
        self.docstrings = set()
        self.parse_error = None
        self.req = "yes" if is_entry else "no"
        self.visited = False
        self.unreached = False
        self.names = {name}


def collect_aliases(tree):
    al = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                if a.asname:
                    al[a.asname] = a.name
                else:
                    top = a.name.split(".")[0]
                    al.setdefault(top, top)
        elif isinstance(n, ast.ImportFrom):
            base = "." * (n.level or 0) + (n.module or "")
            for a in n.names:
                if a.name == "*":
                    continue
                full = base + a.name if (not base or base.endswith(".")) else base + "." + a.name
                al[a.asname or a.name] = full
    return al


def collect_consts(body, out):
    for st in body:
        if isinstance(st, ast.Assign):
            for t in st.targets:
                if isinstance(t, ast.Name):
                    out[t.id] = st.value
        elif isinstance(st, ast.AnnAssign) and isinstance(st.target, ast.Name) and st.value is not None:
            out[st.target.id] = st.value
        elif isinstance(st, ast.If) or isinstance(st, ast.Try) or type(st).__name__ == "TryStar":
            collect_consts(st.body, out)
            for h in getattr(st, "handlers", []):
                collect_consts(h.body, out)
            collect_consts(getattr(st, "orelse", []), out)


def collect_docstrings(tree):
    ids = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant) and isinstance(n.value.value, str):
            ids.add(id(n.value))
    return ids


# =============================================================== folding
class Folder:
    def __init__(self, mapper, mod, scopes=None):
        self.m = mapper
        self.mod = mod
        self.scopes = scopes if scopes is not None else [mod.consts]
        self.active = set()

    def canon(self, node):
        return self.m.canon(self.mod, node)

    def fold(self, node, d=0):
        if node is None or d > 30:
            return None
        try:
            return self._fold(node, d)
        except RecursionError:
            return None

    def _fold(self, node, d):
        if isinstance(node, ast.Constant):
            v = node.value
            if isinstance(v, str):
                return FV(v)
            if isinstance(v, bool) or v is None:
                return None
            if isinstance(v, (int, float)):
                return FV(str(v), num=True)
            return None
        if isinstance(node, ast.JoinedStr):
            parts = []
            for v in node.values:
                if isinstance(v, ast.Constant):
                    parts.append(FV(str(v.value)))
                elif isinstance(v, ast.FormattedValue):
                    parts.append(self.fold(v.value, d + 1))
                else:
                    parts.append(None)
            return merge(parts, "".join(fvt(p) for p in parts))
        if isinstance(node, ast.BinOp):
            left, right = self.fold(node.left, d + 1), self.fold(node.right, d + 1)
            ops = {ast.Mult: lambda a, b: a * b, ast.Add: lambda a, b: a + b,
                   ast.Sub: lambda a, b: a - b, ast.FloorDiv: lambda a, b: a // b if b else 0}
            if type(node.op) in ops and left is not None and right is not None and left.num and right.num:
                a, b = as_int(left), as_int(right)
                if a is not None and b is not None:
                    return merge([left, right], str(ops[type(node.op)](a, b)), num=True)
            if isinstance(node.op, ast.Add):
                if left is None and right is None:
                    return None
                return merge([left, right], fvt(left) + fvt(right))
            if isinstance(node.op, ast.Div):
                if left is None:
                    return None
                return merge([left, right], posixpath.join(left.t, fvt(right)))
            if isinstance(node.op, ast.Mod) and left is not None:
                return FV(re.sub(r"%[-+ #0-9.]*[sdrifx]", PLACE, left.t), False, left.origin, left.envs)
            return None
        if isinstance(node, ast.Name):
            return self.fold_name(node.id, d)
        if isinstance(node, ast.Attribute):
            return self.fold_attr(node, d)
        if isinstance(node, ast.Call):
            return self.fold_call(node, d)
        if isinstance(node, ast.Subscript):
            return self.fold_subscript(node, d)
        if isinstance(node, ast.IfExp):
            # int(SRC) if SRC.isdigit() else SRC: when only one side folds, it is the likely value (not exact)
            a, b = self.fold(node.body, d + 1), self.fold(node.orelse, d + 1)
            if a is not None and b is not None:
                return a if (a.t, a.exact) == (b.t, b.exact) else None
            p = a if a is not None else b
            return FV(p.t, False, p.origin, p.envs, p.num) if p is not None else None
        return None

    def fold_name(self, n, d):
        if n == "__file__":
            return FV(self.mod.abs_posix, True, {"file"})
        if n == "__name__":
            return FV(self.mod.name)
        canon = self.mod.aliases.get(n)
        if canon in SPECIAL_CONST:
            return SPECIAL_CONST[canon]
        for scope in reversed(self.scopes):
            if n in scope:
                if isinstance(scope[n], FV):  # parameter bound to a caller's value
                    return scope[n]
                key = (id(scope), n)
                if key in self.active:
                    return None
                self.active.add(key)
                try:
                    return self.fold(scope[n], d + 1)
                finally:
                    self.active.discard(key)
        if canon and "." in canon.strip("."):
            modname, _, attr = canon.rpartition(".")
            other = self.m.lookup_module(modname, self.mod)
            if other is not None and attr in other.consts:
                return Folder(self.m, other).fold(other.consts[attr], d + 1)
        return None

    def fold_attr(self, node, d):
        canon = self.canon(node)
        if canon in SPECIAL_CONST:
            return SPECIAL_CONST[canon]
        if node.attr == "parent":
            b = self.fold(node.value, d + 1)
            if b is None:
                return None
            return FV(posixpath.dirname(b.t.rstrip("/")), b.exact, b.origin, b.envs)
        if isinstance(node.value, ast.Name):
            base = self.mod.aliases.get(node.value.id)
            if base:
                other = self.m.lookup_module(base, self.mod)
                if other is not None and node.attr in other.consts:
                    return Folder(self.m, other).fold(other.consts[node.attr], d + 1)
        return None

    def fold_call(self, node, d):
        canon = self.canon(node.func)
        a = [x for x in node.args if not isinstance(x, ast.Starred)]
        if canon in ("os.path.join", "posixpath.join", "ntpath.join"):
            parts = [self.fold(x, d + 1) for x in a]
            if not parts:
                return None
            return merge(parts, posixpath.join(*[fvt(p) for p in parts]))
        if canon in PATH_FUNCS and a:
            p = self.fold(a[0], d + 1)
            if p is None:
                return None
            return FV(PATH_FUNCS[canon](p.t), p.exact, p.origin, p.envs)
        if canon in PATHLIB_CTORS:
            if not a:
                return FV("<cwd>", False, {"cwd"})
            parts = [self.fold(x, d + 1) for x in a]
            return merge(parts, posixpath.join(*[fvt(p) for p in parts]))
        if canon in ("str", "os.fspath", "os.fsdecode"):
            return self.fold(a[0], d + 1) if a else None
        if canon in ("int", "float") and a:
            p = self.fold(a[0], d + 1)
            if p is not None and as_int(p) is not None:
                return FV(p.t, p.exact, p.origin, p.envs, num=True)
            return None
        if canon in ("os.getenv", "os.environ.get"):
            nm = self.fold(a[0], d + 1) if a else None
            name = nm.t if nm is not None and nm.exact else "?"
            dflt = a[1] if len(a) > 1 else kwarg(node, "default")
            if dflt is not None:
                dv = self.fold(dflt, d + 1)
                if dv is None:
                    return FV(f"<env:{name}>", False, {"env"}, (name,))
                return FV(dv.t, False, dv.origin | {"env"}, dv.envs + (name,), dv.num)
            return FV(f"<env:{name}>", False, {"env"}, (name,))
        if canon in ("os.getcwd", "pathlib.Path.cwd"):
            return FV("<cwd>", False, {"cwd"})
        if canon == "pathlib.Path.home":
            return FV("~", False, {"home"})
        if canon in RESOURCE_FILES and a:
            p = self.fold(a[0], d + 1)
            if p is None or not p.exact:
                return None
            target = self.m.lookup_module(p.t, self.mod)
            if target is None:
                return None
            self.m.resource_links.append((self.mod.rel, target.rel))
            return FV(target.path.parent.resolve().as_posix(), True, {"resources"})
        if isinstance(node.func, ast.Attribute):
            meth = node.func.attr
            if meth == "joinpath":
                b = self.fold(node.func.value, d + 1)
                if b is None:
                    return None
                parts = [self.fold(x, d + 1) for x in a]
                return merge([b] + parts, posixpath.join(b.t, *[fvt(p) for p in parts]))
            if meth in ("resolve", "absolute", "expanduser", "as_posix", "strip", "__fspath__"):
                return self.fold(node.func.value, d + 1)
            if meth == "format":
                b = self.fold(node.func.value, d + 1)
                if b is None:
                    return None
                return FV(re.sub(r"\{[^{}]*\}", PLACE, b.t), False, b.origin, b.envs)
            if meth == "replace" and len(a) == 2:
                b, x, y = self.fold(node.func.value, d + 1), self.fold(a[0], d + 1), self.fold(a[1], d + 1)
                if b is not None and x is not None and y is not None and x.exact:
                    return merge([b, y], b.t.replace(x.t, y.t))
                return None
            if (meth == "join" and isinstance(node.func.value, ast.Constant)
                    and isinstance(node.func.value.value, str) and a and isinstance(a[0], (ast.List, ast.Tuple))):
                parts = [self.fold(x, d + 1) for x in a[0].elts]
                return merge(parts, node.func.value.value.join(fvt(p) for p in parts))
        return None

    def node_of(self, expr, d=0):
        """(Folder, AST node) a Name / module.CONST refers to, following simple aliases."""
        if d > 5:
            return None
        if isinstance(expr, ast.Name):
            for scope in reversed(self.scopes):
                if expr.id in scope:
                    v = scope[expr.id]
                    if isinstance(v, FV):
                        return None
                    return (self, v) if not isinstance(v, ast.Name) else self.node_of(v, d + 1)
            canon = self.mod.aliases.get(expr.id)
            if canon and "." in canon.strip("."):
                modname, _, attr = canon.rpartition(".")
                other = self.m.lookup_module(modname, self.mod)
                if other is not None and attr in other.consts:
                    return Folder(self.m, other), other.consts[attr]
            return None
        if isinstance(expr, ast.Attribute) and isinstance(expr.value, ast.Name):
            base = self.mod.aliases.get(expr.value.id)
            if base:
                other = self.m.lookup_module(base, self.mod)
                if other is not None and expr.attr in other.consts:
                    return Folder(self.m, other), other.consts[expr.attr]
        return None

    def fold_subscript(self, node, d):
        sl = node.slice
        if type(sl).__name__ == "Index":  # python 3.8
            sl = sl.value
        v = node.value
        if self.canon(v) == "os.environ":
            k = self.fold(sl, d + 1)
            name = k.t if k is not None and k.exact else "?"
            return FV(f"<env:{name}>", False, {"env"}, (name,))
        ref = self.node_of(v)
        if ref is not None and isinstance(ref[1], ast.Dict):  # CONFIG["host"] with CONFIG = {...}
            k = self.fold(sl, d + 1)
            if k is not None and k.exact:
                for kn, vn in zip(ref[1].keys, ref[1].values):
                    if isinstance(kn, ast.Constant) and str(kn.value) == k.t:
                        return ref[0].fold(vn, d + 1)
            return None
        if (isinstance(v, ast.Call) and isinstance(v.func, ast.Attribute)
                and v.func.attr in ("rsplit", "split") and v.args):
            idx = None
            if isinstance(sl, ast.Constant) and isinstance(sl.value, int):
                idx = sl.value
            elif (isinstance(sl, ast.UnaryOp) and isinstance(sl.op, ast.USub)
                  and isinstance(sl.operand, ast.Constant)):
                idx = -sl.operand.value
            b = self.fold(v.func.value, d + 1)
            s = self.fold(v.args[0], d + 1)
            mx = as_int(self.fold(v.args[1], d + 1)) if len(v.args) > 1 else -1
            if idx is None or b is None or s is None or not s.t:
                return None
            pieces = b.t.rsplit(s.t, mx) if v.func.attr == "rsplit" else b.t.split(s.t, mx)
            try:
                return FV(pieces[idx], b.exact and s.exact, b.origin, b.envs)
            except IndexError:
                return None
        return None


# =============================================================== visitor
class ModuleVisitor(ast.NodeVisitor):
    def __init__(self, mapper, mod, follow=True, unreached=False):
        self.m = mapper
        self.mod = mod
        self.follow = follow
        self.ctx = ["unreached"] if unreached else []
        self.scopes = [mod.consts]
        self.consumed = set()        # constants / path builders already accounted for
        self.handled_calls = set()   # call nodes whose handler must not fire
        self.folder = Folder(mapper, mod, self.scopes)
        self.fn_depth = 0
        self.assign_names = {}
        self.cpu_fallback = set()
        self.fn_stack = []           # enclosing function nodes
        self.unused_vals = set()     # call nodes whose result is assigned to a local that is never read
        self.bound = False           # re-visit of one function with parameters bound to callers' values

    def run(self):
        self.visit_stmts(self.mod.tree.body)

    def visit_stmts(self, stmts):
        """Visit a statement list; code after `if cond: return` only runs when cond is false."""
        pushed = 0
        for st in stmts:
            self.visit(st)
            if isinstance(st, ast.If) and not st.orelse and terminates(st.body):
                bt, _ = self.if_tags(st.test)
                if bt and bt.startswith("branch@"):
                    self.ctx.append(f"branch@{id(st.test)}:0")
                    pushed += 1
        for _ in range(pushed):
            self.ctx.pop()

    def visit_Match(self, node):
        self.visit(node.subject)
        for case in node.cases:
            self.ctx.append(f"branch@{id(case)}:1")
            if case.guard is not None:
                self.visit(case.guard)
            self.visit_stmts(case.body)
            self.ctx.pop()

    def visit_For(self, node):
        self.visit(node.target)
        self.visit(node.iter)
        self.visit_stmts(node.body)
        self.visit_stmts(node.orelse)

    visit_AsyncFor = visit_For

    def visit_While(self, node):
        self.visit(node.test)
        self.visit_stmts(node.body)
        self.visit_stmts(node.orelse)

    def visit_With(self, node):
        for it in node.items:
            self.visit(it)
        self.visit_stmts(node.body)

    visit_AsyncWith = visit_With

    # ---------------------------------------------------------- basics
    def canon(self, node):
        return self.m.canon(self.mod, node)

    def fold(self, node):
        return self.folder.fold(node)

    def consume(self, node):
        if node is not None:
            for n in ast.walk(node):
                self.consumed.add(id(n))

    def creq(self, import_site=False):
        r = "yes"
        for t in self.ctx:
            k = tag_kind(t)
            if k in ("type_checking", "dead", "unreached"):
                return "no"
            if k == "main_guard":
                if not self.mod.is_entry:
                    return "no"
            elif k in ("function", "branch", "platform"):
                r = "conditional"
            elif k in ("optional_import", "fallback_import") and import_site:
                r = "conditional"
        return r

    def container(self):
        """(qualified function name the current code sits in, nested?) - None = module level."""
        cls, qual, nested = None, None, False
        for t in self.ctx:
            if t.startswith("class:"):
                if qual is None and cls is None:
                    cls = t[6:]
                else:
                    nested = True
            elif t.startswith("function:"):
                name = t[9:]
                if qual is None and name != "<lambda>":
                    qual = f"{cls}.{name}" if cls else name
                else:
                    nested = True
        return qual, nested

    def local_req(self, import_site=False):
        """Requirement from branches/guards only - the enclosing function is resolved later by the call graph."""
        r = "yes"
        for t in self.ctx:
            k = tag_kind(t)
            if k in ("type_checking", "dead", "unreached"):
                return "no"
            if k == "main_guard":
                if not self.mod.is_entry:
                    return "no"
            elif k in ("branch", "platform"):
                r = "conditional"
            elif k in ("optional_import", "fallback_import") and import_site:
                r = "conditional"
        if self.container()[1]:
            r = req_min(r, "conditional")
        return r

    def guarded(self):
        """Inside a try whose handler catches OSError (or broader): a failure here has a fallback."""
        for t in self.ctx:
            if t.startswith("guard:") and set(t[6:].split(",")) & {"*", "OSError", "IOError", "Exception",
                                                                   "BaseException", "FileNotFoundError"}:
                return True
        return False

    def resolve_node(self, node, hops=3):
        """The AST a local name was assigned (cmd = [...]; x = find_library(...)), else the node itself."""
        while isinstance(node, ast.Name) and hops > 0:
            nxt = None
            for scope in reversed(self.scopes):
                if node.id in scope:
                    nxt = scope[node.id]
                    break
            if nxt is None or isinstance(nxt, FV):
                return node
            node, hops = nxt, hops - 1
        return node

    def fact(self, node, category, subject, mechanism, evidence="static", confidence="high",
             detail="", import_site=False, cap=None, **extra):
        meta = {"_q": self.container()[0], "_t": list(self.ctx), "_imp": import_site}
        if cap:
            meta["_cap"] = cap   # this site can never be more required than `cap` (e.g. it has a fallback)
        return self.m.add_fact(self.mod, node, category, subject, mechanism, self.creq(import_site),
                               evidence, confidence, detail, site_meta=meta, context=list(self.ctx), **extra)

    def edge(self, m, kind, cap=None):
        return self.m.add_mod_edge(self.mod, m, cap or "yes", kind, self.container()[0], list(self.ctx))

    def visit_block(self, stmts, *tags):
        tags = [t for t in tags if t]
        self.ctx.extend(tags)
        self.visit_stmts(stmts)
        for _ in tags:
            self.ctx.pop()

    # ---------------------------------------------------------- structure
    def if_tags(self, test):
        if (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or self.canon(test) == "typing.TYPE_CHECKING":
            return "type_checking", None
        if isinstance(test, ast.Constant):
            return ("dead", None) if not test.value else (None, "dead")
        if (isinstance(test, ast.Compare) and isinstance(test.left, ast.Name)
                and test.left.id == "__name__"):
            return "main_guard", "branch"
        src = unparse(test, 60)
        if "sys.platform" in src or "os.name" in src or "platform.system" in src:
            return "platform:" + src, "platform:not " + src
        return f"branch@{id(test)}:1", f"branch@{id(test)}:0"

    def visit_If(self, node):
        self.visit(node.test)
        body_tag, else_tag = self.if_tags(node.test)
        self.visit_block(node.body, body_tag)
        self.visit_block(node.orelse, else_tag)

    @staticmethod
    def catches_import(h):
        t = h.type
        if t is None:
            return True
        names = [t] if not isinstance(t, ast.Tuple) else list(t.elts)
        for n in names:
            if isinstance(n, ast.Name) and n.id in ("ImportError", "ModuleNotFoundError", "Exception",
                                                     "BaseException"):
                return True
        return False

    def visit_Try(self, node):
        imp = any(self.catches_import(h) for h in node.handlers)
        caught = set()
        for h in node.handlers:
            ts = [] if h.type is None else (list(h.type.elts) if isinstance(h.type, ast.Tuple) else [h.type])
            caught |= {"*"} if h.type is None else {dotted(t) or "?" for t in ts}
        guard = ("guard:" + ",".join(sorted(caught))) if caught else None
        self.visit_block(node.body, "optional_import" if imp else None, guard)
        for h in node.handlers:
            if h.type is not None:
                self.visit(h.type)
            self.visit_block(h.body, "branch", "fallback_import" if self.catches_import(h) else None)
        self.visit_block(node.orelse)
        self.visit_block(node.finalbody)

    visit_TryStar = visit_Try

    def visit_FunctionDef(self, node):
        for dec in node.decorator_list:
            self.visit(dec)
        scope = {}
        pos = node.args.posonlyargs + node.args.args
        for a, dflt in zip(pos[len(pos) - len(node.args.defaults):], node.args.defaults):
            scope[a.arg] = dflt
        for a, dflt in zip(node.args.kwonlyargs, node.args.kw_defaults):
            if dflt is not None:
                scope[a.arg] = dflt
        self.scopes.append(scope)
        self.ctx.append("function:" + node.name)
        self.fn_depth += 1
        self.fn_stack.append(node)
        self.visit_stmts(node.body)
        self.fn_stack.pop()
        self.fn_depth -= 1
        self.ctx.pop()
        self.scopes.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Lambda(self, node):
        self.ctx.append("function:<lambda>")
        self.visit(node.body)
        self.ctx.pop()

    def visit_ClassDef(self, node):
        for x in node.decorator_list + node.bases:
            self.visit(x)
        self.ctx.append("class:" + node.name)
        self.visit_stmts(node.body)
        self.ctx.pop()

    def visit_Raise(self, node):
        self.consume(node.exc)
        self.generic_visit(node)

    def visit_Assert(self, node):
        if node.msg is not None:
            self.consume(node.msg)   # assert os.path.exists(P), f"Missing: {P}" - a message, not a path
        self.generic_visit(node)

    # ---------------------------------------------------------- imports
    def import_mech(self, level, aliased, is_from, third_party=False):
        if "type_checking" in self.ctx:
            return "type_checking_only"
        if "optional_import" in self.ctx or "fallback_import" in self.ctx:
            return "try_except_optional"
        if any(t.startswith("function:") for t in self.ctx):
            return "lazy_in_function"
        if self.mod.kind == "exec":
            return "exec_file"
        if level > 0:
            return "relative_import"
        if third_party and self.mod.is_init:
            return "init_side_effect"
        if aliased:
            return "alias"
        return "from_import" if is_from else "import_stmt"

    def visit_Import(self, node):
        for a in node.names:
            chain, base = self.m.resolve(a.name)
            if chain:
                self.record_local(node, a.name, chain, base, 0, bool(a.asname), False)
            else:
                self.record_external(node, a.name, bool(a.asname), False)

    def visit_ImportFrom(self, node):
        modname = node.module or ""
        level = node.level or 0
        if modname == "__future__":
            return
        if level:
            base_pkg = self.m.rel_base(self.mod, level)
            if base_pkg is None:
                self.fact(node, "dangling_reference", "." * level + modname, "relative_import",
                          detail="relative import beyond the top-level package: ImportError at runtime",
                          import_site=True)
                return
            full_base = ".".join(x for x in (base_pkg, modname) if x)
            search = [self.mod.base]
        else:
            full_base, search = modname, None
        for a in node.names:
            if a.name != "*":
                sub = f"{full_base}.{a.name}" if full_base else a.name
                chain, base = self.m.resolve(sub, search)
                if chain:
                    self.record_local(node, sub, chain, base, level, bool(a.asname), True)
                    continue
            chain, base = self.m.resolve(full_base, search) if full_base else (None, None)
            if chain:
                self.record_local(node, full_base, chain, base, level, bool(a.asname), True)
            elif level:
                self.fact(node, "dangling_reference", "." * level + modname, "relative_import",
                          detail=f"relative import target not found: {full_base}", import_site=True)
                break
            else:
                self.record_external(node, full_base, bool(a.asname), True)
                break

    def record_local(self, node, dotted_name, chain, base, level, aliased, is_from):
        ereq = self.creq(True)
        for kind, p, name in chain:
            if kind == "ns":
                continue
            m = self.m.get_or_load(p, name, base, "local")
            if m is self.mod:
                continue
            self.edge(m, "import")
            if self.follow:
                self.m.enqueue(m)
        last_kind, last_p, _ = chain[-1]
        resolved = self.m.rel(last_p) + ("/" if last_kind == "ns" else "")
        mech = self.import_mech(level, aliased, is_from)
        detail = []
        if base in self.m.inserted:
            mech = "sys_path_insert"
            detail.append(f"resolved via sys.path entry added at {self.m.inserted[base]}")
        if last_kind == "ns":
            detail.append("namespace package (folder without __init__.py)")
        self.fact(node, "local_module", dotted_name, mech, detail="; ".join(detail),
                  resolved_path=resolved, import_site=True)
        if level == 0:
            self.check_sibling_shadow(node, dotted_name, last_p)

    def check_sibling_shadow(self, node, dotted_name, resolved_p):
        top = dotted_name.split(".")[0]
        here = self.mod.path.parent.resolve()
        if here in self.m.search_paths:
            return
        for cand in (here / (top + ".py"), here / top):
            ok = self.m.isfile(cand) if cand.suffix == ".py" else self.m.isdir(cand)
            if ok and cand.resolve() != self.mod.path.resolve():
                rp = resolved_p.resolve()
                if rp == cand.resolve() or str(rp).startswith(str(cand.resolve()) + os.sep):
                    return
                self.fact(node, "shadowing", self.m.rel(resolved_p), "import_stmt",
                          detail=(f"'import {top}' resolves to {self.m.rel(resolved_p)} (sys.path order), "
                                  f"NOT the sibling {self.m.rel(cand)} next to this file"),
                          import_site=True, resolved_path=self.m.rel(resolved_p),
                          ignored=self.m.rel(cand))
                return

    def record_external(self, node, full, aliased, is_from):
        top = full.split(".")[0]
        if not top:
            return
        ci = self.m.case_mismatch(top)
        if ci:
            self.fact(node, "dangling_reference", full, "import_stmt",
                      detail=f"case mismatch: only '{ci}' exists - works on Windows, ImportError on Linux",
                      import_site=True)
        if top in STDLIB:
            self.m.ext_edges.append((self.mod.rel, "std:" + top, self.creq(True)))
            if top in NOTABLE_STDLIB:
                self.fact(node, "stdlib_import", top, self.import_mech(0, aliased, is_from),
                          detail=NOTABLE_STDLIB[top], import_site=True, import_name=top)
            if top in GUI_IMPORTS:
                self.fact(node, "gui_usage", top, "gui_import", detail="GUI toolkit import: needs a display",
                          import_site=True)
            return
        sib = self.mod.path.parent / (top + ".py")
        if (self.m.isfile(sib) and sib.resolve() != self.mod.path.resolve()
                and self.mod.path.parent.resolve() not in self.m.search_paths):
            self.fact(node, "dangling_reference", full, "import_stmt",
                      detail=(f"sibling {self.m.rel(sib)} is NOT importable as '{top}' (its folder is not "
                              f"on sys.path) - Python 3 resolves '{top}' elsewhere"), import_site=True)
        dist, src = self.m.dist_for(top, full)
        self.m.third_party_tops.add(top)
        mech = self.import_mech(0, aliased, is_from, third_party=True)
        self.fact(node, "third_party_import", dist, mech,
                  detail=("import name differs from package name" if norm_dist(top) != dist else ""),
                  import_site=True, import_name=top, dist_source=src)
        self.m.ext_edges.append((self.mod.rel, "pkg:" + dist, self.creq(True)))
        if top in GUI_IMPORTS:
            self.fact(node, "gui_usage", top, "gui_import", detail="GUI toolkit import: needs a display",
                      import_site=True)

    # ---------------------------------------------------------- calls
    def visit_Call(self, node):
        if id(node) not in self.handled_calls:
            canon = self.canon(node.func)
            meth = node.func.attr if isinstance(node.func, ast.Attribute) else None
            try:
                self.dispatch(node, canon, meth)
            except Exception as e:  # never crash on unusual code
                self.m.warn(f"{self.mod.rel}:{getattr(node, 'lineno', '?')}: {type(e).__name__}: {e}")
            if canon not in ALL_KNOWN and "unreached" not in self.ctx:
                self.record_call(node)
        self.generic_visit(node)

    def record_call(self, node):
        """Fold the arguments of a call to (possibly) project code: bound to the callee's parameters later."""
        if not isinstance(node.func, (ast.Name, ast.Attribute)):
            return
        if any(isinstance(a, ast.Starred) for a in node.args) or any(k.arg is None for k in node.keywords):
            args, kws = None, None   # *args / **kwargs: parameter values unknown
        else:
            args = [self.fold(a) for a in node.args]
            kws = {k.arg: self.fold(k.value) for k in node.keywords}
        cls = next((t[6:] for t in self.ctx if t.startswith("class:")), None)
        self.m.call_folds.append({"mod": self.mod.rel, "func": node.func, "cls": cls, "args": args, "kws": kws,
                                  "from": (self.mod.rel, self.container()[0]), "bound": self.bound})

    def dispatch(self, node, c, meth):
        if c in MESSAGE_FUNCS or (meth in MESSAGE_METHODS and c and "log" in c.lower()):
            for a in node.args:
                self.consume(a)
            for k in node.keywords:
                self.consume(k.value)
            return
        if meth == "add_argument":
            self.consume(kwarg(node, "help"))
        if c in ENV_READ:
            self.h_env(node, c)
        elif c in DYN_IMPORT:
            self.h_dynimport(node, c)
        elif c in SPEC_LOAD:
            self.h_spec(node, c)
        elif c in DISCOVERY:
            self.h_discovery(node)
        elif c in ("exec", "execfile"):
            self.h_exec(node)
        elif c in SYSPATH:
            self.h_syspath(node, SYSPATH[c])
        elif c in RESOURCE_CALLS:
            self.h_resource_call(node, c)
        elif c in OPEN_CALLS:
            self.h_open(node)
        elif c in IO_CALLS:
            self.h_io(node, c)
        elif c == "logging.basicConfig":
            fn = kwarg(node, "filename")
            if fn is not None:
                self.path_fact(node, fn, "write", "python_io")
        if c in NET_CALLS:
            self.h_net(node, c)
        if c in LISTEN_CALLS:
            self.h_listen_fn(node, c)
        if c in SUBPROC:
            self.h_subproc(node, c)
        if c in NATIVE:
            self.h_native(node, c)
        if c in GUI_CALLS:
            self.fact(node, "gui_usage", c, "gui_call",
                      detail="needs a display and GUI-enabled OpenCV (not the headless build)")
        if c in GPU_PROBES:
            self.fact(node, "gpu_usage", f"{c}()", "gpu_probe",
                      detail="GPU probe: the code checks for a GPU, so it has a CPU path - GPU optional")
        elif c and (c.startswith("torch.cuda.") or c.startswith("cv2.cuda.")):
            self.fact(node, "gpu_usage", f"{c}()", "gpu_call", detail=c)
        if c == "torch.device":
            v = self.fold(node.args[0]) if node.args else None
            if v is not None and "cuda" in v.t:
                self.fact(node, "gpu_usage", "torch.device(cuda)", "gpu_call", detail=unparse(node))
                self.gpu_index_consts(node.args[0])
        if c in MP_CALLS:
            self.h_mp(node, c)
        if c in SHM_CALLS:
            self.h_shm(node, c)
        if c in THREAD_CALLS:
            self.fact(node, "thread_config", c, "thread_call",
                      detail="thread-pool size set in code (oversubscription risk with many processes)")
        if c in HWID_CALLS:
            self.h_hwid(node, c)
        if c in TZ_CALLS:
            v = self.fold(node.args[0]) if node.args else None
            self.fact(node, "os_data", f"tzdata:{fvt(v)}", "timezone",
                      detail=f"timezone '{fvt(v)}' needs tzdata in the image (apt tzdata or pip tzdata)")
        if c in FONT_CALLS:
            v = self.fold(arg_or_kw(node, 0, "font"))
            fb = self.guarded()
            self.fact(node, "os_data", f"font:{posixpath.basename(fvt(v))}", "font",
                      detail=("font file must exist in the image (e.g. fonts-dejavu-core)"
                              + ("; OSError is caught: the code falls back to another font" if fb
                                 else " - no fallback: missing font raises OSError")),
                      cap="conditional" if fb else None)
        if c in TMP_CALLS:
            self.fact(node, "os_data", "writable-tmp", "tempfile", detail="needs a writable temp dir (/tmp)")
        if c in PICKLE_CALLS:
            self.h_pickle(node, c)
        if c and c.startswith("torchvision.models.") and (kwarg(node, "pretrained") is not None
                                                           or kwarg(node, "weights") is not None):
            self.fact(node, "runtime_download", f"torchvision:{c.rsplit('.', 1)[-1]}", "hub_download",
                      detail="pretrained weights downloaded into TORCH_HOME at runtime - bake or mount a cache")
        if meth and c not in ALL_KNOWN:
            self.h_method(node, meth, c)
        self.h_kwargs(node, c)
        if (c in PATH_BUILDERS or meth == "joinpath") and id(node) not in self.consumed:
            fv = self.fold(node)
            if fv is not None and posixpath.splitext(fv.t)[1].lower() in (MODEL_EXT | CONFIG_EXT | KEY_EXT):
                self.path_fact(node, None, "ref", "python_io", fv=fv)
                self.consume(node)

    # ---- env
    def env_fact(self, node, name, mech, dv, cap=None):
        secret = secretish(name)
        detail = []
        default = None
        if dv is not None and dv.t not in ("", PLACE):
            default = None if secret else dv.t
            detail.append("default in code: " + (mask(dv.t) if secret else dv.t))
            if secret:
                self.fact(node, "secret", name, "env_default_secret",
                          detail=f"secret-looking env var has a hardcoded default ({mask(dv.t)})")
        if cap:
            detail.append("the value read here is never used by the code - it only matters to libraries "
                          "that read the variable themselves")
        self.fact(node, "env_var", name, mech, detail="; ".join(detail), default=default, cap=cap)
        if name in THREAD_ENVS:
            self.fact(node, "thread_config", name, mech, cap=cap,
                      detail="thread-pool env var: set to 1 per process when running many processes")
        if name in GPU_ENVS:
            self.fact(node, "gpu_usage", name, mech, cap=cap,
                      detail="GPU selection env var (only matters when a GPU is present)")

    def h_env(self, node, c):
        nm_node = arg_or_kw(node, 0, "key")
        nm = self.fold(nm_node)
        self.consume(nm_node)
        if nm is None or not nm.exact:
            self.fact(node, "env_var", "<unresolved>", "env_dynamic_name", "unresolved", detail=unparse(node))
            return
        dflt = arg_or_kw(node, 1, "default")
        if c == "os.environ.setdefault":
            mech = "env_set_default"
        elif dflt is not None:
            mech = "env_default"
        else:
            mech = "env_optional"
        self.env_fact(node, nm.t, mech, self.fold(dflt) if dflt is not None else None,
                      cap="conditional" if id(node) in self.unused_vals else None)

    def visit_Subscript(self, node):
        if self.canon(node.value) == "os.environ":
            k = self.fold(node.slice)
            self.consume(node.slice)
            if k is not None and k.exact:
                if isinstance(node.ctx, ast.Store):
                    self.fact(node, "env_var", k.t, "env_set", detail="set by the code at runtime")
                    if k.t in THREAD_ENVS:
                        self.fact(node, "thread_config", k.t, "env_set", detail="thread-pool env var set in code")
                else:
                    self.env_fact(node, k.t, "env_required", None)
            else:
                self.fact(node, "env_var", "<unresolved>", "env_dynamic_name", "unresolved", detail=unparse(node))
        self.generic_visit(node)

    def visit_Compare(self, node):
        if (len(node.ops) == 1 and isinstance(node.ops[0], (ast.In, ast.NotIn))
                and self.canon(node.comparators[0]) == "os.environ"):
            k = self.fold(node.left)
            if k is not None and k.exact:
                self.env_fact(node, k.t, "env_check", None)
                self.consume(node.left)
        self.generic_visit(node)

    # ---- dynamic imports
    def absolutize(self, name, pkg):
        if not name.startswith("."):
            return name
        level = len(name) - len(name.lstrip("."))
        rest = name[level:]
        parts = pkg.split(".") if pkg else []
        keep = parts[: len(parts) - (level - 1)] if level - 1 <= len(parts) else []
        base = ".".join(keep)
        if not rest:
            return base + "."
        return f"{base}.{rest}" if base else rest

    def dyn_target(self, node, dotted_name, mech, evidence, detail, provisional=None, from_hint=False):
        chain, base = self.m.resolve(dotted_name)
        if chain:
            eidx = None
            for kind, p, name in chain:
                if kind == "ns":
                    continue
                m = self.m.get_or_load(p, name, base, "local")
                if m is self.mod:
                    continue
                eidx = self.edge(m, "dynamic", provisional)
                if self.follow:
                    self.m.enqueue(m)
            f = self.fact(node, "dynamic_import", dotted_name, mech, evidence, detail=detail,
                          resolved_path=self.m.rel(chain[-1][1]), import_site=True)
            if provisional:
                f["_force"] = provisional
            return f, eidx
        if from_hint:
            f = self.fact(node, "dynamic_import", dotted_name, mech, "static_partial", "low",
                          detail=(detail + "; not a local module - verify this value").strip("; "), import_site=True)
            return f, None
        top = dotted_name.split(".")[0]
        if top in STDLIB:
            f = self.fact(node, "dynamic_import", dotted_name, mech, evidence,
                          detail=(detail + "; standard library").strip("; "), import_site=True)
            return f, None
        dist, _ = self.m.dist_for(top, dotted_name)
        self.m.third_party_tops.add(top)
        f = self.fact(node, "dynamic_import", dotted_name, mech, evidence,
                      detail=(detail + f"; third-party package {dist}").strip("; "),
                      import_site=True, import_name=top, dist=dist)
        return f, None

    def h_dynimport(self, node, c):
        arg = arg_or_kw(node, 0, "name")
        pkg_node = arg_or_kw(node, 1, "package") if c == "importlib.import_module" else None
        in_getattr = self.mod.is_init and "function:__getattr__" in self.ctx
        base_mech = "module_getattr" if in_getattr else (
            "dunder_import" if "__import__" in c else "importlib_literal")
        fv = self.fold(arg)
        self.consume(arg)
        pkg = None
        if pkg_node is not None:
            pv = self.fold(pkg_node)
            pkg = pv.t if pv is not None and pv.exact else None
        if pkg is None:
            pkg = self.mod.package
        site = f"{self.mod.rel}:{node.lineno}"
        if fv is not None and fv.exact:
            self.dyn_target(node, self.absolutize(fv.t, pkg), base_mech, "static", "")
            return
        text = fvt(fv)
        if fv is not None and fv.envs and text.startswith("<env:"):
            vals = []
            for e in fv.envs:
                vals += self.m.hints.get(e, [])
            envs = ",".join(fv.envs)
            vals = [(v, w) for v, w in vals if DOTTED_RE.match(self.absolutize(v, pkg))]
            if vals:
                for v, where in vals:
                    self.dyn_target(node, self.absolutize(v, pkg), "importlib_from_env", "static_partial",
                                    f"module name from env {envs}; value '{v}' found in {where}", from_hint=True)
            else:
                self.fact(node, "dynamic_import", "<unresolved>", "importlib_from_env", "unresolved",
                          detail=f"module name comes from env {envs} at runtime; no value found in deploy files",
                          import_site=True)
                self.m.unresolved_dynamic.append(site)
            return
        prefix = text.split("<", 1)[0] if "<" in text else ""
        if prefix:
            absprefix = self.absolutize(prefix, pkg)
            pkgname, _, leafpre = absprefix.rpartition(".")
            cands = self.m.list_submodules(pkgname, leafpre)
            if cands:
                kind = "getattr" if in_getattr else "fstring"
                mech = "module_getattr" if in_getattr else "importlib_fstring"
                g = {"kind": kind, "package": pkgname, "site": site, "cands": []}
                for dn, leaf in cands:
                    f, eidx = self.dyn_target(node, dn, mech, "static_partial",
                                              f"candidate for {unparse(arg)}", provisional="conditional")
                    if f is not None:
                        g["cands"].append({"leaf": leaf, "fact": f, "edge": eidx})
                self.m.groups.append(g)
                return
        self.fact(node, "dynamic_import", "<unresolved>", base_mech, "unresolved",
                  detail=f"module name computed at runtime: {unparse(arg)}", import_site=True)
        self.m.unresolved_dynamic.append(site)

    def h_spec(self, node, c):
        if c == "runpy.run_path":
            name_fv, path_node = None, arg_or_kw(node, 0, "path_name")
        else:
            name_fv = self.fold(arg_or_kw(node, 0, "name", "fullname"))
            path_node = arg_or_kw(node, 1, "location", "path", "pathname")
        for a in node.args:
            self.consume(a)
        fv = self.fold(path_node)
        if fv is None:
            self.fact(node, "dynamic_import", "<unresolved>", "spec_from_file_location", "unresolved",
                      detail=f"file path computed at runtime: {unparse(path_node)}", import_site=True)
            self.m.unresolved_dynamic.append(f"{self.mod.rel}:{node.lineno}")
            return
        info = self.m.classify_path(fv)
        name = (name_fv.t if name_fv is not None and name_fv.exact
                else posixpath.splitext(posixpath.basename(info["subject"]))[0])
        p = self.m.root / info["subject"] if info["loc"] == "project" and "<" not in info["subject"] else None
        if p is not None and p.is_file():
            m = self.m.get_or_load(p, name, p.parent.resolve(), "spec")
            self.edge(m, "spec")
            if self.follow:
                self.m.enqueue(m)
            self.fact(node, "dynamic_import", name, "spec_from_file_location", resolved_path=info["subject"],
                      detail="module loaded from a file path (outside the normal import system)",
                      import_site=True)
        else:
            self.fact(node, "dynamic_import", name, "spec_from_file_location",
                      "unresolved" if info["evidence"] == "unresolved" else "static_partial",
                      resolved_path=info["subject"], import_site=True,
                      detail=("file not found at the computed path" if info["exists"] is False
                              else "path not fully known statically"))

    def h_discovery(self, node):
        a = arg_or_kw(node, 0, "path")
        self.consume(a)
        pkgname = None
        if isinstance(a, ast.Attribute) and a.attr == "__path__":
            tgt = self.canon(a.value)
            if tgt:
                tgt = self.absolutize(tgt, self.mod.package) if tgt.startswith(".") else tgt
                chain, _ = self.m.resolve(tgt)
                if chain:
                    pkgname = tgt
        else:
            el = a.elts[0] if isinstance(a, (ast.List, ast.Tuple)) and a.elts else a
            fv = self.fold(el)
            if fv is not None:
                info = self.m.classify_path(fv)
                if info["loc"] == "project" and "<" not in info["subject"]:
                    pkgname = self.m.dotted_for_dir((self.m.root / info["subject"]).resolve())
        if not pkgname:
            self.fact(node, "dynamic_import", "<unresolved>", "pkgutil_discovery", "unresolved",
                      detail=f"module discovery over a path unknown statically: {unparse(a)}", import_site=True)
            self.m.unresolved_dynamic.append(f"{self.mod.rel}:{node.lineno}")
            return
        self.m.discovered_pkgs.add(pkgname)
        for dn, _leaf in self.m.list_submodules(pkgname):
            self.dyn_target(node, dn, "pkgutil_discovery", "static_partial",
                            f"auto-discovered in package '{pkgname}' (every module there gets imported)")

    def h_exec(self, node):
        a = node.args[0] if node.args else None
        if a is None:
            return
        roots = [a]
        if isinstance(a, ast.Name):
            for scope in reversed(self.scopes):
                if a.id in scope:
                    roots.append(scope[a.id])
                    break
        path_fv = None
        for r in roots:
            for sub in ast.walk(r):
                if not isinstance(sub, ast.Call):
                    continue
                self.handled_calls.add(id(sub))
                cc = self.canon(sub.func)
                cand = None
                if cc in OPEN_CALLS and sub.args:
                    cand = sub.args[0]
                elif cc == "compile" and len(sub.args) >= 2:
                    cand = sub.args[1]
                elif isinstance(sub.func, ast.Attribute) and sub.func.attr == "read_text":
                    cand = sub.func.value
                if cand is not None and path_fv is None:
                    fv = self.fold(cand)
                    if fv is not None and fv.t.endswith(".py"):
                        path_fv = fv
            self.consume(r)
        if path_fv is None:
            self.fact(node, "dynamic_import", "<exec>", "exec_file", "unresolved",
                      detail="exec() of code not traceable to a file", import_site=True)
            return
        info = self.m.classify_path(path_fv)
        p = self.m.root / info["subject"] if info["loc"] == "project" and "<" not in info["subject"] else None
        if p is not None and p.is_file():
            m = self.m.get_or_load(p, p.stem, p.parent.resolve(), "exec")
            self.edge(m, "exec")
            if self.follow:
                self.m.enqueue(m)
            self.fact(node, "dynamic_import", info["subject"], "exec_file", resolved_path=info["subject"],
                      detail="file executed with exec(): its imports run in this process", import_site=True)
        else:
            self.fact(node, "dynamic_import", info["subject"], "exec_file", "static_partial",
                      detail="exec() target not found at the computed path", import_site=True)

    def h_syspath(self, node, kind):
        if kind == "insert":
            a = node.args[1] if len(node.args) > 1 else None
        elif kind == "extend":
            a = (node.args[0].elts[0] if node.args and isinstance(node.args[0], (ast.List, ast.Tuple))
                 and node.args[0].elts else None)
        else:
            a = node.args[0] if node.args else None
        fv = self.fold(a)
        self.consume(a)
        site = f"{self.mod.rel}:{node.lineno}"
        if fv is None:
            self.m.warn(f"{site}: sys.path change with a path unknown statically ({unparse(a)})")
            return
        info = self.m.classify_path(fv)
        if info["loc"] == "project" and "<" not in info["subject"] and (self.m.root / info["subject"]).is_dir():
            d = (self.m.root / info["subject"]).resolve()
            self.m.add_search_path(d, front=(kind == "insert"), site=site)
            self.m.syspath_sites.append({"dir": info["subject"], "site": site, "kind": kind})
        else:
            self.m.warn(f"{site}: sys.path entry {info['subject']} not found in the project")

    def h_resource_call(self, node, c):
        if c in RESOURCE_FILES:
            return  # folded later through .joinpath()/.read_text()
        pkg = self.fold(arg_or_kw(node, 0, "package", "package_or_requirement"))
        res = self.fold(arg_or_kw(node, 1, "resource", "resource_name"))
        if pkg is not None and pkg.exact and res is not None:
            tgt = self.m.lookup_module(pkg.t, self.mod)
            if tgt is not None:
                self.m.resource_links.append((self.mod.rel, tgt.rel))
                fv = FV(posixpath.join(tgt.path.parent.resolve().as_posix(), res.t), res.exact, {"resources"})
                self.path_fact(node, None, "read", "python_io", fv=fv, mechanism="importlib_resources")
                for x in node.args:
                    self.consume(x)

    # ---- file I/O
    def pathish(self, fv):
        return ("/" in fv.t or bool(fv.origin & {"file", "resources", "cwd", "env", "home"})
                or posixpath.splitext(fv.t)[1] != "")

    def path_fact(self, node, argnode, op, io, fv=None, detail="", mechanism=None, cap=None):
        if fv is None:
            if argnode is None:
                return None
            fv = self.fold(argnode)
            self.consume(argnode)
        if fv is None:
            fv = FV(PLACE, False)
        if op == "write" and fv.exact and not fv.t.strip():
            return None   # SAVE_VIDEO = "": an empty path means the feature is switched off
        if re.match(r"^[A-Za-z][A-Za-z0-9+.\-]{1,15}://", fv.t):
            return None   # a URL, not a local path (the network handlers report it)
        info = self.m.classify_path(fv)
        ext = info["ext"]
        if op == "write":
            cat = "file_write"
        elif ext in KEY_EXT:
            cat = "secret"
        elif ext in MODEL_EXT:
            cat = "model_file"
        elif ext in CONFIG_EXT:
            cat = "config_file"
        else:
            cat = "data_file_read"
        d = [detail] if detail else []
        if op in ("ref", "ref_literal") and cat == "data_file_read":
            if op == "ref" or not info["exists"]:
                return None
            # "data/classes.txt" as a plain string naming a file that ships with the project: the value may
            # travel through attributes / objects we cannot follow to the open() - keep the file in the image
            cap = "conditional"
            d.append("named in code as a string and the file exists; the read itself was not traced statically")
        mech = mechanism or (io if op == "write" else info["mech"])
        if op == "check":
            d.append("existence check (code may fall back if missing)")
        if info["env"]:
            d.append("default path; overridable via env " + ",".join(info["env"]))
        if cat == "secret":
            d.append("key/cert material referenced by code - provide at runtime, never bake it")
        if op == "ref_literal" and "/" not in info["subject"]:
            hits = self.m.find_by_basename(info["subject"])
            if hits:
                d.append("file(s) with this name: " + ", ".join(hits[:4]))
        f = self.fact(node, cat, info["subject"], mech, info["evidence"], detail="; ".join(d), op=op, cap=cap,
                      io=io, location=info["loc"], exists=info["exists"], path_origin=info["mech"],
                      path_envs=list(dict.fromkeys(info["env"])))
        ops = f.setdefault("ops", [])
        if op not in ops:
            ops.append(op)
        if info["loc"] == "absolute" and re.match(r"^[A-Za-z]:/", info["subject"]) and not info["env"]:
            # C:\Users\...\video.mp4: exists on the developer's Windows PC only - a Linux container never has it
            self.fact(node, "hardcoded_config", info["subject"], "windows_path",
                      detail="Windows path hardcoded in code - a Linux container cannot see C:\\...: read it from an "
                             "env var or argument (keep this as the default), then mount the file with -v")
            self.m.code_changes.append(f"{self.mod.rel}:{getattr(node, 'lineno', '?')}: Windows path {info['subject']} "
                                       "- read it from an env var / argument")
        return f

    def h_open(self, node):
        p = arg_or_kw(node, 0, "file", "filename")
        mode = self.fold(arg_or_kw(node, 1, "mode"))
        m = mode.t if mode is not None else "r"
        self.path_fact(node, p, "write" if any(ch in m for ch in "wax+") else "read", "python_io")

    def h_io(self, node, c):
        idx, op, io = IO_CALLS[c]
        if c.startswith("shutil.") and op == "write" and c != "shutil.rmtree":
            self.path_fact(node, arg_or_kw(node, 0, "src"), "read", io)
        p = arg_or_kw(node, idx, *PATH_KWS)
        if p is None:
            return
        f = self.path_fact(node, p, op, io)
        if c == "ultralytics.YOLO" and f is not None and f.get("exists") is False:
            self.fact(node, "runtime_download", f["subject"], "hub_download", f["evidence"],
                      detail="ultralytics downloads known model names at runtime if the file is missing")

    def h_pickle(self, node, c):
        if c == "torch.load":
            wo = kwarg(node, "weights_only")
            if isinstance(wo, ast.Constant) and wo.value is True:
                return
        if c == "numpy.load":
            ap = kwarg(node, "allow_pickle")
            if not (isinstance(ap, ast.Constant) and ap.value is True):
                return
        if c in ("torch.load", "joblib.load", "pandas.read_pickle", "pickle.load", "dill.load", "cloudpickle.load"):
            pnode = arg_or_kw(node, 0, "f", "filename", "filepath_or_buffer", "file")
            if c.endswith("pickle.load") or c in ("dill.load", "cloudpickle.load"):
                pnode = self.pickle_source(pnode)
            fv = self.fold(pnode) if pnode is not None else None
            info = self.m.classify_path(fv) if fv is not None else None
            if info and info["loc"] == "project" and "<" not in info["subject"] and info["exists"]:
                p = self.m.root / info["subject"]
                if p.is_file():
                    self.pickle_imports(node, c, p, info["subject"])
                    return
        self.fact(node, "dynamic_import", "<pickle>", "pickle_class_import", "unresolved",
                  detail=(f"{c}: unpickling imports the classes stored in the file by module path - "
                          "those source modules must be in the image (the runtime tracer reveals which)"),
                  import_site=True)

    def pickle_source(self, pnode):
        """pickle.load(fh) / with open(path, 'rb') as fh -> the open() path argument."""
        n = self.resolve_node(pnode)
        if isinstance(n, ast.Call) and self.canon(n.func) in OPEN_CALLS:
            return arg_or_kw(n, 0, "file")
        if isinstance(pnode, ast.Name):
            for fn in reversed(self.fn_stack or [self.mod.tree]):
                for w in ast.walk(fn):
                    if isinstance(w, (ast.With, ast.AsyncWith)):
                        for it in w.items:
                            if (isinstance(it.optional_vars, ast.Name) and it.optional_vars.id == pnode.id
                                    and isinstance(it.context_expr, ast.Call)
                                    and self.canon(it.context_expr.func) in OPEN_CALLS):
                                return arg_or_kw(it.context_expr, 0, "file")
        return pnode

    def pickle_imports(self, node, c, path, rel):
        """Read the class paths a pickle stores (GLOBAL / STACK_GLOBAL opcodes) - nothing is executed."""
        mods = self.m.pickle_globals(path)
        if mods is None:
            self.fact(node, "dynamic_import", "<pickle>", "pickle_class_import", "unresolved",
                      detail=f"{c}({rel}): could not read the pickle stream - the runtime tracer must confirm",
                      import_site=True)
            return
        loader_top = c.split(".")[0]
        seen_tops = set()
        for modname, names in mods.items():
            top = modname.split(".")[0]
            if top in PICKLE_SKIP or top == loader_top:
                if top == "__main__":
                    self.fact(node, "dangling_reference", f"__main__.{sorted(names)[0]}", "pickle_class_import",
                              detail=(f"{rel} stores classes from __main__ ({', '.join(sorted(names)[:3])}): "
                                      "unpickling fails unless the running script defines them"), import_site=True)
                continue
            chain, _ = self.m.resolve(modname)
            if not chain and (top in STDLIB or top in seen_tops):
                continue
            seen_tops.add(top)
            self.dyn_target(node, modname, "pickle_class_import", "static",
                            f"{c}({rel}) unpickles {', '.join(sorted(names)[:3])} from '{modname}': "
                            "no import statement names it, but the file does")

    # ---- network
    def url_fact(self, node, fv, io, download=False):
        if fv is None or fv.t.startswith("<"):
            self.fact(node, "network_endpoint", "<unresolved>", "data_driven", "unresolved",
                      detail="URL/source computed at runtime (e.g. from a DB or config)" + self.m.seed_note())
            return
        u = fv.t
        try:
            parts = urlsplit(u)
            host = parts.hostname or PLACE
            port = parts.port
        except ValueError:
            parts, host, port = None, PLACE, None
        scheme = parts.scheme.lower() if parts else ""
        port = port or SCHEME_PORTS.get(scheme, "?")
        ev = "static" if fv.exact and "<" not in u else ("static_partial" if host != PLACE else "unresolved")
        detail = f"{scheme}:// URL {mask_url(u)}" if scheme else mask_url(u)
        if fv.envs:
            detail += "; parts overridable via env " + ",".join(fv.envs)
        self.fact(node, "network_endpoint", f"{host}:{port}", io, ev, detail=detail, scheme=scheme)
        if parts is not None and parts.password:
            self.fact(node, "secret", mask_url(u), "credentials_in_url", detail="credentials embedded in a URL")
        path_ext = posixpath.splitext(parts.path if parts else "")[1].lower()
        if download or path_ext in MODEL_EXT:
            self.fact(node, "runtime_download", mask_url(u), io, ev,
                      detail="file downloaded at runtime - bake it into the image or mount a cache volume")
        if scheme == "https":
            self.m.https_seen = True

    def endpoint(self, node, hv, pv, io, via, default_port="?"):
        h = hv.t if hv is not None else PLACE
        p = pv.t if pv is not None else str(default_port)
        exact = hv is not None and hv.exact and (pv is None or pv.exact)
        ev = "static" if exact else ("static_partial" if hv is not None else "unresolved")
        envs = list((hv.envs if hv else ()) + (pv.envs if pv else ()))
        detail = via + ("; host/port overridable via env " + ",".join(envs) if envs else "")
        self.fact(node, "network_endpoint", f"{h}:{p}", io, ev, detail=detail)
        # the deploy files (compose / Dockerfile / .env) may set the host env to something else: that is
        # the endpoint the container really talks to (e.g. code default 192.168.x.x, compose DB_HOST: db)
        for e in (hv.envs if hv is not None else ()):
            for val, where in self.m.hints.get(e, []):
                if val != h and not where.lower().endswith((".md", ".rst", ".txt")):
                    self.fact(node, "network_endpoint", f"{val}:{p}", io, "static_partial", deploy_value=True,
                              detail=f"{via}; host from env {e}={val} in {where} (code default {h})")

    def h_net(self, node, c):
        kind = NET_CALLS[c]
        top = c.split(".")[0]
        io = "c_level_io" if top in C_LEVEL_NET else "python_io"
        if kind == "db":
            hv, pv = self.fold(kwarg(node, "host")), self.fold(kwarg(node, "port"))
            if hv is None:
                dsn = self.fold(arg_or_kw(node, 0, "dsn", "conninfo"))
                if dsn is not None:
                    if "://" in dsn.t:
                        self.url_fact(node, dsn, io)
                        return
                    mh = re.search(r"(?i)\bhost\s*=\s*([^\s;]+)", dsn.t)
                    mp = re.search(r"(?i)\bport\s*=\s*([^\s;]+)", dsn.t)
                    hv = FV(mh.group(1), dsn.exact) if mh else None
                    pv = FV(mp.group(1), dsn.exact) if mp else None
            self.endpoint(node, hv, pv, io, f"{c}()", DB_DEFAULT_PORTS.get(top, "?"))
            pw_node = kwarg(node, "password", "passwd")
            pw = self.fold(pw_node) if isinstance(pw_node, ast.Constant) else None
            if pw is not None and pw.exact and pw.t:
                self.fact(node, "secret", f"{c}:password", "hardcoded_kwarg",
                          detail=f"database password literal ({mask(pw.t)})")
            for k in node.keywords:
                self.consume(k.value)
        elif kind in ("url", "download"):
            u = arg_or_kw(node, 1 if c == "requests.request" else 0, "url", "fullurl")
            fv = self.fold(u)
            self.consume(u)
            self.url_fact(node, fv, io, download=(kind == "download"))
            if kind == "download":
                dest = arg_or_kw(node, 1, "filename", "dst", "out", "output")
                if dest is not None:
                    self.path_fact(node, dest, "write", io)
        elif kind == "tuple":
            t = node.args[0] if node.args else None
            if isinstance(t, ast.Tuple) and len(t.elts) >= 2:
                self.endpoint(node, self.fold(t.elts[0]), self.fold(t.elts[1]), io, f"{c}()")
                self.consume(t)
        elif kind == "hostport":
            self.endpoint(node, self.fold(arg_or_kw(node, 0, "host")), self.fold(arg_or_kw(node, 1, "port")),
                          io, f"{c}()")
        elif kind == "capture":
            self.h_capture(node)
        elif kind == "hub":
            self.h_hub(node, c)

    def h_capture(self, node):
        a = arg_or_kw(node, 0, "filename", "index")
        if isinstance(a, ast.Constant) and isinstance(a.value, int) and not isinstance(a.value, bool):
            self.fact(node, "device", f"/dev/video{a.value}", "c_level_io",
                      detail="camera by device index -> compose devices: /dev/video<n>")
            return
        fv = self.fold(a)
        self.consume(a)
        if fv is None or (fv.t.startswith("<") and fv.t.endswith(">")):
            self.fact(node, "network_endpoint", "<unresolved>", "data_driven", "unresolved",
                      detail=f"cv2.VideoCapture source computed at runtime ({unparse(a)}) - e.g. RTSP URLs from a DB"
                             + self.m.seed_note())
            return
        low = fv.t.lower()
        if low.startswith(("rtsp", "rtmp", "http", "udp", "tcp")):
            self.url_fact(node, fv, "c_level_io")
        elif fv.t.isdigit():
            self.fact(node, "device", f"/dev/video{fv.t}", "c_level_io", detail="camera by device index")
        else:
            self.path_fact(node, None, "read", "c_level_io", fv=fv)

    def h_hub(self, node, c):
        if c == "torch.hub.load":
            repo, model = self.fold(arg_or_kw(node, 0, "repo_or_dir")), self.fold(arg_or_kw(node, 1, "model"))
            subj = f"{fvt(repo)}:{fvt(model)}"   # torch.hub's own "owner/repo" + entrypoint naming
            detail = "torch.hub.load: downloads the GitHub repo + weights into TORCH_HOME at runtime"
        elif c == "insightface.app.FaceAnalysis":
            name = self.fold(kwarg(node, "name"))
            subj = f"insightface:{name.t if name else 'buffalo_l'}"
            detail = "downloads the model pack to ~/.insightface/models unless root= points at baked models"
        else:
            repo, fname = self.fold(arg_or_kw(node, 0, "repo_id")), self.fold(arg_or_kw(node, 1, "filename"))
            subj = f"hf:{fvt(repo)}" + (f"/{fname.t}" if fname is not None else "")
            detail = "downloads from the Hugging Face Hub into HF_HOME at runtime"
        ev = "static" if PLACE not in subj else "static_partial"
        self.fact(node, "runtime_download", subj, "hub_download", ev,
                  detail=detail + " - bake at build time or mount a cache volume; set offline flags")

    def h_pretrained(self, node):
        fv = self.fold(node.args[0])
        if fv is None:
            return
        info = self.m.classify_path(fv)
        if info["exists"]:
            self.path_fact(node, None, "read", "python_io", fv=fv, detail=".from_pretrained() local path")
        else:
            self.fact(node, "runtime_download", f"hf:{fv.t}", "hub_download",
                      "static" if fv.exact else "static_partial",
                      detail=".from_pretrained() with a model id downloads into HF_HOME at runtime")

    # ---- servers
    def listen(self, node, hv, pv, default_host, default_port, via):
        h = hv.t if hv is not None else default_host
        p = pv.t if pv is not None else str(default_port)
        if h == "":
            h = "0.0.0.0"
        detail = [f"server started via {via}"]
        if hv is None:
            detail.append(f"no host given -> default {default_host}")
        if h in ("127.0.0.1", "localhost", "::1"):
            detail.append("binds loopback: unreachable through published ports - must bind 0.0.0.0 (code change)")
            self.m.code_changes.append(f"{self.mod.rel}:{node.lineno}: server binds {h} - change to 0.0.0.0")
        if hv is not None and hv.envs:
            detail.append("host overridable via env " + ",".join(hv.envs))
        exact = (hv is None or hv.exact) and (pv is None or pv.exact)
        self.fact(node, "listen_port", f"{h}:{p}", "server_bind", "static" if exact else "static_partial",
                  detail="; ".join(detail), host=h, port=p)

    def h_listen_fn(self, node, c):
        spec = LISTEN_CALLS[c]
        if spec == "tuple":
            t = node.args[0] if node.args else kwarg(node, "server_address")
            if isinstance(t, ast.Tuple) and len(t.elts) >= 2:
                self.listen(node, self.fold(t.elts[0]), self.fold(t.elts[1]), "0.0.0.0", "?", c)
                self.consume(t)
            return
        self.listen(node, self.fold(kwarg(node, "host")), self.fold(kwarg(node, "port")), spec[0], spec[1], c)

    def h_run_method(self, node, c):
        bname = dotted(node.func.value) or ""
        last = bname.split(".")[-1].lower()
        host, port = kwarg(node, "host"), kwarg(node, "port")
        if c and c.split(".")[0] in ("subprocess", "asyncio", "uvicorn", "multiprocessing"):
            return
        if host is None and port is None and not (last in APPISH or last.endswith("app")):
            return
        if last in ("socketio", "sio"):
            host = host if host is not None else (node.args[1] if len(node.args) > 1 else None)
            port = port if port is not None else (node.args[2] if len(node.args) > 2 else None)
        else:
            host = host if host is not None else (node.args[0] if node.args else None)
            port = port if port is not None else (node.args[1] if len(node.args) > 1 else None)
        hv, pv = self.fold(host), self.fold(port)
        self.consume(host)
        self.consume(port)
        self.listen(node, hv, pv, "127.0.0.1", 5000, f"{bname}.run() (Flask-style default 127.0.0.1:5000)")
        ssl = kwarg(node, "ssl_context")
        if ssl is not None:
            self.fact(node, "secret", "ssl_context", "tls_config", "static_partial",
                      detail=f"TLS enabled with {unparse(ssl)} - provide key material at runtime, don't bake it")

    # ---- machine identity / GPU index
    def h_hwid(self, node, c):
        """uuid.getnode() etc. + the licensed value it is compared with (a MAC constant in the same module)."""
        macs = [(n, v) for n, v in self.mod.consts.items()
                if isinstance(v, ast.Constant) and isinstance(v.value, str) and MAC_RE.match(v.value)]
        q = self.container()[0]
        envs = sorted({f["subject"] for f in self.m.facts if f["category"] == "env_var" and f["_mod"] == self.mod.rel
                       and any(s.get("_q") == q for s in f["sites"]) and f["subject"] != "<unresolved>"})
        detail = "reads machine identity (MAC/hostname): a container gets a random MAC/hostname on every run"
        extra = {}
        if macs:
            name, v = macs[0]
            extra["value"] = v.value
            detail += (f"; compared with {name} = '{v.value}' ({self.mod.rel}:{v.lineno}) -> compose "
                       f"mac_address: \"{v.value}\"")
        if envs:
            detail += "; same function reads env " + ", ".join(envs) + " (possible enforcement switch)"
            extra["switch_env"] = envs
        f = self.fact(node, "hardware_identity", c, "hwid_call", detail=detail, **extra)
        if macs and not any(s.get("line") == macs[0][1].lineno for s in f["sites"]):
            f["sites"].append({"file": self.mod.rel, "line": macs[0][1].lineno, "_q": q, "_t": list(self.ctx),
                               "_imp": False})

    def gpu_index_consts(self, expr):
        """torch.device(f"cuda:{GPU_ID}") with GPU_ID = 0 at module level -> hardcoded device index."""
        for n in ast.walk(expr):
            if isinstance(n, ast.Name) and not any(n.id in s for s in self.scopes[1:]):
                v = self.mod.consts.get(n.id)
                if isinstance(v, ast.Constant) and isinstance(v.value, int) and not isinstance(v.value, bool):
                    self.fact(v, "hardcoded_config", f"{n.id} = {v.value}", "string_literal", value=v.value,
                              detail=f"GPU index hardcoded to {v.value}: inside a container the visible GPUs are "
                                     "renumbered from 0 (CUDA_VISIBLE_DEVICES / compose device_ids)")

    # ---- processes / native / misc
    def h_subproc(self, node, c):
        if c.startswith("os.exec"):
            a = node.args[0] if node.args else None
        else:
            a = arg_or_kw(node, 0, "args", "cmd", "command", "program")
        a = self.resolve_node(a)   # cmd = ["ffprobe", ...]; subprocess.run(cmd)
        binary = None
        if isinstance(a, (ast.List, ast.Tuple)) and a.elts:
            fv = self.fold(a.elts[0])
            binary = fv.t if fv is not None else None
        elif a is not None:
            fv = self.fold(a)
            if fv is not None:
                try:
                    toks = shlex.split(fv.t)
                except ValueError:
                    toks = fv.t.split()
                binary = toks[0] if toks else None
        self.consume(a)
        if not binary or binary.startswith("<"):
            self.fact(node, "subprocess_binary", "<unresolved>", "subprocess", "unresolved",
                      detail=f"{c}({unparse(a)})")
            return
        name = posixpath.basename(binary)
        apt = BIN_TO_APT.get(name)
        self.fact(node, "subprocess_binary", name, "subprocess",
                  detail=f"runs '{name}' via {c}" + (f" -> apt: {apt}" if apt else " -> must exist in the image"),
                  apt_candidate=apt)

    def h_native(self, node, c):
        a = arg_or_kw(node, 0, "name")
        src = self.resolve_node(a)   # lib = find_library("gomp"); CDLL(lib)
        via_find = c == "ctypes.util.find_library"
        if isinstance(src, ast.Call) and self.canon(src.func) == "ctypes.util.find_library":
            if src is a:
                return  # CDLL(find_library(...)): the inner find_library call is reported on its own
            via_find, a = True, arg_or_kw(src, 0, "name")
        fv = self.fold(a)
        self.consume(a)
        if fv is None or not fv.exact:
            self.fact(node, "native_library", "<unresolved>", "ctypes", "unresolved", detail=f"{c}({unparse(a)})")
            return
        lib = libname(fv.t)
        subject = fv.t if re.search(r"\.so(\.\d+)*$", fv.t) and not via_find else SONAME.get(lib, f"lib{lib}.so")
        apt = LIB_TO_APT.get(lib)
        # find_library() returns None when the library is missing, and a guarded CDLL has a fallback
        cap = "conditional" if via_find or self.guarded() else None
        how = "looked up with ctypes.util.find_library (returns None if absent)" if c.endswith("find_library") \
            else f"loaded with {c}"
        self.fact(node, "native_library", subject, "ctypes", cap=cap, lib=lib,
                  detail=how + (f" -> apt candidate: {apt}" if apt else ""), apt_candidate=apt)
        if apt and not apt.startswith("("):
            self.fact(node, "system_package_candidate", apt, "ctypes", cap=cap,
                      detail=f"provides {subject} (verify with ldd / apt-file)")

    def h_mp(self, node, c):
        detail = c
        subject = "multiprocessing"
        if c.endswith("set_start_method") or c.endswith("get_context"):
            m = self.fold(arg_or_kw(node, 0, "method"))
            if m is not None and m.exact:
                subject = f"start_method:{m.t}"
                detail += f"; start method '{m.t}'"
                if m.t in ("spawn", "forkserver"):
                    detail += (" - every child re-imports the main module and everything it imports: "
                               "module-level code (log files, makedirs, model loads) runs again in each process")
        self.fact(node, "multiprocessing", subject, "mp_call", detail=detail)

    def h_shm(self, node, c):
        size_node = kwarg(node, "size")
        if size_node is None and c.endswith("SharedMemory") and len(node.args) > 2:
            size_node = node.args[2]
        fv = self.fold(size_node) if size_node is not None else None
        n = as_int(fv)
        create = kwarg(node, "create")
        attach = c.endswith("SharedMemory") and not (isinstance(create, ast.Constant) and create.value is True) \
            and size_node is None
        detail = c
        if n:
            detail += (f"; size {unparse(size_node, 60)} = {n} bytes (~{n / 1048576:.1f} MB) per segment"
                       + (" with defaults of env " + ",".join(dict.fromkeys(fv.envs)) if fv.envs else ""))
            if n < 64 * 1048576:
                detail += f"; {-(-64 * 1048576 // n)} such segments exceed Docker's 64 MB /dev/shm default"
            else:
                detail += "; one segment alone exceeds Docker's 64 MB /dev/shm default"
        elif attach:
            detail += " (attaches to an existing segment)"
        extra = {"size_bytes": n} if n else {}
        if fv is not None and fv.envs:
            extra["size_envs"] = list(dict.fromkeys(fv.envs))
        self.fact(node, "ipc_shared_memory", c, "shm_call",
                  detail=detail + "; lives in /dev/shm -> set compose shm_size", **extra)

    def h_method(self, node, meth, c):
        base = node.func.value
        if meth in METHOD_IO:
            fv = self.fold(base)
            if fv is not None and self.pathish(fv):
                self.path_fact(node, None, METHOD_IO[meth], "python_io", fv=fv, detail=f".{meth}()")
                self.consume(base)
        elif meth == "open":
            fv = self.fold(base)
            if fv is not None and self.pathish(fv):
                mode = self.fold(arg_or_kw(node, 0, "mode"))
                op = "write" if mode is not None and any(ch in mode.t for ch in "wax+") else "read"
                self.path_fact(node, None, op, "python_io", fv=fv, detail=".open()")
                self.consume(base)
        elif meth == "save" and node.args:
            fv = self.fold(node.args[0])
            if fv is not None and posixpath.splitext(fv.t)[1].lower() in SAVE_EXT:
                self.path_fact(node, node.args[0], "write", "python_io", detail=".save()")
        elif meth == "from_pretrained" and node.args:
            self.h_pretrained(node)
        elif meth == "cuda":
            self.fact(node, "gpu_usage", "torch:.cuda()", "gpu_call", detail="moves tensors/models to the GPU")
        elif meth == "to" and (node.args or kwarg(node, "device") is not None):
            dv = self.fold(node.args[0]) if node.args else self.fold(kwarg(node, "device"))
            if dv is not None and "cuda" in dv.t:
                self.fact(node, "gpu_usage", "torch:.to(cuda)", "gpu_call", detail=unparse(node))
        elif meth in ("connect", "connect_ex") and node.args and isinstance(node.args[0], ast.Tuple):
            t = node.args[0]
            if len(t.elts) >= 2:
                self.endpoint(node, self.fold(t.elts[0]), self.fold(t.elts[1]), "python_io", "socket connect")
        elif meth == "bind" and node.args and isinstance(node.args[0], ast.Tuple):
            t = node.args[0]
            if len(t.elts) >= 2:
                self.listen(node, self.fold(t.elts[0]), self.fold(t.elts[1]), "0.0.0.0", "?", "socket.bind()")
        elif meth == "run":
            self.h_run_method(node, c)
        elif meth in ("share_memory_", "share_memory"):
            self.fact(node, "ipc_shared_memory", "torch.share_memory", "shm_call",
                      detail="tensor moved to shared memory (/dev/shm)")

    def h_kwargs(self, node, c):
        for k in node.keywords:
            if k.arg is None:
                continue
            if k.arg == "device":
                v = self.fold(k.value)
                if v is not None and "cuda" in v.t.lower():
                    self.fact(node, "gpu_usage", "device=cuda", "gpu_kwarg", detail=f"{c or 'call'}(device={v.t!r})")
            elif k.arg in GPU_FLAGS and isinstance(k.value, ast.Constant) and k.value.value is True:
                self.fact(node, "gpu_usage", f"{k.arg}=True", "gpu_kwarg", confidence="medium",
                          detail=f"{c or 'call'}({k.arg}=True)")
            elif (secretish(k.arg) and isinstance(k.value, ast.Constant) and isinstance(k.value.value, str)
                  and k.value.value and NET_CALLS.get(c) != "db"):
                self.fact(node, "secret", k.arg, "hardcoded_kwarg",
                          detail=f"{c or 'call'}({k.arg}={mask(k.value.value)!r})")
                self.consume(k.value)

    # ---------------------------------------------------------- assignments / literals
    def secret_assign(self, node, target, value):
        nm = target.id if isinstance(target, ast.Name) else (
            target.attr if isinstance(target, ast.Attribute) else None)
        if nm and secretish(nm) and isinstance(value, ast.Constant) and isinstance(value.value, str) and value.value:
            self.fact(node, "secret", nm, "hardcoded_assignment",
                      detail=f"hardcoded value {mask(value.value)!r} - move to env var / Docker secret (code change)")
            self.consume(value)
            self.m.code_changes.append(f"{self.mod.rel}:{node.lineno}: hardcoded secret '{nm}'")
        if isinstance(target, ast.Attribute) and target.attr in ("intra_op_num_threads", "inter_op_num_threads"):
            self.fact(node, "thread_config", target.attr, "thread_attr", detail="ONNX Runtime thread pool size set in code")

    def visit_Assign(self, node):
        if (len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
                and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)):
            self.assign_names[id(node.value)] = node.targets[0].id
        if (self.fn_stack and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
                and isinstance(node.value, ast.Call)):
            nm = node.targets[0].id
            if not any(isinstance(n, ast.Name) and n.id == nm and isinstance(n.ctx, ast.Load)
                       for n in ast.walk(self.fn_stack[-1])):
                self.unused_vals.add(id(node.value))
        if self.fn_depth:
            for t in node.targets:
                if isinstance(t, ast.Name):
                    self.scopes[-1][t.id] = node.value
        for t in node.targets:
            self.secret_assign(node, t, node.value)
        self.generic_visit(node)

    def visit_AnnAssign(self, node):
        if node.value is not None:
            if self.fn_depth and isinstance(node.target, ast.Name):
                self.scopes[-1][node.target.id] = node.value
            self.secret_assign(node, node.target, node.value)
        self.generic_visit(node)

    def visit_Dict(self, node):
        for k, v in zip(node.keys, node.values):
            if (isinstance(k, ast.Constant) and isinstance(k.value, str) and secretish(k.value)
                    and isinstance(v, ast.Constant) and isinstance(v.value, str) and v.value):
                self.fact(node, "secret", k.value, "hardcoded_dict", detail=f"dict literal value {mask(v.value)!r}")
                self.consume(v)
        self.generic_visit(node)

    def visit_List(self, node):
        strs = [e.value for e in node.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
        if "CPUExecutionProvider" in strs:
            for e in node.elts:
                if isinstance(e, ast.Constant) and isinstance(e.value, str) and GPU_PROVIDER_RE.search(e.value):
                    self.cpu_fallback.add(id(e))
        self.generic_visit(node)

    visit_Tuple = visit_List

    def visit_Attribute(self, node):
        if isinstance(node.value, ast.Name):
            tgt = self.mod.aliases.get(node.value.id)
            if tgt and not tgt.startswith("."):
                self.m.attr_tokens.setdefault(tgt, set()).add(node.attr)
        self.generic_visit(node)

    def path_expr(self, node):
        if id(node) in self.consumed:
            return
        fv = self.fold(node)
        if fv is None:
            return
        if posixpath.splitext(fv.t)[1].lower() in (MODEL_EXT | CONFIG_EXT | KEY_EXT) and self.pathish(fv):
            self.path_fact(node, None, "ref", "python_io", fv=fv)
            self.consume(node)
        elif fv.origin & {"file", "resources", "cwd", "home"}:
            self.consume(node)  # fragments of a path are not separate references

    def visit_BinOp(self, node):
        if isinstance(node.op, ast.Div) or (isinstance(node.op, ast.Add)):
            self.path_expr(node)
        self.generic_visit(node)

    def visit_JoinedStr(self, node):
        self.path_expr(node)
        self.generic_visit(node)

    def visit_Constant(self, node):
        v = node.value
        if not isinstance(v, str) or not v or len(v) > 400:
            return
        if id(node) in self.consumed or id(node) in self.mod.docstrings:
            return
        m = GPU_PROVIDER_RE.search(v)
        if m:
            if id(node) in self.cpu_fallback:
                f = self.fact(node, "gpu_usage", m.group(0), "provider_string", library="onnxruntime",
                              detail="GPU requested, CPUExecutionProvider listed as fallback: GPU optional")
                f["_force"] = "conditional"
            else:
                self.fact(node, "gpu_usage", m.group(0), "provider_string", library="onnxruntime",
                          detail="ONNX Runtime GPU provider requested with no CPU fallback: GPU required")
        urls = URL_RE.findall(v)
        name = self.assign_names.get(id(node))
        for scheme, rest in urls:
            full = f"{scheme}://{rest}"
            if scheme.lower() in URL_SCHEMES:
                detail = f"URL {mask_url(full)} hardcoded in code - externalize to env/config"
                if re.match(r"(localhost|127\.0\.0\.1)(:|/|$)", rest):
                    detail += ("; inside a container 'localhost' is the container itself, not your machine "
                               "or another service - use the compose service name")
                self.fact(node, "hardcoded_config", name or mask_url(full), "string_literal",
                          detail=detail, value=mask_url(full))
            if re.search(r"://[^/\s:@]+:[^@/\s]+@", full):
                self.fact(node, "secret", mask_url(full), "credentials_in_url", detail="credentials embedded in a URL")
        if not urls:
            for ip in sorted(set(IPV4_RE.findall(v)) - LOOPBACK):
                self.fact(node, "hardcoded_config", name or ip, "string_literal",
                          detail=f"IP address {ip} hardcoded in code - externalize to env/config", value=ip)
        mpw = DSN_PW_RE.search(v)
        if mpw and ("host" in v.lower() or "server" in v.lower()):
            self.fact(node, "secret", "connection-string password", "hardcoded_dsn",
                      detail=f"password inside a connection string ({mask(mpw.group(2))})")
        if v.startswith("/dev/"):
            if v.startswith("/dev/shm"):
                self.fact(node, "ipc_shared_memory", "/dev/shm", "string_literal", detail="uses /dev/shm directly")
            elif not v.startswith(("/dev/null", "/dev/stdout", "/dev/stderr", "/dev/zero", "/dev/urandom",
                                   "/dev/random", "/dev/tty")):
                self.fact(node, "device", v, "string_literal", detail="host device path -> compose devices:")
        s = v.strip()
        ext = posixpath.splitext(s)[1].lower()
        if len(s) < 260 and "\n" not in s and " " not in s and not urls:
            if ext in (MODEL_EXT | CONFIG_EXT | KEY_EXT):
                self.path_fact(node, None, "ref_literal", "python_io", fv=FV(s))
            elif 1 < len(ext) <= 9 and "%" not in s and "{" not in s:
                self.path_fact(node, None, "ref_literal", "python_io", fv=FV(s))  # kept only if the file exists


# =============================================================== call graph
class CallGraphBuilder(ast.NodeVisitor):
    """Call edges (caller -> callee) with branch tags, literal call arguments and registry lookups."""

    def __init__(self, mapper, mod):
        self.m, self.mod = mapper, mod
        self.cls, self.q, self.tags, self.depth, self.params = None, None, [], 0, None
        self.regs = mapper.cg_registries.get(mod.rel, {})
        self.local_regs, self.local_alias = {}, {}

    def run(self):
        self.stmts(self.mod.tree.body)

    def add(self, callee, cap):
        self.m.call_edges.append((self.mod.rel, self.q, callee, cap, list(self.tags)))

    def general_branch(self, t):
        if (isinstance(t, ast.Name) and t.id == "TYPE_CHECKING") or self.m.canon(self.mod, t) == "typing.TYPE_CHECKING":
            return False
        if isinstance(t, ast.Constant):
            return False
        if isinstance(t, ast.Compare) and isinstance(t.left, ast.Name) and t.left.id == "__name__":
            return False
        src = unparse(t, 60)
        return not ("sys.platform" in src or "os.name" in src or "platform.system" in src)

    def stmts(self, body):
        pushed = 0
        for st in body:
            self.visit(st)
            if isinstance(st, ast.If) and not st.orelse and terminates(st.body) and self.general_branch(st.test):
                self.tags.append(f"branch@{id(st.test)}:0")
                pushed += 1
        for _ in range(pushed):
            self.tags.pop()

    def block(self, body, tag):
        if tag:
            self.tags.append(tag)
        self.stmts(body)
        if tag:
            self.tags.pop()

    def visit_If(self, node):
        self.visit(node.test)
        t = node.test
        if (isinstance(t, ast.Name) and t.id == "TYPE_CHECKING") or self.m.canon(self.mod, t) == "typing.TYPE_CHECKING":
            bt, et = "type_checking", None
        elif isinstance(t, ast.Constant):
            bt, et = ("dead", None) if not t.value else (None, "dead")
        elif isinstance(t, ast.Compare) and isinstance(t.left, ast.Name) and t.left.id == "__name__":
            bt, et = "main_guard", "branch"
        elif not self.general_branch(t):
            bt, et = "platform", "platform"
        else:
            bt, et = f"branch@{id(t)}:1", f"branch@{id(t)}:0"
        self.block(node.body, bt)
        self.block(node.orelse, et)

    def visit_Try(self, node):
        self.block(node.body, None)
        for h in node.handlers:
            self.block(h.body, "branch")
        self.block(node.orelse, None)
        self.block(node.finalbody, None)

    visit_TryStar = visit_Try

    def visit_For(self, node):
        self.visit(node.target)
        self.visit(node.iter)
        self.stmts(node.body)
        self.stmts(node.orelse)

    visit_AsyncFor = visit_For

    def visit_While(self, node):
        self.visit(node.test)
        self.stmts(node.body)
        self.stmts(node.orelse)

    def visit_With(self, node):
        for it in node.items:
            self.visit(it)
        self.stmts(node.body)

    visit_AsyncWith = visit_With

    def visit_FunctionDef(self, node):
        for d in node.decorator_list:
            self.visit(d)
        for dflt in node.args.defaults + [x for x in node.args.kw_defaults if x is not None]:
            self.visit(dflt)
        if self.depth == 0:
            saved = (self.q, self.tags, self.params, self.local_regs, self.local_alias)
            self.q = f"{self.cls}.{node.name}" if self.cls else node.name
            self.tags = []
            self.params = set(fn_param_names(node, self.cls is not None)) | {a.arg for a in node.args.kwonlyargs}
            self.local_regs, self.local_alias = {}, {}
            self.depth += 1
            self.stmts(node.body)
            self.depth -= 1
            self.q, self.tags, self.params, self.local_regs, self.local_alias = saved
        else:
            self.tags.append("nested")
            self.depth += 1
            self.stmts(node.body)
            self.depth -= 1
            self.tags.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Lambda(self, node):
        self.tags.append("nested")
        self.visit(node.body)
        self.tags.pop()

    def visit_ClassDef(self, node):
        for x in node.decorator_list + node.bases:
            self.visit(x)
        if self.depth == 0 and self.cls is None:
            self.cls = node.name
            self.stmts(node.body)
            self.cls = None
        else:
            self.tags.append("nested")
            self.stmts(node.body)
            self.tags.pop()

    def visit_Match(self, node):
        self.visit(node.subject)
        for case in node.cases:
            self.tags.append(f"branch@{id(case)}:1")
            if case.guard is not None:
                self.visit(case.guard)
            self.stmts(case.body)
            self.tags.pop()

    def derive(self, expr):
        """(parameter, transforms) when expr is a parameter, a local copy of one, or name.lower() etc."""
        e, tr = unwrap_tr(expr)
        if isinstance(e, ast.Name):
            if e.id in self.local_alias:
                p, t0 = self.local_alias[e.id]
                return p, t0 + tr
            if self.params and e.id in self.params:
                return e.id, tr
        return None

    def track_assign(self, target, value, node):
        if (self.depth == 0 and self.cls is None and isinstance(target, ast.Name)
                and self.regs.get(target.id, {}).get("node") is node):
            return True  # module registry: values are wired up where it is looked up
        if self.q is None or not isinstance(target, ast.Name) or value is None:
            return False
        if registry_dict(value):  # registry defined inside the function
            key = (self.mod.rel, f"{self.q}::{target.id}")
            targets = {}
            for k, v in zip(value.keys, value.values):
                res = self.m.resolve_callee(self.mod, v, self.cls)
                if res:
                    targets[k.value] = res
                    for callee, _kind in res:
                        self.m.cg_escaped.add(callee)
            self.m.cg_registry_targets[key] = targets
            self.local_regs[target.id] = key
            return True
        d = self.derive(value)
        if d:
            self.local_alias[target.id] = d
        else:
            self.local_alias.pop(target.id, None)
            self.local_regs.pop(target.id, None)
        return False

    def visit_Assign(self, node):
        if len(node.targets) == 1 and self.track_assign(node.targets[0], node.value, node):
            return
        self.generic_visit(node)

    def visit_AnnAssign(self, node):
        if self.track_assign(node.target, node.value, node):
            return
        self.generic_visit(node)

    # ---- registries
    def reg_of(self, expr):
        if isinstance(expr, ast.Name):
            if expr.id in self.local_regs:
                return self.local_regs[expr.id]
            if expr.id in self.regs:
                return (self.mod.rel, expr.id)
        return None

    def lookup(self, reg, key, cap):
        e = {"rel": self.mod.rel, "q": self.q, "tags": list(self.tags), "reg": reg, "cap": cap,
             "key": None, "param": None}
        d = self.derive(key)
        if isinstance(key, ast.Constant):
            e["key"] = key.value
        elif d:
            e["param"], e["tr"] = d
        else:
            fv = Folder(self.m, self.mod).fold(key)
            if fv is not None and fv.exact and not fv.origin:
                e["key"] = lit_key(fv)
        self.m.cg_lookups.append(e)

    KEY_ONLY_FUNCS = {"len", "list", "sorted", "set", "tuple", "str", "repr", "frozenset"}

    def visit_Compare(self, node):
        # `name in REGISTRY` only looks at the keys: never an escape
        self.visit(node.left)
        for op, comp in zip(node.ops, node.comparators):
            if isinstance(op, (ast.In, ast.NotIn)) and self.reg_of(comp):
                continue
            self.visit(comp)

    def key_only_call(self, node):
        f = node.func
        if isinstance(f, ast.Attribute) and f.attr == "keys" and self.reg_of(f.value):
            return True
        if (isinstance(f, ast.Name) and f.id in self.KEY_ONLY_FUNCS and len(node.args) == 1
                and self.reg_of(node.args[0])):
            return True
        if (isinstance(f, ast.Attribute) and f.attr == "join" and isinstance(f.value, ast.Constant)
                and len(node.args) == 1 and self.reg_of(node.args[0])):
            return True
        return False

    def visit_Subscript(self, node):
        reg = self.reg_of(node.value)
        if reg and isinstance(node.ctx, ast.Load):
            self.lookup(reg, node.slice, "conditional")
            self.visit(node.slice)
            return
        self.generic_visit(node)

    # ---- calls
    def argval(self, a):
        if isinstance(a, ast.Constant) and isinstance(a.value, (str, int)) and not isinstance(a.value, bool):
            return ("lit", a.value)
        d = self.derive(a)
        if d:
            return ("param", d[0], d[1])
        fv = Folder(self.m, self.mod).fold(a)
        if fv is not None and fv.exact and not fv.origin:
            return ("lit", lit_key(fv))
        return ("top",)

    def argmap(self, node, callee):
        fn = self.m.cg_funcs.get(callee[1], {}).get(callee[2])
        if fn is None or any(isinstance(a, ast.Starred) for a in node.args) or any(k.arg is None for k in node.keywords):
            return "TOP"
        amap = {}
        for p, a in zip(fn_param_names(fn, "." in callee[2]), node.args):
            amap[p] = self.argval(a)
        for k in node.keywords:
            amap[k.arg] = self.argval(k.value)
        return amap

    def visit_args(self, node):
        for a in node.args:
            self.visit(a)
        for k in node.keywords:
            self.visit(k.value)

    def visit_Call(self, node):
        f = node.func
        if self.key_only_call(node):
            return
        if isinstance(f, ast.Subscript) and self.reg_of(f.value):
            self.lookup(self.reg_of(f.value), f.slice, "yes")
            self.visit(f.slice)
            self.visit_args(node)
            return
        if isinstance(f, ast.Attribute) and f.attr == "get" and self.reg_of(f.value) and node.args:
            self.lookup(self.reg_of(f.value), node.args[0], "conditional")
            self.visit_args(node)
            return
        for callee, kind in self.m.resolve_callee(self.mod, f, self.cls):
            if kind == "direct":
                self.add(callee, "yes")
                self.m.cg_callsites.append({"caller": (self.mod.rel, self.q), "callee": callee,
                                            "tags": list(self.tags), "args": self.argmap(node, callee)})
            elif kind == "start":  # Worker(...) of a Process / Thread subclass: its run() is what .start() runs
                self.add(callee, "yes")
                self.m.cg_escaped.add(callee)
            else:
                self.add(callee, "conditional")
                self.m.cg_escaped.add(callee)
        if self.m.canon(self.mod, f) in TARGET_CALLS:
            # Process(target=worker) / Thread(target=...) / Timer(t, fn): the callable runs in the new process
            # positional slot 1 in both Process/Thread(group, target) and Timer(interval, function)
            tgt = kwarg(node, "target", "function") or (node.args[1] if len(node.args) > 1 else None)
            if isinstance(tgt, (ast.Name, ast.Attribute)):
                for callee, _k in self.m.resolve_callee(self.mod, tgt, self.cls, reference=True):
                    self.add(callee, "yes")
                    self.m.cg_escaped.add(callee)
        if isinstance(f, ast.Attribute):
            self.visit(f.value)   # not the attribute itself: that is the call, not a reference
        elif not isinstance(f, ast.Name):
            self.visit(f)
        self.visit_args(node)

    # ---- references (functions passed around as values may run later, with unknown arguments)
    def imported_registry(self, dotted_name):
        if not dotted_name or "." not in dotted_name.strip("."):
            return None
        modname, _, attr = dotted_name.rpartition(".")
        other = self.m.lookup_module(modname, self.mod)
        if other is not None and attr in self.m.cg_registries.get(other.rel, {}):
            return (other.rel, attr)
        return None

    def visit_Name(self, node):
        if not isinstance(node.ctx, ast.Load):
            return
        if node.id in self.local_regs:
            self.m.cg_escaped_regs.add(self.local_regs[node.id])
            return
        if node.id in self.regs:
            self.m.cg_escaped_regs.add((self.mod.rel, node.id))
            return
        reg = self.imported_registry(self.mod.aliases.get(node.id))
        if reg:
            self.m.cg_escaped_regs.add(reg)
            return
        for callee, _k in self.m.resolve_callee(self.mod, node, self.cls, reference=True):
            self.add(callee, "conditional")
            self.m.cg_escaped.add(callee)

    def visit_Attribute(self, node):
        if isinstance(node.ctx, ast.Load) and isinstance(node.value, ast.Name):
            if node.value.id in ("self", "cls") and self.cls:
                q = f"{self.cls}.{node.attr}"
                if q in self.m.cg_funcs.get(self.mod.rel, {}):
                    callee = ("F", self.mod.rel, q)
                    self.add(callee, "conditional")
                    self.m.cg_escaped.add(callee)
            else:
                reg = self.imported_registry(self.m.canon(self.mod, node))
                if reg:
                    self.m.cg_escaped_regs.add(reg)
                else:  # module.func used as a value (callback, target=, registry entry): may run later
                    for callee, _k in self.m.resolve_callee(self.mod, node, self.cls, reference=True):
                        self.add(callee, "conditional")
                        self.m.cg_escaped.add(callee)
        self.generic_visit(node)


# =============================================================== mapper
class Mapper:
    def __init__(self, root, entries, cwd=".", req_files=(), extra_paths=(), excludes=(),
                 use_installed=False):
        self.root = Path(root).resolve()
        self.root_posix = self.root.as_posix()
        cwd = (cwd or ".").replace("\\", "/")
        self.cwd = "" if cwd in ("", ".") else posixpath.normpath(cwd)
        self.entries = list(entries)
        self.extra_paths = list(extra_paths)
        self.excludes = list(excludes)
        self.modules = {}
        self.queue = []
        self.search_paths = []
        self.inserted = {}
        self.syspath_sites = []
        self.facts = []
        self.fact_index = {}
        self.mod_edges = []
        self.ext_edges = []
        self.groups = []
        self.attr_tokens = {}
        self.resource_links = []
        self.unresolved_dynamic = []
        self.discovered_pkgs = set()
        self.deploy_refs = set()
        self.node_req = {}
        self.cg_funcs = {}
        self.code_changes = []
        self.warnings = []
        self.hints = {}
        self.third_party_tops = set()
        self.https_seen = False
        self.call_folds = []         # folded argument values at every call to (maybe) project code
        self.touched = None          # facts added/merged during a bound re-visit
        self.bound_done = {}         # function node -> binding signatures already re-visited
        self.seed_urls = []          # URLs found in SQL seed / fixture data (data-driven endpoint candidates)
        self.files = []
        self._dircache = {}
        self._basenames = None
        self.req_files = [Path(r) if Path(r).is_absolute() else self.root / r for r in req_files]
        if not self.req_files and (self.root / "requirements.txt").is_file():
            self.req_files = [self.root / "requirements.txt"]
        self.requirements, self.indexes = [], []
        for rf in self.req_files:
            reqs, idx = self.parse_requirements(rf, set())
            self.requirements += reqs
            self.indexes += idx
        self.installed_map = {}
        if use_installed:
            try:
                from importlib.metadata import packages_distributions
                self.installed_map = packages_distributions()
            except Exception:
                self.warn("--use-installed needs Python 3.10+; ignored")

    # ---------------------------------------------------------- utilities
    def warn(self, msg):
        if msg not in self.warnings:
            self.warnings.append(msg)

    def rel(self, path):
        p = Path(path)
        try:
            return p.resolve().relative_to(self.root).as_posix()
        except ValueError:
            return p.resolve().as_posix()

    def excluded(self, rel):
        return any(fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(rel + "/", pat) for pat in self.excludes)

    def listdir(self, p):
        k = str(p)
        if k not in self._dircache:
            try:
                self._dircache[k] = set(os.listdir(p))
            except OSError:
                self._dircache[k] = set()
        return self._dircache[k]

    def isfile(self, p):
        return p.name in self.listdir(p.parent) and p.is_file()

    def isdir(self, p):
        return p.name in self.listdir(p.parent) and p.is_dir()

    def case_mismatch(self, top):
        for base in self.search_paths:
            names = self.listdir(base)
            for cand in (top, top + ".py"):
                if cand in names:
                    return None
            for n in names:
                if n.lower() in (top.lower(), top.lower() + ".py") and n not in (top, top + ".py"):
                    return self.rel(base / n)
        return None

    def find_by_basename(self, name):
        if self._basenames is None:
            self._basenames = {}
            for dp, dns, fns in os.walk(self.root):
                dns[:] = [d for d in dns if d not in JUNK_DIRS]
                for fn in fns:
                    self._basenames.setdefault(fn, []).append(self.rel(Path(dp) / fn))
        return self._basenames.get(name, [])

    def add_search_path(self, d, front=False, site=None):
        d = d.resolve()
        if d in self.search_paths:
            return
        if front:
            self.search_paths.insert(0, d)
        else:
            self.search_paths.append(d)
        if site:
            self.inserted[d] = site

    def canon(self, mod, node):
        d = dotted(node)
        if d is None:
            return None
        head, _, rest = d.partition(".")
        tgt = mod.aliases.get(head)
        if tgt:
            return tgt + ("." + rest if rest else "")
        return d

    # ---------------------------------------------------------- module resolution
    def _walk(self, base, parts, ns_first):
        cur, chain, names = base, [], []
        for i, part in enumerate(parts):
            names.append(part)
            name = ".".join(names)
            pkg = cur / part
            if self.isdir(pkg) and self.isfile(pkg / "__init__.py"):
                chain.append(("pkg", pkg / "__init__.py", name))
                cur = pkg
                continue
            f = cur / (part + ".py")
            if self.isfile(f):
                chain.append(("mod", f, name))
                if i != len(parts) - 1:
                    return None
                cur = None
                continue
            if self.isdir(pkg) and (i > 0 or ns_first):
                chain.append(("ns", pkg, name))
                cur = pkg
                continue
            return None
        return chain

    def resolve(self, dotted_name, search=None):
        if not dotted_name or dotted_name.startswith("."):
            return None, None
        parts = dotted_name.split(".")
        bases = search if search is not None else self.search_paths
        for ns_pass in (False, True):
            for base in bases:
                chain = self._walk(base, parts, ns_pass)
                if chain:
                    return chain, base
        return None, None

    def rel_base(self, mod, level):
        parts = mod.package.split(".") if mod.package else []
        up = level - 1
        if not parts or up >= len(parts):
            return None
        return ".".join(parts[: len(parts) - up])

    def lookup_module(self, dotted_name, from_mod):
        if not dotted_name:
            return None
        if dotted_name.startswith("."):
            level = len(dotted_name) - len(dotted_name.lstrip("."))
            base_pkg = self.rel_base(from_mod, level)
            if base_pkg is None:
                return None
            full = ".".join(x for x in (base_pkg, dotted_name.lstrip(".")) if x)
            chain, base = self.resolve(full, [from_mod.base])
        else:
            chain, base = self.resolve(dotted_name)
        if not chain or chain[-1][0] == "ns":
            return None
        _, p, name = chain[-1]
        return self.get_or_load(p, name, base, "local")

    def list_submodules(self, pkgname, leafpre=""):
        if not pkgname:
            return []
        chain, _ = self.resolve(pkgname)
        if not chain:
            return []
        kind, p, _ = chain[-1]
        d = p.parent if kind == "pkg" else (p if kind == "ns" else None)
        if d is None:
            return []
        out = []
        for n in sorted(self.listdir(d)):
            child = d / n
            if n.endswith(".py") and n != "__init__.py" and child.is_file():
                leaf = n[:-3]
            elif child.is_dir() and (child / "__init__.py").is_file():
                leaf = n
            else:
                continue
            if leaf.isidentifier() and leaf.startswith(leafpre):
                out.append((f"{pkgname}.{leaf}", leaf))
        return out

    def dotted_for_dir(self, d):
        for base in self.search_paths:
            try:
                relp = d.relative_to(base)
            except ValueError:
                continue
            parts = list(relp.parts)
            if parts and all(x.isidentifier() for x in parts):
                return ".".join(parts)
        return None

    def name_for(self, path):
        for base in self.search_paths:
            try:
                relp = path.relative_to(base)
            except ValueError:
                continue
            parts = list(relp.with_suffix("").parts)
            if parts and parts[-1] == "__init__":
                parts = parts[:-1]
            if parts and all(x.isidentifier() for x in parts):
                return ".".join(parts), base
        return path.stem, path.parent

    def get_or_load(self, path, name, base, kind, is_entry=False):
        path = Path(path).resolve()
        r = self.rel(path)
        m = self.modules.get(r)
        if m is not None:
            m.names.add(name)
            return m
        m = Mod(path, r, name, Path(base).resolve() if base else path.parent, kind, is_entry)
        self.modules[r] = m
        try:
            src = path.read_text(encoding="utf-8", errors="replace")
            m.tree = ast.parse(src, filename=str(path))
        except SyntaxError as e:
            m.parse_error = f"SyntaxError line {e.lineno}: {e.msg}"
            self.warn(f"{r}: {m.parse_error} (file not analysed)")
            return m
        except OSError as e:
            m.parse_error = str(e)
            return m
        m.aliases = collect_aliases(m.tree)
        collect_consts(m.tree.body, m.consts)
        m.docstrings = collect_docstrings(m.tree)
        return m

    @staticmethod
    def pickle_globals(path, limit=64 * 1024 * 1024):
        """{module: {names}} referenced by a pickle file, or by the *.pkl inside a torch zip archive."""
        import pickletools
        import zipfile
        try:
            blobs = []
            if zipfile.is_zipfile(path):
                with zipfile.ZipFile(path) as z:
                    for zi in z.infolist():
                        if zi.filename.endswith(".pkl") and zi.file_size <= limit:
                            blobs.append(z.read(zi))
            elif path.stat().st_size <= limit:
                blobs.append(path.read_bytes())
        except (OSError, zipfile.BadZipFile, RuntimeError):
            return None
        out, ok = {}, False
        for b in blobs:
            pushed, memo = [], {}
            try:
                for op, arg, _pos in pickletools.genops(b):
                    n = op.name
                    if n in ("PROTO", "FRAME", "STOP", "MARK", "POP", "POP_MARK"):
                        pass
                    elif n == "MEMOIZE":
                        memo[len(memo)] = pushed[-1] if pushed else None
                    elif n in ("PUT", "BINPUT", "LONG_BINPUT"):
                        memo[arg] = pushed[-1] if pushed else None
                    elif n in ("GET", "BINGET", "LONG_BINGET"):
                        pushed.append(memo.get(arg))
                    elif n in ("GLOBAL", "INST") and isinstance(arg, str) and " " in arg:
                        mod, name = arg.split(" ", 1)
                        out.setdefault(mod, set()).add(name)
                        pushed.append(None)
                    elif n == "STACK_GLOBAL":
                        if len(pushed) >= 2 and isinstance(pushed[-2], str) and isinstance(pushed[-1], str):
                            out.setdefault(pushed[-2], set()).add(pushed[-1])
                        pushed.append(None)
                    else:
                        pushed.append(arg if isinstance(arg, str) else None)
                ok = True
            except Exception:
                ok = ok or bool(out)
        return out if ok or out else None

    def add_mod_edge(self, src, dst, cap="yes", kind="import", q=None, tags=None):
        self.mod_edges.append([src.rel, dst.rel, cap, kind, q, tags or []])
        return len(self.mod_edges) - 1

    def enqueue(self, mod):
        if not mod.visited and mod not in self.queue:
            self.queue.append(mod)

    def dist_for(self, top, full):
        found = None
        for k in (full, ".".join(full.split(".")[:2]), top):
            if k in IMPORT_TO_DIST:
                found = IMPORT_TO_DIST[k]
                break
        declared = [r["name"] for r in self.requirements]
        for v in VARIANTS.get(top, []):
            if v in declared:
                return v, "requirements"
        if found:
            return found, "table"
        if self.installed_map.get(top):
            return norm_dist(self.installed_map[top][0]), "installed_metadata"
        nd = norm_dist(top)
        return nd, ("requirements" if nd in declared else "guess")

    def dist_imports(self, dist):
        tops = {k.split(".")[0] for k, d in IMPORT_TO_DIST.items() if d == dist}
        tops |= {t for t, vs in VARIANTS.items() if dist in vs}
        tops.add(dist.replace("-", "_"))
        tops.add(dist)
        for t, ds in self.installed_map.items():
            if any(norm_dist(x) == dist for x in ds):
                tops.add(t)
        return tops

    def classify_path(self, fv):
        t = fv.t.replace("\\", "/")
        o = fv.origin
        mech = ("importlib_resources" if "resources" in o else "file_relative_path" if "file" in o
                else "env_path" if "env" in o else "string_literal")
        loc = "project"
        root = self.root_posix
        if t == root or t.startswith(root + "/"):
            rel = t[len(root):].lstrip("/") or "."
            rel = posixpath.normpath(rel)
        elif t.startswith("<cwd>"):
            rel = posixpath.normpath(posixpath.join(self.cwd or ".", t[5:].lstrip("/")))
            mech = "cwd_relative_path"
        elif t.startswith("/") or re.match(r"^[A-Za-z]:/", t):
            rel, loc = posixpath.normpath(t), "absolute"
        elif t.startswith("~"):
            rel, loc = t, "home"
        elif t.startswith("<"):
            rel, loc = t, "unknown"
        else:
            rel = posixpath.normpath(posixpath.join(self.cwd, t)) if self.cwd else posixpath.normpath(t)
            if mech == "string_literal":
                mech = "cwd_relative_path"
        if loc == "project" and rel.startswith("../"):
            loc = "outside_project"
        exists = (self.root / rel).exists() if loc == "project" and "<" not in rel else None
        ext = posixpath.splitext(rel)[1].lower()
        if rel.startswith("<") and rel.endswith(">") and rel.count("<") == 1:
            evidence = "unresolved"
        else:
            evidence = "static_partial" if ("<" in rel or not fv.exact) else "static"
        return {"subject": rel, "loc": loc, "exists": exists, "ext": ext, "mech": mech,
                "evidence": evidence, "env": list(fv.envs)}

    # ---------------------------------------------------------- facts
    def add_fact(self, mod, node, category, subject, mechanism, ctx_req, evidence, confidence, detail,
                 site_meta=None, **extra):
        line = getattr(node, "lineno", None) if node is not None else None
        file = mod.rel if mod is not None else extra.pop("file", None)
        site = {"file": file, "line": line}
        if site_meta:
            site.update(site_meta)
        for f in self.fact_index.get((category, subject, file), []):
            if line is not None and not any(x.get("line") == line for x in f["sites"]):
                f["sites"].append(site)
            for piece in (detail.split("; ") if detail else []):
                if piece and piece not in f["detail"].split("; "):
                    f["detail"] = (f["detail"] + "; " + piece).strip("; ")
            f["_ctx_req"] = req_max(f["_ctx_req"], ctx_req)
            for k, v in extra.items():   # a later site may know more (e.g. a size folded from bound params)
                if k != "context" and v not in (None, [], "") and f.get(k) in (None, [], ""):
                    f[k] = v
            if self.touched is not None:
                self.touched.append((f, line))
            return f
        f = {"category": category, "subject": subject, "mechanism": mechanism,
             "sites": [site] if file else [],
             "evidence": evidence, "confidence": confidence, "detail": detail,
             "_mod": mod.rel if mod is not None else None, "_ctx_req": ctx_req}
        f.update(extra)
        self.facts.append(f)
        self.fact_index.setdefault((category, subject, file), []).append(f)
        if self.touched is not None:
            self.touched.append((f, line))
        return f

    def derived(self, category, subject, mechanism, detail, required="no", sites=None, confidence="high",
                **extra):
        f = {"category": category, "subject": subject, "mechanism": mechanism, "sites": sites or [],
             "evidence": "derived", "confidence": confidence, "detail": detail, "_mod": None,
             "_ctx_req": required, "required_at_runtime": required}
        f.update(extra)
        self.facts.append(f)
        return f

    # ---------------------------------------------------------- requirements
    def parse_requirements(self, path, seen):
        out, idx = [], []
        p = Path(path)
        if not p.is_file() or p.resolve() in seen:
            if not p.is_file():
                self.warn(f"requirements file not found: {path}")
            return out, idx
        seen.add(p.resolve())
        relf = self.rel(p)
        for i, raw in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            s = raw.strip()
            if not s or s.startswith("#"):
                continue
            egg = re.search(r"#egg=([A-Za-z0-9_.\-]+)", s)
            line = s.split(" #", 1)[0].strip()
            if line.startswith(("-r ", "--requirement")):
                inc = line.split(None, 1)[1].strip() if " " in line else ""
                sub, si = self.parse_requirements(p.parent / inc, seen)
                out += sub
                idx += si
            elif line.startswith(("--index-url", "-i ", "--extra-index-url", "--find-links", "-f ")):
                idx.append(line)
            elif egg:
                out.append({"name": norm_dist(egg.group(1)), "raw": s, "file": relf, "line": i})
            elif line.startswith("-") or "://" in line:
                continue
            else:
                mm = re.match(r"([A-Za-z0-9][A-Za-z0-9._\-]*)", line)
                if mm:
                    out.append({"name": norm_dist(mm.group(1)), "raw": s, "file": relf, "line": i})
        return out, idx

    # ---------------------------------------------------------- deploy hints
    def scan_hints(self):
        pat = re.compile(r"(?<![A-Za-z0-9_$])([A-Z][A-Z0-9_]{2,})\s*(?:=|:\s)\s*[\"']?([^\s\"'#,;\\]+)")
        globs = ("Dockerfile*", "*.dockerfile", "*compose*.yml", "*compose*.yaml", ".env", "*.env",
                 "*.sh", "*.md", "Makefile")
        for dp, dns, fns in os.walk(self.root):
            dns[:] = [d for d in dns if d not in JUNK_DIRS]
            for fn in fns:
                if fn.lower().endswith(".sql"):
                    self.scan_seed(Path(dp) / fn)
                if not any(fnmatch.fnmatch(fn, g) for g in globs):
                    continue
                p = Path(dp) / fn
                deploy_file = not fnmatch.fnmatch(fn, "*.md")
                try:
                    if p.stat().st_size > 1_000_000:
                        continue
                    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
                except OSError:
                    continue
                for i, line in enumerate(lines, 1):
                    if deploy_file:
                        for tok in re.findall(r"[\w.\-/]+", line.split("#", 1)[0]):
                            t = tok.lstrip("./")
                            if t and "/" in t or "." in t:
                                cand = self.root / t
                                if cand.is_file():
                                    self.deploy_refs.add(self.rel(cand))
                    for name, val in pat.findall(line):
                        val = val.strip("`'\"").rstrip("`.,:;)]}>").lstrip("`([{<")
                        if not val or val.startswith(("$", "<")) or "`" in val:
                            continue
                        where = f"{self.rel(p)}:{i}"
                        lst = self.hints.setdefault(name, [])
                        if all(v != val for v, _ in lst):
                            lst.append((val, where))

    def scan_seed(self, p):
        """URLs in SQL seed data: candidates for endpoints the code reads from a database."""
        try:
            if p.stat().st_size > 1_000_000:
                return
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return
        for i, line in enumerate(lines, 1):
            for scheme, rest in URL_RE.findall(line):
                if scheme.lower() in URL_SCHEMES:
                    u = mask_url(f"{scheme}://{rest}".rstrip("',);"))
                    if all(u != v for v, _ in self.seed_urls):
                        self.seed_urls.append((u, f"{self.rel(p)}:{i}"))

    def seed_note(self):
        if not self.seed_urls:
            return ""
        return ("; seed data lists " + ", ".join(u for u, _ in self.seed_urls[:4])
                + f" ({self.seed_urls[0][1]}) - a static hint, the runtime tracer confirms")

    # ---------------------------------------------------------- pipeline
    def run(self):
        self.scan_hints()
        entry_paths = []
        for e in self.entries:
            p = (self.root / e).resolve()
            if not p.is_file():
                self.warn(f"entrypoint not found: {e}")
                continue
            entry_paths.append(p)
            self.add_search_path(p.parent)
        for x in self.extra_paths:
            d = (self.root / x).resolve()
            if d.is_dir():
                self.add_search_path(d, site="--path")
        for p in entry_paths:
            m = self.get_or_load(p, "__main__", p.parent, "entry", is_entry=True)
            m.is_entry, m.kind, m.package = True, "entry", ""
            self.enqueue(m)
        self.drain()
        self.bind_params()          # re-read functions with their parameters bound to the callers' values
        self.select_candidates()
        self.unreached_pass()
        self.build_call_graph()
        self.propagate(self.registry_edges(precise=False))  # pass 1: over-approximate reachability
        for _ in range(6):
            before = dict(self.branch_state)
            self.solve_params()                              # literal values each parameter can take
            self.eval_branches()                             # which branches those values select
            self.propagate(self.registry_edges(precise=True))  # prune what never runs, then repeat:
            if self.branch_state == before:                  # calls inside dead branches no longer count
                break
        self.finalize_required()
        self.drop_write_target_refs()
        self.scan_files()
        self.derive_dangling()
        self.derive_shadowing()
        self.derive_packaging()
        self.derive_os_data()
        self.derive_write_roots()
        self.assign_ids()
        return self.output()

    def drain(self):
        while self.queue:
            mod = self.queue.pop(0)
            if mod.visited:
                continue
            mod.visited = True
            if mod.tree is not None:
                ModuleVisitor(self, mod, follow=True).run()
            while self.resource_links:
                s, t = self.resource_links.pop()
                if s in self.modules and t in self.modules and s != t:
                    self.mod_edges.append([s, t, "conditional", "resources", None, []])
                    self.enqueue(self.modules[t])

    # ---------------------------------------------------------- parameter binding
    @staticmethod
    def placeholder(f):
        return str(f["subject"]).startswith("<") or f["evidence"] == "unresolved"

    def bind_params(self, rounds=4, max_combos=6):
        """download(url) called as download(f"{HUB}/m.onnx"): re-read download() with url bound to that value.

        Facts found this way replace the '<var>' / '<unresolved>' facts of the unbound first read at the same
        line - but only when every caller's value is known (otherwise both stay)."""
        fam = lambda c: "file" if c in ("model_file", "config_file", "data_file_read") else c
        fully_bound = set()
        for _ in range(rounds):
            self.index_defs()
            refs = self.value_refs()
            binds = {}
            for c in self.call_folds:
                if not c["bound"] and c["from"] in fully_bound:
                    continue  # superseded by the calls recorded while re-reading that function bound
                mod = self.modules.get(c["mod"])
                if mod is None or mod.unreached:
                    continue
                for callee, kind in self.resolve_callee(mod, c["func"], c["cls"]):
                    fn = self.cg_funcs.get(callee[1], {}).get(callee[2])
                    if kind != "direct" or fn is None:
                        continue
                    ps = fn_param_names(fn, "." in callee[2])
                    ent = binds.setdefault(callee, {p: [] for p in ps + [a.arg for a in fn.args.kwonlyargs]})
                    if c["args"] is None:
                        for p in ent:
                            ent[p].append(None)
                        continue
                    given = dict(zip(ps, c["args"]))
                    given.update({k: v for k, v in c["kws"].items() if k in ent})
                    for p in ent:
                        ent[p].append(given.get(p, "DEFAULT"))
            did = False
            for callee, ent in binds.items():
                bind, complete = {}, True
                for p, vals in ent.items():
                    if all(v == "DEFAULT" for v in vals):
                        continue  # never passed: the default the first read used is right
                    known = []
                    for v in vals:
                        if v is None or v == "DEFAULT" or re.fullmatch(r"<[^<>]*>", v.t):
                            complete = False
                        elif all((v.t, v.exact) != (k.t, k.exact) for k in known):
                            known.append(v)
                    if known:
                        bind[p] = known
                    else:
                        complete = False
                if not bind:
                    continue
                names = sorted(bind)
                combos = [[]]
                for p in names:
                    combos = [c + [(p, v)] for c in combos for v in bind[p]][:max_combos]
                done = self.bound_done.setdefault(callee, set())
                for combo in combos:
                    sig = tuple((p, v.t, v.exact) for p, v in combo)
                    if sig in done:
                        continue
                    done.add(sig)
                    did = True
                    fn = self.cg_funcs[callee[1]][callee[2]]
                    # a decorated function or one passed around as a value also gets arguments we cannot see
                    exclusive = complete and not fn.decorator_list and callee[2].rsplit(".", 1)[-1] not in refs
                    self.revisit(callee, dict(combo), exclusive, fam)
                if complete:
                    fully_bound.add((callee[1], callee[2]))
            self.drain()
            if not did:
                break

    def value_refs(self):
        """Names used as values rather than called (callbacks, target=, registries)."""
        out = set()
        for m in self.modules.values():
            if m.tree is None or not m.visited or m.unreached:
                continue
            called = {id(n.func) for n in ast.walk(m.tree) if isinstance(n, ast.Call)}
            for n in ast.walk(m.tree):
                if id(n) in called or not isinstance(getattr(n, "ctx", None), ast.Load):
                    continue
                if isinstance(n, ast.Name):
                    out.add(n.id)
                elif isinstance(n, ast.Attribute):
                    out.add(n.attr)
        return out

    def revisit(self, callee, binding, complete, fam):
        _, rel, q = callee
        mod = self.modules[rel]
        fn = self.cg_funcs[rel][q]
        v = ModuleVisitor(self, mod, follow=True)
        v.bound = True
        cls, _, name = q.rpartition(".")
        if cls:
            v.ctx.append("class:" + cls)
        v.ctx.append("function:" + name)
        scope = {}
        pos = fn.args.posonlyargs + fn.args.args
        for a, dflt in zip(pos[len(pos) - len(fn.args.defaults):], fn.args.defaults):
            scope[a.arg] = dflt
        for a, dflt in zip(fn.args.kwonlyargs, fn.args.kw_defaults):
            if dflt is not None:
                scope[a.arg] = dflt
        scope.update(binding)
        v.scopes.append(scope)
        v.fn_depth = 1
        v.fn_stack.append(fn)
        self.touched = []
        try:
            v.visit_stmts(fn.body)
        finally:
            touched, self.touched = self.touched, None
        for f, _line in touched:
            if not self.placeholder(f):
                f["bound_from_callers"] = True
        if not complete:
            return
        resolved = {(fam(f["category"]), line) for f, line in touched if not self.placeholder(f)}
        for f in list(self.facts):
            if f.get("_mod") != rel or not self.placeholder(f):
                continue
            keep = [s for s in f["sites"] if (fam(f["category"]), s.get("line")) not in resolved]
            if len(keep) == len(f["sites"]):
                continue
            if keep:
                f["sites"] = keep
            else:
                self.facts.remove(f)
                lst = self.fact_index.get((f["category"], f["subject"], rel), [])
                if f in lst:
                    lst.remove(f)

    def config_tokens(self):
        toks = {}
        for f in self.facts:
            if f["category"] in ("config_file", "data_file_read") and f.get("exists"):
                p = self.root / f["subject"]
                if p.suffix.lower() not in CONFIG_EXT or not p.is_file():
                    continue
                try:
                    text = p.read_text(encoding="utf-8", errors="replace")[:200_000]
                except OSError:
                    continue
                for tok in re.findall(r"[A-Za-z_][A-Za-z0-9_.]*", text):
                    for piece in {tok, tok.rsplit(".", 1)[-1]}:
                        toks.setdefault(piece, f["subject"])
        return toks

    def select_candidates(self):
        cfg = self.config_tokens()
        hint = {}
        for name, vals in self.hints.items():
            for v, where in vals:
                for tok in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", v):
                    hint.setdefault(tok, where)
        for g in self.groups:
            if g["package"] in self.discovered_pkgs:
                for c in g["cands"]:
                    c["fact"]["mechanism"] = "pkgutil_discovery"
                    c["fact"]["_force"] = None
                    if c["edge"] is not None:
                        self.mod_edges[c["edge"]][2] = "yes"
                continue
            if g["kind"] == "getattr":
                pool = {t: "attribute access in code" for t in self.attr_tokens.get(g["package"], set())}
            else:
                pool = dict(hint)
                pool.update(cfg)
            sel = [c for c in g["cands"] if c["leaf"] in pool]
            if not sel:
                for c in g["cands"]:
                    c["fact"]["detail"] += "; no selector found in config/deploy files - any candidate may load"
                continue
            for c in g["cands"]:
                if c in sel:
                    c["fact"]["mechanism"] = "importlib_from_config" if g["kind"] == "fstring" else "module_getattr"
                    c["fact"]["detail"] = f"selected: '{c['leaf']}' appears in {pool[c['leaf']]}"
                    c["fact"]["_force"] = None
                    if c["edge"] is not None:
                        self.mod_edges[c["edge"]][2] = "yes"
                else:
                    c["fact"]["detail"] = "candidate NOT selected by config - loads only if the config changes"
                    c["fact"]["_force"] = "no"
                    c["fact"]["unselected"] = True
                    if c["edge"] is not None:
                        self.mod_edges[c["edge"]][2] = "no"
                        self.modules[self.mod_edges[c["edge"]][1]].unselected = True

    def walk_project(self):
        for dp, dns, fns in os.walk(self.root):
            d = Path(dp)
            dns[:] = sorted(x for x in dns if x not in JUNK_DIRS and not self.excluded(self.rel(d / x)))
            for fn in sorted(fns):
                p = d / fn
                r = self.rel(p)
                if not self.excluded(r):
                    yield p, r

    def unreached_pass(self):
        for p, r in self.walk_project():
            if not r.endswith(".py"):
                continue
            m = self.modules.get(r)
            if m is not None and m.visited:
                continue
            if m is None:
                name, base = self.name_for(p.resolve())
                m = self.get_or_load(p, name, base, "unreached")
            m.visited = True
            m.unreached = True
            if m.tree is not None:
                ModuleVisitor(self, m, follow=False, unreached=True).run()
        self.resource_links.clear()

    # ---------------------------------------------------------- call graph
    def index_defs(self):
        self.cg_funcs, self.cg_classes, self.cg_methods, self.call_edges = {}, {}, {}, []
        self.cg_registries, self.cg_escaped, self.cg_bases = {}, set(), {}
        mods = [m for m in self.modules.values() if m.visited and not m.unreached and m.tree is not None]
        for m in mods:
            fs, cs = {}, {}
            for st in m.tree.body:
                self._collect_defs(m, st, fs, cs)
            self.cg_funcs[m.rel], self.cg_classes[m.rel] = fs, cs
        return mods

    def build_call_graph(self):
        mods = self.index_defs()
        self.cg_registry_targets, self.cg_lookups = {}, []
        self.cg_callsites, self.cg_escaped_regs = [], set()
        self.param_vals, self.branch_state = {}, {}
        for rel, regs in self.cg_registries.items():
            mod = self.modules[rel]
            for name, r in regs.items():
                targets = {}
                for key, vnode in r["items"]:
                    res = self.resolve_callee(mod, vnode, None)
                    if res:
                        targets[key] = res
                        for callee, _k in res:
                            self.cg_escaped.add(callee)  # called with arguments we cannot see
                self.cg_registry_targets[(rel, name)] = targets
        for m in mods:
            CallGraphBuilder(self, m).run()

    def _collect_defs(self, m, st, fs, cs):
        if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
            fs[st.name] = st
            if st.decorator_list or st.name in MODULE_DUNDERS:
                # decorated (routes, registries, CLI) or module dunder: called by a framework, not by our code
                node = ("F", m.rel, st.name)
                self.call_edges.append((m.rel, None, node, "conditional", []))
                self.cg_escaped.add(node)
        elif isinstance(st, ast.ClassDef):
            meths = set()
            for b in st.body:
                if isinstance(b, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    q = f"{st.name}.{b.name}"
                    fs[q] = b
                    meths.add(b.name)
                    self.cg_methods.setdefault(b.name, []).append(("F", m.rel, q))
                    decos = [dotted(d) or "" for d in b.decorator_list]
                    if decos and not all(d in ("staticmethod", "classmethod") for d in decos):
                        node = ("F", m.rel, q)
                        self.call_edges.append((m.rel, None, node, "conditional", []))
                        self.cg_escaped.add(node)
            cs[st.name] = meths
            self.cg_bases[(m.rel, st.name)] = [b for b in (self.canon(m, x) for x in st.bases) if b]
            if st.decorator_list and "__init__" in meths:
                # @register("x") class ...: instantiated by whatever the decorator registers it with
                node = ("F", m.rel, f"{st.name}.__init__")
                self.call_edges.append((m.rel, None, node, "conditional", []))
                self.cg_escaped.add(node)
        elif ((isinstance(st, ast.Assign) and len(st.targets) == 1 and isinstance(st.targets[0], ast.Name)
               and registry_dict(st.value))
              or (isinstance(st, ast.AnnAssign) and isinstance(st.target, ast.Name) and registry_dict(st.value))):
            # REGISTRY = {"scrfd": ScrfdDetector, ...}: wired up only where it is looked up
            name = st.targets[0].id if isinstance(st, ast.Assign) else st.target.id
            self.cg_registries.setdefault(m.rel, {})[name] = {
                "node": st, "items": list(zip([k.value for k in st.value.keys], st.value.values))}
        elif isinstance(st, (ast.If, ast.Try)) or type(st).__name__ == "TryStar":
            subs = list(st.body) + list(getattr(st, "orelse", []))
            for h in getattr(st, "handlers", []):
                subs += h.body
            for sub in subs:
                self._collect_defs(m, sub, fs, cs)

    def _class_targets(self, rel, cname):
        out = []
        for mname in self.cg_classes.get(rel, {}).get(cname, ()):
            if mname == "__init__":
                out.append((("F", rel, f"{cname}.__init__"), "direct"))
            elif mname in DUNDER_IMPLICIT:
                out.append((("F", rel, f"{cname}.{mname}"), "cha"))
        worker, run = self._worker_run(rel, cname)
        if worker and run:
            out.append((run, "start"))
        return out

    def _base_class(self, rel, name):
        """A base-class name as written in module rel (already alias-expanded) -> (rel, class) in the project."""
        if name in self.cg_classes.get(rel, {}):
            return rel, name
        mod = self.modules.get(rel)
        modname, _, cname = name.rpartition(".")
        if mod is None or not modname:
            return None
        target = self.lookup_module(modname, mod)
        if target is not None and cname in self.cg_classes.get(target.rel, {}):
            return target.rel, cname
        return None

    def _worker_run(self, rel, cname, depth=0):
        """(derives from a Process / Thread class, the run() it would start: its own or a project base's)."""
        if depth > 5:
            return False, None
        own = ("F", rel, f"{cname}.run") if "run" in self.cg_classes.get(rel, {}).get(cname, ()) else None
        worker, inherited = False, None
        for b in self.cg_bases.get((rel, cname), ()):
            if b in WORKER_BASES:
                worker = True
                continue
            hit = self._base_class(rel, b)
            if hit and hit != (rel, cname):
                w, r = self._worker_run(hit[0], hit[1], depth + 1)
                worker, inherited = worker or w, inherited or r
        return worker, own or inherited

    def _resolve_dotted(self, mod, dotted_name, depth=0):
        if depth > 5:
            return []
        lead = len(dotted_name) - len(dotted_name.lstrip("."))
        parts = dotted_name[lead:].split(".")
        for cut in (1, 2):
            if len(parts) < cut or (len(parts) == cut and not lead):
                continue
            modname = "." * lead + ".".join(parts[:-cut])
            target = self.lookup_module(modname, mod)
            if target is None or target.rel not in self.cg_funcs:
                continue
            if cut == 1:
                a = parts[-1]
                if a in self.cg_funcs[target.rel]:
                    return [(("F", target.rel, a), "direct")]
                if a in self.cg_classes.get(target.rel, {}):
                    return self._class_targets(target.rel, a)
                alias = target.aliases.get(a)  # re-exported name: follow it to where it is defined
                if alias:
                    r = self._resolve_dotted(target, alias, depth + 1)
                    if r:
                        return r
            else:
                q = f"{parts[-2]}.{parts[-1]}"
                if q in self.cg_funcs[target.rel]:
                    return [(("F", target.rel, q), "direct")]
                alias = target.aliases.get(parts[-2])
                if alias:
                    r = self._resolve_dotted(target, f"{alias}.{parts[-1]}", depth + 1)
                    if r:
                        return r
        return []

    def resolve_callee(self, mod, func, cls, reference=False):
        funcs = self.cg_funcs.get(mod.rel, {})
        if isinstance(func, ast.Name):
            n = func.id
            if n in funcs:
                return [(("F", mod.rel, n), "direct")]
            if n in self.cg_classes.get(mod.rel, {}):
                return self._class_targets(mod.rel, n)
            tgt = mod.aliases.get(n)
            if tgt and "." in tgt.strip("."):
                return self._resolve_dotted(mod, tgt)
            return []
        if not isinstance(func, ast.Attribute):
            return []
        if reference:   # module.func passed as a value: resolvable by name only, never by method-name guessing
            canon = self.canon(mod, func)
            return self._resolve_dotted(mod, canon) if canon and "." in canon else []
        if isinstance(func.value, ast.Name) and func.value.id in ("self", "cls") and cls:
            q = f"{cls}.{func.attr}"
            if q in funcs:
                return [(("F", mod.rel, q), "direct")]
        canon = self.canon(mod, func)
        if canon:
            r = self._resolve_dotted(mod, canon)
            if r:
                return r
        name = func.attr
        if name.startswith("__") and name.endswith("__"):
            return []
        cands = self.cg_methods.get(name, [])
        if 0 < len(cands) <= 8:  # obj.method(): any project method with that name may run
            return [(c, "cha") for c in cands]
        return []

    def eval_lr(self, tags, is_entry, import_site=False):
        """Requirement contributed by branches/guards/nesting, with literal-argument branch pruning."""
        r, fn_seen, cls_seen = "yes", False, False
        for t in tags:
            k = tag_kind(t)
            if k in ("type_checking", "dead", "unreached"):
                return "no"
            if k == "main_guard":
                if not is_entry:
                    return "no"
            elif k == "branch":
                state = self.branch_state.get(t[7:]) if t.startswith("branch@") else None
                if state == "dead":
                    return "no"
                if state != "always":
                    r = req_min(r, "conditional")
            elif k in ("platform", "nested"):
                r = req_min(r, "conditional")
            elif k in ("optional_import", "fallback_import"):
                if import_site:
                    r = req_min(r, "conditional")
            elif k == "function":
                if fn_seen or t == "function:<lambda>":
                    r = req_min(r, "conditional")
                fn_seen = True
            elif k == "class":
                if fn_seen or cls_seen:
                    r = req_min(r, "conditional")
                cls_seen = True
        return r

    def fn_params(self, node):
        fn = self.cg_funcs.get(node[1], {}).get(node[2])
        if fn is None:
            return [], {}
        ps = fn_param_names(fn, "." in node[2]) + [a.arg for a in fn.args.kwonlyargs]
        defaults = {}
        full = fn.args.posonlyargs + fn.args.args
        lit = lambda d: isinstance(d, ast.Constant) and isinstance(d.value, (str, int)) and not isinstance(d.value, bool)
        for a, d in zip(full[len(full) - len(fn.args.defaults):], fn.args.defaults):
            if lit(d):
                defaults[a.arg] = d.value
        for a, d in zip(fn.args.kwonlyargs, fn.args.kw_defaults):
            if d is not None and lit(d):
                defaults[a.arg] = d.value
        return ps, defaults

    def _node_key(self, rel, q):
        return ("F", rel, q) if q in self.cg_funcs.get(rel, {}) else ("M", rel)

    def solve_params(self):
        """Which literal values can each parameter take? (only when every live caller passes literals)"""
        TOP = "TOP"
        vals, live = {}, []
        for c in self.cg_callsites:
            rel, q = c["caller"]
            m = self.modules.get(rel)
            if m is None or self.node_req.get(self._node_key(rel, q), "no") == "no":
                continue
            if self.eval_lr(c["tags"], m.is_entry) == "no":
                continue
            live.append(c)
            for p in self.fn_params(c["callee"])[0]:
                vals.setdefault((c["callee"], p), set())
        for node in self.cg_escaped:
            for p in self.fn_params(node)[0]:
                vals[(node, p)] = TOP
        for _ in range(50):
            changed = False
            for c in live:
                callee, amap = c["callee"], c["args"]
                rel, q = c["caller"]
                ps, defaults = self.fn_params(callee)
                for p in ps:
                    cur = vals.get((callee, p), set())
                    if cur == TOP:
                        continue
                    if amap == "TOP":
                        new = TOP
                    else:
                        v = amap.get(p)
                        if v is None:
                            new = {defaults[p]} if p in defaults else TOP
                        elif v[0] == "lit":
                            new = {v[1]}
                        elif v[0] == "param" and q in self.cg_funcs.get(rel, {}):
                            new = vals.get((("F", rel, q), v[1]), TOP)
                            if new != TOP and not new:
                                continue  # caller's own values not known yet
                            if new != TOP and len(v) > 2 and v[2]:
                                new = {apply_tr(x, v[2]) for x in new}
                        else:
                            new = TOP
                    if new == TOP:
                        vals[(callee, p)] = TOP
                        changed = True
                    elif not new <= cur:
                        vals[(callee, p)] = cur | new
                        changed = True
            if not changed:
                break
        self.param_vals = vals

    def _lits(self, n):
        if isinstance(n, ast.Constant):
            return {n.value}
        if isinstance(n, ast.Name):  # a known registry: `name in REGISTRY` tests its keys
            reg = self.cg_registries.get(getattr(self, "_cur_rel", None), {}).get(n.id)
            if reg is not None:
                return {k for k, _v in reg["items"]}
        if isinstance(n, (ast.Tuple, ast.List, ast.Set)) and all(isinstance(e, ast.Constant) for e in n.elts):
            return {e.value for e in n.elts}
        return None

    def _derive_base(self, expr, env):
        e, tr = unwrap_tr(expr)
        if isinstance(e, ast.Name) and e.id in env:
            return e.id, tr
        return None

    def _param_test(self, t, env):
        """(tracked name, values for which the test is true) when the test only compares a known value."""
        if isinstance(t, ast.UnaryOp) and isinstance(t.op, ast.Not):
            r = self._param_test(t.operand, env)
            return (r[0], env[r[0]] - r[1]) if r else None
        if isinstance(t, ast.BoolOp):
            parts = [self._param_test(v, env) for v in t.values]
            if parts and all(parts) and len({p for p, _ in parts}) == 1:
                sets = [x for _, x in parts]
                return parts[0][0], (set().union(*sets) if isinstance(t.op, ast.Or) else set.intersection(*sets))
            return None
        if isinstance(t, ast.Compare) and len(t.ops) == 1:
            left, right, op = t.left, t.comparators[0], t.ops[0]
            base = self._derive_base(left, env)
            if base is None and isinstance(op, (ast.Eq, ast.NotEq)):
                base = self._derive_base(right, env)
                if base is not None:
                    left, right = right, left
            if base is None:
                return None
            if isinstance(op, (ast.In, ast.NotIn)) and isinstance(right, ast.Constant):
                return None  # substring test
            lits = self._lits(right)
            if lits is None:
                return None
            name, tr = base
            V = env[name]
            if isinstance(op, (ast.Eq, ast.In, ast.Is)):
                return name, {v for v in V if apply_tr(v, tr) in lits}
            if isinstance(op, (ast.NotEq, ast.NotIn, ast.IsNot)):
                return name, {v for v in V if apply_tr(v, tr) not in lits}
        return None

    def _pattern_values(self, pat):
        k = type(pat).__name__
        if k == "MatchValue":
            return {pat.value.value} if isinstance(pat.value, ast.Constant) else None
        if k == "MatchSingleton":
            return {pat.value}
        if k == "MatchAs":
            return "ALL" if pat.pattern is None else self._pattern_values(pat.pattern)
        if k == "MatchOr":
            parts = [self._pattern_values(x) for x in pat.patterns]
            if any(x is None for x in parts):
                return None
            if any(x == "ALL" for x in parts):
                return "ALL"
            return set().union(*parts)
        return None

    def _walk_branches(self, stmts, env):
        env = dict(env)
        for i, st in enumerate(stmts):
            if isinstance(st, (ast.Assign, ast.AnnAssign)):
                tgt = (st.targets[0] if isinstance(st, ast.Assign) and len(st.targets) == 1
                       else getattr(st, "target", None))
                if isinstance(tgt, ast.Name):
                    base = self._derive_base(st.value, env) if st.value is not None else None
                    if base:
                        env[tgt.id] = {apply_tr(v, base[1]) for v in env[base[0]]}
                    else:
                        env.pop(tgt.id, None)
                else:
                    for n in assigned_names([st]):
                        env.pop(n, None)
                continue
            if isinstance(st, ast.If):
                cond = self._param_test(st.test, env)
                if cond:
                    p, taken = cond
                    V = env[p]
                    rest = V - taken
                    self.branch_state[f"{id(st.test)}:1"] = "dead" if not taken else ("always" if taken == V else "maybe")
                    self.branch_state[f"{id(st.test)}:0"] = "dead" if not rest else ("always" if rest == V else "maybe")
                    if taken:
                        self._walk_branches(st.body, {**env, p: taken})
                    if rest:
                        self._walk_branches(st.orelse, {**env, p: rest})
                    if not st.orelse and terminates(st.body):
                        if rest:
                            self._walk_branches(stmts[i + 1:], {**env, p: rest})
                        return
                else:
                    self._walk_branches(st.body, env)
                    self._walk_branches(st.orelse, env)
            elif type(st).__name__ == "Match":
                base = self._derive_base(st.subject, env)
                V = {apply_tr(v, base[1]) for v in env[base[0]]} if base else None
                remaining = set(V) if V is not None else None
                for case in st.cases:
                    key = f"{id(case)}:1"
                    if remaining is None:
                        self._walk_branches(case.body, env)
                        continue
                    pv = self._pattern_values(case.pattern)
                    taken = set(remaining) if pv == "ALL" else (None if pv is None else {v for v in remaining if v in pv})
                    if taken is None or case.guard is not None:
                        self.branch_state[key] = "maybe" if (taken is None or taken) else "dead"
                        if self.branch_state[key] != "dead":
                            self._walk_branches(case.body, env)
                        continue
                    self.branch_state[key] = "dead" if not taken else ("always" if taken == V else "maybe")
                    if taken:
                        self._walk_branches(case.body, env)
                    remaining -= taken
            elif isinstance(st, (ast.For, ast.AsyncFor, ast.While)):
                self._walk_branches(st.body, env)
                self._walk_branches(st.orelse, env)
            elif isinstance(st, (ast.With, ast.AsyncWith)):
                self._walk_branches(st.body, env)
            elif isinstance(st, ast.Try) or type(st).__name__ == "TryStar":
                self._walk_branches(st.body, env)
                for h in st.handlers:
                    self._walk_branches(h.body, env)
                self._walk_branches(st.orelse, env)
                self._walk_branches(st.finalbody, env)
            if not isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                for n in assigned_names([st]) & set(env):  # reassigned inside a block: value no longer known
                    env.pop(n, None)

    def eval_branches(self):
        self.branch_state = {}
        for rel, funcs in self.cg_funcs.items():
            for qual, fn in funcs.items():
                node = ("F", rel, qual)
                env = {}
                for p in self.fn_params(node)[0]:
                    v = self.param_vals.get((node, p), "TOP")
                    if v != "TOP" and v:
                        env[p] = v
                if env:
                    self._cur_rel = rel
                    self._walk_branches(fn.body, env)

    def registry_edges(self, precise):
        out = []
        for lk in self.cg_lookups:
            targets = self.cg_registry_targets.get(lk["reg"], {})
            cap = lk["cap"]
            if lk["key"] is not None:
                keys = [lk["key"]] if lk["key"] in targets else []
            elif lk["param"] and precise and lk["q"] in self.cg_funcs.get(lk["rel"], {}):  # noqa
                V = self.param_vals.get((("F", lk["rel"], lk["q"]), lk["param"]), "TOP")
                if V == "TOP" or not V:
                    keys, cap = list(targets), req_min(cap, "conditional")
                else:
                    V = {apply_tr(x, lk.get("tr", ())) for x in V}
                    keys = [k for k in targets if k in V]
            else:
                keys, cap = list(targets), req_min(cap, "conditional")
            for k in keys:
                for callee, kind in targets[k]:
                    out.append((lk["rel"], lk["q"], callee, cap if kind == "direct" else req_min(cap, "conditional"),
                                lk["tags"]))
        for reg in self.cg_escaped_regs:  # registry used some other way: every entry may run
            for k, res in self.cg_registry_targets.get(reg, {}).items():
                for callee, _kind in res:
                    out.append((reg[0], None, callee, "conditional", []))
        return out

    def propagate(self, extra_edges=()):
        funcs = self.cg_funcs

        def src_node(rel, q, lr):
            if q is None:
                return ("M", rel), lr
            if q in funcs.get(rel, {}):
                return ("F", rel, q), lr
            return ("M", rel), req_min(lr, "conditional")

        edges = []
        for src, dst, cap, _kind, q, tags in self.mod_edges:
            sm = self.modules.get(src)
            if sm is None or sm.unreached:
                continue
            sn, l2 = src_node(src, q, req_min(cap, self.eval_lr(tags, sm.is_entry, True)))
            edges.append((sn, ("M", dst), l2))
        for rel, q, callee, cap, tags in list(self.call_edges) + list(extra_edges):
            m = self.modules.get(rel)
            sn, l2 = src_node(rel, q, req_min(cap, self.eval_lr(tags, bool(m and m.is_entry))))
            edges.append((sn, callee, l2))
        req = {("M", m.rel): "yes" for m in self.modules.values() if m.is_entry and m.visited}
        changed = True
        while changed:
            changed = False
            for sn, tn, lr in edges:
                sr = req.get(sn)
                if sr is None:
                    continue
                c = req_min(sr, lr)
                if REQ[c] > REQ[req.get(tn, "no")]:
                    req[tn] = c
                    changed = True
        for m in self.modules.values():
            m.req = "no" if m.unreached else req.get(("M", m.rel), "no")
        self.node_req = req

    def finalize_required(self):
        for f in self.facts:
            if not f.get("_mod"):
                continue
            m = self.modules.get(f["_mod"])
            if m is None or m.unreached:
                f["required_at_runtime"] = "no"
                f["detail"] = (f["detail"] + "; in a file never imported from the entrypoint(s)").strip("; ")
                continue
            best, dead_fn, pruned = "no", None, False
            for x in f["sites"]:
                q = x.get("_q")
                lr = self.eval_lr(x.get("_t", []), m.is_entry, x.get("_imp", False))
                if lr == "no" and any(t.startswith("branch@") and self.branch_state.get(t[7:]) == "dead"
                                      for t in x.get("_t", [])):
                    pruned = True
                if q is None:
                    cr = m.req
                elif q in self.cg_funcs.get(m.rel, {}):
                    cr = req_min(m.req, self.node_req.get(("F", m.rel, q), "no"))
                    if cr == "no" and m.req != "no":
                        dead_fn = q
                else:
                    cr = req_min(m.req, "conditional")
                best = req_max(best, req_min(req_min(cr, lr), x.get("_cap", "yes")))
            if f.get("_force"):
                best = req_min(best, f["_force"])
            if (f["category"] in ("model_file", "config_file", "data_file_read") and "check" in f.get("ops", [])
                    and f.get("exists") is False and best != "no"):
                f["_pre_cap"] = best
                best = "no"
                f["detail"] = (f["detail"] + "; missing and only existence-checked: the code falls back").strip("; ")
            f["required_at_runtime"] = best
            if best == "no" and dead_fn:
                f["detail"] = (f["detail"] + f"; inside {dead_fn}(), which nothing reachable calls").strip("; ")
            elif best == "no" and pruned:
                f["detail"] = (f["detail"] + "; in a branch the literal arguments never select").strip("; ")

    # ---------------------------------------------------------- derived facts
    def used_data_paths(self):
        used = set()
        for f in self.facts:
            if (f["category"] in ("model_file", "config_file", "data_file_read", "secret")
                    and f.get("exists") and f.get("required_at_runtime") != "no"):
                used.add(f["subject"])
        return used

    def drop_write_target_refs(self):
        writes = {f["subject"] for f in self.facts if f["category"] == "file_write"}
        self.facts = [f for f in self.facts if not (
            f["category"] in ("config_file", "model_file", "data_file_read") and f.get("op") in ("ref", "ref_literal")
            and f["subject"] in writes)]

    def scan_files(self):
        used = self.used_data_paths()
        caution = ""
        if self.unresolved_dynamic:
            caution = ("; CAUTION: unresolved dynamic imports at " + ", ".join(self.unresolved_dynamic[:5])
                       + " - confirm with the runtime tracer before .dockerignore")
        hashes, entries = {}, []
        for dp, dns, fns in os.walk(self.root):
            d = Path(dp)
            for x in list(dns):
                rx = self.rel(d / x)
                if x in JUNK_DIRS and x != "__pycache__" and not self.excluded(rx):
                    self.derived("unused_asset", rx + "/", "junk_dir",
                                 "IDE/VCS/cache folder - .dockerignore it", sites=[{"file": rx + "/", "line": None}])
            dns[:] = sorted(x for x in dns if x not in JUNK_DIRS and not self.excluded(self.rel(d / x)))
            for fn in sorted(fns):
                p = d / fn
                r = self.rel(p)
                if self.excluded(r):
                    continue
                try:
                    size = p.stat().st_size
                except OSError:
                    continue
                status = self.file_status(r, fn, p.suffix.lower(), used)
                entry = {"path": r, "size": size, "status": status}
                if 32 <= size <= 512 * 1024 * 1024 and status != "junk":
                    try:
                        h = sha256(p)
                        entry["sha256"] = h
                        hashes.setdefault(h, []).append(r)
                    except OSError:
                        pass
                self.files.append(entry)
                entries.append(entry)
        unused = {"code_unreached", "code_not_needed", "unreferenced"}
        blocking = {"code_used", "data_used", "deploy_used", "key_material"}
        dirs = {}
        for e in entries:
            parts = e["path"].split("/")[:-1]
            for i in range(1, len(parts) + 1):
                dirs.setdefault("/".join(parts[:i]), []).append(e)
        root_dirs = {self.rel(b) for b in self.search_paths}
        whole = set()
        for dpath, es in dirs.items():
            n_unused = sum(1 for e in es if e["status"] in unused)
            if (n_unused >= 2 and not any(e["status"] in blocking for e in es) and dpath not in root_dirs):
                whole.add(dpath)
        top_whole = {x for x in whole if not any(x.startswith(o + "/") for o in whole)}
        for dpath in sorted(top_whole):
            files = [e["path"] for e in dirs[dpath] if e["status"] in unused]
            self.derived("unused_asset", dpath + "/", "unused_folder",
                         f"whole folder unused ({len(files)} files, e.g. {', '.join(files[:3])})" + caution,
                         sites=[{"file": dpath + "/", "line": None}], paths=files)
        for e in entries:
            r, status = e["path"], e["status"]
            if any(r.startswith(x + "/") for x in top_whole):
                continue
            site = [{"file": r, "line": None}]
            if status == "code_unreached":
                self.derived("unused_asset", r, "unreached_code", "never imported from the entrypoint(s)" + caution,
                             sites=site)
            elif status == "code_not_needed":
                m = self.modules.get(r)
                why = ("dynamic-import candidate not selected by config" if getattr(m, "unselected", False)
                       else "only imported under TYPE_CHECKING / dead code / uncalled functions")
                self.derived("unused_asset", r, "not_needed_at_runtime", why + caution, sites=site)
            elif status == "unreferenced":
                self.derived("unused_asset", r, "unreferenced_file", "not referenced by any reached code" + caution,
                             sites=site)
            elif status == "key_material":
                self.derived("secret", r, "key_material_in_context",
                             "key/cert file in the build context - 'COPY . .' bakes it into the image",
                             required="conditional", sites=site)
        if any(e["status"] == "junk" for e in self.files):
            self.derived("unused_asset", "**/__pycache__/ + *.pyc", "junk_files",
                         "compiled/IDE junk - .dockerignore it")
        for h, paths in hashes.items():
            if len(paths) > 1:
                paths = sorted(paths)
                self.derived("duplicate_asset", paths[0], "sha256",
                             "identical content: " + ", ".join(paths[1:]),
                             sites=[{"file": x, "line": None} for x in paths], paths=paths)

    def is_meta(self, fn):
        return bool(META_RE.match(fn))

    def file_status(self, r, fn, ext, used):
        if fn in JUNK_FILES or ext in (".pyc", ".pyo"):
            return "junk"
        if ext == ".py" and r in self.deploy_refs and not (self.modules.get(r) and self.modules[r].visited
                                                           and not self.modules[r].unreached):
            return "deploy_used"
        if ext == ".py":
            m = self.modules.get(r)
            if m is not None and m.visited and not m.unreached:
                return "code_not_needed" if m.req == "no" else "code_used"
            return "code_unreached"
        if r in used:
            return "data_used"
        if r in self.deploy_refs:
            return "deploy_used"
        if ext in KEY_EXT:
            return "key_material"
        if self.is_meta(fn):
            return "build_or_docs"
        return "unreferenced"

    def derive_dangling(self):
        for f in list(self.facts):
            if (f["category"] in ("model_file", "config_file", "data_file_read") and f.get("exists") is False
                    and f.get("op") in ("read", "check", "ref") and f["evidence"] == "static"
                    and "/" in f["subject"]):
                req = f.get("required_at_runtime", "conditional")
                if "check" in f.get("ops", []) and req == "no":
                    detail = ("referenced but not present in the project - the code checks for it first and "
                              "falls back, so it won't crash")
                else:
                    detail = ("referenced but not present in the project - read without an existence check: "
                              "expect a crash when this code runs")
                self.derived("dangling_reference", f["subject"], "missing_file", detail, required=req,
                             sites=[{"file": x.get("file"), "line": x.get("line")} for x in f["sites"]])

    def derive_shadowing(self):
        tops = self.third_party_tops
        sp = set(self.search_paths)
        imports = {}
        for f in self.facts:
            if (f["category"] in ("local_module", "dynamic_import") and f.get("resolved_path")
                    and f.get("required_at_runtime") in ("yes", "conditional") and DOTTED_RE.match(str(f["subject"]))):
                imports.setdefault(f["subject"].split(".")[0], []).append(f)
        if imports:
            index = {}
            for p, r in self.walk_project():
                if p.suffix == ".py":
                    index.setdefault(p.stem, set()).add(r)
                    for anc in p.relative_to(self.root).parents:
                        if anc.name:
                            index.setdefault(anc.name, set()).add(anc.as_posix() + "/")
            for top, fs in sorted(imports.items()):
                chain, _ = self.resolve(top)
                if not chain:
                    continue
                kind, p0, _ = chain[0]
                winner = self.rel(p0.parent if kind == "pkg" else p0) + ("/" if kind in ("pkg", "ns") else "")
                others = sorted(x for x in index.get(top, ()) if x != winner and not x.startswith(winner))
                others = [x for x in others if not (x.endswith("/") and winner.startswith(x))]
                if not others:
                    continue
                req = max((f.get("required_at_runtime", "no") for f in fs), key=lambda x: REQ[x])
                self.derived("shadowing", top, "duplicate_name",
                             f"'import {top}' loads {winner} (first on sys.path); other copies in the project: "
                             + ", ".join(others[:5]) + " - a different WORKDIR or sys.path would load another copy",
                             required=req, sites=[x for f in fs for x in f["sites"]][:3],
                             resolved_path=winner.rstrip("/"), others=others)
        for p, r in self.walk_project():
            if not r.endswith(".py"):
                continue
            stem = p.stem
            if stem == "__init__":
                continue
            if stem in tops:
                self.derived("shadowing", r, "same_name_as_package",
                             (f"local file has the same name as the third-party package '{stem}' used by this "
                              "project: pipreqs drops that package from requirements, and if this folder ever "
                              "lands on sys.path it hides the real package"),
                             required="conditional", sites=[{"file": r, "line": None}])
            elif stem in STDLIB and p.parent.resolve() in sp:
                self.derived("shadowing", r, "shadows_stdlib",
                             f"'{stem}.py' sits on sys.path and hides the standard-library module '{stem}'",
                             required="yes", sites=[{"file": r, "line": None}])

    def derive_packaging(self):
        needed, sites = {}, {}
        unreached_users = {}
        for f in self.facts:
            top = f.get("import_name")
            if f["category"] in ("third_party_import", "dynamic_import") and top and top not in STDLIB:
                ctx = f.get("context", [])
                if "unreached" in ctx:
                    unreached_users.setdefault(top, set()).update(s["file"] for s in f["sites"])
                    continue
                if "optional_import" in ctx or "fallback_import" in ctx:
                    continue
                r = f.get("required_at_runtime", "no")
                needed[top] = req_max(needed.get(top, "no"), r)
                if r != "no":
                    sites.setdefault(top, []).extend(f["sites"])
        declared = {}
        for r in self.requirements:
            declared.setdefault(r["name"], r)
        provides = {d: self.dist_imports(d) for d in declared}
        rf = ", ".join(self.rel(x) for x in self.req_files) or "requirements"
        for top, r in sorted(needed.items()):
            if r == "no" or any(top in provides[d] for d in declared):
                continue
            dist, _ = self.dist_for(top, top)
            self.derived("packaging", dist, "missing_requirement",
                         (f"imported ({r}) but not declared in {rf} - it may only be installed transitively "
                          "today; declare it explicitly"), required=r, sites=sites.get(top, [])[:5],
                         import_name=top)
        for d, rq in declared.items():
            rs = [needed[t] for t in provides[d] if t in needed]
            site = [{"file": rq["file"], "line": rq["line"]}]
            if not rs:
                users = sorted(set().union(*[unreached_users.get(t, set()) for t in provides[d]]))
                extra = f" (only in never-imported files: {', '.join(users)})" if users else ""
                self.derived("packaging", d, "orphan_requirement",
                             "declared but never imported by reached code" + extra, sites=site)
            elif max(rs, key=lambda x: REQ[x]) == "no":
                self.derived("packaging", d, "not_needed_at_runtime",
                             (f"only imported under TYPE_CHECKING / dead code / unselected paths - but another "
                              f"package may depend on it: check `pipdeptree -r -p {d}` before removing"),
                             sites=site, confidence="medium")
        live = lambda f: f.get("required_at_runtime") in ("yes", "conditional")
        gui_yes = any(f["category"] == "gui_usage" and f.get("required_at_runtime") == "yes" for f in self.facts)
        gui_any = any(f["category"] == "gui_usage" and live(f) for f in self.facts)
        gpu_yes = bool(self.gpu_required())
        for top, variants in VARIANTS.items():
            ds = [d for d in declared if d in variants]
            if len(ds) < 2:
                continue
            if top == "cv2":
                keep = next((d for d in ds if "headless" not in d), ds[0]) if gui_yes else \
                    next((d for d in ds if "headless" in d), ds[0])
            elif top == "psycopg2":
                keep = next((d for d in ds if "binary" in d), ds[0])
            elif top == "onnxruntime":
                keep = next((d for d in ds if "gpu" in d), ds[0]) if gpu_yes else \
                    next((d for d in ds if d == "onnxruntime"), ds[0])
            else:
                keep = ds[0]
            for d in ds:
                if d == keep:
                    req, why = "yes", f"keep {d}"
                else:
                    req = "conditional" if (top == "cv2" and "headless" not in d and gui_any) else "no"
                    why = f"remove {d}, keep {keep}"
                    if req == "conditional":
                        why += " - unless the GUI code paths are enabled in production"
                self.derived("packaging", d, "dual_distribution",
                             f"{' + '.join(ds)} all install '{top}' into the same folder (the last one installed "
                             f"wins): {why}", required=req,
                             sites=[{"file": declared[d]["file"], "line": declared[d]["line"]}], import_name=top)
        for d, rq in declared.items():
            if d in SOURCE_BUILD:
                self.derived("packaging", d, "source_build", SOURCE_BUILD[d] + " (verify: pip install -v)",
                             required="yes", sites=[{"file": rq["file"], "line": rq["line"]}], confidence="medium")

    def derive_os_data(self):
        live = lambda f: f.get("required_at_runtime") in ("yes", "conditional")
        if self.https_seen:
            self.derived("os_data", "ca-certificates", "https_client",
                         "HTTPS requests need CA certificates in the image", required="conditional")
        if any(f["category"] == "third_party_import" and f.get("import_name") == "matplotlib" and live(f)
               for f in self.facts):
            self.derived("os_data", "writable-home:matplotlib", "cache_dir",
                         "matplotlib writes a font cache to $HOME or $MPLCONFIGDIR - must be writable when non-root",
                         required="conditional")

    def derive_write_roots(self):
        """OUTPUT_ROOT = os.getenv("OUTPUT_ROOT", "/data/output") with writes below it -> the folder to mount."""
        live = lambda f: f.get("required_at_runtime") in ("yes", "conditional")
        writes = [f for f in self.facts if f["category"] == "file_write" and live(f)]
        existing = {f["subject"] for f in self.facts if f["category"] == "file_write"}
        envs = [f for f in self.facts if f["category"] == "env_var" and f.get("default") and live(f)]
        done = set()
        for e in envs:
            d = e["default"].rstrip("/")
            if e["subject"] in done or not d.startswith(("/", "~")) or posixpath.splitext(d)[1] or d in existing:
                continue
            under = [w for w in writes if e["subject"] in (w.get("path_envs") or [])
                     and (w["subject"] == d or w["subject"].startswith(d + "/"))]
            if not under:
                continue
            wmods = {w.get("_mod") for w in under}
            same = [x for x in envs if x["subject"] == e["subject"] and x.get("_mod") in wmods]
            src = same[0] if same else e
            done.add(e["subject"])
            req = max((w["required_at_runtime"] for w in under), key=lambda x: REQ[x])
            self.derived("file_write", d, "env_path",
                         (f"write root from env {e['subject']} (code default {d}): {len(under)} write(s) below it, "
                          f"e.g. {', '.join(w['subject'] for w in under[:3])} - mount one volume here"),
                         required=req, sites=[{"file": s["file"], "line": s["line"]} for s in src["sites"]],
                         path_envs=[e["subject"]], location="absolute" if d.startswith("/") else "home")

    def gpu_required(self, live_only=True):
        """GPU use with no CPU path. Probes (torch.cuda.is_available) and GPU env vars never count."""
        return [f for f in self.facts if f["category"] == "gpu_usage" and f.get("required_at_runtime") == "yes"
                and f.get("mechanism") != "gpu_probe" and not str(f.get("mechanism", "")).startswith("env")]

    # ---------------------------------------------------------- output
    def assign_ids(self):
        order = lambda f: ((f["sites"][0]["file"] or "") if f["sites"] else "~",
                           (f["sites"][0]["line"] or 0) if f["sites"] else 0, f["category"], str(f["subject"]))
        self.facts.sort(key=order)
        for i, f in enumerate(self.facts, 1):
            f["id"] = f"S-{i:04d}"
            f["docker_implication"] = self.implication(f)
            f.pop("_mod", None)
            f.pop("_ctx_req", None)
            f.pop("_force", None)
            f.pop("_pre_cap", None)
            for x in f["sites"]:
                if x.get("_cap"):
                    x["cap"] = x["_cap"]     # never more required than this (fallback / unused value)
                for k in ("_q", "_lr", "_t", "_imp", "_cap"):
                    x.pop(k, None)
            f["context"] = [t for t in f.get("context", []) if t != "unreached"] if f.get("context") else []

    @staticmethod
    def implication(f):
        c, s, r = f["category"], f["subject"], f.get("required_at_runtime")
        if r == "no" and c not in ("unused_asset", "duplicate_asset", "packaging", "shadowing", "secret"):
            return "not needed at runtime (per static analysis)"
        if c in ("local_module", "dynamic_import"):
            rp = f.get("resolved_path")
            return f"COPY {rp}" if rp else ("requirements: " + f["dist"] if f.get("dist") else "verify at runtime")
        if c == "third_party_import":
            if f.get("mechanism") == "try_except_optional":
                return f"optional: the code falls back if {s} is missing"
            return f"requirements: {s}"
        if c == "stdlib_import":
            return "standard library - check the note in detail"
        if c in ("model_file", "config_file", "data_file_read"):
            if f.get("location") == "project":
                return f"COPY {s}" + (" (missing in project!)" if f.get("exists") is False else "")
            return "must exist in the container: bake at build or mount"
        if c == "file_write":
            return f"volume / bind mount: {write_dir(s)}"
        if c == "env_var":
            return f"ENV / compose environment: {s}" + (f" (code default {f['default']})" if f.get("default") else "")
        if c == "secret":
            return "never bake: env var / Docker secret / runtime mount"
        if c == "hardcoded_config":
            return "externalize to env/config (code change)"
        if c == "network_endpoint":
            return f"reach {s} by compose service name; keep internal services unpublished"
        if c == "listen_port":
            return f"EXPOSE / ports: {f.get('port', '?')} (must bind 0.0.0.0)"
        if c == "subprocess_binary":
            return f"apt: {f.get('apt_candidate') or s} (binary must exist in the image)"
        if c == "native_library":
            return f"apt candidate: {f.get('apt_candidate') or 'unknown'} (verify with ldd)"
        if c == "system_package_candidate":
            return f"apt: {s} (verify)"
        if c == "gui_usage":
            return "needs GUI OpenCV + display; if never enabled in production -> headless"
        if c == "gpu_usage":
            return "GPU base image + NVIDIA toolkit, or confirm the CPU fallback works"
        if c == "multiprocessing":
            return "check shm_size, OMP_NUM_THREADS and memory limits"
        if c == "ipc_shared_memory":
            return "compose shm_size above the total segment size"
        if c == "thread_config":
            return "ENV OMP_NUM_THREADS=1 (etc.) when running many processes"
        if c == "hardware_identity":
            return "pin mac_address / hostname in compose"
        if c == "os_data":
            return "install in the image (tzdata / fonts / ca-certificates) or make the dir writable"
        if c == "device":
            return f"compose devices: {s}"
        if c == "runtime_download":
            return "bake at build time or mount a cache volume; enable offline mode"
        if c == "unused_asset":
            return f".dockerignore {s}"
        if c == "duplicate_asset":
            return "keep one copy; .dockerignore the rest"
        if c == "dangling_reference":
            return "file referenced but missing - verify the fallback or the path"
        if c == "shadowing":
            return "import resolution depends on sys.path/WORKDIR - verify which file loads"
        if c == "packaging":
            return {"missing_requirement": f"requirements: add {s}",
                    "orphan_requirement": f"requirements: remove {s}",
                    "not_needed_at_runtime": f"requirements: probably remove {s} (verify transitive users)",
                    "dual_distribution": (f"requirements: keep {s}" if r == "yes" else f"requirements: remove {s}"),
                    "source_build": "compiles at install: needs build tools or a prebuilt variant"}.get(
                f.get("mechanism"), "requirements: review")
        return ""

    def python_version(self):
        for p, r in self.walk_project():
            name = p.name.lower()
            if name.startswith("dockerfile") or name.endswith(".dockerfile"):
                try:
                    m = re.search(r"(?im)^\s*FROM\s+(?:--platform=\S+\s+)?(?:\S+/)?python:(\d+\.\d+)", p.read_text(errors="replace"))
                except OSError:
                    m = None
                if m:
                    return m.group(1), self.rel(p)
            if name in (".python-version", "runtime.txt"):
                try:
                    m = re.search(r"(\d+\.\d+)", p.read_text(errors="replace"))
                except OSError:
                    m = None
                if m:
                    return m.group(1), self.rel(p)
        return None, None

    def recommendations(self):
        live = lambda f: f.get("required_at_runtime") in ("yes", "conditional")
        by = {}
        for f in self.facts:
            by.setdefault(f["category"], []).append(f)
        declared = {r["name"] for r in self.requirements}
        gpu_live = [f for f in by.get("gpu_usage", []) if live(f)]
        gpu_yes = self.gpu_required()
        probes = [f for f in gpu_live if f.get("mechanism") == "gpu_probe"]
        torch_live = any(f.get("import_name") == "torch" and live(f) for f in by.get("third_party_import", []))
        ver, ver_src = self.python_version()
        slim = f"python:{ver}-slim" if ver else "python:<ver>-slim"
        if gpu_yes:
            base = ("nvidia/cuda:<ver>-cudnn-runtime-<os> (runtime, not devel), matching torch's CUDA" if torch_live
                    else "a CUDA runtime base: GPU required with no CPU fallback")
        elif gpu_live:
            why = (f"the code probes with {probes[0]['subject']} and falls back to CPU" if probes
                   else "CPU fallback listed")
            base = f"{slim} - GPU is optional ({why}), so no CUDA base needed"
        else:
            base = f"{slim} - no GPU usage found"
        if ver_src:
            base += f" (Python {ver} from {ver_src})"
        pk = by.get("packaging", [])
        removes = {f["subject"] for f in pk if f["mechanism"] == "orphan_requirement"}
        removes |= {f["subject"] for f in pk if f["mechanism"] == "dual_distribution" and f["required_at_runtime"] != "yes"}
        swaps, notes = [], []
        gui_live = [f for f in by.get("gui_usage", []) if live(f)]
        for d, h in GUI_TO_HEADLESS.items():
            if d in declared and d not in removes:
                if not gui_live:
                    swaps.append((d, h, "no GUI calls reachable"))
                else:
                    notes.append(f"{d}: GUI calls found at " + ", ".join(
                        f"{x['file']}:{x['line']}" for g in gui_live[:3] for x in g["sites"][:1])
                        + " - headless breaks them unless they are never enabled in production")
            elif d in declared and gui_live:
                notes.append(f"{d} removed in favour of {h}: the GUI calls at " + ", ".join(
                    f"{x['file']}:{x['line']}" for g in gui_live[:3] for x in g["sites"][:1])
                    + " must stay disabled in production")
        for d, b in BINARY_SWAP.items():
            if d in declared:
                swaps.append((d, b, "prebuilt wheel, no compiler"))
        cpu_swaps = []
        if not gpu_yes and not any("download.pytorch.org/whl/cpu" in i for i in self.indexes):
            for r in self.requirements:
                if r["name"] in ("torch", "torchvision", "torchaudio") and r["name"] not in removes:
                    pin = r["raw"].split(" #", 1)[0].strip()
                    cpu_swaps.append(f"{pin} -> same version from the CPU index "
                                     "(pip install --index-url https://download.pytorch.org/whl/cpu): no required "
                                     "GPU use, and default PyPI wheels pull multi-GB CUDA libraries")
        kept = (declared - removes - {d for d, _, _ in swaps}) | {b for _, b, _ in swaps}
        apt = {}
        for d in sorted(kept):
            for a in APT_BY_DIST.get(d, []):
                apt.setdefault(a, f"from {d} (verify with ldd)")
        for f in by.get("subprocess_binary", []):
            if live(f) and f.get("apt_candidate"):
                apt.setdefault(f["apt_candidate"], f"binary '{f['subject']}'")
        for f in by.get("system_package_candidate", []):
            if live(f):
                apt.setdefault(f["subject"], "native library")
        for f in by.get("os_data", []):
            if live(f):
                sub = f["subject"]
                if sub.startswith("tzdata"):
                    apt.setdefault("tzdata", "timezone data for zoneinfo (or pip tzdata)")
                elif sub.startswith("font:"):
                    apt.setdefault("fonts-dejavu-core", "font file " + sub[5:])
                elif sub == "ca-certificates":
                    apt.setdefault("ca-certificates", "HTTPS")
        reqs = {"add": [f["subject"] for f in pk if f["mechanism"] == "missing_requirement"],
                "remove": sorted(removes),
                "probably_remove": [f["subject"] for f in pk if f["mechanism"] == "not_needed_at_runtime"],
                "swap": [f"{a} -> {b} ({why})" for a, b, why in swaps] + cpu_swaps,
                "notes": notes}
        copy = set()
        for m in self.modules.values():
            if m.visited and not m.unreached and m.req != "no":
                copy.add(m.rel.split("/")[0] + ("/" if "/" in m.rel else ""))
        for f in self.facts:
            if (f["category"] in ("model_file", "config_file", "data_file_read") and live(f)
                    and f.get("exists") and f.get("location") == "project"):
                copy.add(f["subject"].split("/")[0] + ("/" if "/" in f["subject"] else ""))
        # .dockerignore: unused files, docs/build files, requirements files not in use, key material
        used_req = {self.rel(x) for x in self.req_files}
        ignore = {f["subject"] for f in by.get("unused_asset", [])}
        for e in self.files:
            if e["status"] == "build_or_docs":
                if e["path"].rsplit("/", 1)[-1].lower().startswith("requirements") and e["path"] in used_req:
                    continue
                ignore.add(e["path"])
        keyfiles = [e["path"] for e in self.files if e["status"] == "key_material"]
        by_dir = {}
        for k in keyfiles:
            by_dir.setdefault(posixpath.dirname(k), []).append(k)
        for d, ks in by_dir.items():
            in_dir = [e for e in self.files if posixpath.dirname(e["path"]) == d]
            if d and len(ks) == len(in_dir):
                ignore.add(d + "/")
            else:
                ignore.update(ks)
        env = []
        set_in_code = {f["subject"] for f in by.get("env_var", []) if f.get("mechanism") == "env_set"}
        set_in_code -= {f["subject"] for f in by.get("env_var", []) if f.get("mechanism") != "env_set"}
        for f in by.get("env_var", []):
            if (f["subject"] == "<unresolved>" or f["subject"] in set_in_code
                    or any(e["name"] == f["subject"] for e in env)):
                continue
            env.append({"name": f["subject"], "default_in_code": f.get("default"),
                        "required_at_runtime": f.get("required_at_runtime"),
                        "values_in_deploy_files": [f"{v} ({w})" for v, w in self.hints.get(f["subject"], [])][:3],
                        "secret": secretish(f["subject"])})
        # volumes: sibling folders collapse into their shared parent (/data/media/uploads + thumbnails -> /data/media)
        dirs = sorted({write_dir(f["subject"]) for f in by.get("file_write", []) if live(f)})
        parents = {}
        for v in dirs:
            parents.setdefault(posixpath.dirname(v), []).append(v)
        vols = set()
        for v in dirs:
            par = posixpath.dirname(v)
            vols.add(par if par not in ("", "/", ".") and len(parents[par]) >= 2 else v)
        vols = sorted(v for v in vols if not any(v != o and v.startswith(o + "/") for o in vols))
        # scratch/log dirs only need to exist and be writable (matters once the image runs as a non-root USER)
        scratch = ("/tmp", "/var/tmp", "/var/log", "/run", "/dev/shm")
        writable = sorted({v for v in dirs if v.startswith("/") and v != "."})
        vols = [v for v in vols if not any(v == x or v.startswith(x + "/") for x in scratch)]
        vols = [v + " (~ = the container user's home: /root when running as root)" if v.startswith("~") else v
                for v in vols]
        compose = {
            "ports": sorted({f.get("port") for f in by.get("listen_port", []) if live(f)} - {None}),
            "shm_size": self.shm_advice([f for f in by.get("ipc_shared_memory", []) if live(f)]),
            "mac_address": self.mac_advice([f for f in by.get("hardware_identity", []) if live(f)]),
            "devices": sorted({f["subject"] for f in by.get("device", []) if live(f)}),
            "gpus": ("reserve NVIDIA devices" if gpu_yes else
                     ("not needed - GPU optional (CPU fallback)" if gpu_live else None)),
            "depends_on": self.depends_on([f for f in by.get("network_endpoint", []) if live(f)]),
        }
        images = None
        if len(self.entries) > 1:
            images = {"count": 1, "why": (f"{len(self.entries)} entrypoints ({', '.join(self.entries)}) share one "
                                          "codebase and requirements: build one image, run one compose service "
                                          "per entrypoint with a different command")}
        changes = list(dict.fromkeys(self.code_changes))
        for f in by.get("dangling_reference", []):
            if f.get("required_at_runtime") == "yes" and f["sites"]:
                changes.append(f"{f['sites'][0]['file']}:{f['sites'][0]['line']}: {f['subject']} is missing and "
                               "read without a check")
        cautions = []
        if self.unresolved_dynamic:
            cautions.append("unresolved dynamic imports at " + ", ".join(self.unresolved_dynamic)
                            + " - the runtime tracer (Phase 2) must confirm what they load")
        cautions.append("static analysis cannot see which branches run: 'conditional' items need the runtime tracer")
        out = {"images": images, "base_image": base,
               "apt_candidates": [{"package": k, "why": v} for k, v in sorted(apt.items())],
               "requirements": reqs, "copy": sorted(copy), "dockerignore": sorted(ignore), "env": env,
               "secrets": [{"what": f["subject"], "where": f["sites"][0] if f["sites"] else None,
                            "detail": f["detail"]} for f in by.get("secret", [])],
               "volumes": vols,
               "writable_dirs": [f"{d} - must exist and be writable by the container user (create + chown it in the "
                                 "Dockerfile before switching to a non-root USER)" for d in writable
                                 if not any(d == v.split(" ")[0] or d.startswith(v.split(" ")[0] + "/") for v in vols)],
               "compose_runtime": compose, "code_changes_required": changes,
               "cautions": cautions}
        if images is None:
            del out["images"]
        return out

    @staticmethod
    def shm_advice(shm):
        if not shm:
            return None
        sizes = sorted({f["size_bytes"] for f in shm if f.get("size_bytes")})
        if not sizes:
            return "shared memory used, size unknown statically - measure /dev/shm usage and set shm_size above it"
        seg = max(sizes)
        envs = sorted({e for f in shm for e in f.get("size_envs", [])})
        n64 = -(-64 * 1048576 // seg)
        rec = 1 << max(6, (seg * max(n64, 4) * 2 - 1).bit_length() - 20)
        return (f"{seg / 1048576:.1f} MB per segment" + (f" (with the code defaults of {', '.join(envs)})" if envs else "")
                + (f"; {n64} or more segments exceed Docker's 64 MB default" if n64 > 1 else
                   "; one segment already exceeds Docker's 64 MB default")
                + f" - set shm_size above segments x {seg / 1048576:.1f} MB (e.g. {rec}mb for up to "
                + f"{rec * 1048576 // seg // 2} segments with 2x headroom)")

    @staticmethod
    def mac_advice(hw):
        if not hw:
            return None
        vals = sorted({str(f["value"]) for f in hw if f.get("value")})
        sw = sorted({e for f in hw for e in f.get("switch_env", [])})
        if vals:
            return f"\"{vals[0]}\" (the licensed MAC the code compares with)" + (
                f", or turn the check off via {', '.join(sw)} if that is what the switch does" if sw else "")
        if not any(f["subject"] in ("uuid.getnode", "getmac.get_mac_address", "uuid.uuid1") for f in hw):
            return None
        return "pin it (the code reads the MAC)"

    def depends_on(self, eps):
        hosts = {}
        for f in eps:
            h = str(f["subject"]).rsplit(":", 1)[0]
            if h.startswith("<") or h in ("localhost", "127.0.0.1", "0.0.0.0") or IPV4_RE.fullmatch(h) or "." in h:
                continue
            hosts.setdefault(h, f"{f['subject']} ({f['sites'][0]['file']}:{f['sites'][0]['line']})" if f["sites"]
                             else f["subject"])
        if any(f["subject"] == "<unresolved>" and f.get("mechanism") == "data_driven" for f in eps):
            for u, where in self.seed_urls:
                try:
                    h = urlsplit(u).hostname
                except ValueError:
                    h = None
                if h and not IPV4_RE.fullmatch(h) and h not in ("localhost",) and "." not in h:
                    hosts.setdefault(h, f"{u} in seed data {where} (endpoint read from the DB at runtime)")
        return [f"{h}: {why}" for h, why in sorted(hosts.items())]

    def graph(self):
        nodes, edges = {}, []

        def node(nid, label, typ, **meta):
            if nid not in nodes:
                nodes[nid] = {"id": nid, "label": label, "type": typ, **meta}
            return nid

        for m in self.modules.values():
            if m.visited:
                node("mod:" + m.rel, m.rel, "module", kind=("unreached" if m.unreached else m.kind),
                     required=("no" if m.unreached else m.req), name=m.name, entry=m.is_entry)
        seen = set()
        for src, dst, req, kind, *_rest in self.mod_edges:
            a, b = "mod:" + src, "mod:" + dst
            if a in nodes and b in nodes and a != b and (a, b) not in seen:
                seen.add((a, b))
                edges.append({"source": a, "target": b, "type": kind, "required": req, "evidence": "static"})
        for f in self.facts:
            c = f["category"]
            if c == "local_module" or (c == "dynamic_import" and f.get("resolved_path")
                                       and "mod:" + f["resolved_path"] in nodes):
                continue
            if c == "third_party_import":
                tid = node("pkg:" + f["subject"], f["subject"], "package")
            elif c == "stdlib_import":
                tid = node("std:" + f["subject"], f["subject"], "stdlib")
            else:
                tid = node(f"{c}:{f['subject']}", str(f["subject"]), c)
            for s in f["sites"]:
                fsrc = s.get("file")
                if not fsrc:
                    continue
                sid = "mod:" + fsrc
                if sid not in nodes:
                    if fsrc == f["subject"] or fsrc.rstrip("/") == str(f["subject"]).rstrip("/"):
                        continue
                    sid = node("file:" + fsrc, fsrc, "file")
                if sid != tid:
                    edges.append({"source": sid, "target": tid, "type": c, "required": f.get("required_at_runtime"),
                                  "evidence": f["evidence"], "fact": f["id"], "line": s.get("line")})
        for src, nid, req in self.ext_edges:
            if nid.startswith("std:") and "mod:" + src in nodes:
                node(nid, nid[4:], "stdlib")
                key = ("mod:" + src, nid)
                if key not in seen:
                    seen.add(key)
                    edges.append({"source": "mod:" + src, "target": nid, "type": "stdlib", "required": req,
                                  "evidence": "static"})
        return {"nodes": list(nodes.values()), "edges": edges}

    def output(self):
        counts = {}
        for f in self.facts:
            counts.setdefault(f["category"], 0)
            counts[f["category"]] += 1
        reached = [m for m in self.modules.values() if m.visited and not m.unreached and m.req != "no"]
        return {
            "tool": "xray-static", "version": VERSION, "python": sys.version.split()[0],
            "root": self.root_posix, "entrypoints": self.entries, "cwd": self.cwd or ".",
            "search_paths": [self.rel(p) for p in self.search_paths],
            "requirements_files": [self.rel(p) for p in self.req_files], "indexes": self.indexes,
            "summary": {
                "facts": len(self.facts), "by_category": dict(sorted(counts.items())),
                "by_evidence": {e: sum(1 for f in self.facts if f["evidence"] == e)
                                for e in ("static", "static_partial", "unresolved", "derived")},
                "by_required": {r: sum(1 for f in self.facts if f.get("required_at_runtime") == r)
                                for r in ("yes", "conditional", "no")},
                "modules_needed": len(reached),
                "modules_total": sum(1 for m in self.modules.values() if m.visited),
                "unresolved_dynamic_sites": self.unresolved_dynamic,
            },
            "facts": self.facts,
            "modules": [{"path": m.rel, "name": m.name, "kind": "unreached" if m.unreached else m.kind,
                         "required": "no" if m.unreached else m.req, "parse_error": m.parse_error}
                        for m in sorted(self.modules.values(), key=lambda x: x.rel) if m.visited],
            "files": self.files,
            "recommendations": self.recommendations(),
            "graph": self.graph(),
            "warnings": self.warnings,
        }


# =============================================================== cli
def main(argv=None):
    for _s in (sys.stdout, sys.stderr):   # Windows consoles / pipes: never crash on tree characters or paths
        if hasattr(_s, "reconfigure"):
            _s.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="X-Ray static mapper (Phase 1): map a Python project without running it.")
    ap.add_argument("--root", required=True, help="project root folder")
    ap.add_argument("--entry", action="append", required=True, help="entrypoint path relative to --root (repeatable)")
    ap.add_argument("--requirements", action="append", default=[],
                    help="requirements file(s), relative to --root (default: requirements.txt if present)")
    ap.add_argument("--cwd", default=".", help="working directory for cwd-relative paths, relative to --root "
                                               "(= the Dockerfile WORKDIR; default: the root)")
    ap.add_argument("--path", action="append", default=[], help="extra import search path, relative to --root")
    ap.add_argument("--exclude", action="append", default=[], help="glob to skip, e.g. 'tools/*' (repeatable)")
    ap.add_argument("--use-installed", action="store_true",
                    help="also map import names with the CURRENT environment's package metadata "
                         "(off by default: your host env may not be the container env)")
    ap.add_argument("--out", default="xray_static.json", help="output JSON path")
    args = ap.parse_args(argv)

    mapper = Mapper(args.root, args.entry, args.cwd, args.requirements, args.path, args.exclude, args.use_installed)
    out = mapper.run()
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=1, default=lambda o: sorted(o) if isinstance(o, (set, frozenset)) else str(o))
    s = out["summary"]
    print(f"X-Ray static {VERSION}: {s['facts']} facts, {s['modules_needed']} modules needed "
          f"({s['modules_total']} parsed)")
    print("  by evidence : " + ", ".join(f"{k}={v}" for k, v in s["by_evidence"].items()))
    print("  by required : " + ", ".join(f"{k}={v}" for k, v in s["by_required"].items()))
    print("  categories  : " + ", ".join(f"{k}={v}" for k, v in s["by_category"].items()))
    if s["unresolved_dynamic_sites"]:
        print("  unresolved dynamic sites: " + ", ".join(s["unresolved_dynamic_sites"]))
    for w in out["warnings"][:10]:
        print("  warning: " + w)
    print(f"-> {args.out}")


if __name__ == "__main__":
    main()
