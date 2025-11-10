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
import httpx

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
                'player_client': ['android_creator', 'ios'],
                'player_skip': ['webpage'],
            }
        },
        'ignoreerrors': False,
        'no_warnings': True,
        'http_headers': {
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-us,en;q=0.5',
        }
    }
    
    # Usa i cookie se disponibili
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

@app.get("/api/search-invidious")
async def search_via_invidious(query: str, limit: int = 10):
    """Ricerca usando Invidious (CONSIGLIATO - no blocchi IP!)"""
    import httpx
    
    instances = [
        "https://inv.nadeko.net",
        "https://yewtu.be",
        "https://invidious.privacyredirect.com",
        "https://invidious.protokolla.fi",
        "https://invidious.f5.si",
        "https://yt.artemislena.eu",
        "https://invidious.nerdvpn.de",
        "https://inv.perditum.com",
    ]
    
    logger.info(f"🔍 Ricerca Invidious: {query}")
    
    for instance in instances:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(
                    f"{instance}/api/v1/search",
                    params={
                        "q": f"{query} official music video",
                        "type": "video"
                    }
                )
                
                if response.status_code != 200:
                    continue
                
                data = response.json()
                
                results = []
                for item in data[:limit]:
                    results.append({
                        "title": item.get("title"),
                        "url": f"https://www.youtube.com/watch?v={item.get('videoId')}",
                        "duration": item.get("lengthSeconds"),
                        "uploader": item.get("author"),
                        "thumbnail": item.get("videoThumbnails", [{}])[0].get("url")
                    })
                
                logger.info(f"✅ Trovati {len(results)} risultati da {instance}")
                return JSONResponse(content={
                    "results": results,
                    "count": len(results),
                    "source": "invidious"
                })
                
        except Exception as e:
            logger.warning(f"⚠️ Invidious search {instance} fallito: {e}")
            continue
    
    raise HTTPException(status_code=503, detail="Tutte le istanze Invidious hanno fallito")


@app.get("/api/search")
async def search_music(query: str, limit: int = 10):
    """
    Cerca video musicali - usa Invidious come fallback automatico
    """
    # Prova prima con yt-dlp
    try:
        ydl_opts = get_base_ydl_opts()
        ydl_opts.update({
            'quiet': True,
            'skip_download': True,
            'extract_flat': 'in_playlist',
        })

        search_query = f"ytsearch{limit}:{query} official music video"
        logger.info(f"🔍 Ricerca yt-dlp: {search_query}")

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(search_query, download=False)
            entries = info.get("entries", [])

        results = []
        for e in entries:
            if not e:
                continue
                
            title = e.get("title", "").lower()
            uploader = (e.get("uploader") or "").lower()

            # Filtra risultati non musicali
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

        if results:
            logger.info(f"✅ Trovati {len(results)} risultati (yt-dlp)")
            return JSONResponse(content={"results": results, "count": len(results), "source": "yt-dlp"})

    except Exception as e:
        logger.warning(f"⚠️ yt-dlp fallito: {e}, uso Invidious...")

    # Fallback: usa Invidious
    return await search_via_invidious(query, limit)

@app.get("/api/stream-cloudflare")
async def get_stream_via_cloudflare(url: str):
    """Stream usando Cloudflare Worker come proxy"""
    
    # URL del tuo Cloudflare Worker
    worker_url = "https://yt-downloader-api.leolivieri1910.workers.dev"
    
    if not worker_url:
        # Invece di errore 501, prova a usare il worker direttamente
        logger.warning("⚠️ CLOUDFLARE_WORKER_URL non configurato, tentativo fallback...")
        raise HTTPException(
            status_code=503, 
            detail="Cloudflare Worker non configurato. Aggiungi CLOUDFLARE_WORKER_URL come variabile d'ambiente."
        )
    
    try:
        video_id = url.split('v=')[-1].split('&')[0]
        
        logger.info(f"🌐 Richiesta a Cloudflare Worker: {worker_url}/video/{video_id}")
        
        # Il worker fa le richieste per noi
        async with httpx.AsyncClient(timeout=30) as client:
            # Richiesta al worker per ottenere i dati del video
            response = await client.get(
                f"{worker_url}/video/{video_id}",
                follow_redirects=True
            )
            
            if response.status_code != 200:
                error_text = response.text
                logger.error(f"❌ Worker response {response.status_code}: {error_text}")
                raise HTTPException(
                    status_code=response.status_code, 
                    detail=f"Worker failed: {error_text}"
                )
            
            data = response.json()
            
            logger.info(f"✅ Stream ottenuto via Cloudflare Worker: {data.get('title', 'N/A')}")
            return {
                'stream_url': data['stream_url'],
                'title': data['title'],
                'duration': data.get('duration'),
                'quality': data.get('quality', 'N/A'),
                'source': 'cloudflare-worker'
            }
            
    except httpx.TimeoutException:
        logger.error("❌ Cloudflare Worker timeout")
        raise HTTPException(status_code=504, detail="Cloudflare Worker timeout")
    except httpx.HTTPError as e:
        logger.error(f"❌ Cloudflare Worker HTTP error: {e}")
        raise HTTPException(status_code=503, detail=f"Cloudflare Worker HTTP error: {str(e)}")
    except Exception as e:
        logger.error(f"❌ Cloudflare Worker fallito: {e}")
        raise HTTPException(status_code=503, detail=f"Cloudflare Worker error: {str(e)}")


