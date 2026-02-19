import os
import time
import shutil
import yt_dlp
from flask import Flask, request, jsonify, render_template, send_file
import uuid
from datetime import datetime
import threading
import re
from urllib.parse import urlparse
import subprocess
import sys

app = Flask(__name__)

# ========== CONFIGURATION ==========
DOWNLOAD_FOLDER = os.environ.get('DOWNLOAD_PATH', 'downloads')
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)
app.config['DOWNLOAD_FOLDER'] = DOWNLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'secret-key-12345')

# ========== GLOBAL VARIABLES ==========
download_tasks = {}
tasks_lock = threading.Lock()

# ========== FFMPEG SETUP ==========
def ensure_ffmpeg():
    """Ensure ffmpeg is available - works on Windows, Linux, and Mac"""
    import platform

    # Step 1: Check if ffmpeg is on PATH (works cross-platform)
    ffmpeg_cmd = 'ffmpeg.exe' if platform.system() == 'Windows' else 'ffmpeg'
    try:
        result = subprocess.run(
            [ffmpeg_cmd, '-version'],
            capture_output=True, text=True
        )
        if result.returncode == 0:
            print(f"FFmpeg found on PATH: {ffmpeg_cmd}")
            return ffmpeg_cmd
    except FileNotFoundError:
        pass  # Not on PATH, try next option

    # Step 2: Try imageio-ffmpeg (bundled binary, works everywhere)
    try:
        import imageio_ffmpeg
        ffmpeg_path = imageio_ffmpeg.get_ffmpeg_exe()
        print(f"Using imageio-ffmpeg: {ffmpeg_path}")
        return ffmpeg_path
    except Exception:
        pass

    # Step 3: Try shutil.which (cross-platform PATH check)
    import shutil
    ffmpeg_path = shutil.which('ffmpeg')
    if ffmpeg_path:
        print(f"FFmpeg found via shutil: {ffmpeg_path}")
        return ffmpeg_path

    print("WARNING: ffmpeg not found. Audio conversion will not work.")
    return None

FFMPEG_PATH = ensure_ffmpeg()

# ========== DIRECT YT-DLP DOWNLOAD ==========
def get_ydl_opts(download_type, task_id, ffmpeg_location=None):
    """Build yt-dlp options"""
    output_template = os.path.join(app.config['DOWNLOAD_FOLDER'], f'{task_id}.%(ext)s')
    
    opts = {
        'outtmpl': output_template,
        'quiet': True,
        'no_warnings': True,
        'socket_timeout': 60,
        'retries': 5,
        'fragment_retries': 5,
        'ignoreerrors': False,          # FIXED: was True, caused silent failures
        'no_check_certificate': True,
        'progress_hooks': [lambda d: progress_hook(d, task_id)],
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        },
        'extractor_args': {
            'youtube': {
                'player_client': ['android', 'web'],
            }
        },
    }
    
    # Set ffmpeg location if we found one
    if ffmpeg_location and ffmpeg_location != 'ffmpeg':
        opts['ffmpeg_location'] = ffmpeg_location

    if download_type == 'audio':
        opts['format'] = 'bestaudio/best'
        if ffmpeg_location:
            opts['postprocessors'] = [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }]
        else:
            # No ffmpeg: just download best audio without conversion
            opts['format'] = 'bestaudio[ext=m4a]/bestaudio/best'
    else:
        # Video: prefer mp4 with h264 for maximum compatibility
        opts['format'] = 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best'
        if ffmpeg_location:
            opts['merge_output_format'] = 'mp4'

    return opts


def find_downloaded_file(task_id):
    """Find file downloaded with this task_id prefix"""
    try:
        for file in os.listdir(app.config['DOWNLOAD_FOLDER']):
            if file.startswith(task_id):
                return os.path.join(app.config['DOWNLOAD_FOLDER'], file)
    except Exception:
        pass
    return None


