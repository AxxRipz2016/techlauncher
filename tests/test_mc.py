import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from techlauncher import LauncherError
from techlauncher.mc import auth, install, launch, rules, versions
from techlauncher.paths import Layout


class RulesTest(unittest.TestCase):
    def test_no_rules_allowed(self):
        self.assertTrue(rules.allowed(None))
        self.assertTrue(rules.allowed([]))

    def test_os_rules(self):
        with mock.patch.object(rules, "os_name", return_value="linux"):
            self.assertTrue(rules.allowed([{"action": "allow", "os": {"name": "linux"}}]))
            self.assertFalse(rules.allowed([{"action": "allow", "os": {"name": "windows"}}]))
            # "разрешено всем, кроме osx"
            r = [{"action": "allow"}, {"action": "disallow", "os": {"name": "osx"}}]
            self.assertTrue(rules.allowed(r))

    def test_features(self):
        r = [{"action": "allow", "features": {"has_custom_resolution": True}}]
        self.assertFalse(rules.allowed(r, {}))
        self.assertTrue(rules.allowed(r, {"has_custom_resolution": True}))


class MavenTest(unittest.TestCase):
    def test_paths(self):
        self.assertEqual(install.maven_path("org.lwjgl:lwjgl:3.3.3:natives-linux"),
                         "org/lwjgl/lwjgl/3.3.3/lwjgl-3.3.3-natives-linux.jar")
        self.assertEqual(install.maven_path("net.fabricmc:fabric-loader:0.16.9"),
                         "net/fabricmc/fabric-loader/0.16.9/fabric-loader-0.16.9.jar")
        self.assertEqual(install.maven_path("a.b:c:1@zip"), "a/b/c/1/c-1.zip")

    def test_key_ignores_version(self):
        self.assertEqual(install.library_key("org.ow2.asm:asm:9.6"),
                         install.library_key("org.ow2.asm:asm:9.7"))
        self.assertNotEqual(install.library_key("org.lwjgl:lwjgl:3.3.3"),
                            install.library_key("org.lwjgl:lwjgl:3.3.3:natives-linux"))


class AuthTest(unittest.TestCase):
    def test_offline_uuid_like_the_game(self):
        # Известное значение: офлайн-UUID ника Notch.
        self.assertEqual(auth.offline_uuid("Notch"), "b50ad385829d3141a2167e7d7539ba7f")

    def test_offline_name_validation(self):
        self.assertEqual(auth.offline("Steve_123").username, "Steve_123")
        for bad in ("", "ab", "имя", "a" * 17, "with space"):
            with self.assertRaises(LauncherError):
                auth.offline(bad)


class MergeTest(unittest.TestCase):
    def test_inherits(self):
        parent = {"_id": "1.21", "mainClass": "net.minecraft.client.main.Main",
                  "arguments": {"game": ["--a"], "jvm": ["-Dx"]},
                  "downloads": {"client": {"url": "u", "sha1": "s", "size": 1}},
                  "libraries": [{"name": "org.ow2.asm:asm:9.6"}], "assetIndex": {"id": "17"}}
        child = {"_id": "fabric-1.21", "inheritsFrom": "1.21",
                 "mainClass": "net.fabricmc.loader.impl.launch.knot.KnotClient",
                 "arguments": {"game": [], "jvm": ["-Dfabric"]},
                 "libraries": [{"name": "org.ow2.asm:asm:9.7", "url": "https://maven.fabricmc.net/"}]}
        m = versions.merge([child, parent])
        self.assertEqual(m["mainClass"], "net.fabricmc.loader.impl.launch.knot.KnotClient")
        self.assertEqual(m["client_id"], "1.21")
        self.assertEqual(m["arguments"]["jvm"], ["-Dx", "-Dfabric"])
        self.assertEqual(m["libraries"][0]["name"], "org.ow2.asm:asm:9.7")

        with tempfile.TemporaryDirectory() as d:
            layout = Layout(Path(d))
            plan = install.Plan()
            seen = set()
            for lib in m["libraries"]:
                install._add_library(layout, plan, lib, seen)
            # дубликат asm от родителя отброшен, осталась версия из Fabric
            self.assertEqual(len(plan.classpath), 1)
            self.assertIn("9.7", str(plan.classpath[0]))
            self.assertTrue(plan.downloads[0].url.startswith("https://maven.fabricmc.net/org/ow2/asm/"))


class ArgsTest(unittest.TestCase):
    def test_substitute_and_rules(self):
        items = ["--username", "${auth_player_name}",
                 {"rules": [{"action": "allow", "features": {"has_custom_resolution": True}}],
                  "value": ["--width", "${resolution_width}"]}]
        args = launch._expand(items, {"has_custom_resolution": False})
        self.assertEqual(args, ["--username", "${auth_player_name}"])
        out = launch._substitute(args, {"auth_player_name": "Steve"})
        self.assertEqual(out, ["--username", "Steve"])
        # неизвестная подстановка остаётся как есть
        self.assertEqual(launch._substitute(["${nope}"], {}), ["${nope}"])


class LogTest(unittest.TestCase):
    def test_xml_to_text(self):
        f = launch.LogFormatter()
        lines = [
            '  <log4j:Event logger="x" timestamp="1794663325544" level="INFO" thread="Render thread">',
            '    <log4j:Message><![CDATA[Setting user: Danik]]></log4j:Message>',
            '  </log4j:Event>',
            'plain line',
        ]
        out = [o for l in lines for o in f.feed(l)]
        self.assertEqual(len(out), 2)
        self.assertTrue(out[0].endswith("[Render thread/INFO] Setting user: Danik"))
        self.assertEqual(out[1], "plain line")


if __name__ == "__main__":
    unittest.main()
