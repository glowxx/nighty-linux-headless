#!/usr/bin/env python3
"""
Enforce Nighty's on-disk configuration for a headless deployment.

Run once before each launch (by run.sh) and continuously (by webui_guard.py):

  • notifications.json — disable EVERY boolean under the `toast` and `sound`
    groups, so a headless box never tries to raise desktop popups or play sounds.
  • web_config.json    — set the Web UI credentials / host / port from .env.
  • nighty.config      — force  web = true  (Web UI must always be available;
    it is the only usable interface on a machine without a desktop GUI).

All locations come from the environment (see .env.example). Nothing is hardcoded.

Settings are read from the project's `.env` FILE first, and only then from the
process environment. Parsing `.env` directly is deliberate: the Web UI credentials
(and the runtime paths) must come from the user's file even when this runs with a
stale or empty environment — e.g. after a configuration reset, or detached from
run.sh — so we never silently fall back to a hardcoded default while a `.env`
exists.
"""
import os, sys, json, glob, shutil, time
import urllib.request


def _find_env_file():
    """Locate the project's .env. It lives at the repo root, one level above this
    scripts/ directory; allow an override via NIGHTY_ENV for unusual layouts."""
    override = os.environ.get("NIGHTY_ENV")
    if override and os.path.isfile(override):
        return override
    here = os.path.dirname(os.path.abspath(__file__))
    for c in (os.path.join(os.path.dirname(here), ".env"), os.path.join(here, ".env")):
        if os.path.isfile(c):
            return c
    return None


# Parsed .env, cached and refreshed when the file changes (so live edits to the
# credentials are picked up by the continuous guard without a restart).
_ENV_CACHE = {"path": None, "mtime": None, "vals": {}}


def _env_file_vals():
    path = _find_env_file()
    if not path:
        return {}
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return _ENV_CACHE["vals"]
    if _ENV_CACHE["path"] == path and _ENV_CACHE["mtime"] == mtime:
        return _ENV_CACHE["vals"]
    vals = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
                    v = v[1:-1]   # strip matching surrounding quotes
                vals[k] = v
    except Exception:
        return _ENV_CACHE["vals"]
    _ENV_CACHE.update(path=path, mtime=mtime, vals=vals)
    return vals


def env(k, d=None):
    """Resolve a setting, preferring the project's .env FILE over the process
    environment, and only then a hardcoded default. As long as .env exists and
    defines the key, that value wins — never the default."""
    fv = _env_file_vals()
    if k in fv:
        return fv[k]
    v = os.environ.get(k)
    return v if v is not None else d


def find_appdata():
    """Locate '.../Nighty Selfbot' inside the wine prefix."""
    prefix = env("WINEPREFIX") or os.path.join(env("NIGHTY_HOME", "/opt/nighty"), "prefix")
    user = env("WINEUSER") or ""
    candidates = []
    if user:
        candidates.append(os.path.join(prefix, "drive_c", "users", user,
                                       "AppData", "Roaming", "Nighty Selfbot"))
    candidates += glob.glob(os.path.join(prefix, "drive_c", "users", "*",
                                         "AppData", "Roaming", "Nighty Selfbot"))
    for c in candidates:
        if os.path.isdir(c):
            return c
    return None


def _load(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _save(path, obj):
    # Nighty reads these files with the process default encoding — cp1252 under
    # Wine, exactly as on Windows. Any raw non-ASCII byte we write here (e.g. an
    # emoji inside a Rich-Presence / Custom-Status profile) makes Nighty's own
    # read raise UnicodeDecodeError('charmap', …) and return HTTP 500 on save.
    # So we escape non-ASCII (ensure_ascii=True), matching how Nighty itself
    # writes the file and keeping it pure-ASCII and round-trippable.
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=4, ensure_ascii=True)
    os.replace(tmp, path)


def _has_non_ascii(path):
    """True if the file on disk contains any byte > 127 (i.e. it was written with
    raw UTF-8 and is unreadable by Nighty's cp1252 reader)."""
    try:
        with open(path, "rb") as f:
            return not f.read().isascii()
    except OSError:
        return False


def _disable_bools(node):
    """Recursively set every boolean leaf to False. Returns True if anything changed."""
    changed = False
    if isinstance(node, dict):
        for k, v in list(node.items()):
            if isinstance(v, bool):
                if v:
                    node[k] = False
                    changed = True
            elif isinstance(v, (dict, list)):
                changed = _disable_bools(v) or changed
    elif isinstance(node, list):
        for item in node:
            changed = _disable_bools(item) or changed
    return changed


