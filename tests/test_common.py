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
