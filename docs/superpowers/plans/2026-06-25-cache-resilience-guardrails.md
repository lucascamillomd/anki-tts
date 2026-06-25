# Cache Resilience Guardrails Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make background audio caching resilient to Anki quit/restart, partial generations, and transient Edge TTS failures.

**Architecture:** Keep generated MP3s as the source of truth for playback, and add a small JSON state file under `user_files/` for cache job metadata. The main-thread card scan records missing keys as pending, the worker reports success/failure, and startup cleans stale temp files before resuming missing work.

**Tech Stack:** Python standard library, Anki add-on hooks, existing `AudioCache`, `AudioPrefetcher`, and `TTSEngine` modules, `unittest` test suite run through `uv`.

---

### Task 1: Stale Temporary Audio Cleanup

**Files:**
- Modify: `anki_tts_addon/audio_cache.py`
- Modify: `anki_tts_addon/__init__.py`
- Test: `tests/test_audio_cache.py`
- Test: `tests/test_reviewer_hooks.py`

- [ ] **Step 1: Write failing cache cleanup tests**

Add tests showing `AudioCache.cleanup_temp_files()` deletes `*.tmp` files in the cache root, ignores valid `.mp3` files, and returns the count removed.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m unittest tests.test_audio_cache.AudioCacheTests.test_cleanup_temp_files_removes_only_tmp_files`

Expected: FAIL with `AttributeError: 'AudioCache' object has no attribute 'cleanup_temp_files'`.

- [ ] **Step 3: Implement cleanup**

Add `cleanup_temp_files()` to `AudioCache` and call it during `_on_profile_did_open()` before auto-warming.

- [ ] **Step 4: Verify**

Run: `uv run python -m unittest tests/test_audio_cache.py tests/test_reviewer_hooks.py`

Expected: PASS.

### Task 2: Persistent Cache Job State

**Files:**
- Create: `anki_tts_addon/audio_cache_state.py`
- Test: `tests/test_audio_cache_state.py`

- [ ] **Step 1: Write failing state-store tests**

Add tests for:
- loading missing/corrupt JSON as empty state
- marking a key pending
- marking a key succeeded
- marking a key failed with attempts, last error, and next retry time
- reporting summary counts

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m unittest tests/test_audio_cache_state.py`

Expected: FAIL because `anki_tts_addon.audio_cache_state` does not exist.

- [ ] **Step 3: Implement state store**

Create `AudioCacheState` with JSON persistence under `user_files/audio_cache_state.json`. Store only metadata, not full card text: key, short preview, status, attempts, last error, updated time, next retry time.

- [ ] **Step 4: Verify**

Run: `uv run python -m unittest tests/test_audio_cache_state.py`

Expected: PASS.

### Task 3: Worker Result Reporting and Retry Backoff

**Files:**
- Modify: `anki_tts_addon/tts_engine.py`
- Modify: `anki_tts_addon/audio_prefetch.py`
- Test: `tests/test_tts_engine_cache.py`
- Test: `tests/test_audio_prefetch.py`

- [ ] **Step 1: Write failing tests**

Add tests showing:
- `TTSEngine.prefetch()` returns `True` for cache hit/generation success and `False` when Edge is unavailable or generation fails.
- `AudioPrefetcher` calls a result callback with `ok=True` or `ok=False`.

- [ ] **Step 2: Run focused tests to verify failure**

Run: `uv run python -m unittest tests.test_tts_engine_cache.TTSEngineCacheTests.test_prefetch_returns_false_when_edge_generation_fails tests.test_audio_prefetch.AudioPrefetcherTests.test_result_callback_records_success_and_failure`

Expected: FAIL due missing return values/callback API.

- [ ] **Step 3: Implement result reporting**

Make `TTSEngine.prefetch()` return booleans and add `AudioPrefetcher.set_result_callback(callback)`. The callback receives `(text, config, ok, error_message)`.

- [ ] **Step 4: Verify**

Run: `uv run python -m unittest tests/test_tts_engine_cache.py tests/test_audio_prefetch.py`

Expected: PASS.

### Task 4: Integrate State With All-Card Warming and Status

**Files:**
- Modify: `anki_tts_addon/__init__.py`
- Modify: `tests/test_reviewer_hooks.py`
- Modify: `README.md`

- [ ] **Step 1: Write failing integration tests**

Add tests showing:
- profile open cleans stale temp files
- all-card warming marks missing keys pending
- cached keys are marked succeeded and not queued
- failed keys are skipped until `next_retry_at`
- worker result callback marks success/failure
- `Audio Cache Status` includes failed and pending counts

- [ ] **Step 2: Run focused tests to verify failure**

Run: `uv run python -m unittest tests/test_reviewer_hooks.py`

Expected: FAIL for missing state integration.

- [ ] **Step 3: Implement integration**

Add an `audio_cache_state()` singleton, wire worker result callbacks from `prefetcher()`, update `_queue_audio_cache()` to record pending/succeeded/skipped-failed keys, update status message to include pending and failed counts, and update README.

- [ ] **Step 4: Verify**

Run: `uv run python -m unittest tests/test_reviewer_hooks.py`

Expected: PASS.

### Task 5: Final Verification and Package

**Files:**
- Modify: `build_addon.sh` only if the new state file needs packaging exclusions

- [ ] **Step 1: Run full test suite**

Run: `uv run python -m unittest discover -s tests`

Expected: all tests pass.

- [ ] **Step 2: Rebuild add-on**

Run: `./build_addon.sh`

Expected: `anki_tts.ankiaddon` is rebuilt successfully.

- [ ] **Step 3: Verify archive contents**

Run: `unzip -l anki_tts.ankiaddon | rg '(^|/)(audio_cache_state\\.json|model_downloader\\.py|voices/|vendor/(piper|onnxruntime|numpy)/|CLAUDE\\.md|user_files/audio_cache)' || true`

Expected: no output.

- [ ] **Step 4: Claude CLI review attempt**

Run: `claude -p "Review the current anki-tts diff for regressions in cache resilience, retry/backoff, shutdown/resume behavior, and packaging. Return concise findings only."`

Expected: either concise findings or the known local `401 Invalid authentication credentials` failure.
