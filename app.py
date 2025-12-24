import os
import time
import random
import shutil
import yt_dlp
from flask import Flask, request, jsonify, render_template, send_file, Response
import uuid
from datetime import datetime
import threading
import requests
import re
from urllib.parse import urlparse, parse_qs

app = Flask(__name__)

# ========== INITIAL CHECKS ==========
def check_ffmpeg():
    """Check if ffmpeg is available"""
    ffmpeg_path = shutil.which('ffmpeg')
    if not ffmpeg_path:
        print("⚠️  WARNING: FFmpeg not found in PATH.")
        print("   Audio extraction may fail.")
        return False
    print(f"✅ FFmpeg found at: {ffmpeg_path}")
    return True

# Run checks on startup
print("=" * 50)
print("🎬 RON's Downloader - Starting Server")
print("=" * 50)
check_ffmpeg()

# ========== CONFIGURATION ==========
DOWNLOAD_FOLDER = "downloads"
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)
app.config['DOWNLOAD_FOLDER'] = DOWNLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024  # 500MB
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'default-secret-key-change-in-production')

# ========== GLOBAL VARIABLES ==========
download_tasks = {}

# ========== USER AGENTS ==========
USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Gecko/20100101 Firefox/122.0',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
    'Mozilla/5.0 (iPhone; CPU iPhone OS 17_3 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Mobile/15E148 Safari/604.1'
]

def get_random_user_agent():
    return random.choice(USER_AGENTS)

# ========== SITE SUPPORT CHECK ==========
SUPPORTED_SITES = {
    'youtube.com': True,
    'youtu.be': True,
    'instagram.com': True,
    'facebook.com': True,
    'fb.watch': True,
    'twitter.com': True,
    'x.com': True,
    'tiktok.com': True,
    'vm.tiktok.com': True,
    'vimeo.com': True,
    'dailymotion.com': True,
    'twitch.tv': True,
    'reddit.com': True,
    'soundcloud.com': True,
    'bandcamp.com': True,
    'spotify.com': True,
    'pinterest.com': True,
    'likee.video': True,
    'bilibili.com': True,
    'rutube.ru': True
}

def is_supported_site(url):
    """Check if URL is from supported site"""
    for site in SUPPORTED_SITES.keys():
        if site in url:
            return True
    return False

