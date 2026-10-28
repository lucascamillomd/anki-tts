#!/usr/bin/env bash
# Build the Anki TTS add-on package (.ankiaddon)
# Output: anki_tts.ankiaddon (installable via Anki -> Tools -> Add-ons -> Install from file)
set -euo pipefail

ADDON_DIR="anki_tts_addon"
OUTPUT="anki_tts.ankiaddon"

cd "$(dirname "$0")"

if [ -f "$OUTPUT" ]; then
    rm "$OUTPUT"
fi

# Bundle edge-tts and its dependencies for Anki's embedded Python. Defaults
# match current Anki for macOS (CPython 3.13, arm64); override for other
# targets, e.g. ANKI_PY_PLATFORM=macosx_10_13_x86_64 bash build_addon.sh
ANKI_PY_VERSION="${ANKI_PY_VERSION:-3.13}"
ANKI_PY_PLATFORM="${ANKI_PY_PLATFORM:-macosx_11_0_arm64}"
VENDOR_DIR="$ADDON_DIR/vendor"
# Microsoft rejects older clients with HTTP 403 (edge-tts 7.3.1 switched to a
# new Sec-MS-GEC token), so an outdated vendor/ is replaced, not reused.
# Keep in sync with EDGE_TTS_MIN_VERSION in anki_tts_addon/tts_engine.py
# and the edge-tts floor in pyproject.toml.
EDGE_TTS_MIN_VERSION="${EDGE_TTS_MIN_VERSION:-7.3.1}"

vendored_edge_version() {
    sed -n 's/^__version__ = "\(.*\)"$/\1/p' \
        "$VENDOR_DIR/edge_tts/version.py" 2>/dev/null || true
}

version_ge() {
    python3 -c 'import sys
t = lambda v: tuple(int(p) for p in v.split("."))
sys.exit(0 if t(sys.argv[1]) >= t(sys.argv[2]) else 1)' "$1" "$2" 2>/dev/null
}

CUR="$(vendored_edge_version)"
if [ -z "$CUR" ] || [ -n "${REFRESH_VENDOR:-}" ] \
    || ! version_ge "$CUR" "$EDGE_TTS_MIN_VERSION"; then
    echo "Vendoring edge-tts>=$EDGE_TTS_MIN_VERSION for CPython" \
        "$ANKI_PY_VERSION ($ANKI_PY_PLATFORM) (found: ${CUR:-none})..."
    rm -rf "$VENDOR_DIR"
    python3 -m pip install --quiet --target "$VENDOR_DIR" \
        --python-version "$ANKI_PY_VERSION" \
        --platform "$ANKI_PY_PLATFORM" \
        --implementation cp --only-binary=:all: \
        "edge-tts>=${EDGE_TTS_MIN_VERSION}"
fi
if [ ! -f "$VENDOR_DIR/edge_tts/__init__.py" ]; then
    echo "error: $VENDOR_DIR/edge_tts is missing; Edge TTS would not work." >&2
    exit 1
fi
CUR="$(vendored_edge_version)"
if ! version_ge "$CUR" "$EDGE_TTS_MIN_VERSION"; then
    echo "error: vendored edge-tts ${CUR:-unknown} is older than" \
        "$EDGE_TTS_MIN_VERSION; Microsoft would reject it." >&2
    exit 1
fi
echo "edge-tts $CUR bundled"

# Clean __pycache__ and .pyc files before packaging
find "$ADDON_DIR" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
find "$ADDON_DIR" -name "*.pyc" -delete 2>/dev/null || true

cd "$ADDON_DIR"
zip -r "../$OUTPUT" . \
    -x "*.pyc" \
    -x "*/__pycache__/*" \
    -x "user_files/audio_cache" \
    -x "user_files/audio_cache/*" \
    -x "user_files/*.mp3" \
    -x "user_files/*.json" \
    -x "user_files/*.tmp" \
    -x "user_files/*.log" \
    -x "user_files/*.log.*" \
    -x "CLAUDE.md" \
    -x "*/CLAUDE.md" \
    -x ".DS_Store" \
    -x "meta.json" \
    -x "model_downloader.py" \
    -x "voices/" \
    -x "voices/*" \
    -x "vendor/piper/" \
    -x "vendor/piper/*" \
    -x "vendor/onnxruntime/" \
    -x "vendor/onnxruntime/*" \
    -x "vendor/numpy/" \
    -x "vendor/numpy/*" \
    -x "vendor/bin/*" \
    -x "vendor/edge_playback/*"
cd ..

SIZE=$(du -h "$OUTPUT" | cut -f1)
echo ""
echo "Built: $OUTPUT ($SIZE)"
echo ""
echo "To install:"
echo "  1. Open Anki"
echo "  2. Tools -> Add-ons -> Install from file"
echo "  3. Select $OUTPUT"
echo "  4. Restart Anki"
