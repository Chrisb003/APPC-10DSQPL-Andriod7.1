#!/usr/bin/env python3
"""
APPC-10DSQ tablet dashboard (PORTING_NOTES 4y, 5b).

Manages a few tablets over network adb: online state, storage, screen, foreground app, screenshot,
install / uninstall / open apps, start-up app (+ pinning), the power schedule (weekly times, one-off
dates, days off), screen on/off / restart, the tablet's "Allow new computers to ask" switch and hiding
its Developer options.

Each tablet must have "Allow remote management" switched on (Settings > Developer options > Network
debugging & remote management; off by default). While it is off the dashboard only shows whether the
tablet is online and refuses everything else.

Shutting tablets down is deliberately not offered (they cannot switch themselves back on): no shut-down
button, no new "shut down" schedule times (ones set on the tablet itself are kept as they are).

Needs only Python 3.7+ and adb (Mac: Android platform-tools; Windows: platform-tools on the PATH or
unzipped next to this script; Raspberry Pi / Linux: `sudo apt install adb`). The page asks for a user name and password (chosen on
the first start). Address, port and HTTPS are set on the page's Settings (stored in config.json in the
data folder); by default it only listens on this computer, port 8080, plain HTTP. With HTTPS on, the
dashboard makes its own certificate (data folder/tls/, used by nothing else) or uses the one you name.

    python3 appc_dashboard.py [--data FOLDER] [--host 0.0.0.0] [--port 8443] [--https] [--adb /path/to/adb]

The first time the dashboard's computer connects, each tablet asks "Allow debugging?" on its screen
(tick "Always allow"); it needs "Allow new computers to ask" on (the default) for that.

Android 7.1's adbd serves one network computer at a time, so the dashboard disconnects from a tablet
after --idle seconds without work (default 30), leaving it free for other computers in between.

The tablets are only contacted while the page is open (and its browser tab visible): left running in
the background with no page open, the dashboard does not talk to them at all.

Tablet side: Panel settings' CommandReceiver (am broadcast ... com.appc10dsq.panel.GET / SET).
"""
import argparse
import base64
import concurrent.futures
import errno
import hashlib
import hmac
import json
import os
import posixpath
import re
import secrets
import shlex
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, unquote, parse_qs, quote

HERE = os.path.dirname(os.path.abspath(__file__))


def find_adb():
    """adb on the PATH, else Google's platform-tools unzipped next to this script (handy on Windows),
    else the Android SDK's usual places; "adb" if none is found (commands then report it missing)."""
    exe = "adb.exe" if os.name == "nt" else "adb"
    found = shutil.which("adb")
    if found:
        return found
    for p in (os.path.join(HERE, "platform-tools", exe),
              os.path.expanduser("~/Library/Android/sdk/platform-tools/adb"),
              os.path.join(os.environ.get("LOCALAPPDATA", ""), "Android", "Sdk", "platform-tools", "adb.exe"),
              os.path.expanduser("~/Android/Sdk/platform-tools/adb")):
        if os.path.isfile(p):
            return p
    return "adb"
PANEL = "com.appc10dsq.panel/.CommandReceiver"

args = None
devices = []            # [{"id", "name", "address"}]
status = {}             # id -> last status dict
locks = {}              # id -> threading.Lock (one adb conversation per tablet at a time)
last_used = {}          # address -> time of last adb use (for idle disconnects)
state_lock = threading.Lock()


# ---------------------------------------------------------------- adb helpers

def adb(*a, timeout=30, binary=False, inp=None):
    """Run adb; returns (exit code, output)."""
    try:
        p = subprocess.run([args.adb] + list(a), input=inp, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           timeout=timeout)
        out = p.stdout if binary else p.stdout.decode("utf-8", "replace")
        return p.returncode, out
    except subprocess.TimeoutExpired:
        return 124, b"" if binary else "timed out"
    except FileNotFoundError:
        return 127, b"" if binary else "adb not found (--adb)"


# ------------------------------------------------------------ progress of uploads / installs, for the page
# The page sends an X-Task id with the request and asks GET /api/progress/<id> while it runs.
progress = {}           # task id -> {"phase": text, "pct": 0-100 or None (no percentage), "time"}


def set_progress(task, phase, pct=None):
    if not task:
        return
    now = time.time()
    progress[task] = {"phase": phase, "pct": pct, "time": now}
    for k in [k for k, v in list(progress.items()) if now - v["time"] > 900]:
        progress.pop(k, None)


