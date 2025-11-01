from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, HttpUrl
import yt_dlp
import os
import uuid
from pathlib import Path
import asyncio
from typing import Optional
import logging

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="YT-DLP Audio Downloader API")

# CORS per permettere richieste da React Native
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # In produzione, specifica i domini
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Directory per i file temporanei
DOWNLOAD_DIR = Path("./downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)

# Directory per i cookie
COOKIES_FILE = Path("/etc/secrets/cookies.txt")

# Store per tracciare lo stato dei download
download_status = {}


class DownloadRequest(BaseModel):
    url: HttpUrl
    format: str = "mp3"  # mp3, m4a, opus, etc.
    quality: str = "192"  # bitrate in kbps


class DownloadResponse(BaseModel):
    task_id: str
    status: str
    message: str


def get_base_ydl_opts():
    """Opzioni base per yt-dlp ottimizzate per Render"""
    opts = {
        'nocheckcertificate': True,
        # User agent Android - più affidabile
        'user_agent': 'com.google.android.youtube/17.36.4 (Linux; U; Android 12; GB) gzip',
        'referer': 'https://www.youtube.com/',
        'extractor_args': {
            'youtube': {
                'player_client': ['android'],  # Solo Android client
            }
        },
        'ignoreerrors': False,
        'no_warnings': True,
    }
    
    # Se esiste il file cookies.txt, usalo
    if COOKIES_FILE.exists():
        opts['cookiefile'] = str(COOKIES_FILE)
        logger.info("🍪 Usando cookies per la richiesta")
    
    return opts


def progress_hook(d, task_id):
    """Hook per tracciare il progresso del download"""
    if d['status'] == 'downloading':
        download_status[task_id] = {
            'status': 'downloading',
            'progress': d.get('_percent_str', '0%'),
            'speed': d.get('_speed_str', 'N/A'),
            'eta': d.get('_eta_str', 'N/A')
        }
    elif d['status'] == 'finished':
        download_status[task_id] = {
            'status': 'processing',
            'progress': '100%',
            'message': 'Converting audio...'
        }


async def download_audio(url: str, task_id: str, audio_format: str, quality: str):
    """Funzione asincrona per scaricare l'audio"""
    output_path = DOWNLOAD_DIR / f"{task_id}.%(ext)s"
    
    ydl_opts = get_base_ydl_opts()
    ydl_opts.update({
        # Formato flessibile che funziona con Android client
        'format': 'bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best',
        'outtmpl': str(output_path),
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': audio_format,
            'preferredquality': quality,
        }],
        'progress_hooks': [lambda d: progress_hook(d, task_id)],
        'quiet': False,
        'merge_output_format': audio_format,
    })
    
    try:
        download_status[task_id] = {'status': 'starting', 'progress': '0%'}
        logger.info(f"📥 Inizio download: {url}")
        
        # Esegui il download in un thread separato per non bloccare
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            lambda: yt_dlp.YoutubeDL(ydl_opts).download([str(url)])
        )
        
        # Trova il file scaricato
        output_file = DOWNLOAD_DIR / f"{task_id}.{audio_format}"
        
        if not output_file.exists():
            raise FileNotFoundError(f"File non trovato: {output_file}")
        
        download_status[task_id] = {
            'status': 'completed',
            'progress': '100%',
            'file': str(output_file)
        }
        logger.info(f"✅ Download completato: {output_file}")
        
    except Exception as e:
        logger.error(f"❌ Errore download: {e}")
        download_status[task_id] = {
            'status': 'error',
            'message': str(e)
        }

@app.get("/api/search")
async def search_music(query: str, limit: int = 10):
    """
    Cerca solo video musicali su YouTube senza API key.
    Esempio: /api/search?query=Eminem+Lose+Yourself&limit=5
    """
    try:
        ydl_opts = get_base_ydl_opts()
        ydl_opts.update({
            'quiet': True,
            'skip_download': True,
            'extract_flat': 'in_playlist',
        })

        # 🔍 cerca esplicitamente video musicali
        search_query = f"ytsearch{limit}:{query} official music video"

        logger.info(f"🔍 Ricerca: {search_query}")

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(search_query, download=False)
            entries = info.get("entries", [])

        results = []
        for e in entries:
            if not e:
                continue
                
            title = e.get("title", "").lower()
            uploader = (e.get("uploader") or "").lower()

            # 🎵 Filtra i risultati "non musicali"
            if any(x in title for x in ["remix", "cover", "reaction", "ai cover", "parody", "mashup", "sped up", "slowed"]):
                continue
            if any(x in uploader for x in ["lyrics", "clouds", "topic", "mix"]):
                continue

            results.append({
                "title": e.get("title"),
                "url": f"https://www.youtube.com/watch?v={e.get('id')}",
                "duration": e.get("duration"),
                "uploader": e.get("uploader"),
                "thumbnail": e.get("thumbnails", [{}])[-1].get("url") if e.get("thumbnails") else None
            })

        logger.info(f"✅ Trovati {len(results)} risultati")
        return JSONResponse(content={"results": results, "count": len(results)})

    except Exception as e:
        logger.error(f"❌ Errore ricerca: {e}")
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/api/stream")
async def get_stream_url(url: str, format: str = "audio"):
    """Ottieni l'URL diretto per lo streaming (CONSIGLIATO per Render)"""
    try:
        ydl_opts = get_base_ydl_opts()
        ydl_opts.update({
            'format': 'bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best' if format == 'audio' else 'best',
            'quiet': True,
        })
        
        logger.info(f"🎵 Richiesta stream: {url}")
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            
            # Trova il miglior formato audio
            formats = info.get('formats', [])
            audio_formats = [f for f in formats if f.get('acodec') != 'none' and f.get('url')]
            
            if not audio_formats:
                raise HTTPException(status_code=404, detail="Nessun formato audio disponibile")
            
            # Ordina per qualità audio
            audio_formats.sort(key=lambda x: x.get('abr', 0) or 0, reverse=True)
            best_audio = audio_formats[0]
            
            result = {
                'stream_url': best_audio['url'],
                'title': info.get('title'),
                'duration': info.get('duration'),
                'format': best_audio.get('format_note', 'audio'),
                'quality': f"{best_audio.get('abr', 'N/A')}kbps",
                'ext': best_audio.get('ext')
            }
            
            logger.info(f"✅ Stream URL generato: {info.get('title')}")
            return result
            
    except Exception as e:
        logger.error(f"❌ Errore stream: {e}")
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/download", response_model=DownloadResponse)
async def start_download(request: DownloadRequest, background_tasks: BackgroundTasks):
    """Inizia il download dell'audio (Attenzione: Render ha filesystem effimero)"""
    task_id = str(uuid.uuid4())
    
    logger.info(f"📝 Nuovo task download: {task_id}")
    
    # Avvia il download in background
    background_tasks.add_task(
        download_audio,
        str(request.url),
        task_id,
        request.format,
        request.quality
    )
    
    return DownloadResponse(
        task_id=task_id,
        status="queued",
        message="Download started"
    )


