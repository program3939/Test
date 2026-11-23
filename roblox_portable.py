#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Roblox Portable – Downloader & Launcher (nur Python-Standardbibliothek)

* Läuft ohne Administratorrechte (keine UAC-Erhöhung, keine Registry-Schreibzugriffe).
* Alle Daten liegen unter ./RobloxPortable/ neben dem Programm.
* Lädt ausschließlich von offiziellen Roblox-Hosts (roblox.com / rbxcdn.com).
* Verändert keine Roblox-Dateien, berührt keinen Anti-Cheat. Nur Downloader + Launcher.

CLI (zum Testen):  roblox_portable.py --cli status|install|start [ARG] [--base ORDNER]
"""
import errno
import datetime
import hashlib
import http.client
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

# Originale Umgebungswerte VOR jeder Umleitung merken (für den Leck-Check).
REAL_ENV = {k: os.environ.get(k, "") for k in
            ("LOCALAPPDATA", "APPDATA", "TEMP", "TMP", "USERPROFILE", "PROGRAMDATA")}
sys.dont_write_bytecode = True

UA = "RobloxPortable/1.0 (portable downloader)"
VERSION_APIS = [
    "https://clientsettingscdn.roblox.com/v2/client-version/WindowsPlayer",
    "https://clientsettings.roblox.com/v2/client-version/WindowsPlayer",
]
CDN = "https://setup.rbxcdn.com"
ALLOWED_SUFFIXES = ("roblox.com", "rbxcdn.com")   # nur offizielle Hosts
ALLOW_HTTP = False                                  # nur für Tests änderbar
APP_VERSION = 0                                     # wird beim Build (GitHub Actions) auf die Build-Nummer gesetzt; 0 = Entwicklung, kein Selbst-Update
UPDATE_BASE = "https://github.com/program3939/Test/releases/download/latest"
UPDATE_HOSTS = ("github.com", "githubusercontent.com")   # nur für das Launcher-Update
PLAYER_EXE = "RobloxPlayerBeta.exe"

# Paketname -> Zielunterordner im Versionsordner (wie beim offiziellen Bootstrapper)
PACKAGE_DIRS = {
    "RobloxApp.zip": "",
    "shaders.zip": "shaders",
    "ssl.zip": "ssl",
    "WebView2.zip": "",
    "WebView2RuntimeInstaller.zip": "WebView2RuntimeInstaller",
    "content-avatar.zip": "content/avatar",
    "content-configs.zip": "content/configs",
    "content-fonts.zip": "content/fonts",
    "content-sky.zip": "content/sky",
    "content-sounds.zip": "content/sounds",
    "content-textures2.zip": "content/textures",
    "content-models.zip": "content/models",
    "content-platform-fonts.zip": "PlatformContent/pc/fonts",
    "content-platform-dictionaries.zip": "PlatformContent/pc/shared_compression_dictionaries",
    "content-terrain.zip": "PlatformContent/pc/terrain",
    "content-textures3.zip": "PlatformContent/pc/textures",
    "extracontent-luapackages.zip": "ExtraContent/LuaPackages",
    "extracontent-translations.zip": "ExtraContent/translations",
    "extracontent-models.zip": "ExtraContent/models",
    "extracontent-textures.zip": "ExtraContent/textures",
    "extracontent-places.zip": "ExtraContent/places",
}

APP_SETTINGS = ('<?xml version="1.0" encoding="UTF-8"?>\n<Settings>\n'
                '\t<ContentFolder>content</ContentFolder>\n'
                '\t<BaseUrl>http://www.roblox.com</BaseUrl>\n</Settings>\n')


class UserError(Exception):
    """Fehler mit bereits verständlicher deutscher Meldung."""


class Cancelled(Exception):
    pass


# --------------------------------------------------------------------------- Pfade
class Paths:
    def __init__(self, base=None):
        if base is None:
            exe = sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__)
            base = os.path.join(os.path.dirname(os.path.abspath(exe)), "RobloxPortable")
        self.base = os.path.abspath(base)
        self.versions = os.path.join(self.base, "Versions")
        self.downloads = os.path.join(self.base, "Downloads")
        self.data = os.path.join(self.base, "Data")
        self.local = os.path.join(self.data, "Local")
        self.roaming = os.path.join(self.data, "Roaming")
        self.temp = os.path.join(self.data, "Temp")
        self.profile = os.path.join(self.data, "Profile")
        self.logs = os.path.join(self.base, "Logs")
        self.state = os.path.join(self.base, "state.json")

    def make(self):
        for d in (self.versions, self.downloads, self.local, self.roaming,
                  self.temp, self.profile, self.logs):
            os.makedirs(d, exist_ok=True)


class Ctx:
    """Verbindung zwischen Kernlogik und Oberfläche."""
    def __init__(self, paths, log=None, progress=None, cancel=None):
        self.paths = paths
        self._log = log or (lambda m: None)
        self._progress = progress or (lambda d, t, s: None)
        self.cancel = cancel or threading.Event()
        self.done = 0
        self.total = 0
        self._last = 0.0

    def log(self, msg):
        line = "[%s] %s" % (datetime.datetime.now().strftime("%H:%M:%S"), msg)
        try:
            os.makedirs(self.paths.logs, exist_ok=True)
            with open(os.path.join(self.paths.logs, "launcher.log"), "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass
        self._log(msg)

    def add(self, n, text=""):
        self.done += n
        now = time.time()
        if now - self._last > 0.1 or self.done >= self.total:
            self._last = now
            self._progress(self.done, self.total, text)

    def check_cancel(self):
        if self.cancel.is_set():
            raise Cancelled()


# --------------------------------------------------------------------------- Netzwerk
def check_host(url, allowed=None):
    allowed = allowed or ALLOWED_SUFFIXES
    p = urllib.parse.urlparse(url)
    host = (p.hostname or "").lower()
    if p.scheme != "https" and not (ALLOW_HTTP and p.scheme == "http"):
        raise UserError("Unsichere Verbindung abgelehnt (nur HTTPS erlaubt).")
    if not any(host == s or host.endswith("." + s) for s in allowed):
        raise UserError("Download von nicht-offiziellem Server abgelehnt: %s" % host)


def _open(url, headers=None, timeout=30, allowed=None):
    check_host(url, allowed)
    req = urllib.request.Request(url, headers=dict({"User-Agent": UA}, **(headers or {})))
    resp = urllib.request.urlopen(req, timeout=timeout)
    check_host(resp.geturl(), allowed)        # auch nach Weiterleitungen prüfen
    return resp


NET_ERRORS = (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError,
              http.client.HTTPException)


def explain(e):
    """Technische Ausnahme -> verständliche deutsche Meldung."""
    if isinstance(e, UserError):
        return str(e)
    if isinstance(e, urllib.error.HTTPError):
        if e.code in (401, 403, 407):
            return ("Der Server hat die Anfrage abgelehnt (HTTP %d). Ein Proxy, eine Firewall oder eine "
                    "Netzwerkrichtlinie blockiert vermutlich Roblox." % e.code)
        if e.code == 404:
            return "Die Datei wurde auf dem Roblox-Server nicht gefunden (HTTP 404). Bitte später erneut versuchen."
        return "Serverfehler von Roblox (HTTP %d). Bitte später erneut versuchen." % e.code
    if isinstance(e, NET_ERRORS):
        import ssl
        r = getattr(e, "reason", e)
        if isinstance(r, ssl.SSLError):
            why = ("Zertifikatsfehler: Ein Proxy/Virenscanner im Netzwerk prüft vermutlich den HTTPS-Verkehr "
                   "und Roblox wird dadurch abgelehnt.")
        elif isinstance(r, socket.gaierror):
            why = "Der Servername ließ sich nicht auflösen (DNS). Roblox-Domains sind vermutlich im Netzwerk gesperrt."
        elif isinstance(r, (socket.timeout, TimeoutError)):
            why = "Zeitüberschreitung: Der Server antwortet nicht (Firewall/Netzwerkfilter?)."
        elif isinstance(r, ConnectionRefusedError):
            why = "Die Verbindung wurde aktiv abgelehnt (Firewall/Proxy)."
        else:
            why = "Verbindung fehlgeschlagen."
        return ("Keine Verbindung zu den Roblox-Servern. %s Bitte Internetverbindung, Proxy oder Firewall prüfen. "
                "Technische Details: %s: %s" % (why, type(r).__name__, r))
    if isinstance(e, PermissionError):
        return ("Zugriff auf eine Datei verweigert/blockiert. Möglicherweise sperrt ein Virenscanner, eine "
                "Sicherheitsrichtlinie oder ein laufender Roblox-Prozess die Datei: %s" % e)
    if isinstance(e, OSError) and e.errno == errno.ENOSPC:
        return "Nicht genug Speicherplatz auf dem Datenträger."
    if isinstance(e, zipfile.BadZipFile):
        return "Ein heruntergeladenes Paket ist beschädigt. Bitte erneut installieren."
    return "Unerwarteter Fehler: %s: %s" % (type(e).__name__, e)


def get_latest_version():
    last = None
    for api in VERSION_APIS:
        try:
            with _open(api) as r:
                data = json.loads(r.read().decode("utf-8"))
            v = data.get("clientVersionUpload", "")
            if re.fullmatch(r"version-[0-9A-Za-z]+", v):
                return v
            last = UserError("Die Versions-API lieferte eine unerwartete Antwort.")
        except (UserError,) as e:
            last = e
        except (ValueError, KeyError) as e:
            last = UserError("Die Versions-API lieferte eine ungültige Antwort.")
        except Exception as e:
            last = e
    raise last if last else UserError("Version konnte nicht ermittelt werden.")


class Package:
    def __init__(self, name, md5, csize, size):
        self.name, self.md5, self.csize, self.size = name, md5, csize, size


def parse_manifest(text):
    lines = [l.strip() for l in text.replace("\r", "").split("\n") if l.strip() != ""]
    if not lines or lines[0] != "v0":
        raise UserError("Unbekanntes Paketmanifest-Format von Roblox.")
    body = lines[1:]
    if len(body) % 4:
        raise UserError("Das Paketmanifest ist unvollständig oder beschädigt.")
    out = []
    for i in range(0, len(body), 4):
        name, md5, cs, sz = body[i:i + 4]
        if not (re.fullmatch(r"[A-Za-z0-9._-]+", name) and re.fullmatch(r"[0-9a-fA-F]{32}", md5)
                and cs.isdigit() and sz.isdigit()):
            raise UserError("Ungültiger Eintrag im Paketmanifest: %r" % name)
        out.append(Package(name, md5.lower(), int(cs), int(sz)))
    return out


# --------------------------------------------------------------------------- Zustand
def load_state(paths):
    try:
        with open(paths.state, "r", encoding="utf-8") as f:
            s = json.load(f)
        if isinstance(s, dict):
            return s
    except (OSError, ValueError):
        pass
    return {}


def save_state(paths, st):
    tmp = paths.state + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=1)
    os.replace(tmp, paths.state)


def installed_version(paths):
    st = load_state(paths)
    v = st.get("version")
    if v and st.get("complete") and os.path.isfile(os.path.join(paths.versions, v, PLAYER_EXE)):
        return v
    return None


# --------------------------------------------------------------------------- Download
def _md5(path, ctx=None):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            if ctx:
                ctx.check_cancel()
            h.update(chunk)
    return h.hexdigest()


def download_package(ctx, version, pkg):
    """Lädt ein Paket (mit Fortsetzen), prüft den MD5-Hash, gibt den Pfad der .zip zurück."""
    d = os.path.join(ctx.paths.downloads, version)
    os.makedirs(d, exist_ok=True)
    zpath, part = os.path.join(d, pkg.name), os.path.join(d, pkg.name + ".part")
    url = "%s/%s-%s" % (CDN, version, pkg.name)

    if os.path.isfile(zpath):                       # schon vollständig geladen?
        if _md5(zpath, ctx) == pkg.md5:
            ctx.add(pkg.csize, pkg.name)
            return zpath
        os.remove(zpath)

    counted = 0                                     # bereits in den Fortschritt eingerechnet
    last_err = None
    for attempt in range(1, 5):
        ctx.check_cancel()
        try:
            have = os.path.getsize(part) if os.path.isfile(part) else 0
            if have > pkg.csize:
                os.remove(part)
                have = 0
            if have != pkg.csize:
                hdr = {"Range": "bytes=%d-" % have} if have else {}
                with _open(url, hdr) as r:
                    if have and r.status != 206:    # Server kann nicht fortsetzen
                        have = 0
                    mode = "ab" if have else "wb"
                    if counted != have:
                        ctx.add(have - counted, pkg.name)
                        counted = have
                    with open(part, mode) as f:
                        while True:
                            ctx.check_cancel()
                            chunk = r.read(1 << 16)
                            if not chunk:
                                break
                            f.write(chunk)
                            counted += len(chunk)
                            ctx.add(len(chunk), pkg.name)
            if os.path.getsize(part) != pkg.csize:
                raise http.client.IncompleteRead(b"")
            if _md5(part, ctx) != pkg.md5:
                os.remove(part)
                ctx.add(-counted)
                counted = 0
                last_err = UserError("Prüfsumme von %s stimmt nicht (Datei beschädigt oder manipuliert)." % pkg.name)
                ctx.log("Hash-Fehler bei %s – neuer Versuch %d/4" % (pkg.name, attempt))
                continue
            os.replace(part, zpath)
            return zpath
        except urllib.error.HTTPError as e:
            if e.code == 416:                       # Range unpassend -> von vorn
                if os.path.isfile(part):
                    os.remove(part)
                ctx.add(-counted)
                counted = 0
                last_err = e
                continue
            if e.code in (401, 403, 404, 407):
                raise
            last_err = e
        except (Cancelled, UserError):
            raise
        except PermissionError:
            raise
        except NET_ERRORS as e:
            last_err = e
        ctx.log("Verbindungsproblem bei %s – Versuch %d/4 (Download wird fortgesetzt)" % (pkg.name, attempt))
        time.sleep(min(2 * attempt, 6) if not ALLOW_HTTP else 0.05)
    raise last_err or UserError("Download von %s fehlgeschlagen." % pkg.name)


def extract_zip(ctx, zpath, dest_root, subdir):
    target = os.path.join(dest_root, *[p for p in subdir.split("/") if p]) if subdir else dest_root
    real_t = os.path.realpath(target)
    os.makedirs(target, exist_ok=True)
    with zipfile.ZipFile(zpath) as z:
        for info in z.infolist():
            ctx.check_cancel()
            name = info.filename.replace("\\", "/")
            if name.endswith("/"):
                continue
            parts = [p for p in name.split("/") if p not in ("", ".")]
            if not parts or ".." in parts or re.match(r"^[A-Za-z]:", parts[0]):
                raise UserError("Unsicherer Dateipfad im Paket (%s) – Abbruch." % info.filename)
            out = os.path.realpath(os.path.join(target, *parts))
            if not out.startswith(real_t + os.sep):
                raise UserError("Unsicherer Dateipfad im Paket (%s) – Abbruch." % info.filename)
            os.makedirs(os.path.dirname(out), exist_ok=True)
            with z.open(info) as src, open(out, "wb") as dst:
                shutil.copyfileobj(src, dst, 1 << 20)


def remove_old(ctx, keep):
    p = ctx.paths
    for root in (p.versions, p.downloads):
        if not os.path.isdir(root):
            continue
        for name in os.listdir(root):
            if name != keep:
                try:
                    shutil.rmtree(os.path.join(root, name))
                    ctx.log("Alte Version entfernt: %s" % name)
                except OSError as e:
                    ctx.log("Alte Version %s konnte nicht entfernt werden (%s) – nächster Versuch beim nächsten Start." % (name, e))


def install(ctx, force=False):
    """Installiert/aktualisiert. Rückgabe: 'uptodate' | 'installed'."""
    p = ctx.paths
    p.make()
    ctx.log("Prüfe aktuelle Roblox-Version …")
    latest = get_latest_version()
    ctx.log("Neueste Version: %s" % latest)
    st = load_state(p)
    if not force and installed_version(p) == latest:
        remove_old(ctx, latest)
        ctx.log("Roblox ist aktuell.")
        return "uptodate"

    ctx.log("Lade Paketmanifest …")
    with _open("%s/%s-rbxPkgManifest.txt" % (CDN, latest)) as r:
        pkgs = parse_manifest(r.read().decode("utf-8", "replace"))
    if not any(x.name == "RobloxApp.zip" for x in pkgs):
        raise UserError("Das Manifest enthält kein Player-Paket (RobloxApp.zip).")

    total_unpacked = sum(x.size for x in pkgs)
    free = shutil.disk_usage(p.base).free
    if free < total_unpacked + 200 * 1024 * 1024:
        raise UserError("Zu wenig freier Speicher: benötigt ca. %d MB, frei %d MB."
                        % ((total_unpacked + 200 * 2 ** 20) // 2 ** 20, free // 2 ** 20))

    if st.get("version") != latest:
        st = {"version": latest, "complete": False, "packages": {}}
    st["complete"] = False
    save_state(p, st)

    vdir = os.path.join(p.versions, latest)
    os.makedirs(vdir, exist_ok=True)
    ctx.total = sum(x.csize for x in pkgs)
    ctx.done = 0
    for pkg in pkgs:
        ctx.check_cancel()
        if st["packages"].get(pkg.name) == pkg.md5:
            ctx.add(pkg.csize, pkg.name)
            continue
        if pkg.name not in PACKAGE_DIRS:
            ctx.log("WARNUNG: unbekanntes Paket %s wird übersprungen (Zielordner unbekannt)." % pkg.name)
            ctx.add(pkg.csize, pkg.name)
            continue
        ctx.log("Lade %s …" % pkg.name)
        zpath = download_package(ctx, latest, pkg)
        ctx.log("Entpacke %s …" % pkg.name)
        extract_zip(ctx, zpath, vdir, PACKAGE_DIRS[pkg.name])
        st["packages"][pkg.name] = pkg.md5
        save_state(p, st)
        try:
            os.remove(zpath)
        except OSError:
            pass

    with open(os.path.join(vdir, "AppSettings.xml"), "w", encoding="utf-8") as f:
        f.write(APP_SETTINGS)
    if not os.path.isfile(os.path.join(vdir, PLAYER_EXE)):
        raise UserError("%s fehlt nach der Installation – wurde sie von einem Virenscanner entfernt?" % PLAYER_EXE)
    st["complete"] = True
    save_state(p, st)
    remove_old(ctx, latest)
    ctx.log("Installation abgeschlossen: %s" % latest)
    return "installed"


# --------------------------------------------------------------------------- Start
def child_env(paths):
    paths.make()
    env = dict(os.environ)
    env.update({
        "LOCALAPPDATA": paths.local, "APPDATA": paths.roaming,
        "TEMP": paths.temp, "TMP": paths.temp, "TMPDIR": paths.temp,
        "USERPROFILE": paths.profile, "HOME": paths.profile,
        "WEBVIEW2_USER_DATA_FOLDER": os.path.join(paths.local, "WebView2"),
    })
    return env


_URI_KEYS = {  # roblox-player:-Schlüssel -> Kommandozeile (Format des offiziellen Bootstrappers)
    "gameinfo": "-t", "placelauncherurl": "-j", "browsertrackerid": "-b",
    "robloxlocale": "--rloc", "gamelocale": "--gloc", "launchexp": "-launchExp",
}


def build_args(text):
    """Eingabefeld -> (Argumentliste, Hinweis). Es wird nie eine Shell benutzt."""
    t = (text or "").strip()
    if not t:
        return ["--app"], "Starte Roblox im App-Modus (Anmeldung/Spielauswahl im Programmfenster)."
    low = t.lower()
    if low.startswith("roblox-player:"):
        args = []
        for part in t[len("roblox-player:"):].split("+")[1:]:
            if ":" not in part:
                continue
            k, v = part.split(":", 1)
            k = k.lower()
            if k == "launchmode":
                if v not in ("play", "app"):
                    raise UserError("Nicht unterstützter launchmode: %s" % v)
                args.append("--" + v)
            elif k == "placelauncherurl":
                v = urllib.parse.unquote(v)
                u = urllib.parse.urlparse(v)
                h = (u.hostname or "").lower()
                if u.scheme != "https" or not (h == "roblox.com" or h.endswith(".roblox.com")):
                    raise UserError("placelauncherurl zeigt nicht auf roblox.com – abgelehnt.")
                args += ["-j", v]
            elif k == "launchtime":
                if not v.isdigit():
                    raise UserError("Ungültige launchtime im Link.")
                args.append("--launchtime=" + v)
            elif k == "channel":
                if v:
                    args += ["-channel", v]
            elif k in _URI_KEYS:
                args += [_URI_KEYS[k], urllib.parse.unquote(v)]
        if not args:
            raise UserError("Der roblox-player:-Link enthält keine verwertbaren Angaben.")
        return args, "Starte mit übergebenem roblox-player:-Link (Anmeldetickets gelten nur kurz)."
    if re.search(r"roblox\.com/.*games/\d+", low) or t.isdigit():
        return ["--app"], ("Ein reiner Spiel-Link enthält kein Anmeldeticket. Roblox wird im App-Modus gestartet – "
                           "bitte dort anmelden und das Spiel auswählen.")
    raise UserError("Eingabe nicht erkannt. Erlaubt: leer, roblox-player:-Link oder roblox.com/games/…-Link.")


def is_elevated():
    if os.name != "nt":
        return False
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def start_player(ctx, text):
    p = ctx.paths
    v = installed_version(p)
    if not v:
        raise UserError("Roblox ist noch nicht installiert. Bitte zuerst „Installieren/Aktualisieren“ klicken.")
    args, note = build_args(text)
    ctx.log(note)
    exe = os.path.join(p.versions, v, PLAYER_EXE)
    if not exe.lower().endswith(".exe"):          # nie Skripte (.bat/.ps1/.cmd) starten
        raise UserError("Es werden ausschließlich .exe-Dateien gestartet.")
    if is_elevated():
        raise UserError("Das Programm läuft mit Administratorrechten. Bitte normal (ohne „Als Administrator ausführen“) starten.")
    try:
        # Direkter EXE-Start als Kindprozess: keine Shell, kein Skript, keine Rechteerhöhung.
        return subprocess.Popen([exe] + args, cwd=os.path.dirname(exe), env=child_env(p), shell=False)
    except PermissionError as e:
        raise UserError("Start blockiert (Virenscanner/Richtlinie?): %s" % e)
    except OSError as e:
        raise UserError("Roblox konnte nicht gestartet werden: %s" % e)


# --------------------------------------------------------------------------- Leck-Check
def _inside(path, base):
    path, base = os.path.abspath(path), os.path.abspath(base)
    return path == base or path.startswith(base + os.sep)


def file_snapshot(paths):
    snap = {}
    R = REAL_ENV
    L, A, T, U, PD = R["LOCALAPPDATA"], R["APPDATA"], R["TEMP"] or R["TMP"], R["USERPROFILE"], R["PROGRAMDATA"]
    walk_dirs = []
    if L:
        walk_dirs += [os.path.join(L, "Roblox"), os.path.join(os.path.dirname(L), "LocalLow", "Roblox")]
    if A:
        walk_dirs.append(os.path.join(A, "Roblox"))
    shallow = [d for d in (T, L, A) if d]
    for sm in ([os.path.join(A, "Microsoft", "Windows", "Start Menu", "Programs")] if A else []) + \
              ([os.path.join(PD, "Microsoft", "Windows", "Start Menu", "Programs")] if PD else []):
        shallow += [sm, os.path.join(sm, "Startup")]
    if U:
        shallow.append(os.path.join(U, "Desktop"))

    def add(fp):
        if _inside(fp, paths.base):
            return
        try:
            s = os.stat(fp)
            snap[fp] = (s.st_size, int(s.st_mtime))
        except OSError:
            pass

    count = 0
    for d in walk_dirs:
        for root, _dirs, files in os.walk(d):
            for fn in files:
                add(os.path.join(root, fn))
                count += 1
                if count > 50000:
                    break
    for d in shallow:
        try:
            for name in os.listdir(d):
                if "roblox" in name.lower() or "rbx" in name.lower():
                    fp = os.path.join(d, name)
                    if os.path.isdir(fp):
                        for root, _dirs, files in os.walk(fp):
                            for fn in files:
                                add(os.path.join(root, fn))
                    else:
                        add(fp)
        except OSError:
            pass
    return snap


def reg_snapshot():
    snap = {}
    if os.name != "nt":
        return snap
    import winreg

    def tree(key, depth=0):
        n_sub, n_val, last = winreg.QueryInfoKey(key)
        sig = [n_sub, n_val, last]
        if depth < 4:
            for i in range(n_sub):
                try:
                    with winreg.OpenKey(key, winreg.EnumKey(key, i)) as sk:
                        s = tree(sk, depth + 1)
                        sig[0] += s[0]; sig[1] += s[1]; sig[2] = max(sig[2], s[2])
                except OSError:
                    pass
        return sig

    def scan(root, rname, path):
        try:
            with winreg.OpenKey(root, path) as k:
                for i in range(winreg.QueryInfoKey(k)[0]):
                    name = winreg.EnumKey(k, i)
                    if "roblox" in name.lower() or "rbx" in name.lower():
                        try:
                            with winreg.OpenKey(k, name) as sk:
                                snap["%s\\%s\\%s" % (rname, path, name)] = tuple(tree(sk))
                        except OSError:
                            snap["%s\\%s\\%s" % (rname, path, name)] = None
        except OSError:
            pass

    for root, rname in ((winreg.HKEY_CURRENT_USER, "HKCU"), (winreg.HKEY_LOCAL_MACHINE, "HKLM")):
        for path in (r"Software", r"Software\Classes", r"Software\WOW6432Node",
                     r"Software\Microsoft\Windows\CurrentVersion\Uninstall"):
            scan(root, rname, path)
        try:
            with winreg.OpenKey(root, r"Software\Microsoft\Windows\CurrentVersion\Run") as k:
                for i in range(winreg.QueryInfoKey(k)[1]):
                    n, v, _t = winreg.EnumValue(k, i)
                    if "roblox" in (n + str(v)).lower():
                        snap["%s\\...\\Run\\%s" % (rname, n)] = str(v)
        except OSError:
            pass
    return snap


def take_snapshot(paths):
    return {"files": file_snapshot(paths), "reg": reg_snapshot()}


def diff_report(before, after, label):
    lines = ["Leck-Check (%s):" % label]
    bf, af = before["files"], after["files"]
    new = sorted(k for k in af if k not in bf)
    chg = sorted(k for k in af if k in bf and af[k] != bf[k])
    gone = sorted(k for k in bf if k not in af)
    br, ar = before["reg"], after["reg"]
    rnew = sorted(k for k in ar if k not in br)
    rchg = sorted(k for k in ar if k in br and ar[k] != br[k])
    if not (new or chg or gone or rnew or rchg):
        lines.append("  Keine neuen/geänderten Dateien oder Registry-Schlüssel außerhalb des Ordners gefunden.")
        if os.name != "nt":
            lines.append("  (Hinweis: Nicht unter Windows – Registry-Prüfung entfällt.)")
    for title, items in (("NEUE Dateien außerhalb", new), ("GEÄNDERTE Dateien außerhalb", chg),
                         ("GELÖSCHTE Dateien außerhalb", gone),
                         ("NEUE Registry-Schlüssel", rnew), ("GEÄNDERTE Registry-Schlüssel", rchg)):
        if items:
            lines.append("  %s (%d):" % (title, len(items)))
            lines += ["    " + i for i in items[:40]]
            if len(items) > 40:
                lines.append("    … und %d weitere" % (len(items) - 40))
    lines.append("  Prüfumfang: Roblox-Ordner in echtem LocalAppData/AppData/LocalLow, Namen mit 'roblox'/'rbx' in "
                 "Temp, Startmenü, Autostart, Desktop sowie Registry-Schlüssel unter HKCU/HKLM\\Software "
                 "(Namen mit 'roblox'/'rbx'). Nicht erfasst: Schreibzugriffe in beliebige andere Pfade.")
    return "\n".join(lines)


def monitor_process(ctx, proc, before):
    """Prüft 20 s nach dem Start und beim Beenden; schreibt Bericht in Logs/."""
    reports = []
    t0 = time.time()
    while proc.poll() is None and time.time() - t0 < 20:
        time.sleep(0.5)
    reports.append(diff_report(before, take_snapshot(ctx.paths), "20 s nach Start"))
    ctx.log(reports[-1])
    proc.wait()
    ctx.log("Roblox wurde beendet (Exitcode %s)." % proc.returncode)
    reports.append(diff_report(before, take_snapshot(ctx.paths), "nach Beenden"))
    ctx.log(reports[-1])
    try:
        fn = os.path.join(ctx.paths.logs, "leckcheck-%s.txt" % datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))
        with open(fn, "w", encoding="utf-8") as f:
            f.write("\n\n".join(reports) + "\n")
    except OSError:
        pass


# --------------------------------------------------------------------------- Launcher-Selbstupdate
# Eine laufende EXE kann sich unter Windows nicht selbst ersetzen. Darum: neue Version in
# RobloxPortable\Launcher\v<N>\ entpacken und direkt als EXE starten (kein .bat/.ps1, keine Admin-Rechte).
# Beim nächsten Start leitet die ältere EXE automatisch auf die neueste um.
def newest_local_launcher(paths):
    best = (0, None)
    d = os.path.join(paths.base, "Launcher")
    try:
        names = os.listdir(d)
    except OSError:
        return best
    for n in names:
        m = re.fullmatch(r"v(\d+)", n)
        exe = os.path.join(d, n, "RobloxPortable", "RobloxPortable.exe")
        if m and os.path.isfile(exe) and int(m.group(1)) > best[0]:
            best = (int(m.group(1)), exe)
    return best


def launch_launcher(paths, exe):
    subprocess.Popen([exe, "--base", paths.base], cwd=os.path.dirname(exe), shell=False)


def redirect_to_newer(paths):
    """Beim Start: existiert ein neueres, bereits geladenes Launcher-Update? Dann dorthin wechseln."""
    if APP_VERSION <= 0:
        return False
    n, exe = newest_local_launcher(paths)
    if exe and n > APP_VERSION:
        launch_launcher(paths, exe)
        return True
    return False


def check_launcher_update(ctx):
    """Prüft auf ein neues Launcher-Release. Rückgabe: Pfad der neuen EXE oder None."""
    if APP_VERSION <= 0:
        return None
    p = ctx.paths
    with _open(UPDATE_BASE + "/version.json", allowed=UPDATE_HOSTS) as r:
        info = json.loads(r.read().decode("utf-8"))
    try:
        n, sha = int(info["version"]), str(info["sha256"]).lower()
    except (KeyError, ValueError, TypeError):
        raise UserError("Die Launcher-Versionsangabe ist ungültig.")
    if not re.fullmatch(r"[0-9a-f]{64}", sha):
        raise UserError("Die Launcher-Prüfsumme ist ungültig.")
    have, have_exe = newest_local_launcher(p)
    if n <= max(APP_VERSION, have):
        return None
    ctx.log("Neue Launcher-Version %d gefunden (aktuell %d). Lade …" % (n, max(APP_VERSION, have)))
    d = os.path.join(p.base, "Launcher")
    os.makedirs(d, exist_ok=True)
    zpath, tmp, final = (os.path.join(d, "v%d.zip" % n), os.path.join(d, "v%d.tmp" % n), os.path.join(d, "v%d" % n))
    h = hashlib.sha256()
    with _open(UPDATE_BASE + "/RobloxPortable.zip", timeout=60, allowed=UPDATE_HOSTS) as r, open(zpath, "wb") as f:
        for chunk in iter(lambda: r.read(1 << 16), b""):
            ctx.check_cancel()
            h.update(chunk)
            f.write(chunk)
    if h.hexdigest() != sha:
        os.remove(zpath)
        raise UserError("Launcher-Update verworfen: Prüfsumme stimmt nicht.")
    shutil.rmtree(tmp, ignore_errors=True)
    extract_zip(ctx, zpath, tmp, "")
    if not os.path.isfile(os.path.join(tmp, "RobloxPortable", "RobloxPortable.exe")):
        shutil.rmtree(tmp, ignore_errors=True)
        raise UserError("Launcher-Update unvollständig (EXE fehlt).")
    shutil.rmtree(final, ignore_errors=True)
    os.rename(tmp, final)
    os.remove(zpath)
    for name in os.listdir(d):                       # ältere Launcher-Versionen aufräumen
        if re.fullmatch(r"v\d+", name) and name != "v%d" % n:
            shutil.rmtree(os.path.join(d, name), ignore_errors=True)
    ctx.log("Launcher-Update %d installiert." % n)
    return os.path.join(final, "RobloxPortable", "RobloxPortable.exe")


# --------------------------------------------------------------------------- GUI
def run_gui(paths):
    import queue
    import tkinter as tk
    from tkinter import ttk, messagebox

    paths.make()
    q = queue.Queue()
    cancel = threading.Event()
    ctx = Ctx(paths, log=lambda m: q.put(("log", m)),
              progress=lambda d, t, s: q.put(("prog", d, t, s)), cancel=cancel)

    root = tk.Tk()
    root.title("Roblox Portable" + (" (v%d)" % APP_VERSION if APP_VERSION else ""))
    root.geometry("640x480")
    root.minsize(520, 380)
    status = tk.StringVar(value="Bereit.")
    link = tk.StringVar()

    ttk.Label(root, text="Roblox Portable – Downloader & Launcher", font=("Segoe UI", 12, "bold")).pack(pady=(10, 2))
    ttk.Label(root, textvariable=status, wraplength=600).pack(pady=2)
    bar = ttk.Progressbar(root, maximum=100)
    bar.pack(fill="x", padx=14, pady=4)
    row = ttk.Frame(root); row.pack(fill="x", padx=14, pady=4)
    ttk.Label(row, text="Start-Link (optional):").pack(side="left")
    ttk.Entry(row, textvariable=link).pack(side="left", fill="x", expand=True, padx=6)
    btns = ttk.Frame(root); btns.pack(pady=6)
    b_inst = ttk.Button(btns, text="Installieren/Aktualisieren")
    b_start = ttk.Button(btns, text="Starten")
    b_open = ttk.Button(btns, text="Ordner öffnen")
    b_cancel = ttk.Button(btns, text="Abbrechen", state="disabled")
    for b in (b_inst, b_start, b_open, b_cancel):
        b.pack(side="left", padx=4)
    txt = tk.Text(root, height=12, state="disabled", wrap="word")
    txt.pack(fill="both", expand=True, padx=14, pady=(4, 12))

    busy = {"v": False}

    def set_busy(v):
        busy["v"] = v
        b_inst.config(state="disabled" if v else "normal")
        b_start.config(state="disabled" if v else "normal")
        b_cancel.config(state="normal" if v else "disabled")

    def pump():
        try:
            while True:
                m = q.get_nowait()
                if m[0] == "log":
                    txt.config(state="normal"); txt.insert("end", m[1] + "\n"); txt.see("end"); txt.config(state="disabled")
                elif m[0] == "prog":
                    _, d, t, s = m
                    bar["value"] = (100.0 * d / t) if t else 0
                    status.set("%s  (%.0f / %.0f MB)" % (s, d / 2**20, t / 2**20))
                elif m[0] == "status":
                    status.set(m[1])
                elif m[0] == "restart":
                    try:
                        launch_launcher(paths, m[1])
                        root.destroy()
                        return
                    except OSError as e:
                        status.set("Launcher-Update geladen, Neustart blockiert: %s" % e)
                elif m[0] == "busy":
                    set_busy(m[1])
                elif m[0] == "error":
                    status.set("Fehler: " + m[1])
                    messagebox.showerror("Fehler", m[1])
        except queue.Empty:
            pass
        root.after(100, pump)

    def job(auto=False):
        def work():
            q.put(("busy", True))
            cancel.clear()
            try:
                if auto:                                   # erst Launcher selbst, dann Roblox
                    try:
                        newexe = check_launcher_update(ctx)
                    except Exception as e:
                        ctx.log("Launcher-Update-Prüfung nicht möglich: " + explain(e))
                        newexe = None
                    if newexe:
                        q.put(("status", "Launcher aktualisiert – starte neu …"))
                        q.put(("restart", newexe))
                        return
                if auto and not installed_version(paths):
                    q.put(("status", "Roblox ist noch nicht installiert. Bitte „Installieren/Aktualisieren“ klicken."))
                    return
                res = install(ctx)
                q.put(("status", "Roblox ist aktuell." if res == "uptodate" else "Installation/Update abgeschlossen."))
                q.put(("prog", 1, 1, "Fertig"))
            except Cancelled:
                q.put(("status", "Abgebrochen. Der Download kann später fortgesetzt werden."))
            except Exception as e:
                msg = explain(e)
                ctx.log("FEHLER: " + msg)
                if auto and isinstance(e, NET_ERRORS) and installed_version(paths):
                    q.put(("status", "Offline – die installierte Version wird verwendet."))
                else:
                    q.put(("error", msg))
            finally:
                q.put(("busy", False))
        threading.Thread(target=work, daemon=True).start()

    def do_start():
        try:
            before = take_snapshot(paths)
            proc = start_player(ctx, link.get())
        except Exception as e:
            messagebox.showerror("Fehler", explain(e)); return
        status.set("Roblox läuft … (Leck-Check wird im Hintergrund durchgeführt)")
        threading.Thread(target=lambda: monitor_process(ctx, proc, before), daemon=True).start()

    def do_open():
        paths.make()
        try:
            os.startfile(paths.base)  # type: ignore[attr-defined]
        except AttributeError:
            messagebox.showinfo("Ordner", paths.base)

    b_inst.config(command=job)
    b_start.config(command=do_start)
    b_open.config(command=do_open)
    b_cancel.config(command=cancel.set)
    try:
        if os.name == "nt":
            import ctypes
            if ctypes.windll.shell32.IsUserAnAdmin():
                ctx.log("Hinweis: Programm läuft mit Administratorrechten – für diesen Zweck nicht nötig.")
    except Exception:
        pass
    ctx.log("Basisordner: " + paths.base)
    pump()
    root.after(300, lambda: job(auto=True))       # Update-Prüfung beim Start
    root.mainloop()


# --------------------------------------------------------------------------- CLI / main
def run_cli(paths, argv):
    ctx = Ctx(paths, log=print, progress=lambda d, t, s: None)
    cmd = argv[0] if argv else "status"
    try:
        if cmd == "status":
            print("Basis:", paths.base, "| installiert:", installed_version(paths))
        elif cmd == "install":
            print(install(ctx))
        elif cmd == "update-launcher":
            print(check_launcher_update(ctx))
        elif cmd == "start":
            before = take_snapshot(paths)
            proc = start_player(ctx, argv[1] if len(argv) > 1 else "")
            monitor_process(ctx, proc, before)
        else:
            print("Unbekannter Befehl")
            return 2
    except Exception as e:
        print("FEHLER:", explain(e))
        return 1
    return 0


def main():
    argv = sys.argv[1:]
    base = None
    if "--base" in argv:
        i = argv.index("--base"); base = argv[i + 1]; del argv[i:i + 2]
    paths = Paths(base)
    # Eigene Prozess-Temp-Variablen ebenfalls in den Basisordner legen.
    paths.make()
    os.environ["TEMP"] = os.environ["TMP"] = paths.temp
    if argv and argv[0] == "--cli":
        sys.exit(run_cli(paths, argv[1:]))
    if redirect_to_newer(paths):                  # neueres Launcher-Update bereits vorhanden
        sys.exit(0)
    run_gui(paths)


if __name__ == "__main__":
    main()
