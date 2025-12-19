import os
import time
import random
import yt_dlp
from flask import Flask, request, jsonify, render_template, send_file
import uuid
from pathlib import Path
import json
from datetime import datetime

app = Flask(__name__)

# Configuration
DOWNLOAD_FOLDER = "downloads"
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)
app.config['DOWNLOAD_FOLDER'] = DOWNLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024  # 100MB max file size

# Store download tasks in memory
download_tasks = {}

# User agents for rotation
USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.1 Safari/605.1.15',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Edg/120.0.0.0'
]

def get_random_user_agent():
    return random.choice(USER_AGENTS)

def get_ydl_options(download_type='video'):
    """Get yt-dlp options with anti-bot measures"""
    
    # Format selection based on type
    if download_type == 'audio':
        format_spec = 'bestaudio[ext=m4a]/bestaudio/best'
        postprocessors = [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'm4a',
            'preferredquality': '192',
        }]
    else:  # video
        format_spec = 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best'
        postprocessors = []
    
    output_template = os.path.join(
        app.config['DOWNLOAD_FOLDER'], 
        '%(title).100s.%(ext)s'
    )
    
    # Check if cookies file exists
    cookies_path = 'cookies.txt' if os.path.exists('cookies.txt') else None
    
    return {
        'format': format_spec,
        'outtmpl': output_template,
        'quiet': False,
        'no_warnings': False,
        'ignoreerrors': False,
        'socket_timeout': 30,
        'retries': 10,
        'fragment_retries': 10,
        'skip_unavailable_fragments': True,
        'user_agent': get_random_user_agent(),
        'cookiefile': cookies_path,
        'extract_flat': False,
        'force_ipv4': True,
        'http_headers': {
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-us,en;q=0.5',
            'Accept-Encoding': 'gzip,deflate',
            'Accept-Charset': 'ISO-8859-1,utf-8;q=0.7,*;q=0.7',
            'Connection': 'keep-alive',
            'Referer': 'https://www.youtube.com/',
            'DNT': '1',
            'Upgrade-Insecure-Requests': '1',
        },
        'postprocessors': postprocessors,
        # Add these for better compatibility
        'extractor_args': {
            'youtube': {
                'player_client': ['android', 'web'],
                'player_skip': ['configs', 'webpage'],
            }
        },
        'progress_hooks': [],
    }

def clean_filename(filename):
    """Remove invalid characters from filename"""
    invalid_chars = '<>:"/\\|?*'
    for char in invalid_chars:
        filename = filename.replace(char, '')
    # Replace spaces with underscores and limit length
    filename = filename.replace(' ', '_')[:100]
    return filename

class DownloadProgressHook:
    def __init__(self, task_id):
        self.task_id = task_id
    
    def hook(self, d):
        if d['status'] == 'downloading':
            download_tasks[self.task_id]['progress'] = d.get('_percent_str', '0%').replace('%', '')
            download_tasks[self.task_id]['status'] = 'downloading'
            download_tasks[self.task_id]['message'] = f"Downloading: {d.get('_percent_str', '0%')}"
            if d.get('total_bytes'):
                total_mb = d['total_bytes'] / (1024 * 1024)
                downloaded_mb = d.get('downloaded_bytes', 0) / (1024 * 1024)
                download_tasks[self.task_id]['message'] = f"Downloading: {downloaded_mb:.1f}MB / {total_mb:.1f}MB"
        
        elif d['status'] == 'finished':
            download_tasks[self.task_id]['progress'] = '100'
            download_tasks[self.task_id]['status'] = 'processing'
            download_tasks[self.task_id]['message'] = 'Processing file...'
        
        elif d['status'] == 'error':
            download_tasks[self.task_id]['status'] = 'error'
            download_tasks[self.task_id]['message'] = str(d.get('error', 'Unknown error'))

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/download', methods=['POST'])
def start_download():
    """Start a new download - matches your HTML's API call"""
    try:
        data = request.get_json()
        url = data.get('url')
        download_type = data.get('type', 'video')
        
        if not url:
            return jsonify({'success': False, 'error': 'URL is required'}), 400
        
        # Generate task ID
        task_id = str(uuid.uuid4())[:8]
        
        # Initialize task
        download_tasks[task_id] = {
            'id': task_id,
            'url': url,
            'type': download_type,
            'status': 'initializing',
            'progress': '0',
            'message': 'Starting download...',
            'filename': None,
            'filesize': None,
            'filepath': None,
            'title': None,
            'started_at': datetime.now().isoformat(),
            'completed': False
        }
        
        # Start download in background thread
        import threading
        thread = threading.Thread(target=process_download, args=(task_id, url, download_type))
        thread.daemon = True
        thread.start()
        
        return jsonify({
            'success': True,
            'task_id': task_id,
            'message': 'Download started successfully'
        })
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

