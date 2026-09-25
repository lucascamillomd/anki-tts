"""Pure data model for the TTS cache monitor (no aqt / Qt imports).

The worker thread, the background scan and the main thread only update a
``WarmStats`` object (one leaf lock, O(1) mutators). The UI pulls an
immutable ``MonitorSnapshot`` on a timer and turns it into toolbar HTML/JS and
panel text with the helpers below, so nothing here touches Qt or the
collection and everything can be unit-tested directly.
"""

import json
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Tuple


MAX_ERRORS = 50
MAX_ERROR_CHARS = 240
MAX_PREVIEW_CHARS = 80
RATE_WINDOW_SECONDS = 120
MIN_RATE_SAMPLES = 5


class IconState(str, Enum):
    DISABLED = "disabled"
    IDLE = "idle"
    SCANNING = "scanning"
    WARMING = "warming"
    PAUSED = "paused"
    COMPLETE = "complete"
    WARNING = "warning"
    ERROR = "error"


STATE_LABELS = {
    IconState.DISABLED: "Disabled",
    IconState.IDLE: "Idle",
    IconState.SCANNING: "Scanning cards",
    IconState.WARMING: "Generating audio",
    IconState.PAUSED: "Paused",
    IconState.COMPLETE: "Complete",
    IconState.WARNING: "Needs attention",
    IconState.ERROR: "Edge TTS unavailable",
}


def _clip(text, limit) -> str:
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


@dataclass(frozen=True)
class ErrorEntry:
    at: float
    preview: str
    error: str


@dataclass(frozen=True)
class MonitorSnapshot:
    taken_at: float = 0.0
    enabled: bool = True
    cache_enabled: bool = True
    prefetch_enabled: bool = True
    paused: bool = False           # paused by the user
    edge_waiting: bool = False     # worker holding its queue for Edge
    sync_in_progress: bool = False
    scanning: Optional[str] = None  # "full" | "incremental" | None
    scan_checked: int = 0
    scan_total_cards: Optional[int] = None
    total: Optional[int] = None    # speakable cards at the last full scan
    cached: Optional[int] = None
    totals_stale: bool = False     # totals come from a previous session
    scanned_at: Optional[float] = None
    pending: int = 0
    active: int = 0
    failed: int = 0                # failed entries awaiting retry
    current_preview: Optional[str] = None
    current_elapsed_s: Optional[float] = None
    rate_per_min: Optional[float] = None
    eta_seconds: Optional[float] = None
    completed_session: int = 0
    failed_session: int = 0
    cache_bytes: Optional[int] = None
    cache_limit_bytes: int = 0
    edge_ok: Optional[bool] = None
    edge_error: Optional[str] = None
    edge_retry_in: Optional[float] = None
    live_hits: int = 0
    live_misses: int = 0
    recent_errors: Tuple[ErrorEntry, ...] = field(default_factory=tuple)


class WarmStats:
    """Thread-safe counters shared by the worker, scan and UI.

    The lock is a leaf lock: no method calls out while holding it.
    """

    def __init__(
        self,
        now=time.monotonic,
        wall=time.time,
        window_s=RATE_WINDOW_SECONDS,
        max_errors=MAX_ERRORS,
    ):
        self._now = now
        self._wall = wall
        self._window_s = window_s
        self._lock = threading.Lock()
        self._completions = deque(maxlen=500)
        self._errors = deque(maxlen=max_errors)
        self._completed = 0
        self._failed = 0
        self._scan_kind = None
        self._scan_checked = 0
        self._scan_total = None
        self._version = 0

    # -- scan (main thread / background scan op) ----------------------------

    def begin_scan(self, kind: str, total_cards: Optional[int] = None) -> None:
        with self._lock:
            self._scan_kind = kind
            self._scan_checked = 0
            self._scan_total = total_cards
            self._version += 1

    def scan_progress(self, checked: int, total_cards: Optional[int] = None) -> None:
        with self._lock:
            if self._scan_kind is None:
                return
            self._scan_checked = int(checked)
            if total_cards is not None:
                self._scan_total = int(total_cards)
            self._version += 1

    def end_scan(self) -> None:
        with self._lock:
            if self._scan_kind is None:
                return
            self._scan_kind = None
            self._scan_checked = 0
            self._scan_total = None
            self._version += 1

    # -- worker thread -------------------------------------------------------

    def on_result(self, ok: Optional[bool], error: Optional[str] = None,
                  preview: str = "") -> None:
        if ok is None:
            return
        with self._lock:
            if ok:
                self._completed += 1
                self._completions.append(self._now())
            else:
                self._failed += 1
                self._errors.appendleft(
                    ErrorEntry(
                        at=self._wall(),
                        preview=_clip(preview, MAX_PREVIEW_CHARS),
                        error=_clip(error or "unknown error", MAX_ERROR_CHARS),
                    )
                )
            self._version += 1

    def record_error(self, error: str, preview: str = "") -> None:
        """Add a non-card error (scan failure etc.) to the recent list."""
        with self._lock:
            self._errors.appendleft(
                ErrorEntry(
                    at=self._wall(),
                    preview=_clip(preview, MAX_PREVIEW_CHARS),
                    error=_clip(error, MAX_ERROR_CHARS),
                )
            )
            self._version += 1

    def clear_errors(self) -> None:
        with self._lock:
            if self._errors:
                self._errors.clear()
                self._version += 1

    # -- readers -------------------------------------------------------------

    def rate_per_min(self) -> Optional[float]:
        with self._lock:
            return self._rate_locked(self._now())

    def _rate_locked(self, now) -> Optional[float]:
        cutoff = now - self._window_s
        recent = sum(1 for t in self._completions if t >= cutoff)
        if recent < MIN_RATE_SAMPLES:
            return None
        oldest = next(t for t in self._completions if t >= cutoff)
        span = max(now - oldest, 1.0)
        return recent * 60.0 / span

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "version": self._version,
                "completed": self._completed,
                "failed": self._failed,
                "rate_per_min": self._rate_locked(self._now()),
                "recent_errors": tuple(self._errors),
                "scan_kind": self._scan_kind,
                "scan_checked": self._scan_checked,
                "scan_total": self._scan_total,
            }


