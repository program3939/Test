import hashlib, http.server, io, json, os, shutil, sys, tempfile, threading, unittest, zipfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import roblox_portable as rp


def mkzip(files):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w", zipfile.ZIP_DEFLATED) as z:
        for n, d in files.items():
            z.writestr(n, d)
    return b.getvalue()


def build_release(ver, evil=False, badhash=False):
    big = os.urandom(300_000)
    pk = {"RobloxApp.zip": mkzip({"RobloxPlayerBeta.exe": b"MZfake" + big}),
          "content-fonts.zip": mkzip({"sub\\a.ttf": b"font"})}
    if evil:
        pk["shaders.zip"] = mkzip({"../evil.txt": b"x"})
    files, man = {}, "v0\n"
    for n, d in pk.items():
        md5 = hashlib.md5(d).hexdigest()
        if badhash and n == "content-fonts.zip":
            md5 = "0" * 32
        man += "%s\r\n%s\r\n%d\r\n%d\r\n" % (n, md5, len(d), len(d) * 2)
        files["/%s-%s" % (ver, n)] = d
    files["/%s-rbxPkgManifest.txt" % ver] = man.encode()
    return files


STATE = {"files": {}, "current": "version-aaaa", "cut_once": set(), "ranges": []}


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def do_GET(self):
        if self.path.startswith("/v2/"):
            body = json.dumps({"version": "0.1", "clientVersionUpload": STATE["current"]}).encode()
            self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers()
            self.wfile.write(body); return
        data = STATE["files"].get(self.path)
        if data is None:
            self.send_error(404); return
        rng = self.headers.get("Range")
        start = 0
        if rng:
            STATE["ranges"].append((self.path, rng))
            start = int(rng.split("=")[1].split("-")[0])
            self.send_response(206)
        else:
            self.send_response(200)
        chunk = data[start:]
        self.send_header("Content-Length", str(len(chunk))); self.end_headers()
        if self.path in STATE["cut_once"] and not rng:      # erster Versuch: Verbindung mitten drin kappen
            STATE["cut_once"].discard(self.path)
            self.wfile.write(chunk[:len(chunk) // 2]); self.wfile.flush()
            self.connection.close(); return
        self.wfile.write(chunk)


class T(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        port = cls.srv.server_address[1]
        rp.ALLOWED_SUFFIXES = ("127.0.0.1",); rp.ALLOW_HTTP = True
        rp.VERSION_APIS = ["http://127.0.0.1:%d/v2/x" % port]; rp.CDN = "http://127.0.0.1:%d" % port

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.paths = rp.Paths(os.path.join(self.tmp, "RobloxPortable"))
        self.ctx = rp.Ctx(self.paths)
        STATE.update(files=build_release("version-aaaa"), current="version-aaaa", cut_once=set(), ranges=[])

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_install_resume_update(self):
        STATE["cut_once"].add("/version-aaaa-RobloxApp.zip")          # Abbruch mitten im Download
        self.assertEqual(rp.install(self.ctx), "installed")
        self.assertTrue(any(p.endswith("RobloxApp.zip") for p, _ in STATE["ranges"]), "Resume per Range erwartet")
        v = os.path.join(self.paths.versions, "version-aaaa")
        self.assertTrue(os.path.isfile(os.path.join(v, "RobloxPlayerBeta.exe")))
        self.assertTrue(os.path.isfile(os.path.join(v, "content", "fonts", "sub", "a.ttf")))
        self.assertTrue(os.path.isfile(os.path.join(v, "AppSettings.xml")))
        self.assertEqual(rp.install(self.ctx), "uptodate")
        # Update
        STATE["files"].update(build_release("version-bbbb")); STATE["current"] = "version-bbbb"
        self.assertEqual(rp.install(self.ctx), "installed")
        self.assertEqual(os.listdir(self.paths.versions), ["version-bbbb"])
        self.assertEqual(rp.installed_version(self.paths), "version-bbbb")
        # nichts außerhalb des Basisordners entstanden
        self.assertEqual(os.listdir(self.tmp), ["RobloxPortable"])

    def test_resume_from_existing_part(self):
        d = os.path.join(self.paths.downloads, "version-aaaa"); os.makedirs(d)
        full = STATE["files"]["/version-aaaa-RobloxApp.zip"]
        open(os.path.join(d, "RobloxApp.zip.part"), "wb").write(full[:1000])
        rp.install(self.ctx)
        self.assertIn(("/version-aaaa-RobloxApp.zip", "bytes=1000-"), STATE["ranges"])

    def test_hash_mismatch(self):
        STATE["files"].update(build_release("version-aaaa", badhash=True))
        with self.assertRaises(rp.UserError) as c:
            rp.install(self.ctx)
        self.assertIn("Prüfsumme", str(c.exception))
        self.assertIsNone(rp.installed_version(self.paths))

    def test_zip_slip(self):
        STATE["files"].update(build_release("version-aaaa", evil=True))
        with self.assertRaises(rp.UserError):
            rp.install(self.ctx)
        self.assertFalse(os.path.exists(os.path.join(self.paths.versions, "evil.txt")))

    def test_offline_message(self):
        rp.VERSION_APIS = ["http://127.0.0.1:1/v2/x"]
        try:
            rp.install(self.ctx)
        except Exception as e:
            self.assertIn("Keine Verbindung", rp.explain(e))
        else:
            self.fail("Fehler erwartet")
        finally:
            rp.VERSION_APIS = ["http://127.0.0.1:%d/v2/x" % self.srv.server_address[1]]

    def test_host_allowlist(self):
        old = rp.ALLOWED_SUFFIXES, rp.ALLOW_HTTP
        rp.ALLOWED_SUFFIXES, rp.ALLOW_HTTP = ("roblox.com", "rbxcdn.com"), False
        try:
            for u in ("https://evil.com/x", "https://roblox.com.evil.com/x", "http://setup.rbxcdn.com/x"):
                with self.assertRaises(rp.UserError):
                    rp.check_host(u)
            rp.check_host("https://setup.rbxcdn.com/x"); rp.check_host("https://clientsettingscdn.roblox.com/x")
        finally:
            rp.ALLOWED_SUFFIXES, rp.ALLOW_HTTP = old

    def test_args_and_env(self):
        self.assertEqual(rp.build_args("")[0], ["--app"])
        a, _ = rp.build_args("roblox-player:1+launchmode:play+gameinfo:TICK+launchtime:123+placelauncherurl:"
                             "https%3A%2F%2Fassetgame.roblox.com%2Fgame%2FPlaceLauncher.ashx%3Frequest%3DX+"
                             "browsertrackerid:9+robloxLocale:en_us+gameLocale:en_us+channel:+LaunchExp:InApp")
        self.assertEqual(a[:5], ["--play", "-t", "TICK", "--launchtime=123", "-j"])
        with self.assertRaises(rp.UserError):
            rp.build_args("roblox-player:1+launchmode:play+placelauncherurl:https%3A%2F%2Fevil.com%2Fx")
        with self.assertRaises(rp.UserError):
            rp.build_args("calc.exe & dir")
        self.assertEqual(rp.build_args("https://www.roblox.com/games/123/x")[0], ["--app"])
        env = rp.child_env(self.paths)
        for k in ("LOCALAPPDATA", "APPDATA", "TEMP", "TMP", "USERPROFILE"):
            self.assertTrue(rp._inside(env[k], self.paths.base), k)

    def test_leakcheck_diff(self):
        s = rp.take_snapshot(self.paths)
        self.assertIn("Keine neuen", rp.diff_report(s, s, "t"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
