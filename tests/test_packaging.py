import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ADDON_DIR = ROOT / "anki_tts_addon"


def _read(path):
    return path.read_text(encoding="utf-8")


def _pyproject_value(key):
    match = re.search(rf'^{key} = "([^"]+)"$', _read(ROOT / "pyproject.toml"), re.M)
    return match.group(1) if match else None


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.manifest = json.loads(_read(ADDON_DIR / "manifest.json"))

    def test_package_matches_addon_folder(self):
        self.assertEqual(self.manifest["package"], ADDON_DIR.name)

    def test_requires_anki_with_python_313(self):
        # The vendored aiohttp stack needs Python >= 3.10 and ships cp313
        # extensions; Anki 25.07 was the first release on Python 3.13.
        self.assertEqual(self.manifest["min_point_version"], 250700)
        self.assertNotIn("max_point_version", self.manifest)

    def test_human_version_follows_pyproject(self):
        self.assertEqual(self.manifest["human_version"], _pyproject_value("version"))


class EdgeTtsFloorTests(unittest.TestCase):
    def test_floor_is_the_same_everywhere(self):
        engine = re.search(
            r"^EDGE_TTS_MIN_VERSION = \((\d+), (\d+), (\d+)\)$",
            _read(ADDON_DIR / "tts_engine.py"),
            re.M,
        )
        build = re.search(
            r'EDGE_TTS_MIN_VERSION="\$\{EDGE_TTS_MIN_VERSION:-([\d.]+)\}"',
            _read(ROOT / "build_addon.sh"),
        )
        pyproject = re.search(r'"edge-tts>=([\d.]+)"', _read(ROOT / "pyproject.toml"))
        self.assertIsNotNone(engine)
        self.assertIsNotNone(build)
        self.assertIsNotNone(pyproject)
        floor = ".".join(engine.groups())
        self.assertEqual(build.group(1), floor)
        self.assertEqual(pyproject.group(1), floor)


if __name__ == "__main__":
    unittest.main()