# ========== DIRECT YT-DLP DOWNLOAD ==========
def download_direct(url, download_type, task_id):
    """Direct download using yt-dlp - SUPPORTS ALL SITES"""
    try:
        task = download_tasks[task_id]
        
        ydl_opts = {
            'format': 'bestaudio/best' if download_type == 'audio' else 'best',
            'outtmpl': os.path.join(app.config['DOWNLOAD_FOLDER'], f'{task_id}.%(ext)s'),
            'quiet': True,
            'no_warnings': True,
            'socket_timeout': 120,
            'retries': 5,
            'fragment_retries': 10,
            'user_agent': get_random_user_agent(),
            'http_headers': {
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
                'Accept-Language': 'en-US,en;q=0.5',
                'Accept-Encoding': 'gzip, deflate',
                'DNT': '1',
                'Connection': 'keep-alive',
                'Upgrade-Insecure-Requests': '1',
                'Sec-Fetch-Dest': 'document',
                'Sec-Fetch-Mode': 'navigate',
                'Sec-Fetch-Site': 'none',
                'Sec-Fetch-User': '?1',
                'Cache-Control': 'max-age=0',
                'Referer': 'https://www.google.com/',
            },
            'progress_hooks': [lambda d: progress_hook(d, task_id)],
            'extractor_args': {
                'youtube': {
                    'player_client': ['android', 'web', 'ios'],
                    'skip': ['dash', 'hls'],
                    'throttled_rate': None,
                },
                'instagram': {},
                'facebook': {},
                'twitter': {},
                'tiktok': {}
            },
            'ignoreerrors': True,
            'no_check_certificate': True,
            'force_generic_extractor': False,
        }
        
        if download_type == 'audio':
            ydl_opts['postprocessors'] = [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }]
            ydl_opts['format'] = 'bestaudio/best'
        else:
            ydl_opts['format'] = 'best[ext=mp4]/best[ext=webm]/best'
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            try:
                # Get info first
                info = ydl.extract_info(url, download=False)
                
                if info is None:
                    task.update({
                        'error': 'Could not extract video info. The site may be blocking requests.',
                        'error_type': 'no_info'
                    })
                    return False
                
                # Extract site name
                domain = urlparse(url).netloc
                site_name = domain.replace('www.', '').split('.')[0].capitalize()
                
                task['title'] = info.get('title', f'{site_name} video')
                task['duration'] = info.get('duration', 0)
                task['thumbnail'] = info.get('thumbnail')
                task['site'] = site_name
                
                # Download
                ydl.download([url])
                
            except Exception as e:
                error_msg = str(e)
                print(f"Info extraction error for {url}: {error_msg}")
                task['error'] = f'Info error: {error_msg[:100]}'
                task['error_type'] = 'extraction_error'
                return False
            
            # Find downloaded file
            for file in os.listdir(app.config['DOWNLOAD_FOLDER']):
                if file.startswith(task_id):
                    filepath = os.path.join(app.config['DOWNLOAD_FOLDER'], file)
                    filesize = os.path.getsize(filepath)
                    
                    # Get file extension
                    ext = os.path.splitext(file)[1].lower()
                    if not ext:
                        ext = '.mp4' if download_type == 'video' else '.mp3'
                    
                    # Determine mimetype
                    if ext in ['.mp4', '.webm', '.mov']:
                        mimetype = 'video/mp4'
                        filetype = ext
                    elif ext in ['.mp3', '.m4a', '.aac', '.flac', '.wav']:
                        mimetype = 'audio/mpeg'
                        filetype = ext
                    else:
                        mimetype = 'application/octet-stream'
                        filetype = ext
                    
                    task.update({
                        'status': 'completed',
                        'progress': 100,
                        'filename': file,
                        'filesize': filesize,
                        'filepath': filepath,
                        'filetype': filetype,
                        'mimetype': mimetype,
                        'completed': True,
                        'completed_at': datetime.now().isoformat(),
                        'method': 'direct'
                    })
                    return True
        
        return False
        
    except Exception as e:
        error_msg = str(e)
        print(f"Download error for {url}: {error_msg}")
        task['error'] = error_msg[:200]
        
        # Specific error handling
        if "Unsupported URL" in error_msg:
            task['error_type'] = 'unsupported_url'
        elif "Sign in" in error_msg or "private" in error_msg:
            task['error_type'] = 'private_video'
        elif "ffmpeg" in error_msg.lower():
            task['error_type'] = 'ffmpeg_error'
        
        return False

def progress_hook(d, task_id):
    """Progress hook for yt-dlp"""
    if task_id not in download_tasks:
        return
    
    task = download_tasks[task_id]
    
    if d['status'] == 'downloading':
        if '_percent_str' in d:
            percent = d['_percent_str'].replace('%', '').strip()
            try:
                task['progress'] = float(percent)
                task['status'] = 'downloading'
                task['message'] = f'Downloading: {percent}%'
            except:
                task['progress'] = 0
        # Add speed and ETA
        if '_speed_str' in d:
            task['speed'] = d['_speed_str'].strip()
        if '_eta_str' in d:
            task['eta'] = d['_eta_str'].strip()
    
    elif d['status'] == 'finished':
        task['progress'] = 100
        task['status'] = 'processing'
        task['message'] = 'Processing file...'

