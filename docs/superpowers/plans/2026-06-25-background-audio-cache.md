# Background Audio Cache Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a persistent Edge TTS audio cache that plays already-generated speech immediately and pre-generates missing audio in the background.

**Architecture:** Keep Anki note/card data untouched. Store generated MP3 files in the add-on `user_files/audio_cache/` directory, because Anki preserves `user_files` across add-on upgrades. A shared card-text helper produces the exact speakable text for both live review and warm-cache generation, so prefetch keys match playback keys; all Anki collection/card rendering happens on the main thread before text is handed to the background worker. `TTSEngine` first checks the disk cache, copies cached MP3s to a temporary playback file, generates Edge audio into the cache on a miss under a per-key generation lock, validates the MP3 before publishing it, and a new prefetch worker warms future entries without blocking review.

**Tech Stack:** Python 3.9+, Anki add-on APIs, bundled `edge_tts`, `unittest`, `uv`, macOS/Linux/Windows process playback.

**References:**
- Anki add-on `user_files` is preserved across upgrades: https://addon-docs.ankiweb.net/addon-config.html#user-files
- Anki card rendering API exposes `card.render_output()`: https://github.com/ankitects/anki/blob/master/pylib/anki/cards.py

---

## Execution Rule: Claude Review After Every Step

After each checked step below, run a Claude review before moving to the next step. Use this exact pattern, replacing `TASK` and `STEP` with the current task/step number and including the changed files in `git diff --`:

```bash
python - <<'PY' | claude -p --no-session-persistence --safe-mode --tools "" --permission-mode dontAsk
import subprocess

task = "TASK"
step = "STEP"
summary = "Review the completed plan step for the Anki TTS background audio cache. Check correctness, regressions, and missing tests. Reply with concise findings only."
diff = subprocess.check_output(["git", "diff", "--"], text=True)
print(f"{summary}\n\nCompleted: {task} / {step}\n\nDiff:\n{diff}")
PY
```

If Claude reports a correctness issue, stop and address it before continuing. If Claude only suggests optional scope expansion, record it and continue unless it affects the stated goal.

## File Structure

- Create `anki_tts_addon/audio_cache.py`: pure disk-cache logic; no Anki imports; deterministic cache keys; atomic writes; size cleanup.
- Create `tests/module_loader.py`: test helper that loads add-on submodules without executing `anki_tts_addon/__init__.py`, because `__init__.py` requires Anki's `aqt` package.
- Create `tests/test_audio_cache.py`: cache key, path, reservation, atomic store, and cleanup tests.
- Modify `anki_tts_addon/tts_engine.py`: check cached MP3 before generation; generate Edge audio into cache; expose `prefetch(text, config)` for background warmup; retain Piper/system fallback for true Edge failures.
- Create `tests/test_tts_engine_cache.py`: fake Edge TTS and fake playback; cache hit/miss behavior; prefetch does not play audio; fallback behavior.
- Create `anki_tts_addon/audio_prefetch.py`: small single-worker queue with deduping and stop support.
- Create `tests/test_audio_prefetch.py`: queue dedupe, worker calls `engine.prefetch`, stop behavior.
- Create `anki_tts_addon/card_text.py`: shared rendered/fallback text extraction for question and answer sides.
- Create `tests/test_card_text.py`: prove live-review and warm-cache paths use the same normalization.
- Modify `anki_tts_addon/__init__.py`: instantiate cache/prefetch worker, use the shared text helper for review question/answer text, add menu actions to warm due-card audio and clear the cache.
- Modify `tests/test_reviewer_hooks.py`: assert question playback uses cached rendered HTML through the shared helper.
- Modify `anki_tts_addon/config.json`: add conservative cache defaults.
- Modify `README.md`: document the audio cache behavior, storage location, and warm-cache action.
- Modify `build_addon.sh`: ensure `user_files/README.txt` ships but cached MP3s do not.
- Create `anki_tts_addon/user_files/README.txt`: keeps the preserved user-files folder present in the package.

---

### Task 1: Persistent Audio Cache Module

**Files:**
- Create: `anki_tts_addon/audio_cache.py`
- Create: `tests/module_loader.py`
- Create: `tests/test_audio_cache.py`

- [ ] **Step 1.1: Create the test module loader and write failing cache tests**

Create `tests/module_loader.py`:

```python
import importlib.util
import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ADDON_DIR = ROOT / "anki_tts_addon"


def load_addon_module(name: str):
    package = sys.modules.get("anki_tts_addon")
    if package is None:
        package = types.ModuleType("anki_tts_addon")
        package.__path__ = [str(ADDON_DIR)]
        sys.modules["anki_tts_addon"] = package

    module_name = f"anki_tts_addon.{name}"
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(
        module_name,
        ADDON_DIR / f"{name}.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module
```

Create `tests/test_audio_cache.py`:

```python
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from module_loader import load_addon_module

_MODULE = load_addon_module("audio_cache")
AudioCache = _MODULE.AudioCache
speech_cache_key = _MODULE.speech_cache_key


class AudioCacheTests(unittest.TestCase):
    def test_key_changes_when_text_voice_or_speed_changes(self):
        first = speech_cache_key("hello", "en-GB-RyanNeural", 1.5)
        self.assertEqual(first, speech_cache_key("hello", "en-GB-RyanNeural", 1.5))
        self.assertNotEqual(first, speech_cache_key("hello!", "en-GB-RyanNeural", 1.5))
        self.assertNotEqual(first, speech_cache_key("hello", "en-US-GuyNeural", 1.5))
        self.assertNotEqual(first, speech_cache_key("hello", "en-GB-RyanNeural", 1.0))

    def test_store_from_temp_moves_mp3_atomically(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = AudioCache(Path(tmpdir))
            key = speech_cache_key("hello", "en-GB-RyanNeural", 1.5)
            tmp = cache.temp_path_for_key(key)
            tmp.write_bytes(b"mp3-bytes")

            final = cache.store_from_temp(key, tmp)

            self.assertEqual(final, cache.path_for_key(key))
            self.assertTrue(final.exists())
            self.assertEqual(final.read_bytes(), b"mp3-bytes")
            self.assertFalse(tmp.exists())

    def test_store_from_temp_rejects_empty_audio(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = AudioCache(Path(tmpdir))
            key = speech_cache_key("hello", "en-GB-RyanNeural", 1.5)
            tmp = cache.temp_path_for_key(key)
            tmp.write_bytes(b"")

            with self.assertRaises(ValueError):
                cache.store_from_temp(key, tmp)

            self.assertFalse(cache.path_for_key(key).exists())

    def test_store_from_temp_rejects_tiny_audio(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = AudioCache(Path(tmpdir))
            key = speech_cache_key("hello", "en-GB-RyanNeural", 1.5)
            tmp = cache.temp_path_for_key(key)
            tmp.write_bytes(b"x")

            with self.assertRaises(ValueError):
                cache.store_from_temp(key, tmp)

            self.assertFalse(cache.path_for_key(key).exists())

    def test_generation_lock_is_reused_per_key(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = AudioCache(Path(tmpdir))
            key = speech_cache_key("hello", "en-GB-RyanNeural", 1.5)

            self.assertIs(cache.generation_lock(key), cache.generation_lock(key))
```

- [ ] **Step 1.2: Run tests to verify they fail**

Run:

```bash
uv run python -m unittest tests/test_audio_cache.py
```

Expected: import failure for `anki_tts_addon.audio_cache`.

- [ ] **Step 1.3: Implement cache keying, atomic writes, and cleanup**

Create `anki_tts_addon/audio_cache.py`:

```python
"""Persistent MP3 cache for generated TTS audio."""

import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Iterable


CACHE_FORMAT_VERSION = "edge-mp3-v1"
MIN_AUDIO_BYTES = 1024


def speech_cache_key(
    text: str,
    voice: str,
    speed: float,
    version: str = CACHE_FORMAT_VERSION,
) -> str:
    payload = {
        "text": text,
        "voice": voice,
        "speed": round(float(speed), 3),
        "version": version,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


class AudioCache:
    def __init__(self, root: Path | str):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._locks: dict[str, threading.Lock] = {}
        self._locks_lock = threading.Lock()

    def path_for_key(self, key: str) -> Path:
        return self.root / f"{key}.mp3"

    def temp_path_for_key(self, key: str) -> Path:
        return self.root / f"{key}.{os.getpid()}.tmp"

    def generation_lock(self, key: str) -> threading.Lock:
        with self._locks_lock:
            lock = self._locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._locks[key] = lock
            return lock

    def has(self, key: str) -> bool:
        path = self.path_for_key(key)
        return path.is_file() and path.stat().st_size > 0

    def get(self, key: str) -> Path | None:
        path = self.path_for_key(key)
        if not path.is_file() or path.stat().st_size <= 0:
            return None
        now = time.time()
        os.utime(path, (now, now))
        return path

    def store_from_temp(self, key: str, tmp_path: Path | str) -> Path:
        tmp = Path(tmp_path)
        if not tmp.is_file() or tmp.stat().st_size < MIN_AUDIO_BYTES:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise ValueError("Generated TTS audio was empty")
        final = self.path_for_key(key)
        final.parent.mkdir(parents=True, exist_ok=True)
        os.replace(tmp, final)
        return final

    def remove(self, key: str) -> None:
        try:
            self.path_for_key(key).unlink()
        except FileNotFoundError:
            pass

    def iter_audio_files(self) -> Iterable[Path]:
        return self.root.glob("*.mp3")

    def cleanup(self, max_bytes: int) -> int:
        files = [
            p for p in self.iter_audio_files()
            if p.is_file() and not p.name.endswith(".tmp")
        ]
        total = sum(p.stat().st_size for p in files)
        removed = 0
        if total <= max_bytes:
            return removed

        files.sort(key=lambda p: p.stat().st_mtime)
        for path in files:
            if total <= max_bytes:
                break
            size = path.stat().st_size
            try:
                path.unlink()
            except FileNotFoundError:
                continue
            total -= size
            removed += 1
        return removed
```