def download_direct(url, download_type, task_id):
    """Direct download using yt-dlp"""
    task = download_tasks.get(task_id)
    if not task:
        return False

    try:
        ydl_opts = get_ydl_opts(download_type, task_id, FFMPEG_PATH)
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            # First extract info to get title
            try:
                info = ydl.extract_info(url, download=False)
                if info:
                    title = info.get('title') or info.get('id') or 'Download'
                    with tasks_lock:
                        task['title'] = title
                        task['message'] = f'Downloading: {title[:50]}...'
            except Exception as e:
                print(f"Info extraction warning: {e}")
                # Continue anyway

            # Now download
            ydl.download([url])

        # Find the downloaded file
        filepath = find_downloaded_file(task_id)
        if not filepath or not os.path.exists(filepath):
            with tasks_lock:
                task['error'] = 'File not found after download'
            return False

        filesize = os.path.getsize(filepath)
        if filesize == 0:
            os.remove(filepath)
            with tasks_lock:
                task['error'] = 'Downloaded file is empty'
            return False

        filename = os.path.basename(filepath)
        ext = os.path.splitext(filename)[1].lower()
        if not ext:
            ext = '.mp4' if download_type == 'video' else '.mp3'

        # Determine mimetype
        mime_map = {
            '.mp4': 'video/mp4',
            '.webm': 'video/webm',
            '.mov': 'video/quicktime',
            '.mp3': 'audio/mpeg',
            '.m4a': 'audio/mp4',
            '.aac': 'audio/aac',
            '.flac': 'audio/flac',
            '.wav': 'audio/wav',
            '.ogg': 'audio/ogg',
        }
        mimetype = mime_map.get(ext, 'application/octet-stream')

        domain = urlparse(url).netloc
        site_name = domain.replace('www.', '').split('.')[0].capitalize()

        with tasks_lock:
            task.update({
                'status': 'completed',
                'progress': 100,
                'filename': filename,
                'filesize': filesize,
                'filepath': filepath,
                'filetype': ext,
                'mimetype': mimetype,
                'site': site_name,
                'completed': True,
                'completed_at': datetime.now().isoformat(),
                'message': 'Download complete!',
            })
        return True

    except yt_dlp.utils.DownloadError as e:
        error_msg = str(e)
        print(f"yt-dlp DownloadError: {error_msg}")
        with tasks_lock:
            task['error'] = _friendly_error(error_msg)
        return False
    except Exception as e:
        error_msg = str(e)
        print(f"Download error: {error_msg}")
        with tasks_lock:
            task['error'] = error_msg[:200]
        return False


def _friendly_error(error_msg):
    """Convert technical errors to user-friendly messages"""
    msg = error_msg.lower()
    if 'sign in' in msg or 'login' in msg or 'age' in msg:
        return 'This video requires login or age verification. Try a public video.'
    if 'private' in msg:
        return 'This video is private and cannot be downloaded.'
    if 'unavailable' in msg or 'not available' in msg:
        return 'This video is unavailable in our region or has been removed.'
    if 'copyright' in msg:
        return 'This video is blocked due to copyright restrictions.'
    if 'rate limit' in msg or '429' in msg:
        return 'Rate limited by the platform. Please try again in a few minutes.'
    if 'not supported' in msg or 'no video formats' in msg:
        return 'This URL/platform is not supported or no downloadable formats found.'
    if 'ffmpeg' in msg:
        return 'Media processing error. The video may still download in original format.'
    return error_msg[:200]


def progress_hook(d, task_id):
    """Progress hook for yt-dlp"""
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
                percent_str = d['_percent_str'].replace('%', '').strip()
                percent = float(percent_str)
            else:
                percent = 0

            with tasks_lock:
                task['progress'] = min(percent, 99)  # Save 100 for completion
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


# ========== DOWNLOAD PROCESSING ==========
def process_download(task_id, url, download_type):
    """Main download processor"""
    task = download_tasks.get(task_id)
    if not task:
        return

    try:
        if not url.startswith(('http://', 'https://')):
            with tasks_lock:
                task.update({'status': 'error', 'message': 'Invalid URL', 'completed': False})
            return

        domain = urlparse(url).netloc.replace('www.', '')
        site_name = domain.split('.')[0].capitalize()

        with tasks_lock:
            task['status'] = 'starting'
            task['site'] = site_name
            task['message'] = f'Connecting to {site_name}...'

        success = download_direct(url, download_type, task_id)

        if not success:
            error = task.get('error', 'Download failed. The site may not be supported or the video is unavailable.')
            with tasks_lock:
                task.update({
                    'status': 'error',
                    'message': error,
                    'completed': False,
                })

    except Exception as e:
        print(f"process_download error: {str(e)}")
        with tasks_lock:
            task.update({
                'status': 'error',
                'message': f'Unexpected error: {str(e)[:100]}',
                'completed': False,
            })