# ========== DOWNLOAD PROCESSING ==========
def process_download(task_id, url, download_type):
    """Main download processor - WORKS FOR ALL SITES"""
    try:
        task = download_tasks[task_id]
        
        # Check if URL is valid
        if not url.startswith(('http://', 'https://')):
            task.update({
                'status': 'error',
                'message': 'Invalid URL. Use http:// or https://',
                'completed': False
            })
            return
        
        task['status'] = 'checking_url'
        task['message'] = 'Checking URL...'
        
        # Extract domain for display
        try:
            domain = urlparse(url).netloc.replace('www.', '')
            task['site'] = domain.split('.')[0].capitalize()
        except:
            task['site'] = 'Unknown'
        
        task['status'] = 'starting'
        task['message'] = f'Downloading from {task["site"]}...'
        
        # Try direct download with yt-dlp (works for 1000+ sites)
        if download_direct(url, download_type, task_id):
            return
        
        # If failed
        task.update({
            'status': 'error',
            'message': f'Failed to download from {task["site"]}. The site may not be supported.',
            'completed': False,
        })
        
    except Exception as e:
        print(f"Download processing error: {str(e)}")
        task.update({
            'status': 'error',
            'message': f'Error: {str(e)[:100]}',
            'completed': False,
            'error': str(e)
        })

# ========== FLASK ROUTES ==========
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/download', methods=['POST'])
def start_download():
    """Start download - ACCEPTS ALL SITES"""
    try:
        data = request.get_json()
        url = data.get('url', '').strip()
        download_type = data.get('type', 'video')
        
        if not url:
            return jsonify({'success': False, 'error': 'URL is required'}), 400
        
        # Check if valid URL
        if not url.startswith(('http://', 'https://')):
            return jsonify({
                'success': False,
                'error': 'Invalid URL. Must start with http:// or https://'
            }), 400
        
        # Generate task ID
        task_id = str(uuid.uuid4())[:12]
        
        # Extract site name for display
        try:
            domain = urlparse(url).netloc.replace('www.', '')
            site_name = domain.split('.')[0].capitalize()
        except:
            site_name = 'Unknown'
        
        # Create task
        download_tasks[task_id] = {
            'id': task_id,
            'url': url,
            'type': download_type,
            'status': 'starting',
            'progress': 0,
            'message': f'Preparing download from {site_name}...',
            'filename': None,
            'filesize': None,
            'filepath': None,
            'filetype': None,
            'mimetype': None,
            'title': None,
            'site': site_name,
            'started_at': datetime.now().isoformat(),
            'completed': False,
            'method': None,
            'error': None,
            'error_type': None,
            'speed': None,
            'eta': None
        }
        
        # Start in background
        thread = threading.Thread(
            target=process_download,
            args=(task_id, url, download_type),
            daemon=True
        )
        thread.start()
        
        return jsonify({
            'success': True,
            'task_id': task_id,
            'message': f'Download from {site_name} started'
        })
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/status/<task_id>', methods=['GET'])
def get_status(task_id):
    """Get status"""
    if task_id not in download_tasks:
        return jsonify({'error': 'Task not found'}), 404
    
    task = download_tasks[task_id]
    
    # Clean old completed tasks (after 5 minutes)
    if task.get('completed') or task.get('status') == 'error':
        started = datetime.fromisoformat(task['started_at'].replace('Z', '+00:00'))
        if (datetime.now() - started).total_seconds() > 300:  # 5 minutes
            # Delete file if exists
            if task.get('filepath') and os.path.exists(task['filepath']):
                try:
                    os.remove(task['filepath'])
                except:
                    pass
            del download_tasks[task_id]
            return jsonify({'error': 'Task expired'}), 404
    
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
        'error': task.get('error'),
        'error_type': task.get('error_type')
    })