def build_snapshot(
    status: dict,
    stats: dict,
    *,
    enabled: bool = True,
    cache_enabled: bool = True,
    prefetch_enabled: bool = True,
    user_paused: bool = False,
    cache_bytes: Optional[int] = None,
    cache_limit_bytes: int = 0,
    now_mono: Optional[float] = None,
    now_wall: Optional[float] = None,
) -> MonitorSnapshot:
    """Combine the add-on status dict and WarmStats into one snapshot.

    ``status`` has the shape of ``audio_cache_status_snapshot()``; missing
    keys are tolerated so partial/fake data never breaks the UI.
    """
    status = status or {}
    stats = stats or {}
    now_mono = time.monotonic() if now_mono is None else now_mono
    now_wall = time.time() if now_wall is None else now_wall
    cache = status.get("cache") or {}
    worker = status.get("worker") or {}
    state = status.get("state") or {}
    scan = status.get("scan") or {}
    engine = status.get("engine") or {}
    edge = engine.get("edge") or {}
    live = engine.get("live") or {}

    pending = int(worker.get("pending", 0) or 0)
    active = int(worker.get("active", 0) or 0)
    rate = stats.get("rate_per_min")
    eta = None
    if rate and (pending + active) > 0:
        eta = (pending + active) / rate * 60.0

    edge_ok = None
    if edge:
        edge_ok = not edge.get("open", False)

    started = worker.get("current_started_at")
    elapsed = None
    if worker.get("current") and started is not None:
        elapsed = max(0.0, now_mono - started)

    scanning = stats.get("scan_kind") or scan.get("running")
    user_paused = bool(user_paused or worker.get("user_paused"))
    edge_waiting = bool(
        worker.get("edge_waiting",
                   worker.get("paused", False) and not user_paused)
    )

    return MonitorSnapshot(
        taken_at=now_wall,
        enabled=bool(enabled),
        cache_enabled=bool(cache_enabled),
        prefetch_enabled=bool(prefetch_enabled),
        paused=user_paused,
        edge_waiting=edge_waiting,
        sync_in_progress=bool(scan.get("sync_in_progress")),
        scanning=scanning,
        scan_checked=int(stats.get("scan_checked", 0) or 0),
        scan_total_cards=stats.get("scan_total"),
        total=cache.get("total"),
        cached=cache.get("cached"),
        totals_stale=bool(cache.get("stale")),
        scanned_at=cache.get("scanned_at"),
        pending=pending,
        active=active,
        failed=int(state.get("failed", 0) or 0),
        current_preview=(
            _clip(worker.get("current"), MAX_PREVIEW_CHARS)
            if worker.get("current") else None
        ),
        current_elapsed_s=elapsed,
        rate_per_min=rate,
        eta_seconds=eta,
        completed_session=int(stats.get("completed", 0) or 0),
        failed_session=int(stats.get("failed", 0) or 0),
        cache_bytes=cache_bytes,
        cache_limit_bytes=int(cache_limit_bytes or 0),
        edge_ok=edge_ok,
        edge_error=edge.get("last_error"),
        edge_retry_in=(edge.get("retry_in") if edge.get("open") else None),
        live_hits=int(live.get("hits", 0) or 0),
        live_misses=int(live.get("misses", 0) or 0),
        recent_errors=tuple(stats.get("recent_errors") or ()),
    )


