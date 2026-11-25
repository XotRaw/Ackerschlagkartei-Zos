#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Mac-Aufräumer – Speicher- und Leistungs-Check für macOS.

Alles läuft LOKAL auf deinem Mac:
  * Es wird keine Internetverbindung aufgebaut, keine Daten verlassen den Rechner.
  * Der eingebaute Mini-Webserver lauscht nur auf 127.0.0.1 (nur dein Mac selbst
    kann ihn erreichen) und verlangt ein zufälliges Zugangs-Token.
  * Gelöscht wird nie endgültig – Dateien wandern in den Papierkorb
    (im Finder per "Zurücklegen" wiederherstellbar).

Benötigt nur Python 3.8+ (Standardbibliothek, keine Zusatzpakete).
Start:  python3 mac_aufraeumer.py
"""

import hashlib
import heapq
import json
import os
import plistlib
import re
import secrets
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

VERSION = "1.0"
HOME = os.path.realpath(os.path.expanduser("~"))
IS_MAC = sys.platform == "darwin"
SF_DATALESS = 0x40000000  # iCloud-Datei, die nur in der Cloud liegt (nicht lokal geladen)
MAX_DIR_DEPTH = 6         # bis zu dieser Tiefe werden Ordnergrößen gespeichert
TOP_N = 600               # so viele größte Dateien werden gemerkt

# --------------------------------------------------------------------------
# Kategorien
# --------------------------------------------------------------------------
CAT_IMG = "Bilder"
CAT_VID = "Videos"
CAT_AUD = "Musik & Audio"
CAT_DOC = "Dokumente"
CAT_ARC = "Archive & Installer"
CAT_APP = "Programme"
CAT_DEV = "Code & Entwicklung"
CAT_LIB = "App-Daten & Caches"
CAT_HID = "Versteckte Ordner"
CAT_TRASH = "Papierkorb"
CAT_OTHER = "Sonstiges"

EXT_GROUPS = {
    CAT_IMG: "jpg jpeg png gif heic heif tif tiff bmp webp raw cr2 cr3 nef arw dng orf rw2 raf psd svg ico icns avif jxl xcf ai eps",
    CAT_VID: "mov mp4 m4v avi mkv wmv flv webm mts m2ts 3gp mpg mpeg vob prores braw insv",
    CAT_AUD: "mp3 m4a aac wav aif aiff flac alac ogg opus wma caf m4r m4b mid midi",
    CAT_DOC: "pdf doc docx xls xlsx ppt pptx odt ods odp rtf txt md pages numbers key csv epub mobi tex log",
    CAT_ARC: "zip rar 7z tar gz tgz bz2 xz dmg pkg mpkg iso img xip sparseimage",
    CAT_APP: "app",
    CAT_DEV: "py js ts jsx tsx java c h cpp hpp m mm swift go rs rb php sh json xml yml yaml sql db sqlite o a dylib so class jar whl ipa",
}
EXT_CAT = {}
for _cat, _exts in EXT_GROUPS.items():
    for _e in _exts.split():
        EXT_CAT["." + _e] = _cat

IMAGE_EXT = {"." + e for e in "jpg jpeg png gif heic heif tif tiff bmp webp dng cr2 cr3 nef arw orf rw2 raf psd".split()}
INSTALLER_EXT = {".dmg", ".pkg", ".mpkg", ".iso", ".xip"}

# Ordner, die macOS wie eine einzelne Datei behandelt ("Pakete")
PACKAGE_EXT = {
    ".app": CAT_APP, ".photoslibrary": CAT_IMG, ".photolibrary": CAT_IMG, ".aplibrary": CAT_IMG,
    ".lrlibrary": CAT_IMG, ".lrdata": CAT_IMG, ".musiclibrary": CAT_AUD, ".tvlibrary": CAT_VID,
    ".imovielibrary": CAT_VID, ".fcpbundle": CAT_VID, ".logicx": CAT_AUD, ".band": CAT_AUD,
    ".pages": CAT_DOC, ".numbers": CAT_DOC, ".key": CAT_DOC, ".rtfd": CAT_DOC,
    ".sparsebundle": CAT_ARC, ".xcarchive": CAT_DEV, ".xcodeproj": CAT_DEV, ".xcworkspace": CAT_DEV,
    ".framework": CAT_DEV, ".bundle": CAT_OTHER, ".plugin": CAT_OTHER, ".pvm": CAT_OTHER,
    ".vmwarevm": CAT_OTHER, ".utm": CAT_OTHER, ".dtbase2": CAT_DOC,
}


def zone_for_top(name):
    """Zuordnung der obersten Ordner im Benutzerordner."""
    if name == "Library":
        return CAT_LIB
    if name == ".Trash":
        return CAT_TRASH
    if name.startswith("."):
        return CAT_HID
    return None


def run(cmd, timeout=20):
    """Lokales Programm ausführen, Fehler still schlucken."""
    try:
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
        return r.stdout.decode("utf-8", "replace")
    except Exception:
        return ""


# --------------------------------------------------------------------------
# Ähnliche Bilder: dHash über "sips" (in macOS eingebaut)
# --------------------------------------------------------------------------
def bmp_to_gray(data):
    """Liest eine unkomprimierte BMP-Datei und liefert Graustufen-Zeilen."""
    if data[:2] != b"BM":
        raise ValueError("keine BMP")
    off = struct.unpack_from("<I", data, 10)[0]
    dib = struct.unpack_from("<I", data, 14)[0]
    w = struct.unpack_from("<i", data, 18)[0]
    h = struct.unpack_from("<i", data, 22)[0]
    bpp = struct.unpack_from("<H", data, 28)[0]
    comp = struct.unpack_from("<I", data, 30)[0]
    if comp not in (0, 3):
        raise ValueError("komprimierte BMP")
    bottom_up = h > 0
    h = abs(h)
    palette = None
    if bpp == 8:
        pstart = 14 + dib
        ncol = (off - pstart) // 4
        palette = [(data[pstart + i * 4 + 2], data[pstart + i * 4 + 1], data[pstart + i * 4]) for i in range(ncol)]
    elif bpp not in (24, 32):
        raise ValueError("BMP-Format %d bit" % bpp)
    rowsize = ((bpp * w + 31) // 32) * 4
    rows = []
    for y in range(h):
        ry = h - 1 - y if bottom_up else y
        base = off + ry * rowsize
        row = []
        for x in range(w):
            if palette is not None:
                r, g, b = palette[data[base + x]]
            else:
                i = base + x * (bpp // 8)
                b, g, r = data[i], data[i + 1], data[i + 2]
            row.append(0.299 * r + 0.587 * g + 0.114 * b)
        rows.append(row)
    return rows


def dhash_from_rows(rows):
    """64-Bit-Differenz-Hash aus 8 Zeilen x 9 Spalten."""
    v = 0
    for r in range(8):
        row = rows[r]
        for c in range(8):
            v = (v << 1) | (1 if row[c] > row[c + 1] else 0)
    return v


def popcount(x):
    return bin(x).count("1")


# --------------------------------------------------------------------------
# Scanner
# --------------------------------------------------------------------------
class Scanner:
    def __init__(self):
        self.lock = threading.Lock()
        self.thread = None
        self.status = {"phase": "idle", "message": "Noch kein Scan gestartet."}
        self.results = None
        self.cancel = False
        self.deleted = set()
        self.tmpdir = tempfile.mkdtemp(prefix="mac-aufraeumer-")
        self._reset()

    def _reset(self):
        self.dir_sizes = {}
        self.dir_children = {}
        self.dir_files = {}
        self.pkg_dirs = set()
        self.cat = {}
        self.top = []
        self._ctr = 0
        self.size_groups = {}
        self.images = []
        self.installers = []
        self.node_modules = []
        self.seen_inodes = set()
        self.files_scanned = 0
        self.bytes_scanned = 0
        self.perm_errors = 0
        self.perm_paths = []
        self._last_status = 0.0

    # ---------------- öffentliche Steuerung ----------------
    def start(self, opts):
        if self.thread and self.thread.is_alive():
            return False
        self.cancel = False
        self.thread = threading.Thread(target=self._run, args=(opts,), daemon=True)
        self.thread.start()
        return True

    def set_status(self, **kw):
        with self.lock:
            self.status.update(kw)

    def get_status(self):
        with self.lock:
            s = dict(self.status)
        s["files"] = self.files_scanned
        s["bytes"] = self.bytes_scanned
        s["running"] = bool(self.thread and self.thread.is_alive())
        s["has_results"] = self.results is not None
        return s

    # ---------------- Hauptablauf ----------------
    def _run(self, opts):
        t0 = time.time()
        try:
            self._reset()
            self.deleted = set()
            root = os.path.realpath(os.path.expanduser(opts.get("root") or HOME))
            if not os.path.isdir(root):
                self.set_status(phase="error", message="Ordner nicht gefunden: " + root)
                return
            self.root = root
            self.root_is_home = root == HOME
            self.include_library = bool(opts.get("include_library"))
            self.min_dup = max(1, int(opts.get("min_dup_kb") or 100)) * 1024
            self.root_dev = os.stat(root).st_dev
            self.skip_dirs = {"/Volumes", "/System/Volumes", "/dev", "/proc", "/sys", "/private/var/vm", "/cores"}

            self.set_status(phase="scan", message="Durchsuche " + root, progress=None)
            total = self._walk(root, 0, None, False)
            if self.cancel:
                self.set_status(phase="cancelled", message="Scan abgebrochen.")
                return

            self.set_status(phase="apps", message="Analysiere Programme …", progress=None)
            apps = self._scan_apps()

            dups = self._find_duplicates() if not self.cancel else []
            similar = []
            if opts.get("similar") and IS_MAC and not self.cancel:
                similar = self._find_similar()
            if self.cancel:
                self.set_status(phase="cancelled", message="Scan abgebrochen.")
                return

            self.set_status(phase="tips", message="Suche Aufräum-Möglichkeiten …", progress=None)
            tips = self._tips()

            top = sorted(self.top, reverse=True)
            self.results = {
                "root": root,
                "root_is_home": self.root_is_home,
                "total": total,
                "files": self.files_scanned,
                "duration": round(time.time() - t0, 1),
                "perm_errors": self.perm_errors,
                "perm_paths": self.perm_paths[:15],
                "categories": sorted(
                    [{"name": k, "size": v[0], "count": v[1]} for k, v in self.cat.items()],
                    key=lambda x: -x["size"]),
                "top_files": [it for _, _, it in top],
                "apps": apps,
                "dups": dups,
                "similar": similar,
                "similar_enabled": bool(opts.get("similar")),
                "tips": tips,
                "installers": sorted(self.installers, key=lambda x: -x["s"])[:200],
                "scanned_at": time.time(),
            }
            self.set_status(phase="done", message="Fertig in %.0f Sekunden." % (time.time() - t0), progress=None)
        except Exception as e:  # pragma: no cover
            import traceback
            traceback.print_exc()
            self.set_status(phase="error", message="Fehler: %s" % e)

    # ---------------- Dateisystem durchlaufen ----------------
    def _tick(self, path):
        now = time.time()
        if now - self._last_status > 0.25:
            self._last_status = now
            self.set_status(message=path)

    def _record_dir(self, path, depth, size, direct=0, direct_n=0, pkg=False):
        if depth <= MAX_DIR_DEPTH:
            self.dir_sizes[path] = size
            self.dir_files[path] = (direct, direct_n)
            if pkg:
                self.pkg_dirs.add(path)
            if depth > 0:
                self.dir_children.setdefault(os.path.dirname(path), []).append(path)

    def _add_top(self, size, path, mtime, cat, pkg=False):
        self._ctr += 1
        item = (size, self._ctr, {"p": path, "s": size, "m": int(mtime), "c": cat, "pkg": pkg})
        if len(self.top) < TOP_N:
            heapq.heappush(self.top, item)
        elif size > self.top[0][0]:
            heapq.heappushpop(self.top, item)

    def _add_cat(self, cat, size, n):
        c = self.cat.get(cat)
        if c is None:
            self.cat[cat] = [size, n]
        else:
            c[0] += size
            c[1] += n

    def _dup_zone_ok(self, zone):
        if zone is None:
            return True
        if zone == CAT_TRASH:
            return False
        return self.include_library

    def _dirsize(self, path, seen=None):
        """Größe eines Ordners (inkl. Unterordner) – für Pakete wie .app."""
        if seen is None:
            seen = self.seen_inodes
        total = 0
        n = 0
        stack = [path]
        while stack:
            if self.cancel:
                break
            d = stack.pop()
            try:
                it = os.scandir(d)
            except OSError:
                continue
            with it:
                for e in it:
                    try:
                        st = e.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if stat.S_ISDIR(st.st_mode):
                        stack.append(e.path)
                    elif stat.S_ISREG(st.st_mode):
                        if st.st_nlink > 1:
                            key = (st.st_dev, st.st_ino)
                            if key in seen:
                                continue
                            seen.add(key)
                        total += st.st_blocks * 512
                        n += 1
        return total, n

    def _walk(self, path, depth, zone, in_nm):
        total = 0
        direct = 0
        direct_n = 0
        try:
            it = os.scandir(path)
        except PermissionError:
            self.perm_errors += 1
            if len(self.perm_paths) < 50:
                self.perm_paths.append(path)
            return 0
        except OSError:
            return 0
        self._tick(path)
        with it:
            for e in it:
                if self.cancel:
                    break
                try:
                    st = e.stat(follow_symlinks=False)
                except OSError:
                    continue
                mode = st.st_mode
                name = e.name
                if stat.S_ISDIR(mode):
                    if st.st_dev != self.root_dev or e.path in self.skip_dirs:
                        continue
                    z = zone
                    if depth == 0 and self.root_is_home:
                        z = zone_for_top(name)
                    ext = os.path.splitext(name)[1].lower()
                    if ext in PACKAGE_EXT:
                        sz, n = self._dirsize(e.path)
                        cat = z or PACKAGE_EXT[ext]
                        self._add_cat(cat, sz, 1)
                        self._add_top(sz, e.path, st.st_mtime, cat, True)
                        self._record_dir(e.path, depth + 1, sz, sz, n, pkg=True)
                        self.files_scanned += n
                        self.bytes_scanned += sz
                        total += sz
                        continue
                    is_nm = name == "node_modules"
                    sz = self._walk(e.path, depth + 1, z, in_nm or is_nm)
                    if is_nm and not in_nm and sz > 0:
                        self.node_modules.append({"p": e.path, "s": sz})
                    total += sz
                elif stat.S_ISREG(mode):
                    if st.st_nlink > 1:
                        key = (st.st_dev, st.st_ino)
                        if key in self.seen_inodes:
                            continue
                        self.seen_inodes.add(key)
                    du = st.st_blocks * 512
                    total += du
                    direct += du
                    direct_n += 1
                    self.files_scanned += 1
                    self.bytes_scanned += du
                    ext = os.path.splitext(name)[1].lower()
                    z = zone
                    if depth == 0 and self.root_is_home and name.startswith("."):
                        z = CAT_HID
                    cat = z or EXT_CAT.get(ext, CAT_OTHER)
                    self._add_cat(cat, du, 1)
                    self._add_top(du, e.path, st.st_mtime, cat)
                    if ext in INSTALLER_EXT and du >= 1024 * 1024 and z != CAT_TRASH:
                        self.installers.append({"p": e.path, "s": du, "m": int(st.st_mtime), "c": CAT_ARC, "pkg": False})
                    dataless = (getattr(st, "st_flags", 0) & SF_DATALESS) or (st.st_blocks == 0 and st.st_size > 0)
                    if not dataless and not in_nm and self._dup_zone_ok(z):
                        if st.st_size >= self.min_dup:
                            self.size_groups.setdefault(st.st_size, []).append((e.path, int(st.st_mtime)))
                        if ext in IMAGE_EXT and st.st_size >= 20 * 1024:
                            self.images.append((e.path, st.st_size))
        self._record_dir(path, depth, total, direct, direct_n)
        return total

    # ---------------- Programme ----------------
    def _lookup_size(self, path):
        if path in self.dir_sizes:
            return self.dir_sizes[path]
        if os.path.isdir(path):
            return self._dirsize(path, seen=set())[0]
        try:
            return os.lstat(path).st_blocks * 512
        except OSError:
            return 0

    def _scan_apps(self):
        cands = []
        for r in ("/Applications", os.path.join(HOME, "Applications")):
            try:
                entries = list(os.scandir(r))
            except OSError:
                continue
            for e in entries:
                try:
                    if e.is_symlink():
                        continue
                    if e.name.endswith(".app") and e.is_dir():
                        cands.append(e.path)
                    elif e.is_dir():
                        for e2 in os.scandir(e.path):
                            if e2.name.endswith(".app") and e2.is_dir(follow_symlinks=False):
                                cands.append(e2.path)
                except OSError:
                    continue

        lib = os.path.join(HOME, "Library")

        def one(app):
            info = {}
            try:
                with open(os.path.join(app, "Contents", "Info.plist"), "rb") as f:
                    info = plistlib.load(f)
            except Exception:
                pass
            bid = str(info.get("CFBundleIdentifier") or "")
            name = str(info.get("CFBundleDisplayName") or info.get("CFBundleName") or os.path.basename(app)[:-4])
            size = self.dir_sizes.get(app)
            if size is None:
                size = self._dirsize(app, seen=set())[0]
            last = None
            if IS_MAC:
                out = run(["mdls", "-name", "kMDItemLastUsedDate", "-raw", app], timeout=10).strip()
                m = re.match(r"(\d{4})-(\d\d)-(\d\d) (\d\d):(\d\d):(\d\d)", out)
                if m:
                    import calendar
                    last = calendar.timegm(tuple(int(x) for x in m.groups()) + (0, 0, 0))
            related = []
            if bid and not bid.startswith("com.apple."):
                names = {bid, name, os.path.basename(app)[:-4]}
                cand = []
                for n in names:
                    cand += [os.path.join(lib, "Application Support", n), os.path.join(lib, "Caches", n),
                             os.path.join(lib, "Logs", n)]
                cand += [os.path.join(lib, "Containers", bid), os.path.join(lib, "Preferences", bid + ".plist"),
                         os.path.join(lib, "Saved Application State", bid + ".savedState"),
                         os.path.join(lib, "HTTPStorages", bid), os.path.join(lib, "WebKit", bid)]
                seen_p = set()
                for p in cand:
                    if p in seen_p or not os.path.lexists(p):
                        continue
                    seen_p.add(p)
                    related.append({"p": p, "s": self._lookup_size(p)})
            return {
                "p": app, "name": name, "id": bid, "s": size,
                "version": str(info.get("CFBundleShortVersionString") or ""),
                "store": os.path.exists(os.path.join(app, "Contents", "_MASReceipt")),
                "apple": bid.startswith("com.apple."),
                "last": last, "related": related,
                "rs": sum(r["s"] for r in related),
            }

        apps = []
        with ThreadPoolExecutor(max_workers=6) as ex:
            futs = [ex.submit(one, a) for a in cands]
            for i, f in enumerate(as_completed(futs)):
                if self.cancel:
                    break
                try:
                    apps.append(f.result())
                except Exception:
                    pass
                self.set_status(message="Programme: %d / %d" % (i + 1, len(cands)), progress=(i + 1) / max(1, len(cands)))
        apps.sort(key=lambda a: -(a["s"] + a["rs"]))
        return apps

    # ---------------- Duplikate ----------------
    def _hash(self, path, limit=None):
        h = hashlib.blake2b(digest_size=16)
        read = 0
        with open(path, "rb") as f:
            while True:
                if self.cancel:
                    raise RuntimeError("abgebrochen")
                n = 1024 * 1024 if limit is None else min(1024 * 1024, limit - read)
                if n <= 0:
                    break
                b = f.read(n)
                if not b:
                    break
                h.update(b)
                read += len(b)
        return h.hexdigest()

    def _find_duplicates(self):
        groups = [(s, l) for s, l in self.size_groups.items() if len(l) > 1]
        work = sum(s * len(l) for s, l in groups)
        self.set_status(phase="dups", message="Suche doppelte Dateien …", progress=0.0)
        done = [0]
        PART = 64 * 1024

        def prog(nbytes, path):
            done[0] += nbytes
            self._tick(path)
            with self.lock:
                self.status["progress"] = min(1.0, done[0] / max(1, work))

        result = []
        for size, files in sorted(groups, key=lambda g: -g[0]):
            if self.cancel:
                break
            # 1. Stufe: Anfang der Datei vergleichen
            by_part = {}
            for p, m in files:
                try:
                    k = self._hash(p, PART)
                except Exception:
                    continue
                by_part.setdefault(k, []).append((p, m))
                prog(min(size, PART), p)
            # 2. Stufe: komplette Datei vergleichen
            for sub in by_part.values():
                if len(sub) < 2:
                    continue
                if size <= PART:
                    final = {"x": sub}
                else:
                    final = {}
                    for p, m in sub:
                        try:
                            k = self._hash(p)
                        except Exception:
                            continue
                        final.setdefault(k, []).append((p, m))
                        prog(size - PART, p)
                for same in final.values():
                    if len(same) > 1:
                        ext = os.path.splitext(same[0][0])[1].lower()
                        result.append({
                            "size": size,
                            "c": EXT_CAT.get(ext, CAT_OTHER),
                            "waste": size * (len(same) - 1),
                            "files": sorted([{"p": p, "m": m} for p, m in same], key=lambda x: x["m"]),
                        })
        result.sort(key=lambda g: -g["waste"])
        return result[:3000]

    # ---------------- Ähnliche Bilder ----------------
    def _find_similar(self, threshold=6):
        imgs = self.images[:25000]
        self.set_status(phase="similar", message="Vergleiche Bilder …", progress=0.0)
        tmp = tempfile.mkdtemp(prefix="hash-", dir=self.tmpdir)

        def h(p):
            out = os.path.join(tmp, hashlib.sha1(p.encode("utf-8", "replace")).hexdigest() + ".bmp")
            try:
                subprocess.run(["sips", "-s", "format", "bmp", "-z", "8", "9", p, "--out", out],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
                with open(out, "rb") as f:
                    data = f.read()
                return dhash_from_rows(bmp_to_gray(data))
            except Exception:
                return None
            finally:
                try:
                    os.remove(out)
                except OSError:
                    pass

        hashes = []
        with ThreadPoolExecutor(max_workers=max(2, min(8, (os.cpu_count() or 4)))) as ex:
            futs = {ex.submit(h, p): (p, s) for p, s in imgs}
            for i, f in enumerate(as_completed(futs)):
                if self.cancel:
                    for x in futs:
                        x.cancel()
                    break
                v = f.result()
                p, s = futs[f]
                if v is not None and v != 0 and v != (1 << 64) - 1:
                    hashes.append((p, s, v))
                if i % 20 == 0:
                    self.set_status(message=p, progress=(i + 1) / max(1, len(imgs)))
        shutil.rmtree(tmp, ignore_errors=True)
        return group_similar(hashes, threshold)

    # ---------------- Aufräum-Tipps ----------------
    def _children_sizes(self, path, limit=12):
        out = []
        kids = self.dir_children.get(path)
        if kids is not None:
            for k in kids:
                out.append({"p": k, "s": self.dir_sizes.get(k, 0)})
        else:
            try:
                for e in os.scandir(path):
                    if e.is_symlink():
                        continue
                    out.append({"p": e.path, "s": self._lookup_size(e.path)})
            except OSError:
                pass
        out.sort(key=lambda x: -x["s"])
        return out[:limit]

    def _tips(self):
        L = os.path.join(HOME, "Library")
        tips = []

        def add(tid, title, path, text, kind="info", children=False, cmd=None, size=None, items=None, min_size=0):
            if path and not os.path.exists(path):
                return
            s = size if size is not None else (self._lookup_size(path) if path else 0)
            if s < min_size:
                return
            t = {"id": tid, "title": title, "p": path, "s": s, "text": text, "kind": kind, "cmd": cmd}
            if children and path:
                t["items"] = self._children_sizes(path)
            if items is not None:
                t["items"] = items
            tips.append(t)

        add("trash", "Papierkorb", os.path.join(HOME, ".Trash"),
            "Dateien im Papierkorb belegen weiterhin Platz, bis du ihn leerst.", kind="trash")
        add("downloads", "Downloads-Ordner", os.path.join(HOME, "Downloads"),
            "Hier sammeln sich oft alte Installer, ZIPs und doppelte Dateien. Unten die größten Einträge.",
            kind="items", children=True)
        if self.installers:
            add("installers", "Installationsdateien (.dmg / .pkg / .iso)", None,
                "Nach der Installation eines Programms wird die Installationsdatei nicht mehr gebraucht.",
                kind="items", size=sum(i["s"] for i in self.installers), items=self.installers[:15])
        add("caches", "App-Caches (~/Library/Caches)", os.path.join(L, "Caches"),
            "Zwischenspeicher von Programmen. Kann gelöscht werden – die Apps legen ihn bei Bedarf neu an. "
            "Am besten vorher die betroffene App beenden. Nach dem Löschen kann eine App beim ersten Start etwas langsamer sein.",
            kind="items", children=True, min_size=50 * 1024 * 1024)
        add("logs", "Protokolldateien (~/Library/Logs)", os.path.join(L, "Logs"),
            "Log-Dateien von Programmen. Werden meist nicht mehr gebraucht.",
            kind="items", children=True, min_size=50 * 1024 * 1024)
        add("ios-backup", "iPhone-/iPad-Backups", os.path.join(L, "Application Support", "MobileSync", "Backup"),
            "Lokale Geräte-Backups können sehr groß sein. Verwalte sie am sichersten im Finder: iPhone in der "
            "Seitenleiste wählen → „Backups verwalten…“. Lösche nur Backups von Geräten, die du nicht mehr hast "
            "oder die per iCloud gesichert sind.", kind="reveal", min_size=1)
        add("xcode-derived", "Xcode DerivedData", os.path.join(L, "Developer", "Xcode", "DerivedData"),
            "Build-Zwischenergebnisse von Xcode. Kann komplett gelöscht werden.", kind="items", children=True, min_size=1)
        add("xcode-archives", "Xcode Archives", os.path.join(L, "Developer", "Xcode", "Archives"),
            "Alte App-Archive. Nur behalten, was du noch für Symbolisierung/Releases brauchst.", kind="items", children=True, min_size=1)
        add("xcode-devsupport", "Xcode iOS DeviceSupport", os.path.join(L, "Developer", "Xcode", "iOS DeviceSupport"),
            "Debug-Symbole für ältere iOS-Versionen. Alte Versionen können weg.", kind="items", children=True, min_size=1)
        add("simulators", "iOS-Simulatoren", os.path.join(L, "Developer", "CoreSimulator"),
            "Simulator-Geräte. Nicht mehr verfügbare löschst du sicher mit:", kind="cmd",
            cmd="xcrun simctl delete unavailable", min_size=500 * 1024 * 1024)
        add("mail-dl", "Mail-Anhänge (Mail Downloads)",
            os.path.join(L, "Containers", "com.apple.mail", "Data", "Library", "Mail Downloads"),
            "Geöffnete Mail-Anhänge werden hier zwischengespeichert und können gelöscht werden.",
            kind="items", children=True, min_size=10 * 1024 * 1024)
        add("mail", "Apple Mail Postfächer", os.path.join(L, "Mail"),
            "Mails mit großen Anhängen belegen viel Platz. In Mail: große Mails suchen und Anhänge entfernen "
            "(E-Mail → Anhänge entfernen). Diesen Ordner NICHT manuell löschen.", kind="info", min_size=500 * 1024 * 1024)
        add("brew", "Homebrew-Cache", os.path.join(L, "Caches", "Homebrew"),
            "Heruntergeladene Pakete von Homebrew. Aufräumen mit:", kind="cmd", cmd="brew cleanup --prune=all", min_size=1)
        for p, t, c in ((os.path.join(HOME, ".npm"), "npm-Cache", "npm cache clean --force"),
                        (os.path.join(L, "Caches", "Yarn"), "Yarn-Cache", "yarn cache clean"),
                        (os.path.join(L, "Caches", "pip"), "pip-Cache", "pip cache purge"),
                        (os.path.join(HOME, ".gradle", "caches"), "Gradle-Cache", "rm -rf ~/.gradle/caches"),
                        (os.path.join(L, "Caches", "CocoaPods"), "CocoaPods-Cache", "pod cache clean --all")):
            add("dev-" + t, t, p, "Entwickler-Cache, wird bei Bedarf neu geladen. Aufräumen mit:", kind="cmd", cmd=c,
                min_size=100 * 1024 * 1024)
        add("docker", "Docker-Daten", os.path.join(L, "Containers", "com.docker.docker"),
            "Docker-Images und Container. Ungenutzte Daten entfernen mit:", kind="cmd",
            cmd="docker system prune -a", min_size=1024 * 1024 * 1024)
        if self.node_modules:
            nm = sorted(self.node_modules, key=lambda x: -x["s"])
            add("node_modules", "node_modules-Ordner (JavaScript-Projekte)", None,
                "Abhängigkeiten von JavaScript-Projekten. Lassen sich jederzeit mit „npm install“ wiederherstellen – "
                "bei alten Projekten löschen.", kind="items", size=sum(x["s"] for x in nm), items=nm[:15],
                min_size=200 * 1024 * 1024)
        for p in ("/Library/Application Support/GarageBand", "/Library/Application Support/Logic",
                  "/Library/Audio/Apple Loops"):
            add("gb-" + p, "Musik-Sounds: " + os.path.basename(p), p,
                "Zusatz-Sounds von GarageBand/Logic. Wenn du die Apps nicht nutzt: App löschen und diesen Ordner "
                "(braucht Admin-Passwort) im Finder entfernen.", kind="reveal", min_size=500 * 1024 * 1024)
        # Fotos-Mediathek
        photos = [it for _, _, it in self.top if it["p"].endswith(".photoslibrary")]
        for it in photos:
            add("photos-" + it["p"], "Fotos-Mediathek", it["p"],
                "Die Fotos-App findet Duplikate selbst: Fotos → Seitenleiste „Duplikate“. Mit iCloud-Fotos kannst "
                "du unter Fotos → Einstellungen → iCloud „Mac-Speicher optimieren“ wählen – dann bleiben nur "
                "kleine Vorschauen lokal.", kind="reveal", size=it["s"])
        if IS_MAC:
            out = run(["tmutil", "listlocalsnapshots", "/"])
            snaps = [l for l in out.splitlines() if "com.apple" in l]
            if snaps:
                tips.append({"id": "snapshots", "title": "Lokale Time-Machine-Schnappschüsse", "p": None, "s": 0,
                             "kind": "cmd", "cmd": "tmutil thinlocalsnapshots / 999999999999 4",
                             "text": "%d lokale Schnappschüsse gefunden. macOS löscht sie automatisch, wenn Platz knapp "
                                     "wird – sie können aber die Anzeige „belegt“ aufblähen. Manuell ausdünnen mit:" % len(snaps)})
        tips.append({"id": "icloud", "title": "iCloud Drive: Mac-Speicher optimieren", "p": None, "s": 0, "kind": "settings",
                     "text": "Systemeinstellungen → Apple-ID → iCloud → iCloud Drive → „Mac-Speicher optimieren“. "
                             "Selten genutzte Dateien bleiben dann nur in der Cloud.", "open": "icloud"})
        tips.append({"id": "storage", "title": "macOS-Speicherverwaltung", "p": None, "s": 0, "kind": "settings",
                     "text": "Apples eigene Übersicht: Systemeinstellungen → Allgemein → Speicher. Dort kannst du u. a. "
                             "„Papierkorb automatisch leeren“ aktivieren.", "open": "storage"})
        tips.sort(key=lambda t: -t["s"])
        return tips


def group_similar(hashes, threshold=6):
    """Gruppiert Bilder mit ähnlichem Hash (Hamming-Distanz <= threshold).
    Schublade-Prinzip: Bei <= 7 abweichenden Bits stimmt mindestens eines der 8 Bytes überein."""
    n = len(hashes)
    parent = list(range(n))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    for chunk in range(8):
        buckets = {}
        shift = chunk * 8
        for i, (_, _, v) in enumerate(hashes):
            buckets.setdefault((v >> shift) & 0xFF, []).append(i)
        for idx in buckets.values():
            if len(idx) < 2 or len(idx) > 3000:
                continue
            for a in range(len(idx)):
                va = hashes[idx[a]][2]
                for b in range(a + 1, len(idx)):
                    if popcount(va ^ hashes[idx[b]][2]) <= threshold:
                        ra, rb = find(idx[a]), find(idx[b])
                        if ra != rb:
                            parent[ra] = rb
    groups = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    out = []
    for idx in groups.values():
        if len(idx) < 2 or len(idx) > 50:
            continue
        files = sorted(({"p": hashes[i][0], "s": hashes[i][1]} for i in idx), key=lambda x: -x["s"])
        out.append({"files": files, "waste": sum(f["s"] for f in files[1:])})
    out.sort(key=lambda g: -g["waste"])
    return out[:1500]


# --------------------------------------------------------------------------
# System / Leistung
# --------------------------------------------------------------------------
def system_info():
    info = {"mac": IS_MAC}
    data_vol = "/System/Volumes/Data" if os.path.isdir("/System/Volumes/Data") else "/"
    try:
        du = shutil.disk_usage(data_vol)
        info["disk"] = {"total": du.total, "used": du.total - du.free, "free": du.free}
    except OSError:
        pass
    info["cpus"] = os.cpu_count()
    try:
        info["load"] = os.getloadavg()
    except OSError:
        info["load"] = None
    if IS_MAC:
        info["os"] = run(["sw_vers", "-productVersion"]).strip()
        try:
            info["mem_total"] = int(run(["sysctl", "-n", "hw.memsize"]).strip())
        except ValueError:
            pass
        vm = run(["vm_stat"])
        m = re.search(r"page size of (\d+) bytes", vm)
        ps = int(m.group(1)) if m else 4096
        pages = {}
        for line in vm.splitlines()[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                v = v.strip().rstrip(".")
                if v.isdigit():
                    pages[k.strip()] = int(v) * ps
        info["mem"] = {
            "wired": pages.get("Pages wired down", 0),
            "active": pages.get("Pages active", 0),
            "inactive": pages.get("Pages inactive", 0),
            "compressed": pages.get("Pages occupied by compressor", 0),
            "free": pages.get("Pages free", 0) + pages.get("Pages speculative", 0),
            "purgeable": pages.get("Pages purgeable", 0),
            "file_backed": pages.get("File-backed pages", 0),
        }
        sw = run(["sysctl", "-n", "vm.swapusage"])
        m = re.search(r"used = ([\d.,]+)M", sw)
        if m:
            info["swap_used"] = int(float(m.group(1).replace(",", ".")) * 1024 * 1024)
        mp = run(["memory_pressure", "-Q"], timeout=10)
        m = re.search(r"free percentage:\s*(\d+)%", mp)
        if m:
            info["mem_free_pct"] = int(m.group(1))
        bt = run(["sysctl", "-n", "kern.boottime"])
        m = re.search(r"sec = (\d+)", bt)
        if m:
            info["uptime"] = time.time() - int(m.group(1))
    else:
        try:
            with open("/proc/meminfo") as f:
                mi = {l.split(":")[0]: int(l.split()[1]) * 1024 for l in f if len(l.split()) >= 2}
            info["mem_total"] = mi.get("MemTotal")
        except Exception:
            pass

    # Prozesse – nach App zusammengefasst
    procs = []
    out = run(["ps", "-Ao", "pid=,pcpu=,rss=,comm="])
    for line in out.splitlines():
        parts = line.strip().split(None, 3)
        if len(parts) < 4:
            continue
        try:
            pid, cpu, rss = int(parts[0]), float(parts[1].replace(",", ".")), int(parts[2]) * 1024
        except ValueError:
            continue
        comm = parts[3]
        m = re.search(r"/([^/]+)\.app/", comm)
        app = m.group(1) if m else os.path.basename(comm)
        procs.append({"pid": pid, "cpu": cpu, "rss": rss, "name": os.path.basename(comm), "app": app})
    apps = {}
    for p in procs:
        a = apps.setdefault(p["app"], {"app": p["app"], "cpu": 0.0, "rss": 0, "n": 0})
        a["cpu"] += p["cpu"]
        a["rss"] += p["rss"]
        a["n"] += 1
    al = list(apps.values())
    info["by_cpu"] = sorted(al, key=lambda a: -a["cpu"])[:15]
    info["by_mem"] = sorted(al, key=lambda a: -a["rss"])[:15]
    info["nproc"] = len(procs)

    # Hintergrund-/Startobjekte
    agents = []
    for d, scope in ((os.path.join(HOME, "Library", "LaunchAgents"), "Benutzer"),
                     ("/Library/LaunchAgents", "Alle Benutzer"), ("/Library/LaunchDaemons", "System-Dienst")):
        try:
            names = sorted(os.listdir(d))
        except OSError:
            continue
        for n in names:
            if not n.endswith(".plist"):
                continue
            p = os.path.join(d, n)
            item = {"p": p, "scope": scope, "label": n[:-6], "prog": ""}
            try:
                with open(p, "rb") as f:
                    pl = plistlib.load(f)
                item["label"] = str(pl.get("Label") or item["label"])
                prog = pl.get("Program") or (pl.get("ProgramArguments") or [""])[0]
                item["prog"] = str(prog)
                item["disabled"] = bool(pl.get("Disabled"))
            except Exception:
                pass
            agents.append(item)
    info["agents"] = agents
    return info


# --------------------------------------------------------------------------
# Dateiaktionen
# --------------------------------------------------------------------------
PROTECTED = {HOME, "/Applications"} | {os.path.join(HOME, d) for d in (
    "Library", "Desktop", "Documents", "Downloads", "Pictures", "Movies", "Music", "Applications", ".Trash",
    "Public", "Library/Mobile Documents", "Library/CloudStorage")}


def path_allowed(p):
    p = os.path.abspath(p)
    if p in PROTECTED:
        return False
    if not (p.startswith(HOME + os.sep) or p.startswith("/Applications/")):
        return False
    # ~/Library/<X> selbst schützen, Inhalte darin sind erlaubt
    lib = os.path.join(HOME, "Library") + os.sep
    if p.startswith(lib) and os.sep not in p[len(lib):]:
        return False
    return True


TRASH_SCRIPT = """on run argv
  repeat with p in argv
    try
      tell application "Finder" to delete ((POSIX file (contents of p)) as alias)
    end try
  end repeat