@app.get("/api/stream-piped")
async def get_stream_via_piped(url: str):
    """Stream usando Piped API (alternativa a Invidious)"""
    
    video_id = url.split('v=')[-1].split('&')[0]
    
    # Istanze Piped pubbliche
    instances = [
        "https://pipedapi.kavin.rocks",
        "https://piped-api.garudalinux.org",
        "https://pipedapi.tokhmi.xyz",
        "https://api.piped.projectsegfau.lt",
    ]
    
    logger.info(f"🔄 Tentativo stream Piped per: {video_id}")
    
    for instance in instances:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.get(
                    f"{instance}/streams/{video_id}",
                    follow_redirects=True
                )
                
                if response.status_code != 200:
                    continue
                
                data = response.json()
                
                # Piped restituisce audioStreams
                audio_streams = data.get('audioStreams', [])
                
                if not audio_streams:
                    continue
                
                # Ordina per qualità
                audio_streams.sort(key=lambda x: int(x.get('bitrate', 0)), reverse=True)
                best = audio_streams[0]
                
                result = {
                    'stream_url': best['url'],
                    'title': data.get('title'),
                    'duration': data.get('duration'),
                    'quality': f"{best.get('bitrate', 0)/1000:.0f}kbps",
                    'format': best.get('mimeType'),
                    'source': 'piped',
                    'instance_used': instance
                }
                
                logger.info(f"✅ Stream Piped ottenuto da: {instance}")
                return result
                
        except Exception as e:
            logger.warning(f"⚠️ Piped {instance} fallito: {e}")
            continue
    
    raise HTTPException(
        status_code=503, 
        detail="Tutte le istanze Piped hanno fallito"
    )


@app.get("/api/stream-multi")
async def get_stream_multi_fallback(url: str):
    """
    Stream con fallback multipli (CONSIGLIATO!)
    Prova nell'ordine: yt-dlp → Invidious → Piped → Cloudflare (se configurato)
    """
    errors = []
    
    # 1. Prova yt-dlp (veloce ma può essere bloccato)
    try:
        logger.info("🔄 Tentativo 1/4: yt-dlp")
        result = await get_stream_url(url)
        logger.info("✅ Stream ottenuto con yt-dlp")
        return {**result, 'fallback_used': False, 'method': 'yt-dlp'}
    except Exception as e:
        errors.append({"method": "yt-dlp", "error": str(e)[:150]})
        logger.warning(f"⚠️ yt-dlp fallito: {str(e)[:100]}")
    
    # 2. Prova Invidious (affidabile)
    try:
        logger.info("🔄 Tentativo 2/4: Invidious")
        result = await get_stream_via_invidious(url)
        logger.info("✅ Stream ottenuto con Invidious")
        return {**result, 'fallback_used': True, 'method': 'invidious'}
    except Exception as e:
        errors.append({"method": "invidious", "error": str(e)[:150]})
        logger.warning(f"⚠️ Invidious fallito: {str(e)[:100]}")
    
    # 3. Prova Piped (alternativa)
    try:
        logger.info("🔄 Tentativo 3/4: Piped")
        result = await get_stream_via_piped(url)
        logger.info("✅ Stream ottenuto con Piped")
        return {**result, 'fallback_used': True, 'method': 'piped'}
    except Exception as e:
        errors.append({"method": "piped", "error": str(e)[:150]})
        logger.warning(f"⚠️ Piped fallito: {str(e)[:100]}")
    
    # 4. Prova Cloudflare Worker (se configurato)
    worker_url = os.getenv('CLOUDFLARE_WORKER_URL')
    if worker_url:
        try:
            logger.info("🔄 Tentativo 4/4: Cloudflare Worker")
            result = await get_stream_via_cloudflare(url)
            logger.info("✅ Stream ottenuto con Cloudflare Worker")
            return {**result, 'fallback_used': True, 'method': 'cloudflare'}
        except Exception as e:
            errors.append({"method": "cloudflare", "error": str(e)[:150]})
            logger.warning(f"⚠️ Cloudflare fallito: {str(e)[:100]}")
    else:
        logger.info("⏭️ Cloudflare Worker non configurato, skip")
        errors.append({"method": "cloudflare", "error": "Not configured (set CLOUDFLARE_WORKER_URL)"})
    
    # Tutti i metodi hanno fallito
    logger.error(f"❌ Tutti i metodi hanno fallito per: {url}")
    raise HTTPException(
        status_code=503,
        detail={
            "message": "Tutti i metodi disponibili hanno fallito",
            "url": url,
            "tried_methods": len(errors),
            "errors": errors
        }
    )


