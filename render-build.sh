#!/usr/bin/env bash
# Installa dipendenze Python
pip install --upgrade pip
pip install fastapi uvicorn yt-dlp pydantic

# Installa FFmpeg (disponibile su Render)
echo "✅ Dipendenze installate"