- [ ] **Step 1.4: Run tests to verify cache module passes**

Run:

```bash
uv run python -m unittest tests/test_audio_cache.py
```

Expected: `Ran 5 tests` and `OK`.

- [ ] **Step 1.5: Ask Claude to review Task 1**

Run the Claude review command from the execution rule with `TASK="Task 1"` and `STEP="1.5"`.

---

### Task 2: Engine Uses Cached Edge Audio Before Live Generation

**Files:**
- Modify: `anki_tts_addon/tts_engine.py`
- Create: `tests/test_tts_engine_cache.py`

- [ ] **Step 2.1: Write failing tests for cache hit, cache miss, and prefetch**

Create `tests/test_tts_engine_cache.py`:

```python
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from module_loader import load_addon_module

_AUDIO_CACHE = load_addon_module("audio_cache")
_TTS_ENGINE = load_addon_module("tts_engine")
AudioCache = _AUDIO_CACHE.AudioCache
speech_cache_key = _AUDIO_CACHE.speech_cache_key
TTSEngine = _TTS_ENGINE.TTSEngine


class FakeEdgeTTS:
    class Communicate:
        saves = []

        def __init__(self, text, voice, rate):
            self.text = text
            self.voice = voice
            self.rate = rate

        async def save(self, path):
            type(self).saves.append((self.text, self.voice, self.rate, path))
            Path(path).write_bytes(b"edge-mp3" * 200)


class CachedTTSEngineTests(unittest.TestCase):
    def test_cache_hit_plays_existing_file_without_edge_generation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = AudioCache(tmpdir)
            engine = TTSEngine(audio_cache=cache)
            played = []
            engine._play_file = lambda path: played.append(Path(path).read_bytes())
            engine._get_edge_tts = lambda: self.fail("edge should not be loaded")
            key = speech_cache_key("hello", engine.EDGE_VOICE, 1.5)
            cache.path_for_key(key).write_bytes(b"cached")

            engine._speak("hello", {"speed": 1.5, "fallback_to_system": True})

            self.assertEqual(played, [b"cached"])

    def test_cache_miss_generates_edge_audio_into_cache_then_plays_it(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            FakeEdgeTTS.Communicate.saves = []
            cache = AudioCache(tmpdir)
            engine = TTSEngine(audio_cache=cache)
            played = []
            engine._play_file = lambda path: played.append(Path(path).read_bytes())
            engine._get_edge_tts = lambda: FakeEdgeTTS

            engine._speak("hello", {"speed": 1.5, "fallback_to_system": True})

            key = speech_cache_key("hello", engine.EDGE_VOICE, 1.5)
            self.assertEqual(cache.path_for_key(key).read_bytes(), b"edge-mp3" * 200)
            self.assertEqual(played, [b"edge-mp3" * 200])
            self.assertEqual(FakeEdgeTTS.Communicate.saves[0][0], "hello")

    def test_prefetch_generates_but_does_not_play(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            FakeEdgeTTS.Communicate.saves = []
            cache = AudioCache(tmpdir)
            engine = TTSEngine(audio_cache=cache)
            engine._play_file = lambda _path: self.fail("prefetch should not play")
            engine._get_edge_tts = lambda: FakeEdgeTTS

            engine.prefetch("hello", {"speed": 1.5})

            key = speech_cache_key("hello", engine.EDGE_VOICE, 1.5)
            self.assertTrue(cache.has(key))
```

