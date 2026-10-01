"""
X-Ray runtime tracer hook (Phase 2). Python imports this file automatically at startup
(it is named `sitecustomize` and its folder is put first on PYTHONPATH by `xray_trace.py run`).
PYTHONPATH is inherited, so every Python process of the app is traced, including
multiprocessing "spawn" children and the resource tracker.

It records, per process, into $XRAY_TRACE_DIR/events-<pid>.jsonl:
  py_hook    sys.addaudithook: imports, open() read/write, os.mkdir/rename, socket
             getaddrinfo/connect/bind, subprocess/os.system/exec, ctypes.dlopen, exec(),
             urllib / http.client requests, pickle.find_class, sqlite3.connect
             + an os.environ wrapper (env var reads and writes, with the default used)
  py_wrapper wrappers around C-level calls the audit hooks cannot see: cv2.imwrite/imread/
             VideoCapture, onnxruntime.InferenceSession, mediapipe BaseOptions, psycopg2.connect, torch.load/hub.load/
             cuda.is_available, requests, PIL ImageFont.truetype, multiprocessing start method /
             Process.start / SharedMemory, uuid.getnode, ctypes.util.find_library
  coverage   which lines of the project's own .py files ran (sys.settrace, project files only)
  at exit    loaded shared libraries (/proc/self/maps), thread counts, top-level packages

Only active when XRAY_TRACE_DIR is set. It never changes what the program does: every
recording step is wrapped in try/except, and a failure only prints one warning.
Exception, opt-in only: XRAY_CAMERA / XRAY_GUI=off swap a camera for a video file or stream and turn
cv2 windows off (see "input substitution" below), so apps made for a webcam + screen run in a container.
Env: XRAY_ROOT (project root, default: cwd), XRAY_COVERAGE=0 disables line coverage.
"""
import os
import sys

_HOOK_DIR = os.path.dirname(os.path.abspath(__file__))


def _chain_other_sitecustomize():
    """Python imports only the first `sitecustomize` on sys.path; run the one we hide (if any)."""
    try:
        import importlib.machinery
        import importlib.util
        paths = [p for p in sys.path if os.path.abspath(p or ".") != _HOOK_DIR]
        spec = importlib.machinery.PathFinder.find_spec("sitecustomize", paths)
        if spec and spec.origin and os.path.dirname(os.path.abspath(spec.origin)) != _HOOK_DIR:
            mod = importlib.util.module_from_spec(spec)
            sys.modules["_xray_chained_sitecustomize"] = mod
            spec.loader.exec_module(mod)
    except Exception as e:  # never break startup
        sys.stderr.write(f"[xray-trace] could not run the original sitecustomize: {e!r}\n")