@app.route('/api/download-file/<task_id>', methods=['GET'])
def download_file(task_id):
    """Serve file - FIXED TO AUTO-SAVE TO DEVICE"""
    if task_id not in download_tasks:
        return jsonify({'error': 'Task not found'}), 404
    
    task = download_tasks[task_id]
    
    # Check if ready
    if not task.get('completed'):
        return jsonify({'error': 'File not ready'}), 404
    
    # If file exists
    if task.get('filepath') and os.path.exists(task['filepath']):
        # Determine filename
        if task.get('title'):
            # Clean filename
            safe_title = re.sub(r'[<>:"/\\|?*]', '', task['title'])
            safe_title = safe_title.replace('\n', ' ').replace('\r', ' ')
            safe_title = safe_title[:80].strip()
            
            # Get extension from filetype or default
            ext = task.get('filetype', '')
            if not ext or ext == 'None':
                ext = '.mp4' if task['type'] == 'video' else '.mp3'
            
            # Ensure extension starts with dot
            if not ext.startswith('.'):
                ext = '.' + ext
            
            filename = f"{safe_title}{ext}"
        else:
            filename = task.get('filename', 'download.mp4')
        
        # Determine mimetype
        mimetype = task.get('mimetype')
        if not mimetype:
            if filename.endswith('.mp4'):
                mimetype = 'video/mp4'
            elif filename.endswith('.mp3'):
                mimetype = 'audio/mpeg'
            elif filename.endswith('.m4a'):
                mimetype = 'audio/mp4'
            else:
                mimetype = 'application/octet-stream'
        
        print(f"📥 Sending file: {filename} ({mimetype})")
        
        # FIXED: This will auto-save to user's Downloads folder
        return send_file(
            task['filepath'],
            as_attachment=True,
            download_name=filename,
            mimetype=mimetype
        )
    
    return jsonify({'error': 'File not found'}), 404

@app.route('/api/check-url', methods=['POST'])
def check_url():
    """Check if URL is supported"""
    try:
        data = request.get_json()
        url = data.get('url', '').strip()
        
        if not url:
            return jsonify({'supported': False, 'error': 'URL required'}), 400
        
        # Try to extract info with yt-dlp
        try:
            ydl_opts = {
                'quiet': True,
                'no_warnings': True,
                'user_agent': get_random_user_agent()
            }
            
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                
                return jsonify({
                    'supported': True,
                    'title': info.get('title', 'Unknown'),
                    'duration': info.get('duration', 0),
                    'thumbnail': info.get('thumbnail'),
                    'site': urlparse(url).netloc.replace('www.', '')
                })
        except:
            return jsonify({
                'supported': False,
                'error': 'Cannot extract info. The site may not be supported.'
            })
            
    except Exception as e:
        return jsonify({'supported': False, 'error': str(e)}), 500

@app.route('/health', methods=['GET'])
def health():
    return jsonify({
        'status': 'ok',
        'time': datetime.now().isoformat(),
        'active_tasks': len(download_tasks),
        'ffmpeg': shutil.which('ffmpeg') is not None,
        'supported_sites': list(SUPPORTED_SITES.keys())[:10]  # Show first 10
    })

# ========== CLEANUP ==========
def cleanup():
    """Clean old files and tasks"""
    try:
        # Clean old files (older than 1 hour)
        now = time.time()
        for file in os.listdir(DOWNLOAD_FOLDER):
            filepath = os.path.join(DOWNLOAD_FOLDER, file)
            if os.path.isfile(filepath):
                if now - os.path.getmtime(filepath) > 3600:  # 1 hour
                    try:
                        os.remove(filepath)
                        print(f"🧹 Cleaned up old file: {file}")
                    except:
                        pass
        
        # Clean old tasks (older than 30 minutes)
        expired = []
        for task_id, task in download_tasks.items():
            started = datetime.fromisoformat(task['started_at'].replace('Z', '+00:00'))
            if (datetime.now() - started).total_seconds() > 1800:  # 30 minutes
                expired.append(task_id)
        
        for task_id in expired:
            del download_tasks[task_id]
            
    except Exception as e:
        print(f"Cleanup error: {e}")

# Run cleanup before each request
@app.before_request
def before_request():
    cleanup()

# ========== MAIN ==========
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print(f"🚀 Server starting on port {port}")
    print(f"📁 Download folder: {os.path.abspath(DOWNLOAD_FOLDER)}")
    print(f"🌐 Supported sites: {len(SUPPORTED_SITES)}+ platforms")
    print(f"🔗 Open in browser: http://localhost:{port}")
    print("=" * 50)
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
