import os
import time
import random
import yt_dlp
from flask import Flask, request, jsonify, render_template, send_file
import uuid
from pathlib import Path
from datetime import datetime
import threading

app = Flask(__name__)

# ========== CONFIGURATION ==========
DOWNLOAD_FOLDER = "downloads"
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)
app.config['DOWNLOAD_FOLDER'] = DOWNLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024  # 100MB
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'your-secret-key-here')

# ========== GLOBAL VARIABLES ==========
download_tasks = {}

# ========== USER AGENTS ==========
USER_AGENTS = [
    # Desktop Chrome
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36',
    # Firefox
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Gecko/20100101 Firefox/122.0',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0',
    # Safari
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.1 Safari/605.1.15',
    # Mobile
    'Mozilla/5.0 (iPhone; CPU iPhone OS 17_3 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Mobile/15E148 Safari/604.1',
    'Mozilla/5.0 (Linux; Android 14; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Mobile Safari/537.36',
    'Mozilla/5.0 (Linux; Android 13; SM-G991B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Mobile Safari/537.36',
    # Edge
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36 Edg/121.0.0.0'
]

# ========== HELPER FUNCTIONS ==========
def get_random_user_agent():
    return random.choice(USER_AGENTS)

def setup_cookies():
    """Setup cookies from environment or file"""
    # Try environment variable (for Render.com)
    cookies_env = os.environ.get('YOUTUBE_COOKIES')
    if cookies_env:
        try:
            with open('cookies.txt', 'w', encoding='utf-8') as f:
                f.write(cookies_env)
            print("✓ Cookies loaded from environment")
            return 'cookies.txt'
        except Exception as e:
            print(f"✗ Failed to write cookies: {e}")
    
    # Try local file
    if os.path.exists('cookies.txt'):
        print("✓ Cookies loaded from file")
        return 'cookies.txt'
    
    print("⚠ No cookies found - YouTube may block some videos")
    return None

def clean_filename(filename):
    """Clean filename for safe saving"""
    if not filename:
        return "video_download"
    
    # Remove invalid characters
    invalid_chars = '<>:"/\\|?*\'"'
    for char in invalid_chars:
        filename = filename.replace(char, '')
    
    # Replace spaces and trim
    filename = filename.replace(' ', '_')
    filename = filename[:80]  # Limit length
    
    return filename

# ========== YT-DLP CONFIGURATION ==========
def get_ydl_options(download_type='video'):
    """Get yt-dlp options with anti-bot measures"""
    
    # Format selection
    if download_type == 'audio':
        format_spec = 'bestaudio[ext=m4a]/bestaudio/best'
        postprocessors = [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'm4a',
            'preferredquality': '192',
        }]
    else:  # video
        # Limit to 720p to reduce bandwidth and avoid 1080p+ issues
        format_spec = 'bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best'
        postprocessors = []
    
    # Setup cookies
    cookies_path = setup_cookies()
    
    # Random user agent
    user_agent = get_random_user_agent()
    
    # Check if mobile
    is_mobile = any(x in user_agent for x in ['Mobile', 'iPhone', 'Android'])
    
    # Different settings for mobile vs desktop
    if is_mobile:
        extractor_args = {
            'youtube': {
                'player_client': ['android', 'ios'],
                'player_skip': ['configs', 'js', 'webpage'],
            }
        }
    else:
        extractor_args = {
            'youtube': {
                'player_client': ['web'],
                'player_skip': ['configs'],
            }
        }
    
    # Return options
    return {
        'format': format_spec,
        'outtmpl': os.path.join(app.config['DOWNLOAD_FOLDER'], '%(title).80s.%(ext)s'),
        'quiet': True,
        'no_warnings': False,
        'ignoreerrors': False,
        'socket_timeout': 60,
        'retries': 15,
        'fragment_retries': 15,
        'skip_unavailable_fragments': True,
        'user_agent': user_agent,
        'cookiefile': cookies_path,
        'extract_flat': False,
        'force_ipv4': True,
        'sleep_interval_requests': random.randint(1, 3),
        'sleep_interval': random.randint(1, 3),
        'http_headers': {
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Accept-Encoding': 'gzip, deflate, br',
            'Accept-Charset': 'utf-8, iso-8859-1;q=0.5',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'none',
            'Sec-Fetch-User': '?1',
            'Cache-Control': 'max-age=0',
            'DNT': '1',
            'Referer': 'https://www.youtube.com/',
        },
        'postprocessors': postprocessors,
        'extractor_args': extractor_args,
        'progress_hooks': [],
        # YouTube specific
        'youtube_include_dash_manifest': False,
        'youtube_include_hls_manifest': False,
        'ignore_no_formats_error': True,
        'compat_opts': ['no-youtube-unavailable-videos'],
        'extractor_retries': 3,
        'dynamic_mpd': False,
        'merge_output_format': 'mp4',
        'concurrent_fragment_downloads': 3,  # Download multiple fragments at once
    }

