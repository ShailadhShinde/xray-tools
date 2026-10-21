"""
X-Ray inputs: give a program in a container its inputs - without changing its code. Two settings, each optional:

XRAY_SOURCE - what every video source the program opens gets instead (cv2.VideoCapture: a camera number, an
    rtsp:// address, a video file). The run files fill it from SOURCE at their top:
        a video file      /xsource/lane 3.mp4           every source gets this video
        a folder          /xsource                      source 1 gets the 1st video in it, source 2 the 2nd ...
                                                        (by name; a source keeps its video when it reconnects)
        a stream          rtsp://user:pass@10.0.0.5/1   every source gets this stream
    Each swap is printed once per process:  [xray] video source: C:\\...\\lane.mp4 -> /xsource/lane 3.mp4

PC_PATHS - files the code names by a path of your PC (C:\\Users\\me\\Videos\\lane.mp4 in a config): the run file mounts
    each one, and this gives the code the container path. One or more  PC_PATH=CONTAINER_PATH  separated by |;
    everything below a folder maps too. Case and / against \\ do not matter (as on Windows):
        PC_PATHS=C:\\Users\\me\\Videos\\lane.mp4=/xin/pc_1/lane.mp4|D:\\data=/xin/pc_2
    Covered: cv2.VideoCapture (and .open), cv2.imread, cv2.imwrite, open(), os.path.exists / isfile / isdir /
    getsize, onnxruntime.InferenceSession.

With this folder on PYTHONPATH, Python loads this file at start - in every process the program starts, too. Both
settings empty: nothing is changed. Any path or source not covered is passed on as it is.
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
VIDEO_EXT = (".mp4", ".avi", ".mov", ".mkv", ".m4v", ".webm", ".mpg", ".mpeg", ".ts", ".wmv", ".flv", ".h264", ".h265")


def _chain_other_sitecustomize():
    """Python imports only the first sitecustomize on sys.path: run the next one (the image's own, if any). The
    folders of sitecustomize files already running (X-Ray's trace hook, this one) are skipped."""
    try:
        import importlib.machinery
        import importlib.util
        skip = {_HERE}
        first = getattr(sys.modules.get("sitecustomize"), "__file__", None)
        if first:
            skip.add(os.path.dirname(os.path.abspath(first)))
        paths = [p for p in sys.path if os.path.abspath(p or ".") not in skip]
        spec = importlib.machinery.PathFinder.find_spec("sitecustomize", paths)
        if spec and spec.origin and os.path.dirname(os.path.abspath(spec.origin)) not in skip:
            mod = importlib.util.module_from_spec(spec)
            sys.modules["_xray_inputs_chained_sitecustomize"] = mod
            spec.loader.exec_module(mod)
    except Exception as e:      # never break the program's start
        sys.stderr.write(f"[xray] could not run the image's own sitecustomize: {e!r}\n")


# ------------------------------------------------------------------ PC_PATHS
def _read_map():
    out = []
    for item in (os.environ.get("PC_PATHS") or "").split("|"):
        if "=" not in item:
            continue
        pc, cont = item.rsplit("=", 1)          # a container path X-Ray writes has no "="
        pc = pc.strip().replace("\\", "/").rstrip("/")
        if pc and cont.strip():
            out.append((pc.lower(), cont.strip().rstrip("/")))
    return sorted(out, key=lambda kv: -len(kv[0]))      # the longest PC path first


MAP = _read_map()


def to_container(p):
    """a PC path in PC_PATHS -> its container path; anything else unchanged"""
    if not MAP:
        return p
    try:
        s = os.fspath(p)
    except TypeError:
        return p
    if not isinstance(s, str) or not s:
        return p
    n = s.replace("\\", "/")
    low = n.lower()
    for pc, cont in MAP:
        if low == pc:
            return cont
        if low.startswith(pc + "/"):
            return cont + n[len(pc):]
    return p


# ------------------------------------------------------------------ XRAY_SOURCE
SOURCE = (os.environ.get("XRAY_SOURCE") or "").strip()
_SHOWN = set()


def _videos(folder):
    try:
        return sorted(os.path.join(folder, f) for f in os.listdir(folder) if f.lower().endswith(VIDEO_EXT))
    except OSError:
        return []


def _slot(original):
    """the same number for the same original source in every process of the program (files in /tmp/xray-sources:
    the first process to ask for a source creates its slot; a reconnect finds it again)"""
    import hashlib
    d = "/tmp/xray-sources"
    os.makedirs(d, exist_ok=True)
    key = os.path.join(d, hashlib.sha1(repr(original).encode()).hexdigest())
    try:
        with open(key) as fh:
            return int(fh.read().strip() or 0)
    except (OSError, ValueError):
        pass
    n = 0
    while True:
        try:
            os.close(os.open(os.path.join(d, f"slot-{n}"), os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            break
        except FileExistsError:
            n += 1
    try:
        with open(key, "w") as fh:
            fh.write(str(n))
    except OSError:
        pass
    return n


def swap_source(original):
    """what VideoCapture opens instead of what the code asked for (XRAY_SOURCE); else the PC_PATHS mapping"""
    if not SOURCE:
        return to_container(original)
    new = SOURCE
    if os.path.isdir(SOURCE):
        vids = _videos(SOURCE)
        if not vids:
            sys.stderr.write(f"[xray] XRAY_SOURCE {SOURCE}: no video files in this folder - the code's own source\n")
            return to_container(original)
        new = vids[_slot(original) % len(vids)]
    if repr(original) not in _SHOWN:
        _SHOWN.add(repr(original))
        sys.stderr.write(f"[xray] video source: {original} -> {new}\n")
        sys.stderr.flush()
    return new


# ------------------------------------------------------------------ the patches
def _first_arg(a, kw, names, fn=to_container):
    if a:
        return (fn(a[0]),) + tuple(a[1:]), kw
    for n in names:
        if n in kw:
            kw = dict(kw)
            kw[n] = fn(kw[n])
            break
    return a, kw


def _wrap(fn, names=("file", "path", "filename")):
    def w(*a, **kw):
        a, kw = _first_arg(a, kw, names)
        return fn(*a, **kw)
    w.__name__ = getattr(fn, "__name__", "wrapped")
    w.__doc__ = getattr(fn, "__doc__", None)
    w.__wrapped__ = fn
    return w


def _patch_cv2(m):
    base = getattr(m, "VideoCapture", None)
    if base is not None and not getattr(base, "_xray_inputs", False):
        class VideoCapture(base):
            _xray_inputs = True

            def __init__(self, *a, **kw):
                if a or kw.get("filename") is not None or kw.get("index") is not None:
                    a, kw = _first_arg(a, kw, ("filename", "index"), swap_source)
                super().__init__(*a, **kw)

            def open(self, *a, **kw):
                a, kw = _first_arg(a, kw, ("filename", "index"), swap_source)
                return super().open(*a, **kw)
        VideoCapture.__name__ = VideoCapture.__qualname__ = "VideoCapture"
        m.VideoCapture = VideoCapture
    if MAP:
        for n in ("imread", "imwrite"):
            if hasattr(m, n):
                setattr(m, n, _wrap(getattr(m, n), ("filename",)))


def _patch_ort(m):
    base = getattr(m, "InferenceSession", None)
    if MAP and base is not None and not getattr(base, "_xray_inputs", False):
        class InferenceSession(base):
            _xray_inputs = True

            def __init__(self, *a, **kw):
                a, kw = _first_arg(a, kw, ("path_or_bytes",))
                super().__init__(*a, **kw)
        InferenceSession.__name__ = InferenceSession.__qualname__ = "InferenceSession"
        m.InferenceSession = InferenceSession


PATCH = {"cv2": _patch_cv2, "onnxruntime": _patch_ort}


class _AfterImport:
    """meta-path finder: lets the normal finders load the module, then patches it"""
    busy = set()

    def find_spec(self, name, path=None, target=None):
        if name not in PATCH or name in self.busy:
            return None
        self.busy.add(name)
        try:
            spec = None
            for finder in sys.meta_path:
                if finder is self or getattr(finder, "find_spec", None) is None:
                    continue
                spec = finder.find_spec(name, path, target)
                if spec is not None:
                    break
        finally:
            self.busy.discard(name)
        loader = getattr(spec, "loader", None)
        orig = getattr(loader, "exec_module", None)
        if spec is None or orig is None:
            return spec

        def exec_module(module, _orig=orig, _loader=loader, _name=name):
            try:
                del _loader.exec_module
            except Exception:
                pass
            _orig(module)
            PATCH[_name](sys.modules.get(_name) or module)
        try:
            loader.exec_module = exec_module
        except Exception:
            pass
        return spec

    def invalidate_caches(self):
        pass


_chain_other_sitecustomize()
if MAP or SOURCE:
    if MAP:
        import builtins
        import io
        import posixpath
        builtins.open = io.open = _wrap(builtins.open)
        for _n in ("exists", "isfile", "isdir", "getsize"):
            setattr(posixpath, _n, _wrap(getattr(posixpath, _n)))
    for _name, _fn in PATCH.items():
        if _name in sys.modules:
            _fn(sys.modules[_name])
    sys.meta_path.insert(0, _AfterImport())
