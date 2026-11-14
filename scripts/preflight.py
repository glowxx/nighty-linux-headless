#!/usr/bin/env python3
"""Small, side-effect-free startup/install checks and diagnostics generator for Nighty headless."""

from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import datetime
import glob
import os
from pathlib import Path
import platform
import re
import shutil
import socket
import ssl
import struct
import subprocess
import sys
from typing import Dict, List, Optional, Tuple
import urllib.error
import urllib.request


PE_MACHINES = {
    0x014C: "i386 (32-bit)",
    0x8664: "x86-64 (64-bit)",
    0xAA64: "ARM64",
}

WINE_NATIVE_LIBS = (
    ("X11", "libx11-6", True),
    ("Xext", "libxext6", True),
    ("Xrender", "libxrender1", True),
    ("Xfixes", "libxfixes3", True),
    ("Xrandr", "libxrandr2", True),
    ("Xcomposite", "libxcomposite1", True),
    ("Xi", "libxi6", True),
    ("Xcursor", "libxcursor1", True),
    ("Xinerama", "libxinerama1", True),
    ("EGL", "libegl1", True),
    ("cups", "libcups2", True),
    ("xkbregistry", "libxkbregistry0", False),
)

TOKEN_RE_RAW = re.compile(r"\b([A-Za-z0-9_-]{24,32}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{20,45})\b")
MFA_RE_RAW = re.compile(r"\b(mfa\.[A-Za-z0-9_-]{40,})\b")
KEY_VALUE_TOKEN = re.compile(r"""(["']?(?:token|license|password|secret|auth)["']?\s*[:=]\s*["']?)([^"'\r\n\s]{6,})(["']?)""", re.IGNORECASE)


def redact_secrets(text: str) -> str:
    """Mask tokens, license keys, and secrets from diagnostic outputs."""
    if not text:
        return ""
    text = TOKEN_RE_RAW.sub(lambda m: m.group(1)[:6] + "...REDACTED..." + m.group(1)[-4:], text)
    text = MFA_RE_RAW.sub(lambda m: m.group(1)[:8] + "...REDACTED..." + m.group(1)[-4:], text)
    text = KEY_VALUE_TOKEN.sub(r"\1***REDACTED***\3", text)
    return text


class PreflightError(RuntimeError):
    pass


KNOWN_FAILURES = (
    (
        "bundled-extension-dll",
        re.compile(r"DLL load failed while importing (\w+)"),
        "A compiled extension bundled inside Nighty.exe could not load one of its own dependency DLLs "
        "(Windows reports this as 'Module not found' for the dependency, not for the module named). "
        "This happens inside the frozen binary and the Wine prefix, so re-running the installer or the "
        "repack will not change it. A 64-bit-only Wine prefix with no WoW64 support is a common trigger. "
        "See issue #13.",
    ),
    (
        "pillow-imaging",
        re.compile(r"cannot import name '_imaging' from 'PIL'"),
        "Pillow's compiled extension failed to load for the same reason as the DLL failure above: its "
        "dependency chain is unresolvable in this Wine prefix. Not a wrapper misconfiguration. See issue #13.",
    ),
    (
        "wine-module-import",
        re.compile(r"err:module:(?:import_dll|loader_init|LdrInitializeThunk)"),
        "Wine could not resolve a DLL import. Run 'bash scripts/install.sh' to install the native "
        "Wine/X11 runtime libraries, and check that WINEPREFIX points at a prefix built by this installer.",
    ),
    (
        "bad-exe-format",
        re.compile(r"Bad EXE format"),
        "The supplied Nighty.exe is not an x86-64 PE32+ binary. On ARM/Box64 a 32-bit or damaged "
        "executable cannot run; supply a 64-bit Nighty.exe and re-run 'bash scripts/install.sh'.",
    ),
    (
        "bad-marshal-data",
        re.compile(r"bad marshal data"),
        "The repack ran under the wrong Python. The marshal format is version-specific; "
        "re-run install.sh to detect the executable's Python version and fetch a matching interpreter.",
    ),
    (
        "missing-license",
        re.compile(r"KeyError\(?'motd'\)?"),
        "Nighty has no valid license, so on_ready aborts before registering its command tree. The bot "
        "connects but no commands work. Complete the Activate step with your Nighty license key.",
    ),
)


def scan_known_failures(text: str) -> List[Dict[str, str]]:
    if not text:
        return []
    found = []
    for name, pattern, advice in KNOWN_FAILURES:
        match = pattern.search(text)
        if match:
            found.append({"id": name, "match": match.group(0), "advice": advice})
    return found


