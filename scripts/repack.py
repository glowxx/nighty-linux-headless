#!/usr/bin/env python3
"""
Repack YOUR Nighty.exe, replacing the bundled `webview` package with a headless
stub. This lets Nighty's backend (and its native Web UI) start on a machine that
cannot render the desktop GUI (a headless Linux host under Wine) without touching
Nighty's license check or its protected code.

  • It does NOT crack, unlicense, or redistribute Nighty. You supply your own
    licensed Nighty.exe; this only swaps the GUI layer for a no-op stub so the
    program can run headless and serve its own Web UI.
  • The stub captures the JS-API bridge and exposes a loopback-only control
    server (default 127.0.0.1:8765) used during first-run onboarding.

IMPORTANT: run this under Python 3.8 — the frozen runtime is 3.8 and the
marshal format of the embedded archive must match. The installer fetches a
3.8 interpreter via `uv` for exactly this step.

Usage:
    python3.8 repack.py [SRC.exe] [OUT.exe]
Env:
    NIGHTY_STUB_PORT   control-server port inside the stub (default 8765)
    NIGHTY_STUB_LOG    optional wine path for the stub log (e.g. Z:\\tmp\\stub.log)
"""
import struct, marshal, zlib, sys, os, errno

BIND_MOUNT_ERRNOS = (errno.EBUSY, errno.EXDEV)


def write_output(path, payload):
    tmp_path = path + ".tmp"
    with open(tmp_path, "wb") as fh:
        fh.write(payload)
        fh.flush()
        os.fsync(fh.fileno())
    try:
        os.replace(tmp_path, path)
        return "atomic"
    except OSError as exc:
        if exc.errno not in BIND_MOUNT_ERRNOS:
            raise
    with open(path, "r+b") as fh:
        fh.write(payload)
        fh.truncate(len(payload))
        fh.flush()
        os.fsync(fh.fileno())
    written = os.path.getsize(path)
    if written != len(payload):
        sys.exit("ERROR: in-place write of %s is %d bytes, expected %d — %s kept for recovery."
                 % (path, written, len(payload), tmp_path))
    try:
        os.unlink(tmp_path)
    except OSError:
        pass
    return "in-place (target is a bind mount)"


SRC = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("NIGHTY_EXE", "Nighty.exe")
OUT = sys.argv[2] if len(sys.argv) > 2 else os.environ.get("NIGHTY_STUB", "Nighty_stub.exe")

