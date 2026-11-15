import sys, zipfile, tempfile, unittest
from pathlib import Path
from unittest import mock

from techlauncher import wine, archive, LauncherError
from techlauncher.ini import IniFile
from techlauncher.paths import Layout


class IniTest(unittest.TestCase):
    def test_edit_preserves_format(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "options.ini"
            p.write_bytes(b"[options]\r\nwidth = 1024\r\nheight=768\r\n; comment\r\n\r\n[audio]\r\nmusic volume = 1.0\r\n")
            ini = IniFile(p)
            self.assertTrue(ini.set("Width", "1920"))
            self.assertTrue(ini.set("height", "1080"))
            self.assertFalse(ini.set("fullscreen", "true"))
            ini.save()
            self.assertEqual(p.read_bytes(),
                b"[options]\r\nwidth = 1920\r\nheight=1080\r\n; comment\r\nfullscreen = true\r\n\r\n[audio]\r\nmusic volume = 1.0\r\n")

    def test_missing_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "options.ini"
            ini = IniFile(p); ini.set("width", "800"); ini.save()
            self.assertEqual(p.read_bytes(), b"[options]\r\nwidth = 800\r\n")


class WineTest(unittest.TestCase):
    def test_linux_uses_wine(self):
        with tempfile.TemporaryDirectory() as d,              mock.patch.object(sys, "platform", "linux"),              mock.patch("shutil.which", lambda n: "/usr/bin/wine" if n == "wine" else None):
            layout = Layout(Path(d))
            prefix = Path(d) / "pfx"
            cmd, env = wine.build_command(layout, Path("/games/scp/SCP.exe"), ["-x"], None, prefix)
            self.assertEqual(cmd, ["/usr/bin/wine", str(Path("/games/scp/SCP.exe")), "-x"])
            self.assertEqual(env["WINEPREFIX"], str(prefix))
            self.assertEqual(env["PULSE_LATENCY_MSEC"], "60")

    def test_linux_no_wine(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(sys, "platform", "linux"),              mock.patch("shutil.which", lambda n: None):
            with self.assertRaises(LauncherError):
                wine.build_command(Layout(Path(d)), Path("a.exe"), [], None, None)


class WineManagedTest(unittest.TestCase):
    def _managed(self, d):
        layout = Layout(Path(d))
        bin_dir = layout.wine_dir / "wine-10.0-amd64-wow64" / "bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / "wine").write_text("")
        (bin_dir / "wineserver").write_text("")
        (layout.wine_dir / wine.CURRENT_FILE).write_text("wine-10.0-amd64-wow64")
        return layout, bin_dir

    def test_managed_env_pins_own_server(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(sys, "platform", "linux"), \
                mock.patch.dict("os.environ", {"PATH": "/usr/bin"}, clear=True):
            layout, bin_dir = self._managed(d)
            w = wine.resolve(layout)
            self.assertTrue(w.managed)
            env = w.env(layout.wine_prefix)
            self.assertEqual(env["WINESERVER"], str(bin_dir / "wineserver"))
            self.assertEqual(env["WINELOADER"], str(bin_dir / "wine"))
            self.assertTrue(env["PATH"].startswith(str(bin_dir)))
            self.assertIn("winemenubuilder.exe=d", env["WINEDLLOVERRIDES"])

    def test_install_skips_same_version(self):
        rel = wine.WineRelease("10.0", "wine-10.0-amd64-wow64.tar.xz", "http://x", 1, None)
        with tempfile.TemporaryDirectory() as d, mock.patch.object(sys, "platform", "linux"), \
                mock.patch.object(wine, "find_release", lambda tag=None: rel), \
                mock.patch.object(wine.net, "download_many") as dl:
            layout, bin_dir = self._managed(d)
            self.assertEqual(wine.install(layout, log=lambda m: None), bin_dir / "wine")
            dl.assert_not_called()

    def test_find_release_prefers_stable(self):
        def asset(name):
            return {"name": name, "browser_download_url": "u/" + name, "size": 5,
                    "digest": "sha256:ab"}
        releases = [
            {"tag_name": "11.0-rc1", "prerelease": False, "assets": [asset("wine-11.0-rc1-amd64-wow64.tar.xz")]},
            {"tag_name": "10.9", "prerelease": False, "assets": [asset("wine-10.9-amd64.tar.xz")]},
            {"tag_name": "10.8", "prerelease": False, "assets": [asset("wine-10.8-amd64-wow64.tar.xz")]},
        ]
        with mock.patch.object(wine.net, "get_json", lambda url: releases):
            rel = wine.find_release()
        self.assertEqual((rel.tag, rel.sha256), ("10.8", "ab"))

    @unittest.skipIf(sys.platform == "win32", "символические ссылки")
    def test_sandbox(self):
        with tempfile.TemporaryDirectory() as d:
            home, prefix = Path(d) / "home", Path(d) / "pfx"
            (home / "Documents").mkdir(parents=True)
            user = prefix / "drive_c" / "users" / "student"
            user.mkdir(parents=True)
            (user / "Documents").symlink_to(home / "Documents")
            (user / "AppData").mkdir()
            self.assertEqual(wine.sandbox(prefix), 1)
            self.assertFalse((user / "Documents").is_symlink())
            self.assertTrue((user / "Documents").is_dir())
            self.assertEqual(wine.sandbox(prefix), 0)


class ArchiveTest(unittest.TestCase):
    def test_flatten_and_zip_slip(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            z = d / "a.zip"
            with zipfile.ZipFile(z, "w") as zf:
                zf.writestr("Game v1/Game.exe", "x")
                zf.writestr("Game v1/Saves/s.txt", "y")
            archive.extract(z, d / "out")
            self.assertTrue((d / "out" / "Game.exe").is_file())

            bad = d / "bad.zip"
            with zipfile.ZipFile(bad, "w") as zf:
                zf.writestr("../evil.txt", "x")
            with self.assertRaises(LauncherError):
                archive.extract(bad, d / "out2")
            self.assertFalse((d / "evil.txt").exists())
            self.assertFalse((d / "out2.extracting").exists())


if __name__ == "__main__":
    unittest.main()
