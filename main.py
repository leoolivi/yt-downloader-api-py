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
# Render monta i Secret Files in /etc/secrets/ (READ-ONLY)
# Dobbiamo copiarli in una directory scrivibile
SOURCE_COOKIES = Path("/etc/secrets/cookies.txt")
COOKIES_FILE = Path("./cookies.txt")

# Copia i cookies da /etc/secrets/ alla directory locale (scrivibile)
if SOURCE_COOKIES.exists():
    try:
        import shutil
        shutil.copy(SOURCE_COOKIES, COOKIES_FILE)
        logger.info(f"✅ Cookies copiati da {SOURCE_COOKIES} a {COOKIES_FILE}")
    except Exception as e:
        logger.error(f"❌ Errore nel copiare i cookies: {e}")
        # Prova a leggere direttamente (potrebbe funzionare per alcune operazioni)
        if SOURCE_COOKIES.exists():
            COOKIES_FILE = SOURCE_COOKIES
            logger.info(f"⚠️ Uso diretto di {SOURCE_COOKIES}")
elif COOKIES_FILE.exists():
    logger.info(f"✅ Cookies trovati in: {COOKIES_FILE}")
else:
    logger.warning("⚠️ Nessun cookie trovato - potrebbero esserci problemi con YouTube")

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
        # User agent Android Creator - client più permissivo
        'user_agent': 'com.google.android.youtube/19.09.37 (Linux; U; Android 13; en_US) gzip',
        'extractor_args': {
            'youtube': {
                'player_client': ['android_creator', 'ios', 'mweb'],
                'player_skip': ['webpage', 'configs'],
                'max_comments': [0],
            }
        },
        'ignoreerrors': False,
        'no_warnings': True,
        # Evita formati che richiedono OAuth
        'format': 'best[protocol^=http]',
        # Headers aggiuntivi
        'http_headers': {
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-us,en;q=0.5',
        }
    }
    
    # Aggiungi proxy se disponibile
    proxy_url = os.getenv('PROXY_URL')
    if proxy_url:
        opts['proxy'] = proxy_url
        logger.info("🌐 Usando proxy")
    
    # Usa i cookie se disponibili (ma potrebbero non bastare)
    if COOKIES_FILE.exists():
        opts['cookiefile'] = str(COOKIES_FILE)
        logger.info("🍪 Usando cookies per la richiesta")
    
    # Aggiungi po_token e visitor_data se disponibili come env vars
    po_token = os.getenv('YOUTUBE_PO_TOKEN')
    visitor_data = os.getenv('YOUTUBE_VISITOR_DATA')
    
    if po_token and visitor_data:
        opts['extractor_args']['youtube']['po_token'] = [po_token]
        opts['extractor_args']['youtube']['visitor_data'] = [visitor_data]
        logger.info("🔑 Usando po_token e visitor_data")
    
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
        # Download diretto senza conversione (FFmpeg non disponibile su Render Free)
        'format': 'bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best',
        'outtmpl': str(output_path),
        # COMMENTATO: richiede FFmpeg
        # 'postprocessors': [{
        #     'key': 'FFmpegExtractAudio',
        #     'preferredcodec': audio_format,
        #     'preferredquality': quality,
        # }],
        'progress_hooks': [lambda d: progress_hook(d, task_id)],
        'quiet': False,
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
        
        # Trova il file scaricato (può essere m4a, webm, etc)
        possible_extensions = ['m4a', 'webm', 'opus', 'mp3', 'ogg']
        output_file = None
        
        for ext in possible_extensions:
            potential_file = DOWNLOAD_DIR / f"{task_id}.{ext}"
            if potential_file.exists():
                output_file = potential_file
                break
        
        if not output_file:
            raise FileNotFoundError(f"File non trovato con nessuna estensione comune")
        
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

@app.get("/api/stream-invidious")
async def get_stream_via_invidious(url: str):
    """Stream usando Invidious (CONSIGLIATO - no cookies/proxy needed!)"""
    import httpx
    
    # Estrai video ID
    video_id = url.split('v=')[-1].split('&')[0]
    
    # Istanze Invidious pubbliche
    instances = [
        "https://inv.nadeko.net",
        "https://invidious.privacyredirect.com",
        "https://invidious.protokolla.fi",
        "https://yt.artemislena.eu",
        "https://invidious.flokinet.to",
        "https://invidious.lunar.icu",
    ]
    
    logger.info(f"🔄 Tentativo stream Invidious per: {video_id}")
    
    for instance in instances:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.get(
                    f"{instance}/api/v1/videos/{video_id}",
                    follow_redirects=True
                )
                
                if response.status_code != 200:
                    continue
                
                data = response.json()
                
                # Trova i formati audio
                audio_formats = [
                    f for f in data.get('adaptiveFormats', [])
                    if f.get('type', '').startswith('audio')
                ]
                
                if not audio_formats:
                    continue
                
                # Ordina per qualità
                audio_formats.sort(key=lambda x: int(x.get('bitrate', 0)), reverse=True)
                best = audio_formats[0]
                
                result = {
                    'stream_url': best['url'],
                    'title': data.get('title'),
                    'duration': data.get('lengthSeconds'),
                    'quality': f"{int(best.get('bitrate', 0))/1000:.0f}kbps",
                    'format': best.get('type'),
                    'instance_used': instance
                }
                
                logger.info(f"✅ Stream Invidious ottenuto da: {instance}")
                return result
                
        except Exception as e:
            logger.warning(f"⚠️ Invidious {instance} fallito: {e}")
            continue
    
    raise HTTPException(
        status_code=503, 
        detail="Tutte le istanze Invidious hanno fallito. Riprova tra qualche secondo."
    )