def push(a, src, dst, task=None, phase="Copying to the tablet", timeout=3600):
    """adb push, reporting how much has arrived on the tablet (its size there, checked every second)."""
    total = os.path.getsize(src)
    if not task or total < (1 << 20):
        set_progress(task, phase, None)
        return adb("-s", a, "push", src, dst, timeout=timeout)
    set_progress(task, phase, 0.0)
    try:
        p = subprocess.Popen([args.adb, "-s", a, "push", src, dst], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        return 127, "adb not found (--adb)"
    end = time.time() + timeout
    while True:
        try:
            p.wait(timeout=1.0)
            break
        except subprocess.TimeoutExpired:
            if time.time() > end:
                p.kill()
                p.wait()
                return 124, "timed out"
        rc, out = adb("-s", a, "shell", "stat -c %%s %s 2>/dev/null" % shlex.quote(dst), timeout=10)
        try:
            set_progress(task, phase, min(99.0, 100.0 * int(out.strip().splitlines()[-1]) / total))
        except (ValueError, IndexError):
            pass
    out = p.stdout.read().decode("utf-8", "replace")
    if p.returncode == 0:
        set_progress(task, phase, 100.0)
    return p.returncode, out


def is_network(address):
    return ":" in address


def adb_state(address):
    _, out = adb("devices", timeout=10)
    for line in out.splitlines()[1:]:
        p = line.split()
        if len(p) >= 2 and p[0] == address:
            return p[1]
    return None


def port_open(address, t=2.0):
    host, port = address.rsplit(":", 1)
    try:
        with socket.create_connection((host, int(port)), timeout=t):
            return True
    except OSError:
        return False


def connect(address):
    """Make sure adb can talk to the tablet. Returns device / unauthorized / unreachable / busy / missing."""
    if not is_network(address):
        return adb_state(address) or "missing"
    st = adb_state(address)
    if st == "device":
        return st
    if st is not None:
        adb("disconnect", address, timeout=10)
    if not port_open(address):
        return "unreachable"
    adb("connect", address, timeout=15)
    for _ in range(10):
        st = adb_state(address)
        if st in ("device", "unauthorized"):
            break
        time.sleep(0.5)
    if st == "offline" or st is None:
        # Android 7.1's adbd answers one network computer at a time
        adb("disconnect", address, timeout=10)
        return "busy"
    return st


def shell(address, cmd, timeout=30):
    last_used[address] = time.time()
    return adb("-s", address, "shell", cmd, timeout=timeout)


def reaper():
    """Disconnect from tablets that have not been used for --idle seconds."""
    while True:
        time.sleep(5)
        if args.idle <= 0:
            continue
        for d in list(devices):
            a = d["address"]
            if is_network(a) and a in last_used and time.time() - last_used[a] > args.idle:
                lk = locks.setdefault(d["id"], threading.Lock())
                if lk.acquire(blocking=False):
                    try:
                        if a in last_used and time.time() - last_used[a] > args.idle:
                            adb("disconnect", a, timeout=10)
                            last_used.pop(a, None)
                    finally:
                        lk.release()


# ---------------------------------------------------------------- tablet commands

STATUS_SCRIPT = r"""
echo @build; getprop ro.build.display.id
echo @serial; getprop ro.serialno
echo @uptime; cat /proc/uptime
echo @df; df /data; df /storage/external_sd 2>/dev/null
echo @mem; grep -E '^(MemTotal|MemFree|Cached):' /proc/meminfo
echo @load; cat /proc/loadavg
echo @wake; dumpsys power | grep -m1 'mWakefulness='
echo @top; dumpsys activity activities | grep -m1 -E 'mFocusedActivity|mResumedActivity'
echo @time; date +%s; date +%Z; getprop persist.sys.timezone
echo @ip; ip -4 -o addr show 2>/dev/null
echo @end
"""


def parse_status(out):
    sec, cur = {}, None
    for line in out.splitlines():
        line = line.rstrip("\r")
        if line.startswith("@"):
            cur = line[1:]
            sec[cur] = []
        elif cur:
            sec[cur].append(line)
    s = {}
    g = lambda k: sec.get(k, [])
    s["build"] = (g("build") or [""])[0]
    s["serial"] = (g("serial") or [""])[0]
    try:
        s["uptime"] = int(float(g("uptime")[0].split()[0]))
    except Exception:
        pass
    disks = []
    for l in g("df"):
        p = l.split()
        if len(p) >= 6 and p[1].isdigit():
            disks.append({"mount": p[5], "total_kb": int(p[1]), "used_kb": int(p[2]), "free_kb": int(p[3])})
    s["disks"] = disks
    mem = {}
    for l in g("mem"):
        m = re.match(r"(\w+):\s+(\d+)", l)
        if m:
            mem[m.group(1)] = int(m.group(2))
    if mem:
        s["mem_total_kb"] = mem.get("MemTotal", 0)
        s["mem_free_kb"] = mem.get("MemFree", 0) + mem.get("Cached", 0)
    try:
        s["load"] = float(g("load")[0].split()[0])
    except Exception:
        pass
    w = " ".join(g("wake"))
    m = re.search(r"mWakefulness=(\w+)", w)
    s["screen"] = {"Awake": "on", "Asleep": "off", "Dozing": "off", "Dreaming": "screensaver"}.get(m.group(1), m.group(1)) if m else ""
    m = re.search(r"u0 ([\w.]+)/([\w.$]+)", " ".join(g("top")))
    s["foreground"] = m.group(1) if m else ""
    t = g("time")
    try:
        s["clock"] = int(t[0])
        s["clock_offset"] = int(t[0]) - int(time.time())
        s["timezone"] = t[2] if len(t) > 2 and t[2] else (t[1] if len(t) > 1 else "")
    except Exception:
        pass
    ips = []
    for l in g("ip"):
        m = re.search(r"^\d+:\s+(\S+)\s+inet\s+([\d.]+)", l)
        if m and m.group(1) != "lo":
            ips.append(m.group(1) + " " + m.group(2))
    s["ips"] = ips
    return s


def panel(address, payload=None, firmware=False):
    """Panel settings state (GET, optionally with the firmware packages on the SD card / USB stick) or
    change (SET with payload dict); returns the state dict."""
    if payload is None:
        cmd = "am broadcast -n %s -a com.appc10dsq.panel.GET%s" % (PANEL, " --ez firmware true" if firmware else "")
    else:
        b64 = base64.b64encode(json.dumps(payload).encode()).decode()
        cmd = "am broadcast -n %s -a com.appc10dsq.panel.SET --es b64 %s" % (PANEL, b64)
    rc, out = shell(address, cmd, timeout=30)
    m = re.search(r'Broadcast completed: result=(-?\d+), data="(.*)"\s*$', out, re.S)
    if m and m.group(1) == "2":   # remote management off on the tablet
        if payload is not None:
            raise RuntimeError(REMOTE_OFF)
        return json.loads(m.group(2))
    if not m:
        if "result=0" in out and "data" not in out:
            raise RuntimeError("Panel settings on this tablet has no dashboard support (needs version 1.1)")
        raise RuntimeError("no answer from Panel settings: " + out.strip()[-300:])
    data = json.loads(m.group(2))
    if m.group(1) != "1":
        raise RuntimeError(data.get("error", "Panel settings refused the command"))
    return data


REMOTE_OFF = ("Remote management is off on this tablet - switch it on there: Settings > Developer options > "
              "Network debugging & remote management > Allow remote management")


def require_remote(address):
    """The tablet's "Allow remote management" switch also covers what the dashboard does through adb alone."""
    if not panel(address).get("remote_management"):
        raise RuntimeError(REMOTE_OFF)


# ---------------------------------------------------------------- tablet storage

ROOTS = {"internal": "/sdcard", "sd": "/mnt/external_sd"}


def tablet_path(root, rel):
    """Absolute path on the tablet inside internal storage or the SD card (no way out with ..)."""
    if root not in ROOTS:
        raise ValueError("unknown storage")
    rel = posixpath.normpath("/" + (rel or "")).lstrip("/")
    if rel in ("", "."):
        return ROOTS[root]
    if rel.startswith("..") or "\n" in rel:
        raise ValueError("bad path")
    return ROOTS[root] + "/" + rel


def list_files(address, root, rel):
    d = tablet_path(root, rel)
    cmd = ("cd %s 2>/dev/null || { echo @missing; exit 0; }; df -k . | tail -1; "
           "for f in * .*; do [ \"$f\" = . ] || [ \"$f\" = .. ] || [ ! -e \"$f\" ] || stat -c '%%F|%%s|%%Y|%%n' \"./$f\"; done"
           % shlex.quote(d))
    rc, out = shell(address, cmd, timeout=60)
    if "@missing" in out:
        raise RuntimeError("not there (no SD card?)" if root == "sd" and not rel else "folder not found")
    lines = out.splitlines()
    space = {}
    entries = []
    for l in lines:
        l = l.rstrip("\r")
        q = l.split("|", 3)
        if len(q) == 4 and q[3].startswith("./"):
            entries.append({"name": q[3][2:], "dir": q[0] == "directory", "size": int(q[1] or 0), "mtime": int(q[2] or 0)})
        else:
            p = l.split()
            if len(p) >= 4 and p[1].isdigit() and not space:
                space = {"total_kb": int(p[1]), "free_kb": int(p[3])}
    entries.sort(key=lambda e: (not e["dir"], e["name"].lower()))
    return {"root": root, "path": posixpath.normpath("/" + (rel or "")).lstrip("/"), "entries": entries, **space}


# Google Play Protect scans every adb install and, for apps it does not know, waits for "Install
# anyway" on the tablet's screen (nobody there, screen often off). Installs from the dashboard are
# deliberate, so "Verify apps over USB" (verifier_verify_adb_installs) is switched off for the install
# and put back afterwards.
NO_VERIFY = ('v=$(settings get global verifier_verify_adb_installs); settings put global verifier_verify_adb_installs 0; '
             '%s; [ "$v" = null ] && settings delete global verifier_verify_adb_installs >/dev/null '
             '|| settings put global verifier_verify_adb_installs "$v"')


def install_apk(address, tablet_file):
    rc, out = shell(address, NO_VERIFY % ("pm install -r %s" % shlex.quote(tablet_file)), timeout=600)
    return {"ok": "Success" in out, "output": out.strip()[-500:]}


def media_scan(address, path):
    """Let Gallery / Music see added or removed files."""
    shell(address, "am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE -d %s >/dev/null"
          % shlex.quote("file://" + path), timeout=20)


KNOWN_APPS = {"com.android.launcher3": "Home screen", "com.appc10dsq.panel": "Panel settings",
              "com.android.systemui": "System UI", "com.android.packageinstaller": "Package installer"}


def app_name(package, apps):
    """App name for a package, from the tablet's launcher list ([label, component] pairs)."""
    for label, comp in apps:
        if comp.split("/")[0] == package:
            return label
    return KNOWN_APPS.get(package, package)


def refresh(dev):
    """Poll one tablet (called by the poller and on demand)."""
    lk = locks.setdefault(dev["id"], threading.Lock())
    if not lk.acquire(timeout=2):   # busy (firmware update, long copy): keep what we know
        return status.get(dev["id"], {})
    try:
        a = dev["address"]
        st = connect(a)
        s = {"state": st, "checked": int(time.time())}
        prev = status.get(dev["id"], {})
        if st == "device":
            last_used[a] = time.time()
            rc, out = shell(a, STATUS_SCRIPT, timeout=40)
            if "@end" in out:
                s.update(parse_status(out))
                s["last_seen"] = s["checked"]
                try:
                    p = panel(a)
                    s["foreground_name"] = app_name(s.get("foreground", ""), p.get("apps") or [])
                    s["panel"] = {k: p.get(k) for k in ("remote_management", "panel_version", "next", "adb_allow_new",
                                                         "adb_allow_new_usb", "adb_notify", "startup", "pinned", "side_leds",
                                                         "volume", "volume_max", "brightness", "developer_options_shown",
                                                         "quiet", "pin_lock", "pin_restart", "timezone", "auto_time", "now")}
                    if not p.get("remote_management"):   # only online/offline then
                        s = {k: s[k] for k in ("state", "checked", "last_seen", "panel", "serial", "build") if k in s}
                except Exception as e:
                    s["panel_error"] = str(e)
            else:
                s["state"] = "error"
                s["error"] = out.strip()[-300:]
        if "last_seen" not in s and prev.get("last_seen"):
            s["last_seen"] = prev["last_seen"]
        status[dev["id"]] = s
        return s
    finally:
        lk.release()


# The tablets are only contacted while someone has the page open: the page asks for the tablet list
# every 15 s while its tab is visible; without that for VIEW_TIMEOUT seconds the poller does nothing
# (and the idle reaper then drops the adb connections).
VIEW_TIMEOUT = 45
last_view = 0.0
poll_now = threading.Event()


def page_seen():
    global last_view
    idle = time.time() - last_view > VIEW_TIMEOUT
    last_view = time.time()
    if idle:   # page just opened (again): fresh status straight away
        poll_now.set()


def poller():
    while True:
        poll_now.wait(max(10, args.interval))
        poll_now.clear()
        if time.time() - last_view > VIEW_TIMEOUT:
            continue
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
            list(ex.map(refresh, list(devices)))


def with_device(dev, fn, remote=True):
    """Run fn(address) with the tablet connected (and remote management on); raises otherwise."""
    lk = locks.setdefault(dev["id"], threading.Lock())
    if not lk.acquire(timeout=30):
        raise RuntimeError("the tablet is busy (firmware update or another command) - try again in a moment")
    try:
        st = connect(dev["address"])
        if st != "device":
            raise RuntimeError({
                "unauthorized": "waiting for \"Allow debugging?\" to be accepted on the tablet",
                "unreachable": "tablet not reachable on the network",
                "busy": "another computer is connected to this tablet over adb (Android 7.1 allows one at a time)",
                "missing": "tablet not connected",
            }.get(st, st))
        last_used[dev["address"]] = time.time()
        if remote:
            require_remote(dev["address"])
        return fn(dev["address"])
    finally:
        lk.release()


# ---------------------------------------------------------------- storage

def dev_file():
    return os.path.join(args.data, "devices.json")


def apk_dir():
    d = os.path.join(args.data, "apks")
    os.makedirs(d, exist_ok=True)
    return d


def load_devices():
    global devices
    try:
        with open(dev_file()) as f:
            devices = json.load(f).get("devices", [])
    except FileNotFoundError:
        devices = []


def save_devices():
    os.makedirs(args.data, exist_ok=True)
    tmp = dev_file() + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"devices": devices}, f, indent=2)
    os.replace(tmp, dev_file())