# The stub `webview` module. Port and log path are read from the environment at
# runtime (inside wine), so nothing is hardcoded.
STUB_INIT = r'''
import sys, os, threading, time, traceback, queue
_taskq = queue.Queue()
_PORT = int(os.environ.get("NIGHTY_STUB_PORT", "8765"))
_LOG = os.environ.get("NIGHTY_STUB_LOG", "")
_logstate = {"fh": None, "last": None, "repeats": 0}
_loglock = threading.Lock()
def _logfh():
    if _logstate["fh"] is None and _LOG:
        try: _logstate["fh"] = open(_LOG, "a", encoding="utf-8", errors="replace")
        except Exception: _logstate["fh"] = False
    return _logstate["fh"] or None
def _logemit(line):
    try: print(line, flush=True)
    except Exception: pass
    fh = _logfh()
    if fh:
        try:
            fh.write(line + "\n"); fh.flush()
        except Exception: pass
def _log(*a):
    msg = "[STUBWV] " + " ".join(str(x) for x in a)
    # Hold the lock across the emit too: the stub is heavily multi-threaded
    # (ctl handlers, async workers), so releasing before writing would let two
    # threads interleave lines and misattribute the "repeated N times" summary,
    # and would race two opens of the log file handle.
    with _loglock:
        if msg == _logstate["last"]:
            _logstate["repeats"] += 1
            return
        repeated = _logstate["repeats"]
        _logstate["last"] = msg
        _logstate["repeats"] = 0
        if repeated:
            _logemit("[STUBWV] (previous line repeated %d more times)" % repeated)
        _logemit(msg)

class WebViewException(Exception): pass
class JavascriptException(Exception): pass

token = "stub-token"
windows = []
settings = {'ALLOW_DOWNLOADS': False,'ALLOW_FILE_URLS': True,'OPEN_EXTERNAL_LINKS_IN_BROWSER': True,'OPEN_DEVTOOLS_IN_DEBUG': False,'REMOTE_DEBUGGING_PORT': None}
_JS_API = []
try:
    _EV_CAP = int(os.environ.get("NIGHTY_STUB_EVENT_CAP", "2000") or "2000")
except ValueError:
    _EV_CAP = 2000  # a typo'd value must not crash stub import
if _EV_CAP < 0:
    _EV_CAP = 2000  # negatives would read as "unbounded"; fall back to the default
_events = []
_evbase = 0
_evlock = threading.Lock()
def _emit(_t, **kw):
    global _evbase
    with _evlock:
        kw['type'] = _t; kw['seq'] = _evbase + len(_events); _events.append(kw)
        if _EV_CAP > 0 and len(_events) > _EV_CAP:
            drop = len(_events) - _EV_CAP
            del _events[:drop]
            _evbase += drop

class _Event:
    def __init__(self, name): self._name=name; self._handlers=[]
    def __iadd__(self, h): self._handlers.append(h); _log("event +=", self._name, getattr(h,'__name__',h)); return self
    def __isub__(self, h):
        try: self._handlers.remove(h)
        except ValueError: pass
        return self
    def __call__(self,*a,**k): return self.fire(*a,**k)
    def set(self,*a,**k): return self.fire(*a,**k)
    def fire(self,*a,**k):
        for h in list(self._handlers):
            try: _log("FIRE", self._name, "->", getattr(h,'__name__',h)); h(*a,**k)
            except Exception: _log("handler error", self._name); traceback.print_exc()

class Events:
    def __init__(self):
        for n in ['shown','loaded','closing','closed','minimized','maximized','restored','resized','moved','before_show','before_load']:
            setattr(self, n, _Event(n))

class Window:
    def __init__(self, uid, title, url=None, html=None, js_api=None, **kw):
        self.uid=uid; self.title=title; self.real_url=url; self.html=html; self._js_api=js_api
        self.events=Events(); self.gui=None; self.shown=True; self.loaded=threading.Event(); self.closed=False
        _log("Window uid=%r title=%r url=%r html?=%s js_api=%s" % (uid,title,url,bool(html), type(js_api).__name__ if js_api else None))
        if js_api is not None:
            _JS_API.append(js_api)
            try:
                meth=[m for m in dir(js_api) if not m.startswith('_')]
                _log("js_api methods (%d): %s" % (len(meth), ", ".join(meth)))
            except Exception: pass
    def evaluate_js(self, script, callback=None,*a,**k):
        try:
            s=str(script); ss=s if len(s)<160 else s[:160]+"...(%d)"%len(s)
            _log("evaluate_js:", ss.replace(chr(10)," "))
            _emit('evaluate_js', uid=self.uid, script=ss)
        except Exception: pass
        if callback:
            try: callback(None)
            except Exception: pass
        return None
    def run_js(self, script,*a,**k): return self.evaluate_js(script)
    def load_url(self, url): _log("load_url:", url); self.real_url=url; _emit('load_url', uid=self.uid, url=url)
    def load_html(self, content, base_uri=''): _log("load_html len=%d" % len(content)); _emit('load_html', uid=self.uid, html_len=len(content))
    def get_current_url(self): return self.real_url
    def get_elements(self, selector): _log("get_elements", selector); return []
    def set_title(self, t): self.title=t
    def show(self): self.shown=True; _log("show")
    def hide(self): _log("hide")
    def on_top(self,*a,**k): pass
    def minimize(self): _log("minimize")
    def restore(self): _log("restore")
    def maximize(self): _log("maximize")
    def toggle_fullscreen(self): _log("toggle_fullscreen")
    def resize(self,*a,**k): pass
    def move(self,*a,**k): pass
    def destroy(self): _log("destroy"); self.closed=True
    def expose(self,*funcs):
        for f in funcs: _log("expose", getattr(f,'__name__',f))
    def create_file_dialog(self,*a,**k): _log("create_file_dialog"); return None
    def create_confirmation_dialog(self,*a,**k): return True
    def native(self): return None

_ctl_started = [False]
def _start_ctl():
    if _ctl_started[0]: return
    _ctl_started[0] = True
    import json
    try:
        from http.server import BaseHTTPRequestHandler, HTTPServer
        try:
            from http.server import ThreadingHTTPServer as _SRV
        except Exception:
            from socketserver import ThreadingMixIn
            class _SRV(ThreadingMixIn, HTTPServer): daemon_threads = True
    except Exception as e:
        _log("ctl import fail", repr(e)); return
    class H(BaseHTTPRequestHandler):
        def log_message(self,*a): pass
        def _send(self, obj, code=200):
            b=json.dumps(obj, default=lambda o: repr(o)).encode("utf-8","replace")
            self.send_response(code); self.send_header("Content-Type","application/json")
            self.send_header("Content-Length",str(len(b))); self.end_headers(); self.wfile.write(b)
        def do_GET(self):
            if self.path.startswith("/api/methods"):
                apis=[{"index":i,"type":type(o).__name__,"methods":[m for m in dir(o) if not m.startswith("_")]} for i,o in enumerate(_JS_API)]
                return self._send({"apis":apis,"windows":[{"uid":w.uid,"title":w.title,"url":w.real_url} for w in windows]})
            if self.path.startswith("/bridge/events"):
                from urllib.parse import urlparse, parse_qs
                q=parse_qs(urlparse(self.path).query)
                try: since=int(q.get("since",["0"])[0])
                except Exception: since=0
                with _evlock:
                    start = since - _evbase
                    if start < 0: start = 0
                    evs = list(_events[start:]); total = _evbase + len(_events)
                cur = windows[-1].real_url if windows else None
                return self._send({"events":evs,"total":total,"current_url":cur,
                                   "windows":[{"uid":w.uid,"title":w.title,"url":w.real_url} for w in windows]})
            return self._send({"hint":"POST /api/call {api,method,args}"},200)
        def do_POST(self):
            try:
                ln=int(self.headers.get("Content-Length",0) or 0); body=self.rfile.read(ln) if ln else b"{}"
                req=json.loads(body or b"{}")
            except Exception as e: return self._send({"ok":False,"error":"bad json %r"%(e,)},400)
            idx=req.get("api",0); method=req.get("method"); args=req.get("args",[]) or []; kwargs=req.get("kwargs",{}) or {}
            try:
                obj=_JS_API[idx]; fn=getattr(obj, method)
            except Exception as e:
                return self._send({"ok":False,"error":"lookup %r"%(e,)},400)
            if req.get("async"):
                def _bg():
                    try: _log("CTL async api[%d].%s ->" % (idx,method), fn(*args,**kwargs))
                    except Exception as e: _log("CTL async api[%d].%s RAISED %r" % (idx,method,e))
                threading.Thread(target=_bg, daemon=True, name="ctl-async").start()
                return self._send({"ok":True,"started":True,"method":method})
            # dispatch onto the start() thread (single consistent event-loop, like real pywebview)
            _log("CTL call api[%d].%s (dispatch to main)" % (idx, method))
            box={}; done=threading.Event()
            _taskq.put((fn, args, kwargs, box, done))
            wait_s = float(req.get("timeout", 45))
            if done.wait(wait_s):
                if 'exc' in box:
                    return self._send({"ok":False,"error":repr(box['exc']),"tb":box.get('tb','')},500)
                return self._send({"ok":True,"method":method,"result":box['result']})
            else:
                return self._send({"ok":True,"running":True,"method":method,"note":"still running on main thread"})
    def _run():
        try:
            srv=_SRV(("127.0.0.1",_PORT), H); _log("CTL server up (threaded) on 127.0.0.1:%d" % _PORT); srv.serve_forever()
        except Exception as e: _log("CTL server failed", repr(e))
    threading.Thread(target=_run, daemon=True, name="stub-ctl").start()

def create_window(title='', url=None, html=None, js_api=None, **kwargs):
    _log("create_window title=%r url=%r kwargs=%s" % (title,url,list(kwargs.keys())))
    uid='master' if not windows else 'win%d'%(len(windows)+1)
    w=Window(uid,title,url=url,html=html,js_api=js_api,**kwargs); windows.append(w)
    apii = (_JS_API.index(js_api) if js_api in _JS_API else -1)
    _emit('create_window', uid=uid, title=title, url=url, api=apii)
    if js_api is not None: _start_ctl()
    return w

def active_window(): return windows[0] if windows else None

def start(func=None, args=None, gui=None, debug=False, http_server=False, http_port=None, **kwargs):
    _log("start() gui=%r http_server=%r http_port=%r debug=%r func=%r extra=%s" % (gui,http_server,http_port,debug,getattr(func,'__name__',func),list(kwargs.keys())))
    def _ready():
        time.sleep(2)
        for w in list(windows):
            try: w.events.before_show.fire()
            except Exception: pass
            try: w.events.shown.fire()
            except Exception: pass
            try: w.events.loaded.fire()
            except Exception: pass
            try: w.loaded.set()
            except Exception: pass
        _log("fired shown+loaded for %d window(s)" % len(windows))
    threading.Thread(target=_ready, daemon=True, name="stub-ready").start()
    funcs = func if isinstance(func,(list,tuple)) else ([func] if func else [])
    for fn in funcs:
        if fn is None: continue
        def _rf(fn=fn):
            try: _log("calling start-func", getattr(fn,'__name__',fn)); fn(); _log("start-func returned")
            except Exception: _log("start-func raised"); traceback.print_exc()
        threading.Thread(target=_rf, daemon=True, name="stub-startfunc").start()
    _log("start() main dispatch loop ready (api calls run on this thread)")
    while True:
        try:
            fn, a, k, box, done = _taskq.get()
        except Exception:
            continue
        try:
            box['result'] = fn(*a, **k)
        except Exception as e:
            box['exc'] = e; box['tb'] = traceback.format_exc()
        try: done.set()
        except Exception: pass

def screens(): return []
'''