def triage_log(path: Optional[Path] = None, tail_bytes: int = 256 * 1024) -> int:
    if path is None:
        diag_env = os.environ.get("NIGHTY_DIAG_DIR")
        base = Path(diag_env) if diag_env else Path(__file__).resolve().parents[1] / "diagnostics"
        path = base / "backend.log"
    if not path.is_file():
        print(f"[triage] no log to inspect at {path}")
        return 0
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - tail_bytes))
            text = fh.read().decode("utf-8", errors="replace")
    except OSError as exc:
        print(f"[triage] could not read {path}: {exc}", file=sys.stderr)
        return 1
    findings = scan_known_failures(text)
    if not findings:
        print(f"[triage] no known failure signature in the last {tail_bytes // 1024} KiB of {path.name}")
        return 0
    print(f"[triage] {len(findings)} known failure signature(s) in {path.name}:")
    for item in findings:
        print()
        print(f"  [{item['id']}] matched: {redact_secrets(item['match'])}")
        print(f"  {item['advice']}")
    print()
    return 0


def pe_machine(path: Path) -> int:
    try:
        with path.open("rb") as fh:
            header = fh.read(64)
            if len(header) < 64 or header[:2] != b"MZ":
                raise PreflightError("not a Windows PE executable (missing MZ header)")
            pe_offset = struct.unpack_from("<I", header, 0x3C)[0]
            if pe_offset < 64 or pe_offset > path.stat().st_size - 6:
                raise PreflightError("invalid PE header offset")
            fh.seek(pe_offset)
            pe = fh.read(6)
    except OSError as exc:
        raise PreflightError(f"cannot read file: {exc}") from exc
    if len(pe) != 6 or pe[:4] != b"PE\0\0":
        raise PreflightError("invalid PE signature")
    return struct.unpack_from("<H", pe, 4)[0]


def check_pe(path: Path, require_x64: bool) -> int:
    if not path.is_file():
        print(f"[preflight] ERROR: executable not found: {path}", file=sys.stderr)
        return 2
    try:
        machine = pe_machine(path)
    except PreflightError as exc:
        print(f"[preflight] ERROR: {path}: {exc}", file=sys.stderr)
        return 2
    label = PE_MACHINES.get(machine, f"unknown machine 0x{machine:04x}")
    if require_x64 and machine != 0x8664:
        print(
            f"[preflight] ERROR: {path.name} is {label}; ARM/Box64 requires an x86-64 PE32+ build.",
            file=sys.stderr,
        )
        return 3
    if machine not in (0x014C, 0x8664):
        print(f"[preflight] ERROR: unsupported executable architecture: {label}", file=sys.stderr)
        return 3
    print(f"[preflight] {path.name}: {label}")
    return 0


def missing_native_libs() -> List[Tuple[str, str, bool]]:
    missing: List[Tuple[str, str, bool]] = []
    for library, package, required in WINE_NATIVE_LIBS:
        candidate = ctypes.util.find_library(library)
        if not candidate:
            missing.append((library, package, required))
            continue
        try:
            ctypes.CDLL(candidate)
        except OSError:
            missing.append((library, package, required))
    return missing


def check_libs(quiet: bool) -> int:
    missing = missing_native_libs()
    if not missing:
        if not quiet:
            print("[preflight] native Wine/X11 libraries: OK")
        return 0
    required_missing = [item for item in missing if item[2]]
    optional_missing = [item for item in missing if not item[2]]
    if required_missing and not quiet:
        print("[preflight] missing native libraries required by Wine/Box64:", file=sys.stderr)
        for library, package, _required in required_missing:
            print(f"  {library} (Debian package: {package})", file=sys.stderr)
    if optional_missing and not quiet:
        print("[preflight] optional native libraries not present (startup may still work):", file=sys.stderr)
        for library, package, _required in optional_missing:
            print(f"  {library} (Debian package: {package})", file=sys.stderr)
    return 4 if required_missing else 0


def resolve_command(value: str) -> Optional[Path]:
    if os.sep in value or (os.altsep and os.altsep in value):
        return Path(value).expanduser()
    resolved = shutil.which(value)
    return Path(resolved) if resolved else None


