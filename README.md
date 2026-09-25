# Anki TTS

An Anki add-on that automatically reads card content aloud during reviews using high-quality neural text-to-speech.

## How It Works

The add-on uses a two-tier TTS fallback system:

1. **Edge TTS** (online) — Microsoft's neural voice "Ryan" (British Male). Best quality, requires internet.
2. **System TTS** (offline fallback) — macOS `say`, Linux `espeak`, or Windows SAPI.

When you review a card, the add-on automatically reads the question aloud. Edge TTS audio is cached on disk after it is generated, so repeated cards can start speaking immediately without waiting on the network. On profile open and after collection sync, the add-on delays automatic warming and then queues missing question audio in small batches so Anki can finish opening and remain responsive. If Edge TTS is unavailable for uncached text (no internet, service down), it falls back to your system voice.

## Installation

1. Build `anki_tts.ankiaddon` locally (see below).
2. Open Anki.
3. Go to **Tools → Add-ons → Install from file**.
4. Select the `.ankiaddon` file.
5. Restart Anki.

## Usage

Once installed, the add-on works automatically during reviews. Access settings from the menu bar:

- **Anki TTS → Toggle TTS** (or `Ctrl+Shift+T`) — Enable/disable TTS
- **Anki TTS → Settings...** — Open the settings dialog
- **Anki TTS → Warm All Audio Cache** — Rescan every card and pre-generate missing question audio in the background
- **Anki TTS → Audio Cache Status** — Quick tooltip with cached/total, pending and failed counts (from live counters; never rescans the collection)
- **Anki TTS → Show Monitor** — Open the TTS cache monitor panel (see below)
- **Anki TTS → Clear Audio Cache** — Asks for confirmation, stops background generation and deletes cached audio files in the background
- **Speaker icon in the top toolbar** (right of Sync) — Live cache state; click it to show or hide the monitor panel

### TTS Cache Monitor

A speaker icon sits in Anki's top toolbar, just right of **Sync**. Its colour shows the background cache state at a glance, and hovering it shows a summary such as `TTS cache 4,780/28,912 (16%) - 120 queued - 312/min - ETA 1h 20m`.

| Icon | Meaning |
|------|---------|
| Grey | Idle, nothing to do |
| Green with a spinning ring and a percentage | Scanning cards or generating audio |
| Green, no ring | Every speakable card has cached audio |
| Amber | Paused by you, or some clips failed and are waiting to retry (shown as `!`) |
| Red `!` | Edge TTS is unreachable and nothing has been generated this session |
| Faded | TTS, the audio cache or background prefetch is turned off |

Click the icon (or use **Anki TTS → Show Monitor** when the toolbar is hidden, for example during review with some themes) to open a panel on the right side of the main window. It shows:

- a progress bar of cached vs total speakable cards (totals come from the last full scan; after a restart they are marked "from last scan" until the next full scan finishes)
- pending/active queue size, clips awaiting retry, and scan progress (`full: 12,400/30,120 cards`)
- the clip being generated right now and how long it has been running
- generation rate, ETA, and this session's generated/failed counts
- disk usage against the cache size limit (measured once in the background when the panel first opens, then tracked incrementally)
- Edge TTS status, including the reason and retry time when the add-on has backed off after repeated network errors
- how many clips played during review came straight from the cache
- the last 50 errors with their real cause (for example `request timed out`) and the start of the card text

Buttons:

- **Pause / Resume** — hold background generation after the current clip (for this Anki session only; it is not saved). Quitting Anki is never blocked by a paused worker.
- **Warm all** — rescan every card and queue missing audio.
- **Retry failed** — retry failed clips now instead of waiting for their backoff (runs a full card rescan only if old failures from a previous version have no stored text).
- **Clear cache...** — delete all cached audio after confirmation; files are removed in the background.
- **Open cache folder** / **Open log** — open `user_files/audio_cache/` or the folder with `user_files/anki_tts.log`.

The panel refreshes at most twice a second while it is open or work is running, and every 3 seconds otherwise. Each refresh only reads in-memory counters: no disk scans, no config reads and no collection access, so the monitor itself cannot slow Anki down. The background worker never calls into the UI.

The add-on also writes a rotating log (`user_files/anki_tts.log`, up to 1 MB plus 2 backups) with warnings and errors from Edge TTS, the background worker and card scans. Attach it when reporting a problem.