def process_download(task_id, url, download_type):
    """Process download in background"""
    try:
        # Update task status
        download_tasks[task_id]['status'] = 'preparing'
        download_tasks[task_id]['message'] = 'Getting video info...'
        
        # Setup yt-dlp
        ydl_opts = get_ydl_options(download_type)
        
        # Add progress hook
        progress_hook = DownloadProgressHook(task_id)
        ydl_opts['progress_hooks'] = [progress_hook.hook]
        
        # Generate unique filename
        filename_suffix = str(uuid.uuid4())[:6]
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            # Get video info
            info = ydl.extract_info(url, download=False)
            title = info.get('title', 'video')
            clean_title = clean_filename(title)
            
            # Update task with info
            download_tasks[task_id]['title'] = title
            download_tasks[task_id]['status'] = 'downloading'
            download_tasks[task_id]['message'] = 'Starting download...'
            
            # Set output template with unique name
            ext = 'm4a' if download_type == 'audio' else 'mp4'
            filename = f"{clean_title}_{filename_suffix}.{ext}"
            filepath = os.path.join(app.config['DOWNLOAD_FOLDER'], filename)
            
            ydl_opts['outtmpl'] = filepath.replace(f'.{ext}', '')
            
            # Actually download
            download_tasks[task_id]['status'] = 'downloading'
            
            # Retry logic
            max_retries = 3
            for attempt in range(max_retries):
                try:
                    ydl = yt_dlp.YoutubeDL(ydl_opts)
                    result = ydl.download([url])
                    
                    # Update task with completion
                    if os.path.exists(filepath):
                        filesize = os.path.getsize(filepath)
                        download_tasks[task_id].update({
                            'status': 'completed',
                            'progress': '100',
                            'message': 'Download completed!',
                            'filename': filename,
                            'filesize': filesize,
                            'filepath': filepath,
                            'completed': True,
                            'completed_at': datetime.now().isoformat()
                        })
                    else:
                        # Try to find the actual file
                        for file in os.listdir(app.config['DOWNLOAD_FOLDER']):
                            if filename_suffix in file:
                                actual_path = os.path.join(app.config['DOWNLOAD_FOLDER'], file)
                                filesize = os.path.getsize(actual_path)
                                download_tasks[task_id].update({
                                    'status': 'completed',
                                    'progress': '100',
                                    'message': 'Download completed!',
                                    'filename': file,
                                    'filesize': filesize,
                                    'filepath': actual_path,
                                    'completed': True,
                                    'completed_at': datetime.now().isoformat()
                                })
                                break
                    
                    break  # Success, exit retry loop
                    
                except yt_dlp.utils.DownloadError as e:
                    error_msg = str(e)
                    if attempt < max_retries - 1:
                        wait_time = 2 ** attempt
                        download_tasks[task_id]['message'] = f'Retry {attempt + 1} in {wait_time}s...'
                        time.sleep(wait_time)
                        # Rotate user agent
                        ydl_opts['user_agent'] = get_random_user_agent()
                    else:
                        raise e
                        
    except Exception as e:
        error_msg = str(e)
        download_tasks[task_id].update({
            'status': 'error',
            'message': f'Download failed: {error_msg}',
            'error': error_msg,
            'completed': False
        })

@app.route('/api/status/<task_id>', methods=['GET'])
def get_status(task_id):
    """Get download status - matches your HTML's API call"""
    if task_id not in download_tasks:
        return jsonify({'error': 'Task not found'}), 404
    
    task = download_tasks[task_id]
    
    # Format progress as number
    progress = task.get('progress', '0')
    if progress.endswith('%'):
        progress = progress.replace('%', '')
    
    response = {
        'task_id': task_id,
        'status': task.get('status', 'unknown'),
        'progress': float(progress) if progress.replace('.', '').isdigit() else 0,
        'message': task.get('message', ''),
        'title': task.get('title', ''),
        'filename': task.get('filename'),
        'filesize': task.get('filesize'),
        'type': task.get('type', 'video'),
        'completed': task.get('completed', False)
    }
    
    return jsonify(response)

@app.route('/api/download-file/<task_id>', methods=['GET'])
def download_file(task_id):
    """Serve the downloaded file - matches your HTML's API call"""
    if task_id not in download_tasks:
        return jsonify({'error': 'Task not found'}), 404
    
    task = download_tasks[task_id]
    
    if not task.get('completed') or not task.get('filepath'):
        return jsonify({'error': 'File not ready'}), 404
    
    filepath = task['filepath']
    filename = task['filename']
    
    if not os.path.exists(filepath):
        return jsonify({'error': 'File not found'}), 404
    
    # Check if it's auto-download
    auto = request.args.get('auto', 'false').lower() == 'true'
    manual = request.args.get('manual', 'false').lower() == 'true'
    
    # For auto-download, force download with special headers
    if auto:
        response = send_file(
            filepath,
            as_attachment=True,
            download_name=filename,
            mimetype='application/octet-stream'
        )
        response.headers['Content-Disposition'] = f'attachment; filename="{filename}"'
        return response
    
    # For manual download
    return send_file(filepath, as_attachment=True, download_name=filename)

@app.route('/api/info', methods=['POST'])
def get_video_info():
    """Get video info without downloading"""
    try:
        data = request.get_json()
        url = data.get('url')
        
        if not url:
            return jsonify({'error': 'URL is required'}), 400
        
        ydl_opts = get_ydl_options()
        ydl_opts['extract_flat'] = False
        
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
        return jsonify({'error': str(e), 'available': False}), 500

# Cleanup old tasks and files
def cleanup_old_files():
    """Remove files and tasks older than 1 hour"""
    import time
    current_time = time.time()
    
    # Clean old tasks
    to_remove = []
    for task_id, task in download_tasks.items():
        started_at = task.get('started_at')
        if started_at:
            try:
                import dateutil.parser
                task_time = dateutil.parser.parse(started_at).timestamp()
                if current_time - task_time > 3600:  # 1 hour
                    to_remove.append(task_id)
            except:
                pass
    
    for task_id in to_remove:
        del download_tasks[task_id]
    
    # Clean old files
    for file in os.listdir(app.config['DOWNLOAD_FOLDER']):
        file_path = os.path.join(app.config['DOWNLOAD_FOLDER'], file)
        if os.path.isfile(file_path):
            file_age = current_time - os.path.getmtime(file_path)
            if file_age > 3600:  # 1 hour
                try:
                    os.remove(file_path)
                except:
                    pass

@app.before_request
def before_request():
    cleanup_old_files()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
