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
if [ ! -d "$VENDOR_DIR/edge_tts" ] || [ -n "${REFRESH_VENDOR:-}" ]; then
    echo "Vendoring edge-tts for CPython $ANKI_PY_VERSION ($ANKI_PY_PLATFORM)..."
    rm -rf "$VENDOR_DIR"
    python3 -m pip install --quiet --target "$VENDOR_DIR" \
        --python-version "$ANKI_PY_VERSION" \
        --platform "$ANKI_PY_PLATFORM" \
        --implementation cp --only-binary=:all: \
        "edge-tts>=7.0.0"
fi
if [ ! -f "$VENDOR_DIR/edge_tts/__init__.py" ]; then
    echo "error: $VENDOR_DIR/edge_tts is missing; Edge TTS would not work." >&2
    exit 1
fi

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
