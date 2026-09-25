"""Toolbar icon and side panel for the TTS cache monitor (main thread only).

Background threads never call into this module. A QTimer on the main thread
pulls a cheap snapshot (no disk, no collection access) and pushes it to the
toolbar icon (only when its signature changes) and to the dock when visible.
Everything is guarded: if the toolbar web view, a hook, or a Qt class is
missing, the monitor degrades silently and the add-on keeps working.
"""

import logging
import time
from typing import Callable, Dict, Optional

from aqt import gui_hooks, mw

from .monitor_model import (
    IconState,
    MonitorSnapshot,
    STATE_LABELS,
    derive_icon_state,
    edge_text,
    format_ago,
    format_bytes,
    format_count,
    format_duration,
    format_eta,
    format_rate,
    progress_fraction,
    progress_text,
    scan_fraction,
    toolbar_link_html,
    toolbar_signature,
    toolbar_update_js,
)


log = logging.getLogger(__name__)

TOOLBAR_CMD = "anki_tts_monitor"
TOOLBAR_ELEMENT_ID = "anki-tts-monitor"
FAST_TICK_MS = 500     # dock visible, or scanning / warming
SLOW_TICK_MS = 3000    # idle with the dock hidden
DOCK_OBJECT_NAME = "AnkiTTSMonitorDock"
DOCK_TITLE = "TTS Cache"

ACTION_NAMES = (
    "toggle_pause",
    "warm_all",
    "retry_failed",
    "clear_cache",
    "open_cache_folder",
    "open_log",
)

_snapshot_provider: Optional[Callable[[], MonitorSnapshot]] = None
_actions: Dict[str, Callable[[], None]] = {}
_timer = None
_panel: Optional["MonitorPanel"] = None
_alive = False
_last_sig = None
_link_inserted = False
_tick_error_logged = False


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------

def configure(snapshot_provider, actions: Optional[dict] = None) -> None:
    global _snapshot_provider, _actions
    _snapshot_provider = snapshot_provider
    _actions = dict(actions or {})


def _snapshot() -> MonitorSnapshot:
    provider = _snapshot_provider
    if provider is None:
        return MonitorSnapshot(taken_at=time.time())
    return provider()


def _hook(name):
    return getattr(gui_hooks, name, None)


def register_toolbar_hooks() -> None:
    """Remove-then-append so re-imports never register twice."""
    for name, callback in (
        ("top_toolbar_did_init_links", on_top_toolbar_did_init_links),
        ("top_toolbar_did_redraw", on_top_toolbar_did_redraw),
    ):
        hook = _hook(name)
        if hook is None:
            continue
        try:
            hook.remove(callback)
        except (ValueError, AttributeError):
            pass
        try:
            hook.append(callback)
        except Exception:
            log.debug("Could not register %s", name, exc_info=True)


def on_top_toolbar_did_init_links(links, toolbar) -> None:
    global _link_inserted, _last_sig
    try:
        handlers = getattr(toolbar, "link_handlers", None)
        if isinstance(handlers, dict):
            handlers[TOOLBAR_CMD] = toggle_dock
        snap = _snapshot()
        html = toolbar_link_html(TOOLBAR_CMD, TOOLBAR_ELEMENT_ID, snap)
        index = next(
            (i for i, link in enumerate(links) if 'id="sync"' in link), None
        )
        if index is None:
            links.append(html)
        else:
            links.insert(index + 1, html)
        _link_inserted = True
        _last_sig = toolbar_signature(snap)
    except Exception:
        log.warning("Could not add TTS monitor toolbar icon", exc_info=True)


def on_top_toolbar_did_redraw(_toolbar=None) -> None:
    global _last_sig
    # The page was rebuilt; push state again on the next tick.
    _last_sig = None