def _install():
    import atexit
    import functools
    import json
    import threading
    import time
    from dis import opmap

    OUT = os.environ["XRAY_TRACE_DIR"]
    ROOT = os.path.realpath(os.environ.get("XRAY_ROOT") or os.getcwd())
    CWD0 = os.getcwd()
    T0 = time.time()
    TLS = threading.local()
    LOCK = threading.RLock()
    PREFIXES = tuple({os.path.realpath(p) for p in (sys.prefix, sys.base_prefix, sys.exec_prefix)})
    STDLIB = os.path.dirname(os.__file__)
    SECRETISH = ("PASS", "SECRET", "TOKEN", "KEY", "PWD", "CREDENTIAL", "AUTH")
    MAX_PER_SITE = 400
    COVERAGE = os.environ.get("XRAY_COVERAGE", "1") != "0"
    IMPORT_NAME = opmap["IMPORT_NAME"]

    S = {"fd": None, "pid": None, "seen": {}, "per_site": {}, "seq": 0, "warned": set(),
         "pending": {}, "stale": {}, "tops": {}, "cov": {}, "cov_new": 0, "patched": set(), "lazy": set()}

    # ------------------------------------------------------------------ output
    def _open_out():
        S["pid"] = os.getpid()
        path = os.path.join(OUT, f"events-{S['pid']}.jsonl")
        S["fd"] = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        S["seen"], S["per_site"], S["seq"] = {}, {}, 0

    def _write(rec):
        rec.setdefault("t", round(time.time() - T0, 3))
        line = json.dumps(rec, default=repr, separators=(",", ":"), ensure_ascii=False) + "\n"
        with LOCK:
            if S["pid"] != os.getpid():
                _open_out()
            os.write(S["fd"], line.encode("utf-8", "replace"))

    def _emit(rec, key=None):
        """Write rec once per key; later duplicates only bump a counter (written at exit)."""
        if key is not None:
            n = S["seen"].get(key)
            if n is not None:
                n[1] += 1
                return
            site_key = (rec["k"], tuple(rec.get("site") or ()))
            c = S["per_site"].get(site_key, 0)
            if c >= MAX_PER_SITE:
                S["per_site"][site_key] = c + 1
                return
            S["per_site"][site_key] = c + 1
            S["seq"] += 1
            rec["i"] = S["seq"]
            S["seen"][key] = [S["seq"], 1]
        _write(rec)

    def _warn(what, e):
        if what not in S["warned"]:
            S["warned"].add(what)
            try:
                _write({"k": "warning", "what": what, "error": repr(e)[:300]})
            except Exception:
                pass

    # ------------------------------------------------------------------ where
    proj_cache = {}

    def is_project(fn):
        r = proj_cache.get(fn)
        if r is None:
            p = fn if fn.startswith("<") else os.path.realpath(os.path.join(CWD0, fn))
            ok = ((os.path.isabs(fn) or os.path.exists(p)) and p.startswith(ROOT + os.sep) and "site-packages" not in p and "dist-packages" not in p
                  and not p.startswith(_HOOK_DIR + os.sep))
            r = proj_cache[fn] = os.path.relpath(p, ROOT).replace(os.sep, "/") if ok else ""
        return r

    def short(fn):
        if not fn or fn.startswith("<"):
            return fn
        if is_project(fn):
            return is_project(fn)
        parts = fn.replace(os.sep, "/").split("/")
        for i, part in enumerate(parts):
            if part in ("site-packages", "dist-packages"):
                return "/".join(parts[i + 1:])
        if fn.startswith(STDLIB + os.sep):
            return "stdlib:" + fn[len(STDLIB) + 1:]
        return fn

    def where(transparent=(), bulk=()):
        """-> (site [file, line] of the innermost project frame, direct caller file, direct?, bulk?)
        Left in TLS: caller_fn (direct caller's function), inner (innermost non-hook frame file, may
        be <frozen importlib...>), import_op (the direct caller is executing an import statement)."""
        try:
            f = sys._getframe(2)
        except ValueError:
            f = None
        caller = None
        TLS.caller_fn = TLS.inner = TLS.caller_file = None
        TLS.import_op = False
        TLS.skipped = skipped = []
        while f is not None:
            co = f.f_code
            fn = co.co_filename
            if fn.startswith(_HOOK_DIR):
                f = f.f_back
                continue
            if TLS.inner is None:
                TLS.inner = fn
            if caller is None and transparent and fn.endswith(transparent):
                if co.co_name in bulk:
                    return None, fn, False, True
                skipped.append(co.co_name)
                f = f.f_back
                continue
            if fn.startswith("<frozen"):
                f = f.f_back
                continue
            if caller is None:
                caller = TLS.caller_file = fn
                TLS.caller_fn = co.co_name
                try:
                    TLS.import_op = co.co_code[f.f_lasti] == IMPORT_NAME
                except Exception:
                    pass
            rel = is_project(fn)
            if rel:
                return [rel, f.f_lineno], short(caller), fn == caller, False
            f = f.f_back
        return None, short(caller), False, False

    def busy():
        return getattr(TLS, "busy", False)

    # ------------------------------------------------------------------ imports
    def is_stdlib_file(path):
        return bool(path) and path.startswith(STDLIB) and "site-packages" not in path \
            and "dist-packages" not in path

    def mod_file(m):
        f = getattr(m, "__file__", None)
        if f:
            return f
        p = getattr(m, "__path__", None)
        try:
            return list(p)[0] if p else None
        except Exception:
            return None

    def resolve_pending(final=False):
        pend = S["pending"]
        if not pend:
            return
        if final:
            pend.update(S["stale"])
            S["stale"] = {}
        for name in list(pend):
            m = sys.modules.get(name)
            if m is None and not final:
                rec = pend[name]
                rec["_n"] = rec.get("_n", 0) + 1
                if rec["_n"] > 50:      # probably a failed optional import: check again at exit
                    S["stale"][name] = pend.pop(name)
                continue
            rec = pend.pop(name)
            rec.pop("_n", None)
            if m is None:
                if rec["direct"]:
                    rec["k"] = "import_failed"
                    _emit(rec, ("import_failed", name))
                continue
            fpath = mod_file(m)
            rec["file"] = fpath
            if fpath is None or getattr(m, "__spec__", None) is not None and \
                    getattr(m.__spec__, "origin", None) in ("built-in", "frozen"):
                rec["file"] = None
                kind = "builtin"
            elif is_project(fpath):
                kind = "local"
                rec["file"] = is_project(fpath)
            elif is_stdlib_file(os.path.realpath(fpath)):
                kind = "stdlib"
            else:
                kind = "third_party"
            rec["kind"] = kind
            top = name.split(".")[0]
            site = rec.get("site")
            if rec["direct"] and site and kind in ("local", "third_party") and "." not in name:
                # shadowing: a same-named module next to the importing file that did NOT win
                here = os.path.dirname(os.path.join(ROOT, site[0]))
                for sib in (os.path.join(here, name + ".py"), os.path.join(here, name, "__init__.py")):
                    if os.path.exists(sib) and fpath and os.path.realpath(sib) != os.path.realpath(fpath):
                        rec["sibling"] = is_project(sib) or sib
                        break
            if kind == "third_party" and top not in S["tops"]:
                S["tops"][top] = fpath
                _emit({"k": "package", "top": top, "file": fpath, "site": rec.get("site"),
                       "caller": rec.get("caller")}, ("package", top))
            if kind == "local" or (rec["direct"] and kind in ("third_party", "stdlib")):
                _emit(rec, ("import", name))

    def caller_kind(name, caller):
        """How an import was triggered, when it was not a plain import statement."""
        u = getattr(TLS, "unpickling", None)
        fn = getattr(TLS, "caller_fn", None)
        if u and (u == name or u.startswith(name + ".")) or fn == "find_class":
            if u == name:
                TLS.unpickling = None
            return "pickle"
        if "import_module" in (getattr(TLS, "skipped", None) or ()):
            return "importlib"
        cf = getattr(TLS, "caller_file", None)
        if fn == "__import__" or not getattr(TLS, "import_op", True) and cf and is_project(cf):
            return "dunder_import"     # __import__(...) / C-level import called from project code
        return None

    # ------------------------------------------------------------------ audit hook
    def fs(p):
        if isinstance(p, bytes):
            p = os.fsdecode(p)
        if not isinstance(p, str):
            p = os.fspath(p) if hasattr(p, "__fspath__") else None
        return p

    def absn(p):
        return os.path.normpath(p if os.path.isabs(p) else os.path.join(os.getcwd(), p))

    IMPORTLIB_T = ("/importlib/__init__.py",)
    CTYPES_T = ("/ctypes/__init__.py",)
    SKIP_READ_PREFIX = ("/proc/self/", "/dev/", "/sys/fs/cgroup/", "/proc/", "/etc/ld.so")
    KEEP_READ_UNDER_PY = ("/tzdata/zoneinfo/", "/certifi/")

    def is_write(mode, flags):
        if isinstance(mode, str) and any(c in mode for c in "wax+"):
            return True
        if isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC):
            return True
        return False

    def on_open(args):
        path = fs(args[0])
        if path is None:
            return
        mode = args[1] if len(args) > 1 else None
        flags = args[2] if len(args) > 2 else None
        p = absn(path)
        w = is_write(mode, flags)
        if w:
            if "__pycache__" in p or p.endswith(".pyc") or p.startswith(("/dev/null", "/proc/")):
                return
        else:
            if p.endswith((".pyc", ".pyi", ".pth", ".so")) or p.startswith(SKIP_READ_PREFIX) \
                    and not p.startswith("/sys/class/net"):
                return
            if p.startswith(PREFIXES) and not any(k in p for k in KEEP_READ_UNDER_PY):
                return
            if "site-packages" in p or "dist-packages" in p:
                if not any(k in p for k in KEEP_READ_UNDER_PY):
                    return
        site, caller, direct, _ = where()
        if not w and p.endswith(".py") and (not direct or str(TLS.inner).startswith("<frozen")):
            return      # module source read by the import system / linecache / tracebacks
        _emit({"k": "open", "path": p, "w": w, "mode": mode if isinstance(mode, str) else flags,
               "site": site, "caller": caller, "direct": direct}, ("open", p, w, tuple(site or ())))

    def on_event(event, args):
        if event == "open":
            return on_open(args)
        if event == "exec":
            # module code of a project file: the builder drops the ones that were normal imports and keeps
            # spec_from_file_location().exec_module() / exec(compile(open(f).read(), f, "exec"))
            code = args[0]
            target = getattr(code, "co_filename", None)
            if not target or getattr(code, "co_name", None) != "<module>" or not is_project(target):
                return
            site, caller, direct, _ = where()
            return _emit({"k": "exec_module", "file": is_project(target), "site": site, "caller": caller,
                          "inner": TLS.inner}, ("exec_module", target, tuple(site or ())))
        site, caller, direct, _ = where(EVENT_T.get(event, ()))
        rec = {"site": site, "caller": caller, "direct": direct}
        if event == "os.mkdir":
            p = fs(args[0])
            if p is None or p.endswith("__pycache__"):
                return
            rec.update(k="mkdir", path=absn(p))
            return _emit(rec, ("mkdir", rec["path"], tuple(site or ())))
        if event == "os.rename":
            src, dst = fs(args[0]), fs(args[1])
            if dst is None or "__pycache__" in dst:
                return
            rec.update(k="rename", src=absn(src) if src else None, path=absn(dst))
            return _emit(rec, ("rename", rec["path"], tuple(site or ())))
        if event == "socket.getaddrinfo":
            host, port = args[0], args[1]
            if isinstance(host, bytes):
                host = host.decode("ascii", "replace")
            rec.update(k="resolve", host=host, port=port)
            return _emit(rec, ("resolve", host, port, tuple(site or ())))
        if event in ("socket.connect", "socket.bind"):
            sock, addr = args[0], args[1]
            k = "connect" if event == "socket.connect" else "bind"
            if isinstance(addr, tuple) and len(addr) >= 2:
                rec.update(k=k, host=addr[0], port=addr[1], family=getattr(sock, "family", None),
                           type=getattr(sock, "type", None))
            elif isinstance(addr, (str, bytes)):
                rec.update(k=k + "_unix", path=fs(addr))
            else:
                return
            return _emit(rec, (k, rec.get("host"), rec.get("port"), rec.get("path"), tuple(site or ())))
        if event == "subprocess.Popen":
            exe, argv = args[0], args[1]
            if isinstance(argv, (str, bytes)):
                argv = [fs(argv)]
            argv = [fs(a) if not isinstance(a, str) else a for a in (argv or [])]
            rec.update(k="exec_process", executable=fs(exe), argv=[str(a)[:300] for a in argv[:20]])
            return _emit(rec, ("exec_process", tuple(rec["argv"][:1]), tuple(site or ())))
        if event == "os.system":
            rec.update(k="exec_process", shell=True, argv=[str(fs(args[0]))[:500]])
            return _emit(rec, ("exec_process", rec["argv"][0], tuple(site or ())))
        if event in ("os.exec", "os.posix_spawn", "os.spawn"):
            path = args[1] if event == "os.spawn" else args[0]
            argv = args[2] if event == "os.spawn" else args[1]
            rec.update(k="exec_process", executable=fs(path), argv=[str(fs(a))[:300] for a in list(argv or [])[:20]])
            return _emit(rec, ("exec_process", rec["executable"], tuple(site or ())))
        if event == "ctypes.dlopen":
            name = fs(args[0]) if args[0] is not None else None
            if name is None:
                return
            rec.update(k="dlopen", name=name)
            return _emit(rec, ("dlopen", name, tuple(site or ())))
        if event == "urllib.Request":
            rec.update(k="http", url=args[0], method=args[3] if len(args) > 3 else None, via="urllib")
            return _emit(rec, ("http", args[0], tuple(site or ())))
        if event == "http.client.send":
            data = args[1]
            if isinstance(data, (bytes, bytearray)) and len(data) < 65536:
                head = bytes(data[:4096]).split(b"\r\n")
                first = head[0].split(b" ")
                if len(first) == 3 and first[2].startswith(b"HTTP/1.") and first[0].isalpha():
                    host = next((h.split(b":", 1)[1].strip() for h in head[1:] if h.lower().startswith(b"host:")), b"")
                    scheme = "https" if "HTTPS" in type(args[0]).__name__ else "http"
                    url = f"{scheme}://{host.decode('ascii', 'replace')}{first[1].decode('ascii', 'replace')}"
                    rec.update(k="http", url=url, method=first[0].decode("ascii", "replace"), via="http.client")
                    return _emit(rec, ("http", url, tuple(site or ())))
            return
        if event == "pickle.find_class":
            TLS.unpickling = args[0]
            rec.update(k="unpickle", mod=args[0], cls=args[1])
            return _emit(rec, ("unpickle", args[0], args[1], tuple(site or ())))
        if event == "sqlite3.connect":
            rec.update(k="sqlite", db=fs(args[0]))
            return _emit(rec, ("sqlite", rec["db"], tuple(site or ())))

    # library frames to look through when deciding whether project code made the call itself
    EVENT_T = {"ctypes.dlopen": ("/ctypes/__init__.py",), "subprocess.Popen": ("/subprocess.py",),
               "socket.getaddrinfo": ("/socket.py",), "socket.connect": ("/socket.py",)}
    INTERESTING = frozenset({
        "open", "exec", "os.mkdir", "os.rename", "socket.getaddrinfo", "socket.connect",
        "socket.bind", "subprocess.Popen", "os.system", "os.exec", "os.posix_spawn", "os.spawn",
        "ctypes.dlopen", "urllib.Request", "http.client.send", "pickle.find_class", "sqlite3.connect"})

    def audit(event, args):
        if event not in INTERESTING:
            return
        if getattr(TLS, "busy", False):
            return
        TLS.busy = True
        try:
            if S["pending"]:
                resolve_pending()
            on_event(event, args)
        except Exception as e:
            _warn("audit:" + event, e)
        finally:
            TLS.busy = False

    # ------------------------------------------------------------------ os.environ wrapper
    ENV_T = ("/os.py", "/_collections_abc.py", "<frozen os>", "<frozen _collections_abc>")   # frozen in 3.11+
    ENV_BULK = {"copy", "__iter__", "__repr__", "__eq__", "keys", "items", "values", "__len__", "_data",
                "__ior__", "__or__", "__ror__"}

    def mask(name, value):
        if value is None:
            return None
        if any(s in name.upper() for s in SECRETISH):
            return "***"
        return str(value)[:300]

    def env_record(op, key, value, missing):
        if busy():
            return
        TLS.busy = True
        try:
            site, caller, direct, bulk = where(ENV_T, ENV_BULK)
            if bulk:
                return
            default, how = None, "item"          # item: os.environ[X]  get: .get(X, d) / os.getenv(X, d)
            f = sys._getframe(2)
            while f is not None:
                fn_ = f.f_code.co_filename
                if fn_.endswith(ENV_T) and f.f_code.co_name in ("get", "getenv"):
                    how = "get"
                    d = f.f_locals.get("default")
                    default = mask(key, d) if d is not None else None
                    break
                if not fn_.endswith(ENV_T) and not fn_.startswith(_HOOK_DIR):
                    break
                f = f.f_back
            # a stdlib helper called straight from project code (os.path.expanduser -> HOME) counts as project
            via = None
            if site and not direct and caller in ("stdlib:posixpath.py", "stdlib:tempfile.py", "stdlib:getpass.py"):
                via = caller
            if site and (direct or via):
                rec = {"k": "env", "op": op, "name": key, "value": mask(key, value), "missing": missing,
                       "default": default, "how": how, "site": site, "caller": caller, "via": via}
                _emit(rec, ("env", op, key, tuple(site)))
            else:
                _emit({"k": "env_lib", "op": op, "name": key, "missing": missing, "caller": caller,
                       "site": site}, ("env_lib", op, key, caller))
        except Exception as e:
            _warn("env", e)
        finally:
            TLS.busy = False

    Base = type(os.environ)

    class _XRayEnviron(Base):
        def __getitem__(self, key):
            try:
                value = Base.__getitem__(self, key)
            except KeyError:
                env_record("read", key, None, True)
                raise
            env_record("read", key, value, False)
            return value

        def __setitem__(self, key, value):
            env_record("set", key, value, False)
            Base.__setitem__(self, key, value)

        def __delitem__(self, key):
            env_record("unset", key, None, False)
            Base.__delitem__(self, key)

    _XRayEnviron.__name__ = Base.__name__
    _XRayEnviron.__qualname__ = Base.__qualname__

    # ------------------------------------------------------------------ wrappers
    def rec_call(k, fields, key_extra=(), transparent=()):
        if busy():
            return
        TLS.busy = True
        try:
            site, caller, direct, _ = where(transparent)
            rec = {"k": k, "site": site, "caller": caller, "direct": direct}
            rec.update(fields)
            _emit(rec, (k,) + tuple(key_extra) + (tuple(site or ()),))
        except Exception as e:
            _warn("wrap:" + k, e)
        finally:
            TLS.busy = False

    def wrap(owner, attr, make):
        """make(args, kwargs, result, exc) -> (kind, fields, key_extra) or None."""
        orig = getattr(owner, attr, None)
        if orig is None or getattr(orig, "_xray_wrapped", False):
            return

        def w(*a, **kw):
            try:
                r = orig(*a, **kw)
            except BaseException as e:
                try:
                    out = make(a, kw, None, e)
                    if out:
                        rec_call(*out)
                except Exception as e2:
                    _warn("wrap:" + attr, e2)
                raise
            try:
                out = make(a, kw, r, None)
                if out:
                    rec_call(*out)
            except Exception as e2:
                _warn("wrap:" + attr, e2)
            return r

        try:
            functools.update_wrapper(w, orig)
        except Exception:
            pass
        w._xray_wrapped = True
        setattr(owner, attr, w)

    def arg(a, kw, i, name, default=None):
        if len(a) > i:
            return a[i]
        return kw.get(name, default)

    def pathish(v):
        if isinstance(v, (bytes, os.PathLike)):
            v = fs(v)
        if isinstance(v, str) and v and "://" not in v:
            return absn(v)
        return v if v is None or isinstance(v, (str, int)) else f"<{type(v).__name__}>"

    def err(e):
        return None if e is None else f"{type(e).__name__}: {str(e)[:200]}"

    def subclass_ctor(owner, attr, make, pre=None, extra=None):
        """Replace a class by a recording subclass (keeps isinstance working); fall back to a function.
        pre(self, a, kw) -> (a, kw) may swap the arguments (input substitution); make() sees the originals."""
        base = getattr(owner, attr, None)
        if base is None or getattr(base, "_xray_wrapped", False):
            return
        try:
            def __init__(self, *a, **kw):
                e = None
                a2, kw2 = pre(self, a, kw) if pre else (a, kw)
                try:
                    base.__init__(self, *a2, **kw2)
                except BaseException as ex:
                    e = ex
                    raise
                finally:
                    try:
                        out = make(self, a, kw, e)
                        if out:
                            rec_call(*out)
                    except Exception as e2:
                        _warn("wrap:" + attr, e2)
            # __slots__ = (): a subclass instance with a __dict__ segfaults when freed for C types like
            # cv2.VideoCapture (seen with opencv 5.0 at interpreter shutdown)
            ns = {"__init__": __init__, "_xray_wrapped": True, "__slots__": (),
                  "__module__": getattr(base, "__module__", None),
                  "__qualname__": getattr(base, "__qualname__", base.__name__)}
            ns.update((extra or (lambda b: {}))(base))
            sub = type(base.__name__, (base,), ns)
        except TypeError:
            return wrap(owner, attr, lambda a, kw, r, e: make(r, a, kw, e))
        setattr(owner, attr, sub)

    # ------------------------------------------------------------------ input substitution (opt-in)
    # XRAY_CAMERA: lines "0=/data/clip.mp4" / "0=rtsp://host/stream": cv2.VideoCapture(0) opens that instead
    # (a file is replayed in a loop, like a camera that never ends). XRAY_GUI=off: cv2 windows are not opened,
    # waitKey returns "no key"; every XRAY_GUI_SAVE seconds (default 5, 0 = never) the shown frame is saved
    # to <trace>/gui/. The app's code is not changed; the events still record the ORIGINAL source / call.
    CAMERA = {}
    for _line in (os.environ.get("XRAY_CAMERA") or "").splitlines():
        if "=" in _line:
            _k, _v = _line.split("=", 1)
            CAMERA[_k.strip()] = _v.strip()
    GUI_OFF = (os.environ.get("XRAY_GUI") or "").strip().lower() == "off"
    try:
        GUI_SAVE = float(os.environ.get("XRAY_GUI_SAVE", "5"))
    except ValueError:
        GUI_SAVE = 5.0

    def cam_sub(src):
        if src is None or isinstance(src, bool) or not isinstance(src, (int, str)):
            return None
        key = str(src)
        if key.startswith("/dev/video"):
            key = key[len("/dev/video"):]
        return CAMERA.get(key)

    PACE = {}   # id(VideoCapture) -> time the next frame is due
    SUBS = {}   # id(VideoCapture) -> substitute (no attributes on the object: see subclass_ctor)

    def cam_args(a, kw):
        """(new_args, new_kw, substitute) - the substitute drops the apiPreference (CAP_DSHOW etc.)."""
        src = a[0] if a else kw.get("filename", kw.get("index"))
        new = cam_sub(src)
        if new is None:
            return a, kw, None
        kw = {k: v for k, v in kw.items() if k not in ("filename", "index", "apiPreference")}
        return (new,), kw, new

    def gui_off(m):
        orig_imwrite = getattr(m, "imwrite", None)
        st = {"last": {}, "n": 0}

        def imshow(winname, mat, *a, **kw):
            if not GUI_SAVE or orig_imwrite is None or st["n"] >= 40:
                return None
            now = time.time()
            if now - st["last"].get(winname, 0) < GUI_SAVE:
                return None
            st["last"][winname] = now
            st["n"] += 1
            TLS.busy = True
            try:
                d = os.path.join(OUT, "gui")
                os.makedirs(d, exist_ok=True)
                safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(winname))[:40] or "window"
                orig_imwrite(os.path.join(d, f"{safe}-{st['n']:02d}-{now - T0:06.1f}s.jpg"), mat)
            except Exception as e:
                _warn("gui_save", e)
            finally:
                TLS.busy = False
            return None

        def waitKey(delay=0, *a, **kw):
            try:
                if delay and delay > 0:
                    time.sleep(delay / 1000.0)
            except Exception:
                pass
            return -1

        def noop(*a, **kw):
            return None
        repl = {"imshow": imshow, "waitKey": waitKey, "waitKeyEx": waitKey, "pollKey": lambda *a, **kw: -1,
                "getWindowProperty": lambda *a, **kw: 1.0, "getTrackbarPos": lambda *a, **kw: 0}
        for n in ("namedWindow", "destroyAllWindows", "destroyWindow", "moveWindow", "resizeWindow",
                  "setWindowTitle", "setMouseCallback", "setWindowProperty", "createTrackbar", "setTrackbarPos",
                  "startWindowThread"):
            repl[n] = noop
        for n, f in repl.items():
            if hasattr(m, n):
                f.__name__ = n
                setattr(m, n, f)

    def p_cv2(m):
        if GUI_OFF:
            gui_off(m)
        wrap(m, "imwrite", lambda a, kw, r, e: ("cv_write", {"path": pathish(arg(a, kw, 0, "filename")),
                                                            "ok": bool(r) if e is None else False, "error": err(e)},
                                               (pathish(arg(a, kw, 0, "filename")),)))
        wrap(m, "imread", lambda a, kw, r, e: ("cv_read", {"path": pathish(arg(a, kw, 0, "filename")),
                                                          "ok": r is not None and e is None, "error": err(e)},
                                              (pathish(arg(a, kw, 0, "filename")),)))
        wrap(m, "setNumThreads", lambda a, kw, r, e: ("thread_call", {"api": "cv2.setNumThreads",
                                                                     "value": arg(a, kw, 0, "nthreads")},
                                                     ("cv2.setNumThreads", arg(a, kw, 0, "nthreads"))))
        for gui in ("imshow", "namedWindow", "waitKey", "destroyAllWindows"):
            wrap(m, gui, lambda a, kw, r, e, g=gui: ("gui", {"api": "cv2." + g, "error": err(e),
                                                             "gui_off": GUI_OFF or None}, (g,)))

        def vc(self, a, kw, e):
            src = a[0] if a else kw.get("filename", kw.get("index"))
            if src is None:
                return None
            return ("video_open", {"source": src if isinstance(src, int) else pathish(src),
                                   "substituted_by": cam_sub(src),
                                   "opened": bool(self.isOpened()) if e is None and self is not None and
                                   hasattr(self, "isOpened") else None, "error": err(e)}, (str(src),))

        def vc_pre(self, a, kw):
            a2, kw2, new = cam_args(a, kw)
            SUBS[id(self)] = new
            return a2, kw2

        def vc_extra(base):
            if not CAMERA:
                return {}

            def open(self, *a, **kw):
                a2, kw2, new = cam_args(a, kw)
                SUBS[id(self)] = new
                return base.open(self, *a2, **kw2)

            def read(self, *a, **kw):
                sub = SUBS.get(id(self))
                if sub and "://" not in sub:
                    # pace the file like a camera: one frame per 1/fps seconds, not as fast as the disk allows
                    fps = base.get(self, 5)        # CAP_PROP_FPS
                    if 0 < fps <= 240:
                        nxt = PACE.get(id(self))
                        now = time.time()
                        if nxt is not None and nxt > now:
                            time.sleep(nxt - now)
                        PACE[id(self)] = max(now, nxt or 0) + 1.0 / fps
                r = base.read(self, *a, **kw)
                if sub and "://" not in sub and not r[0]:
                    base.set(self, 1, 0)          # CAP_PROP_POS_FRAMES = 0: replay the file from the start
                    r = base.read(self, *a, **kw)
                return r
            return {"open": open, "read": read}
        subclass_ctor(m, "VideoCapture", vc, pre=vc_pre if CAMERA else None, extra=vc_extra)
        cls = getattr(m, "VideoCapture", None)
        if cls is not None and getattr(cls, "_xray_wrapped", False) and isinstance(cls, type):
            wrap(cls, "open", lambda a, kw, r, e: ("video_open", {"source": pathish(arg(a, kw, 1, "filename")),
                                                                 "substituted_by": cam_sub(arg(a, kw, 1, "filename")),
                                                                 "opened": bool(r), "error": err(e)},
                                                  (str(arg(a, kw, 1, "filename")),)))
        dnn = getattr(m, "dnn", None)
        if dnn is not None:
            for fn in ("readNet", "readNetFromONNX", "readNetFromCaffe", "readNetFromTensorflow"):
                wrap(dnn, fn, lambda a, kw, r, e, fn=fn: ("model_load", {"api": "cv2.dnn." + fn,
                                                                          "path": pathish(arg(a, kw, 0, "model")),
                                                                          "error": err(e)},
                                                           (fn, str(arg(a, kw, 0, "model")))))

    def p_ort(m):
        def sess(self, a, kw, e):
            src = arg(a, kw, 0, "path_or_bytes")
            src = pathish(src) if isinstance(src, (str, os.PathLike)) else f"<{type(src).__name__}>"
            req = arg(a, kw, 2, "providers")
            got = None
            if e is None and self is not None:
                try:
                    got = self.get_providers()
                except Exception:
                    pass
            return ("model_load", {"api": "onnxruntime.InferenceSession", "path": src,
                                   "providers_requested": [p if isinstance(p, str) else p[0] for p in (req or [])],
                                   "providers_active": got, "error": err(e)}, ("ort", str(src)))
        subclass_ctor(m, "InferenceSession", sess)

    def p_psycopg2(m):
        def conn(a, kw, r, e):
            dsn = arg(a, kw, 0, "dsn")
            info = {k: kw.get(k) for k in ("host", "port", "dbname", "database", "user") if kw.get(k) is not None}
            if isinstance(dsn, str):
                if "://" in dsn:
                    import urllib.parse
                    u = urllib.parse.urlsplit(dsn)
                    info.update(host=u.hostname, port=u.port, dbname=u.path.lstrip("/"), user=u.username)
                else:
                    for part in dsn.split():
                        if "=" in part:
                            k2, v2 = part.split("=", 1)
                            if k2 in ("host", "port", "dbname", "user"):
                                info[k2] = v2
            info = {k: (str(v) if v is not None else None) for k, v in info.items()}
            return ("db_connect", {"api": "psycopg2.connect", **info, "error": err(e)},
                    (info.get("host"), info.get("port")))
        wrap(m, "connect", conn)

    def p_torch(m):
        wrap(m, "load", lambda a, kw, r, e: ("model_load", {
            "api": "torch.load", "path": pathish(arg(a, kw, 0, "f")) if isinstance(arg(a, kw, 0, "f"), (str, os.PathLike))
            else f"<{type(arg(a, kw, 0, 'f')).__name__}>", "weights_only": kw.get("weights_only"),
            "error": err(e)}, ("torch.load", str(arg(a, kw, 0, "f")))))
        hub = sys.modules.get("torch.hub")
        if hub is not None:
            p_torch_hub(hub)
        cuda = sys.modules.get("torch.cuda")
        if cuda is not None:
            p_torch_cuda(cuda)

    def p_torch_hub(m):
        wrap(m, "load", lambda a, kw, r, e: ("hub_load", {"api": "torch.hub.load",
                                                          "repo": str(arg(a, kw, 0, "repo_or_dir")),
                                                          "model": str(arg(a, kw, 1, "model")), "error": err(e)},
                                            (str(arg(a, kw, 0, "repo_or_dir")), str(arg(a, kw, 1, "model")))))

    def p_torch_cuda(m):
        wrap(m, "is_available", lambda a, kw, r, e: ("gpu_probe", {"api": "torch.cuda.is_available", "result": r},
                                                     ("torch.cuda.is_available",)))

    def p_requests(m):
        cls = getattr(m, "Session", None)
        if cls is not None:
            wrap(cls, "request", lambda a, kw, r, e: ("http", {
                "url": str(arg(a, kw, 2, "url")), "method": str(arg(a, kw, 1, "method")), "via": "requests",
                "status": getattr(r, "status_code", None), "error": err(e)}, (str(arg(a, kw, 2, "url")),)))

    def p_pil_font(m):
        def tt(a, kw, r, e):
            font = arg(a, kw, 0, "font")
            name = fs(font) if isinstance(font, (str, bytes, os.PathLike)) else f"<{type(font).__name__}>"
            return ("font", {"api": "PIL.ImageFont.truetype", "requested": name,
                             "resolved": getattr(r, "path", None) if isinstance(getattr(r, "path", None), str) else None,
                             "ok": e is None, "error": err(e)}, (name,))
        wrap(m, "truetype", tt)

    def p_mp(m):
        wrap(m, "set_start_method", lambda a, kw, r, e: ("mp_start_method", {"method": arg(a, kw, 0, "method"),
                                                                             "error": err(e)},
                                                         (arg(a, kw, 0, "method"),)))
        wrap(m, "get_context", lambda a, kw, r, e: ("mp_start_method", {"method": arg(a, kw, 0, "method"),
                                                                        "via": "get_context"},
                                                    (arg(a, kw, 0, "method"),)) if arg(a, kw, 0, "method") else None)

    def p_mp_process(m):
        cls = getattr(m, "BaseProcess", None)
        if cls is None:
            return

        orig = cls.start

        # BaseProcess.start deletes _target after starting: read it first
        def start_w(self, *a, **kw):
            t = getattr(self, "_target", None)
            try:
                return orig(self, *a, **kw)
            finally:
                try:
                    tname = f"{getattr(t, '__module__', '?')}.{getattr(t, '__qualname__', '?')}" if t else None
                    rec_call("mp_process", {"target": tname, "name": getattr(self, "name", None),
                                            "child_pid": getattr(self, "pid", None)}, (tname, getattr(self, "pid", None)))
                except Exception as e2:
                    _warn("wrap:Process.start", e2)
        if not getattr(orig, "_xray_wrapped", False):
            functools.update_wrapper(start_w, orig)
            start_w._xray_wrapped = True
            cls.start = start_w

    def p_shm(m):
        cls = getattr(m, "SharedMemory", None)
        if cls is None or getattr(cls.__init__, "_xray_wrapped", False):
            return
        orig = cls.__init__

        def init(self, name=None, create=False, size=0, *a, **kw):
            e = None
            try:
                return orig(self, name, create, size, *a, **kw)
            except BaseException as ex:
                e = ex
                raise
            finally:
                try:
                    if create or e is None:
                        rec_call("shm", {"name": getattr(self, "_name", name), "create": bool(create),
                                         "size": getattr(self, "_size", size) if e is None else size,
                                         "error": err(e)}, (str(name), bool(create), e is None))
                except Exception as e2:
                    _warn("wrap:SharedMemory", e2)
        functools.update_wrapper(init, orig)
        init._xray_wrapped = True
        cls.__init__ = init

    def p_uuid(m):
        wrap(m, "getnode", lambda a, kw, r, e: ("hwid", {"api": "uuid.getnode",
                                                         "value": ":".join(f"{(r >> (8 * i)) & 0xff:02x}"
                                                                           for i in reversed(range(6)))
                                                         if isinstance(r, int) else None}, ("uuid.getnode",)))

    def p_ctypes(m):
        """Which file did CDLL(name) really load? (an already-loaded copy with the same soname wins)"""
        cls = getattr(m, "CDLL", None)
        if cls is None or getattr(cls.__init__, "_xray_wrapped", False):
            return
        orig = cls.__init__
        state = {}

        class LinkMap(m.Structure):
            _fields_ = [("l_addr", m.c_void_p), ("l_name", m.c_char_p)]

        def real_path(handle):
            if "dlinfo" not in state:
                state["dlinfo"] = None
                prev = getattr(TLS, "busy", False)
                TLS.busy = True
                try:
                    for libname in (None, "libdl.so.2"):     # glibc >= 2.34 has dlinfo in libc itself
                        try:
                            lib = cls.__new__(cls)
                            orig(lib, libname)
                            state["dlinfo"] = lib.dlinfo
                            break
                        except (OSError, AttributeError):
                            continue
                finally:
                    TLS.busy = prev
            fn = state["dlinfo"]
            if fn is None or not handle:
                return None
            lm = m.POINTER(LinkMap)()
            if fn(m.c_void_p(handle), 2, m.byref(lm)) != 0:     # RTLD_DI_LINKMAP
                return None
            n = lm.contents.l_name
            return os.fsdecode(n) if n else None

        def init(self, name, *a, **kw):
            try:
                orig(self, name, *a, **kw)
            except OSError as e:
                if name is not None and not busy():
                    rec_call("dlopen_resolved", {"name": fs(name), "path": None, "error": err(e)},
                             (str(name), "error"), CTYPES_T)
                raise
            if name is not None and not busy():
                try:
                    path = real_path(getattr(self, "_handle", 0))
                except Exception as e2:
                    path = None
                    _warn("dlinfo", e2)
                rec_call("dlopen_resolved", {"name": fs(name), "path": path}, (str(name),), CTYPES_T)
        functools.update_wrapper(init, orig)
        init._xray_wrapped = True
        cls.__init__ = init

    def p_ctypes_util(m):
        wrap(m, "find_library", lambda a, kw, r, e: ("find_library", {"name": arg(a, kw, 0, "name"), "result": r},
                                                     (arg(a, kw, 0, "name"),)))

    def p_mp_base_options(m):
        # mediapipe hands model_asset_path to C++, which opens the .task/.tflite file (no audit event):
        # BaseOptions.to_pb2() runs inside create_from_options(), right before that load
        cls = getattr(m, "BaseOptions", None)
        if cls is None:
            return

        def rec(a, kw, r, e):
            o = a[0] if a else None
            p = getattr(o, "model_asset_path", None)
            if p is None:
                return None if getattr(o, "model_asset_buffer", None) is None else (
                    "model_load", {"api": "mediapipe BaseOptions", "path": "<bytes>", "error": err(e)},
                    ("mediapipe", "<bytes>"))
            return ("model_load", {"api": "mediapipe BaseOptions", "path": pathish(p), "error": err(e)},
                    ("mediapipe", str(p)))
        wrap(cls, "to_pb2", rec)

    PATCHES = {"cv2": p_cv2, "mediapipe.tasks.python.core.base_options": p_mp_base_options, "onnxruntime": p_ort, "psycopg2": p_psycopg2, "torch": p_torch,
               "torch.hub": p_torch_hub, "torch.cuda": p_torch_cuda, "requests.sessions": p_requests,
               "PIL.ImageFont": p_pil_font, "multiprocessing": p_mp, "multiprocessing.process": p_mp_process,
               "multiprocessing.shared_memory": p_shm, "uuid": p_uuid, "ctypes.util": p_ctypes_util,
               "ctypes": p_ctypes}

    def apply_patch(name, module):
        if module is None or ("done", id(module)) in S["patched"]:
            return
        S["patched"].add(("done", id(module)))
        S["lazy"].discard(name)
        prev = getattr(TLS, "busy", False)
        TLS.busy = True
        try:
            PATCHES[name](module)
        except Exception as e:
            _warn("patch:" + name, e)
        finally:
            TLS.busy = prev

    def lazy_patch(name):
        m = sys.modules.get(name)
        if m is not None and not getattr(getattr(m, "__spec__", None), "_initializing", False):
            apply_patch(name, m)

    in_find = set()

    class _XRayPostImport:
        """Meta-path finder that patches a module right after it has executed."""

        def find_spec(self, name, path=None, target=None):
            # every import that reaches the finders passes here (import statements, __import__,
            # importlib.import_module, unpickling): record where it was triggered from
            if not getattr(TLS, "busy", False) and name not in S["pending"] and name not in S["stale"] \
                    and name not in ("__main__", "__mp_main__"):
                TLS.busy = True
                try:
                    site, caller, direct, _ = where(IMPORTLIB_T)
                    S["pending"][name] = {"k": "import", "mod": name, "site": site, "caller": caller,
                                          "direct": direct, "caller_kind": caller_kind(name, caller)}
                    for n in list(S["lazy"]):
                        lazy_patch(n)
                except Exception as e:
                    _warn("import", e)
                finally:
                    TLS.busy = False
            if name not in PATCHES or name in in_find:
                return None
            in_find.add(name)
            try:
                spec = None
                for finder in sys.meta_path:
                    if finder is self:
                        continue
                    fnd = getattr(finder, "find_spec", None)
                    if fnd is None:
                        continue
                    spec = fnd(name, path, target)
                    if spec is not None:
                        break
            finally:
                in_find.discard(name)
            if spec is None:
                return None
            loader = spec.loader
            orig = getattr(loader, "exec_module", None)
            if orig is None or isinstance(loader, type):
                S["lazy"].add(name)
                return spec

            def exec_module(module, _orig=orig, _name=name, _loader=loader):
                try:
                    del _loader.exec_module
                except Exception:
                    pass
                _orig(module)
                apply_patch(_name, sys.modules.get(_name) or module)
            try:
                loader.exec_module = exec_module
            except Exception:
                S["lazy"].add(name)
            return spec

        def invalidate_caches(self):
            pass

    # ------------------------------------------------------------------ coverage
    def cov_flush():
        new = {}
        for fn, (lines, sent) in S["cov"].items():
            d = lines - sent
            if d:
                sent |= d
                d.discard(0)
                if d:
                    new[is_project(fn) or fn] = sorted(d)
        S["cov_new"] = 0
        if new:
            _write({"k": "cov", "lines": new})

    def ltrace(frame, event, arg_):
        if event == "line":
            try:
                S["cov"][frame.f_code.co_filename][0].add(frame.f_lineno)
                S["cov_new"] += 1
                if S["cov_new"] > 20000:
                    cov_flush()
            except Exception:
                pass
        return ltrace

    def gtrace(frame, event, arg_):
        fn = frame.f_code.co_filename
        rel = proj_cache.get(fn)
        if rel is None:
            rel = is_project(fn)
        if not rel:
            return None
        ent = S["cov"].get(fn)
        if ent is None:
            ent = S["cov"][fn] = (set(), set())
        ent[0].add(frame.f_lineno)
        return ltrace

    # ------------------------------------------------------------------ exit
    def at_exit():
        TLS.busy = True
        try:
            resolve_pending(final=True)
            libs = []
            try:
                with open("/proc/self/maps") as fh:
                    for line in fh:
                        parts = line.split(None, 5)
                        if len(parts) == 6 and ".so" in parts[5]:
                            p = parts[5].strip()
                            if p not in libs:
                                libs.append(p)
            except OSError:
                pass
            info = {"k": "exit", "libs": libs, "cpu_count": os.cpu_count()}
            try:   # resources used by this process: peak RAM (VmHWM), CPU seconds, wall seconds
                with open("/proc/self/status") as fh:
                    for line in fh:
                        if line.startswith("VmHWM:"):
                            info["peak_rss_mb"] = round(int(line.split()[1]) / 1024, 1)
                t = os.times()
                info["cpu_s"] = round(t.user + t.system, 2)
                info["wall_s"] = round(time.time() - T0, 2)
            except Exception:
                pass
            tc = getattr(sys.modules.get("torch"), "cuda", None)
            if tc is not None:
                try:
                    if tc.is_initialized():
                        info["gpu_peak_mb"] = round(tc.max_memory_reserved() / 2 ** 20, 1)
                except Exception:
                    pass
            try:
                info["affinity"] = len(os.sched_getaffinity(0))
            except Exception:
                pass
            if "torch" in sys.modules:
                try:
                    info["torch_threads"] = sys.modules["torch"].get_num_threads()
                except Exception:
                    pass
            if "cv2" in sys.modules:
                try:
                    info["cv2_threads"] = sys.modules["cv2"].getNumThreads()
                except Exception:
                    pass
            if "multiprocessing" in sys.modules:
                try:
                    info["mp_start_method"] = sys.modules["multiprocessing"].get_start_method(allow_none=True)
                except Exception:
                    pass
            if "matplotlib" in sys.modules:
                try:
                    info["mpl_backend"] = dict.__getitem__(sys.modules["matplotlib"].rcParams, "backend")
                except Exception:
                    pass
            tops = {}
            for name, m in list(sys.modules.items()):
                if "." in name or m is None:
                    continue
                fpath = mod_file(m)
                if fpath and not is_stdlib_file(os.path.realpath(fpath)) and not is_project(fpath):
                    tops[name] = fpath
            info["packages"] = tops
            info["counts"] = {str(v[0]): v[1] for v in S["seen"].values() if v[1] > 1}
            over = {f"{k}@{':'.join(map(str, s))}": c - MAX_PER_SITE for (k, s), c in S["per_site"].items()
                    if c > MAX_PER_SITE}
            if over:
                info["overflow"] = over
            cov_flush()
            _write(info)
        except Exception as e:
            _warn("exit", e)

    # ------------------------------------------------------------------ install
    os.makedirs(OUT, exist_ok=True)
    _open_out()
    argv = list(getattr(sys, "orig_argv", sys.argv))
    _write({"k": "start", "pid": os.getpid(), "ppid": os.getppid(), "argv": argv[:12], "cwd": CWD0,
            "root": ROOT, "uid": os.getuid(), "gid": os.getgid(), "exe": sys.executable,
            "python": sys.version.split()[0], "epoch": T0,
            "spawn_child": "--multiprocessing-fork" in argv,
            "env_names": sorted(os.environ.keys()),
            "tracer": {"hook": _HOOK_DIR, "coverage": COVERAGE}})

    def after_fork():
        S["pending"] = {}
        _open_out()
        _write({"k": "start", "pid": os.getpid(), "ppid": os.getppid(), "argv": argv[:12], "forked": True,
                "cwd": os.getcwd(), "root": ROOT, "epoch": time.time()})
    os.register_at_fork(after_in_child=after_fork)

    # existence checks by project code (os.path.exists/isfile/isdir): no audit event exists for them.
    # A missing file the code checks for = a dangling reference with a fallback (fp16 model -> fp32).
    import posixpath

    def check_wrapper(orig, name):
        def w(path, *a, **kw):
            r = orig(path, *a, **kw)
            if not getattr(TLS, "busy", False):
                try:
                    f = sys._getframe(1)
                    rel = is_project(f.f_code.co_filename)
                    if rel:
                        TLS.busy = True
                        try:
                            p = fs(path)
                            if isinstance(p, str):
                                p = absn(p)
                                _emit({"k": "path_check", "fn": name, "path": p, "exists": bool(r),
                                       "site": [rel, f.f_lineno]}, ("path_check", p, rel, f.f_lineno))
                        finally:
                            TLS.busy = False
                except Exception as e:
                    _warn("path_check", e)
            return r
        functools.update_wrapper(w, orig)
        w._xray_wrapped = True
        return w
    for _n in ("exists", "isfile", "isdir"):
        _o = getattr(posixpath, _n)
        if not getattr(_o, "_xray_wrapped", False):
            setattr(posixpath, _n, check_wrapper(_o, _n))

    os.environ.__class__ = _XRayEnviron
    for name in PATCHES:
        if name in sys.modules:
            apply_patch(name, sys.modules[name])
    sys.meta_path.insert(0, _XRayPostImport())
    atexit.register(at_exit)
    if COVERAGE:
        sys.settrace(gtrace)
        threading.settrace(gtrace)
    sys.addaudithook(audit)


if os.environ.get("XRAY_TRACE_DIR"):
    try:
        _install()
    except Exception as _e:
        sys.stderr.write(f"[xray-trace] tracer disabled in pid {os.getpid()}: {_e!r}\n")
_chain_other_sitecustomize()