def enforce_notifications(appdata):
    path = os.path.join(appdata, "data", "notifications.json")
    d = _load(path)
    if d is None:
        return "skip (missing)"
    changed = False
    for group in ("toast", "sound"):
        val = d.get(group)
        if isinstance(val, (dict, list)):
            changed = _disable_bools(val) or changed
        elif isinstance(val, bool) and val:
            d[group] = False
            changed = True
    if changed:
        _save(path, d)
        return "updated (toast+sound disabled)"
    return "ok (already disabled)"


def _backup_differs_from_source(source_path, backup_path):
    try:
        if not os.path.exists(backup_path):
            return True
        return os.path.getsize(backup_path) != os.path.getsize(source_path)
    except OSError:
        return True


def enforce_web(appdata):
    msgs = []

    # web_config.json — credentials, host, port
    wc_path = os.path.join(appdata, "web_config.json")
    wc = _load(wc_path)
    if wc is None:
        wc = {}
    desired = {
        "username": env("WEBUI_USERNAME", "admin"),
        "password": env("WEBUI_PASSWORD", ""),
        "host": env("WEBUI_HOST", "127.0.0.1"),
        "port": int(env("WEBUI_PORT", "8090")),
    }
    chg = False
    for k, v in desired.items():
        if k == "password" and v == "":
            continue  # never blank an existing password just because env is empty
        if wc.get(k) != v:
            wc[k] = v
            chg = True
    if chg:
        _save(wc_path, wc)
        msgs.append("web_config updated")
    else:
        msgs.append("web_config ok")

    # nighty.config — web must stay true (hard enforcement + auto-recovery from .bak)
    nc_path = os.path.join(appdata, "nighty.config")
    nc = _load(nc_path)
    if nc is None or not os.path.exists(nc_path) or os.path.getsize(nc_path) == 0:
        bak_pattern = os.path.join(appdata, "nighty.config.bak*")
        baks = sorted(glob.glob(bak_pattern), key=lambda p: os.path.getmtime(p) if os.path.exists(p) else 0, reverse=True)
        restored = False
        for b in baks:
            d = _load(b)
            if isinstance(d, dict) and d.get("web") is not None:
                try:
                    shutil.copyfile(b, nc_path)
                    nc = d
                    msgs.append("nighty.config auto-restored from backup")
                    restored = True
                    break
                except Exception:
                    pass
        if not restored:
            nc = {"web": True}
            _save(nc_path, nc)
            msgs.append("nighty.config auto-created with web=true")
    elif nc.get("web") is not True:
        nc["web"] = True
        _save(nc_path, nc)
        msgs.append("nighty.config web -> true")
    else:
        msgs.append("web already true")

    if isinstance(nc, dict) and nc:
        backup_path = os.path.join(appdata, "nighty.config.bak")
        if _backup_differs_from_source(nc_path, backup_path):
            try:
                shutil.copyfile(nc_path, backup_path)
            except Exception:
                pass

    return "; ".join(msgs)


def _active_profile_is_rpc(d):
    """True if the active profile contains an 'rpc' entry. RPC presets make Nighty
    fetch image assets through the bundled Go tls-client, whose JSON handling
    intermittently segfaults under Box64 and takes the whole backend down.
    Custom-status rotators (text/emoji only) do not fetch those assets and are
    safe to auto-run. `d` is either profile.json or the getUserProfiles result —
    both carry {active_profile, profiles}."""
    active = d.get("active_profile")
    for p in d.get("profiles", []) or []:
        if isinstance(p, dict) and active in p:
            return any(isinstance(e, dict) and "rpc" in e for e in (p.get(active) or []))
    return False


def enforce_safe_presence(appdata):
    """Keep data/profile.json readable by Nighty, and stop only crash-prone
    presets from auto-running — without clobbering the user's startup choice for
    safe ones.

      • Encoding: Nighty reads this file with the cp1252 default under Wine, so a
        raw non-ASCII byte (emoji in a profile) makes its read raise
        UnicodeDecodeError and the panel's "save profile" return HTTP 500. We
        re-save via _save (ensure_ascii=True) whenever the file holds non-ASCII.
      • Crash safety: only RPC presets crash the backend under Box64 (image-asset
        fetch). So we force running/run_at_startup off ONLY when the active
        profile is RPC-type. Custom-status rotators are left under the user's
        control, so the Web UI's "Run last active profile on startup" works for
        them."""
    path = os.path.join(appdata, "data", "profile.json")
    d = _load(path)
    if not isinstance(d, dict):
        return "skip (no profile.json)"
    changed = False
    if _active_profile_is_rpc(d):
        note = "RPC profile — auto-start disabled (Box64 crash hazard)"
        for k in ("running", "run_at_startup"):
            if d.get(k) is not False:
                d[k] = False
                changed = True
    else:
        note = "custom-status profile — startup left to user"
    if changed or _has_non_ascii(path):
        _save(path, d)
    return note


