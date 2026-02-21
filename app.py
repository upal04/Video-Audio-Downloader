import os
import time
import yt_dlp
from flask import Flask, request, jsonify, render_template, send_file
import uuid
from datetime import datetime
import threading
import re
from urllib.parse import urlparse
import subprocess
import shutil
import platform

app = Flask(__name__)

# ========== CONFIGURATION ==========
DOWNLOAD_FOLDER = os.environ.get('DOWNLOAD_PATH', 'downloads')
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)
app.config['DOWNLOAD_FOLDER'] = DOWNLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'secret-key-12345')

COOKIES_FILE = 'cookies.txt'

# ========== COOKIES SETUP ==========
def setup_cookies():
    c1 = os.environ.get('COOKIES_CONTENT', '').strip()
    c2 = os.environ.get('COOKIES_EXTRA', '').strip()
    parts = []
    if c1:
        parts.append(c1)
    if c2:
        lines = [l for l in c2.splitlines() if not l.startswith('# Netscape') and not l.startswith('# This file')]
        parts.append('\n'.join(lines))
    if parts:
        with open(COOKIES_FILE, 'w', encoding='utf-8') as f:
            f.write('# Netscape HTTP Cookie File\n')
            f.write('\n'.join(parts))
        print(f"Cookies: COOKIES_CONTENT={'yes' if c1 else 'no'}, COOKIES_EXTRA={'yes' if c2 else 'no'}")
    elif os.path.exists(COOKIES_FILE):
        print(f"Cookies loaded from file.")
    else:
        print("WARNING: No cookies found.")

setup_cookies()

# ========== GLOBAL STATE ==========
download_tasks = {}
tasks_lock = threading.Lock()

# ========== FFMPEG SETUP ==========
def ensure_ffmpeg():
    sys_cmd = 'ffmpeg.exe' if platform.system() == 'Windows' else 'ffmpeg'
    try:
        result = subprocess.run([sys_cmd, '-version'], capture_output=True, text=True)
        if result.returncode == 0:
            return sys_cmd
    except FileNotFoundError:
        pass
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    return shutil.which('ffmpeg')

FFMPEG_PATH = ensure_ffmpeg()
FFPROBE_PATH = shutil.which('ffprobe')
print(f"FFmpeg:  {FFMPEG_PATH or 'not found'}")
print(f"FFprobe: {FFPROBE_PATH or 'not found'}")