def check_wine(value: str) -> int:
    path = resolve_command(value)
    if path is None or not path.is_file():
        print(f"[preflight] ERROR: WINE_BIN does not resolve to a file: {value}", file=sys.stderr)
        return 5
    if not os.access(path, os.X_OK):
        print(f"[preflight] ERROR: WINE_BIN is not executable: {path}", file=sys.stderr)
        return 5
    try:
        with path.open("rb") as fh:
            magic = fh.read(2)
    except OSError as exc:
        print(f"[preflight] ERROR: cannot read WINE_BIN {path}: {exc}", file=sys.stderr)
        return 5
    kind = "launcher script" if magic == b"#!" else "executable"
    print(f"[preflight] WINE_BIN: {path} ({kind})")
    return 0


def run_network_checks(quiet: bool = False) -> Tuple[int, List[Tuple[str, str, bool]]]:
    """Run comprehensive network diagnostics (DNS, TLS, HTTP/Cloudflare, TCP ports)."""
    results: List[Tuple[str, str, bool]] = []
    has_error = False

    # 1. DNS Resolution checks
    domains = [
        "discord.com",
        "gateway.discord.gg",
        "cdn.discordapp.com",
        "lrclib.net",
        "api.spotify.com",
    ]
    if not quiet:
        print("[diag] Checking DNS resolution...")
    for domain in domains:
        try:
            ip = socket.gethostbyname(domain)
            results.append(("DNS " + domain, f"Resolved to {ip}", True))
        except socket.gaierror as e:
            results.append(("DNS " + domain, f"FAILED ({e})", False))
            has_error = True

    # 2. HTTPS / TLS Handshake to Discord API
    if not quiet:
        print("[diag] Checking HTTPS/TLS connectivity to Discord API...")
    url = "https://discord.com/api/v10/gateway"
    req = urllib.request.Request(url, headers={"User-Agent": "NightyHeadlessDiag/1.0"})
    try:
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:
            results.append(("TLS discord.com", f"HTTP {resp.status} OK", True))
    except urllib.error.HTTPError as e:
        if e.code == 403:
            results.append(("TLS discord.com", "HTTP 403 Forbidden (Possible Cloudflare / Anti-Bot block)", False))
            has_error = True
        else:
            results.append(("TLS discord.com", f"HTTP {e.code} ({e.reason})", True))
    except (urllib.error.URLError, ssl.SSLError, OSError) as e:
        results.append(("TLS discord.com", f"FAILED ({e})", False))
        has_error = True

    # 3. Port binding checks (8088 bridge, 8090 webui, 8765 stub)
    ports = [
        (8088, "Bridge LAN"),
        (8090, "Web UI Native"),
        (8765, "Stub CTL"),
    ]
    if not quiet:
        print("[diag] Checking local port status...")
    for port, label in ports:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            is_open = s.connect_ex(("127.0.0.1", port)) == 0
            status_str = "BOUND (Listening)" if is_open else "CLOSED (Available)"
            results.append((f"Port :{port} ({label})", status_str, True))

    # Print summary
    if not quiet:
        print("\n=== NETWORK DIAGNOSTICS SUMMARY ===")
        for test_name, detail, ok in results:
            status = "OK" if ok else "FAIL"
            print(f"  [{status}] {test_name:<30}: {detail}")
        print("===================================\n")

    return (1 if has_error else 0), results


def check_network(quiet: bool = False) -> int:
    code, _ = run_network_checks(quiet)
    return code


def get_mem_info() -> Dict[str, str]:
    mem = {}
    if os.path.exists("/proc/meminfo"):
        try:
            with open("/proc/meminfo", "r", encoding="utf-8") as f:
                for line in f:
                    parts = line.split(":")
                    if len(parts) == 2:
                        k = parts[0].strip()
                        v = parts[1].strip()
                        if k in ("MemTotal", "MemAvailable", "MemFree", "SwapTotal", "SwapFree"):
                            mem[k] = v
        except Exception:
            pass
    return mem


def get_disk_info(paths: List[Path]) -> Dict[str, str]:
    info = {}
    for p in paths:
        try:
            if p.exists():
                usage = shutil.disk_usage(p)
                total_gb = usage.total / (1024**3)
                free_gb = usage.free / (1024**3)
                used_gb = usage.used / (1024**3)
                pct = (usage.used / usage.total) * 100 if usage.total else 0
                info[str(p)] = f"{used_gb:.1f}GB / {total_gb:.1f}GB used ({pct:.1f}%), {free_gb:.1f}GB free"
        except Exception as e:
            info[str(p)] = f"error: {e}"
    return info