@app.get("/api/status/{task_id}")
async def get_status(task_id: str):
    """Ottieni lo stato di un download"""
    if task_id not in download_status:
        raise HTTPException(status_code=404, detail="Task not found")
    
    return download_status[task_id]


@app.get("/api/download/{task_id}")
async def download_file(task_id: str):
    """Scarica il file completato"""
    if task_id not in download_status:
        raise HTTPException(status_code=404, detail="Task not found")
    
    status = download_status[task_id]
    
    if status['status'] != 'completed':
        raise HTTPException(status_code=400, detail="Download not completed")
    
    file_path = Path(status['file'])
    
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    
    return FileResponse(
        path=file_path,
        filename=file_path.name,
        media_type='audio/mpeg'
    )


@app.get("/api/info")
async def get_video_info(url: str):
    """Ottieni informazioni sul video senza scaricarlo"""
    try:
        ydl_opts = get_base_ydl_opts()
        ydl_opts.update({
            'quiet': True,
        })
        
        logger.info(f"ℹ️ Richiesta info: {url}")
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            
            result = {
                'title': info.get('title'),
                'duration': info.get('duration'),
                'thumbnail': info.get('thumbnail'),
                'uploader': info.get('uploader'),
                'formats_available': len(info.get('formats', []))
            }
            
            logger.info(f"✅ Info recuperate: {info.get('title')}")
            return result
            
    except Exception as e:
        logger.error(f"❌ Errore info: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/api/cleanup/{task_id}")
async def cleanup_file(task_id: str):
    """Elimina il file scaricato dal server"""
    if task_id in download_status:
        status = download_status[task_id]
        if status['status'] == 'completed':
            file_path = Path(status['file'])
            if file_path.exists():
                file_path.unlink()
                logger.info(f"🗑️ File eliminato: {file_path}")
        del download_status[task_id]
    
    return {"message": "Cleaned up successfully"}


@app.get("/debug/test")
async def test_youtube():
    """Test connessione YouTube e diagnostica"""
    result = {
        "cookies_loaded": COOKIES_FILE.exists(),
        "cookies_path": str(COOKIES_FILE),
        "download_dir_exists": DOWNLOAD_DIR.exists(),
        "secrets_dir_exists": Path("/etc/secrets/").exists(),
    }
    
    # Verifica se ci sono file in /etc/secrets/
    try:
        secrets_path = Path("/etc/secrets/")
        if secrets_path.exists():
            result["secrets_files"] = [f.name for f in secrets_path.iterdir()]
    except Exception as e:
        result["secrets_error"] = str(e)
    
    # Test connessione YouTube
    try:
        ydl_opts = get_base_ydl_opts()
        ydl_opts['quiet'] = True
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info("https://www.youtube.com/watch?v=jNQXAC9IVRw", download=False)
            
        result.update({
            "youtube_connection": "✅ OK",
            "test_video_title": info.get('title'),
            "formats_available": len(info.get('formats', [])),
            "audio_formats": len([f for f in info.get('formats', []) if f.get('acodec') != 'none']),
        })
    except Exception as e:
        result.update({
            "youtube_connection": "❌ ERROR",
            "error": str(e)
        })
    
    # Test FFmpeg
    try:
        import subprocess
        ffmpeg_version = subprocess.run(['ffmpeg', '-version'], capture_output=True, text=True)
        result['ffmpeg'] = "✅ Installato" if ffmpeg_version.returncode == 0 else "❌ Non trovato"
    except:
        result['ffmpeg'] = "❌ Non trovato"
    
    return result


@app.get("/")
async def root():
    return {
        "message": "YT-DLP Audio Downloader API",
        "status": "🚀 Running",
        "cookies": "✅ Loaded" if COOKIES_FILE.exists() else "❌ Missing",
        "endpoints": {
            "GET /api/search?query=...": "Cerca video musicali",
            "GET /api/stream?url=...": "Ottieni URL stream (CONSIGLIATO)",
            "POST /api/download": "Download audio (richiede FFmpeg)",
            "GET /api/status/{task_id}": "Stato download",
            "GET /api/info?url=...": "Info video",
            "GET /debug/test": "Test diagnostica"
        }
    }

@app.get("/keepalive")
async def keepalive():
    return {"status": "alive", "cookies": COOKIES_FILE.exists()}


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)