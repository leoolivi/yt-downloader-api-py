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
    
    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': str(output_path),
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': audio_format,
            'preferredquality': quality,
        }],
        'progress_hooks': [lambda d: progress_hook(d, task_id)],
        'quiet': False,
        'no_warnings': False,
        # Fix per errore 403
        'nocheckcertificate': True,
        'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'referer': 'https://www.youtube.com/',
        'extractor_args': {'youtube': {'player_client': ['android', 'web']}},
    }
    
    try:
        download_status[task_id] = {'status': 'starting', 'progress': '0%'}
        
        # Esegui il download in un thread separato per non bloccare
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            lambda: yt_dlp.YoutubeDL(ydl_opts).download([str(url)])
        )
        
        # Trova il file scaricato
        output_file = DOWNLOAD_DIR / f"{task_id}.{audio_format}"
        
        download_status[task_id] = {
            'status': 'completed',
            'progress': '100%',
            'file': str(output_file)
        }
        
    except Exception as e:
        download_status[task_id] = {
            'status': 'error',
            'message': str(e)
        }

@app.get("/api/search")
async def search_videos(query: str, limit: int = 10):
    """
    Cerca video su YouTube senza usare API key.
    Esempio: /api/search?query=Eminem+Lose+Yourself&limit=5
    """
    try:
        ydl_opts = {
            'quiet': True,
            'skip_download': True,
            'extract_flat': 'in_playlist',
            'nocheckcertificate': True,
            'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'referer': 'https://www.youtube.com/',
            'extractor_args': {'youtube': {'player_client': ['android', 'web']}},
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            search_query = f"ytsearch{limit}:{query}"
            info = ydl.extract_info(search_query, download=False)
            entries = info.get("entries", [])

        results = []
        for e in entries:
            results.append({
                "title": e.get("title"),
                "url": f"https://www.youtube.com/watch?v={e.get('id')}",
                "duration": e.get("duration"),
                "uploader": e.get("uploader"),
                "thumbnail": e.get("thumbnails", [{}])[-1].get("url") if e.get("thumbnails") else None
            })

        return JSONResponse(content={"results": results, "count": len(results)})

    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/download", response_model=DownloadResponse)
async def start_download(request: DownloadRequest, background_tasks: BackgroundTasks):
    """Inizia il download dell'audio"""
    task_id = str(uuid.uuid4())
    
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
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'nocheckcertificate': True,
            'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'referer': 'https://www.youtube.com/',
            'extractor_args': {'youtube': {'player_client': ['android', 'web']}},
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            
            return {
                'title': info.get('title'),
                'duration': info.get('duration'),
                'thumbnail': info.get('thumbnail'),
                'uploader': info.get('uploader'),
                'formats_available': len(info.get('formats', []))
            }
    except Exception as e:
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
        del download_status[task_id]
    
    return {"message": "Cleaned up successfully"}


@app.get("/")
async def root():
    return {
        "message": "YT-DLP Audio Downloader API",
        "endpoints": {
            "POST /api/download": "Start audio download",
            "GET /api/status/{task_id}": "Check download status",
            "GET /api/download/{task_id}": "Download completed file",
            "GET /api/info?url=": "Get video info",
            "DELETE /api/cleanup/{task_id}": "Delete downloaded file"
        }
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)