def find(dev_id):
    for d in devices:
        if d["id"] == dev_id:
            return d
    raise KeyError("no such tablet")


def norm_address(a):
    a = a.strip()
    if re.fullmatch(r"[\w.-]+", a) and "." in a:   # IP / host name without a port
        a += ":5555"
    return a


# ------------------------------------------------------------ USB set-up (PORTING_NOTES 5m)
def adb_pubkey():
    """This computer's adb public key line (the key adb uses for every tablet), from adbkey.pub."""
    home = os.environ.get("ANDROID_USER_HOME") or os.path.join(os.environ.get("ANDROID_SDK_HOME") or os.path.expanduser("~"), ".android")
    pub = os.path.join(home, "adbkey.pub")
    if not os.path.exists(pub) and os.path.exists(os.path.join(home, "adbkey")):
        rc, out = adb("pubkey", os.path.join(home, "adbkey"), timeout=10)
        if rc == 0 and out.startswith("QAAAA"):
            return out.strip()
    with open(pub) as f:
        line = f.read().strip()
    if not line.startswith("QAAAA"):
        raise RuntimeError("unexpected adb key file " + pub)
    return line


def usb_tablets():
    """Tablets on this computer's USB ports (adb devices -l), with what the set-up would change."""
    _, out = adb("devices", "-l", timeout=10)
    found = []
    for line in out.splitlines()[1:]:
        f = line.split()
        if len(f) < 2 or is_network(f[0]):
            continue
        t = {"serial": f[0], "state": f[1], "model": next((x[6:] for x in f if x.startswith("model:")), "")}
        if f[1] == "device":
            _, info = adb("-s", f[0], "shell", "getprop ro.product.model; getprop ro.build.display.id; settings get global appc_remote_mgmt; "
                          "getprop persist.adb.tcp.port; ip -4 addr", timeout=20)
            rows = info.splitlines()
            if len(rows) >= 4:
                t["model"], t["build"] = rows[0].strip(), rows[1].strip()
                t["remote_management"] = rows[2].strip() == "1"
                t["network_adb"] = rows[3].strip() not in ("", "0")
                t["ip"] = {m.group(2): m.group(1) for m in re.finditer(r"inet (\d+\.\d+\.\d+\.\d+)/\d+.*?\s(\w+)\s*$", info, re.M)
                           if not m.group(1).startswith("127.")}
            t["appc"] = t["model"] in ("APPC-10DSQ", "APPC_10DSQ")
        known = [d for d in devices if d["address"] == f[0] or status.get(d["id"], {}).get("serial") == f[0]
                 or any(d["address"] == ip + ":5555" for ip in t.get("ip", {}).values())]
        if known:
            t["in_list"] = {"id": known[0]["id"], "name": known[0]["name"], "address": known[0]["address"]}
        found.append(t)
    return found


def usb_setup(serial):
    """Over the USB cable: remote management on, this computer's adb key stored on the tablet (so it connects over the
    network without "Allow debugging?"), network debugging on, then the tablet goes into the list by its network
    address and is connected over the network. Returns the steps done."""
    steps = []
    if adb_state(serial) != "device":
        raise RuntimeError("the tablet is not ready on USB - unlock it and allow this computer (tick \"Always allow\")")
    _, model = adb("-s", serial, "shell", "getprop ro.product.model", timeout=15)
    if model.strip() != "APPC-10DSQ":
        raise RuntimeError("not an APPC-10DSQ (model %s)" % model.strip())
    adb("-s", serial, "shell", "settings put global appc_remote_mgmt 1", timeout=15)
    _, v = adb("-s", serial, "shell", "settings get global appc_remote_mgmt", timeout=15)
    if v.strip() != "1":
        raise RuntimeError("could not switch on remote management")
    steps.append("Remote management switched on")
    key = adb_pubkey()
    p = panel(serial, {"adb_key_add": key, "network_adb": True})
    if "network_adb" not in p:
        raise RuntimeError("this tablet's Panel settings is too old for the USB set-up (needs firmware N1.7)")
    steps.append("This computer allowed on the tablet for good (%s)" % (key.split(" ", 1)[1] if " " in key else "adb key"))
    steps.append("Network debugging switched on (port 5555)")
    time.sleep(2.5)   # adbd restarts about a second after the answer
    for _ in range(40):
        if adb_state(serial) == "device":
            break
        time.sleep(0.5)
    ips = p.get("ip") or {}
    ip = ips.get("eth0") or ips.get("wlan0") or next(iter(ips.values()), None)
    if not ip:
        steps.append("No network connection on the tablet yet - connect Ethernet or Wi-Fi, then run the set-up again")
        return {"steps": steps, "device": None}
    addr = ip + ":5555"
    with state_lock:
        dev = next((d for d in devices if d["address"] in (serial, addr) or status.get(d["id"], {}).get("serial") == serial), None)
        if dev:
            if dev["address"] != addr:
                steps.append("%s: address %s -> %s" % (dev["name"], dev["address"], addr))
                dev["address"] = addr
        else:
            dev = {"id": "t%d" % int(time.time() * 1000), "name": "Tablet " + serial[-4:], "address": addr}
            devices.append(dev)
            steps.append("Added to the list as \"%s\" (%s)" % (dev["name"], addr))
        save_devices()
    st = connect(addr)
    steps.append("Network connection: " + {"device": "connected", "unauthorized": "refused (the key was not accepted)",
                                           "unreachable": "not reachable from this computer"}.get(st, st))
    threading.Thread(target=refresh, args=(dev,), daemon=True).start()
    return {"steps": steps, "device": dev, "connected": st == "device"}


def safe_apk_name(n):
    n = os.path.basename(n or "")
    n = re.sub(r"[^A-Za-z0-9._ +-]", "_", n)
    if not n.lower().endswith(".apk"):
        raise ValueError("only .apk files")
    return n


# ---------------------------------------------------------------- HTTP

# ---------------------------------------------------------------- settings (config.json)

VERSION = "0.4"
DEFAULTS = {"host": "127.0.0.1", "port": 8080, "https": False, "cert": "", "key": "", "names": [], "users": {}}
config = dict(DEFAULTS)
restart = threading.Event()     # listener settings changed: reopen the port
quitting = threading.Event()


def cfg_file():
    return os.path.join(args.data, "config.json")


def load_config():
    global config
    config = dict(DEFAULTS)
    try:
        with open(cfg_file()) as f:
            config.update(json.load(f))
    except FileNotFoundError:
        pass


def save_config():
    os.makedirs(args.data, exist_ok=True)
    tmp = cfg_file() + ".tmp"
    with open(tmp, "w") as f:
        json.dump(config, f, indent=2)
    try:
        os.chmod(tmp, 0o600)     # holds the password hashes
    except OSError:
        pass
    os.replace(tmp, cfg_file())


def listen():
    """(host, port, https) in use: the command line wins over config.json for this run."""
    return (args.host or config["host"], args.port or int(config["port"]),
            config["https"] if args.https is None else args.https)


# ------------------------------------------------------------ port in use: move to the next free one
# The same behaviour as the ArtNet Lighting Controller / Sound Control Deck apps (PortProbe in their
# TLSCertificate.swift, TriggerAPIServer.swift): before listening, check the port is free; if another program
# has it (or this account may not use it), run on the next free port above it, say so, and try the preferred
# port again at the next start (the setting itself is not changed).
active_port = None      # the port actually listened on
port_notice = None      # why it differs from the preferred one (None when it does not)