def setup() -> None:
    """Profile opened: start the refresh timer and make sure the icon exists."""
    global _alive, _timer, _last_sig
    _alive = True
    _last_sig = None
    if _timer is None:
        try:
            from aqt.qt import QTimer

            timer = QTimer(mw)
            timer.timeout.connect(_tick)
            _timer = timer
        except Exception:
            log.debug("TTS monitor timer unavailable", exc_info=True)
            _timer = None
    if _timer is not None:
        try:
            _timer.setInterval(SLOW_TICK_MS)
            _timer.start()
        except Exception:
            log.debug("Could not start TTS monitor timer", exc_info=True)
    if not _link_inserted:
        toolbar = getattr(mw, "toolbar", None)
        draw = getattr(toolbar, "draw", None)
        if draw is not None:
            try:
                draw()
            except Exception:
                log.debug("Could not redraw toolbar", exc_info=True)
    _tick()


def teardown() -> None:
    """Profile closing: stop refreshing; the dock is hidden, not deleted."""
    global _alive, _last_sig
    _alive = False
    _last_sig = None
    if _timer is not None:
        try:
            _timer.stop()
        except Exception:
            log.debug("Could not stop TTS monitor timer", exc_info=True)
    if _panel is not None:
        try:
            _panel.dock.hide()
        except Exception:
            log.debug("Could not hide TTS monitor", exc_info=True)


def _dock_visible() -> bool:
    if _panel is None:
        return False
    try:
        return bool(_panel.dock.isVisible())
    except Exception:
        return False


def _tick() -> None:
    global _tick_error_logged
    if not _alive:
        return
    try:
        snap = _snapshot()
        push_toolbar_state(snap)
        visible = _dock_visible()
        if visible:
            _panel.render(snap)
        busy = derive_icon_state(snap) in (IconState.SCANNING, IconState.WARMING)
        if _timer is not None:
            interval = FAST_TICK_MS if (visible or busy) else SLOW_TICK_MS
            if getattr(_timer, "interval", lambda: None)() != interval:
                _timer.setInterval(interval)
        _tick_error_logged = False
    except Exception:
        if not _tick_error_logged:
            log.warning("TTS monitor refresh failed", exc_info=True)
            _tick_error_logged = True


def refresh_now() -> None:
    """Re-render right away (e.g. after a button changed state)."""
    _tick()


def push_toolbar_state(snap: MonitorSnapshot) -> None:
    global _last_sig
    if not _alive:
        return
    sig = toolbar_signature(snap)
    if sig == _last_sig:
        return
    web = getattr(getattr(mw, "toolbar", None), "web", None)
    if web is None:
        return
    try:
        web.eval(toolbar_update_js(TOOLBAR_ELEMENT_ID, snap))
    except Exception:
        log.debug("Could not update TTS toolbar icon", exc_info=True)
        return
    _last_sig = sig


def _ensure_panel() -> Optional["MonitorPanel"]:
    global _panel
    if _panel is None:
        try:
            _panel = MonitorPanel(mw, _run_action)
        except Exception:
            log.warning("Could not create TTS monitor panel", exc_info=True)
            _panel = None
    return _panel


def show_dock() -> None:
    on_show = _actions.get("on_show")
    if on_show is not None:
        try:
            on_show()
        except Exception:
            log.debug("TTS monitor on_show failed", exc_info=True)
    panel = _ensure_panel()
    if panel is None:
        return
    try:
        panel.dock.show()
        if hasattr(panel.dock, "raise_"):
            panel.dock.raise_()
    except Exception:
        log.debug("Could not show TTS monitor", exc_info=True)
        return
    panel.render(_snapshot(), force=True)
    if _alive:
        _tick()


def toggle_dock() -> None:
    if _dock_visible():
        try:
            _panel.dock.hide()
        except Exception:
            log.debug("Could not hide TTS monitor", exc_info=True)
        return
    show_dock()


def _run_action(name: str) -> None:
    action = _actions.get(name)
    if action is None:
        return
    try:
        action()
    except Exception:
        log.warning("TTS monitor action %s failed", name, exc_info=True)
    refresh_now()


# ---------------------------------------------------------------------------
# Dock panel
# ---------------------------------------------------------------------------

_ROWS = (
    ("state", "State"),
    ("queue", "Queue"),
    ("failed", "Failed"),
    ("scan", "Scan"),
    ("current", "Current"),
    ("rate", "Rate"),
    ("eta", "ETA"),
    ("session", "This session"),
    ("disk", "Disk"),
    ("edge", "Edge TTS"),
    ("live", "Review playback"),
)