- [ ] **Step 2.2: Run tests to verify they fail**

Run:

```bash
uv run python -m unittest tests/test_tts_engine_cache.py
```

Expected: constructor/type failure because `TTSEngine(audio_cache=...)` and `prefetch()` do not exist yet.

- [ ] **Step 2.3: Modify `TTSEngine` for cached playback**

Edit `anki_tts_addon/tts_engine.py`:

```python
from pathlib import Path

from .audio_cache import AudioCache, speech_cache_key
```

Change the constructor:

```python
def __init__(self, audio_cache: Optional[AudioCache] = None):
    self._process: Optional[subprocess.Popen] = None
    self._lock = threading.Lock()
    self._edge_tts = None
    self._edge_tts_checked = False
    self._edge_tts_failed = False
    self._piper_voice = None
    self._piper_voice_checked = False
    self._audio_cache = audio_cache
```

Add helpers inside `TTSEngine`:

```python
def _cache_key(self, text: str, speed: float) -> str:
    return speech_cache_key(text, self.EDGE_VOICE, speed)

def _cached_path(self, text: str, speed: float) -> Optional[Path]:
    if self._audio_cache is None:
        return None
    return self._audio_cache.get(self._cache_key(text, speed))

def _generate_edge_cached(self, text: str, speed: float, edge_tts) -> Optional[Path]:
    if self._audio_cache is None:
        return None
    key = self._cache_key(text, speed)
    cached = self._audio_cache.get(key)
    if cached:
        return cached
    with self._audio_cache.generation_lock(key):
        cached = self._audio_cache.get(key)
        if cached:
            return cached
        tmp = self._audio_cache.temp_path_for_key(key)
        try:
            self._save_edge_audio(text, speed, edge_tts, str(tmp))
            return self._audio_cache.store_from_temp(key, tmp)
        except Exception:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise

def _play_cached_file(self, path: Path) -> None:
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
        tmp = f.name
        f.write(path.read_bytes())
    try:
        self._play_file(tmp)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass

def _save_edge_audio(self, text: str, speed: float, edge_tts, path: str) -> None:
    import asyncio

    rate_pct = int((speed - 1.0) * 100)
    rate_str = f"+{rate_pct}%" if rate_pct >= 0 else f"{rate_pct}%"

    async def _generate(tmp_path: str):
        comm = edge_tts.Communicate(text, self.EDGE_VOICE, rate=rate_str)
        await comm.save(tmp_path)

    asyncio.run(_generate(path))

def prefetch(self, text: str, config: dict) -> None:
    speed = config.get("speed", 1.5)
    if not text or self._audio_cache is None:
        return
    if self._cached_path(text, speed):
        return
    edge_tts = self._get_edge_tts()
    if edge_tts is None:
        return
    try:
        self._generate_edge_cached(text, speed, edge_tts)
    except Exception as e:
        log.warning("Edge TTS prefetch failed: %s", e)
```

Modify `_speak()` so the first logic after `speed` and `fallback` is:

```python
cached = self._cached_path(text, speed)
if cached:
    self._play_cached_file(cached)
    return
```

Modify the Edge branch in `_speak()`:

```python
if not self._edge_tts_failed:
    edge_tts = self._get_edge_tts()
    if edge_tts is not None:
        try:
            cached_path = self._generate_edge_cached(text, speed, edge_tts)
            if cached_path:
                self._play_cached_file(cached_path)
            else:
                self._speak_edge(text, speed, edge_tts)
            return
        except Exception as e:
            log.warning("Edge TTS failed, switching to Piper: %s", e)
            self._edge_tts_failed = True
            _notify_edge_unavailable(e)
```

Modify `_speak_edge()` to delegate generation:

```python
def _speak_edge(self, text: str, speed: float, edge_tts) -> None:
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
        tmp = f.name
    try:
        self._save_edge_audio(text, speed, edge_tts, tmp)
        self._play_file(tmp)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
```

- [ ] **Step 2.4: Run engine cache tests**

Run:

```bash
uv run python -m unittest tests/test_tts_engine_cache.py
```

Expected: `Ran 3 tests` and `OK`.

- [ ] **Step 2.5: Run all tests**

Run:

```bash
uv run python -m unittest discover -s tests
```

Expected: all tests pass.

- [ ] **Step 2.6: Ask Claude to review Task 2**

Run the Claude review command from the execution rule with `TASK="Task 2"` and `STEP="2.6"`.