def _can_bind(family, addr, port):
    try:
        s = socket.socket(family, socket.SOCK_STREAM)
    except OSError:
        return True             # no IPv6 on this computer: nothing can be listening there
    try:
        if os.name != "nt":     # on Windows SO_REUSEADDR would let us take a port that is in use
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)   # ignores our own closed connections only
        if family == socket.AF_INET6:
            s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        s.bind((addr, port))
        return True
    except OSError as e:
        return e.errno not in (errno.EADDRINUSE, errno.EACCES, getattr(errno, "WSAEADDRINUSE", -1), getattr(errno, "WSAEACCES", -1))
    finally:
        s.close()


def port_free(host, port):
    """No other program listening on the port (IPv4 or IPv6, any address), and our own address can take it."""
    ok = _can_bind(socket.AF_INET, "0.0.0.0", port) and _can_bind(socket.AF_INET6, "::", port)
    return ok and (host in ("", "0.0.0.0") or _can_bind(socket.AF_INET, host, port))


def first_free(host, start, tries=50):
    """The preferred port if free, otherwise the next free one above it."""
    for p in range(start, min(start + tries, 65536)):
        if port_free(host, p):
            return p
    return None


def clear_port_notice():
    global port_notice
    port_notice = None


def running():
    """(host, port, https) actually in use: listen() with the port the listener ended up on."""
    host, port, https = listen()
    return host, active_port or port, https


def local_addresses():
    ips = {"127.0.0.1"}
    try:
        for i in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(i[4][0])
    except OSError:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))     # no packet is sent; just picks the outgoing address
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    return sorted(ips)


# ---------------------------------------------------------------- HTTPS: the dashboard's own certificate

def tls_paths():
    d = os.path.join(args.data, "tls")
    return os.path.join(d, "appc-dashboard.crt"), os.path.join(d, "appc-dashboard.key"), os.path.join(d, "appc-dashboard.json")


def cert_names(extra=()):
    host = socket.gethostname()
    names = ["localhost", host] + ([host + ".local"] if "." not in host else [])
    ips = local_addresses()
    for x in list(config.get("names") or []) + list(extra):
        x = str(x).strip().strip("[]")
        if not x:
            continue
        try:
            import ipaddress
            ipaddress.ip_address(x)
            ips.append(x)
        except ValueError:
            if re.fullmatch(r"[A-Za-z0-9.-]{1,253}", x):
                names.append(x)
    return names, ips


def ensure_cert(force=False, extra=()):
    """The dashboard's own self-signed certificate (data folder/tls/): made on first use, remade when it
    has run out or on request. Used only by this app."""
    crt, key, meta = tls_paths()
    info = None
    try:
        with open(meta) as f:
            info = json.load(f)
    except (OSError, ValueError):
        pass
    if not force and info and os.path.exists(crt) and os.path.exists(key) \
            and info.get("not_after", "") > time.strftime("%Y-%m-%d"):
        return info
    import appc_cert
    names, ips = cert_names(extra)
    c, k, info = appc_cert.make(names, ips, days=800, host_label=socket.gethostname())
    os.makedirs(os.path.dirname(crt), exist_ok=True)
    fd = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(k)
    with open(crt, "w") as f:
        f.write(c)
    with open(meta, "w") as f:
        json.dump(info, f, indent=2)
    return info


def cert_info():
    if config.get("cert") and config.get("key"):
        info = {"custom": True, "file": config["cert"]}
        try:
            d = ssl._ssl._test_decode_cert(config["cert"])
            info["subject"] = ", ".join("=".join(x) for r in d.get("subject", ()) for x in r)
            info["not_after"] = d.get("notAfter", "")
            info["names"] = [v for _, v in d.get("subjectAltName", ())]
        except Exception as e:
            info["problem"] = str(e)
        return info
    crt, key, meta = tls_paths()
    try:
        with open(meta) as f:
            return dict(json.load(f), custom=False, file=crt)
    except (OSError, ValueError):
        return {"custom": False, "missing": True}


def tls_context():
    if config.get("cert") and config.get("key"):
        crt, key = config["cert"], config["key"]
    else:
        ensure_cert()
        crt, key, _ = tls_paths()
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(crt, key)
    return ctx


# ---------------------------------------------------------------- login

PBKDF_ITER = 200000
SESSION_SECONDS = 12 * 3600
COOKIE = "appc_session"
sessions = {}       # token -> {"user", "expires"}
login_fails = {}    # client address -> [count, locked until]
setup_code = None   # first-time setup from another computer needs this (printed at start)


def hash_password(pw, salt=None, it=PBKDF_ITER):
    salt = salt or secrets.token_bytes(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, it)
    return {"salt": base64.b64encode(salt).decode(), "hash": base64.b64encode(h).decode(), "iter": it}


def check_password(user, pw):
    u = (config.get("users") or {}).get(user)
    if not u:
        hash_password(pw or "", b"0" * 16)    # same work for unknown names
        return False
    h = hash_password(pw or "", base64.b64decode(u["salt"]), int(u.get("iter", PBKDF_ITER)))
    return hmac.compare_digest(h["hash"], u["hash"])


def valid_user(user, pw):
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,32}", user or ""):
        raise ValueError("user name: 1 to 32 letters, digits, dot, dash or underscore")
    if len(pw or "") < 8:
        raise ValueError("the password needs at least 8 characters")


def new_session(user):
    for t in [t for t, v in sessions.items() if v["expires"] < time.time()]:
        sessions.pop(t, None)
    tok = secrets.token_urlsafe(32)
    sessions[tok] = {"user": user, "expires": time.time() + SESSION_SECONDS}
    return tok