# ========== PROGRESS HOOK ==========
class DownloadProgressHook:
    def __init__(self, task_id):
        self.task_id = task_id
    
    def hook(self, d):
        if self.task_id not in download_tasks:
            return
        
        task = download_tasks[self.task_id]
        
        if d['status'] == 'downloading':
            # Get percentage
            percent_str = d.get('_percent_str', '0%')
            percent = percent_str.replace('%', '').strip()
            
            try:
                percent_float = float(percent) if percent.replace('.', '').isdigit() else 0
                task['progress'] = percent_float
                
                # Create message
                message = f"Downloading: {percent}%"
                if '_speed_str' in d and d['_speed_str']:
                    message += f" ({d['_speed_str']})"
                if '_eta_str' in d and d['_eta_str']:
                    message += f" - ETA: {d['_eta_str']}"
                
                task['message'] = message
                task['status'] = 'downloading'
                
            except ValueError:
                task['progress'] = 0
        
        elif d['status'] == 'finished':
            task['progress'] = 100
            task['status'] = 'processing'
            task['message'] = 'Processing file...'
        
        elif d['status'] == 'error':
            task['status'] = 'error'
            task['message'] = str(d.get('error', 'Unknown error'))

# ========== DOWNLOAD PROCESSING ==========
def process_download(task_id, url, download_type):
    """Process download in background thread"""
    try:
        task = download_tasks[task_id]
        task['status'] = 'preparing'
        task['message'] = 'Initializing download...'
        
        # Generate unique filename
        unique_id = str(uuid.uuid4())[:6]
        
        # Configure yt-dlp
        ydl_opts = get_ydl_options(download_type)
        progress_hook = DownloadProgressHook(task_id)
        ydl_opts['progress_hooks'] = [progress_hook.hook]
        
        # Update task
        task['status'] = 'getting_info'
        task['message'] = 'Fetching video information...'
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            # Get video info with retry
            info = None
            for attempt in range(3):
                try:
                    info = ydl.extract_info(url, download=False)
                    break
                except Exception as e:
                    if attempt == 2:
                        raise
                    time.sleep(2 ** attempt)  # Exponential backoff
                    ydl_opts['user_agent'] = get_random_user_agent()
            
            if not info:
                raise Exception("Failed to get video information")
            
            # Extract info
            title = info.get('title', 'video')
            clean_title = clean_filename(title)
            duration = info.get('duration', 0)
            
            # Update task with video info
            task['title'] = title
            task['duration'] = duration
            task['status'] = 'downloading'
            task['message'] = 'Starting download...'
            
            # Determine file extension
            ext = 'm4a' if download_type == 'audio' else 'mp4'
            filename = f"{clean_title}_{unique_id}.{ext}"
            filepath = os.path.join(app.config['DOWNLOAD_FOLDER'], filename)
            
            # Update output template
            ydl_opts['outtmpl'] = filepath.replace(f'.{ext}', '.%(ext)s')
            
            # Try download with retries
            max_retries = 4
            for attempt in range(max_retries):
                try:
                    ydl = yt_dlp.YoutubeDL(ydl_opts)
                    result = ydl.download([url])
                    
                    # Find the downloaded file
                    downloaded_file = None
                    for file in os.listdir(app.config['DOWNLOAD_FOLDER']):
                        if unique_id in file or clean_title.replace(' ', '_') in file:
                            downloaded_file = os.path.join(app.config['DOWNLOAD_FOLDER'], file)
                            break
                    
                    if not downloaded_file:
                        # Try to find any new file
                        files_before = set(os.listdir(app.config['DOWNLOAD_FOLDER']))
                        time.sleep(2)
                        files_after = set(os.listdir(app.config['DOWNLOAD_FOLDER']))
                        new_files = files_after - files_before
                        if new_files:
                            downloaded_file = os.path.join(app.config['DOWNLOAD_FOLDER'], list(new_files)[0])
                    
                    if not downloaded_file or not os.path.exists(downloaded_file):
                        raise FileNotFoundError("Downloaded file not found")
                    
                    # Get file info
                    filesize = os.path.getsize(downloaded_file)
                    actual_filename = os.path.basename(downloaded_file)
                    
                    # Update task with success
                    task.update({
                        'status': 'completed',
                        'progress': 100,
                        'message': 'Download completed successfully!',
                        'filename': actual_filename,
                        'filesize': filesize,
                        'filepath': downloaded_file,
                        'completed': True,
                        'completed_at': datetime.now().isoformat(),
                        'error': None
                    })
                    
                    print(f"✓ Download completed: {actual_filename} ({filesize} bytes)")
                    return
                    
                except yt_dlp.utils.DownloadError as e:
                    error_msg = str(e)
                    
                    # Check for bot detection
                    if "Sign in" in error_msg or "bot" in error_msg or "confirm you're not a bot" in error_msg:
                        if attempt < max_retries - 1:
                            wait_time = (2 ** attempt) + random.randint(1, 3)
                            task['message'] = f'Retrying in {wait_time}s (attempt {attempt + 1}/{max_retries})...'
                            time.sleep(wait_time)
                            
                            # Rotate user agent
                            ydl_opts['user_agent'] = get_random_user_agent()
                            
                            # Switch to mobile user agent on last retry
                            if attempt == max_retries - 2:
                                mobile_agents = [ua for ua in USER_AGENTS if 'Mobile' in ua or 'Android' in ua or 'iPhone' in ua]
                                if mobile_agents:
                                    ydl_opts['user_agent'] = random.choice(mobile_agents)
                                    task['message'] = 'Trying mobile user agent...'
                        else:
                            # Final failure
                            task.update({
                                'status': 'error',
                                'message': 'YouTube blocked the request. This video requires login or is not available.',
                                'error': 'YouTube bot detection triggered',
                                'completed': False
                            })
                            return
                    else:
                        # Other error
                        if attempt < max_retries - 1:
                            time.sleep(2 ** attempt)
                            continue
                        else:
                            raise
            
    except Exception as e:
        error_msg = str(e)
        print(f"✗ Download error for task {task_id}: {error_msg}")
        
        if task_id in download_tasks:
            download_tasks[task_id].update({
                'status': 'error',
                'message': f'Download failed: {error_msg[:100]}',
                'error': error_msg,
                'completed': False
            })

