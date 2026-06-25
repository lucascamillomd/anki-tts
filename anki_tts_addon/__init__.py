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
import os
import threading
from pathlib import Path

from .audio_cache import AudioCache, speech_cache_key
from .audio_cache_state import AudioCacheState
from .audio_prefetch import AudioPrefetcher
from .card_text import speakable_answer_text, speakable_question_text
from .tts_engine import TTSEngine, set_status_callback
from .review_html_cache import (
    REVIEW_ANSWER_CONTEXT,
    REVIEW_QUESTION_CONTEXT,
    cache_review_html,
    clear_review_html_cache,
    get_review_html,
)


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
AUTO_WARM_START_DELAY_MS = 5000
AUTO_WARM_SYNC_DELAY_MS = 1000
AUTO_WARM_BATCH_DELAY_MS = 100
AUTO_WARM_BATCH_SIZE = 10
MANUAL_WARM_BATCH_DELAY_MS = 25
MANUAL_WARM_BATCH_SIZE = 100
WARM_PROGRESS_INTERVAL_CARDS = 1000
STATUS_BATCH_DELAY_MS = 25
STATUS_BATCH_SIZE = 100
STATUS_PROGRESS_INTERVAL_CARDS = 1000
ALL_CARDS_SEARCH = ""

_engine: Optional[TTSEngine] = None
_engine_cache_enabled: Optional[bool] = None
_prefetcher: Optional[AudioPrefetcher] = None
_audio_cache: Optional[AudioCache] = None
_audio_cache_state: Optional[AudioCacheState] = None
_cache_status_notification_pending = False
_cache_status_queueing = False
_cache_status_idle_seen_while_queueing = False
_cache_status_lock = threading.Lock()
_auto_warm_scheduled = False
_auto_warm_lock = threading.Lock()
_profile_generation = 0


def _advance_profile_generation() -> None:
    global _profile_generation
    _profile_generation += 1