STUB_WINDOW = "from webview import Window, WebViewException, Events\n"
STUB_ERRORS = "class WebViewException(Exception):\n    pass\nclass JavascriptException(Exception):\n    pass\n"
STUB_GUILIB = "def initialize(*a, **k):\n    return None\nrenderer=None\n"

REPLACE_SRC = {
    'webview': STUB_INIT,
    'webview.window': STUB_WINDOW,
    'webview.errors': STUB_ERRORS,
    'webview.guilib': STUB_GUILIB,
}

def compile_blob(name, src):
    code = compile(src, '<stub:%s>' % name, 'exec')
    return zlib.compress(marshal.dumps(code), 9)

def main():
    if not os.path.isfile(SRC):
        sys.exit("ERROR: source binary not found: %s (drop your Nighty.exe there)" % SRC)
    if os.path.abspath(OUT) == os.path.abspath(SRC):
        sys.exit("ERROR: output path must differ from the source path (%s) — refusing to overwrite your licensed binary." % SRC)
    if sys.version_info[:2] != (3, 8):
        print("WARNING: not running under Python 3.8 (got %d.%d). The marshal format"
              " must match the frozen runtime; use the uv-provided 3.8 interpreter."
              % sys.version_info[:2])
    data = open(SRC, "rb").read()
    total = len(data)
    cmagic = b'MEI\014\013\012\013\016'
    ci = data.rfind(cmagic)
    if ci < 0:
        sys.exit("ERROR: PyInstaller CArchive cookie not found — is this a Nighty one-file exe?")
    mg, lenpkg, tocpos, toclen, pyvers = struct.unpack("!8sIIII", data[ci:ci+24])
    pylib_field = data[ci+24:ci+24+64]
    ps = total - lenpkg
    toc = data[ps+tocpos:ps+tocpos+toclen]
    ENTRY = struct.Struct("!IIIIBc")
    entries = []
    off = 0
    while off < len(toc):
        elen, epos, dlen, ulen, cflag, tc = ENTRY.unpack_from(toc, off)
        if elen < ENTRY.size or off + elen > len(toc):
            sys.exit("ERROR: corrupt CArchive TOC entry at offset %d (elen=%d) — is this a Nighty one-file exe?" % (off, elen))
        name = toc[off+ENTRY.size:off+elen].rstrip(b'\0').decode('utf-8','replace')
        entries.append([name, tc, cflag, epos, dlen, ulen])
        off += elen
    pyz = next((e for e in entries if e[1] == b'z'), None)
    if pyz is None:
        sys.exit("ERROR: no PYZ entry found in the CArchive — is this a Nighty one-file exe?")
    praw = data[ps+pyz[3]:ps+pyz[3]+pyz[4]]
    assert praw[:4] == b'PYZ\0'
    pymagic = praw[4:8]
    ztocpos = struct.unpack("!i", praw[8:12])[0]
    ztoc = marshal.loads(praw[ztocpos:])  # list of (name,(tc,off,len)) — basic types, OK in 3.8

    DATA_START = 12
    newdata = bytearray()
    newztoc = []
    replaced = []
    for name, meta in ztoc:
        tcz, o, l = meta
        if name in REPLACE_SRC:
            blob = compile_blob(name, REPLACE_SRC[name])
            replaced.append((name, l, len(blob)))
        else:
            blob = praw[o:o+l]
        noff = DATA_START + len(newdata)
        newdata += blob
        newztoc.append((name, (tcz, noff, len(blob))))
    ztocpos2 = DATA_START + len(newdata)
    newpyz = b'PYZ\0' + pymagic + struct.pack("!i", ztocpos2) + bytes(newdata) + marshal.dumps(newztoc)
    print("replaced PYZ entries:", replaced)
    print("PYZ size old=%d new=%d" % (len(praw), len(newpyz)))

    newpkg = bytearray()
    newentries = []
    for name, tc, cflag, epos, dlen, ulen in entries:
        if tc == b'z':
            blob = newpyz; dlen2 = len(newpyz); ulen2 = len(newpyz); cflag2 = 0
        else:
            blob = data[ps+epos:ps+epos+dlen]; dlen2 = dlen; ulen2 = ulen; cflag2 = cflag
        npos = len(newpkg)
        newpkg += blob
        newentries.append((npos, dlen2, ulen2, cflag2, tc, name))
    new_tocpos = len(newpkg)
    tocbuf = bytearray()
    for npos, dlen2, ulen2, cflag2, tc, name in newentries:
        nb = name.encode('utf-8')
        base = ENTRY.size + len(nb) + 1
        elen = ((base + 15) // 16) * 16
        namefield = nb + b'\0' * (elen - ENTRY.size - len(nb))
        tocbuf += ENTRY.pack(elen, npos, dlen2, ulen2, cflag2, tc) + namefield
    toclen2 = len(tocbuf)
    lenpkg2 = new_tocpos + toclen2 + 88
    cookie = struct.pack("!8sIIII", cmagic, lenpkg2, new_tocpos, toclen2, pyvers) + pylib_field
    assert len(cookie) == 88
    newpkg += tocbuf + cookie
    out = data[:ps] + bytes(newpkg)
    missing = sorted(set(REPLACE_SRC) - {n for n, _, _ in replaced})
    if missing:
        sys.exit("ERROR: no PYZ entry matched %s — refusing to write a stub that still needs a GUI." % missing)
    how = write_output(OUT, out)
    print("wrote %s : %d bytes (orig %d, %s)" % (OUT, len(out), total, how))

if __name__ == "__main__":
    main()