# ========== FLASK ROUTES ==========
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/download', methods=['POST'])
def start_download():
    """Start a new download"""
    try:
        data = request.get_json()
        
        if not data:
            return jsonify({'success': False, 'error': 'No data provided'}), 400
        
        url = data.get('url', '').strip()
        download_type = data.get('type', 'video')
        
        if not url:
            return jsonify({'success': False, 'error': 'URL is required'}), 400
        
        # Validate URL
        if not url.startswith('http'):
            return jsonify({'success': False, 'error': 'Invalid URL format'}), 400
        
        # Check if it's YouTube
        if 'youtube.com' not in url and 'youtu.be' not in url:
            return jsonify({
                'success': False, 
                'error': 'Currently only YouTube URLs are supported. Support for other platforms coming soon.'
            }), 400
        
        # Generate task ID
        task_id = str(uuid.uuid4())[:8]
        
        # Create task
        download_tasks[task_id] = {
            'id': task_id,
            'url': url,
            'type': download_type,
            'status': 'queued',
            'progress': 0,
            'message': 'Waiting to start...',
            'filename': None,
            'filesize': None,
            'filepath': None,
            'title': None,
            'duration': None,
            'started_at': datetime.now().isoformat(),
            'completed': False,
            'error': None
        }
        
        # Start download in background
        thread = threading.Thread(
            target=process_download,
            args=(task_id, url, download_type),
            daemon=True
        )
        thread.start()
        
        # Update status
        download_tasks[task_id]['status'] = 'starting'
        download_tasks[task_id]['message'] = 'Starting download process...'
        
        return jsonify({
            'success': True,
            'task_id': task_id,
            'message': 'Download started successfully. Please wait...'
        })
        
    except Exception as e:
        print(f"Error in start_download: {str(e)}")
        return jsonify({
            'success': False,
            'error': 'Failed to start download. Please try again.'
        }), 500

@app.route('/api/status/<task_id>', methods=['GET'])
def get_status(task_id):
    """Get download status"""
    if task_id not in download_tasks:
        return jsonify({'error': 'Task not found'}), 404
    
    task = download_tasks[task_id]
    
    # Prepare response
    response = {
        'task_id': task_id,
        'status': task.get('status', 'unknown'),
        'progress': float(task.get('progress', 0)),
        'message': task.get('message', ''),
        'title': task.get('title', ''),
        'filename': task.get('filename'),
        'filesize': task.get('filesize'),
        'type': task.get('type', 'video'),
        'completed': task.get('completed', False),
        'error': task.get('error')
    }
    
    # Clean up completed tasks after 10 minutes
    if task.get('completed') or task.get('status') == 'error':
        completed_time = task.get('completed_at') or task.get('started_at')
        if completed_time:
            try:
                completed_dt = datetime.fromisoformat(completed_time.replace('Z', '+00:00'))
                if (datetime.now() - completed_dt).total_seconds() > 600:  # 10 minutes
                    if task_id in download_tasks:
                        del download_tasks[task_id]
            except:
                pass
    
    return jsonify(response)