class MonitorPanel:
    """QDockWidget with progress, counters, errors and action buttons."""

    def __init__(self, parent, run_action):
        from aqt.qt import (
            QDockWidget,
            QGridLayout,
            QHBoxLayout,
            QLabel,
            QListWidget,
            QProgressBar,
            QPushButton,
            Qt,
            QVBoxLayout,
            QWidget,
        )

        self._run_action = run_action
        self._last_render_key = None
        self._last_errors_key = None
        self._list_item_cls = None
        try:
            from aqt.qt import QListWidgetItem

            self._list_item_cls = QListWidgetItem
        except Exception:
            pass

        dock = QDockWidget(DOCK_TITLE, parent)
        dock.setObjectName(DOCK_OBJECT_NAME)
        self.dock = dock
        try:
            areas = Qt.DockWidgetArea
            dock.setAllowedAreas(
                areas.LeftDockWidgetArea | areas.RightDockWidgetArea
            )
        except Exception:
            pass
        try:
            feats = QDockWidget.DockWidgetFeature
            dock.setFeatures(
                feats.DockWidgetClosable
                | feats.DockWidgetMovable
                | feats.DockWidgetFloatable
            )
        except Exception:
            pass

        body = QWidget(dock)
        layout = QVBoxLayout(body)

        self.header = QLabel("")
        layout.addWidget(self.header)

        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setTextVisible(True)
        layout.addWidget(self.progress)

        grid = QGridLayout()
        self.labels = {}
        for row, (key, title) in enumerate(_ROWS):
            name = QLabel(title)
            value = QLabel("-")
            try:
                value.setWordWrap(True)
            except Exception:
                pass
            grid.addWidget(name, row, 0)
            grid.addWidget(value, row, 1)
            self.labels[key] = value
        layout.addLayout(grid)

        self.disk_bar = QProgressBar()
        self.disk_bar.setRange(0, 1000)
        self.disk_bar.setTextVisible(False)
        try:
            self.disk_bar.setMaximumHeight(6)
        except Exception:
            pass
        layout.addWidget(self.disk_bar)

        layout.addWidget(QLabel("Recent errors"))
        self.errors = QListWidget()
        layout.addWidget(self.errors)

        self.buttons = {}
        for row_actions in (
            (("toggle_pause", "Pause"), ("warm_all", "Warm all"),
             ("retry_failed", "Retry failed")),
            (("clear_cache", "Clear cache..."),
             ("open_cache_folder", "Open cache folder"),
             ("open_log", "Open log")),
        ):
            row = QHBoxLayout()
            for name, label in row_actions:
                button = QPushButton(label)
                button.clicked.connect(self._make_handler(name))
                row.addWidget(button)
                self.buttons[name] = button
            layout.addLayout(row)
        try:
            layout.addStretch()
        except Exception:
            pass

        dock.setWidget(body)
        try:
            parent.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)
        except Exception:
            log.debug("Could not dock TTS monitor", exc_info=True)
        dock.hide()

    def _make_handler(self, name):
        return lambda *_args: self._run_action(name)

    def _set(self, key, text):
        self.labels[key].setText(text)

    def render(self, snap: MonitorSnapshot, force: bool = False) -> None:
        state = derive_icon_state(snap)
        key = (snap.__class__, _render_key(snap))
        if not force and key == self._last_render_key:
            return
        self._last_render_key = key

        label = STATE_LABELS.get(state, state.value)
        if snap.totals_stale and snap.scanned_at:
            label += f" (totals from last scan, {format_ago(snap.scanned_at, snap.taken_at)})"
        self.header.setText(f"TTS audio cache: {label}")

        frac = progress_fraction(snap)
        self.progress.setValue(0 if frac is None else int(round(frac * 1000)))
        self.progress.setFormat(progress_text(snap))

        self._set("state", label)
        self._set(
            "queue",
            f"{format_count(snap.pending)} pending, {format_count(snap.active)} active",
        )
        self._set("failed", f"{format_count(snap.failed)} awaiting retry")
        if snap.scanning:
            total = format_count(snap.scan_total_cards)
            frac_scan = scan_fraction(snap)
            pct = "" if frac_scan is None else f" ({frac_scan * 100:.0f}%)"
            self._set(
                "scan",
                f"{snap.scanning}: {format_count(snap.scan_checked)}/{total} cards{pct}",
            )
        elif snap.sync_in_progress:
            self._set("scan", "waiting for sync")
        else:
            self._set(
                "scan",
                "last full scan " + format_ago(snap.scanned_at, snap.taken_at),
            )
        if snap.current_preview:
            self._set(
                "current",
                f"“{snap.current_preview}” "
                f"({format_duration(snap.current_elapsed_s)})",
            )
        else:
            self._set("current", "-")
        self._set("rate", format_rate(snap.rate_per_min))
        self._set("eta", format_eta(snap.eta_seconds))
        self._set(
            "session",
            f"{format_count(snap.completed_session)} generated, "
            f"{format_count(snap.failed_session)} failed",
        )
        limit = snap.cache_limit_bytes
        if snap.cache_bytes is None:
            self._set("disk", f"measuring... (limit {format_bytes(limit)})")
            self.disk_bar.setValue(0)
        else:
            self._set(
                "disk",
                f"{format_bytes(snap.cache_bytes)} of {format_bytes(limit)}",
            )
            ratio = snap.cache_bytes / limit if limit else 0
            self.disk_bar.setValue(int(max(0.0, min(1.0, ratio)) * 1000))
        self._set("edge", edge_text(snap))
        total_live = snap.live_hits + snap.live_misses
        if total_live:
            self._set(
                "live",
                f"{snap.live_hits:,} cached / {total_live:,} played "
                f"({100 * snap.live_hits / total_live:.0f}% instant)",
            )
        else:
            self._set("live", "-")

        self.buttons["toggle_pause"].setText("Resume" if snap.paused else "Pause")
        self._render_errors(snap)

    def _render_errors(self, snap: MonitorSnapshot) -> None:
        errors = snap.recent_errors
        errors_key = (len(errors), errors[0] if errors else None)
        if errors_key == self._last_errors_key:
            return
        self._last_errors_key = errors_key
        self.errors.clear()
        if not errors:
            self.errors.addItem("No errors this session")
            return
        for entry in errors:
            when = time.strftime("%H:%M:%S", time.localtime(entry.at))
            text = f"{when} - {entry.error}"
            if entry.preview:
                text += f" - “{entry.preview}”"
            if self._list_item_cls is not None:
                item = self._list_item_cls(text)
                try:
                    item.setToolTip(text)
                except Exception:
                    pass
                self.errors.addItem(item)
            else:
                self.errors.addItem(text)


