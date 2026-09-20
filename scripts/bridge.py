#!/usr/bin/env python3
"""
nighty-linux-headless — LAN bridge.

Exposes Nighty's native Web UI (the "legacy" interface) safely over the LAN, and
provides a clean first-run onboarding flow (paste account token, then bot/app
token) for when the panel is not up yet.

  • GET  /            -> the native Web UI (reverse-proxied from WEBUI_PORT),
                         or onboarding pages while it is still starting.
  • POST /rpc         -> forwarded to the stub control server (onboarding only).
  • GET  /state       -> onboarding state machine.
  • GET  /events      -> stub event stream (onboarding only).

All hosts/ports/credentials come from the environment (see .env.example).
Nothing is hardcoded; no secrets live in this file.
"""
import os, io, sys, json, ssl, hmac, time, base64, select, socket, urllib.request, urllib.error, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    from enforce_config import find_appdata  # same directory
except Exception:
    def find_appdata():
        return None

STUB_PORT = int(os.environ.get("STUB_PORT", "8765"))
WEBUI_HOST = os.environ.get("WEBUI_HOST", "127.0.0.1")
WEBUI_PORT = int(os.environ.get("WEBUI_PORT", "8090"))
HOST = os.environ.get("BRIDGE_HOST", "0.0.0.0")
PORT = int(os.environ.get("BRIDGE_PORT", "8088"))
START_TIME = time.time()
MAX_REQUEST_BYTES = int(os.environ.get("BRIDGE_MAX_REQUEST_BYTES", "262144"))
WS_ALLOWED_PREFIXES = ("/socket.io", "/ws")

STUB = "http://127.0.0.1:%d" % STUB_PORT
WEBPANEL = "http://%s:%d" % (WEBUI_HOST, WEBUI_PORT)

WEBUI_USERNAME = os.environ.get("WEBUI_USERNAME", "")
WEBUI_PASSWORD = os.environ.get("WEBUI_PASSWORD", "")
BRIDGE_AUTH = (os.environ.get("BRIDGE_AUTH", "auto") or "auto").strip().lower()
LOOPBACK_BINDS = ("127.0.0.1", "::1", "localhost")
WEAK_PASSWORDS = frozenset(("", "change-this-please", "secret", "password", "admin", "nighty"))
AUTH_REALM = "Nighty"


def bind_is_public():
    return HOST not in LOOPBACK_BINDS


def client_is_loopback(addr):
    # Matches the raw TCP peer only (never X-Forwarded-For), so this can't be
    # spoofed by a header. Covers dotted-quad 127/8 and both the IPv4-mapped
    # IPv6 form (::ffff:127.x.x.x) and ::1.
    return (addr == "::1"
            or addr.startswith("127.")
            or addr.startswith("::ffff:127."))


def auth_enabled():
    if BRIDGE_AUTH in ("off", "0", "false", "no"):
        return False
    if BRIDGE_AUTH in ("on", "1", "true", "yes"):
        return True
    return bind_is_public()


def env_file_path():
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (os.path.join(os.path.dirname(here), ".env"), os.path.join(here, ".env")):
        if os.path.isfile(cand):
            return cand
    return None