AUTH_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Tablet Dashboard</title>
<link rel="icon" href="/logo.svg" type="image/svg+xml">
<style>:root{--bg:#f4f5f7;--card:#fff;--text:#1d2330;--muted:#677085;--line:#dfe3ea;--accent:#2563eb;--bad:#b91c1c}
@media (prefers-color-scheme: dark){:root{--bg:#11151c;--card:#1a202a;--text:#e6e9ef;--muted:#98a2b3;--line:#2b3340;--accent:#4f8cff;--bad:#f87171}}
body{margin:0;font:15px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;background:var(--bg);color:var(--text);
display:flex;min-height:100vh;align-items:center;justify-content:center}
form{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:24px;width:min(360px,calc(100vw - 32px));box-sizing:border-box}
h1{font-size:18px;margin:0 0 4px}p{color:var(--muted);margin:0 0 14px;font-size:13px}
label{display:block;font-size:13px;color:var(--muted);margin-top:10px}
input{width:100%;box-sizing:border-box;font:inherit;color:inherit;background:var(--card);border:1px solid var(--line);border-radius:8px;padding:8px 10px;margin-top:3px}
button{margin-top:16px;width:100%;font:inherit;border:0;border-radius:8px;padding:9px;background:var(--accent);color:#fff;cursor:pointer}
#err{color:var(--bad);font-size:13px;min-height:18px;margin-top:10px}</style></head><body>
<form id="f"><img src="/logo.svg" alt="" width="56" height="56" style="display:block;margin:-6px 0 6px -6px"><h1>Tablet Dashboard</h1><p>__INTRO__</p>
<label>User name<input id="u" autocomplete="username" autofocus required></label>
<label>Password<input id="p" type="password" autocomplete="__AUTOCOMPLETE__" required></label>
__EXTRA__<button>__BUTTON__</button><div id="err"></div></form>
<script>
const mode = "__MODE__";
document.getElementById("f").addEventListener("submit", async e => {
  e.preventDefault();
  const v = id => (document.getElementById(id) || {}).value || "";
  if (mode === "setup" && v("p") !== v("p2")) { document.getElementById("err").textContent = "The two passwords are different"; return; }
  const r = await fetch("/auth/" + mode, {method: "POST", headers: {"Content-Type": "application/json", "X-APPC": "1"},
    body: JSON.stringify({user: v("u"), password: v("p"), code: v("c")})});
  if (r.ok) location.reload(); else document.getElementById("err").textContent = (await r.json()).error || "Failed";
});
</script></body></html>"""


def auth_page(local):
    if config.get("users"):
        rep = {"__MODE__": "login", "__INTRO__": "Log in to manage the tablets.", "__BUTTON__": "Log in",
               "__AUTOCOMPLETE__": "current-password", "__EXTRA__": ""}
    else:
        extra = '<label>Password again<input id="p2" type="password" autocomplete="new-password" required></label>'
        if not local:
            extra += '<label>Setup code (shown where the dashboard was started)<input id="c" inputmode="numeric" required></label>'
        rep = {"__MODE__": "setup", "__INTRO__": "First start: choose the user name and password for this dashboard.",
               "__BUTTON__": "Create login", "__AUTOCOMPLETE__": "new-password", "__EXTRA__": extra}
    page = AUTH_PAGE
    for k, v in rep.items():
        page = page.replace(k, v)
    return page


# ---------------------------------------------------------------- firmware library, update of several tablets

def fw_dir():
    d = os.path.join(args.data, "firmware")
    os.makedirs(d, exist_ok=True)
    return d


def safe_fw_name(n):
    n = os.path.basename(n or "")
    if not re.fullmatch(r"[A-Za-z0-9._+-]{1,120}", n) or not n.lower().endswith(".img"):
        raise ValueError("firmware file name: letters, digits, . _ + - only, ending in .img")
    return n


def fw_info(path):
    """What Panel settings' Firmware update reads from a Rockchip package: RKFW / RKAF headers and the
    package-file notes (# build, # hardware V..., # keeps data, # stock restore)."""
    st = os.stat(path)
    info = {"file": os.path.basename(path), "size": st.st_size, "time": int(st.st_mtime), "build": "",
            "hardware": "", "keeps_data": False, "stock_restore": False}
    try:
        with open(path, "rb") as f:
            h = f.read(0x66)
            if h[:4] != b"RKFW":
                info["problem"] = "not a Rockchip firmware package"
                return info
            if struct.unpack_from("<I", h, 21)[0] != 0x70:
                info["problem"] = "made for a different chip (not RK3188)"
                return info
            img, ln = struct.unpack_from("<II", h, 33)
            if img + ln + 32 != st.st_size:
                info["problem"] = "the file is incomplete (copy it again)"
                return info
            f.seek(img)
            a = f.read(0x800)
            if a[:4] != b"RKAF":
                info["problem"] = "damaged package header"
                return info
            for i in range(struct.unpack_from("<I", a, 136)[0]):
                o = 140 + 112 * i
                if o + 112 > len(a):
                    break
                if a[o:o + 32].split(b"\0")[0] != b"package-file":
                    continue
                pos, = struct.unpack_from("<I", a, o + 96)
                size, = struct.unpack_from("<I", a, o + 108)
                f.seek(img + pos)
                for line in f.read(min(size, 65536)).decode("latin1").splitlines():
                    if line.startswith("# build "):
                        info["build"] = line[8:].strip()
                    elif line.startswith("# hardware V"):
                        info["hardware"] = line[12:].strip().split(" ")[0]
                    elif line.startswith("# stock restore"):
                        info["stock_restore"] = True
                    elif line.startswith("# keeps data"):
                        info["keeps_data"] = True
            if not info["hardware"]:
                info["problem"] = "not our firmware: the package does not say which hardware revision it is for"
    except (OSError, struct.error) as e:
        info["problem"] = "cannot read it (%s)" % e
    return info


_md5_cache = {}


def file_md5(path):
    st = os.stat(path)
    key = (path, st.st_size, st.st_mtime)
    if key not in _md5_cache:
        h = hashlib.md5()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        _md5_cache[key] = h.hexdigest()
    return _md5_cache[key]


jobs = []                           # newest first
job_slots = threading.Semaphore(3)  # tablets copied to at the same time
SD = "/mnt/external_sd"


def start_fw_job(fname, ids):
    path = os.path.join(fw_dir(), safe_fw_name(fname))
    if not os.path.isfile(path):
        raise KeyError("firmware file not in the library")
    info = fw_info(path)
    if info.get("problem"):
        raise ValueError("%s: %s" % (fname, info["problem"]))
    devs = [find(i) for i in ids]
    if not devs:
        raise ValueError("no tablets chosen")
    busy = {i for j in jobs for i, d in j["devices"].items() if not d["done"]}
    if busy & {d["id"] for d in devs}:
        raise ValueError("one of these tablets is still being updated")
    job = {"id": "j%d" % int(time.time() * 1000), "file": info["file"], "build": info["build"],
           "keeps_data": info["keeps_data"], "stock_restore": info["stock_restore"], "started": int(time.time()),
           "devices": {d["id"]: {"name": d["name"], "state": "Waiting", "detail": "", "percent": None,
                                 "done": False, "ok": False} for d in devs}}
    jobs.insert(0, job)
    del jobs[10:]
    for d in devs:
        threading.Thread(target=fw_worker, args=(job, d, path, info), daemon=True).start()
    return job


def fw_worker(job, dev, path, info):
    st = job["devices"][dev["id"]]
    a = dev["address"]
    name = info["file"]
    remote = SD + "/" + name

    def step(state, detail="", percent=None):
        st.update(state=state, detail=detail, percent=percent)

    def finish(ok, state, detail=""):
        st.update(state=state, detail=detail, percent=None, done=True, ok=ok)

    try:
        with job_slots:
            step("Preparing", "checking the firmware file")
            md5 = file_md5(path)
            lk = locks.setdefault(dev["id"], threading.Lock())
            with lk:
                c = connect(a)
                if c != "device":
                    raise RuntimeError({"unauthorized": "this computer is not allowed on the tablet yet",
                                        "unreachable": "tablet not reachable", "missing": "tablet not connected",
                                        "busy": "another computer is connected to the tablet"}.get(c, c))
                last_used[a] = time.time()
                require_remote(a)
                rc, out = shell(a, "df -k %s/ 2>/dev/null | tail -1; echo @; stat -c %%s %s 2>/dev/null" % (SD, shlex.quote(remote)))
                dfout, _, have = out.partition("@")
                f = next((l.split() for l in dfout.splitlines()
                          if "external_sd" in l and len(l.split()) >= 6 and l.split()[3].isdigit()), None)
                if not f:
                    raise RuntimeError("no SD card in the tablet (the firmware is installed from the card)")
                same = False
                if have.strip() == str(info["size"]):
                    step("Checking", "the file is already on the card - comparing")
                    rc, out = shell(a, "md5sum %s" % shlex.quote(remote), timeout=600)
                    same = out.split()[:1] == [md5]
                if not same:
                    if int(f[3]) * 1024 < info["size"] + (16 << 20):
                        raise RuntimeError("not enough room on the tablet's SD card")
                    part = remote + ".part"
                    stop = threading.Event()

                    def progress():
                        while not stop.wait(4):
                            rc2, o2 = adb("-s", a, "shell", "stat -c %%s %s 2>/dev/null" % shlex.quote(part), timeout=15)
                            if o2.strip().isdigit():
                                step("Copying", "to the tablet's SD card", int(100 * int(o2.strip()) / info["size"]))
                            last_used[a] = time.time()
                    step("Copying", "to the tablet's SD card", 0)
                    threading.Thread(target=progress, daemon=True).start()
                    try:
                        rc, out = adb("-s", a, "push", path, part, timeout=3600)
                    finally:
                        stop.set()
                    if rc != 0:
                        raise RuntimeError("copy failed: " + out.strip()[-200:])
                    step("Checking", "comparing the copy with the original", 100)
                    rc, out = shell(a, "md5sum %s" % shlex.quote(part), timeout=600)
                    if out.split()[:1] != [md5]:
                        shell(a, "rm -f %s" % shlex.quote(part))
                        raise RuntimeError("the copy on the card is damaged - try again")
                    shell(a, "mv %s %s" % (shlex.quote(part), shlex.quote(remote)))
                step("Checking", "the tablet is checking the firmware")
                fw = [x for x in panel(a, firmware=True).get("firmware", []) if x.get("file") == name]
                if not fw:
                    raise RuntimeError("the tablet does not see the firmware file on its card")
                if fw[0].get("problem"):
                    raise RuntimeError(fw[0]["problem"])
                step("Installing", "the tablet is restarting into the installer (about 5 minutes)")
                panel(a, {"do": "firmware_install", "file": name})
            # the tablet restarts, installs and comes back
            time.sleep(75)
            end = time.time() + 20 * 60
            wipes = not info["keeps_data"]
            while time.time() < end:
                seen = None
                if lk.acquire(timeout=20):
                    try:
                        seen = connect(a)
                        if seen == "device":
                            last_used[a] = time.time()
                            rc, out = shell(a, "getprop sys.boot_completed; getprop ro.build.display.id")
                            l = out.split()
                            if l[:1] == ["1"]:
                                build = l[1] if len(l) > 1 else ""
                                if info["build"] and build == info["build"]:
                                    return finish(True, "Done", "now on " + build + (
                                        " - the tablet was reset: switch remote management on again there" if wipes else ""))
                                if info["stock_restore"]:
                                    return finish(True, "Done", "stock firmware restored (" + build + ")")
                                st.update(detail="started, reports " + build)
                    finally:
                        lk.release()
                if seen == "unauthorized" and wipes:
                    return finish(True, "Done", "installed; the tablet was reset - allow this computer on it and "
                                  "switch remote management on again")
                time.sleep(10)
            finish(False, "Not confirmed", "the tablet did not report the new firmware within 20 minutes")
    except Exception as e:
        finish(False, "Failed", str(e))


class Handler(BaseHTTPRequestHandler):
    server_version = "APPCDashboard/" + VERSION
    timeout = 300            # per socket operation (slow clients do not hold a thread for ever)
    extra_headers = ()

    def is_local(self):
        """From this computer itself (loopback, or its own address)."""
        ip = self.client_address[0]
        return ip in ("127.0.0.1", "::1", "::ffff:127.0.0.1") or ip == self.connection.getsockname()[0]

    def session_user(self):
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE and v in sessions:
                se = sessions[v]
                if se["expires"] < time.time():
                    sessions.pop(v, None)
                    return None
                se["expires"] = time.time() + SESSION_SECONDS
                self.token = v
                return se["user"]
        return None

    def cookie(self, token, age=SESSION_SECONDS):
        self.extra_headers = (("Set-Cookie", "%s=%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Strict%s" % (
            COOKIE, token, age, "; Secure" if listen()[2] else "")),)

    def body_to_file(self, path):
        n = int(self.headers.get("Content-Length") or 0)
        with open(path, "wb") as f:
            while n > 0:
                chunk = self.rfile.read(min(n, 1 << 20))
                if not chunk:
                    raise ValueError("upload was cut short")
                f.write(chunk)
                n -= len(chunk)

    def log_message(self, fmt, *a):
        if args.verbose:
            sys.stderr.write("%s %s\n" % (self.address_string(), fmt % a))

    def send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in self.extra_headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def body(self, raw=False):
        n = int(self.headers.get("Content-Length") or 0)
        data = self.rfile.read(n) if n else b""
        return data if raw else (json.loads(data or b"{}"))

    def do_GET(self):
        self.route("GET")

    def do_POST(self):
        self.route("POST")

    def do_DELETE(self):
        self.route("DELETE")

    def route(self, method):
        u = urlparse(self.path)
        self.query = {k: v[0] for k, v in parse_qs(u.query).items()}
        path = [unquote(p) for p in u.path.strip("/").split("/") if p]
        try:
            if method == "GET" and path == ["logo.svg"]:    # the logo (also on the login page and as the favicon)
                with open(os.path.join(HERE, "logo.svg"), "rb") as f:
                    return self.send(200, f.read(), "image/svg+xml")
            if path[:1] == ["auth"]:
                return self.auth(method, path[1:])
            self.user = "local" if args.no_login else self.session_user()
            if not self.user:
                if path[:1] == ["api"]:
                    return self.send(401, {"error": "login"})
                return self.send(200, auth_page(self.is_local()), "text/html; charset=utf-8")
            if method != "GET" and self.headers.get("X-APPC") != "1":
                return self.send(403, {"error": "request not from the dashboard page"})
            r = self.dispatch(method, path)
            if r is not None:
                self.send(200, r)
        except KeyError as e:
            self.send(404, {"error": str(e).strip("'\"")})
        except (ValueError, RuntimeError) as e:
            self.send(400, {"error": str(e)})
        except Exception as e:
            self.send(500, {"error": repr(e)})

    def auth(self, m, p):
        """Login, first-time setup, logout, and the Mac app's own window (local token)."""
        ip = self.client_address[0]
        if p == ["local"] and m == "GET":
            if args.local_token and self.is_local() and hmac.compare_digest(self.query.get("token", ""), args.local_token):
                self.cookie(new_session("this computer"))
            self.send_response(302)
            self.send_header("Location", "/")
            for k, v in self.extra_headers:
                self.send_header(k, v)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if m != "POST" or self.headers.get("X-APPC") != "1":
            raise KeyError("not found")
        if p == ["logout"]:
            self.session_user()
            sessions.pop(getattr(self, "token", ""), None)
            self.cookie("", 0)
            return self.send(200, {"ok": True})
        b = self.body()
        user, pw = str(b.get("user", "")).strip(), str(b.get("password", ""))
        if p == ["setup"]:
            if config.get("users"):
                raise ValueError("the login already exists")
            if not self.is_local() and not hmac.compare_digest(str(b.get("code", "")).strip(), setup_code or "-"):
                raise ValueError("wrong setup code (it is shown where the dashboard was started)")
            valid_user(user, pw)
            config["users"] = {user: hash_password(pw)}
            save_config()
            self.cookie(new_session(user))
            return self.send(200, {"ok": True})
        if p == ["login"]:
            f = login_fails.get(ip, [0, 0])
            if f[1] > time.time():
                raise ValueError("too many wrong tries - wait %d seconds" % (f[1] - time.time() + 1))
            if not check_password(user, pw):
                f[0] += 1
                if f[0] >= 5:
                    f = [0, time.time() + 30]
                login_fails[ip] = f
                time.sleep(0.5)
                raise ValueError("wrong user name or password")
            login_fails.pop(ip, None)
            self.cookie(new_session(user))
            return self.send(200, {"ok": True})
        raise KeyError("not found")

    def dispatch(self, m, p):
        if m == "GET" and (not p or p == ["index.html"]):
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                return self.send(200, f.read(), "text/html; charset=utf-8")
        if not p or p[0] != "api":
            raise KeyError("not found")
        p = p[1:]

        # this dashboard: who is logged in, listener settings, certificate, users
        if p == ["session"] and m == "GET":
            return {"user": self.user, "login": not args.no_login, "app": self.user == "this computer", "version": VERSION}
        if p == ["settings"] and m == "GET":
            host, port, https = running()
            return {"host": config["host"], "port": int(config["port"]), "https": bool(config["https"]),
                    "port_notice": port_notice,
                    "cert": config.get("cert", ""), "key": config.get("key", ""), "names": config.get("names") or [],
                    "running": {"host": host, "port": port, "https": https},
                    "fixed": {"host": args.host is not None, "port": args.port is not None, "https": args.https is not None},
                    "certificate": cert_info() if (config["https"] or https) else None,
                    "users": sorted(config.get("users") or {}), "addresses": local_addresses(),
                    "hostname": socket.gethostname(), "data": os.path.abspath(args.data), "adb": args.adb, "version": VERSION}
        if p == ["settings"] and m == "POST":
            b = self.body()
            new = dict(config)
            if "host" in b:
                new["host"] = str(b["host"]).strip() or "127.0.0.1"
            if "port" in b:
                new["port"] = int(b["port"])
                if not 1 <= new["port"] <= 65535:
                    raise ValueError("port must be 1 to 65535")
            if "https" in b:
                new["https"] = bool(b["https"])
            for k in ("cert", "key"):
                if k in b:
                    new[k] = str(b[k]).strip()
            if "names" in b:
                new["names"] = [str(x).strip() for x in b["names"] if str(x).strip()]
            if bool(new["cert"]) != bool(new["key"]):
                raise ValueError("give both the certificate file and its key file, or neither")
            for k in ("cert", "key"):
                if new[k] and not os.path.isfile(new[k]):
                    raise ValueError("file not found: " + new[k])
            if new["cert"]:
                try:
                    ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(new["cert"], new["key"])
                except Exception as e:
                    raise ValueError("that certificate and key cannot be used: %s" % e)
            ours = new["port"] == active_port and new["host"] == config["host"]   # the port we already run on
            if (new["host"], new["port"]) != (config["host"], int(config["port"])) and args.port is None and not ours:
                if not port_free(args.host or new["host"], new["port"]):   # the same check as at start-up
                    nxt = first_free(args.host or new["host"], new["port"] + 1)
                    raise ValueError("port %d is in use by another program (or not allowed for this account)%s"
                                     % (new["port"], " - %d is free" % nxt if nxt else ""))
                try:    # and the address itself can be used on this computer
                    t = socket.socket()
                    t.bind((args.host or new["host"], 0))
                    t.close()
                except OSError as e:
                    raise ValueError("cannot listen on %s: %s" % (new["host"], e.strerror or e))
            changed = any(new[k] != config[k] for k in ("host", "port", "https", "cert", "key"))
            config_before = dict(config)
            config.update(new)
            save_config()
            if new["https"]:
                ensure_cert()
            if changed and not (ours and (new["host"], new["https"], new["cert"], new["key"]) ==
                                (config_before["host"], config_before["https"], config_before["cert"], config_before["key"])):
                threading.Timer(0.7, restart.set).start()    # after this answer has gone out
            elif changed:
                changed = False             # only made the port in use the preferred one: nothing to reopen
                clear_port_notice()
            host, port, https = running()
            return {"restart": changed, "port": port, "https": https}
        if p == ["settings", "certificate"] and m == "POST":      # make a new own certificate
            b = self.body()
            extra = [str(x) for x in b.get("names", [])] + [(self.headers.get("Host") or "").rsplit(":", 1)[0]]
            if "names" in b:
                config["names"] = [str(x).strip() for x in b["names"] if str(x).strip()]
            config["cert"] = config["key"] = ""
            save_config()
            info = ensure_cert(force=True, extra=extra)
            if listen()[2]:
                threading.Timer(0.7, restart.set).start()
            return info
        if p == ["settings", "certificate"] and m == "GET":       # download it (to add to a trusted list)
            crt = config.get("cert") or tls_paths()[0]
            with open(crt, "rb") as f:
                data = f.read()
            self.extra_headers = (("Content-Disposition", 'attachment; filename="appc-dashboard.crt"'),)
            return self.send(200, data, "application/x-x509-ca-cert")
        if p == ["users"] and m == "POST":                        # add a user or change a password
            b = self.body()
            user, pw = str(b.get("user", "")).strip(), str(b.get("password", ""))
            valid_user(user, pw)
            config.setdefault("users", {})[user] = hash_password(pw)
            save_config()
            return {"users": sorted(config["users"])}
        if len(p) == 2 and p[0] == "users" and m == "DELETE":
            if p[1] not in (config.get("users") or {}):
                raise KeyError("no such user")
            if len(config["users"]) == 1:
                raise ValueError("this is the only login - add another one first")
            del config["users"][p[1]]
            save_config()
            for t in [t for t, v in sessions.items() if v["user"] == p[1]]:
                sessions.pop(t, None)
            return {"users": sorted(config["users"])}

        # firmware library and updates of several tablets
        if p == ["firmware"] and m == "GET":
            return [fw_info(os.path.join(fw_dir(), n)) for n in sorted(os.listdir(fw_dir())) if n.lower().endswith(".img")]
        if p == ["firmware"] and m == "POST":
            n = safe_fw_name(unquote(self.headers.get("X-Filename") or ""))
            tmp = os.path.join(fw_dir(), n + ".part")
            try:
                self.body_to_file(tmp)
                info = fw_info(tmp)
                if info.get("problem"):
                    raise ValueError("%s: %s" % (n, info["problem"]))
                os.replace(tmp, os.path.join(fw_dir(), n))
            finally:
                if os.path.exists(tmp):
                    os.remove(tmp)
            return fw_info(os.path.join(fw_dir(), n))
        if len(p) == 2 and p[0] == "firmware" and m == "DELETE":
            os.remove(os.path.join(fw_dir(), safe_fw_name(p[1])))
            return {"ok": True}
        if p == ["firmware", "install"] and m == "POST":
            b = self.body()
            return start_fw_job(b.get("file"), b.get("devices") or [])
        if p == ["jobs"] and m == "GET":
            return jobs

        # tablets
        if p == ["devices"] and m == "GET":
            page_seen()
            return [dict(d, status=status.get(d["id"], {})) for d in devices]
        if p == ["devices"] and m == "POST":
            b = self.body()
            a = norm_address(b.get("address", ""))
            if not a:
                raise ValueError("address needed")
            if any(d["address"] == a for d in devices):
                raise ValueError("already in the list")
            d = {"id": "t%d" % int(time.time() * 1000), "name": (b.get("name") or a).strip(), "address": a}
            with state_lock:
                devices.append(d)
                save_devices()
            threading.Thread(target=refresh, args=(d,), daemon=True).start()
            return d
        if len(p) == 2 and p[0] == "progress" and m == "GET":   # an upload / install the page is waiting for
            return progress.get(p[1]) or {"phase": None, "pct": None}
        if p == ["usb"] and m == "GET":               # tablets on this computer's USB ports
            return usb_tablets()
        if len(p) == 3 and p[0] == "usb" and p[2] == "setup" and m == "POST":
            return usb_setup(p[1])
        if p == ["refresh"] and m == "POST":
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
                list(ex.map(refresh, list(devices)))
            return {"ok": True}
        if len(p) >= 2 and p[0] == "devices":
            dev = find(p[1])
            sub = p[2:]
            if not sub and m == "DELETE":
                with state_lock:
                    devices.remove(dev)
                    save_devices()
                if is_network(dev["address"]):
                    adb("disconnect", dev["address"], timeout=10)
                status.pop(dev["id"], None)
                return {"ok": True}
            if not sub and m == "POST":   # rename / change address
                b = self.body()
                with state_lock:
                    if b.get("name"):
                        dev["name"] = b["name"].strip()
                    if b.get("address"):
                        dev["address"] = norm_address(b["address"])
                    save_devices()
                return dev
            if sub == ["refresh"] and m == "POST":
                return refresh(dev)
            if sub == ["panel"] and m == "GET":
                return with_device(dev, lambda a: panel(a), remote=False)
            if sub == ["panel"] and m == "POST":
                b = self.body()
                allowed = {"schedule", "skip", "startup", "adb_allow_new", "adb_allow_new_usb", "adb_notify", "buttons",
                           "pin_allowed_clear", "quiet", "pin_lock", "pin_restart", "time", "timezone", "auto_time",
                           "volume", "brightness"}
                payload = {k: v for k, v in b.items() if k in allowed}
                if not payload:
                    raise ValueError("nothing to change")

                def change(a):
                    if "schedule" in payload:   # no new shut-down times from the dashboard
                        kept = {e.get("id") for e in panel(a).get("schedule", []) if e.get("action") == "shutdown"}
                        for e in payload["schedule"]:
                            if e.get("action") == "shutdown" and e.get("id") not in kept:
                                raise ValueError("shut-down times can only be set on the tablet itself")
                    return panel(a, payload)
                return with_device(dev, change)
            if sub == ["screenshot"] and m == "GET":
                def shot(a):
                    rc, png = adb("-s", a, "exec-out", "screencap", "-p", timeout=30, binary=True)
                    if not png.startswith(b"\x89PNG"):
                        raise RuntimeError("screenshot failed")
                    return png
                png = with_device(dev, shot)
                return self.send(200, png, "image/png")
            if sub == ["files"] and m == "GET":
                q = self.query
                return with_device(dev, lambda a: list_files(a, q.get("root", "internal"), q.get("path", "")))
            if sub == ["file"] and m == "GET":   # download
                q = self.query
                tp = tablet_path(q.get("root", "internal"), q.get("path", ""))
                fd, tmp = tempfile.mkstemp(prefix="appc_dl_")
                os.close(fd)
                try:
                    rc, out = with_device(dev, lambda a: adb("-s", a, "pull", tp, tmp, timeout=3600))
                    if rc != 0:
                        raise RuntimeError(out.strip()[-300:])
                    size = os.path.getsize(tmp)
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Length", str(size))
                    self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + quote(posixpath.basename(tp)))
                    self.end_headers()
                    with open(tmp, "rb") as f:
                        shutil.copyfileobj(f, self.wfile, 1 << 20)
                finally:
                    os.remove(tmp)
                return None
            if sub == ["file"] and m == "POST":   # upload into folder ?root=&path=
                q = self.query
                name = posixpath.basename(unquote(self.headers.get("X-Filename") or ""))
                if not name or name in (".", "..") or "/" in name:
                    raise ValueError("bad file name")
                tp = tablet_path(q.get("root", "internal"), posixpath.join(q.get("path", ""), name))
                n = int(self.headers.get("Content-Length") or 0)
                fd, tmp = tempfile.mkstemp(prefix="appc_ul_")
                try:
                    with os.fdopen(fd, "wb") as f:
                        while n > 0:
                            chunk = self.rfile.read(min(n, 1 << 20))
                            if not chunk:
                                break
                            f.write(chunk)
                            n -= len(chunk)

                    task = self.headers.get("X-Task")

                    def up(a):
                        rc, out = push(a, tmp, tp, task)
                        if rc != 0:
                            raise RuntimeError(out.strip()[-300:])
                        media_scan(a, tp)
                        return {"ok": True, "path": tp}
                    return with_device(dev, up)
                finally:
                    os.remove(tmp)
            if sub == ["file"] and m == "DELETE":
                q = self.query
                if not q.get("path"):
                    raise ValueError("will not delete a whole storage")
                tp = tablet_path(q.get("root", "internal"), q["path"])

                def rm(a):
                    rc, out = shell(a, "rm -rf %s && echo @ok" % shlex.quote(tp), timeout=300)
                    if "@ok" not in out:
                        raise RuntimeError(out.strip()[-300:] or "delete failed")
                    media_scan(a, tp)
                    return {"ok": True}
                return with_device(dev, rm)
            if sub == ["firmware"] and m == "GET":
                return with_device(dev, lambda a: panel(a, firmware=True).get("firmware", []))
            if sub == ["install-file"] and m == "POST":
                b = self.body()
                tp = tablet_path(b.get("root", "internal"), b.get("path", ""))
                if tp.lower().endswith(".apk"):   # Android's own installer, like adb install (data kept on update)
                    set_progress(self.headers.get("X-Task"), "Installing", None)
                    return with_device(dev, lambda a: install_apk(a, tp))
                if tp.lower().endswith(".img"):   # firmware: Panel settings checks it and restarts into recovery
                    if b.get("root") != "sd" or "/" in b.get("path", "").strip("/"):
                        raise ValueError("firmware must be at the top level of the SD card")
                    name = posixpath.basename(tp)
                    return with_device(dev, lambda a: panel(a, {"do": "firmware_install", "file": name}))
                raise ValueError("only .apk and firmware .img files can be installed")
            if sub == ["mkdir"] and m == "POST":
                b = self.body()
                tp = tablet_path(b.get("root", "internal"), b.get("path", ""))
                rc, out = with_device(dev, lambda a: shell(a, "mkdir -p %s && echo @ok" % shlex.quote(tp)))
                if "@ok" not in out:
                    raise RuntimeError(out.strip() or "could not create the folder")
                return {"ok": True}
            if sub == ["apps"] and m == "GET":
                cmd = ("for p in $(pm list packages -3 | cut -d: -f2); do "
                       "echo \"$p $(dumpsys package $p | grep -m1 versionName= | cut -d= -f2)\"; done")
                rc, out = with_device(dev, lambda a: shell(a, cmd, timeout=60))
                apps = []
                for l in out.splitlines():
                    q = l.strip().split(" ", 1)
                    if q and q[0]:
                        apps.append({"package": q[0], "version": q[1] if len(q) > 1 else ""})
                return sorted(apps, key=lambda x: x["package"])
            if sub == ["action"] and m == "POST":
                b = self.body()
                act = b.get("do")
                if act in ("screen_on", "screen_off", "reboot", "unpin", "open_startup_app", "hide_developer_options",
                           "show_developer_options", "identify"):
                    return with_device(dev, lambda a: panel(a, {"do": act}))
                if act == "launch":
                    comp = b.get("component", "")
                    if not re.fullmatch(r"[\w.]+/[\w.$]+", comp):
                        raise ValueError("bad component")
                    rc, out = with_device(dev, lambda a: shell(
                        a, "am start -a android.intent.action.MAIN -c android.intent.category.LAUNCHER -f 0x10200000 -n '%s'" % comp))
                    return {"ok": "Error" not in out, "output": out.strip()}
                if act in ("uninstall", "stop"):
                    pkg = b.get("package", "")
                    if not re.fullmatch(r"[\w.]+", pkg):
                        raise ValueError("bad package name")
                    if act == "stop":
                        rc, out = with_device(dev, lambda a: shell(a, "am force-stop " + pkg))
                    else:
                        rc, out = with_device(dev, lambda a: adb("-s", a, "uninstall", pkg, timeout=120))
                    return {"ok": rc == 0 and "Failure" not in out, "output": out.strip()}
                raise ValueError("unknown action")
            if sub == ["install"] and m == "POST":
                b = self.body()
                f = os.path.join(apk_dir(), safe_apk_name(b.get("apk")))
                if not os.path.isfile(f):
                    raise KeyError("APK not in the library")
                tmp = "/data/local/tmp/appc_dashboard.apk"

                task = self.headers.get("X-Task")

                def inst(a):   # push + pm install, so Play Protect can be skipped (install_apk)
                    rc, out = push(a, f, tmp, task, timeout=600)
                    if rc != 0:
                        return {"ok": False, "output": out.strip()[-500:]}
                    set_progress(task, "Installing", None)
                    try:
                        return install_apk(a, tmp)
                    finally:
                        shell(a, "rm -f " + tmp)
                return with_device(dev, inst)
            raise KeyError("not found")

        # APK library
        if p == ["apks"] and m == "GET":
            out = []
            for n in sorted(os.listdir(apk_dir())):
                if n.lower().endswith(".apk"):
                    st = os.stat(os.path.join(apk_dir(), n))
                    out.append({"name": n, "size": st.st_size, "time": int(st.st_mtime)})
            return out
        if p == ["apks"] and m == "POST":
            n = safe_apk_name(self.headers.get("X-Filename"))
            data = self.body(raw=True)
            if not data.startswith(b"PK"):
                raise ValueError("not an APK file")
            fd, tmp = tempfile.mkstemp(dir=apk_dir(), suffix=".part")
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, os.path.join(apk_dir(), n))
            return {"name": n, "size": len(data)}
        if len(p) == 2 and p[0] == "apks" and m == "DELETE":
            os.remove(os.path.join(apk_dir(), safe_apk_name(p[1])))
            return {"ok": True}
        raise KeyError("not found")


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = os.name != "nt"     # on Windows it would let a second program share the port

    def handle_error(self, request, client_address):
        if args.verbose:    # dropped connections, plain http on the https port, ...
            super().handle_error(request, client_address)


def open_listener():
    global active_port, port_notice
    host, preferred, https = listen()
    port = first_free(host, preferred)
    while True:
        if port is None:
            raise OSError("no free port found from %d to %d" % (preferred, min(preferred + 49, 65535)))
        try:
            srv = Server((host, port), Handler)
            break
        except OSError as e:
            if e.errno not in (errno.EADDRINUSE, errno.EACCES):
                raise
            port = first_free(host, port + 1)   # taken between the check and the bind: the next one
    active_port = port
    port_notice = None if port == preferred else (
        "Port %d is in use by another program (or not allowed for this account), so the dashboard is running on port %d."
        % (preferred, port))
    if https:
        # the handshake happens in the request's own thread, so a slow client cannot block the others
        srv.socket = tls_context().wrap_socket(srv.socket, server_side=True, do_handshake_on_connect=False)
    return srv


def parent_alive(pid):
    if os.name != "posix":      # (on Windows os.kill would end the process; --parent-pid is only used by the Mac app)
        return True
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return True


def main():
    global args, setup_code
    ap = argparse.ArgumentParser(description="APPC-10DSQ tablet dashboard")
    ap.add_argument("--host", default=None, help="address to listen on for this run (otherwise from the Settings page; "
                    "127.0.0.1 = this computer only, 0.0.0.0 = every network)")
    ap.add_argument("--port", type=int, default=None, help="port for this run (otherwise from the Settings page, 8080 at first)")
    ap.add_argument("--https", dest="https", action="store_true", default=None, help="HTTPS for this run")
    ap.add_argument("--no-https", dest="https", action="store_false", help="plain HTTP for this run")
    ap.add_argument("--adb", default=find_adb(), help="adb to use (default: adb on the PATH, else platform-tools/ next to "
                    "this script, else the Android SDK's)")
    ap.add_argument("--data", default=HERE, help="folder for settings, the tablet list, the APK and firmware libraries")
    ap.add_argument("--interval", type=int, default=60, help="seconds between status checks")
    ap.add_argument("--idle", type=int, default=30, help="disconnect after this many idle seconds (0 = stay connected)")
    ap.add_argument("--no-login", action="store_true", help="no user name / password (only for a computer nobody else can reach)")
    ap.add_argument("--set-password", metavar="USER", help="set this user's password (asked on the terminal) and exit")
    ap.add_argument("--local-token", default=os.environ.get("APPC_LOCAL_TOKEN"), help=argparse.SUPPRESS)
    ap.add_argument("--parent-pid", type=int, default=0, help=argparse.SUPPRESS)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    load_config()
    if args.set_password:
        import getpass
        pw = getpass.getpass("New password for %s: " % args.set_password)
        valid_user(args.set_password, pw)
        config.setdefault("users", {})[args.set_password] = hash_password(pw)
        save_config()
        print("saved")
        return
    rc, out = adb("version", timeout=20)
    if rc != 0:
        sys.exit("adb does not run (%s): install it or pass --adb" % out.strip())
    print(out.splitlines()[0] if out else "adb")
    load_devices()
    threading.Thread(target=poller, daemon=True).start()
    threading.Thread(target=reaper, daemon=True).start()
    if args.no_login:
        print("WARNING: --no-login - anyone who can reach this port controls the tablets")
    elif not config.get("users"):
        setup_code = "%06d" % secrets.randbelow(1000000)
        print("First start: open the page and choose the login. From another computer the setup code is %s" % setup_code)
    srv = None
    try:
        while not quitting.is_set():
            try:
                srv = open_listener()
            except Exception as e:
                if srv is None:
                    sys.exit("cannot start: %s" % e)
                print("new settings do not work (%s) - still on the old address" % e, flush=True)
                srv = None
                restart.clear()
                continue
            host, port, https = running()
            if port_notice:     # the Mac app shows this line to the user
                print("APPC-DASHBOARD-NOTICE " + port_notice, flush=True)
            shown = "127.0.0.1" if host in ("0.0.0.0", "127.0.0.1", "") else host
            # the Mac app reads this line to know where its window should go
            print("APPC-DASHBOARD-URL %s://%s:%d/" % ("https" if https else "http", shown, port), flush=True)
            if host == "0.0.0.0":
                print("Reachable from other computers at %s" % ", ".join(
                    "%s://%s:%d/" % ("https" if https else "http", ip, port) for ip in local_addresses() if ip != "127.0.0.1"), flush=True)
            restart.clear()
            t = threading.Thread(target=srv.serve_forever, daemon=True)
            t.start()
            while not restart.wait(2):
                if args.parent_pid and not parent_alive(args.parent_pid):
                    quitting.set()
                    break
            srv.shutdown()
            srv.server_close()
    except KeyboardInterrupt:
        pass
    for d in list(devices):     # leave the tablets free
        if is_network(d["address"]):
            adb("disconnect", d["address"], timeout=10)


if __name__ == "__main__":
    main()