def _profile_job_is_current(profile_generation: int, collection) -> bool:
    return (
        profile_generation == _profile_generation
        and collection is not None
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


def _stop_prefetcher(timeout=None) -> bool:
    global _prefetcher
    if _prefetcher is None:
        return True

    if not _prefetcher.stop(timeout=timeout):
        return False

    _prefetcher = None
    return True


def prefetcher() -> AudioPrefetcher:
    global _prefetcher
    if _prefetcher is None:
        _prefetcher = AudioPrefetcher(engine())
        if hasattr(_prefetcher, "set_idle_callback"):
            _prefetcher.set_idle_callback(_on_audio_prefetch_idle)
        if hasattr(_prefetcher, "set_result_callback"):
            _prefetcher.set_result_callback(_on_audio_prefetch_result)
    return _prefetcher


def _sync_prefetcher_with_config(conf: dict) -> bool:
    if _prefetch_enabled(conf):
        return prefetcher().start() is not False

    if _engine is not None:
        _engine.stop()
    return _stop_prefetcher(timeout=PREFETCH_STOP_TIMEOUT_SECONDS)


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
        if not _stop_prefetcher(timeout=PREFETCH_STOP_TIMEOUT_SECONDS):
            return _engine
        if (
            hasattr(_engine, "wait_for_speech")
            and not _engine.wait_for_speech(
                timeout=PREFETCH_STOP_TIMEOUT_SECONDS
            )
        ):
            return _engine
        _engine = TTSEngine(
            audio_cache=_configured_audio_cache(conf),
            cache_max_bytes=_cache_max_bytes(conf),
        )
        _engine_cache_enabled = cache_enabled
    elif hasattr(_engine, "set_cache_max_bytes"):
        _engine.set_cache_max_bytes(_cache_max_bytes(conf))
    return _engine


def _show_status(msg: str):
    """Show a tooltip on the main thread (safe from background threads)."""
    mw.taskman.run_on_main(lambda: tooltip(msg))


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
        engine().stop()
        return

    question_html = get_review_html(
        card, REVIEW_QUESTION_CONTEXT, card.question()
    )
    text = speakable_question_text(card, question_html)
    _speak_text(text, conf)


def on_reviewer_did_show_answer(card) -> None:
    """Read the answer aloud if configured to do so."""
    conf = get_config()
    if not conf.get("enabled", True) or not conf.get("speak_answer", False):
        return

    answer_html = get_review_html(card, REVIEW_ANSWER_CONTEXT, card.answer())
    text = speakable_answer_text(card, answer_html)
    _speak_text(text, conf)


def on_reviewer_will_end() -> None:
    """Stop TTS when leaving the reviewer."""
    clear_review_html_cache()
    engine().stop()


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


def _begin_cache_status_notification() -> None:
    global _cache_status_notification_pending
    global _cache_status_queueing
    global _cache_status_idle_seen_while_queueing
    with _cache_status_lock:
        _cache_status_notification_pending = True
        _cache_status_queueing = True
        _cache_status_idle_seen_while_queueing = False


def _finish_cache_status_queueing(queued: int) -> None:
    global _cache_status_notification_pending
    global _cache_status_queueing
    global _cache_status_idle_seen_while_queueing
    should_notify = False
    with _cache_status_lock:
        if queued == 0:
            _cache_status_notification_pending = False
        _cache_status_queueing = False
        should_notify = (
            _cache_status_notification_pending
            and _cache_status_idle_seen_while_queueing
        )
        _cache_status_idle_seen_while_queueing = False

    if should_notify:
        _on_audio_prefetch_idle()


def _reset_cache_status_notification() -> None:
    global _cache_status_notification_pending
    global _cache_status_queueing
    global _cache_status_idle_seen_while_queueing
    with _cache_status_lock:
        _cache_status_notification_pending = False
        _cache_status_queueing = False
        _cache_status_idle_seen_while_queueing = False


def _on_audio_prefetch_idle() -> None:
    global _cache_status_notification_pending
    global _cache_status_idle_seen_while_queueing
    with _cache_status_lock:
        if not _cache_status_notification_pending:
            return
        if _cache_status_queueing:
            _cache_status_idle_seen_while_queueing = True
            return
        _cache_status_notification_pending = False

    mw.taskman.run_on_main(show_audio_cache_status)


def _cache_key_for_text(text: str, conf: dict) -> str:
    return speech_cache_key(
        text, TTSEngine.EDGE_VOICE, conf.get("speed", 1.5)
    )


def _on_audio_prefetch_result(text, config, ok, error_message) -> None:
    if ok is None:
        return
    key = _cache_key_for_text(text, config or {})
    if ok:
        audio_cache_state().mark_succeeded(key, text)
    else:
        audio_cache_state().mark_failed(
            key, error_message or "prefetch failed", text
        )


def _audio_cache_counts() -> tuple:
    conf = get_config()
    cache = audio_cache()
    state = audio_cache_state()
    total = 0
    cached = 0
    pending = 0
    failed = 0

    for card_id in mw.col.find_cards(ALL_CARDS_SEARCH):
        card = mw.col.get_card(card_id)
        card_cached, card_total, card_pending, card_failed = (
            _audio_cache_counts_for_card(card, conf, cache, state)
        )
        cached += card_cached
        total += card_total
        pending += card_pending
        failed += card_failed

    return cached, total, pending, failed


def _audio_cache_counts_for_card(card, conf, cache, state) -> tuple:
    text = speakable_question_text(card)
    if not text:
        return 0, 0, 0, 0

    key = _cache_key_for_text(text, conf)
    if cache.has(key):
        return 1, 1, 0, 0

    entry = state.entry(key) or {}
    if entry.get("status") == "pending":
        return 0, 1, 1, 0
    if entry.get("status") == "failed":
        return 0, 1, 0, 1
    return 0, 1, 0, 0


def _worker_status_label() -> str:
    if _prefetcher is None or not hasattr(_prefetcher, "status"):
        return "idle"

    status = _prefetcher.status()
    if status.get("active", 0) > 0 or status.get("pending", 0) > 0:
        return "running"
    return "idle"


def _audio_cache_status_message() -> str:
    conf = get_config()
    if not _cache_enabled(conf):
        return "TTS audio caching is disabled"
    if mw.col is None:
        return "No Anki collection is open"

    cached, total, pending, failed = _audio_cache_counts()
    return _format_audio_cache_status_message(cached, total, pending, failed)


def _format_audio_cache_status_message(cached, total, pending, failed) -> str:
    missing = total - cached
    worker = _worker_status_label()
    if missing == 0:
        return f"Audio cache complete: {cached}/{total} cards cached; worker {worker}"
    detail = [f"{missing} missing"]
    if pending:
        detail.append(f"{pending} pending")
    if failed:
        detail.append(f"{failed} failed")
    return (
        f"Audio cache incomplete: {cached}/{total} cards cached; "
        f"{'; '.join(detail)}; worker {worker}"
    )


def _show_tooltip(msg: str):
    tooltip(msg)


def show_audio_cache_status():
    """Report whether all speakable question audio is cached."""
    conf = get_config()
    if not _cache_enabled(conf):
        _show_tooltip("TTS audio caching is disabled")
        return
    if mw.col is None:
        _show_tooltip("No Anki collection is open")
        return

    _show_tooltip("Checking TTS audio cache status...")
    profile_generation = _profile_generation
    collection = mw.col
    QTimer.singleShot(
        0,
        lambda: _show_audio_cache_status_batched(
            conf, profile_generation, collection
        ),
    )


def _show_audio_cache_status_batched(
    conf: dict, profile_generation: int, collection
):
    if not _profile_job_is_current(profile_generation, collection):
        return

    cache = audio_cache()
    state = audio_cache_state()
    card_ids = list(collection.find_cards(ALL_CARDS_SEARCH))
    job = {
        "index": 0,
        "cached": 0,
        "total": 0,
        "pending": 0,
        "failed": 0,
        "last_progress_index": 0,
    }

    def _process_next_batch():
        if not _profile_job_is_current(profile_generation, collection):
            return

        processed = 0
        batch_size = max(1, int(STATUS_BATCH_SIZE))
        while processed < batch_size and job["index"] < len(card_ids):
            card_id = card_ids[job["index"]]
            job["index"] += 1
            processed += 1
            card = collection.get_card(card_id)
            cached, total, pending, failed = _audio_cache_counts_for_card(
                card, conf, cache, state
            )
            job["cached"] += cached
            job["total"] += total
            job["pending"] += pending
            job["failed"] += failed

        if job["index"] < len(card_ids):
            progress_interval = max(1, int(STATUS_PROGRESS_INTERVAL_CARDS))
            if job["index"] - job["last_progress_index"] >= progress_interval:
                job["last_progress_index"] = job["index"]
                _show_tooltip(
                    "Checking TTS audio cache: "
                    f"{job['index']}/{len(card_ids)} cards checked; "
                    f"{job['cached']}/{job['total']} speakable cached"
                )
            QTimer.singleShot(STATUS_BATCH_DELAY_MS, _process_next_batch)
            return

        _show_tooltip(
            _format_audio_cache_status_message(
                job["cached"],
                job["total"],
                job["pending"],
                job["failed"],
            )
        )

    _process_next_batch()


def _queue_audio_cache_card(card, conf, worker, cache, state) -> bool:
    text = speakable_question_text(card)
    if not text:
        return False

    key = _cache_key_for_text(text, conf)
    if cache.has(key):
        state.mark_succeeded(key, text)
        return False
    if not state.can_retry(key):
        return False
    if worker.enqueue(text, conf):
        state.mark_pending(key, text)
        return True
    return False


def _queue_audio_cache(
    search_query: str, show_tooltip: bool, notify_on_idle: bool = False
) -> int:
    """Queue question audio generation without card access off-thread."""
    conf = get_config()
    if not _prefetch_enabled(conf):
        if show_tooltip:
            tooltip("TTS audio caching is disabled")
        return 0

    if mw.col is None:
        return 0

    worker = prefetcher()
    if worker.start() is False:
        if show_tooltip:
            tooltip(
                "Skipped queuing TTS audio because audio generation is still stopping"
            )
        return 0

    cache = audio_cache()
    state = audio_cache_state()
    if notify_on_idle:
        _begin_cache_status_notification()

    queued = 0
    try:
        for card_id in mw.col.find_cards(search_query):
            card = mw.col.get_card(card_id)
            if _queue_audio_cache_card(card, conf, worker, cache, state):
                queued += 1
    finally:
        if show_tooltip:
            tooltip(f"Queued {queued} cards for TTS audio caching")
        if notify_on_idle:
            _finish_cache_status_queueing(queued)

    return queued


def _queue_audio_cache_batched(
    search_query: str,
    show_tooltip: bool,
    notify_on_idle: bool = False,
    batch_size: Optional[int] = None,
    batch_delay_ms: Optional[int] = None,
) -> int:
    """Queue question audio generation in small UI-thread batches."""
    conf = get_config()
    if not _prefetch_enabled(conf):
        if show_tooltip:
            _show_tooltip("TTS audio caching is disabled")
        return 0

    if mw.col is None:
        return 0

    profile_generation = _profile_generation
    collection = mw.col
    worker = prefetcher()
    if worker.start() is False:
        if show_tooltip:
            _show_tooltip(
                "Skipped queuing TTS audio because audio generation is still stopping"
            )
        return 0

    cache = audio_cache()
    state = audio_cache_state()
    if notify_on_idle:
        _begin_cache_status_notification()
    if show_tooltip:
        _show_tooltip("Queueing TTS audio cache...")

    job = {
        "card_ids": None,
        "index": 0,
        "queued": 0,
        "last_progress_index": 0,
    }

    def _finish_job():
        if show_tooltip:
            _show_tooltip(f"Queued {job['queued']} cards for TTS audio caching")
        if notify_on_idle:
            _finish_cache_status_queueing(job["queued"])

    def _process_next_batch():
        if not _profile_job_is_current(profile_generation, collection):
            return

        if job["card_ids"] is None:
            job["card_ids"] = list(collection.find_cards(search_query))

        processed = 0
        safe_batch_size = max(
            1,
            int(
                batch_size
                if batch_size is not None
                else MANUAL_WARM_BATCH_SIZE
            ),
        )
        while (
            processed < safe_batch_size
            and job["index"] < len(job["card_ids"])
        ):
            card_id = job["card_ids"][job["index"]]
            job["index"] += 1
            processed += 1
            card = collection.get_card(card_id)
            if _queue_audio_cache_card(card, conf, worker, cache, state):
                job["queued"] += 1

        if job["index"] < len(job["card_ids"]):
            progress_interval = max(1, int(WARM_PROGRESS_INTERVAL_CARDS))
            if (
                show_tooltip
                and job["index"] - job["last_progress_index"]
                >= progress_interval
            ):
                job["last_progress_index"] = job["index"]
                _show_tooltip(
                    "Queueing TTS audio cache: "
                    f"{job['index']}/{len(job['card_ids'])} cards checked; "
                    f"{job['queued']} queued"
                )
            safe_delay_ms = (
                batch_delay_ms
                if batch_delay_ms is not None
                else MANUAL_WARM_BATCH_DELAY_MS
            )
            QTimer.singleShot(safe_delay_ms, _process_next_batch)
            return

        _finish_job()

    QTimer.singleShot(0, _process_next_batch)
    return 0


def warm_all_audio_cache():
    """Queue all-card question audio generation in the background."""
    _queue_audio_cache_batched(
        ALL_CARDS_SEARCH, show_tooltip=True, notify_on_idle=True
    )


def _schedule_auto_warm_all_audio_cache(delay_ms: int) -> None:
    """Schedule automatic warming after the UI has had a chance to open."""
    global _auto_warm_scheduled
    conf = get_config()
    if not _prefetch_enabled(conf) or mw.col is None:
        return
    profile_generation = _profile_generation
    collection = mw.col

    with _auto_warm_lock:
        if _auto_warm_scheduled:
            return
        _auto_warm_scheduled = True

    def _run_scheduled_warm():
        global _auto_warm_scheduled
        with _auto_warm_lock:
            _auto_warm_scheduled = False
        if not _profile_job_is_current(profile_generation, collection):
            return
        _auto_warm_all_audio_cache(profile_generation, collection)

    QTimer.singleShot(delay_ms, _run_scheduled_warm)


def _auto_warm_all_audio_cache(profile_generation=None, collection=None):
    """Silently keep missing cached audio warm in small UI-thread batches."""
    conf = get_config()
    if not _prefetch_enabled(conf) or mw.col is None:
        return 0
    if profile_generation is None:
        profile_generation = _profile_generation
    if collection is None:
        collection = mw.col
    if not _profile_job_is_current(profile_generation, collection):
        return 0

    worker = prefetcher()
    if worker.start() is False:
        return 0

    cache = audio_cache()
    state = audio_cache_state()
    card_ids = list(collection.find_cards(ALL_CARDS_SEARCH))
    job = {"index": 0, "queued": 0}
    _begin_cache_status_notification()

    def _finish_job():
        _finish_cache_status_queueing(job["queued"])

    def _process_next_batch():
        if not _profile_job_is_current(profile_generation, collection):
            return

        processed = 0
        while (
            processed < AUTO_WARM_BATCH_SIZE
            and job["index"] < len(card_ids)
        ):
            card_id = card_ids[job["index"]]
            job["index"] += 1
            processed += 1
            card = collection.get_card(card_id)
            if _queue_audio_cache_card(card, conf, worker, cache, state):
                job["queued"] += 1

        if job["index"] < len(card_ids):
            QTimer.singleShot(AUTO_WARM_BATCH_DELAY_MS, _process_next_batch)
            return

        _finish_job()

    _process_next_batch()
    return job["queued"]


def on_sync_did_finish():
    """Refresh missing cached audio after collection sync completes."""
    _schedule_auto_warm_all_audio_cache(AUTO_WARM_SYNC_DELAY_MS)


def warm_due_audio_cache():
    """Backward-compatible alias for older callers."""
    warm_all_audio_cache()


def clear_audio_cache():
    """Stop background audio generation and remove cached TTS audio files."""
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

    removed = 0
    for path in list(audio_cache().iter_audio_files()):
        try:
            path.unlink()
        except OSError:
            continue
        removed += 1

    audio_cache_state().clear()
    tooltip(f"Cleared {removed} TTS audio files")


# ---------------------------------------------------------------------------
# Bootstrap — must wait for profile to load before accessing mw.form / config
# ---------------------------------------------------------------------------

_menu_added = False
_hooks_registered = False


def _on_profile_did_open():
    global _menu_added, _hooks_registered
    _advance_profile_generation()
    set_status_callback(_show_status)
    conf = get_config()
    _sync_prefetcher_with_config(conf)
    if _cache_enabled(conf):
        audio_cache().cleanup_temp_files()
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

        clear_cache_action = QAction("Clear Audio Cache", mw)
        clear_cache_action.triggered.connect(clear_audio_cache)
        menu.addAction(clear_cache_action)

        mw.form.menubar.addMenu(menu)
        _menu_added = True

    # Register reviewer hooks (flag prevents duplicates on profile re-open)
    if not _hooks_registered:
        gui_hooks.card_will_show.append(cache_review_html)
        gui_hooks.reviewer_did_show_question.append(on_reviewer_did_show_question)
        gui_hooks.reviewer_did_show_answer.append(on_reviewer_did_show_answer)
        gui_hooks.reviewer_will_end.append(on_reviewer_will_end)
        if hasattr(gui_hooks, "sync_did_finish"):
            gui_hooks.sync_did_finish.append(on_sync_did_finish)
        _hooks_registered = True


def _on_profile_will_close():
    global _engine, _engine_cache_enabled, _hooks_registered
    global _audio_cache, _audio_cache_state
    global _auto_warm_scheduled
    _advance_profile_generation()
    _reset_cache_status_notification()
    with _auto_warm_lock:
        _auto_warm_scheduled = False
    prefetcher_stopped = _stop_prefetcher(
        timeout=PREFETCH_STOP_TIMEOUT_SECONDS
    )

    if _engine is not None:
        _engine.stop()
    speech_stopped = True
    if (
        _engine is not None
        and hasattr(_engine, "wait_for_speech")
    ):
        speech_stopped = _engine.wait_for_speech(
            timeout=PREFETCH_STOP_TIMEOUT_SECONDS
        )
    if prefetcher_stopped and speech_stopped:
        _engine = None
        _engine_cache_enabled = None
        _audio_cache = None
        _audio_cache_state = None

    # Remove hooks so they aren't duplicated on next profile open
    if _hooks_registered:
        gui_hooks.card_will_show.remove(cache_review_html)
        gui_hooks.reviewer_did_show_question.remove(on_reviewer_did_show_question)
        gui_hooks.reviewer_did_show_answer.remove(on_reviewer_did_show_answer)
        gui_hooks.reviewer_will_end.remove(on_reviewer_will_end)
        if hasattr(gui_hooks, "sync_did_finish"):
            gui_hooks.sync_did_finish.remove(on_sync_did_finish)
        clear_review_html_cache()
        _hooks_registered = False


gui_hooks.profile_did_open.append(_on_profile_did_open)
gui_hooks.profile_will_close.append(_on_profile_will_close)