# ---------------------------------------------------------------------------
# Derived values
# ---------------------------------------------------------------------------

def derive_icon_state(s: MonitorSnapshot) -> IconState:
    if not (s.enabled and s.cache_enabled and s.prefetch_enabled):
        return IconState.DISABLED
    if s.edge_ok is False and s.completed_session == 0 and (
        s.pending + s.active
    ) > 0:
        return IconState.ERROR
    if s.paused:
        return IconState.PAUSED
    if s.scanning:
        return IconState.SCANNING
    if s.pending + s.active > 0:
        return IconState.WARMING
    if s.failed > 0 or s.edge_ok is False:
        return IconState.WARNING
    if s.total and s.cached is not None and s.cached >= s.total:
        return IconState.COMPLETE
    return IconState.IDLE


def progress_fraction(s: MonitorSnapshot) -> Optional[float]:
    if not s.total or s.cached is None:
        return None
    return max(0.0, min(1.0, s.cached / s.total))


def scan_fraction(s: MonitorSnapshot) -> Optional[float]:
    if not s.scanning or not s.scan_total_cards:
        return None
    return max(0.0, min(1.0, s.scan_checked / s.scan_total_cards))


def format_count(n) -> str:
    return "-" if n is None else f"{int(n):,}"


def format_bytes(n) -> str:
    if n is None:
        return "-"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            if unit in ("B", "KB", "MB"):
                return f"{n:.0f} {unit}"
            return f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} GB"


def format_eta(seconds) -> str:
    if seconds is None:
        return "-"
    seconds = max(0, int(round(seconds)))
    if seconds < 60:
        return "<1m"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h" if hours else f"{days}d"


def format_rate(per_min) -> str:
    if per_min is None:
        return "-"
    if per_min < 10:
        return f"{per_min:.1f}/min"
    return f"{per_min:,.0f}/min"


def format_duration(seconds) -> str:
    if seconds is None:
        return "-"
    return f"{seconds:.1f}s"


def format_ago(then, now) -> str:
    if then is None:
        return "never"
    delta = max(0, int(now - then))
    if delta < 60:
        return "just now"
    return f"{format_eta(delta)} ago"


def progress_text(s: MonitorSnapshot) -> str:
    if not s.total or s.cached is None:
        return "No full scan yet"
    pct = 100.0 * s.cached / s.total
    return f"{s.cached:,} / {s.total:,} ({pct:.1f}%)"


def edge_text(s: MonitorSnapshot) -> str:
    if s.edge_ok is None:
        return "Not used yet"
    if s.edge_ok:
        return "OK"
    reason = s.edge_error or "unavailable"
    if s.edge_retry_in:
        return f"Unavailable: {reason} (retry in {format_eta(s.edge_retry_in)})"
    return f"Unavailable: {reason}"


def tooltip_text(s: MonitorSnapshot) -> str:
    state = derive_icon_state(s)
    parts = []
    if s.total and s.cached is not None:
        pct = int(100 * s.cached / s.total)
        parts.append(f"TTS cache {s.cached:,}/{s.total:,} ({pct}%)")
    else:
        parts.append("TTS cache")
    if state == IconState.DISABLED:
        parts.append("disabled")
    elif state == IconState.PAUSED:
        parts.append("paused")
    elif state == IconState.SCANNING:
        if s.scan_total_cards:
            parts.append(
                f"scanning {s.scan_checked:,}/{s.scan_total_cards:,} cards"
            )
        else:
            parts.append("scanning")
    elif state in (IconState.WARMING, IconState.ERROR):
        parts.append(f"{s.pending + s.active:,} queued")
        if s.rate_per_min:
            parts.append(format_rate(s.rate_per_min))
        if s.eta_seconds is not None:
            parts.append(f"ETA {format_eta(s.eta_seconds)}")
    if s.edge_ok is False:
        parts.append("Edge unavailable")
    if s.failed:
        parts.append(f"{s.failed:,} failed")
    return " - ".join(parts)