def persist_generated_password(value):
    path = env_file_path()
    if not path:
        return False
    try:
        with io.open(path, encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
        out, replaced = [], False
        for line in lines:
            if line.startswith("WEBUI_PASSWORD="):
                out.append("WEBUI_PASSWORD=" + value)
                replaced = True
            else:
                out.append(line)
        if not replaced:
            out.append("WEBUI_PASSWORD=" + value)
        tmp = path + ".tmp"
        # Create the temp file 0600 from the outset. Creating it with the
        # process umask (typically 0644) and chmod-ing afterward would leave the
        # plaintext password world-readable for the intervening window.
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with io.open(fd, "w", encoding="utf-8") as fh:
            fh.write("\n".join(out) + "\n")
        try:
            os.chmod(tmp, 0o600)  # no-op if O_CREAT mode already applied; harmless
        except OSError:
            pass
        os.replace(tmp, path)
        return True
    except Exception:
        return False


def resolve_credentials():
    user = WEBUI_USERNAME or "admin"
    pw = WEBUI_PASSWORD
    if pw.strip().lower() in WEAK_PASSWORDS:
        pw = base64.urlsafe_b64encode(os.urandom(18)).decode("ascii").rstrip("=")
        saved = persist_generated_password(pw)
        banner = "=" * 72
        print(banner, flush=True)
        print("bridge: WEBUI_PASSWORD is unset or a known default, and the bridge is", flush=True)
        print("bridge: bound to a network-facing address. Generated a strong one:", flush=True)
        print("bridge:     username: %s" % user, flush=True)
        print("bridge:     password: %s" % pw, flush=True)
        print("bridge: %s" % ("saved to .env" if saved else "could NOT save to .env - set it manually"), flush=True)
        print(banner, flush=True)
    return user, pw


AUTH_USER = ""
AUTH_PASS = ""
if auth_enabled():
    AUTH_USER, AUTH_PASS = resolve_credentials()


UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
_ctx = ssl.create_default_context(); _ctx.check_hostname = False; _ctx.verify_mode = ssl.CERT_NONE


_web_up_cache = {"t": 0.0, "v": False}


def web_up():
    # Cache the probe for ~1s so we don't open a fresh TCP connection on every
    # asset/poll request (adds latency that hurts socket.io responsiveness).
    now = time.time()
    if now - _web_up_cache["t"] < 1.0:
        return _web_up_cache["v"]
    s = socket.socket(); s.settimeout(0.4)
    try:
        s.connect((WEBUI_HOST, WEBUI_PORT)); ok = True
    except Exception:
        ok = False
    finally:
        try: s.close()
        except Exception: pass
    _web_up_cache["v"] = ok; _web_up_cache["t"] = now
    return ok


def stub_get(path):
    with urllib.request.urlopen(STUB + path, timeout=10) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def stub_call(api_idx, method, args=None, **opts):
    """POST a method call to the stub control server and return its JSON."""
    payload = {"api": api_idx, "method": method, "args": args or []}
    payload.update(opts)
    req = urllib.request.Request(STUB + "/api/call", data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8", "replace"))
        except Exception:
            return {"ok": False, "error": "http %d" % e.code}
    except Exception as e:
        return {"ok": False, "error": repr(e)}


# Privileged gateway intents live in the Discord application's "flags" bitfield.
# Each intent has a "full" bit and an "unverified/limited" bit; either one being
# set means the toggle is on in the developer portal. Nighty's bot needs all
# three to work properly, so we require them before handing the token over.
INTENTS = [
    ("Presence Intent",        (1 << 12) | (1 << 13)),
    ("Server Members Intent",  (1 << 14) | (1 << 15)),
    ("Message Content Intent", (1 << 18) | (1 << 19)),
]


def _bot_token_app_id(token):
    """A bot token's first dot-segment is the base64 of its application id."""
    try:
        seg = token.split(".", 1)[0]
        seg += "=" * (-len(seg) % 4)
        return base64.urlsafe_b64decode(seg).decode("ascii")
    except Exception:
        return None


def _discord_bot_get(token, path):
    """GET discord.com/api/v10 authenticated as a bot. Returns (status, json|None)."""
    req = urllib.request.Request("https://discord.com/api/v10" + path, headers={
        "Authorization": "Bot " + token,
        "User-Agent": "DiscordBot (https://github.com/nighty-linux-headless 1.0)"})
    try:
        with urllib.request.urlopen(req, timeout=20, context=_ctx) as r:
            return r.status, json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8", "replace"))
        except Exception:
            return e.code, None
    except Exception:
        return -1, None


def check_bot_token(token):
    """Validate a bot token and report its privileged-intent state.

    We talk to Discord's own API as the bot, so we know — before Nighty ever
    sees the token — whether it is valid and whether the intents the bot needs
    are switched on. Returns a dict the onboarding page understands:

      valid        - the token authenticated as a bot
      app_id       - the application id (used for the intents settings link)
      bot_name     - the bot's username (shown back for confirmation)
      intents_ok   - every required privileged intent is enabled
      missing      - human names of the intents still turned off
      intents_url  - deep link to the Bot tab where the toggles live
      error        - a human message when valid is False
    """
    token = (token or "").strip()
    if not token:
        return {"valid": False, "error": "Paste your bot token first."}
    st, me = _discord_bot_get(token, "/users/@me")
    if st == 401 or not (isinstance(me, dict) and me.get("id")):
        return {"valid": False,
                "error": "Discord rejected this token. Make sure it is a *bot* token "
                         "(Developer Portal -> your app -> Bot -> Reset Token) and that "
                         "you copied all of it."}
    bot_name = me.get("username") or "your bot"
    _, app = _discord_bot_get(token, "/applications/@me")
    app_id = (app.get("id") if isinstance(app, dict) else None) or _bot_token_app_id(token)
    flags = app.get("flags", 0) if isinstance(app, dict) else 0
    missing = [name for name, bits in INTENTS if not (flags & bits)]
    return {"valid": True, "app_id": app_id, "bot_name": bot_name,
            "intents_ok": not missing, "missing": missing,
            "intents_url": "https://discord.com/developers/applications/%s/bot" % app_id}


def _write_app_id(app_id):
    """Reconcile the saved app context's id with the pasted token's application,
    so a later restart auto-logs in against the right application."""
    appdata = find_appdata()
    if not appdata or not app_id:
        return
    try:
        cfg = os.path.join(appdata, "nighty.config")
        d = json.load(open(cfg, encoding="utf-8"))
        changed = False
        for u, info in (d.get("logins") or {}).items():
            ap = (info or {}).get("app")
            if isinstance(ap, dict) and ap.get("token") and ap.get("id") != app_id:
                ap["id"] = app_id; changed = True
        if changed:
            tmp = cfg + ".tmp"
            json.dump(d, open(tmp, "w", encoding="utf-8"), indent=4, ensure_ascii=True)
            os.replace(tmp, cfg)
    except Exception:
        pass


def submit_bot_token(token):
    """Hand a validated bot token to Nighty (its MainApi.getAppTokenInput step).

    The page already ran check_bot_token, but we never trust the page — we
    re-validate the token and its intents here. Then we feed it to Nighty. If
    Nighty has no application context yet (it raises KeyError('app')), we set one
    up by pointing AppCreateApi at the token's OWN application's dev-portal URLs.
    Because we supply the bot token ourselves, Nighty never has to reset it —
    which is exactly why this avoids the developer-portal password and captcha.
    """
    token = (token or "").strip()
    chk = check_bot_token(token)
    if not chk.get("valid"):
        return {"ok": False, "error": chk.get("error", "Invalid bot token.")}
    if not chk.get("intents_ok"):
        return {"ok": False, "needs_intents": True, "missing": chk.get("missing", []),
                "intents_url": chk.get("intents_url"),
                "error": "Turn on the required intents, then re-check."}
    apis = stub_get("/api/methods").get("apis", [])
    main_idx = api_index(apis, "MainApi", 0)

    def feed():
        # getAppTokenInput is long-running: it saves the token and logs the bot
        # in. The stub normally answers {"running": true} at once, but under load
        # the HTTP call can time out while the work continues on the main thread —
        # that is "accepted", not "failed", so don't surface it as an error.
        r = stub_call(main_idx, "getAppTokenInput", [token])
        if not r.get("ok") and "timed out" in (r.get("error") or "").lower():
            return {"ok": True, "running": True}
        return r

    res = feed()
    if not res.get("ok") and "app" in (res.get("error") or "").lower():
        ac_idx = api_index(apis, "AppCreateApi", -1)
        if ac_idx >= 0 and chk.get("app_id"):
            base = "https://discord.com/developers/applications"
            for u in (base, "%s/%s/information" % (base, chk["app_id"]),
                      "%s/%s/bot" % (base, chk["app_id"])):
                stub_call(ac_idx, "url_callback_change", [u]); time.sleep(4)
        res = feed()
    if not res.get("ok"):
        return {"ok": False, "error": res.get("error") or "Nighty did not accept the bot token."}
    _write_app_id(chk.get("app_id"))
    return {"ok": True, "app_id": chk.get("app_id"), "bot_name": chk.get("bot_name")}


def _discord_account_get(token, path):
    """GET discord.com/api/v10 authenticated as a USER account (raw token, no
    'Bot ' prefix). Returns (status, json|None)."""
    req = urllib.request.Request("https://discord.com/api/v10" + path, headers={
        "Authorization": token,
        "User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=20, context=_ctx) as r:
            return r.status, json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8", "replace"))
        except Exception:
            return e.code, None
    except Exception:
        return -1, None


def check_account_token(token):
    """Validate a Discord ACCOUNT token against the API. Returns {ok, username,
    id} on success or {ok:False, error}. The username becomes the login key in
    nighty.config, so we resolve it here rather than trusting the browser."""
    token = (token or "").strip()
    if not token:
        return {"ok": False, "error": "Paste your account token first."}
    st, me = _discord_account_get(token, "/users/@me")
    if st == 401 or not (isinstance(me, dict) and me.get("id")):
        return {"ok": False, "error": "Discord rejected this account token. "
                "Copy the whole token (not your password) and try again."}
    return {"ok": True, "id": me.get("id"), "username": me.get("username") or me.get("id")}


def _bot_authorized(token, app):
    """Pre-flight OAuth check: has the companion bot been authorized/installed yet?

    Guild-install apps expose a reliable, real-time signal — a non-empty guild
    list. User-install apps do NOT: Discord's approximate_user_install_count is
    cached and lags badly right after authorize (a freshly-approved app reads 0
    for a long time, while one installed hours earlier reads 1), so a 0 here is
    NOT proof the user skipped the step, and hard-blocking on it produced false
    "bot is not authorized yet" errors even after a correct user-install.

    So we only ever hard-block an app that is *guild-install only* and sits in no
    guild. For user-installable apps we defer to the authoritative post-boot gate
    instead of the stale count: after provisioning, bot_link_status() reads the
    backend log and, when the bot really is unauthorized, on_ready fails
    (NotFound 10003) — which drives the 'authorize' backstop mode. That gate is
    real-time and never a false positive."""
    _, gl = _discord_bot_get(token, "/users/@me/guilds")
    if isinstance(gl, list) and gl:
        return True
    if (app or {}).get("approximate_user_install_count") or 0:
        return True
    # No guild, cached-0 install count: authorized iff the app supports user
    # install (can't confirm here — let the post-boot auth-screen gate decide).
    return 1 in _app_integration_types(app)


def _write_auth_license(key):
    """Write the Nighty license into auth.json directly (merging any other keys)."""
    p = _auth_path()
    if not p:
        return False
    d = {}
    if os.path.exists(p):
        try:
            d = json.load(open(p, encoding="utf-8")) or {}
        except Exception:
            d = {}
    d["license"] = key
    tmp = p + ".tmp"
    json.dump(d, open(tmp, "w", encoding="utf-8"), indent=4, ensure_ascii=True)
    os.replace(tmp, p)
    return True


def _write_login(username, account_token, app_id, bot_token, make_active=True):
    """Merge one account into nighty.config.logins in Nighty's own on-disk shape,
    so a single backend start boots fully configured. Preserves every other key
    Nighty wrote (its defaults, and any other accounts). When make_active, this
    login becomes the active one and the rest are deactivated."""
    appdata = find_appdata()
    if not appdata:
        return False
    cfg = os.path.join(appdata, "nighty.config")
    d = {}
    if os.path.exists(cfg):
        try:
            d = json.load(open(cfg, encoding="utf-8")) or {}
        except Exception:
            d = {}
    logins = d.get("logins")
    if not isinstance(logins, dict):
        logins = {}
    if make_active:
        for info in logins.values():
            if isinstance(info, dict):
                info["active"] = False
    logins[username] = {
        "token": account_token,
        "date_added": time.strftime("%d %B %Y, at %H:%M:%S"),
        "active": bool(make_active),
        "app": {"id": app_id, "token": bot_token},
    }
    d["logins"] = logins
    d["web"] = True
    tmp = cfg + ".tmp"
    json.dump(d, open(tmp, "w", encoding="utf-8"), indent=4, ensure_ascii=True)
    os.replace(tmp, cfg)
    return True


def _restart_backend():
    """Bounce Nighty's backend; run.sh's loop relaunches it in ~3s."""
    try:
        os.system("pkill -f '[N]ighty_stub'")
    except Exception:
        pass


def _boot_link_signals(seg):
    """Classify one backend-log segment (the text after a 'CTL server up' marker)
    into the raw bot-link signals shared by both the polled probe and the cached
    status check, so the two can never drift apart:
      on_ready_failed  - the companion bot's on_ready raised NotFound(10003) /
                         'Unknown Channel' from application_commands (the bot is no
                         longer authorized on the account).
      gateway_rejected - the account gateway was rejected (HTTP 403).
      positive         - the bot logged in AND synced its commands (healthy link).
    """
    on_ready_failed = ("Ignoring exception in on_ready" in seg and "application_commands" in seg
                       and ("10003" in seg or "Unknown Channel" in seg or "NotFound" in seg))
    gateway_rejected = "server rejected WebSocket connection: HTTP 403" in seg
    positive = "Logged in as" in seg and "Commands synced" in seg
    return on_ready_failed, gateway_rejected, positive


def _boot_gen():
    """How many times the backend has booted so far — one 'CTL server up' line per
    launch. Used as a boundary so post-restart verification reads the NEW boot's
    log segment, never a stale success from the previous account."""
    p = _backend_log_path()
    if not p:
        return 0
    try:
        return _read_backend_log_tail(p).count("CTL server up")
    except Exception:
        return 0


def _bot_link_probe(prev_gen):
    """One-shot (non-blocking) authoritative check of whether the companion bot
    actually linked after a restart, reading Nighty's own backend log rather than
    Discord's cached install counts (which lag in both directions and cannot tell
    an authorized user-install from a de-authorized one). Scoped to the NEW boot
    only — a "CTL server up" beyond prev_gen — so a stale success from the
    previous account is never read. Returns:
      'booting'      - the new backend has not produced a verdict yet.
      'connected'    - positive proof in the new boot: bot logged in AND commands
                       synced (what a real, authorized bot always emits).
      'unauthorized' - on_ready failed (NotFound 10003 / Unknown Channel) or the
                       account gateway was rejected (HTTP 403).
    This is polled by get_state so the /provision HTTP request never has to block
    on the ~60-90s boot (a long-held request was dropping on the LAN and showing
    a false 'Bridge not reachable' even though provisioning had succeeded)."""
    p = _backend_log_path()
    if not p:
        return "booting"
    try:
        lines = _read_backend_log_tail(p).splitlines()
    except Exception:
        lines = []
    idxs = [i for i, ln in enumerate(lines) if "CTL server up" in ln]
    if len(idxs) <= prev_gen:                    # new backend not up yet
        return "booting"
    seg = "\n".join(lines[idxs[-1]:])
    on_ready_failed, gateway_rejected, positive = _boot_link_signals(seg)
    if on_ready_failed or gateway_rejected:
        return "unauthorized"
    if positive:
        return "connected"
    return "booting"


def _verify_path():
    ad = find_appdata()
    return os.path.join(ad, ".provision_verify") if ad else None


def _write_verify(gen):
    """Mark that a provisioning just restarted the backend and its bot link must
    be verified (against boot generation `gen`) by the next get_state polls."""
    p = _verify_path()
    if not p:
        return
    try:
        json.dump({"gen": int(gen), "ts": time.time()}, open(p, "w", encoding="utf-8"))
    except Exception:
        pass


def _read_verify():
    p = _verify_path()
    if not p or not os.path.exists(p):
        return None
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return None


def _clear_verify():
    p = _verify_path()
    try:
        if p and os.path.exists(p):
            os.remove(p)
    except Exception:
        pass


def _add_account_path():
    ad = find_appdata()
    return os.path.join(ad, ".add_account") if ad else None


def add_account_active():
    """True while add_account.sh has put the box into 'add another account' mode.
    In that mode the bridge re-serves the setup wizard (retitled) even though the
    box is already onboarded/locked, so a second account can be provisioned."""
    p = _add_account_path()
    return bool(p and os.path.exists(p))


def _clear_add_account():
    p = _add_account_path()
    try:
        if p and os.path.exists(p):
            os.remove(p)
    except Exception:
        pass


def provision(license_key, account_token, bot_token, make_active=True, restart=True):
    """One-shot onboarding. Validate the license/tokens against Discord, enforce
    the OAuth gate, then write auth.json + nighty.config directly so Nighty boots
    fully set up in a single start (no step-by-step stub feeding or restarts).
    Returns {ok:True, username, bot_name} or {ok:False, step, error, ...}."""
    license_key = (license_key or "").strip()
    account_token = (account_token or "").strip()
    bot_token = (bot_token or "").strip()
    if not (account_token and bot_token):
        return {"ok": False, "error": "Account token and bot token are required."}
    # A Nighty install has ONE license, shared by every account. Require one only
    # when none is saved yet; adding another account reuses the saved license (the
    # wizard skips the license step), so an empty license_key is fine there.
    if not license_key and not license_set():
        return {"ok": False, "step": "license", "error": "A Nighty license key is required."}
    acct = check_account_token(account_token)
    if not acct.get("ok"):
        return {"ok": False, "step": "account", "error": acct.get("error")}
    chk = check_bot_token(bot_token)
    if not chk.get("valid"):
        return {"ok": False, "step": "bot", "error": chk.get("error")}
    if not chk.get("intents_ok"):
        return {"ok": False, "step": "bot", "needs_intents": True,
                "missing": chk.get("missing"), "intents_url": chk.get("intents_url"),
                "error": "Enable the required intents, then finish."}
    _, app = _discord_bot_get(bot_token, "/applications/@me")
    app = app if isinstance(app, dict) else {}
    app_id = chk.get("app_id")
    if not _bot_authorized(bot_token, app):
        return {"ok": False, "step": "oauth",
                "authorize_url": bot_authorize_url(app_id, _app_integration_types(app)),
                "error": "The bot is not authorized yet. Open 'Authorize on Discord', "
                         "approve it, then press finish."}
    # Only (over)write the license when a new one was entered; otherwise keep the
    # one already on disk (add-account case).
    if license_key and not _write_auth_license(license_key):
        return {"ok": False, "error": "Could not write the license file."}
    prev_gen = _boot_gen()
    if not _write_login(acct["username"], account_token, app_id, bot_token, make_active=make_active):
        return {"ok": False, "error": "Could not write the account into nighty.config."}
    if restart:
        _restart_backend()
        # Authoritative OAuth verification happens in get_state (polled by the
        # client), NOT here: Discord's install counts are cached and lag in both
        # directions, so the real signal is whether Nighty's own companion bot
        # links after boot. We must NOT block this HTTP request on the ~60-90s
        # boot — a long-held request was dropping on the LAN and surfacing a false
        # "Bridge not reachable" even though provisioning had succeeded. So we
        # arm a verify marker and return immediately; get_state then reports
        # loading -> main (bot linked) or authorize (bot not authorized).
        _write_verify(prev_gen)
    return {"ok": True, "username": acct["username"], "bot_name": chk.get("bot_name")}


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "identity"})
    with urllib.request.urlopen(req, timeout=15, context=_ctx) as r:
        return r.read(), r.headers.get("Content-Type", "text/html")


def api_index(apis, typ, default=0):
    return next((a["index"] for a in apis if a["type"] == typ), default)


def saved_app_id():
    appdata = find_appdata()
    if not appdata:
        return None
    try:
        cfg = os.path.join(appdata, "nighty.config")
        d = json.load(open(cfg, encoding="utf-8"))
        for u, info in (d.get("logins") or {}).items():
            ap = (info or {}).get("app") or {}
            if ap.get("id"):
                return ap["id"]
    except Exception:
        pass
    return None


def saved_account_token():
    """The Discord account token of the active login (if any)."""
    appdata = find_appdata()
    if not appdata:
        return None
    try:
        d = json.load(open(os.path.join(appdata, "nighty.config"), encoding="utf-8"))
        for u, info in (d.get("logins") or {}).items():
            info = info or {}
            if info.get("active") and info.get("token"):
                return info["token"]
    except Exception:
        pass
    return None


def saved_bot_token():
    """The bot/app token saved for the active account (if any)."""
    appdata = find_appdata()
    if not appdata:
        return None
    try:
        d = json.load(open(os.path.join(appdata, "nighty.config"), encoding="utf-8"))
        for u, info in (d.get("logins") or {}).items():
            info = info or {}; app = info.get("app") or {}
            if info.get("active") and app.get("token"):
                return app["token"]
    except Exception:
        pass
    return None


def _app_integration_types(app):
    """Discord integration types the app supports: 0 = guild install, 1 = user install."""
    try:
        cfg = (app or {}).get("integration_types_config") or {}
        return sorted(int(k) for k in cfg.keys())
    except Exception:
        return []


def bot_authorize_url(app_id, integration_types=None):
    """Build the Discord OAuth2 authorization URL the user opens to link the bot.

    Nighty's companion bot is added to the user's Discord through the standard
    OAuth2 authorize flow. A user-installable app (integration type 1) is added
    to the account itself; a classic app is invited to a server (type 0). We pick
    the right URL from the app's configured integration types so the link works
    on the first click without the user choosing options manually."""
    if not app_id:
        return None
    base = "https://discord.com/oauth2/authorize"
    types = integration_types or []
    # Nighty's companion bot runs on the ACCOUNT, so prefer user-install
    # (integration_type=1) whenever the app supports it — even when it also allows
    # guild install. The guild ("add to server") flow is only correct for apps
    # that do NOT support user-install at all, so it stays as the fallback.
    if 1 in types:
        return "%s?client_id=%s&integration_type=1&scope=applications.commands" % (base, app_id)
    return "%s?client_id=%s&scope=bot+applications.commands&permissions=8" % (base, app_id)


def authorization_status():
    """Report whether the bot is authorized on the account yet, plus the link to fix it.

    We ask Discord, as the bot, whether it is installed anywhere — added to at
    least one server (guild install) or installed on at least one account (user
    install). When it is installed nowhere, Nighty parks on its auth screen and
    the Web UI never starts; authorize_url is what the user opens to authorize it.
    The 'authorized' flag is a best-effort hint (Discord's install counts are
    approximate and cached) — the authoritative signal we gate the UI on is Nighty
    itself parking on auth.html."""
    tok = saved_bot_token()
    app_id = saved_app_id()
    out = {"authorized": None, "app_id": app_id, "authorize_url": bot_authorize_url(app_id),
           "bot_name": None, "integration": "guild"}
    if not tok:
        return out
    st, app = _discord_bot_get(tok, "/applications/@me")
    if isinstance(app, dict) and app.get("id"):
        types = _app_integration_types(app)
        out["app_id"] = app.get("id")
        out["bot_name"] = app.get("name")
        out["integration"] = "user" if 1 in types else "guild"
        out["authorize_url"] = bot_authorize_url(app.get("id"), types)
        _, gl = _discord_bot_get(tok, "/users/@me/guilds")
        guilds = len(gl) if isinstance(gl, list) else 0
        users = app.get("approximate_user_install_count") or 0
        out["authorized"] = bool(guilds) or bool(users)
    return out


def _auth_path():
    ad = find_appdata()
    return os.path.join(ad, "auth.json") if ad else None


def _lock_path():
    ad = find_appdata()
    return os.path.join(ad, ".setup_locked") if ad else None


def setup_locked():
    """True once the initial setup has fully completed (license verified, tokens
    saved, and a working/authorized bot observed).

    Lockdown is a security boundary: while locked, every setup and authorization
    screen is bypassed and the app boots straight into the working state, so a
    backend restart can never re-open the authorize window (which would otherwise
    let someone re-link the bot/account from the UI). The only supported way back
    to the setup flow is uninstall.sh's 'Reset Configuration Only', which deletes
    this lock together with the saved config."""
    p = _lock_path()
    return bool(p and os.path.exists(p))


def _set_lock():
    """Persist the lockdown marker (idempotent)."""
    p = _lock_path()
    if not p or os.path.exists(p):
        return
    try:
        with open(p, "w", encoding="utf-8") as f:
            f.write("locked %s\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
    except Exception:
        pass


def license_set():
    """True if a Nighty license key is already saved in auth.json."""
    p = _auth_path()
    if not p:
        return False
    try:
        return bool((json.load(open(p, encoding="utf-8")) or {}).get("license"))
    except Exception:
        return False


def save_license(key):
    """Save the user's Nighty license key (step 0 of onboarding).

    Without a license Nighty connects to its own server unlicensed; the bot's
    on_ready then aborts with KeyError('motd') *before* the slash-command tree is
    synced — so the bot shows up online but every command answers "not found".
    We let Nighty's own saver run (for any in-memory activation) and also write
    auth.json directly so it is in place for the next launch.
    """
    key = (key or "").strip()
    if not key:
        return {"ok": False, "error": "Enter your Nighty license key."}
    try:
        apis = stub_get("/api/methods").get("apis", [])
        stub_call(api_index(apis, "MainApi", 0), "saveKeyToAppdata", [key])
    except Exception:
        pass
    p = _auth_path()
    if p:
        try:
            d = {}
            if os.path.exists(p):
                try: d = json.load(open(p, encoding="utf-8")) or {}
                except Exception: d = {}
            d["license"] = key
            tmp = p + ".tmp"
            json.dump(d, open(tmp, "w", encoding="utf-8"), indent=4, ensure_ascii=True)
            os.replace(tmp, p)
        except Exception as e:
            return {"ok": False, "error": "Could not save the license: %r" % (e,)}
    if not license_set():
        return {"ok": False, "error": "License was not saved — check the key and try again."}
    # Nighty reads the license once, at startup, so a key saved into a running
    # backend is ignored by the in-memory client (the bot would still hit
    # KeyError('motd')). Restart the backend so it boots WITH the license on
    # disk. run.sh's keep-alive loop relaunches it. We do this now, before any
    # account is active, so the selfbot does not auto-start and race the rest of
    # onboarding — sign-in + bot then run on a freshly-licensed backend.
    try:
        os.system("pkill -f '[N]ighty_stub'")
    except Exception:
        pass
    return {"ok": True, "restarting": True}


def _backend_log_path():
    diag = os.environ.get("NIGHTY_DIAG_DIR")
    if diag:
        p = os.path.join(diag, "backend.log")
        if os.path.exists(p):
            return p
    nh = os.environ.get("NIGHTY_HOME")
    if nh:
        p = os.path.join(nh, "backend.log")
        if os.path.exists(p):
            return p
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    fallback = os.path.join(here, "diagnostics", "backend.log")
    return fallback if os.path.exists(fallback) else (os.path.join(nh, "backend.log") if nh else None)


def _read_backend_log_tail(path, max_bytes=None):
    """Read a bounded tail from Nighty's append-only backend log.

    The log can grow to tens of megabytes. Reading it in full every five seconds
    made bridge state checks hit the SD card and allocate large strings. A few
    MiB contains many complete boot segments while bounding both I/O and memory.
    """
    if max_bytes is None:
        max_bytes = int(os.environ.get("BACKEND_LOG_SCAN_BYTES", str(4 * 1024 * 1024)))
    with open(path, "rb") as stream:
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        offset = max(0, size - max_bytes)
        stream.seek(offset)
        raw = stream.read()
    if offset:
        split = raw.find(b"\n")
        raw = raw[split + 1:] if split >= 0 else b""
    return raw.decode("utf-8", "replace")


_link_cache = {"t": 0.0, "v": None}


def bot_link_status():
    """Whether the companion bot is actually authorized/connected on the account.

    This is the authoritative 'is the bot linked?' check, and it deliberately does
    NOT trust Discord's application install counts: those are approximate and
    cached, so a disconnected user-install bot still reports a stale install (a
    false positive). Nighty also does not halt when the bot is disconnected — it
    boots, syncs global commands and serves the panel regardless — so 'panel is up'
    is not proof the bot works either.

    Instead we read Nighty's own backend log. When the bot is no longer authorized
    on the account, the companion bot's on_ready can't reach its command channel
    and raises NotFound(10003)/'Unknown Channel' from application_commands; the
    account selfbot's gateway is also rejected (HTTP 403). We scope the scan to the
    CURRENT backend process (everything after the last 'CTL server up' marker) so a
    stale failure from a previous boot does not linger after a fresh, re-authorized
    start. Cached ~5s so it is cheap to call per request."""
    now = time.time()
    if _link_cache["v"] is not None and now - _link_cache["t"] < 5.0:
        return _link_cache["v"]
    out = {"connected": True, "reason": None}
    p = _backend_log_path()
    if p and os.path.exists(p):
        try:
            lines = _read_backend_log_tail(p).splitlines()
        except Exception:
            lines = []
        start = 0
        for i, ln in enumerate(lines):
            if "CTL server up" in ln:        # one per backend launch
                start = i
        seg = "\n".join(lines[start:])
        on_ready_failed, gateway_rejected, _ = _boot_link_signals(seg)
        if on_ready_failed or gateway_rejected:
            out = {"connected": False,
                   "reason": "bot_unauthorized" if on_ready_failed else "account_rejected"}
    _link_cache["v"] = out
    _link_cache["t"] = now
    return out


def _onboarded():
    """License + account token + bot token all saved (past the setup pages)."""
    return bool(license_set() and saved_account_token() and saved_bot_token())


def panel_blocked():
    """True when the box is onboarded but the bot is not actually linked.

    Used to stop the bridge from serving the native panel in the unauthorized
    state — Nighty itself would happily serve it, so the gate lives here. Once the
    setup is locked, the panel is never blocked: a completed install always boots
    straight through."""
    if setup_locked():
        return False
    if not (license_set() and saved_account_token() and saved_bot_token()):
        return False
    return not bot_link_status()["connected"]


def get_state():
    ev = stub_get("/bridge/events?since=0")
    methods = stub_get("/api/methods")
    apis = methods.get("apis", [])
    types = [a["type"] for a in apis]
    cur = ev.get("current_url") or ""
    main_idx = api_index(apis, "MainApi", 0)
    app_id = saved_app_id()
    # The popup ("Create app") window can leave a stale discord.com URL in
    # current_url; track the MAIN window's own last URL so we read Nighty's real
    # screen, not a leftover from a side window.
    master_url = ""
    for e in ev.get("events", []):
        if e.get("uid") == "master" and e.get("url"):
            master_url = e["url"]
    if not master_url:
        master_url = cur
    login_markers = ("loading.html", "discord_login.html", "auth.html")
    # Post-provision verification window. /provision no longer blocks on the boot
    # (that dropped on the LAN and showed a false "Bridge not reachable"); it arms
    # a verify marker and returns at once, and the client polls here. We gate on
    # Nighty's own backend log — the authoritative signal — not Discord's cached
    # install counts. This also covers add-account on a locked box, which the
    # plain "locked => main" shortcut below would otherwise wave straight through.
    verify = _read_verify()
    if verify is not None and (time.time() - verify.get("ts", 0)) > 300:
        # Safety net: never let a stuck verification (e.g. a backend that never
        # emits a verdict) brick the panel — give up after 5 min and let the
        # normal main/authorize logic take over.
        _clear_verify()
        verify = None
    if verify is not None:
        probe = _bot_link_probe(verify.get("gen", 0))
        if probe != "connected":
            # 'booting' -> keep the spinner; 'unauthorized' -> bot was never / no
            # longer authorized, show the authorize backstop. The marker stays so
            # this holds until the user re-authorizes and finishes again (which
            # re-arms it with a fresh boot generation).
            return {"mode": "authorize" if probe == "unauthorized" else "loading",
                    "cur": cur, "main_idx": main_idx, "total": ev.get("total", 0),
                    "app_id": app_id, "events": ev.get("events", []),
                    "locked": setup_locked(), "ready": bool(web_up())}
        # Verified: the bot really linked. Leave verification (and add-account)
        # mode and fall through to the normal main/lock path.
        _clear_verify()
        _clear_add_account()
    locked = setup_locked()
    if locked:
        # Lockdown: setup already completed once. Never re-enter any setup or
        # authorization screen — boot straight into the working state. (Reset is
        # only possible via uninstall.sh's 'Reset Configuration Only'.)
        mode = "main"
    elif not _onboarded():
        # Not fully onboarded yet (license, account token or bot token missing).
        # The new wizard collects everything client-side and provisions in one
        # shot, so we no longer track Nighty's per-screen sub-state — a single
        # 'setup' mode drives the whole flow regardless of Nighty's current page.
        mode = "setup"
    elif not bot_link_status()["connected"]:
        # Onboarded but the bot is not actually linked (Nighty boots and serves
        # the panel even when the bot is disconnected, so check the link
        # explicitly) — present the authorization backstop.
        mode = "authorize"
    else:
        mode = "main"
    # Acquire the lock the first time a complete, working setup is observed: the
    # native panel is actually serving AND the bot link is healthy. From then on
    # the box is locked into the working state across restarts.
    if not locked and mode == "main" and web_up() and license_set() \
            and saved_account_token() and saved_bot_token() and bot_link_status()["connected"]:
        _set_lock()
        locked = True
    return {"mode": mode, "cur": cur, "main_idx": main_idx, "total": ev.get("total", 0),
            "app_id": app_id, "events": ev.get("events", []), "locked": locked,
            "ready": bool(web_up())}


STATE_PUBLIC_FIELDS = ("mode", "ready", "locked")


def public_state(state, is_local):
    if is_local:
        return state
    return {k: state.get(k) for k in STATE_PUBLIC_FIELDS if k in state}


CSS = """:root{--bg:#080a0f;--panel:#0f131c;--panel-2:#0c0f17;--line:#1b2233;--line-2:#232c42;
--txt:#eef2fb;--mut:#8b96ad;--mut-2:#5c6885;--brand:#6d8bff;--brand-2:#8ba6ff;--ok:#37d399;--err:#ff6b6b;--warn:#f5b544;--radius:14px}
*{box-sizing:border-box}html,body{height:100%}
body{margin:0;font-family:'Inter','Segoe UI',system-ui,-apple-system,Arial,sans-serif;color:var(--txt);background:var(--bg);
display:flex;align-items:center;justify-content:center;padding:24px;line-height:1.5;-webkit-font-smoothing:antialiased;
background-image:radial-gradient(60rem 40rem at 88% -12%,rgba(109,139,255,.10),transparent 60%),radial-gradient(50rem 40rem at -10% 110%,rgba(55,211,153,.06),transparent 55%)}
.shell{width:100%;max-width:440px}
.brand{display:flex;align-items:center;gap:11px;margin:0 2px 18px}
.brand .mark{width:38px;height:38px;flex:0 0 auto;background:url('data:image/png;base64,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') center/contain no-repeat;filter:drop-shadow(0 6px 14px rgba(77,107,255,.35))}
.brand .name{font-weight:700;font-size:15px;letter-spacing:.2px}.brand .name span{color:var(--mut)}
.card{background:linear-gradient(180deg,var(--panel),var(--panel-2));border:1px solid var(--line);border-radius:var(--radius);box-shadow:0 24px 60px -20px rgba(0,0,0,.7);overflow:hidden}
.steps{display:flex;gap:6px;padding:16px 20px 0}
.steps .seg{height:3px;flex:1;border-radius:99px;background:var(--line-2);overflow:hidden;position:relative}
.steps .seg i{position:absolute;inset:0;width:0;background:linear-gradient(90deg,var(--brand),var(--brand-2));border-radius:99px;transition:width .45s cubic-bezier(.4,0,.2,1)}
.steps .seg.done i,.steps .seg.active i{width:100%}
.body{padding:22px 24px 24px}
.eyebrow{font-size:11px;font-weight:600;letter-spacing:.14em;text-transform:uppercase;color:var(--brand-2);margin-bottom:9px}
h1{font-size:21px;font-weight:700;margin:0 0 6px;letter-spacing:-.2px}
.lead{color:var(--mut);font-size:13.5px;margin:0 0 20px}.lead b{color:var(--txt);font-weight:600}
label{display:block;font-size:11px;font-weight:600;letter-spacing:.08em;text-transform:uppercase;color:var(--mut-2);margin:0 0 8px}
.field{position:relative;margin-bottom:4px}
input{width:100%;background:var(--panel-2);border:1px solid var(--line-2);border-radius:11px;color:var(--txt);padding:13px 42px 13px 14px;font-size:14px;outline:none;transition:border-color .15s,box-shadow .15s;font-family:inherit}
input::placeholder{color:#48546f}
input:focus{border-color:var(--brand);box-shadow:0 0 0 3px rgba(109,139,255,.16)}
input.bad{border-color:var(--err);box-shadow:0 0 0 3px rgba(255,107,107,.14)}
input.good{border-color:var(--ok);box-shadow:0 0 0 3px rgba(55,211,153,.14)}
.reveal{position:absolute;right:6px;top:50%;transform:translateY(-50%);background:none;border:0;color:var(--mut-2);cursor:pointer;padding:8px;border-radius:8px;display:grid;place-items:center}
.reveal:hover{color:var(--txt);background:var(--line)}
.hint{font-size:12px;color:var(--mut-2);margin:9px 2px 0;line-height:1.55}.hint a{color:var(--brand-2);text-decoration:none}.hint a:hover{text-decoration:underline}
.callout{margin-top:14px;border:1px solid var(--line-2);border-radius:11px;padding:12px 14px;background:var(--panel-2);font-size:12.5px;color:var(--mut)}
.callout.warn{border-color:#5a4413;background:rgba(245,181,68,.06)}
.callout.warn .h{color:var(--warn);font-weight:600;margin-bottom:5px;display:flex;align-items:center;gap:7px}
.callout ul{margin:6px 0 0;padding-left:18px}.callout li{margin:3px 0;color:var(--txt)}
.actions{display:flex;gap:10px;margin-top:22px}
.btn{flex:1;border:0;border-radius:11px;padding:13px;font-size:14px;font-weight:600;cursor:pointer;font-family:inherit;transition:transform .08s,filter .15s,opacity .15s;display:flex;align-items:center;justify-content:center;gap:8px;text-decoration:none}
.btn:active{transform:translateY(1px)}
.btn.primary{background:linear-gradient(135deg,var(--brand),#4d6bff);color:#fff;box-shadow:0 8px 22px -10px rgba(77,107,255,.8)}
.btn.primary:disabled{opacity:.45;cursor:not-allowed;box-shadow:none}
.btn.ghost{flex:0 0 auto;padding:13px 16px;background:var(--panel-2);border:1px solid var(--line-2);color:var(--mut)}
.btn.ghost:hover{color:var(--txt)}
.btn.link{background:var(--panel-2);border:1px solid var(--line-2);color:var(--brand-2)}.btn.link:hover{border-color:var(--brand)}
.status{display:flex;align-items:center;gap:9px;margin-top:16px;font-size:13px;color:var(--mut);min-height:20px}
.status.ok{color:var(--ok)}.status.err{color:var(--err)}.status.busy{color:var(--brand-2)}
.dot{width:7px;height:7px;border-radius:50%;background:currentColor;flex:none;opacity:0;transition:opacity .12s}
.status.ok .dot,.status.err .dot,.status.busy .dot{opacity:1}
.status.busy .dot{animation:pulse 1.1s ease-in-out infinite}
@keyframes pulse{0%,100%{opacity:.35;transform:scale(.85)}50%{opacity:1;transform:scale(1)}}
.spin{width:15px;height:15px;border:2px solid rgba(255,255,255,.25);border-top-color:#fff;border-radius:50%;animation:rot .7s linear infinite}
.spin.big{width:44px;height:44px;border-width:4px;border-color:var(--line-2);border-top-color:var(--brand-2);margin:6px auto 0}
@keyframes rot{to{transform:rotate(360deg)}}
.foot{padding:14px 24px;border-top:1px solid var(--line);color:var(--mut-2);font-size:11px;background:var(--panel-2);display:flex;align-items:center;gap:8px;line-height:1.5}
.scene{animation:sc .35s cubic-bezier(.2,.7,.3,1)}@keyframes sc{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
.center{text-align:center}"""

COMMON_JS = """
var msg=document.getElementById('msg'),st=document.getElementById('st'),dot=document.getElementById('dot');
function set(t,cls){msg.textContent=t;st.className='status'+(cls?' '+cls:'');dot.className='dot '+(cls==='ok'?'ok':cls==='err'?'err':'busy');}
// Robust JSON fetch. Returns parsed JSON, or null if the response was not JSON
// (e.g. an HTML page the bridge can briefly return while the backend is
// restarting / mid-proxy). Callers treat null as "not ready yet" and retry,
// instead of crashing on "Unexpected token '<'".
async function jtry(path,opts){try{var r=await fetch(path,Object.assign({cache:'no-store'},opts||{}));var t=await r.text();try{return JSON.parse(t);}catch(e){return null;}}catch(e){return null;}}
// POST JSON, auto-retrying while the response is non-JSON (backend still coming up).
async function jpost(path,body,onwait){for(var i=0;i<60;i++){var j=await jtry(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body||{})});if(j!==null)return j;if(onwait)onwait(i+1);await new Promise(function(r){setTimeout(r,2500);});}return {ok:false,error:'backend not responding (still starting?) — reload to retry'};}
async function rpc(idx,method,args){var j=await jpost('/rpc',{api:idx,method:method,args:args||[]});if(!j||!j.ok)throw new Error((j&&j.error)||'rpc');return j.result;}
"""


def setup_page(title="Nighty <span>&middot; Setup</span>", first_lead=None, skip_license=None):
    """Single continuous onboarding wizard: License -> Account token -> Bot token
    -> Authorize, collected client-side and provisioned in one shot (see
    /provision). Replaces the old multi-page, stub-driven, restart-between-steps
    flow. `title`/`first_lead` let add_account.sh retitle it for extra accounts.

    A Nighty install has ONE license shared across all accounts, so when a working
    license is already saved (skip_license, default: auto-detect via license_set)
    the wizard shows the license step as already done and starts at the account
    step; /provision then reuses the saved license."""
    sl = license_set() if skip_license is None else bool(skip_license)
    return ("""<!DOCTYPE html><html lang=en><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Nighty &mdash; Setup</title><style>%s</style></head><body>
<div class=shell>
  <div class=brand><div class=mark></div><div class=name>%s</div></div>
  <div class=card>
    <div class=steps id=steps><div class=seg><i></i></div><div class=seg><i></i></div><div class=seg><i></i></div><div class=seg><i></i></div></div>
    <div class=body id=body></div>
    <div class=foot><svg width=12 height=12 viewBox="0 0 24 24" fill=none stroke=currentColor stroke-width=2><path d="M12 2 4 6v6c0 5 3.5 8 8 10 4.5-2 8-5 8-10V6z"/></svg>Everything stays on this device &mdash; keys are written only to Nighty's local config.</div>
  </div>
</div>
<script>
var LEAD=%s;var SKIP_LICENSE=%s;var MINSTEP=SKIP_LICENSE?1:0;
var STEPS=[
 {eye:'Step 1 of 4',title:'Activate Nighty',lead:LEAD||'Enter your <b>Nighty license key</b>. Nighty needs it to start &mdash; without it the bot signs in but no commands work.',label:'License key',ph:'Paste your Nighty license key',help:'From your Nighty purchase (dashboard or order email).',check:null},
 {eye:'Step 2 of 4',title:'Sign in',lead:'Paste your <b>Discord account token</b>. This is the account Nighty runs as.',label:'Account token',ph:'Paste your Discord account token',help:'Token only, never your password. Sent straight to your local backend.',check:'/check_account'},
 {eye:'Step 3 of 4',title:'Connect your bot',lead:'Paste your <b>bot token</b>. It is verified and checked for the required intents before it is used.',label:'Bot token',ph:'Paste your bot token',help:'Developer Portal &rarr; your app &rarr; Bot &rarr; Reset Token. Enable Presence, Server Members and Message Content.',check:'/check_bot'}
];
var i=MINSTEP,vals=['','',''],reveal=false,authUrl='';
function el(id){return document.getElementById(id);}
async function jpost(p,b){try{var r=await fetch(p,{method:'POST',cache:'no-store',headers:{'Content-Type':'application/json'},body:JSON.stringify(b||{})});var t=await r.text();try{return JSON.parse(t);}catch(e){return null;}}catch(e){return null;}}
async function jget(p){try{var r=await fetch(p,{cache:'no-store'});var t=await r.text();try{return JSON.parse(t);}catch(e){return null;}}catch(e){return null;}}
function segs(){var s=el('steps').children;for(var k=0;k<4;k++)s[k].className='seg'+(k<i?' done':k===i?' active':'');}
var EYE='<svg width=17 height=17 viewBox="0 0 24 24" fill=none stroke=currentColor stroke-width=2><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7z"/><circle cx=12 cy=12 r=3/></svg>';
function setmsg(t,c){var st=el('st');if(!st)return;st.className='status'+(c?' '+c:'');el('msg').innerHTML=t||'&nbsp;';}
function busy(b,on,label){b.disabled=on;if(on){if(!b.dataset.t)b.dataset.t=b.innerHTML;b.innerHTML='<span class=spin></span>'+(label||'Working&hellip;');}else if(b.dataset.t){b.innerHTML=b.dataset.t;b.dataset.t='';}}
function render(){
 segs();
 if(i<3){var s=STEPS[i];
  el('body').innerHTML='<div class=scene><div class=eyebrow>'+s.eye+'</div><h1>'+s.title+'</h1><p class=lead>'+s.lead+'</p>'
   +'<label>'+s.label+'</label><div class=field><input id=inp type='+(reveal?'text':'password')+' placeholder="'+s.ph+'" autocomplete=off spellcheck=false value="'+(vals[i]||'').replace(/"/g,'&quot;')+'"><button class=reveal id=rev title="Show / hide">'+EYE+'</button></div>'
   +'<p class=hint>'+s.help+'</p><div id=extra></div>'
   +'<div class=actions>'+(i>MINSTEP?'<button class="btn ghost" id=back>Back</button>':'')+'<button class="btn primary" id=next>Continue</button></div>'
   +'<div class=status id=st><span class=dot></span><span id=msg>&nbsp;</span></div></div>';
  var inp=el('inp');inp.focus();
  el('rev').onclick=function(){reveal=!reveal;vals[i]=inp.value;render();};
  if(el('back'))el('back').onclick=function(){vals[i]=inp.value;i--;reveal=false;render();};
  el('next').onclick=go;inp.addEventListener('keydown',function(e){if(e.key==='Enter')go();});
 }else{
  el('body').innerHTML='<div class=scene><div class=eyebrow>Step 4 of 4</div><h1>Authorize the bot</h1><p class=lead>One-time Discord step. Authorize the companion bot, then confirm &mdash; Nighty will not start until it is linked to your account.</p>'
   +'<a class="btn link" id=auth href="'+(authUrl||'#')+'" target=_blank rel=noopener>Authorize on Discord &#8599;</a>'
   +'<div class="callout warn"><div class=h><svg width=14 height=14 viewBox="0 0 24 24" fill=none stroke=currentColor stroke-width=2><path d="M12 9v4M12 17h.01M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/></svg>Authorization required</div>Nighty refuses to start if the bot is not linked. Authorize first, then confirm below.</div>'
   +'<div class=actions><button class="btn ghost" id=back>Back</button><button class="btn primary" id=fin>I\\'ve authorized &mdash; finish</button></div>'
   +'<div class=status id=st><span class=dot></span><span id=msg>&nbsp;</span></div></div>';
  el('back').onclick=function(){i--;reveal=false;render();};
  el('fin').onclick=finish;
 }
}
async function go(){
 var inp=el('inp'),v=(inp.value||'').trim();vals[i]=v;el('extra').innerHTML='';inp.classList.remove('bad','good');
 if(!v){inp.classList.add('bad');setmsg('This field is required.','err');return;}
 var s=STEPS[i],btn=el('next');
 if(!s.check){inp.classList.add('good');i++;reveal=false;render();return;}
 busy(btn,true,'Verifying&hellip;');setmsg('Checking with Discord&hellip;','busy');
 var j=await jpost(s.check,{token:v});busy(btn,false);
 if(!j){setmsg('Bridge not reachable &mdash; try again.','err');return;}
 if(s.check==='/check_bot'){
  if(!j.valid){inp.classList.add('bad');setmsg(j.error||'Invalid bot token.','err');return;}
  if(!j.intents_ok){inp.classList.add('bad');setmsg('Some required intents are OFF.','err');
   el('extra').innerHTML='<div class="callout warn"><div class=h>Enable these intents, then continue</div><ul>'+(j.missing||[]).map(function(m){return '<li>'+m+'</li>';}).join('')+'</ul><a class="btn link" style="margin-top:10px" target=_blank rel=noopener href="'+(j.intents_url||'#')+'">Open Bot settings &#8599;</a></div>';return;}
  authUrl=j.authorize_url||'';
 }else if(!j.ok){inp.classList.add('bad');setmsg(j.error||'Rejected.','err');return;}
 inp.classList.add('good');setmsg('Looks good.','ok');setTimeout(function(){i++;reveal=false;render();},300);
}
async function finish(){
 var btn=el('fin');busy(btn,true,'Provisioning&hellip;');setmsg('Writing configuration and starting Nighty&hellip;','busy');
 var j=await jpost('/provision',{license:vals[0],account_token:vals[1],bot_token:vals[2]});
 if(!j){busy(btn,false);setmsg('Bridge not reachable &mdash; try again.','err');return;}
 if(!j.ok){busy(btn,false);if(j.step==='oauth'&&j.authorize_url){authUrl=j.authorize_url;el('auth').href=authUrl;}setmsg(j.error||'Could not finish setup.','err');return;}
 setmsg('Starting Nighty and verifying the bot link (up to ~3 min)&hellip;','busy');
 var n=0;(function chk(){jget('/state').then(function(s){if(s&&s.mode==='main'&&s.ready){busy(btn,false);setmsg('Bot linked &mdash; loading panel&hellip;','ok');location.href='/';return;}if(s&&s.mode==='authorize'){busy(btn,false);setmsg('The bot is not authorized on the account &mdash; open Authorize on Discord, approve it, then finish again.','err');return;}if(++n>120){location.href='/';return;}setTimeout(chk,3000);});})();
}
render();
</script></body></html>""" % (CSS, title, ("null" if first_lead is None else json.dumps(first_lead)),
        ("true" if sl else "false"))).encode("utf-8")


def authorize_page(app_id=None):
    info = authorization_status()
    url = info.get("authorize_url") or ""
    bot_name = info.get("bot_name") or "your bot"
    integ = info.get("integration") or "guild"
    where = ("to your Discord account" if integ == "user"
             else "to a Discord server you are in")
    disabled = "" if url else "disabled"
    return ("""<!DOCTYPE html><html lang=en><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Nighty &mdash; Authorize</title><style>%s</style></head><body>
<div class=shell>
  <div class=brand><div class=mark></div><div class=name>Nighty <span>&middot; Authorize</span></div></div>
  <div class=card><div class=body>
    <div class=eyebrow>Almost there</div><h1>Authorize the bot</h1>
    <p class=lead>Nighty needs you to <b>authorize %s</b> %s before it can start. One-time Discord step &mdash; no password is handled here.</p>
    <a class="btn link" id=auth href="%s" target=_blank rel=noopener>Authorize on Discord &#8599;</a>
    <div class="callout warn"><div class=h><svg width=14 height=14 viewBox="0 0 24 24" fill=none stroke=currentColor stroke-width=2><path d="M12 9v4M12 17h.01M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/></svg>Authorization required</div>Approve the bot on Discord, then press confirm below. Nighty will not start until it is linked.</div>
    <button class="btn primary" id=go style="width:100%%;margin-top:14px" %s>I&#39;ve authorized &mdash; continue</button>
    <div class=status id=st><span class=dot id=dot></span><span id=msg>%s</span></div>
  </div>
  <div class=foot>Opens Discord&#39;s official authorization page for your application (id %s). The bridge never sees your Discord password.</div></div>
</div>
<script>%s
var go=document.getElementById('go');
go.onclick=async function(){go.disabled=true;set('Applying authorization &mdash; restarting Nighty (about 90 seconds)&hellip;','ok');
 await jpost('/recheck_auth',{});
 var tries=0;(function chk(){jtry('/state').then(function(s){if(s&&s.mode&&s.mode!=='authorize'&&s.mode!=='setup'){set('Authorized &mdash; starting Nighty&hellip;','ok');location.href='/';return;}if(++tries>80){set('Still waiting &mdash; if you authorized, reload in a moment.','err');go.disabled=false;return;}setTimeout(chk,3000);});})();};
</script></body></html>""" % (
        CSS, bot_name, where, url, disabled,
        ("Authorize the bot, then continue." if url else
         "Could not build the authorization link - reload after the bot is ready."),
        info.get("app_id") or app_id or "?", COMMON_JS)).encode("utf-8")


def loading_page():
    """Polished 'waiting for backend' screen shown whenever the panel is not yet
    reachable (early boot, the stub/backend still starting). It polls /ready and
    swaps itself for the real panel the moment the backend answers — so the user
    never sees a bare white error page during startup."""
    return ("""<!DOCTYPE html><html lang=en><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Nighty &mdash; Starting</title><style>%s</style></head><body>
<div class=shell>
  <div class=brand style="justify-content:center"><div class=mark></div><div class=name>Nighty <span>&middot; Starting</span></div></div>
  <div class=card><div class="body center">
    <div class="spin big"></div>
    <p class=lead id=msg style="margin-top:18px">Waiting for the backend to load&hellip;</p>
  </div>
  <div class=foot style="justify-content:center">First start can take a minute or two &mdash; this page continues on its own.</div></div>
</div>
<script>
// Advance as soon as a real screen exists: panel up (mode main + ready), or the
// setup / authorization screen. Any non-loading state moves us on, so we never
// hang on a panel that will not come up.
var n=0, msg=document.getElementById('msg');
function advance(s){
  if(!s||!s.mode) return false;
  if(s.mode==='loading') return false;
  if(s.mode==='main') return !!s.ready;
  return true;   // setup / authorize
}
function poll(){
  fetch('/state',{cache:'no-store'}).then(function(r){return r.json();}).then(function(s){
    if(advance(s)){ msg.textContent='Ready — loading…'; location.replace('/'); return; }
    n++; msg.textContent='Waiting for the backend to load… ('+(n*2)+'s)'; setTimeout(poll,2000);
  }).catch(function(){ n++; setTimeout(poll,2000); });
}
setTimeout(poll,1500);
</script></body></html>""" % (CSS,)).encode("utf-8")


def build_ui():
    try:
        s = get_state()
    except Exception:
        # Stub/backend not reachable yet (early boot) — show the loading screen
        # instead of letting the exception surface as a blank error page.
        return loading_page()
    if s["mode"] == "setup":
        return setup_page()
    if s["mode"] == "authorize":
        return authorize_page(s.get("app_id"))
    # mode == main but the native panel is not up yet — graceful loading screen.
    return loading_page()


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 30
    server_version = "nighty-bridge"
    sys_version = ""

    def log_message(self, *a):
        pass

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, BrokenPipeError, socket.timeout, TimeoutError)):
            return
        try:
            sys.stderr.write("[bridge] %s from %s: %r\n" % (type(exc).__name__, client_address[0], exc))
            sys.stderr.flush()
        except Exception:
            pass

    def _security_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")

    def version_string(self):
        # server_version + " " + sys_version; with sys_version="" the base
        # class emits a trailing space ("nighty-bridge "). Return the clean name.
        return self.server_version

    def _send_status(self, code, body=b""):
        # These are all reject/short-circuit responses that may not have read the
        # request body (401/400/413/403). Under HTTP/1.1 keep-alive an unread body
        # would desync the next request on the connection, so close it.
        self.close_connection = True
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self._security_headers()
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def request_is_local(self):
        try:
            return client_is_loopback(self.client_address[0])
        except Exception:
            return False

    def authorized(self):
        if not auth_enabled():
            return True
        if self.request_is_local():
            return True
        hdr = self.headers.get("Authorization", "")
        if not hdr.startswith("Basic "):
            return False
        try:
            raw = base64.b64decode(hdr[6:].strip()).decode("utf-8", "replace")
            user, _, pw = raw.partition(":")
            # Compare on both fields with no short-circuit: `and` would skip the
            # password check on a username miss and leak, via timing, whether the
            # username was right. `&` forces both compare_digest calls to run.
            # Kept inside the try so a non-ASCII credential (TypeError) fails
            # closed to 401 instead of raising out of the auth gate.
            ok_user = hmac.compare_digest(user, AUTH_USER)
            ok_pass = hmac.compare_digest(pw, AUTH_PASS)
            return bool(ok_user & ok_pass)
        except Exception:
            return False

    def demand_auth(self):
        body = b"authentication required"
        # A 401 on a POST is answered before the request body is read; under
        # HTTP/1.1 keep-alive the unread body would desync the next request
        # (and corrupt the client's authenticated retry on the same socket).
        # Close so the retry opens a fresh connection.
        self.close_connection = True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="%s", charset="UTF-8"' % AUTH_REALM)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self._security_headers()
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _send(self, body, ctype="text/html; charset=utf-8", code=200):
        if isinstance(body, str):
            body = body.encode("utf-8", "replace")
        self.send_response(code); self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store"); self.send_header("Content-Length", str(len(body)))
        self._security_headers()
        self.end_headers()
        try: self.wfile.write(body)
        except Exception: pass

    def _proxy(self, method, body=None):
        url = WEBPANEL + self.path
        hdrs = {k: v for k, v in self.headers.items()
                if k.lower() not in ("host", "content-length", "connection", "accept-encoding", "keep-alive")}
        hdrs["Accept-Encoding"] = "identity"
        req = urllib.request.Request(url, data=(body if method == "POST" else None), method=method, headers=hdrs)
        try:
            r = urllib.request.urlopen(req, timeout=40)
            data = r.read(); status = r.status; rh = r.headers
        except urllib.error.HTTPError as e:
            data = e.read(); status = e.code; rh = e.headers
        except Exception as e:
            return self._send("panel proxy error: %r" % (e,), code=502)
        self.send_response(status)
        self.send_header("Content-Type", rh.get("Content-Type", "application/octet-stream"))
        for c in (rh.get_all("Set-Cookie") or []):
            self.send_header("Set-Cookie", c)
        loc = rh.get("Location")
        if loc:
            self.send_header("Location", loc)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data))); self.end_headers()
        try: self.wfile.write(data)
        except Exception: pass

    def _proxy_ws(self):
        """Low-latency, full-duplex tunnel for the native UI's socket.io WebSocket.

        Uses select() so neither direction can starve the other, disables Nagle
        (TCP_NODELAY) so the latency-sensitive engine.io upgrade probe and the
        tiny ping/pong frames are flushed immediately, and turns on TCP
        keepalive. The previous version copied each direction on its own thread
        with Nagle left on; under an emulated backend that delayed the upgrade
        handshake enough that socket.io kept dropping and reconnecting (the
        "Disconnected — Reconnecting…" loop in the Web UI)."""
        try:
            up = socket.create_connection((WEBUI_HOST, WEBUI_PORT), timeout=10)
        except Exception as e:
            return self._send("ws upstream error: %r" % (e,), code=502)
        client = self.connection
        self.close_connection = True
        for s in (up, client):
            try:
                # Clear any inherited recv timeout (create_connection leaves a
                # 10s one) — select() gates readiness, so a blocking recv must
                # never time out and tear down an otherwise-healthy socket.
                s.settimeout(None)
                s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                s.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            except Exception:
                pass
        # Replay the client's upgrade request verbatim to the upstream.
        req = "%s %s %s\r\n" % (self.command, self.path, self.request_version)
        for k, v in self.headers.items():
            req += "%s: %s\r\n" % (k, v)
        req += "\r\n"
        try:
            up.sendall(req.encode("latin-1", "replace"))
        except Exception as e:
            up.close(); return self._send("ws send error: %r" % (e,), code=502)

        socks = [client, up]
        try:
            while True:
                # 90s idle ceiling: engine.io pings every 25s, so a fully silent
                # connection for 90s is dead — anything live keeps flowing.
                r, _, x = select.select(socks, [], socks, 90)
                if x or not r:
                    break
                stop = False
                for s in r:
                    try:
                        data = s.recv(65536)
                    except Exception:
                        stop = True; break
                    if not data:           # peer closed this direction → done
                        stop = True; break
                    dst = up if s is client else client
                    try:
                        dst.sendall(data)
                    except Exception:
                        stop = True; break
                if stop:
                    break
        finally:
            for s in (up, client):
                try: s.close()
                except Exception: pass

    def do_GET(self):
        try:
            health_path = self.path in ("/healthz", "/ready") or self.path.startswith(("/healthz?", "/ready?"))
            if not health_path and not self.authorized():
                return self.demand_auth()
            api_path = self.path.startswith(("/state", "/events", "/ready", "/healthz"))
            # Add-another-account mode (set by add_account.sh): re-serve the setup
            # wizard, retitled, even though the box is already onboarded/locked, so
            # a second account can be provisioned. Takes priority over the panel.
            if not api_path and add_account_active():
                if self.headers.get("Upgrade", "").lower() == "websocket":
                    return self._send_status(403)
                return self._send(setup_page(
                    title="Nighty <span>&middot; Add account</span>",
                    first_lead="Adding an <b>additional account</b> to Nighty. Your existing "
                               "license is reused, so this starts at the account step."))
            # Authorization gate: if the box is onboarded but the bot is not
            # actually linked (and not locked), refuse to serve the native panel
            # and force the authorize screen — Nighty would otherwise serve a
            # non-working panel.
            if not api_path and panel_blocked():
                if self.headers.get("Upgrade", "").lower() == "websocket":
                    return self._send_status(403)
                return self._send(authorize_page())
            # Early setup (license / sign-in / bot token): the bridge owns the
            # onboarding UI. Never proxy the native panel here, even if a stale
            # backend still has 8090 open — show our setup pages instead.
            if not api_path and not setup_locked() and not _onboarded():
                if self.headers.get("Upgrade", "").lower() == "websocket":
                    return self._send_status(403)
                return self._send(build_ui())
            # Native Web UI ("legacy") is the primary interface.
            if web_up() and not api_path:
                if self.headers.get("Upgrade", "").lower() == "websocket":
                    if not self.path.startswith(WS_ALLOWED_PREFIXES):
                        return self._send_status(403)
                    return self._proxy_ws()
                return self._proxy("GET")
            if self.path in ("/healthz", "/ready") or self.path.startswith(("/healthz?", "/ready?")):
                up = bool(web_up() and not panel_blocked())
                resp = {"status": "ok" if up else "starting", "ready": up}
                if self.request_is_local() or self.authorized():
                    resp["backend_running"] = up
                    resp["mode"] = "panel" if up else ("onboarding" if not setup_locked() and not _onboarded() else "loading")
                    resp["uptime_seconds"] = round(time.time() - START_TIME, 2)
                    resp["timestamp"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                return self._send(json.dumps(resp), "application/json", code=200 if up else 503)
            if self.path == "/" or self.path.startswith("/ui"):
                return self._send(build_ui())
            if self.path.startswith("/state"):
                try:
                    return self._send(json.dumps(public_state(get_state(), self.request_is_local())), "application/json")
                except Exception:
                    return self._send(json.dumps({"mode": "loading", "locked": setup_locked(),
                                                  "ready": bool(web_up())}), "application/json")
            if self.path.startswith("/events"):
                if not self.request_is_local():
                    return self._send_status(404, b"not found")
                q = urllib.parse.urlparse(self.path).query
                return self._send(urllib.request.urlopen(STUB + "/bridge/events?" + q, timeout=10).read(), "application/json")
            # Any other path while the panel is not up yet (e.g. an asset request)
            # — show the loading screen, never a bare 404/white page.
            return self._send(loading_page())
        except Exception:
            return self._send(loading_page())

    def do_POST(self):
        try:
            if not self.authorized():
                return self.demand_auth()
            try:
                ln = int(self.headers.get("Content-Length", 0) or 0)
            except (TypeError, ValueError):
                return self._send_status(400)
            if ln < 0 or ln > MAX_REQUEST_BYTES:
                return self._send_status(413)
            body = self.rfile.read(ln) if ln else b""
            # Onboarding / control endpoints (/rpc, /provision, /recheck_auth,
            # /check_*) drive the loopback-only stub control server and the on-disk
            # config. They are used solely by the bridge's own setup wizard and the
            # add-account flow. Once the box is locked into the working state the
            # LAN client talks to the native panel (reverse-proxied) and never calls
            # them, so we keep them closed then — the loopback stub is never
            # reachable from the LAN in normal operation. This changes nothing a
            # user sees: a locked box's legitimate traffic does not hit these paths.
            if self.path.startswith(("/recheck_auth", "/check_account", "/check_bot",
                                     "/provision", "/rpc")) \
                    and setup_locked() and not add_account_active():
                return self._send("not found", code=404)
            if self.path.startswith("/recheck_auth"):
                # Authorization backstop: the user confirms they authorized the
                # bot. Lock the setup as complete so the authorize screen never
                # reappears (only uninstall.sh's reset re-opens it), and restart
                # the backend so Nighty reconnects with the now-authorized bot.
                if _onboarded():
                    _set_lock()
                try:
                    os.system("pkill -f '[N]ighty_stub'")
                except Exception:
                    pass
                return self._send(json.dumps({"ok": True, "restarting": True}), "application/json")
            if self.path.startswith("/check_account"):
                # Validate a Discord account token against the API (resolves the
                # username used as the login key). Nothing is written yet.
                try: tok = json.loads(body or b"{}").get("token", "")
                except Exception: tok = ""
                return self._send(json.dumps(check_account_token(tok)), "application/json")
            if self.path.startswith("/check_bot"):
                # Validate the bot token + its intents, and return the OAuth
                # authorize URL for the final step. Nothing is handed to Nighty.
                try: tok = json.loads(body or b"{}").get("token", "")
                except Exception: tok = ""
                chk = check_bot_token(tok)
                if chk.get("valid") and chk.get("app_id"):
                    _, app = _discord_bot_get(tok, "/applications/@me")
                    chk["authorize_url"] = bot_authorize_url(
                        chk["app_id"], _app_integration_types(app if isinstance(app, dict) else {}))
                return self._send(json.dumps(chk), "application/json")
            if self.path.startswith("/provision"):
                # Single-shot onboarding: validate license + tokens, enforce the
                # OAuth gate, write auth.json + nighty.config directly, restart.
                try: p = json.loads(body or b"{}")
                except Exception: p = {}
                res = provision(p.get("license", ""), p.get("account_token", ""),
                                p.get("bot_token", ""))
                # NB: add-account mode is left ON here on purpose. provision only
                # writes config + restarts and returns at once; the bot link is not
                # verified yet. get_state clears add-account (and the verify marker)
                # only once the bot is confirmed linked, so a reload during the
                # verify/authorize window still serves the wizard, not the panel.
                return self._send(json.dumps(res), "application/json")
            if self.path.startswith("/rpc"):
                req = urllib.request.Request(STUB + "/api/call", data=body,
                                             headers={"Content-Type": "application/json"}, method="POST")
                try:
                    with urllib.request.urlopen(req, timeout=60) as r:
                        return self._send(r.read(), "application/json")
                except urllib.error.HTTPError as e:
                    # The stub returns a JSON body with the *real* error and
                    # traceback on 4xx/5xx — log it (so it lands in the bridge
                    # log) and forward it verbatim instead of masking it behind
                    # a generic "HTTPError 500".
                    err_body = e.read()
                    try:
                        import sys
                        sys.stderr.write("[rpc-error] %s\n" % err_body.decode("utf-8", "replace"))
                        sys.stderr.flush()
                    except Exception:
                        pass
                    return self._send(err_body, "application/json", e.code)
            if web_up() and not panel_blocked():
                return self._proxy("POST", body)
            return self._send("not found", code=404)
        except Exception as e:
            return self._send(json.dumps({"ok": False, "error": repr(e)}), "application/json", 500)


if __name__ == "__main__":
    print("bridge on %s:%d  ->  Web UI %s  /  stub %s" % (HOST, PORT, WEBPANEL, STUB), flush=True)
    ThreadingHTTPServer((HOST, PORT), H).serve_forever()