def _stub_call(method, args=(), api=0, timeout=8):
    """Invoke a Nighty MainApi method through the loopback stub control server.
    Returns the parsed JSON reply, or None on any failure."""
    port = env("NIGHTY_STUB_PORT") or env("STUB_PORT", "8765")
    body = json.dumps({"api": api, "method": method, "args": list(args)}).encode()
    req = urllib.request.Request(
        "http://127.0.0.1:%s/api/call" % port, data=body,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None


def stop_unsafe_running_profile():
    """Stop an RPC profile running IN MEMORY (the on-disk flag alone does not halt
    an already-running rotator). Leaves safe custom-status profiles running, so
    the user's startup choice is honoured for them. Cheap no-op when nothing runs
    or the runner is a custom-status profile."""
    res = _stub_call("getUserProfiles")
    if not res or not res.get("ok"):
        return "skip (stub not ready)"
    prof = res.get("result") or {}
    if not prof.get("running"):
        return "ok (nothing running)"
    if not _active_profile_is_rpc(prof):
        return "ok (running profile is safe custom-status)"
    active = prof.get("active_profile")
    if not active:
        return "running RPC but no active_profile"
    off = _stub_call("toggleUserProfile", [active])
    if off and off.get("ok"):
        return "stopped unsafe RPC profile %r" % active
    return "tried to stop %r (stub busy)" % active


# Notification sounds Nighty fetches on demand from its CDN. Nighty downloads
# them with urllib, whose default User-Agent ("Python-urllib/x.y") Cloudflare
# rejects with HTTP 403 — so the file never lands in data/sounds/ and Nighty
# retries on every matching event (the user-visible "Error downloading sound
# nicknames.mp3 (…/sounds/nickupdates.mp3): HTTP Error 403: Forbidden"). We
# fetch them once with a browser User-Agent (which the CDN serves with 200) so
# they sit on disk and Nighty's download-if-missing path never makes the
# blocked request. The mapping of notification category -> file name lives in
# Nighty's frozen code; this is the set confirmed to exist on the CDN. Add a
# name here if a new notification sound shows the same 403.
SOUND_BASE = "https://nighty.one/download/files/sounds"
SOUND_FILES = (
    "connected.mp3",
    "roleupdates.mp3",
    "nickupdates.mp3",
    "relationship.mp3",
    "typing.wav",
    "pinged.wav",
    "giveaway_found.wav",
    "disconnected.wav",
    "nitro_sniped.wav",
)
# Sound aliases mapping: Nighty checks for local filenames on disk (keys),
# but fetches target filenames from CDN (values). Pre-creating all disk aliases
# ensures Nighty's internal download check finds them on disk and never raises HTTP 403.
SOUND_ALIASES = {
    "roles.mp3": "roleupdates.mp3",
    "nicknames.mp3": "nickupdates.mp3",
    "friends.mp3": "relationship.mp3",
    "giveaways.wav": "giveaway_found.wav",
    "nitro.wav": "nitro_sniped.wav",
    "nitro_sniped.mp3": "nitro_sniped.wav",
    "nitro.mp3": "nitro_sniped.wav",
}
_BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def prefetch_sounds(appdata):
    """Pre-seed data/sounds/ with the CDN sounds Nighty would otherwise fail to
    download (Cloudflare 403s its urllib User-Agent). Idempotent and fail-soft:
    skips files already present and never raises into the launch path."""
    dest = os.path.join(appdata, "data", "sounds")
    try:
        os.makedirs(dest, exist_ok=True)
    except OSError:
        return "skip (no sounds dir)"
    have, fetched, failed = 0, [], []
    for name in SOUND_FILES:
        path = os.path.join(dest, name)
        try:
            if os.path.exists(path) and os.path.getsize(path) > 100:
                have += 1
                continue
        except OSError:
            pass
        try:
            req = urllib.request.Request("%s/%s" % (SOUND_BASE, name),
                                         headers={"User-Agent": _BROWSER_UA})
            with urllib.request.urlopen(req, timeout=20) as r:
                data = r.read()
            if not data:
                raise ValueError("empty body")
            tmp = path + ".tmp"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
            fetched.append(name)
        except Exception as e:
            failed.append("%s (%s)" % (name, e))

    # Ensure aliases exist on disk so Nighty's internal check never triggers urllib 403
    for alias_name, target_name in SOUND_ALIASES.items():
        alias_path = os.path.join(dest, alias_name)
        target_path = os.path.join(dest, target_name)
        try:
            if os.path.exists(target_path) and os.path.getsize(target_path) > 100:
                if not os.path.exists(alias_path) or os.path.getsize(alias_path) <= 100:
                    shutil.copyfile(target_path, alias_path)
                    fetched.append(alias_name)
        except Exception as e:
            failed.append("alias %s (%s)" % (alias_name, e))

    msg = "%d already present" % have
    if fetched:
        msg += ", fetched %d" % len(fetched)
    if failed:
        msg += ", failed: %s" % ", ".join(failed)
    return msg


# Nighty's user-history tracker (data/misc/user_history.json) is rewritten IN FULL
# on the bot's asyncio event loop by the onUserUpdate listener — every time a
# tracked user changes name/avatar. Left unbounded the file grows to many MB; on
# the emulated backend that whole-file json.dump then blocks the loop for seconds,
# so the gateway heartbeat times out ("heartbeat blocked") and the bot misses
# Discord's 3s interaction ACK ("the application did not respond"). Nighty offers
# no switch to turn the tracker off and we cannot touch its frozen writer — but we
# CAN cap the file it loads: pruning it here, ONCE before each backend launch and
# BEFORE Nighty reads it, keeps Nighty's in-memory dict (and therefore every
# rewrite) small for the whole session. We keep the most-recently-added users and
# write ASCII (cp1252-safe, see _save). Runs at startup only — mid-session pruning
# would just be overwritten from Nighty's larger in-memory copy.
USER_HISTORY_MAX_BYTES = int(env("USER_HISTORY_MAX_BYTES", "524288") or "524288")  # 512 KiB


def cap_user_history(appdata):
    path = os.path.join(appdata, "data", "misc", "user_history.json")
    try:
        size = os.path.getsize(path)
    except OSError:
        return "skip (none)"
    if size <= USER_HISTORY_MAX_BYTES:
        return "ok (%d KiB, under cap)" % (size // 1024)
    d = _load(path)
    hist = d.get("user_history") if isinstance(d, dict) else None
    if not isinstance(hist, dict) or not hist:
        return "skip (unexpected shape)"
    items = list(hist.items())                       # insertion order: oldest first
    keep_n = max(1, int(len(items) * USER_HISTORY_MAX_BYTES / size))
    kept = items[-keep_n:]                            # keep the most-recent users
    d["user_history"] = dict(kept)
    _save(path, d)
    return "pruned %d -> %d users (%d KiB over %d KiB cap)" % (
        len(items), len(kept), size // 1024, USER_HISTORY_MAX_BYTES // 1024)


def sanitize_all_json_encodings(appdata):
    """Scan all JSON files in AppData and ensure they are ASCII-encoded (ensure_ascii=True).
    Prevents UnicodeDecodeError ('charmap') under Wine when files contain raw UTF-8 emoji."""
    healed = 0
    for root, _, files in os.walk(appdata):
        for f in files:
            if f.endswith(".json") or f.endswith(".config"):
                full_path = os.path.join(root, f)
                if _has_non_ascii(full_path):
                    d = _load(full_path)
                    if d is not None:
                        _save(full_path, d)
                        healed += 1
    return "healed %d non-ASCII JSON file(s)" % healed if healed else "ok (all JSON clean ASCII)"


def _is_mei_in_use(mei_name):
    """True if any active process in /proc currently references this _MEI directory in its memory maps."""
    proc_dir = "/proc"
    if not os.path.isdir(proc_dir):
        return False
    try:
        for entry in os.listdir(proc_dir):
            if entry.isdigit():
                maps_file = os.path.join(proc_dir, entry, "maps")
                try:
                    with open(maps_file, "r", encoding="utf-8", errors="ignore") as f:
                        if mei_name in f.read():
                            return True
                except Exception:
                    pass
    except Exception:
        pass
    return False


MIRROR_BOUNDARY_CHECK_BYTES = 4096


def _mirror_continues_source(src, dst, mirror_size):
    if mirror_size == 0:
        return True
    window = min(MIRROR_BOUNDARY_CHECK_BYTES, mirror_size)
    offset = mirror_size - window
    with open(src, "rb") as source, open(dst, "rb") as mirror:
        source.seek(offset)
        mirror.seek(offset)
        return source.read(window) == mirror.read(window)


def _mirror_appended_tail(src, dst):
    source_size = os.path.getsize(src)
    mirror_size = os.path.getsize(dst) if os.path.exists(dst) else 0
    source_was_truncated = source_size < mirror_size
    # When the sizes match we still confirm the trailing window matches before
    # returning: an in-place rewrite to the same length would otherwise leave the
    # mirror silently stale. The check reads at most 4 KiB from local files, so
    # it stays cheap on the common no-growth tick.
    if source_was_truncated or not _mirror_continues_source(src, dst, mirror_size):
        shutil.copyfile(src, dst)
        return
    if source_size == mirror_size:
        return
    with open(src, "rb") as source, open(dst, "ab") as mirror:
        source.seek(mirror_size)
        shutil.copyfileobj(source, mirror)


def sync_nighty_log(appdata, diag_dir=None):
    """Ensure AppData/nighty.log is mirrored / copied to the diagnostics directory."""
    if not appdata:
        return
    src = os.path.join(appdata, "nighty.log")
    if not os.path.isfile(src):
        return
    if not diag_dir:
        diag_dir = os.environ.get("NIGHTY_DIAG_DIR")
        if not diag_dir:
            here_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            diag_dir = os.path.join(here_dir, "diagnostics")
    try:
        os.makedirs(diag_dir, exist_ok=True)
        dst = os.path.join(diag_dir, "nighty.log")
        try:
            _mirror_appended_tail(src, dst)
        except OSError:
            pass
    except Exception:
        pass


def rotate_and_clean_logs(appdata):
    """Rotate log files in diagnostics/ and NIGHTY_HOME (capped at 10 MB, keeping last 2 MB).
    Also cleans stale PyInstaller _MEI* extraction directories from /tmp and mirrors nighty.log."""
    diag_env = os.environ.get("NIGHTY_DIAG_DIR")
    here_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    root_diag = diag_env or os.path.join(here_dir, "diagnostics")
    nighty_home = env("NIGHTY_HOME") or os.path.dirname(os.path.dirname(appdata))
    home_diag = os.path.join(nighty_home, "diagnostics")
    for d in (root_diag, home_diag):
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            pass

    sync_nighty_log(appdata, root_diag)

    rotated = 0
    max_bytes = 10 * 1024 * 1024  # 10 MB
    keep_bytes = 2 * 1024 * 1024  # 2 MB

    mirrored_names = {"nighty.log"}

    search_dirs = list(dict.fromkeys([root_diag, home_diag, nighty_home]))
    for sdir in search_dirs:
        if not sdir or not os.path.isdir(sdir):
            continue
        try:
            for f in os.listdir(sdir):
                if f.endswith(".log") and f not in mirrored_names:
                    p = os.path.join(sdir, f)
                    try:
                        sz = os.path.getsize(p)
                        if sz > max_bytes:
                            with open(p, "rb") as fp:
                                fp.seek(sz - keep_bytes)
                                tail = fp.read()
                            with open(p, "wb") as fp:
                                fp.write(b"[truncated log rotation]\n" + tail)
                            rotated += 1
                    except Exception:
                        pass
        except Exception:
            pass

    mei_cleaned = 0
    tmp_dir = os.environ.get("TEMP") or "/tmp"
    if os.path.isdir(tmp_dir):
        try:
            for item in os.listdir(tmp_dir):
                if item.startswith("_MEI"):
                    full = os.path.join(tmp_dir, item)
                    try:
                        mtime = os.path.getmtime(full)
                        if (time.time() - mtime) > 1800 and not _is_mei_in_use(item):
                            shutil.rmtree(full, ignore_errors=True)
                            mei_cleaned += 1
                    except Exception:
                        pass
        except Exception:
            pass

    msg = "logs ok"
    if rotated:
        msg = "rotated %d log(s)" % rotated
    if mei_cleaned:
        msg += ", cleaned %d stale _MEI dir(s)" % mei_cleaned
    return msg


def main():
    appdata = find_appdata()
    if not appdata:
        print("[enforce] Nighty appdata not found yet — it appears after the first launch.")
        return 0
    print("[enforce] appdata:", appdata)
    print("[enforce] notifications:", enforce_notifications(appdata))
    print("[enforce] web:", enforce_web(appdata))
    print("[enforce] profile:", enforce_safe_presence(appdata))
    print("[enforce] sounds:", prefetch_sounds(appdata))
    print("[enforce] user_history:", cap_user_history(appdata))
    print("[enforce] json_encoding:", sanitize_all_json_encodings(appdata))
    print("[enforce] diagnostics:", rotate_and_clean_logs(appdata))
    return 0


if __name__ == "__main__":
    sys.exit(main())