def badge_text(s: MonitorSnapshot) -> str:
    state = derive_icon_state(s)
    if state in (IconState.WARMING, IconState.SCANNING):
        frac = progress_fraction(s)
        return "" if frac is None else f"{int(frac * 100)}%"
    if state in (IconState.WARNING, IconState.ERROR):
        return "!"
    return ""


def toolbar_signature(s: MonitorSnapshot) -> tuple:
    return (derive_icon_state(s).value, badge_text(s), tooltip_text(s))


# ---------------------------------------------------------------------------
# Toolbar HTML / JS
# ---------------------------------------------------------------------------

_SPEAKER_SVG = (
    '<svg class="tts-svg" viewBox="0 0 24 24" width="18" height="18" '
    'aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2" '
    'stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M11 5 6 9H3v6h3l5 4z" fill="currentColor"/>'
    '<path d="M15.5 8.5a5 5 0 0 1 0 7"/>'
    '<path d="M18.5 5.5a9 9 0 0 1 0 13"/>'
    "</svg>"
)


def _toolbar_css(element_id: str) -> str:
    sel = f"#{element_id}"
    return (
        f"{sel}{{position:relative;display:inline-flex;align-items:center;"
        f"vertical-align:middle;color:var(--fg-subtle,#888)}}"
        f"{sel} .tts-svg{{display:block}}"
        f"{sel} .tts-ring{{position:absolute;left:50%;top:50%;width:24px;"
        f"height:24px;margin:-12px 0 0 -12px;border-radius:50%;"
        f"border:2px solid transparent;box-sizing:border-box;"
        f"pointer-events:none}}"
        f"{sel} .tts-ring.tts-spin{{border-top-color:currentColor;"
        f"animation:anki-tts-spin 1s linear infinite}}"
        f"@keyframes anki-tts-spin{{to{{transform:rotate(360deg)}}}}"
        f"{sel} .tts-badge{{font-size:10px;margin-left:2px;font-weight:600}}"
        f"{sel} .tts-badge:empty{{display:none}}"
        f'{sel}[data-state="warming"],{sel}[data-state="scanning"],'
        f'{sel}[data-state="complete"]{{color:var(--state-review,#2e7d32)}}'
        f'{sel}[data-state="paused"],{sel}[data-state="warning"]'
        f"{{color:var(--state-buried,#c77c02)}}"
        f'{sel}[data-state="error"]{{color:var(--accent-danger,#d32f2f)}}'
        f'{sel}[data-state="disabled"]{{opacity:.45}}'
    )


def _is_busy(state: IconState) -> bool:
    return state in (IconState.WARMING, IconState.SCANNING)


def toolbar_link_html(cmd: str, element_id: str, s: MonitorSnapshot) -> str:
    """Our own anchor (not Toolbar.create_link: its label lands in aria-label)."""
    from html import escape

    state = derive_icon_state(s)
    tip = escape(tooltip_text(s), quote=True)
    ring_class = "tts-ring tts-spin" if _is_busy(state) else "tts-ring"
    return (
        f"<style>{_toolbar_css(element_id)}</style>"
        f'<a class="hitem" tabindex="-1" id="{escape(element_id)}" '
        f'data-state="{state.value}" aria-label="{tip}" title="{tip}" '
        f"href=\"#\" onclick=\"return pycmd('{escape(cmd)}')\">"
        f"{_SPEAKER_SVG}<span class=\"{ring_class}\"></span>"
        f'<span class="tts-badge">{escape(badge_text(s))}</span></a>'
    )


def _js_str(value: str) -> str:
    # json.dumps escapes quotes/newlines; also break "</" so the string can
    # never close a surrounding <script> element.
    return json.dumps(value).replace("</", "<\\/")


def toolbar_update_js(element_id: str, s: MonitorSnapshot) -> str:
    state = derive_icon_state(s)
    tip = tooltip_text(s)
    return (
        "(function(){"
        f"var el=document.getElementById({_js_str(element_id)});"
        "if(!el)return;"
        # Other add-ons (e.g. Anki Terminator) insert toolbar links at fixed
        # indexes after our hook runs; re-anchor the icon right after Sync.
        "var s=document.getElementById('sync');"
        "if(s&&s.nextElementSibling!==el)s.after(el);"
        f"el.dataset.state={_js_str(state.value)};"
        f"el.title={_js_str(tip)};"
        f"el.setAttribute('aria-label',{_js_str(tip)});"
        "var b=el.querySelector('.tts-badge');"
        f"if(b)b.textContent={_js_str(badge_text(s))};"
        "var r=el.querySelector('.tts-ring');"
        f"if(r)r.classList.toggle('tts-spin',{'true' if _is_busy(state) else 'false'});"
        "})();"
    )
