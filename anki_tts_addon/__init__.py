"""
Anki TTS - Automatic Text-to-Speech for Anki Reviews

Reads card content aloud using Edge TTS (Ryan, online/cached) with fallback
to system TTS.

Install: Tools -> Add-ons -> Install from file -> select anki_tts.ankiaddon
"""

from aqt import mw, gui_hooks
from aqt.qt import (
    QAction,
    QMenu,
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QDoubleSpinBox,
    QCheckBox,
    QPushButton,
    QGroupBox,
    QTimer,
)
from aqt.utils import tooltip
from typing import Optional
import logging
import os
import threading
import time
from pathlib import Path

from . import addon_log
from . import monitor_ui
from .audio_cache import AudioCache, CACHE_FORMAT_VERSION, speech_cache_key
from .audio_cache_state import AudioCacheState
from .audio_prefetch import AudioPrefetcher
from .card_text import (
    rendered_question_html,
    speakable_answer_text,
    speakable_question_text,
)
from .monitor_model import WarmStats, build_snapshot
from .tts_engine import TTSEngine, set_status_callback
from .review_html_cache import (
    REVIEW_ANSWER_CONTEXT,
    REVIEW_QUESTION_CONTEXT,
    cache_review_html,
    clear_review_html_cache,
    get_review_html,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def get_config() -> dict:
    return mw.addonManager.getConfig(__name__) or {}


def save_config(conf: dict):
    mw.addonManager.writeConfig(__name__, conf)


# ---------------------------------------------------------------------------
# Engine singleton
# ---------------------------------------------------------------------------

DEFAULT_CACHE_MAX_MB = 2048
PREFETCH_STOP_TIMEOUT_SECONDS = 1.0
CLEAR_AUDIO_CACHE_STOP_TIMEOUT_SECONDS = 1.0
# Profile close runs on the Qt main thread: keep each wait tiny. In-flight
# requests are cancelled first, so workers exit almost immediately.
CLOSE_STOP_TIMEOUT_SECONDS = 0.1
AUTO_WARM_START_DELAY_MS = 5000
AUTO_WARM_SYNC_DELAY_MS = 1000
# Only temp files older than this are stale leftovers from a previous run.
TEMP_FILE_MIN_AGE_SECONDS = 300
ALL_CARDS_SEARCH = ""
# Bump when question-text extraction changes so the next warm-up rescans
# every card instead of trusting the incremental watermark.
WARM_TEXT_VERSION = 2
# Background scan chunking (see _scan_step): cards per collection op, a
# time cap per op, and the pause between ops.
SCAN_CHUNK_CARDS = 200
SCAN_CHUNK_MAX_SECONDS = 0.1
SCAN_CHUNK_GAP_MS = 50
SCAN_CHECK_EVERY_ITEMS = 500

_engine: Optional[TTSEngine] = None
_engine_cache_enabled: Optional[bool] = None
_prefetcher: Optional[AudioPrefetcher] = None
_audio_cache: Optional[AudioCache] = None
_audio_cache_state: Optional[AudioCacheState] = None
_profile_generation = 0
_shutting_down = False
_sync_in_progress = False

# Monitor state. WarmStats is the only object the worker/scan threads share
# with the UI; the monitor timer never reads config or touches the disk.
_warm_stats = WarmStats()
_user_paused = False  # session only; not saved to config
_monitor_conf = {
    "enabled": True,
    "cache_enabled": True,
    "prefetch_enabled": True,
    "cache_limit_bytes": DEFAULT_CACHE_MAX_MB * 1024 * 1024,
}


def stats() -> WarmStats:
    return _warm_stats


def _update_monitor_config(conf: dict) -> None:
    _monitor_conf.update(
        enabled=bool(conf.get("enabled", True)),
        cache_enabled=bool(conf.get("cache_enabled", True)),
        prefetch_enabled=_prefetch_enabled(conf),
        cache_limit_bytes=_cache_max_bytes(conf),
    )


def _advance_profile_generation() -> None:
    global _profile_generation
    _profile_generation += 1


def _collection_is_open(collection) -> bool:
    return collection is not None and getattr(collection, "db", None) is not None


def _profile_job_is_current(profile_generation: int, collection) -> bool:
    return (
        profile_generation == _profile_generation
        and not _shutting_down
        and not _sync_in_progress
        and _collection_is_open(collection)
        and mw.col is collection
    )


def _addon_user_files_dir() -> Path:
    path = Path(os.path.dirname(os.path.abspath(__file__))) / "user_files"
    path.mkdir(parents=True, exist_ok=True)
    return path


def audio_cache() -> AudioCache:
    global _audio_cache
    if _audio_cache is None:
        _audio_cache = AudioCache(_addon_user_files_dir() / "audio_cache")
    return _audio_cache


def audio_cache_state() -> AudioCacheState:
    global _audio_cache_state
    if _audio_cache_state is None:
        _audio_cache_state = AudioCacheState(
            _addon_user_files_dir() / "audio_cache_state.json"
        )
    return _audio_cache_state


def _cache_enabled(conf: Optional[dict] = None) -> bool:
    conf = conf if conf is not None else get_config()
    return conf.get("cache_enabled", True)


def _prefetch_enabled(conf: dict) -> bool:
    return (
        conf.get("enabled", True)
        and conf.get("cache_enabled", True)
        and conf.get("prefetch_enabled", True)
    )


def _cache_max_bytes(conf: dict) -> int:
    try:
        max_mb = float(conf.get("cache_max_mb", DEFAULT_CACHE_MAX_MB))
    except (TypeError, ValueError):
        max_mb = DEFAULT_CACHE_MAX_MB

    if max_mb <= 0:
        max_mb = DEFAULT_CACHE_MAX_MB
    return int(max_mb * 1024 * 1024)


def _configured_audio_cache(conf: dict) -> Optional[AudioCache]:
    global _audio_cache
    if not _cache_enabled(conf):
        _audio_cache = None
        return None
    return audio_cache()


def _detach_prefetcher_callbacks(worker) -> None:
    if hasattr(worker, "set_idle_callback"):
        worker.set_idle_callback(None)
    if hasattr(worker, "set_result_callback"):
        worker.set_result_callback(None)


def _stop_prefetcher(timeout=None) -> bool:
    global _prefetcher
    if _prefetcher is None:
        return True

    if not _prefetcher.stop(timeout=timeout):
        return False

    _prefetcher = None
    # The stopped worker discarded its queue: nothing is pending any more.
    if _audio_cache_state is not None:
        _audio_cache_state.clear_pending()
    return True


def prefetcher() -> AudioPrefetcher:
    global _prefetcher
    if _prefetcher is None:
        _prefetcher = AudioPrefetcher(engine())
        if _user_paused and hasattr(_prefetcher, "pause"):
            _prefetcher.pause()
        if hasattr(_prefetcher, "set_idle_callback"):
            _prefetcher.set_idle_callback(_on_audio_prefetch_idle)
        if hasattr(_prefetcher, "set_result_callback"):
            _prefetcher.set_result_callback(_on_audio_prefetch_result)
    return _prefetcher


def _sync_prefetcher_with_config(conf: dict) -> bool:
    _update_monitor_config(conf)
    if _prefetch_enabled(conf):
        return prefetcher().start() is not False

    _cancel_running_warm_job()
    if _engine is not None:
        _engine.stop()
    return _stop_prefetcher(timeout=PREFETCH_STOP_TIMEOUT_SECONDS)


def _shutdown_engine(old_engine, timeout: float) -> None:
    if old_engine is not None and hasattr(old_engine, "shutdown"):
        try:
            old_engine.shutdown(timeout=timeout)
        except Exception:
            log.warning("TTS engine shutdown failed", exc_info=True)


def engine() -> TTSEngine:
    global _engine, _engine_cache_enabled
    conf = get_config()
    cache_enabled = _cache_enabled(conf)
    if _engine is None:
        _engine = TTSEngine(
            audio_cache=_configured_audio_cache(conf),
            cache_max_bytes=_cache_max_bytes(conf),
        )
        _engine_cache_enabled = cache_enabled
    elif (
        _engine_cache_enabled is not None
        and _engine_cache_enabled != cache_enabled
    ):
        _engine.stop()
        drained = _stop_prefetcher(timeout=PREFETCH_STOP_TIMEOUT_SECONDS)
        if drained and hasattr(_engine, "wait_for_speech"):
            drained = _engine.wait_for_speech(
                timeout=PREFETCH_STOP_TIMEOUT_SECONDS
            )
        if drained:
            old_engine = _engine
            _engine = TTSEngine(
                audio_cache=_configured_audio_cache(conf),
                cache_max_bytes=_cache_max_bytes(conf),
            )
            _shutdown_engine(old_engine, CLOSE_STOP_TIMEOUT_SECONDS)
        else:
            # Could not fully drain in-flight work for a clean rebuild;
            # reconfigure the existing engine in place so it immediately
            # honors the new cache setting instead of keeping stale wiring.
            if hasattr(_engine, "set_audio_cache"):
                _engine.set_audio_cache(_configured_audio_cache(conf))
            if hasattr(_engine, "set_cache_max_bytes"):
                _engine.set_cache_max_bytes(_cache_max_bytes(conf))
        _engine_cache_enabled = cache_enabled
    elif hasattr(_engine, "set_cache_max_bytes"):
        _engine.set_cache_max_bytes(_cache_max_bytes(conf))
    return _engine


def _show_status(msg: str):
    """Show a tooltip on the main thread (safe from background threads)."""
    if _shutting_down:
        return
    try:
        mw.taskman.run_on_main(lambda: tooltip(msg))
    except Exception:
        log.debug("Could not show TTS status", exc_info=True)


def _speak_text(text: str, conf: dict):
    tts = engine()
    if text:
        tts.speak(text, conf)
    else:
        tts.stop()


# ---------------------------------------------------------------------------
# Reviewer hooks
# ---------------------------------------------------------------------------

def on_reviewer_did_show_question(card) -> None:
    """Called every time the reviewer shows a new question."""
    conf = get_config()
    if not conf.get("enabled", True):
        return

    if not conf.get("speak_question", True):
        if _engine is not None:
            _engine.stop()
        return

    # card.question() renders the card again: only fall back to it when the
    # reviewer HTML was not captured by card_will_show.
    question_html = get_review_html(card, REVIEW_QUESTION_CONTEXT, None)
    if question_html is None:
        question_html = card.question()
    text = speakable_question_text(card, question_html)
    _speak_text(text, conf)


def on_reviewer_did_show_answer(card) -> None:
    """Read the answer aloud if configured to do so."""
    conf = get_config()
    if not conf.get("enabled", True) or not conf.get("speak_answer", False):
        return

    answer_html = get_review_html(card, REVIEW_ANSWER_CONTEXT, None)
    if answer_html is None:
        answer_html = card.answer()
    text = speakable_answer_text(card, answer_html)
    _speak_text(text, conf)


def on_reviewer_will_end() -> None:
    """Stop TTS when leaving the reviewer."""
    clear_review_html_cache()
    # Never build an engine just to stop it (this also runs while closing).
    if _engine is not None:
        _engine.stop()


# ---------------------------------------------------------------------------
# Settings dialog
# ---------------------------------------------------------------------------

class SettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Anki TTS Settings")
        self.setMinimumWidth(360)
        self.conf = get_config()
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        # -- Enabled --
        self.enabled_cb = QCheckBox("Enable TTS during reviews")
        self.enabled_cb.setChecked(self.conf.get("enabled", True))
        layout.addWidget(self.enabled_cb)

        # -- Voice info --
        voice_group = QGroupBox("Voice")
        voice_layout = QVBoxLayout()
        voice_layout.addWidget(
            QLabel("Ryan (British Male) — Edge TTS online, cached locally")
        )
        voice_layout.addWidget(QLabel("System voice — offline fallback"))
        voice_group.setLayout(voice_layout)
        layout.addWidget(voice_group)

        # -- Speed --
        speed_group = QGroupBox("Speed")
        speed_layout = QHBoxLayout()
        speed_layout.addWidget(QLabel("Rate:"))
        self.speed_spin = QDoubleSpinBox()
        self.speed_spin.setRange(0.5, 2.0)
        self.speed_spin.setSingleStep(0.1)
        self.speed_spin.setValue(self.conf.get("speed", 1.5))
        self.speed_spin.setSuffix("x")
        speed_layout.addWidget(self.speed_spin)
        speed_group.setLayout(speed_layout)
        layout.addWidget(speed_group)

        # -- Reading options --
        opts_group = QGroupBox("Reading Options")
        opts_layout = QVBoxLayout()
        self.speak_q_cb = QCheckBox("Read question aloud")
        self.speak_q_cb.setChecked(self.conf.get("speak_question", True))
        opts_layout.addWidget(self.speak_q_cb)
        self.speak_a_cb = QCheckBox("Read answer aloud")
        self.speak_a_cb.setChecked(self.conf.get("speak_answer", False))
        opts_layout.addWidget(self.speak_a_cb)
        self.fallback_cb = QCheckBox("Fall back to system TTS as last resort")
        self.fallback_cb.setChecked(
            self.conf.get("fallback_to_system", True)
        )
        opts_layout.addWidget(self.fallback_cb)
        opts_group.setLayout(opts_layout)
        layout.addWidget(opts_group)

        # -- Test Voice --
        test_btn = QPushButton("Test Voice")
        test_btn.clicked.connect(self._test_voice)
        layout.addWidget(test_btn)

        # -- Buttons --
        btn_layout = QHBoxLayout()
        save_btn = QPushButton("Save")
        save_btn.clicked.connect(self._save)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addStretch()
        btn_layout.addWidget(cancel_btn)
        btn_layout.addWidget(save_btn)
        layout.addLayout(btn_layout)

    def _test_voice(self):
        """Preview the current speed setting."""
        test_conf = {
            "speed": self.speed_spin.value(),
            "fallback_to_system": self.fallback_cb.isChecked(),
        }
        engine().speak("This is a test of the Anki text to speech voice.", test_conf)

    def _save(self):
        self.conf["enabled"] = self.enabled_cb.isChecked()
        self.conf["speed"] = self.speed_spin.value()
        self.conf["speak_question"] = self.speak_q_cb.isChecked()
        self.conf["speak_answer"] = self.speak_a_cb.isChecked()
        self.conf["fallback_to_system"] = self.fallback_cb.isChecked()
        save_config(self.conf)
        _sync_prefetcher_with_config(self.conf)
        tooltip("Anki TTS settings saved")
        self.accept()


# ---------------------------------------------------------------------------
# Menu
# ---------------------------------------------------------------------------

def toggle_tts():
    conf = get_config()
    conf["enabled"] = not conf.get("enabled", True)
    save_config(conf)
    _sync_prefetcher_with_config(conf)
    state = "enabled" if conf["enabled"] else "disabled"
    tooltip(f"Anki TTS {state}")


# ---------------------------------------------------------------------------
# Background audio cache warm-up
#
# One warm job at a time. Its collection scan (render + text extraction for
# every changed card) runs off the UI thread via QueryOp; the main thread only
# enqueues the result. Profile open and sync scan incrementally, from a
# watermark committed once the worker has drained the queued clips.
# ---------------------------------------------------------------------------

_warm_lock = threading.RLock()
_warm_job: Optional[dict] = None
_warm_rescan: Optional[dict] = None
_warm_scheduled = False
_warm_schedule_token = 0
_idle_seen_during_queueing = False
_manual_notify_pending = False
_pending_watermark: Optional[tuple] = None
_quit_hook_registered = False

# Live counters for status/monitor UIs; guarded by _stats_lock.
_stats_lock = threading.Lock()
_scan_stats = {
    "total": None,
    "missing": None,
    "scanned_at": None,
    "last_scan": None,
}


def _cache_key_for_text(text: str, conf: dict) -> str:
    return speech_cache_key(
        text, TTSEngine.EDGE_VOICE, conf.get("speed", 1.5)
    )


def _warm_question_text(card) -> str:
    """Question text for warm-up/status, keyed to match live playback.

    The live reviewer hooks read the exact ``card_will_show`` HTML for a card
    when it is shown. When that HTML is available for this card, warm-up keys
    its audio off the same source so the warmed clip is found on playback
    instead of being stored under a divergent key; otherwise it falls back to
    ``render_output`` (the closest predictor of the live text).
    """
    rendered = rendered_question_html(card)
    html = get_review_html(card, REVIEW_QUESTION_CONTEXT, rendered)
    return speakable_question_text(card, html)


def _scan_signature(conf: dict) -> str:
    speed = round(float(conf.get("speed", 1.5)), 3)
    return (
        f"{TTSEngine.EDGE_VOICE}|{speed}|{CACHE_FORMAT_VERSION}|"
        f"{WARM_TEXT_VERSION}"
    )


def _watermark_meta_key(collection) -> str:
    return f"warm_watermark:{getattr(collection, 'path', '') or ''}"


def _load_watermark(collection, conf: dict) -> Optional[dict]:
    value = audio_cache_state().get_meta(_watermark_meta_key(collection))
    if not isinstance(value, dict):
        return None
    if value.get("signature") != _scan_signature(conf):
        return None
    try:
        return {"mod": int(value["mod"]), "usn": int(value["usn"])}
    except (KeyError, TypeError, ValueError):
        return None


_CHANGED_CARDS_SQL = (
    "select c.id from cards c join notes n on c.nid = n.id "
    "where c.mod > ? or n.mod > ? or c.usn > ? or n.usn > ?"
)
# Editing a note type's templates changes the question text of all its cards
# without touching the cards or notes themselves.
_CHANGED_NOTETYPE_SQL = (
    " or n.mid in (select id from notetypes where mtime_secs > ? or usn > ?)"
)


def _select_card_ids(col, since: Optional[dict]) -> list:
    if since is None:
        return list(col.find_cards(ALL_CARDS_SEARCH))
    # Local edits bump mod (seconds); synced-in changes carry a server usn
    # above anything seen at the last scan. Unsynced rows (usn -1) are NOT
    # selected by usn: for a collection that never syncs every row is -1,
    # which would turn each "incremental" scan into a full 30k-card render.
    mod, usn = since["mod"], since["usn"]
    try:
        return list(
            col.db.list(
                _CHANGED_CARDS_SQL + _CHANGED_NOTETYPE_SQL,
                mod, mod, usn, usn, mod, usn,
            )
        )
    except Exception:
        log.debug("Note type change query failed; using cards/notes only",
                  exc_info=True)
    return list(col.db.list(_CHANGED_CARDS_SQL, mod, mod, usn, usn))


def _max_usn(col) -> int:
    cards = col.db.scalar("select coalesce(max(usn), 0) from cards") or 0
    notes = col.db.scalar("select coalesce(max(usn), 0) from notes") or 0
    try:
        notetypes = (
            col.db.scalar("select coalesce(max(usn), 0) from notetypes") or 0
        )
    except Exception:
        notetypes = 0
    return max(int(cards), int(notes), int(notetypes))


def _new_scan_result(full: bool) -> dict:
    return {
        "full": full,
        "cards": 0,
        "total": 0,
        "cached": 0,
        "missing": {},
        "errors": 0,
        "aborted": False,
        "watermark": None,
    }


def _scan_begin(col, since, conf, cache, cancel_event) -> dict:
    """Background: select the cards to scan (cheap; no rendering yet)."""
    scan = {
        "started": int(time.time()),
        "max_usn": None,
        "card_ids": [],
        "next": 0,
        "cached_keys": set(),
        "done": False,
        "result": _new_scan_result(since is None),
    }
    result = scan["result"]
    if cancel_event.is_set() or not _collection_is_open(col):
        result["aborted"] = True
        return scan
    try:
        scan["max_usn"] = _max_usn(col)
    except Exception:
        log.debug("Could not read collection usn", exc_info=True)
    scan["card_ids"] = _select_card_ids(col, since)
    result["cards"] = len(scan["card_ids"])
    _warm_stats.scan_progress(0, len(scan["card_ids"]))
    scan["cached_keys"] = cache.cached_keys()
    return scan


def _scan_step(col, scan, conf, cancel_event) -> dict:
    """Background: render one bounded chunk of cards, then return.

    Anki runs every collection task on ONE executor thread, so a scan that
    renders 30k cards in a single op would queue the reviewer's own
    answer/undo ops behind it for minutes. Each chunk is short and the next
    one is queued behind whatever Anki submitted meanwhile.
    Must not touch Qt. Per-card errors (deleted cards, render failures) are
    skipped so one card never ends the scan.
    """
    result = scan["result"]
    card_ids = scan["card_ids"]
    cached_keys = scan["cached_keys"]
    index = scan["next"]
    end = min(len(card_ids), index + SCAN_CHUNK_CARDS)
    deadline = time.monotonic() + SCAN_CHUNK_MAX_SECONDS
    while index < end:
        if cancel_event.is_set() or not _collection_is_open(col):
            result["aborted"] = True
            break
        card_id = card_ids[index]
        index += 1
        try:
            card = col.get_card(card_id)
            text = _warm_question_text(card)
        except Exception:
            result["errors"] += 1
            log.debug("Skipping card %s during TTS warm-up", card_id, exc_info=True)
            text = None
        if text:
            key = _cache_key_for_text(text, conf)
            result["total"] += 1
            if key in cached_keys:
                result["cached"] += 1
            else:
                result["missing"].setdefault(key, text)
        if time.monotonic() >= deadline:
            break
    scan["next"] = index
    _warm_stats.scan_progress(index)
    if not result["aborted"] and index >= len(card_ids):
        scan["done"] = True
        if scan["max_usn"] is not None:
            result["watermark"] = {
                "mod": scan["started"] - 2,
                "usn": scan["max_usn"],
                "signature": _scan_signature(conf),
            }
    return scan


def _scan_collection_for_audio(col, since, conf, cache, cancel_event) -> dict:
    """Whole scan in one call (tools/tests); the add-on itself uses chunks."""
    scan = _scan_begin(col, since, conf, cache, cancel_event)
    while not scan["result"]["aborted"] and not scan["done"]:
        _scan_step(col, scan, conf, cancel_event)
    return scan["result"]


def _query_op_class():
    try:
        from aqt.operations import QueryOp
    except Exception:
        return None
    return QueryOp


def _run_collection_query(collection, op, on_success, on_failure):
    """Run op(col) in the background and call back on the main thread.

    No progress dialog: it would be modal and block reviewing; the monitor
    shows scan progress instead.
    """
    query_op = _query_op_class()
    if query_op is not None:
        query = query_op(parent=mw, op=op, success=on_success)
        if hasattr(query, "failure"):
            query = query.failure(on_failure)
        query.run_in_background()
        return

    def _on_done(future):
        try:
            result = future.result()
        except Exception as error:
            on_failure(error)
            return
        on_success(result)

    mw.taskman.run_in_background(lambda: op(collection), _on_done)


def _run_scan_op(job: dict, op) -> None:
    collection = job["collection"]

    def _op(col):
        # QueryOp passes whatever mw.col is when the op runs; a profile
        # switch in between must not scan (or render from) another collection.
        if col is not collection or mw.col is not collection:
            return None
        return op(col)

    try:
        _run_collection_query(
            collection,
            _op,
            lambda scan: _on_warm_scan_step(job, scan),
            lambda error: _on_warm_scan_failed(job, error),
        )
    except Exception as error:
        _on_warm_scan_failed(job, error)


def _on_warm_scan_step(job: dict, scan: Optional[dict]) -> None:
    """Main thread: queue the next chunk, or hand the result over."""
    try:
        if (
            scan is None
            or scan["result"].get("aborted")
            or not _warm_job_is_current(job)
        ):
            _finish_warm_job(job)
            return
        if scan["done"]:
            _on_warm_scan_done(job, scan["result"])
            return

        def _next_chunk():
            if not _warm_job_is_current(job):
                _finish_warm_job(job)
                return
            _run_scan_op(
                job,
                lambda col: _scan_step(col, scan, job["conf"], job["cancel"]),
            )

        # A short gap lets the UI thread and other collection ops run.
        QTimer.singleShot(SCAN_CHUNK_GAP_MS, _next_chunk)
    except Exception as error:
        _on_warm_scan_failed(job, error)


def _merge_rescan(current: Optional[dict], full: bool, manual: bool) -> dict:
    current = current or {"full": False, "manual": False}
    return {
        "full": current["full"] or full,
        "manual": current["manual"] or manual,
    }


def _warm_job_is_current(job: dict) -> bool:
    return (
        _warm_job is job
        and not job["cancel"].is_set()
        and job["worker"] is _prefetcher
        and _profile_job_is_current(job["profile_generation"], job["collection"])
    )


def _start_warm_job(full: bool = False, manual: bool = False) -> bool:
    """Scan (incrementally unless full) and queue missing question audio."""
    global _warm_job, _warm_rescan, _idle_seen_during_queueing
    conf = get_config()
    if not _prefetch_enabled(conf):
        if manual:
            _show_tooltip("TTS audio caching is disabled")
        return False
    collection = mw.col
    if collection is None or _shutting_down:
        return False

    with _warm_lock:
        if _warm_job is not None or _sync_in_progress:
            _warm_rescan = _merge_rescan(_warm_rescan, full, manual)
            if manual:
                _show_tooltip(
                    "TTS audio scan will run after the current "
                    "scan or sync finishes"
                )
            return False
        if _warm_rescan is not None:
            full = full or _warm_rescan["full"]
            manual = manual or _warm_rescan["manual"]
            _warm_rescan = None

    worker = prefetcher()
    if worker.start() is False:
        if manual:
            _show_tooltip(
                "Skipped queuing TTS audio because audio generation is still stopping"
            )
        return False

    since = None if full else _load_watermark(collection, conf)
    cache = audio_cache()
    job = {
        "profile_generation": _profile_generation,
        "collection": collection,
        "conf": conf,
        "worker": worker,
        "manual": manual,
        "full": since is None,
        "cancel": threading.Event(),
        "queued": 0,
    }
    with _warm_lock:
        _warm_job = job
        _idle_seen_during_queueing = False
    _warm_stats.begin_scan("full" if job["full"] else "incremental")
    if manual:
        _show_tooltip("Queueing TTS audio cache...")

    def _first_chunk(col):
        scan = _scan_begin(col, since, conf, cache, job["cancel"])
        if scan["result"]["aborted"]:
            return scan
        return _scan_step(col, scan, conf, job["cancel"])

    _run_scan_op(job, _first_chunk)
    return True


def _on_warm_scan_failed(job: dict, error) -> None:
    try:
        if not _warm_job_is_current(job):
            # Cancelled by sync/close: the collection going away mid-op is
            # expected and must not show up as an error.
            log.debug("Cancelled TTS audio scan ended with: %s", error)
            return
        log.warning("TTS audio warm-up scan failed: %s", error)
        _record_last_error(f"scan failed: {error}")
        _warm_stats.record_error(f"card scan failed: {error}")
        if job["manual"]:
            _show_tooltip(f"TTS audio scan failed: {error}")
    except Exception:
        log.warning("TTS audio scan failure handling failed", exc_info=True)
    finally:
        _finish_warm_job(job)


def _on_warm_scan_done(job: dict, result: dict) -> None:
    global _pending_watermark
    try:
        if not _warm_job_is_current(job) or result.get("aborted"):
            return
        if not _prefetch_enabled(get_config()):
            return
        state = audio_cache_state()
        worker = job["worker"]
        # Record coverage before enqueueing so results that arrive while we
        # are still queueing update the new counters, not stale ones.
        _record_scan_stats(result)
        items = dict(result["missing"])
        for key, text in state.retry_candidates():
            items.setdefault(key, text)

        queued = 0
        for index, (key, text) in enumerate(items.items()):
            if index % SCAN_CHECK_EVERY_ITEMS == SCAN_CHECK_EVERY_ITEMS - 1:
                if not _warm_job_is_current(job) or not _prefetch_enabled(
                    get_config()
                ):
                    break
            if not state.can_retry(key):
                continue
            # Mark before enqueue so a fast worker result can't be
            # overwritten by a late "pending"; roll back if refused.
            if not state.mark_pending(key, text):
                continue
            try:
                accepted = worker.enqueue(text, job["conf"])
            except Exception:
                log.warning("Could not queue TTS audio", exc_info=True)
                accepted = False
            if accepted:
                queued += 1
            else:
                state.cancel_pending(key)

        job["queued"] = queued
        job["completed"] = True
        with _stats_lock:
            if _scan_stats["last_scan"] is not None:
                _scan_stats["last_scan"]["queued"] = queued
        if result.get("watermark") is not None:
            _pending_watermark = (
                _watermark_meta_key(job["collection"]),
                result["watermark"],
            )
    except Exception:
        log.exception("TTS audio warm-up failed")
        _record_last_error("warm-up failed")
    finally:
        _finish_warm_job(job)


def _finish_warm_job(job: dict) -> None:
    global _warm_job, _idle_seen_during_queueing, _manual_notify_pending
    with _warm_lock:
        if _warm_job is not job:
            return
        _warm_job = None
        idle_seen = _idle_seen_during_queueing
        _warm_stats.end_scan()
        _idle_seen_during_queueing = False
        rescan = _warm_rescan is not None

    if _shutting_down:
        return
    queued = job.get("queued", 0)
    if job["manual"] and job.get("completed"):
        _show_tooltip(f"Queued {queued} cards for TTS audio caching")
        _manual_notify_pending = queued > 0
    if queued == 0 or idle_seen:
        _on_prefetch_idle_main()
    if rescan and not _sync_in_progress:
        try:
            _start_warm_job()
        except Exception:
            log.warning("Could not start queued TTS audio scan", exc_info=True)


def _cancel_running_warm_job(rescan: bool = False) -> None:
    global _warm_rescan
    with _warm_lock:
        job = _warm_job
        if job is None:
            return
        job["cancel"].set()
        if rescan:
            _warm_rescan = _merge_rescan(
                _warm_rescan, job["full"], job["manual"]
            )


def _schedule_auto_warm_all_audio_cache(delay_ms: int) -> None:
    """Schedule automatic warming after the UI has had a chance to open."""
    global _warm_scheduled, _warm_schedule_token
    conf = get_config()
    if not _prefetch_enabled(conf) or mw.col is None or _shutting_down:
        return
    profile_generation = _profile_generation
    collection = mw.col

    with _warm_lock:
        if _warm_scheduled:
            return
        _warm_scheduled = True
        _warm_schedule_token += 1
        token = _warm_schedule_token

    def _run_scheduled_warm():
        global _warm_scheduled
        with _warm_lock:
            # A stale timer (cancelled, or from a closed profile) must not
            # clear the flag of a newer schedule.
            if token != _warm_schedule_token:
                return
            _warm_scheduled = False
        if not _profile_job_is_current(profile_generation, collection):
            return
        try:
            _start_warm_job()
        except Exception:
            log.warning("Could not start TTS audio scan", exc_info=True)

    QTimer.singleShot(delay_ms, _run_scheduled_warm)


def _cancel_scheduled_auto_warm() -> None:
    global _warm_scheduled, _warm_schedule_token
    with _warm_lock:
        _warm_schedule_token += 1
        _warm_scheduled = False


def warm_all_audio_cache():
    """Rescan every card and queue missing question audio in the background."""
    _start_warm_job(full=True, manual=True)


def on_sync_will_start():
    """Stop touching the collection while a sync holds it."""
    global _sync_in_progress
    _sync_in_progress = True
    _cancel_scheduled_auto_warm()
    _cancel_running_warm_job(rescan=True)


def on_sync_did_finish():
    """Refresh missing cached audio for cards changed by the sync."""
    global _sync_in_progress
    _sync_in_progress = False
    _schedule_auto_warm_all_audio_cache(AUTO_WARM_SYNC_DELAY_MS)


# ---------------------------------------------------------------------------
# Worker callbacks (called from the prefetch thread)
# ---------------------------------------------------------------------------

def _record_last_error(message: str) -> None:
    with _stats_lock:
        last = dict(_scan_stats.get("last_scan") or {})
        last["error"] = message
        _scan_stats["last_scan"] = last


def _record_scan_stats(result: dict) -> None:
    now = int(time.time())
    missing_keys = set(result["missing"])
    with _stats_lock:
        if result["full"]:
            _scan_stats["total"] = result["total"]
            _scan_stats["missing"] = missing_keys
            _scan_stats["scanned_at"] = now
        elif _scan_stats["missing"] is not None:
            _scan_stats["missing"].update(missing_keys)
        _scan_stats["last_scan"] = {
            "kind": "full" if result["full"] else "incremental",
            "at": now,
            "cards": result["cards"],
            "speakable": result["total"],
            "missing": len(missing_keys),
            "queued": 0,
            "errors": result["errors"],
        }
        total = _scan_stats["total"]
        missing_count = (
            len(_scan_stats["missing"])
            if _scan_stats["missing"] is not None
            else None
        )
    if result["full"] and missing_count is not None:
        audio_cache_state().set_meta(
            "scan_stats",
            {"total": total, "missing": missing_count, "at": now},
        )


def _on_audio_prefetch_result(text, config, ok, error_message) -> None:
    if _shutting_down:
        return
    try:
        _warm_stats.on_result(ok, error_message or "prefetch failed", text)
    except Exception:
        log.warning("Could not record TTS monitor stats", exc_info=True)
    key = _cache_key_for_text(text, config or {})
    # Worker thread: never lazily create state here (the profile may be
    # closing and just dropped it).
    state = _audio_cache_state
    if state is None:
        return
    if ok is None:
        state.cancel_pending(key)
        return
    if ok:
        state.mark_succeeded(key, text)
        with _stats_lock:
            if _scan_stats["missing"] is not None:
                _scan_stats["missing"].discard(key)
    else:
        state.mark_failed(key, error_message or "prefetch failed", text)
    state.flush_if_due()


def _on_audio_prefetch_idle() -> None:
    if _shutting_down:
        return
    mw.taskman.run_on_main(_on_prefetch_idle_main)


def _on_prefetch_idle_main() -> None:
    """Worker drained: commit the scan watermark and notify manual runs.

    Runs via run_on_main: an exception here would surface as Anki's error
    dialog, so it is logged instead.
    """
    try:
        _prefetch_idle_main()
    except Exception:
        log.warning("TTS idle handling failed", exc_info=True)


def _prefetch_idle_main() -> None:
    global _idle_seen_during_queueing, _manual_notify_pending
    global _pending_watermark
    if _shutting_down:
        return
    with _warm_lock:
        if _warm_job is not None:
            _idle_seen_during_queueing = True
            return
    worker = _prefetcher
    if worker is not None and hasattr(worker, "status"):
        status = worker.status()
        if status.get("pending", 0) or status.get("active", 0):
            return

    if _pending_watermark is not None:
        meta_key, value = _pending_watermark
        _pending_watermark = None
        audio_cache_state().set_meta(meta_key, value)
    _flush_state_in_background()

    if _manual_notify_pending:
        _manual_notify_pending = False
        show_audio_cache_status()


def _flush_state_in_background() -> None:
    state = _audio_cache_state
    if state is None or not state.is_dirty():
        return
    # uses_collection=False: Anki's collection executor has one thread and a
    # file write must not wait behind (or delay) collection ops.
    _run_in_background(state.flush)


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

def _worker_status() -> dict:
    worker = _prefetcher
    if worker is None or not hasattr(worker, "status"):
        return {"running": False, "active": 0, "pending": 0, "paused": False}
    return worker.status()


def _worker_status_label(worker_status: Optional[dict] = None) -> str:
    status = worker_status if worker_status is not None else _worker_status()
    if status.get("paused"):
        return "paused"
    if status.get("active", 0) > 0 or status.get("pending", 0) > 0:
        return "running"
    return "idle"


def audio_cache_status_snapshot() -> dict:
    """Thread-safe O(1) snapshot of warm-up/cache/worker status."""
    with _stats_lock:
        total = _scan_stats["total"]
        missing = _scan_stats["missing"]
        missing_count = len(missing) if missing is not None else None
        scanned_at = _scan_stats["scanned_at"]
        last_scan = dict(_scan_stats["last_scan"] or {})
    stale = False
    if total is None and _audio_cache_state is not None:
        meta = _audio_cache_state.get_meta("scan_stats")
        if isinstance(meta, dict):
            try:
                total = int(meta["total"])
                missing_count = int(meta["missing"])
                scanned_at = meta.get("at")
                stale = True
            except (KeyError, TypeError, ValueError):
                total = None
    cached = None
    if total is not None and missing_count is not None:
        cached = max(0, total - missing_count)

    engine_status = {}
    if _engine is not None and hasattr(_engine, "status_snapshot"):
        engine_status = _engine.status_snapshot()
    with _warm_lock:
        job = _warm_job
        scanning = None if job is None else ("full" if job["full"] else "incremental")
    return {
        "cache": {
            "total": total,
            "cached": cached,
            "stale": stale,
            "scanned_at": scanned_at,
        },
        "worker": _worker_status(),
        "state": (
            _audio_cache_state.summary()
            if _audio_cache_state is not None
            else {"pending": 0, "failed": 0}
        ),
        "scan": {
            "running": scanning,
            "sync_in_progress": _sync_in_progress,
            "last": last_scan,
        },
        "engine": engine_status,
    }


def _format_audio_cache_status_message(snapshot: dict) -> str:
    cache = snapshot["cache"]
    worker = snapshot["worker"]
    total = cache["total"]
    cached = cache["cached"]
    detail = []
    if total is None:
        head = "Audio cache: no full scan yet (use Warm All Audio Cache)"
    else:
        missing = total - cached
        state = "complete" if missing == 0 else "incomplete"
        head = f"Audio cache {state}: {cached}/{total} cards cached"
        if missing:
            detail.append(f"{missing} missing")
        if cache["stale"]:
            detail.append("as of last full scan")
    pending = worker.get("pending", 0) + worker.get("active", 0)
    if pending:
        detail.append(f"{pending} pending")
    failed = snapshot["state"].get("failed", 0)
    if failed:
        detail.append(f"{failed} failed")
    edge = snapshot["engine"].get("edge") or {}
    if edge.get("open"):
        detail.append(f"Edge paused, retry in {int(edge['retry_in'])}s")
    if snapshot["scan"]["running"]:
        detail.append("scan running")
    detail.append(f"worker {_worker_status_label(worker)}")
    return "; ".join([head] + detail)


def _show_tooltip(msg: str):
    tooltip(msg)


def show_audio_cache_status():
    """Report cache coverage from live counters (no collection re-scan)."""
    conf = get_config()
    if not _cache_enabled(conf):
        _show_tooltip("TTS audio caching is disabled")
        return
    if mw.col is None:
        _show_tooltip("No Anki collection is open")
        return
    _show_tooltip(
        _format_audio_cache_status_message(audio_cache_status_snapshot())
    )


def _reset_scan_stats() -> None:
    with _stats_lock:
        _scan_stats.update(
            total=None, missing=None, scanned_at=None, last_scan=None
        )


def clear_audio_cache():
    """Stop background audio generation and remove cached TTS audio files."""
    global _pending_watermark
    _cancel_running_warm_job()
    if _engine is not None:
        _engine.stop()
    if not _stop_prefetcher(timeout=CLEAR_AUDIO_CACHE_STOP_TIMEOUT_SECONDS):
        tooltip(
            "Skipped clearing TTS audio cache because audio generation is still in progress"
        )
        return
    if _engine is not None:
        if (
            hasattr(_engine, "wait_for_speech")
            and not _engine.wait_for_speech(
                timeout=CLEAR_AUDIO_CACHE_STOP_TIMEOUT_SECONDS
            )
        ):
            tooltip(
                "Skipped clearing TTS audio cache because audio generation is still in progress"
            )
            return

    cache = audio_cache()
    _pending_watermark = None
    _reset_scan_stats()
    _warm_stats.clear_errors()
    audio_cache_state().clear()
    # Unlinking tens of thousands of files must not freeze the UI.
    _run_in_background(
        lambda: _remove_cached_audio_files(cache),
        _on_audio_cache_cleared,
    )


def _remove_cached_audio_files(cache) -> int:
    removed = 0
    for path in list(cache.iter_audio_files()):
        try:
            path.unlink()
        except OSError:
            continue
        removed += 1
    if hasattr(cache, "invalidate_size"):
        cache.invalidate_size()
    if hasattr(cache, "total_bytes"):
        cache.total_bytes()  # re-measure off the UI thread
    return removed


def _on_audio_cache_cleared(future) -> None:
    try:
        removed = future.result()
    except Exception as error:
        log.warning("Clearing TTS audio cache failed", exc_info=True)
        tooltip(f"Clearing TTS audio cache failed: {error}")
        return
    tooltip(f"Cleared {removed} TTS audio files")


def _run_in_background(task, on_done=None) -> None:
    """mw.taskman.run_in_background without collection access, if supported."""
    run = getattr(getattr(mw, "taskman", None), "run_in_background", None)
    if run is None:
        result = _CompletedFuture(task)
        if on_done is not None:
            on_done(result)
        return
    try:
        run(task, on_done, uses_collection=False)
    except TypeError:
        # Older Anki without the uses_collection argument.
        run(task, on_done)


class _CompletedFuture:
    def __init__(self, task):
        self._result = None
        self._error = None
        try:
            self._result = task()
        except Exception as error:
            self._error = error

    def result(self):
        if self._error is not None:
            raise self._error
        return self._result


def confirm_clear_audio_cache():
    """Ask before deleting every cached clip (menu and monitor button)."""
    try:
        from aqt.utils import askUser
    except Exception:
        askUser = None
    if askUser is not None and not askUser(
        "Delete all cached TTS audio? It will be generated again in the "
        "background, which can take a long time for large collections.",
        parent=mw,
    ):
        return
    clear_audio_cache()


# ---------------------------------------------------------------------------
# Monitor (toolbar icon + side panel)
# ---------------------------------------------------------------------------

def monitor_snapshot():
    """Cheap snapshot for the monitor timer: no disk, config or col access."""
    cache = _audio_cache
    cache_bytes = None
    if cache is not None and hasattr(cache, "known_total_bytes"):
        cache_bytes = cache.known_total_bytes()
    worker = _prefetcher
    user_paused = _user_paused
    if worker is not None and hasattr(worker, "is_paused"):
        user_paused = user_paused or worker.is_paused()
    return build_snapshot(
        audio_cache_status_snapshot(),
        _warm_stats.snapshot(),
        enabled=_monitor_conf["enabled"],
        cache_enabled=_monitor_conf["cache_enabled"],
        prefetch_enabled=_monitor_conf["prefetch_enabled"],
        user_paused=user_paused,
        cache_bytes=cache_bytes,
        cache_limit_bytes=_monitor_conf["cache_limit_bytes"],
    )


def toggle_pause_audio_cache():
    """Pause/resume background generation for this session."""
    global _user_paused
    _user_paused = not _user_paused
    worker = _prefetcher
    if worker is not None:
        if _user_paused and hasattr(worker, "pause"):
            worker.pause()
        elif not _user_paused and hasattr(worker, "resume"):
            worker.resume()
    log.info("TTS background generation %s", "paused" if _user_paused else "resumed")


def retry_failed_audio_cache():
    """Retry every failed clip now instead of waiting for its backoff."""
    state = audio_cache_state()
    count = state.retry_failures_now()
    # Legacy failures carry no text: only a full card rescan can retry them.
    full = state.has_failures_without_text()
    _warm_stats.clear_errors()
    if count == 0 and not full:
        _show_tooltip("No failed TTS audio to retry")
        return
    _start_warm_job(full=full, manual=True)


def _open_folder(path) -> None:
    try:
        from aqt.utils import openFolder
    except Exception:
        openFolder = None
    if openFolder is None:
        _show_tooltip(str(path))
        return
    openFolder(str(path))


def open_audio_cache_folder():
    _open_folder(audio_cache().root)


def open_log_file():
    path = addon_log.log_path()
    if path is None:
        _show_tooltip("The TTS log file is not available")
        return
    if not Path(path).exists():
        path = Path(path).parent
    _open_folder(path)


def show_monitor():
    monitor_ui.show_dock()


_cache_measure_for = None


def _start_cache_size_measurement() -> None:
    """Measure the cache once in the background (when the panel opens).

    Later changes are tracked incrementally by AudioCache, so the monitor
    never scans the cache directory on its refresh path.
    """
    global _cache_measure_for
    if not _monitor_conf["cache_enabled"]:
        return
    try:
        cache = audio_cache()
    except Exception:
        return
    if not hasattr(cache, "total_bytes") or not hasattr(cache, "known_total_bytes"):
        return
    if cache.known_total_bytes() is not None or _cache_measure_for is cache:
        return
    _cache_measure_for = cache

    def _done(future):
        global _cache_measure_for
        if _cache_measure_for is cache:
            _cache_measure_for = None
        try:
            future.result()
        except Exception:
            log.debug("Could not measure TTS audio cache", exc_info=True)

    try:
        _run_in_background(cache.total_bytes, _done)
    except Exception:
        _cache_measure_for = None
        log.debug("Could not measure TTS audio cache", exc_info=True)


monitor_ui.configure(
    monitor_snapshot,
    {
        "toggle_pause": toggle_pause_audio_cache,
        "warm_all": warm_all_audio_cache,
        "retry_failed": retry_failed_audio_cache,
        "clear_cache": confirm_clear_audio_cache,
        "open_cache_folder": open_audio_cache_folder,
        "open_log": open_log_file,
        "on_show": _start_cache_size_measurement,
    },
)


# ---------------------------------------------------------------------------
# Bootstrap — must wait for profile to load before accessing mw.form / config
# ---------------------------------------------------------------------------

_menu_added = False
_hooks_registered = False


def _register_quit_hook() -> None:
    global _quit_hook_registered
    if _quit_hook_registered:
        return
    signal = getattr(getattr(mw, "app", None), "aboutToQuit", None)
    if signal is None:
        return
    signal.connect(_on_app_about_to_quit)
    _quit_hook_registered = True


def _on_app_about_to_quit() -> None:
    # profile_will_close normally ran already; this is a safety net so no
    # worker keeps running network or Qt calls through interpreter shutdown.
    try:
        _shutdown_background_work()
    except Exception:
        log.warning("TTS shutdown on quit failed", exc_info=True)


def _safely(step, description: str) -> None:
    """Run one shutdown step; a failure must never abort the rest."""
    try:
        step()
    except Exception:
        log.warning("TTS shutdown: %s failed", description, exc_info=True)


def _shutdown_background_work() -> None:
    """Stop warm-up, prefetch and speech with minimal main-thread waiting.

    Every step is isolated: this runs inside profile_will_close, where an
    exception would show Anki's error dialog and skip the remaining cleanup
    (leaving threads running into quit).
    """
    global _shutting_down
    _shutting_down = True
    set_status_callback(None)
    _safely(_cancel_scheduled_auto_warm, "cancel scheduled scan")
    _safely(_cancel_running_warm_job, "cancel scan")

    worker = _prefetcher
    if worker is not None:
        _safely(lambda: _detach_prefetcher_callbacks(worker), "detach worker")
    engine_ = _engine
    if engine_ is not None and hasattr(engine_, "cancel_prefetch"):
        _safely(engine_.cancel_prefetch, "cancel prefetch")
    _safely(_stop_prefetcher_for_close, "stop worker")

    if engine_ is not None:
        _safely(engine_.stop, "stop speech")
        if hasattr(engine_, "shutdown"):
            _safely(
                lambda: _shutdown_engine(engine_, CLOSE_STOP_TIMEOUT_SECONDS),
                "stop engine",
            )
        elif hasattr(engine_, "wait_for_speech"):
            _safely(
                lambda: engine_.wait_for_speech(
                    timeout=CLOSE_STOP_TIMEOUT_SECONDS
                ),
                "wait for speech",
            )

    state = _audio_cache_state
    if state is not None:
        _safely(state.clear_pending, "clear pending")
        _safely(state.flush, "save state")


def _stop_prefetcher_for_close() -> None:
    global _prefetcher
    if not _stop_prefetcher(timeout=CLOSE_STOP_TIMEOUT_SECONDS):
        # Its request is cancelled and its callbacks are detached, so it
        # exits on its own; the next profile gets a fresh worker.
        _prefetcher = None


def _on_profile_did_open():
    global _menu_added, _hooks_registered, _shutting_down, _sync_in_progress
    _advance_profile_generation()
    _shutting_down = False
    _sync_in_progress = False
    set_status_callback(_show_status)
    _register_quit_hook()
    try:
        addon_log.setup(__name__, _addon_user_files_dir())
    except Exception:
        log.debug("Could not set up TTS log file", exc_info=True)
    conf = get_config()
    _sync_prefetcher_with_config(conf)
    if _cache_enabled(conf):
        # Globbing a 30k-file cache directory must not delay profile open.
        cache = audio_cache()
        try:
            _run_in_background(
                lambda: cache.cleanup_temp_files(
                    min_age_seconds=TEMP_FILE_MIN_AGE_SECONDS
                )
            )
        except Exception:
            log.debug("Could not clean TTS temp files", exc_info=True)
    _schedule_auto_warm_all_audio_cache(AUTO_WARM_START_DELAY_MS)

    # Only add the menu once (persists across profile switches)
    if not _menu_added:
        menu = QMenu("Anki TTS", mw)

        toggle_action = QAction("Toggle TTS", mw)
        toggle_action.setShortcut("Ctrl+Shift+T")
        toggle_action.triggered.connect(toggle_tts)
        menu.addAction(toggle_action)

        settings_action = QAction("Settings...", mw)
        settings_action.triggered.connect(lambda: SettingsDialog(mw).exec())
        menu.addAction(settings_action)

        warm_cache_action = QAction("Warm All Audio Cache", mw)
        warm_cache_action.triggered.connect(warm_all_audio_cache)
        menu.addAction(warm_cache_action)

        status_action = QAction("Audio Cache Status", mw)
        status_action.triggered.connect(show_audio_cache_status)
        menu.addAction(status_action)

        monitor_action = QAction("Show Monitor", mw)
        monitor_action.triggered.connect(show_monitor)
        menu.addAction(monitor_action)

        clear_cache_action = QAction("Clear Audio Cache", mw)
        clear_cache_action.triggered.connect(confirm_clear_audio_cache)
        menu.addAction(clear_cache_action)

        mw.form.menubar.addMenu(menu)
        _menu_added = True

    # Register reviewer hooks (flag prevents duplicates on profile re-open)
    if not _hooks_registered:
        for hook_name, callback in _registered_hooks():
            hook = getattr(gui_hooks, hook_name, None)
            if hook is not None:
                hook.append(callback)
        _hooks_registered = True

    try:
        monitor_ui.setup()
    except Exception:
        log.warning("Could not start TTS monitor", exc_info=True)


def _on_profile_will_close():
    global _engine, _engine_cache_enabled, _hooks_registered
    global _audio_cache, _audio_cache_state, _sync_in_progress
    global _pending_watermark, _manual_notify_pending
    global _warm_job, _warm_rescan
    try:
        monitor_ui.teardown()
    except Exception:
        log.debug("Could not stop TTS monitor", exc_info=True)
    _advance_profile_generation()
    try:
        _shutdown_background_work()
    except Exception:
        log.warning("TTS shutdown failed", exc_info=True)
    _warm_stats.end_scan()
    with _warm_lock:
        _warm_job = None
        _warm_rescan = None
    _pending_watermark = None
    _manual_notify_pending = False
    _sync_in_progress = False
    _reset_scan_stats()

    _engine = None
    _engine_cache_enabled = None
    _audio_cache = None
    _audio_cache_state = None

    # Remove hooks so they aren't duplicated on next profile open
    if _hooks_registered:
        for hook_name, callback in _registered_hooks():
            hook = getattr(gui_hooks, hook_name, None)
            if hook is None:
                continue
            try:
                hook.remove(callback)
            except (ValueError, AttributeError):
                pass
        clear_review_html_cache()
        _hooks_registered = False


def _registered_hooks():
    return (
        ("card_will_show", cache_review_html),
        ("reviewer_did_show_question", on_reviewer_did_show_question),
        ("reviewer_did_show_answer", on_reviewer_did_show_answer),
        ("reviewer_will_end", on_reviewer_will_end),
        ("sync_will_start", on_sync_will_start),
        ("sync_did_finish", on_sync_did_finish),
    )


gui_hooks.profile_did_open.append(_on_profile_did_open)
gui_hooks.profile_will_close.append(_on_profile_will_close)