@app.get("/api/search-multi")
async def search_multi_fallback(query: str, limit: int = 10):
    """
    Ricerca con fallback multipli
    Prova nell'ordine: yt-dlp → Invidious → Piped
    """
    errors = []
    
    # 1. Prova yt-dlp
    try:
        logger.info("🔄 Ricerca tentativo 1: yt-dlp")
        result = await search_music(query, limit)
        return result
    except Exception as e:
        errors.append(f"yt-dlp: {str(e)[:100]}")
        logger.warning(f"⚠️ Ricerca yt-dlp fallita: {e}")
    
    # 2. Prova Invidious
    try:
        logger.info("🔄 Ricerca tentativo 2: Invidious")
        result = await search_via_invidious(query, limit)
        return result
    except Exception as e:
        errors.append(f"invidious: {str(e)[:100]}")
        logger.warning(f"⚠️ Ricerca Invidious fallita: {e}")
    
    # 3. Prova Piped
    try:
        logger.info("🔄 Ricerca tentativo 3: Piped")
        
        instances = [
            "https://pipedapi.kavin.rocks",
            "https://piped-api.garudalinux.org",
        ]
        
        for instance in instances:
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    response = await client.get(
                        f"{instance}/search",
                        params={"q": f"{query} official music video", "filter": "music_songs"}
                    )
                    
                    if response.status_code != 200:
                        continue
                    
                    data = response.json()
                    items = data.get('items', [])
                    
                    results = []
                    for item in items[:limit]:
                        if item.get('type') == 'stream':
                            results.append({
                                "title": item.get("title"),
                                "url": item.get("url"),
                                "duration": item.get("duration"),
                                "uploader": item.get("uploaderName"),
                                "thumbnail": item.get("thumbnail")
                            })
                    
                    if results:
                        logger.info(f"✅ Ricerca Piped completata: {len(results)} risultati")
                        return JSONResponse(content={
                            "results": results,
                            "count": len(results),
                            "source": "piped"
                        })
            except:
                continue
                
    except Exception as e:
        errors.append(f"piped: {str(e)[:100]}")
        logger.warning(f"⚠️ Ricerca Piped fallita: {e}")
    
    raise HTTPException(
        status_code=503,
        detail={
            "message": "Tutti i metodi di ricerca hanno fallito",
            "errors": errors
        }
    )