---

### Task 3: Background Prefetch Worker

**Files:**
- Create: `anki_tts_addon/audio_prefetch.py`
- Create: `tests/test_audio_prefetch.py`

- [ ] **Step 3.1: Write failing worker tests**

Create `tests/test_audio_prefetch.py`:

```python
import time
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from module_loader import load_addon_module

_MODULE = load_addon_module("audio_prefetch")
AudioPrefetcher = _MODULE.AudioPrefetcher


class FakeEngine:
    def __init__(self):
        self.calls = []

    def prefetch(self, text, config):
        self.calls.append((text, dict(config)))


class AudioPrefetcherTests(unittest.TestCase):
    def test_enqueue_dedupes_same_text_and_config(self):
        engine = FakeEngine()
        prefetcher = AudioPrefetcher(engine)
        config = {"speed": 1.5}

        prefetcher.enqueue("hello", config)
        prefetcher.enqueue("hello", config)
        prefetcher.drain_for_tests()

        self.assertEqual(engine.calls, [("hello", config)])

    def test_empty_text_is_ignored(self):
        engine = FakeEngine()
        prefetcher = AudioPrefetcher(engine)

        prefetcher.enqueue("", {"speed": 1.5})
        prefetcher.drain_for_tests()

        self.assertEqual(engine.calls, [])

    def test_start_and_stop_worker(self):
        engine = FakeEngine()
        prefetcher = AudioPrefetcher(engine)
        prefetcher.start()
        prefetcher.enqueue("hello", {"speed": 1.5})
        deadline = time.time() + 2
        while time.time() < deadline and not engine.calls:
            time.sleep(0.01)
        prefetcher.stop()

        self.assertEqual(engine.calls, [("hello", {"speed": 1.5})])
```

- [ ] **Step 3.2: Run tests to verify they fail**

Run:

```bash
uv run python -m unittest tests/test_audio_prefetch.py
```

Expected: import failure for `anki_tts_addon.audio_prefetch`.

- [ ] **Step 3.3: Implement the worker**

Create `anki_tts_addon/audio_prefetch.py`:

```python
"""Background queue for warming generated TTS audio."""

import queue
import threading
from typing import Any


class AudioPrefetcher:
    def __init__(self, engine):
        self._engine = engine
        self._queue: queue.Queue[tuple[str, dict[str, Any]] | None] = queue.Queue()
        self._queued_keys: set[tuple[str, tuple[tuple[str, Any], ...]]] = set()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def _key(self, text: str, config: dict) -> tuple[str, tuple[tuple[str, Any], ...]]:
        relevant = {
            "speed": config.get("speed", 1.5),
        }
        return (text, tuple(sorted(relevant.items())))

    def enqueue(self, text: str, config: dict) -> None:
        if not text:
            return
        key = self._key(text, config)
        with self._lock:
            if key in self._queued_keys:
                return
            self._queued_keys.add(key)
        self._queue.put((text, dict(config)))

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._queue.put(None)
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None

    def drain_for_tests(self) -> None:
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                return
            if item is None:
                return
            self._process(item)

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            self._process(item)

    def _process(self, item: tuple[str, dict[str, Any]]) -> None:
        text, config = item
        try:
            self._engine.prefetch(text, config)
        finally:
            key = self._key(text, config)
            with self._lock:
                self._queued_keys.discard(key)
```

- [ ] **Step 3.4: Run prefetch tests**

Run:

```bash
uv run python -m unittest tests/test_audio_prefetch.py
```

Expected: `Ran 3 tests` and `OK`.

- [ ] **Step 3.5: Run all tests**

Run:

```bash
uv run python -m unittest discover -s tests
```

Expected: all tests pass.

- [ ] **Step 3.6: Ask Claude to review Task 3**

Run the Claude review command from the execution rule with `TASK="Task 3"` and `STEP="3.6"`.

---

### Task 4: Shared Card Text Helper and Review Playback Wiring

**Files:**
- Create: `anki_tts_addon/card_text.py`
- Create: `tests/test_card_text.py`
- Modify: `anki_tts_addon/__init__.py`
- Modify: `tests/test_reviewer_hooks.py`
- Modify: `anki_tts_addon/config.json`
- Create: `anki_tts_addon/user_files/README.txt`

- [ ] **Step 4.1: Write failing card text and reviewer hook tests**

Create `tests/test_card_text.py`:

```python
import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from module_loader import load_addon_module

_MODULE = load_addon_module("card_text")
rendered_question_html = _MODULE.rendered_question_html
speakable_question_text = _MODULE.speakable_question_text


class CardTextTests(unittest.TestCase):
    def test_rendered_question_html_prefers_render_output(self):
        class Output:
            question_text = "rendered question"

        class Card:
            ord = 0

            def render_output(self, reload=True):
                return Output()

            def question(self):
                return "raw question"

        self.assertEqual(rendered_question_html(Card()), "rendered question")

    def test_rendered_question_html_falls_back_to_question(self):
        class Card:
            ord = 0

            def render_output(self, reload=True):
                raise RuntimeError("render failed")

            def question(self):
                return "raw question"

        self.assertEqual(rendered_question_html(Card()), "raw question")

    def test_speakable_question_text_matches_visible_cloze(self):
        class Card:
            ord = 0

            def question(self):
                return (
                    "Ruxolitinib has {{c3::JAK2}}, "
                    "{{c1::myelofibrosis}}, and {{c2::polycythemia vera}}"
                )

        rendered = (
            "Ruxolitinib has JAK2, myelofibrosis, "
            '<span class="cloze">[...]</span>'
        )

        self.assertEqual(
            speakable_question_text(Card(), rendered),
            "Ruxolitinib has JAK2, myelofibrosis, bla bla bla",
        )
```

Modify `tests/test_reviewer_hooks.py`:

```python
class _FakePrefetcher:
    def __init__(self):
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True
```

Add this test:

```python
def test_question_tts_uses_shared_rendered_text_helper(self):
    card = _FakeCard()
    engine = _FakeEngine()
    prefetcher = _FakePrefetcher()
    self.addon._engine = engine
    self.addon._prefetcher = prefetcher
    rendered_question = (
        "Ruxolitinib is used to treat chronic myeloproliferative "
        "disorders that have JAK2 mutations, including myelofibrosis "
        'and <span class="cloze">[...]</span>'
    )
    self.addon.cache_review_html(
        rendered_question, card, self.addon.REVIEW_QUESTION_CONTEXT
    )

    self.addon.on_reviewer_did_show_question(card)

    self.assertEqual(
        engine.spoken[0][0],
        "Ruxolitinib is used to treat chronic myeloproliferative disorders "
        "that have JAK2 mutations, including myelofibrosis and bla bla bla",
    )
```

Update `_FakeAddonManager.getConfig()` to include:

```python
"cache_enabled": True,
"prefetch_enabled": True,
"cache_max_mb": 2048,
```

- [ ] **Step 4.2: Run hook tests to verify they fail**

Run:

```bash
uv run python -m unittest tests/test_reviewer_hooks.py
```

Expected: failure because `anki_tts_addon.card_text` does not exist yet and review hooks do not use it.

- [ ] **Step 4.3: Implement shared text helper and add-on lifecycle wiring**

Create `anki_tts_addon/card_text.py`:

```python
"""Shared card text extraction for live TTS and background cache warmup."""

import logging

from .text_processing import extract_speakable_text

log = logging.getLogger(__name__)


def rendered_question_html(card, fallback_html: str | None = None) -> str:
    if fallback_html:
        return fallback_html
    try:
        output = card.render_output(reload=True)
        rendered = getattr(output, "question_text", None)
        if rendered:
            return rendered
    except Exception as e:
        log.warning("Card render_output() failed during TTS cache warmup: %s", e)
    return card.question()


def speakable_question_text(card, rendered_html: str | None = None) -> str:
    html = rendered_question_html(card, rendered_html)
    return extract_speakable_text(html, active_ord=card.ord)


def speakable_answer_text(card, rendered_html: str | None = None) -> str:
    html = rendered_html if rendered_html is not None else card.answer()
    return extract_speakable_text(html, strip_question=True, active_ord=card.ord)
```

Modify `anki_tts_addon/__init__.py` imports:

```python
import os
from pathlib import Path

from .audio_cache import AudioCache
from .audio_prefetch import AudioPrefetcher
from .card_text import speakable_answer_text, speakable_question_text
```

Add globals below `_engine`:

```python
_prefetcher: Optional[AudioPrefetcher] = None
_audio_cache: Optional[AudioCache] = None
```

Add helpers:

```python
def _addon_user_files_dir() -> Path:
    return Path(os.path.dirname(os.path.abspath(__file__))) / "user_files"

def audio_cache() -> AudioCache:
    global _audio_cache
    if _audio_cache is None:
        _audio_cache = AudioCache(_addon_user_files_dir() / "audio_cache")
    return _audio_cache

def engine() -> TTSEngine:
    global _engine
    if _engine is None:
        _engine = TTSEngine(audio_cache=audio_cache())
    return _engine

def prefetcher() -> AudioPrefetcher:
    global _prefetcher
    if _prefetcher is None:
        _prefetcher = AudioPrefetcher(engine())
    return _prefetcher

def _speak_text(text: str, conf: dict) -> None:
    if not text:
        return
    engine().speak(text, conf)
```

Replace the question hook extraction with:

```python
question_html = get_review_html(
    card, REVIEW_QUESTION_CONTEXT, card.question()
)
text = speakable_question_text(card, question_html)
_speak_text(text, conf)
```

Replace the answer hook extraction with:

```python
answer_html = get_review_html(card, REVIEW_ANSWER_CONTEXT, card.answer())
text = speakable_answer_text(card, answer_html)
_speak_text(text, conf)
```

In `_on_profile_did_open()`, after `set_status_callback(_show_status)`:

```python
prefetcher().start()
```

In `_on_profile_will_close()`, before clearing `_engine`:

```python
global _prefetcher, _audio_cache
if _prefetcher is not None:
    _prefetcher.stop()
    _prefetcher = None
```

Add `anki_tts_addon/user_files/README.txt`:

```text
This folder is preserved by Anki when the add-on is upgraded.
Anki TTS stores generated audio cache files under audio_cache/.
```

Modify `anki_tts_addon/config.json` to include:

```json
{
  "enabled": true,
  "speak_question": true,
  "speak_answer": false,
  "fallback_to_system": true,
  "speed": 1.5,
  "cache_enabled": true,
  "prefetch_enabled": true,
  "cache_max_mb": 2048
}
```

- [ ] **Step 4.4: Run reviewer hook tests**

Run:

```bash
uv run python -m unittest tests/test_reviewer_hooks.py
```

Expected: tests pass.

- [ ] **Step 4.5: Run all tests**

Run:

```bash
uv run python -m unittest discover -s tests
```

Expected: all tests pass.

- [ ] **Step 4.6: Ask Claude to review Task 4**

Run the Claude review command from the execution rule with `TASK="Task 4"` and `STEP="4.6"`.

---

### Task 5: Warm Due-Card Cache Command and Cleanup Controls

**Files:**
- Modify: `anki_tts_addon/__init__.py`
- Modify: `tests/test_card_text.py`

- [ ] **Step 5.1: Write failing tests for warmup helper behavior**

Append to `tests/test_card_text.py`:

```python
import importlib
import sys
import types
import unittest


class WarmupTests(unittest.TestCase):
    def test_render_card_question_prefers_render_output(self):
        class Output:
            question_text = "rendered question"

        class Card:
            def render_output(self, reload=True):
                return Output()

            def question(self):
                return "raw question"

        self.assertEqual(rendered_question_html(Card()), "rendered question")

    def test_render_card_question_falls_back_to_question(self):
        class Card:
            def render_output(self, reload=True):
                raise RuntimeError("render failed")

            def question(self):
                return "raw question"

        self.assertEqual(rendered_question_html(Card()), "raw question")
```

- [ ] **Step 5.2: Run tests to verify they fail**

Run:

```bash
uv run python -m unittest tests/test_card_text.py
```

Expected: failure until warmup uses the shared helper consistently.

- [ ] **Step 5.3: Implement warmup helpers and menu actions**

Add to `anki_tts_addon/__init__.py`:

```python
def warm_due_audio_cache() -> None:
    conf = get_config()
    if not conf.get("enabled", True) or not conf.get("cache_enabled", True):
        return

    # This runs on Anki's main thread. Do not move collection/card rendering
    # into AudioPrefetcher; the worker receives only plain text and config.
    card_ids = mw.col.find_cards("is:due")
    queued = 0
    for card_id in card_ids:
        card = mw.col.get_card(card_id)
        text = speakable_question_text(card)
        if text:
            prefetcher().enqueue(text, conf)
            queued += 1
    tooltip(f"Queued {queued} due cards for TTS audio caching")

def clear_audio_cache() -> None:
    if _prefetcher is not None:
        # stop() joins the worker before files are deleted.
        _prefetcher.stop()
    removed = 0
    for path in list(audio_cache().iter_audio_files()):
        try:
            path.unlink()
            removed += 1
        except OSError:
            pass
    tooltip(f"Cleared {removed} TTS audio files")
```

In `_on_profile_did_open()`, add menu actions after `download_action`:

```python
warm_cache_action = QAction("Warm Due Audio Cache", mw)
warm_cache_action.triggered.connect(warm_due_audio_cache)
menu.addAction(warm_cache_action)

clear_cache_action = QAction("Clear Audio Cache", mw)
clear_cache_action.triggered.connect(clear_audio_cache)
menu.addAction(clear_cache_action)
```

- [ ] **Step 5.4: Run warmup tests**

Run:

```bash
uv run python -m unittest tests/test_card_text.py
```

Expected: tests pass.

- [ ] **Step 5.5: Run all tests**

Run:

```bash
uv run python -m unittest discover -s tests
```

Expected: all tests pass.

- [ ] **Step 5.6: Ask Claude to review Task 5**

Run the Claude review command from the execution rule with `TASK="Task 5"` and `STEP="5.6"`.

---

### Task 6: Documentation, Build Exclusions, and Final Verification

**Files:**
- Modify: `README.md`
- Modify: `build_addon.sh`

- [ ] **Step 6.1: Update README**

Add a section under “Text Processing”:

```markdown
### Audio Cache

Anki TTS caches generated Edge TTS MP3 files under the add-on's `user_files/audio_cache/` directory. Anki preserves `user_files/` when the add-on is upgraded, so generated audio survives reinstalling the package.

During review, the add-on plays cached audio immediately when available. If audio is missing, it generates the file with Edge TTS, stores it, and then plays it. The “Warm Due Audio Cache” menu action queues due cards for background generation.

If card text, voice, or speed changes, the cache key changes and new audio is generated automatically. Old files can be removed with “Clear Audio Cache”.
```

- [ ] **Step 6.2: Update build exclusions**

Modify `build_addon.sh` zip exclusions so it includes `user_files/README.txt` but excludes generated cache files:

```bash
    -x "user_files/audio_cache/*" \
```

Remove the existing blanket exclusion:

```bash
    -x "user_files/*" \
```

- [ ] **Step 6.3: Run full tests**

Run:

```bash
uv run python -m unittest discover -s tests
```

Expected: all tests pass.

- [ ] **Step 6.4: Rebuild plugin**

Run:

```bash
./build_addon.sh
```

Expected: `Built: anki_tts.ankiaddon`.

- [ ] **Step 6.5: Verify package contents**

Run:

```bash
unzip -l anki_tts.ankiaddon | rg '(audio_cache.py|audio_prefetch.py|user_files/README.txt|user_files/audio_cache)'
```

Expected:
- `audio_cache.py` is present.
- `audio_prefetch.py` is present.
- `user_files/README.txt` is present.
- no generated `user_files/audio_cache/*.mp3` files are present.

- [ ] **Step 6.6: Ask Claude to review Task 6 and the final diff**

Run the Claude review command from the execution rule with `TASK="Task 6"` and `STEP="6.6"`.

---

## Final Acceptance Criteria

- Cached review audio plays without contacting Edge TTS when the MP3 already exists.
- First-time Edge TTS generation stores MP3s in `anki_tts_addon/user_files/audio_cache/`.
- Background prefetch can generate audio without playing it.
- Review question and answer hooks speak text through the shared rendered-text helper.
- The warm due-card cache action enqueues text through the same shared helper, so its cache keys match playback keys when Python-rendered text matches reviewer-rendered text.
- Cache keys change when text, voice, speed, or cache format version changes.
- The warm-cache menu can queue due cards.
- The clear-cache menu removes generated MP3s.
- `uv run python -m unittest discover -s tests` passes.
- `./build_addon.sh` produces `anki_tts.ankiaddon` without bundled generated MP3 cache files.

## Known Non-Goals For This Plan

- Do not edit Anki notes or add `[sound:...]` fields.
- Do not sync generated audio through Anki media in this version.
- Do not remove Piper/system fallback yet; they remain fallback paths for Edge failures.
- Do not attempt perfect bulk rendering for every custom JavaScript template. The exact reviewer text is cached when shown, and warmup uses Anki’s Python render output as a best-effort pre-generation path.

## Self-Review

- Spec coverage: persistent cache, background generation, update-on-text-change behavior, immediate playback, and plugin rebuild are covered by Tasks 1-6.
- Placeholder scan: no `TBD`, `TODO`, or unspecified test commands remain.
- Type consistency: `AudioCache`, `speech_cache_key`, `TTSEngine.prefetch`, `AudioPrefetcher.enqueue/start/stop`, and `speakable_question_text` are introduced before use in later tasks.
