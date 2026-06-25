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
    -x "vendor/numpy/*"
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