@app.get("/api/stream-invidious")
async def get_stream_via_invidious(url: str):
    """Stream usando Invidious (CONSIGLIATO - no cookies/proxy needed!)"""
    import httpx
    
    # Estrai video ID
    video_id = url.split('v=')[-1].split('&')[0]
    
    # Istanze Invidious pubbliche
    instances = [
        "https://inv.perditum.com",
        "https://inv.nadeko.net",
        "https://yewtu.be",
        "https://invidious.privacyredirect.com",
        "https://invidious.protokolla.fi",
        "https://invidious.f5.si",
        "https://yt.artemislena.eu",
        "https://invidious.nerdvpn.de",
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
    """Ottieni l'URL per lo streaming - usa Invidious come fallback"""
    
    # Prova prima con yt-dlp (potrebbe funzionare in rari casi)
    try:
        ydl_opts = get_base_ydl_opts()
        ydl_opts.update({
            'format': 'bestaudio[ext=m4a]/bestaudio/best' if format == 'audio' else 'best',
            'quiet': True,
        })
        
        logger.info(f"🎵 Richiesta stream yt-dlp: {url}")
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            
            formats = info.get('formats', [])
            audio_formats = [f for f in formats if f.get('acodec') != 'none' and f.get('url')]
            
            if audio_formats:
                audio_formats.sort(key=lambda x: x.get('abr', 0) or 0, reverse=True)
                best = audio_formats[0]
                
                logger.info(f"✅ Stream yt-dlp ottenuto: {info.get('title')}")
                return {
                    'stream_url': best['url'],
                    'title': info.get('title'),
                    'duration': info.get('duration'),
                    'quality': f"{best.get('abr', 'N/A')}kbps",
                    'source': 'yt-dlp'
                }
    except Exception as e:
        logger.warning(f"⚠️ yt-dlp fallito: {e}, uso Invidious...")
    
    # Fallback: usa Invidious (funziona sempre)
    return await get_stream_via_invidious(url)

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


@app.get("/debug/cloudflare")
async def test_cloudflare_worker():
    """Test configurazione Cloudflare Worker"""
    worker_url = os.getenv('CLOUDFLARE_WORKER_URL')
    
    result = {
        "configured": bool(worker_url),
        "worker_url": worker_url if worker_url else "Not set (add CLOUDFLARE_WORKER_URL env var)",
    }
    
    if not worker_url:
        result["instructions"] = {
            "step_1": "Crea worker su workers.cloudflare.com",
            "step_2": "Incolla il codice fornito",
            "step_3": "Deploy e copia l'URL",
            "step_4": "Aggiungi CLOUDFLARE_WORKER_URL su Render Environment Variables"
        }
        return result
    
    # Test connessione al worker
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(f"{worker_url}/")
            
            result["worker_status"] = "✅ Online" if response.status_code == 200 else f"❌ Error {response.status_code}"
            result["worker_response"] = response.json() if response.status_code == 200 else response.text[:200]
            
    except Exception as e:
        result["worker_status"] = "❌ Unreachable"
        result["worker_error"] = str(e)
    
    return result


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
        "cloudflare_worker": "✅ Configured" if os.getenv('CLOUDFLARE_WORKER_URL') else "❌ Not configured",
        "endpoints": {
            "🔥 RECOMMENDED": {
                "GET /api/stream-multi?url=...": "Stream con fallback multipli (BEST)",
                "GET /api/search-multi?query=...": "Ricerca con fallback multipli (BEST)",
            },
            "🎵 STREAMING": {
                "GET /api/stream?url=...": "Stream con auto-fallback",
                "GET /api/stream-invidious?url=...": "Stream via Invidious (sempre funziona)",
                "GET /api/stream-piped?url=...": "Stream via Piped",
                "GET /api/stream-cloudflare?url=...": "Stream via Cloudflare Worker",
            },
            "🔍 SEARCH": {
                "GET /api/search?query=...": "Ricerca con auto-fallback",
                "GET /api/search-invidious?query=...": "Ricerca via Invidious",
            },
            "📥 DOWNLOAD": {
                "POST /api/download": "Download audio (richiede FFmpeg)",
                "GET /api/status/{task_id}": "Stato download",
                "GET /api/download/{task_id}": "Scarica file completato",
            },
            "ℹ️ INFO": {
                "GET /api/info?url=...": "Info video",
                "GET /debug/test": "Test diagnostica",
                "GET /debug/cookies": "Verifica cookies",
            }
        },
        "setup_instructions": {
            "cloudflare_worker": "Crea worker su workers.cloudflare.com e configura CLOUDFLARE_WORKER_URL",
            "cookies": "Aggiungi cookies.txt come Secret File su Render"
        }
    }

@app.get("/keepalive")
async def keepalive():
    return {"status": "alive", "cookies": COOKIES_FILE.exists()}


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)