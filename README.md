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
- **Anki TTS → Warm All Audio Cache** — Pre-generate missing question audio for all cards in the background
- **Anki TTS → Audio Cache Status** — Count cached vs missing question audio for all speakable cards
- **Anki TTS → Clear Audio Cache** — Stop background generation and remove cached audio files

Cached audio is stored under `anki_tts_addon/user_files/audio_cache/`. Cache job state is stored in `anki_tts_addon/user_files/audio_cache_state.json` so interrupted background work can be resumed after restart. Anki preserves `user_files` across add-on upgrades, so the cache survives reinstalling a newer package. The package itself only ships `user_files/README.txt`; generated MP3/state files are excluded from builds.

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

If Anki quits before background caching finishes, pending cache work remains in the state file and is reconsidered on the next startup or sync. Stale partial `.tmp` audio files are removed on profile open. Transient Edge failures are marked failed with retry backoff, so repeated startup/sync scans do not immediately hammer the service.

If card text, voice, or speed changes, the cache key changes and new audio is generated automatically. The same all-card warm pass runs silently on profile open and after collection sync, but startup warming waits briefly and then scans cards in small UI-thread batches to avoid freezing Anki. Use **Anki TTS -> Audio Cache Status** to check `cached/total` progress plus pending/failed counts at any time. Old files can be removed with **Anki TTS -> Clear Audio Cache**.

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
  audio_prefetch.py    # Background cache warmer
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