Cached audio is stored under `anki_tts_addon/user_files/audio_cache/`. Failed clips (with their retry backoff) and the last scan position are stored in `anki_tts_addon/user_files/audio_cache_state.json`, written at most every few seconds from a background thread. Anki preserves `user_files` across add-on upgrades, so the cache survives reinstalling a newer package. The package itself only ships `user_files/README.txt`; generated MP3/state files are excluded from builds.

### Settings

| Option | Default | Description |
|--------|---------|-------------|
| Enable TTS | On | Master on/off switch |
| Speed | 1.5x | Speech rate (0.5x – 2.0x) |
| Read question | On | Speak the question when a card is shown |
| Read answer | Off | Speak the answer when revealed |
| System TTS fallback | On | Fall back to system voice as last resort |
| Audio cache | On | Store generated Edge TTS audio for faster replay |
| Background prefetch | On | Warm missing question audio without blocking review |
| Cache size limit | 2048 MB | Remove older cached audio when the cache grows beyond this limit |

### Text Processing

The add-on intelligently handles card content:

- Strips HTML tags and decodes entities
- Removes MathJax/LaTeX expressions (`\(...\)`, `\[...\]`, `$$...$$`)
- Converts Greek letters and math symbols to spoken forms (e.g., `π` → "pi")
- Replaces cloze deletions `[...]` with "bla bla bla"
- Skips image-only cards
- Caps text at 500 characters to avoid excessively long readings

### Audio Cache

Anki TTS caches generated Edge TTS MP3 files under the add-on's `user_files/audio_cache/` directory. Anki preserves `user_files/` when the add-on is upgraded, so generated audio survives reinstalling the package.

During review, the add-on plays cached audio immediately when available. If audio is missing, it generates the file with Edge TTS, stores it, and then plays it. The **Anki TTS -> Warm All Audio Cache** menu action queues every card for background generation. Existing cache files are skipped, so this is mainly work for new or changed card text. When a warm-all pass drains, the add-on reports whether the cache is complete or how many cards are still missing audio.

If Anki quits before background caching finishes, the next startup rescans the cards that were not finished. Stale partial `.tmp` audio files are removed on profile open. Transient Edge failures are marked failed with retry backoff, so repeated startup/sync scans do not immediately hammer the service.

If card text, voice, or speed changes, the cache key changes and new audio is generated automatically. The same all-card warm pass runs silently on profile open and after collection sync, but startup warming waits briefly, scans off the UI thread in small batches (so reviewing, the browser and sync never wait behind it), and only looks at cards changed since the last completed pass (including cards whose note type template was edited). Use the toolbar icon or **Anki TTS -> Show Monitor** to watch progress at any time. Old files can be removed with **Anki TTS -> Clear Audio Cache**.

## Building from Source

```bash
git clone https://github.com/lcamillo/anki-tts.git
cd anki-tts
bash build_addon.sh
```

This produces `anki_tts.ankiaddon` — install it via Anki's add-on manager.

### Prerequisites for Building

The `anki_tts_addon/vendor/` directory must contain the bundled Python dependencies for Edge TTS (for example `edge-tts`, `aiohttp`, and their dependencies) compiled for your target platform. These are not checked into git due to size.

## Project Structure

```
anki_tts_addon/
  __init__.py          # Add-on entry point, hooks, settings dialog
  tts_engine.py        # Edge TTS cache plus system fallback engine
  text_processing.py   # HTML/LaTeX stripping, symbol replacement
  config.json          # Default settings
  manifest.json        # Anki add-on metadata
  audio_cache.py       # Persistent generated-audio cache
  audio_cache_state.py # Persistent cache job/failure metadata
  audio_prefetch.py    # Background cache warmer (pause/resume, status)
  monitor_model.py     # Pure monitor data model: stats, snapshot, formatting, toolbar HTML/JS
  monitor_ui.py        # Toolbar icon, refresh timer and monitor dock (Qt, main thread only)
  addon_log.py         # Rotating log file in user_files/anki_tts.log
  card_text.py         # Shared card text extraction for live and cached audio
  user_files/          # Preserved local files; generated audio cache lives here
  vendor/              # Bundled Python dependencies
build_addon.sh         # Builds the .ankiaddon package
```

## Compatibility

- Anki 2.1.x+ (tested with Anki 25.02)
- macOS, Linux, Windows

## License

See [LICENSE](LICENSE) for details.