# ========== FLASK ROUTES ==========
@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/download', methods=['POST'])
def start_download():
    """Start download"""
    try:
        data = request.get_json()
        if not data:
            return jsonify({'success': False, 'error': 'Invalid JSON body'}), 400

        url = data.get('url', '').strip()
        download_type = data.get('type', 'video')

        if not url:
            return jsonify({'success': False, 'error': 'URL is required'}), 400

        if not url.startswith(('http://', 'https://')):
            return jsonify({'success': False, 'error': 'Invalid URL. Must start with http:// or https://'}), 400

        if download_type not in ('video', 'audio'):
            download_type = 'video'

        task_id = str(uuid.uuid4())[:12]

        try:
            domain = urlparse(url).netloc.replace('www.', '')
            site_name = domain.split('.')[0].capitalize()
        except Exception:
            site_name = 'Unknown'

        with tasks_lock:
            download_tasks[task_id] = {
                'id': task_id,
                'url': url,
                'type': download_type,
                'status': 'starting',
                'progress': 0,
                'message': f'Starting download from {site_name}...',
                'filename': None,
                'filesize': None,
                'filepath': None,
                'filetype': None,
                'mimetype': None,
                'title': site_name + ' Download',
                'site': site_name,
                'started_at': datetime.now().isoformat(),
                'completed': False,
                'speed': None,
                'eta': None,
                'error': None,
            }

        thread = threading.Thread(
            target=process_download,
            args=(task_id, url, download_type),
            daemon=True
        )
        thread.start()

        return jsonify({'success': True, 'task_id': task_id, 'message': 'Download started'})

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/status/<task_id>', methods=['GET'])
def get_status(task_id):
    """Get task status"""
    with tasks_lock:
        if task_id not in download_tasks:
            return jsonify({'error': 'Task not found'}), 404

        task = dict(download_tasks[task_id])  # copy to avoid lock issues

    # Auto-clean old completed/errored tasks after 5 minutes
    try:
        started = datetime.fromisoformat(task['started_at'])
        age_seconds = (datetime.now() - started).total_seconds()

        if age_seconds > 300 and (task.get('completed') or task.get('status') == 'error'):
            # Clean up file
            if task.get('filepath') and os.path.exists(task.get('filepath', '')):
                try:
                    os.remove(task['filepath'])
                except Exception:
                    pass
            with tasks_lock:
                download_tasks.pop(task_id, None)
            return jsonify({'error': 'Task expired'}), 404
    except Exception:
        pass

    return jsonify({
        'task_id': task_id,
        'status': task.get('status', 'unknown'),
        'progress': task.get('progress', 0),
        'message': task.get('message', ''),
        'title': task.get('title', ''),
        'filename': task.get('filename'),
        'filesize': task.get('filesize'),
        'filetype': task.get('filetype'),
        'mimetype': task.get('mimetype'),
        'type': task.get('type', 'video'),
        'site': task.get('site', 'Unknown'),
        'speed': task.get('speed'),
        'eta': task.get('eta'),
        'completed': task.get('completed', False),
    })


@app.route('/api/download-file/<task_id>', methods=['GET'])
def download_file(task_id):
    """Serve the downloaded file"""
    with tasks_lock:
        task = download_tasks.get(task_id)

    if not task:
        return jsonify({'error': 'Task not found'}), 404

    if not task.get('completed'):
        return jsonify({'error': 'File not ready yet'}), 404

    filepath = task.get('filepath')
    if not filepath or not os.path.exists(filepath):
        return jsonify({'error': 'File not found on server'}), 404

    # Build safe filename
    title = task.get('title') or 'download'
    safe_title = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '', title).strip()
    safe_title = safe_title[:80] or 'download'

    ext = task.get('filetype', '')
    if not ext or ext == 'None':
        ext = '.mp4' if task.get('type') == 'video' else '.mp3'
    if not ext.startswith('.'):
        ext = '.' + ext

    download_name = f"{safe_title}{ext}"

    mimetype = task.get('mimetype') or 'application/octet-stream'

    return send_file(
        filepath,
        as_attachment=True,
        download_name=download_name,
        mimetype=mimetype
    )


@app.route('/health', methods=['GET'])
def health():
    with tasks_lock:
        active = len(download_tasks)
    return jsonify({
        'status': 'ok',
        'time': datetime.now().isoformat(),
        'active_tasks': active,
        'ffmpeg': FFMPEG_PATH or 'not found',
        'yt_dlp_version': yt_dlp.version.__version__,
    })


# ========== CLEANUP ==========
def cleanup_old_files():
    """Clean old files and task entries"""
    try:
        now = time.time()
        folder = app.config['DOWNLOAD_FOLDER']
        for file in os.listdir(folder):
            filepath = os.path.join(folder, file)
            if os.path.isfile(filepath):
                if now - os.path.getmtime(filepath) > 3600:  # 1 hour
                    try:
                        os.remove(filepath)
                    except Exception:
                        pass

        # Clean old task entries
        with tasks_lock:
            to_delete = []
            for tid, task in download_tasks.items():
                try:
                    started = datetime.fromisoformat(task['started_at'])
                    if (datetime.now() - started).total_seconds() > 600:
                        to_delete.append(tid)
                except Exception:
                    to_delete.append(tid)
            for tid in to_delete:
                download_tasks.pop(tid, None)
    except Exception:
        pass


@app.before_request
def before_request():
    # Only run cleanup occasionally to avoid overhead on every request
    if hash(datetime.now().minute) % 10 == 0:
        threading.Thread(target=cleanup_old_files, daemon=True).start()


# ========== MAIN ==========
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