def _render_key(snap: MonitorSnapshot) -> tuple:
    """Snapshot fields that change what the panel shows (minus the clock)."""
    elapsed = (
        None if snap.current_elapsed_s is None
        else round(snap.current_elapsed_s, 1)
    )
    ago_bucket = None if snap.scanned_at is None else int(
        (snap.taken_at - snap.scanned_at) // 60
    )
    return (
        snap.enabled, snap.cache_enabled, snap.prefetch_enabled, snap.paused,
        snap.edge_waiting, snap.sync_in_progress, snap.scanning,
        snap.scan_checked, snap.scan_total_cards, snap.total, snap.cached,
        snap.totals_stale, ago_bucket, snap.pending, snap.active, snap.failed,
        snap.current_preview, elapsed,
        None if snap.rate_per_min is None else round(snap.rate_per_min),
        None if snap.eta_seconds is None else int(snap.eta_seconds // 60),
        snap.completed_session, snap.failed_session, snap.cache_bytes,
        snap.cache_limit_bytes, snap.edge_ok, snap.edge_error,
        None if snap.edge_retry_in is None else int(snap.edge_retry_in // 60),
        snap.live_hits, snap.live_misses,
        len(snap.recent_errors),
        snap.recent_errors[0] if snap.recent_errors else None,
    )


register_toolbar_hooks()