def scan_recent_errors(diag_dir: Path) -> List[str]:
    """Scan latest log files in diag_dir and extract critical signatures/tracebacks."""
    findings = []
    error_patterns = [
        re.compile(r"Traceback \(most recent call last\):", re.IGNORECASE),
        re.compile(r"^[A-Za-z_][\w.]*(?:Error|Exception|Warning|Panic)\b.*", re.IGNORECASE),
        re.compile(r"UnicodeDecodeError.*", re.IGNORECASE),
        re.compile(r"UnicodeEncodeError.*", re.IGNORECASE),
        re.compile(r"panic:.*", re.IGNORECASE),
        re.compile(r"fatal error:.*", re.IGNORECASE),
        re.compile(r"\[signal SIGSEGV.*", re.IGNORECASE),
        re.compile(r"Windows fatal exception.*", re.IGNORECASE),
        re.compile(r"HTTP 403.*", re.IGNORECASE),
        re.compile(r"HTTP 500.*", re.IGNORECASE),
        re.compile(r"10003.*NotFound", re.IGNORECASE),
        re.compile(r"10062.*Unknown interaction", re.IGNORECASE),
        re.compile(r"backend panel timed out after", re.IGNORECASE),
    ]

    log_files = ["backend.log", "nighty.log", "bridge.log", "guard.log", "xvfb.log"]
    for name in log_files:
        path = diag_dir / name
        if not path.is_file():
            continue
        try:
            with path.open("rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - 256 * 1024))
                tail = f.read().decode("utf-8", errors="replace").splitlines()
            
            for line in tail[-100:]:
                for pat in error_patterns:
                    if pat.search(line):
                        findings.append(f"[{name}] {line.strip()}")
                        break
        except Exception:
            pass

    # Deduplicate preserving order
    seen = set()
    deduped = []
    for item in findings:
        clean = redact_secrets(item)
        if clean not in seen:
            seen.add(clean)
            deduped.append(clean)
    return deduped[-20:]


def generate_report(diag_dir: Optional[Path] = None, outfile: Optional[Path] = None, quiet: bool = False) -> int:
    """Generate a complete, self-contained, sanitized system and diagnostics report."""
    if diag_dir is None:
        diag_env = os.environ.get("NIGHTY_DIAG_DIR")
        if diag_env:
            diag_dir = Path(diag_env)
        else:
            diag_dir = Path(__file__).resolve().parents[1] / "diagnostics"
    
    diag_dir.mkdir(parents=True, exist_ok=True)
    out_path = outfile or (diag_dir / "system_info.txt")

    lines: List[str] = []
    now_utc = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines.append("=" * 80)
    lines.append("NIGHTY LINUX HEADLESS - SYSTEM & DIAGNOSTICS REPORT")
    lines.append(f"Generated: {now_utc}")
    lines.append("=" * 80)
    lines.append("")

    # 1. Host & Environment
    lines.append("── [1] System & Host Environment ──")
    lines.append(f"Platform:     {platform.platform()}")
    lines.append(f"Architecture: {platform.machine()} (Python: {platform.python_version()})")
    lines.append(f"CPU Cores:    {os.cpu_count() or 'Unknown'}")
    in_docker = os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv")
    lines.append(f"Environment:  {'Docker Container' if in_docker else 'Bare Metal Host'}")
    if hasattr(os, "getuid"):
        lines.append(f"Process UID:  {os.getuid()} / GID: {os.getgid()}")
    lines.append(f"Diag Dir:     {diag_dir}")
    lines.append("")

    # 2. Resources (Memory & Disk)
    lines.append("── [2] Memory & Storage ──")
    mem = get_mem_info()
    if mem:
        lines.append(f"RAM Total:    {mem.get('MemTotal', 'N/A')}")
        lines.append(f"RAM Avail:    {mem.get('MemAvailable', mem.get('MemFree', 'N/A'))}")
        lines.append(f"Swap Total:   {mem.get('SwapTotal', 'N/A')}")
        lines.append(f"Swap Free:    {mem.get('SwapFree', 'N/A')}")
    else:
        lines.append("RAM info:     N/A (non-Linux procfs)")

    check_paths = [Path("/"), diag_dir]
    data_dir = os.environ.get("NIGHTY_HOME") or os.environ.get("DATA_DIR")
    if data_dir:
        check_paths.append(Path(data_dir))
    disk = get_disk_info(check_paths)
    for p_str, d_info in disk.items():
        lines.append(f"Disk [{p_str}]: {d_info}")
    lines.append("")

    # 3. Emulation & Dependencies
    lines.append("── [3] Runtime & Emulation Stack ──")
    wine_bin = os.environ.get("WINE_BIN", "wine64")
    resolved_wine = resolve_command(wine_bin)
    lines.append(f"WINE_BIN:     {wine_bin} (resolved: {resolved_wine or 'NOT FOUND'})")
    
    box64_profile = os.environ.get("NIGHTY_BOX64_PROFILE", "balanced")
    lines.append(f"Box64 Profile:{box64_profile}")
    
    missing_libs = missing_native_libs()
    if not missing_libs:
        lines.append("Native X11:   All required runtime libraries present (OK)")
    else:
        req = [lib for lib, _, req in missing_libs if req]
        opt = [lib for lib, _, req in missing_libs if not req]
        if req:
            lines.append(f"Native X11:   MISSING REQUIRED: {', '.join(req)}")
        if opt:
            lines.append(f"Native X11:   Missing optional: {', '.join(opt)}")

    ca_bundle = os.environ.get("SSL_CERT_FILE", "/etc/ssl/certs/ca-certificates.crt")
    lines.append(f"SSL CA Certs: {ca_bundle} ({'Present' if os.path.isfile(ca_bundle) else 'Missing'})")
    lines.append("")

    # 4. Network Status
    lines.append("── [4] Network Diagnostics ──")
    _, net_results = run_network_checks(quiet=True)
    for test_name, detail, ok in net_results:
        status_lbl = "OK" if ok else "FAIL"
        lines.append(f"[{status_lbl:4s}] {test_name:<28}: {detail}")
    lines.append("")

    # 5. Log Files in Diagnostics Directory
    lines.append("── [5] Files in Diagnostics Folder ──")
    try:
        found_files = sorted(diag_dir.glob("*"))
        if found_files:
            for item in found_files:
                if item.is_file():
                    sz = item.stat().st_size
                    sz_str = f"{sz / 1024:.1f} KB" if sz < 1024 * 1024 else f"{sz / (1024 * 1024):.2f} MB"
                    mtime = datetime.datetime.fromtimestamp(item.stat().st_mtime, tz=datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                    lines.append(f"  • {item.name:<25} {sz_str:>10}  (modified {mtime})")
        else:
            lines.append("  (diagnostics folder is currently empty)")
    except Exception as e:
        lines.append(f"  Error reading diag dir: {e}")
    lines.append("")

    # 6. Recent Error / Crash Signatures
    lines.append("── [6] Recent Errors / Warnings in Logs ──")
    errs = scan_recent_errors(diag_dir)
    if errs:
        for err in errs:
            lines.append(f"  ! {err}")
    else:
        lines.append("  (No critical errors or tracebacks detected in recent log tails)")
    lines.append("")
    lines.append("=" * 80)
    lines.append("END OF REPORT - This file is redacted and safe to attach. The raw logs beside it are NOT: check them for tokens first.")
    lines.append("=" * 80)

    report_content = "\n".join(lines) + "\n"
    try:
        out_path.write_text(report_content, encoding="utf-8", errors="replace")
        if not quiet:
            print(f"[diag] Diagnostics summary saved to: {out_path}")
            print(report_content)
        return 0
    except OSError as exc:
        print(f"[diag] ERROR: Cannot write report to {out_path}: {exc}", file=sys.stderr)
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    pe = sub.add_parser("pe", help="validate a Windows PE executable")
    pe.add_argument("path", type=Path)
    pe.add_argument("--require-x64", action="store_true")
    libs = sub.add_parser("libs", help="check native Wine/X11 runtime libraries")
    libs.add_argument("--quiet", action="store_true")
    wine = sub.add_parser("wine", help="validate WINE_BIN")
    wine.add_argument("path")
    diag = sub.add_parser("diag", help="run network and environment diagnostics")
    diag.add_argument("--quiet", action="store_true")
    report = sub.add_parser("report", help="generate full system_info.txt diagnostics report")
    report.add_argument("--diag-dir", type=Path, default=None, help="path to diagnostics directory")
    report.add_argument("--outfile", type=Path, default=None, help="path to output report file")
    report.add_argument("--quiet", action="store_true", help="do not print to stdout")
    triage = sub.add_parser("triage", help="explain known backend failure signatures")
    triage.add_argument("--log", type=Path, default=None, help="path to the log to inspect")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "pe":
        return check_pe(args.path, args.require_x64)
    if args.command == "libs":
        return check_libs(args.quiet)
    if args.command == "wine":
        return check_wine(args.path)
    if args.command == "diag":
        return check_network(args.quiet)
    if args.command == "report":
        return generate_report(args.diag_dir, args.outfile, args.quiet)
    if args.command == "triage":
        return triage_log(args.log)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