@app.route('/api/download-file/<task_id>', methods=['GET'])
def download_file(task_id):
    """Serve the downloaded file"""
    if task_id not in download_tasks:
        return jsonify({'error': 'Task not found'}), 404
    
    task = download_tasks[task_id]
    
    # Check if download is completed
    if not task.get('completed') or not task.get('filepath'):
        return jsonify({'error': 'File not ready or download failed'}), 404
    
    filepath = task['filepath']
    filename = task['filename']
    
    if not os.path.exists(filepath):
        return jsonify({'error': 'File not found on server'}), 404
    
    try:
        # Determine MIME type
        if filename and filename.lower().endswith('.m4a'):
            mimetype = 'audio/mp4'
        elif filename and filename.lower().endswith('.mp4'):
            mimetype = 'video/mp4'
        else:
            mimetype = 'application/octet-stream'
        
        # Send file
        response = send_file(
            filepath,
            as_attachment=True,
            download_name=filename,
            mimetype=mimetype
        )
        
        # Add caching headers
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
        
        return response
        
    except Exception as e:
        print(f"Error serving file: {str(e)}")
        return jsonify({'error': 'Failed to serve file'}), 500

@app.route('/api/info', methods=['POST'])
def get_video_info():
    """Get video information without downloading"""
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': 'No data provided', 'available': False}), 400
        
        url = data.get('url', '').strip()
        if not url:
            return jsonify({'error': 'URL is required', 'available': False}), 400
        
        # Simple yt-dlp options for info only
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'extract_flat': False,
            'force_ipv4': True,
            'socket_timeout': 30,
            'user_agent': get_random_user_agent(),
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            
            return jsonify({
                'title': info.get('title'),
                'duration': info.get('duration'),
                'thumbnail': info.get('thumbnail'),
                'uploader': info.get('uploader'),
                'available': True
            })
            
    except Exception as e:
        error_msg = str(e)
        return jsonify({
            'error': f'Failed to get video info: {error_msg[:100]}',
            'available': False
        }), 500

@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint for Render"""
    return jsonify({
        'status': 'healthy',
        'timestamp': datetime.now().isoformat(),
        'downloads_folder': os.path.exists(DOWNLOAD_FOLDER),
        'active_tasks': len(download_tasks)
    })

# ========== CLEANUP ==========
def cleanup_old_files():
    """Remove files older than 1 hour"""
    try:
        current_time = time.time()
        max_age = 3600  # 1 hour
        
        for filename in os.listdir(DOWNLOAD_FOLDER):
            filepath = os.path.join(DOWNLOAD_FOLDER, filename)
            if os.path.isfile(filepath):
                file_age = current_time - os.path.getmtime(filepath)
                if file_age > max_age:
                    try:
                        os.remove(filepath)
                        print(f"Cleaned up old file: {filename}")
                    except:
                        pass
    except Exception as e:
        print(f"Cleanup error: {e}")

def cleanup_old_tasks():
    """Remove tasks older than 2 hours"""
    try:
        current_time = datetime.now()
        max_age = 7200  # 2 hours
        
        to_remove = []
        for task_id, task in download_tasks.items():
            started_at = task.get('started_at')
            if started_at:
                try:
                    started_dt = datetime.fromisoformat(started_at.replace('Z', '+00:00'))
                    age = (current_time - started_dt).total_seconds()
                    if age > max_age:
                        to_remove.append(task_id)
                except:
                    pass
        
        for task_id in to_remove:
            if task_id in download_tasks:
                del download_tasks[task_id]
    except Exception as e:
        print(f"Task cleanup error: {e}")

@app.before_request
def before_request():
    """Run cleanup before each request"""
    cleanup_old_files()
    cleanup_old_tasks()

# ========== MAIN ==========
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    
    print("=" * 50)
    print("🚀 RON's Downloader Server Starting...")
    print(f"📁 Download folder: {os.path.abspath(DOWNLOAD_FOLDER)}")
    print(f"🌐 Port: {port}")
    print(f"📦 yt-dlp version: {yt_dlp.version.__version__}")
    print("=" * 50)
    
    # Check cookies
    cookies_path = setup_cookies()
    if cookies_path:
        print("✅ Cookies loaded successfully")
    else:
        print("⚠ No cookies file found - YouTube may block some videos")
    
    # Run app
    app.run(
        host='0.0.0.0',
        port=port,
        debug=False,
        threaded=True
    )