end run"""


def move_to_trash(paths):
    ok, failed = [], []
    todo = []
    for p in paths:
        p = os.path.abspath(p)
        if not path_allowed(p):
            failed.append({"p": p, "err": "Geschützter Ort – wird aus Sicherheitsgründen nicht gelöscht."})
        elif not os.path.lexists(p):
            ok.append(p)
        else:
            todo.append(p)
    if IS_MAC and todo:
        for i in range(0, len(todo), 100):
            run(["osascript", "-e", TRASH_SCRIPT] + todo[i:i + 100], timeout=600)
    trash = os.path.join(HOME, ".Trash")
    for p in todo:
        if not os.path.lexists(p):
            ok.append(p)
            continue
        try:  # Ersatzweg: direkt in den Papierkorb-Ordner verschieben
            os.makedirs(trash, exist_ok=True)
            base = os.path.basename(p)
            dest = os.path.join(trash, base)
            k = 1
            while os.path.lexists(dest):
                stem, ext = os.path.splitext(base)
                dest = os.path.join(trash, "%s %d%s" % (stem, k, ext))
                k += 1
            shutil.move(p, dest)
            ok.append(p)
        except Exception as e:
            failed.append({"p": p, "err": str(e)})
    return ok, failed


# --------------------------------------------------------------------------
# Webserver (nur 127.0.0.1)
# --------------------------------------------------------------------------
SCANNER = Scanner()
TOKEN = secrets.token_urlsafe(24)
HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "oberflaeche.html")


class Handler(BaseHTTPRequestHandler):
    server_version = "MacAufraeumer/" + VERSION

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _host_ok(self):
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost")

    def _auth(self, q):
        t = self.headers.get("X-Token") or (q.get("t") or [""])[0]
        return secrets.compare_digest(t, TOKEN)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > 5_000_000:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if not self._host_ok() or not self._auth(q):
            return self._send(403, "Zugriff verweigert. Bitte den Link aus dem Terminal verwenden.", "text/plain; charset=utf-8")
        if u.path in ("/", "/index.html"):
            with open(HTML_PATH, "rb") as f:
                csp = ("default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
                       "script-src 'self' 'unsafe-inline'; connect-src 'self'")
                return self._send(200, f.read(), "text/html; charset=utf-8", {"Content-Security-Policy": csp})
        if u.path == "/api/status":
            return self._send(200, SCANNER.get_status())
        if u.path == "/api/results":
            r = SCANNER.results
            if r is None:
                return self._send(200, {})
            return self._send(200, filter_deleted(r, SCANNER.deleted))
        if u.path == "/api/folder":
            return self._send(200, folder_view((q.get("p") or [""])[0]))
        if u.path == "/api/system":
            return self._send(200, system_info())
        if u.path == "/api/thumb":
            return self._thumb((q.get("p") or [""])[0])
        return self._send(404, {"error": "unbekannt"})

    def do_POST(self):
        u = urlparse(self.path)
        if not self._host_ok() or not self._auth({}):
            return self._send(403, {"error": "Zugriff verweigert"})
        if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            return self._send(415, {"error": "JSON erwartet"})
        b = self._body()
        if u.path == "/api/scan":
            started = SCANNER.start(b)
            return self._send(200, {"started": started})
        if u.path == "/api/cancel":
            SCANNER.cancel = True
            return self._send(200, {"ok": True})
        if u.path == "/api/trash":
            paths = [p for p in (b.get("paths") or []) if isinstance(p, str)]
            sizes = {p: known_size(p) for p in paths}
            ok, failed = move_to_trash(paths)
            freed = 0
            for p in ok:
                SCANNER.deleted.add(p)
                freed += sizes.get(p, 0)
                subtract_size(p, sizes.get(p, 0))
            return self._send(200, {"ok": ok, "failed": failed, "freed": freed})
        if u.path == "/api/reveal":
            p = b.get("path") or ""
            if IS_MAC and os.path.lexists(p):
                run(["open", "-R", p])
            return self._send(200, {"ok": True})
        if u.path == "/api/open":
            targets = {
                "activity": ["open", "-a", "Activity Monitor"],
                "storage": ["open", "x-apple.systempreferences:com.apple.settings.Storage"],
                "loginitems": ["open", "x-apple.systempreferences:com.apple.LoginItems-Settings.extension"],
                "icloud": ["open", "x-apple.systempreferences:com.apple.preferences.AppleIDPrefPane"],
                "trash": ["open", os.path.join(HOME, ".Trash")],
            }
            cmd = targets.get(b.get("what"))
            if IS_MAC and cmd:
                run(cmd)
            return self._send(200, {"ok": bool(cmd)})
        if u.path == "/api/empty-trash":
            if IS_MAC:
                run(["osascript", "-e", 'tell application "Finder" to empty trash'], timeout=600)
            return self._send(200, {"ok": True})
        if u.path == "/api/quit":
            self._send(200, {"ok": True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        return self._send(404, {"error": "unbekannt"})

    def _thumb(self, p):
        p = os.path.abspath(p)
        ext = os.path.splitext(p)[1].lower()
        if not IS_MAC or ext not in IMAGE_EXT or not os.path.isfile(p):
            return self._send(404, b"", "text/plain")
        out = os.path.join(SCANNER.tmpdir, hashlib.sha1(p.encode("utf-8", "replace")).hexdigest() + ".jpg")
        if not os.path.exists(out):
            run(["sips", "-s", "format", "jpeg", "-s", "formatOptions", "70", "-Z", "320", p, "--out", out], timeout=30)
        try:
            with open(out, "rb") as f:
                return self._send(200, f.read(), "image/jpeg", {"Cache-Control": "max-age=3600"})
        except OSError:
            return self._send(404, b"", "text/plain")


def known_size(p):
    if p in SCANNER.dir_sizes:
        return SCANNER.dir_sizes[p]
    try:
        st = os.lstat(p)
    except OSError:
        return 0
    if stat.S_ISDIR(st.st_mode):
        return SCANNER._dirsize(p, seen=set())[0]
    return st.st_blocks * 512


def subtract_size(p, size):
    d = SCANNER.dir_sizes
    d.pop(p, None)
    parent = os.path.dirname(p)
    kids = SCANNER.dir_children.get(parent)
    if kids and p in kids:
        kids.remove(p)
    elif parent in SCANNER.dir_files:
        s, n = SCANNER.dir_files[parent]
        SCANNER.dir_files[parent] = (max(0, s - size), max(0, n - 1))
    while parent and parent != os.path.dirname(parent):
        if parent in d:
            d[parent] = max(0, d[parent] - size)
        parent = os.path.dirname(parent)


def is_deleted(p, deleted):
    if p in deleted:
        return True
    parent = os.path.dirname(p)
    while parent and parent != os.path.dirname(parent):
        if parent in deleted:
            return True
        parent = os.path.dirname(parent)
    return False


def filter_deleted(r, deleted):
    if not deleted:
        return r
    r = dict(r)
    nd = lambda it: not is_deleted(it["p"], deleted)
    r["top_files"] = [x for x in r["top_files"] if nd(x)]
    r["installers"] = [x for x in r["installers"] if nd(x)]
    r["apps"] = [dict(a, related=[x for x in a["related"] if nd(x)]) for a in r["apps"] if nd(a)]
    for key in ("dups", "similar"):
        groups = []
        for g in r[key]:
            files = [f for f in g["files"] if nd(f)]
            if len(files) > 1:
                g = dict(g, files=files)
                if "size" in g:
                    g["waste"] = g["size"] * (len(files) - 1)
                groups.append(g)
        r[key] = groups
    tips = []
    for t in r["tips"]:
        t = dict(t)
        if "items" in t:
            t["items"] = [x for x in t["items"] if nd(x)]
        if t.get("p") and t["p"] in SCANNER.dir_sizes:
            t["s"] = SCANNER.dir_sizes[t["p"]]
        tips.append(t)
    r["tips"] = tips
    return r


def folder_view(p):
    S = SCANNER
    if not p:
        p = getattr(S, "root", HOME)
    p = os.path.abspath(p)
    if p not in S.dir_sizes:
        return {"path": p, "missing": True}
    kids = []
    for k in S.dir_children.get(p, []):
        if k in S.deleted:
            continue
        kids.append({"p": k, "s": S.dir_sizes.get(k, 0), "pkg": k in S.pkg_dirs,
                     "deep": k not in S.dir_children and k not in S.pkg_dirs})
    kids.sort(key=lambda x: -x["s"])
    files = [it for _, _, it in S.top if os.path.dirname(it["p"]) == p and not it["pkg"] and it["p"] not in S.deleted]
    files.sort(key=lambda x: -x["s"])
    direct = S.dir_files.get(p, (0, 0))
    root = getattr(S, "root", HOME)
    return {"path": p, "size": S.dir_sizes[p], "children": kids, "files": files[:30],
            "direct": {"s": direct[0], "n": direct[1]},
            "parent": os.path.dirname(p) if p != root else None, "root": root}


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Mac-Aufräumer – lokaler Speicher- und Leistungs-Check")
    ap.add_argument("--port", type=int, default=0, help="Port (Standard: zufällig freier Port)")
    ap.add_argument("--no-browser", action="store_true", help="Browser nicht automatisch öffnen")
    args = ap.parse_args()

    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    srv.daemon_threads = True
    url = "http://127.0.0.1:%d/?t=%s" % (srv.server_address[1], TOKEN)
    print("=" * 64)
    print(" Mac-Aufräumer %s läuft – nur lokal auf diesem Mac." % VERSION)
    print(" Öffne im Browser:\n   " + url)
    print(" Beenden: Fenster schließen oder Ctrl+C drücken.")
    print("=" * 64)
    if not IS_MAC:
        print(" Hinweis: Kein macOS erkannt – einige Funktionen sind eingeschränkt.")
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        shutil.rmtree(SCANNER.tmpdir, ignore_errors=True)
        print("\nBeendet.")


if __name__ == "__main__":
    sys.setrecursionlimit(20000)
    try:  # tiefe Ordnerstrukturen brauchen mehr Stack im Scan-Thread
        threading.stack_size(64 * 1024 * 1024)
    except (ValueError, RuntimeError):
        pass
    main()