# ========== YT-DLP OPTIONS ==========
def get_ydl_opts(download_type, task_id, url=''):
    output_template = os.path.join(app.config['DOWNLOAD_FOLDER'], f'{task_id}.%(ext)s')
    has_cookies = os.path.exists(COOKIES_FILE)

    try:
        netloc = urlparse(url).netloc.lower()
    except Exception:
        netloc = ''

    is_facebook = any(x in netloc for x in ['facebook.com', 'fb.watch', 'fb.com'])
    is_youtube  = any(x in netloc for x in ['youtube.com', 'youtu.be'])

    opts = {
        'outtmpl': output_template,
        'quiet': True,
        'no_warnings': True,
        'socket_timeout': 60,
        'retries': 10,
        'fragment_retries': 10,
        'ignoreerrors': False,
        'no_check_certificate': True,
        'progress_hooks': [lambda d: progress_hook(d, task_id)],
        'http_headers': {
            'User-Agent': (
                'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) '
                'AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1'
                if is_facebook else
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36'
            ),
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept': '*/*',
        },
    }

    if is_youtube:
        opts['extractor_args'] = {
            'youtube': {
                'player_client': ['mweb', 'tv_embedded', 'ios', 'android'],
                'player_skip': ['webpage', 'configs'],
            }
        }

    if has_cookies:
        opts['cookiefile'] = COOKIES_FILE

    if FFMPEG_PATH and FFMPEG_PATH not in ('ffmpeg', 'ffmpeg.exe'):
        opts['ffmpeg_location'] = FFMPEG_PATH

    # ── AUDIO ──
    if download_type == 'audio':
        if is_facebook:
            # Facebook: download the full best stream then extract audio with ffmpeg
            # bestaudio alone often gets blocked; using 'best' then extracting is more reliable
            opts['format'] = 'best'
            if FFMPEG_PATH:
                opts['postprocessors'] = [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '192',
                }]
        else:
            # Instagram, TikTok, Twitter etc: request audio-only stream directly
            opts['format'] = 'bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio'
            if FFMPEG_PATH:
                opts['postprocessors'] = [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '192',
                }]
        # If no ffmpeg: file stays as m4a/webm — still audio, plays fine

    # ── VIDEO ──
    else:
        if FFMPEG_PATH:
            opts['format'] = 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best'
            opts['merge_output_format'] = 'mp4'
        else:
            opts['format'] = 'best[ext=mp4]/best'

    return opts


def find_downloaded_file(task_id):
    try:
        for f in os.listdir(app.config['DOWNLOAD_FOLDER']):
            if f.startswith(task_id):
                return os.path.join(app.config['DOWNLOAD_FOLDER'], f)
    except Exception:
        pass
    return None


def _friendly_error(error_msg):
    msg = error_msg.lower()
    if any(x in msg for x in ['sign in', 'login required', 'age-restricted', 'bot', 'confirm your age', 'checkpoint']):
        return 'This video requires login or is age-restricted. Try a public video.'
    if 'private' in msg:
        return 'This video is private.'
    if any(x in msg for x in ['copyright', 'blocked in your country']):
        return 'Blocked due to copyright or regional restrictions.'
    if any(x in msg for x in ['429', 'rate limit', 'too many requests']):
        return 'Rate limited. Please wait a few minutes and try again.'
    if any(x in msg for x in ['unsupported url', 'no video formats found', 'not supported']):
        return 'This URL is not supported. Make sure the link is correct and the video is public.'
    if 'ffmpeg' in msg:
        return 'Processing failed. Try downloading as video instead.'
    if any(x in msg for x in ['network', 'connection', 'timed out', 'timeout']):
        return 'Network error. Please try again.'
    # Generic fallback — show real error, don't swallow it
    return error_msg[:300]


def progress_hook(d, task_id):
    task = download_tasks.get(task_id)
    if not task:
        return
    if d['status'] == 'downloading':
        try:
            downloaded = d.get('downloaded_bytes', 0)
            total = d.get('total_bytes') or d.get('total_bytes_estimate', 0)
            if total and total > 0:
                percent = round((downloaded / total) * 100, 1)
            elif '_percent_str' in d:
                percent = float(d['_percent_str'].replace('%', '').strip())
            else:
                percent = 0
            with tasks_lock:
                task['progress'] = min(percent, 99)
                task['status'] = 'downloading'
                task['message'] = f'Downloading... {percent:.0f}%'
                if '_speed_str' in d:
                    task['speed'] = d['_speed_str'].strip()
                if '_eta_str' in d:
                    task['eta'] = d['_eta_str'].strip()
        except Exception:
            pass
    elif d['status'] == 'finished':
        with tasks_lock:
            task['progress'] = 99
            task['status'] = 'processing'
            task['message'] = 'Processing file...'


def download_direct(url, download_type, task_id):
    task = download_tasks.get(task_id)
    if not task:
        return False
    try:
        ydl_opts = get_ydl_opts(download_type, task_id, url)
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            try:
                info = ydl.extract_info(url, download=False)
                if info:
                    title = info.get('title') or info.get('id') or 'Download'
                    with tasks_lock:
                        task['title'] = title
                        task['message'] = f'Downloading: {title[:50]}...'
            except Exception as e:
                print(f"Info extraction (non-fatal): {e}")
            ydl.download([url])

        filepath = find_downloaded_file(task_id)
        if not filepath or not os.path.exists(filepath):
            with tasks_lock:
                task['error'] = 'File not found after download.'
            return False

        filesize = os.path.getsize(filepath)
        if filesize == 0:
            os.remove(filepath)
            with tasks_lock:
                task['error'] = 'Downloaded file is empty.'
            return False

        filename = os.path.basename(filepath)
        ext = os.path.splitext(filename)[1].lower() or ('.mp4' if download_type == 'video' else '.mp3')
        mime_map = {
            '.mp4': 'video/mp4',  '.webm': 'video/webm', '.mov': 'video/quicktime',
            '.mp3': 'audio/mpeg', '.m4a':  'audio/mp4',  '.aac': 'audio/aac',
            '.flac': 'audio/flac','.wav':  'audio/wav',  '.ogg': 'audio/ogg',
        }
        mimetype = mime_map.get(ext, 'application/octet-stream')
        site_name = urlparse(url).netloc.replace('www.', '').split('.')[0].capitalize()

        with tasks_lock:
            task.update({
                'status': 'completed', 'progress': 100,
                'filename': filename, 'filesize': filesize,
                'filepath': filepath, 'filetype': ext,
                'mimetype': mimetype, 'site': site_name,
                'completed': True,
                'completed_at': datetime.now().isoformat(),
                'message': 'Download complete!',
            })
        return True

    except yt_dlp.utils.DownloadError as e:
        err = str(e)
        print(f"DownloadError: {err}")
        with tasks_lock:
            task['error'] = _friendly_error(err)
        return False
    except Exception as e:
        err = str(e)
        print(f"Unexpected error: {err}")
        with tasks_lock:
            task['error'] = err[:300]
        return False


def process_download(task_id, url, download_type):
    task = download_tasks.get(task_id)
    if not task:
        return
    try:
        if not url.startswith(('http://', 'https://')):
            with tasks_lock:
                task.update({'status': 'error', 'message': 'Invalid URL', 'completed': False})
            return
        site_name = urlparse(url).netloc.replace('www.', '').split('.')[0].capitalize()
        with tasks_lock:
            task['status'] = 'starting'
            task['site'] = site_name
            task['message'] = f'Connecting to {site_name}...'

        success = download_direct(url, download_type, task_id)
        if not success:
            error = task.get('error', 'Download failed.')
            with tasks_lock:
                task.update({'status': 'error', 'message': error, 'completed': False})
    except Exception as e:
        print(f"process_download error: {e}")
        with tasks_lock:
            task.update({'status': 'error', 'message': str(e)[:200], 'completed': False})


# ========== ROUTES ==========
@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/download', methods=['POST'])
def start_download():
    try:
        data = request.get_json()
        if not data:
            return jsonify({'success': False, 'error': 'Invalid request'}), 400
        url = data.get('url', '').strip()
        download_type = data.get('type', 'video')
        if not url:
            return jsonify({'success': False, 'error': 'URL is required'}), 400
        if not url.startswith(('http://', 'https://')):
            return jsonify({'success': False, 'error': 'URL must start with http:// or https://'}), 400
        if download_type not in ('video', 'audio'):
            download_type = 'video'

        task_id = str(uuid.uuid4())[:12]
        site_name = urlparse(url).netloc.replace('www.', '').split('.')[0].capitalize()

        with tasks_lock:
            download_tasks[task_id] = {
                'id': task_id, 'url': url, 'type': download_type,
                'status': 'starting', 'progress': 0,
                'message': f'Starting download from {site_name}...',
                'filename': None, 'filesize': None, 'filepath': None,
                'filetype': None, 'mimetype': None,
                'title': f'{site_name} Download', 'site': site_name,
                'started_at': datetime.now().isoformat(),
                'completed': False, 'speed': None, 'eta': None, 'error': None,
            }

        threading.Thread(
            target=process_download,
            args=(task_id, url, download_type),
            daemon=True
        ).start()
        return jsonify({'success': True, 'task_id': task_id})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/status/<task_id>', methods=['GET'])
def get_status(task_id):
    with tasks_lock:
        if task_id not in download_tasks:
            return jsonify({'error': 'Task not found'}), 404
        task = dict(download_tasks[task_id])

    # Only expire tasks that are DONE/ERRORED and older than 30 minutes
    # Never expire tasks that are still running (status: starting/downloading/processing)
    try:
        age = (datetime.now() - datetime.fromisoformat(task['started_at'])).total_seconds()
        is_finished = task.get('completed') or task.get('status') == 'error'
        if age > 1800 and is_finished:
            fp = task.get('filepath', '')
            if fp and os.path.exists(fp):
                try: os.remove(fp)
                except Exception: pass
            with tasks_lock:
                download_tasks.pop(task_id, None)
            return jsonify({'error': 'Task expired'}), 404
    except Exception:
        pass

    return jsonify({
        'task_id':   task_id,
        'status':    task.get('status', 'unknown'),
        'progress':  task.get('progress', 0),
        'message':   task.get('message', ''),
        'title':     task.get('title', ''),
        'filename':  task.get('filename'),
        'filesize':  task.get('filesize'),
        'filetype':  task.get('filetype'),
        'mimetype':  task.get('mimetype'),
        'type':      task.get('type', 'video'),
        'site':      task.get('site', 'Unknown'),
        'speed':     task.get('speed'),
        'eta':       task.get('eta'),
        'completed': task.get('completed', False),
    })


@app.route('/api/download-file/<task_id>', methods=['GET'])
def download_file(task_id):
    with tasks_lock:
        task = download_tasks.get(task_id)
    if not task:
        return jsonify({'error': 'Task not found'}), 404
    if not task.get('completed'):
        return jsonify({'error': 'File not ready'}), 404
    filepath = task.get('filepath')
    if not filepath or not os.path.exists(filepath):
        return jsonify({'error': 'File not found'}), 404

    title = task.get('title') or 'download'
    safe_title = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '', title).strip()[:80] or 'download'
    ext = task.get('filetype') or ('.mp4' if task.get('type') == 'video' else '.mp3')
    if not ext.startswith('.'):
        ext = '.' + ext

    return send_file(
        filepath,
        as_attachment=True,
        download_name=f"{safe_title}{ext}",
        mimetype=task.get('mimetype') or 'application/octet-stream'
    )


@app.route('/health', methods=['GET'])
def health():
    with tasks_lock:
        active = len(download_tasks)
    return jsonify({
        'status': 'ok',
        'time': datetime.now().isoformat(),
        'active_tasks': active,
        'ffmpeg':  FFMPEG_PATH  or 'not found',
        'ffprobe': FFPROBE_PATH or 'not found',
        'yt_dlp_version': yt_dlp.version.__version__,
        'cookies_loaded': os.path.exists(COOKIES_FILE),
    })


# ========== CLEANUP ==========
def cleanup_old_files():
    try:
        now = time.time()
        for f in os.listdir(app.config['DOWNLOAD_FOLDER']):
            fp = os.path.join(app.config['DOWNLOAD_FOLDER'], f)
            if os.path.isfile(fp) and now - os.path.getmtime(fp) > 3600:
                try: os.remove(fp)
                except Exception: pass
        with tasks_lock:
            to_del = [
                tid for tid, t in download_tasks.items()
                if (datetime.now() - datetime.fromisoformat(t['started_at'])).total_seconds() > 3600
            ]
            for tid in to_del:
                download_tasks.pop(tid, None)
    except Exception:
        pass

@app.before_request
def before_request():
    if hash(datetime.now().minute) % 10 == 0:
        threading.Thread(target=cleanup_old_files, daemon=True).start()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