@app.get("/api/stream-proxy")
async def get_stream_url_via_proxy(url: str):
    """Stream usando proxy pubblici gratuiti come fallback"""
    import httpx
    
    # Lista di proxy pubblici gratuiti (cambiano spesso)
    proxies = [
        None,  # Prova prima senza proxy
        "http://proxy.toolskk.com:8080",
        "http://pubproxy.com:8080",
        "socks5://proxy.server:1080",
    ]
    
    video_id = url.split('v=')[-1].split('&')[0]
    
    for proxy in proxies:
        try:
            ydl_opts = {
                'quiet': True,
                'no_warnings': True,
                'format': 'bestaudio[ext=m4a]/bestaudio/best',
                'extractor_args': {
                    'youtube': {
                        'player_client': ['ios', 'android_creator'],
                    }
                },
            }
            
            if proxy:
                ydl_opts['proxy'] = proxy
                logger.info(f"🌐 Tentativo con proxy: {proxy}")
            
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                
                formats = info.get('formats', [])
                audio_formats = [f for f in formats if f.get('acodec') != 'none' and f.get('url')]
                
                if audio_formats:
                    audio_formats.sort(key=lambda x: x.get('abr', 0) or 0, reverse=True)
                    best = audio_formats[0]
                    
                    logger.info(f"✅ Stream ottenuto tramite proxy: {proxy or 'diretto'}")
                    return {
                        'stream_url': best['url'],
                        'title': info.get('title'),
                        'duration': info.get('duration'),
                        'quality': f"{best.get('abr', 'N/A')}kbps"
                    }
        except Exception as e:
            logger.warning(f"⚠️ Proxy {proxy} fallito: {e}")
            continue
    
    raise HTTPException(status_code=503, detail="Tutti i proxy hanno fallito")


@app.get("/api/stream")
async def get_stream_url(url: str, format: str = "audio"):
    """Ottieni l'URL diretto per lo streaming (CONSIGLIATO per Render)"""
    try:
        # Prova prima senza cookie (client iOS)
        ydl_opts_no_auth = {
            'nocheckcertificate': True,
            'quiet': True,
            'no_warnings': True,
            'format': 'bestaudio[ext=m4a]/bestaudio/best' if format == 'audio' else 'best',
            'extractor_args': {
                'youtube': {
                    'player_client': ['ios', 'android_creator'],
                    'player_skip': ['webpage'],
                }
            },
        }
        
        logger.info(f"🎵 Richiesta stream (no-auth): {url}")
        
        try:
            with yt_dlp.YoutubeDL(ydl_opts_no_auth) as ydl:
                info = ydl.extract_info(url, download=False)
                
                # Trova il miglior formato audio
                formats = info.get('formats', [])
                audio_formats = [f for f in formats if f.get('acodec') != 'none' and f.get('url')]
                
                if audio_formats:
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
                    
                    logger.info(f"✅ Stream URL generato (no-auth): {info.get('title')}")
                    return result
        except Exception as e:
            logger.warning(f"⚠️ No-auth fallito: {e}, provo con cookies...")
        
        # Fallback: prova con cookies
        ydl_opts = get_base_ydl_opts()
        ydl_opts.update({
            'format': 'bestaudio[ext=m4a]/bestaudio/best' if format == 'audio' else 'best',
            'quiet': True,
        })
        
        logger.info(f"🎵 Richiesta stream (with-auth): {url}")
        
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
            
            logger.info(f"✅ Stream URL generato (with-auth): {info.get('title')}")
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


@app.get("/debug/cookies")
async def debug_cookies():
    """Verifica lo stato dei cookies"""
    result = {
        "cookies_file_exists": COOKIES_FILE.exists(),
        "cookies_path": str(COOKIES_FILE),
    }
    
    if COOKIES_FILE.exists():
        try:
            content = COOKIES_FILE.read_text()
            lines = content.split('\n')
            result["cookies_lines"] = len(lines)
            result["cookies_first_line"] = lines[0] if lines else None
            result["cookies_valid_format"] = content.startswith('# Netscape HTTP Cookie File')
            result["cookies_size_kb"] = round(len(content) / 1024, 2)
            
            # Conta i cookie YouTube
            youtube_cookies = [l for l in lines if '.youtube.com' in l and not l.startswith('#')]
            result["youtube_cookies_count"] = len(youtube_cookies)
            
        except Exception as e:
            result["error"] = str(e)
    
    return result


@app.get("/debug/test")
async def test_youtube():
    """Test connessione YouTube e diagnostica"""
    source_cookies = Path("/etc/secrets/cookies.txt")
    
    result = {
        "cookies_loaded": COOKIES_FILE.exists(),
        "cookies_path": str(COOKIES_FILE),
        "source_cookies_exists": source_cookies.exists(),
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