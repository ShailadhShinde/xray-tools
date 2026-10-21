"""
X-Ray no_windows: OpenCV windows off in a container, without changing the program's code.

A container has no screen: cv2.imshow there stops the program (Qt: 'Could not load the Qt platform plugin "xcb"').
With this folder on PYTHONPATH, Python loads this file at start. When NO_WINDOWS=1, the window calls of cv2 do
nothing - imshow, namedWindow, destroyAllWindows, moveWindow ... - and waitKey waits as asked and returns -1
("no key pressed"). Every imshow in the program is covered, whatever its window names or settings; the rest of the
program runs unchanged. NO_WINDOWS=0 or unset: nothing is changed. matplotlib draws to files (plt.show() does
nothing) - MPLBACKEND=Agg, unless already set.

Used by xray_plan.py's compose.yml and xray_runfile.py's run-naive when the code opens windows and has no setting of
its own for them:  -v "<this folder>:/opt/xray-no-windows:ro" -e PYTHONPATH=/opt/xray-no-windows -e NO_WINDOWS=1
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))


def _chain_other_sitecustomize():
    """Python imports only the first sitecustomize on sys.path: run the one this file hides (if the image has one)"""
    try:
        import importlib.machinery
        import importlib.util
        skip = {_HERE}          # this folder, and the one of a sitecustomize already running (X-Ray's trace hook)
        first = getattr(sys.modules.get("sitecustomize"), "__file__", None)
        if first:
            skip.add(os.path.dirname(os.path.abspath(first)))
        paths = [p for p in sys.path if os.path.abspath(p or ".") not in skip]
        spec = importlib.machinery.PathFinder.find_spec("sitecustomize", paths)
        if spec and spec.origin and os.path.dirname(os.path.abspath(spec.origin)) not in skip:
            mod = importlib.util.module_from_spec(spec)
            sys.modules["_no_windows_chained_sitecustomize"] = mod
            spec.loader.exec_module(mod)
    except Exception as e:      # never break the program's start
        sys.stderr.write(f"[no_windows] could not run the image's own sitecustomize: {e!r}\n")


def _windows_off(m):
    import time

    def wait(delay=0, *a, **kw):
        if delay and delay > 0:
            time.sleep(delay / 1000.0)
        return -1

    def noop(*a, **kw):
        return None
    for n in ("imshow", "namedWindow", "destroyAllWindows", "destroyWindow", "moveWindow", "resizeWindow",
              "setWindowTitle", "setMouseCallback", "setWindowProperty", "createTrackbar", "setTrackbarPos",
              "startWindowThread"):
        if hasattr(m, n):
            setattr(m, n, noop)
    for n in ("waitKey", "waitKeyEx"):
        if hasattr(m, n):
            setattr(m, n, wait)
    if hasattr(m, "pollKey"):
        m.pollKey = lambda *a, **kw: -1
    if hasattr(m, "getWindowProperty"):          # "is the window still open?" - yes, so loops keep running
        m.getWindowProperty = lambda *a, **kw: 1.0
    if hasattr(m, "getTrackbarPos"):
        m.getTrackbarPos = lambda *a, **kw: 0


class _AfterImport:
    """meta-path finder: lets the normal finders load cv2, then switches its windows off"""
    busy = set()

    def find_spec(self, name, path=None, target=None):
        if name != "cv2" or name in self.busy:
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

        def exec_module(module, _orig=orig, _loader=loader):
            try:
                del _loader.exec_module
            except Exception:
                pass
            _orig(module)
            _windows_off(sys.modules.get("cv2") or module)
        try:
            loader.exec_module = exec_module
        except Exception:
            pass
        return spec

    def invalidate_caches(self):
        pass


_chain_other_sitecustomize()
if (os.environ.get("NO_WINDOWS") or "").strip().lower() in ("1", "yes", "true", "on"):
    os.environ.setdefault("MPLBACKEND", "Agg")
    if "cv2" in sys.modules:
        _windows_off(sys.modules["cv2"])
    else:
        sys.meta_path.insert(0, _AfterImport())
