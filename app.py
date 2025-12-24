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
from urllib.parse import urlparse

app = Flask(__name__)

# ========== CONFIGURATION ==========
DOWNLOAD_FOLDER = "downloads"
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)
app.config['DOWNLOAD_FOLDER'] = DOWNLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'secret-key-12345')

# ========== GLOBAL VARIABLES ==========
download_tasks = {}

# ========== DIRECT YT-DLP DOWNLOAD ==========
def download_direct(url, download_type, task_id):
    """Direct download using yt-dlp"""
    try:
        task = download_tasks[task_id]
        
        # SIMPLE BUT EFFECTIVE CONFIG - NO COMPLEX HEADERS THAT BREAK
        ydl_opts = {
            'format': 'bestaudio/best' if download_type == 'audio' else 'best',
            'outtmpl': os.path.join(app.config['DOWNLOAD_FOLDER'], f'{task_id}.%(ext)s'),
            'quiet': True,
            'no_warnings': True,
            'socket_timeout': 30,
            'retries': 10,
            'fragment_retries': 10,
            'ignoreerrors': True,
            'no_check_certificate': True,
            'progress_hooks': [lambda d: progress_hook(d, task_id)],
            # CRITICAL: Add these for YouTube
            'extract_flat': False,
            'postprocessors': [],
        }
        
        if download_type == 'audio':
            ydl_opts['postprocessors'] = [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }]
            ydl_opts['format'] = 'bestaudio/best'
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            # SIMPLE: Just download without getting info first
            result = ydl.download([url])
            
            if result == 0:  # Download successful
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
                        
                        # Try to get title from filename or URL
                        domain = urlparse(url).netloc
                        site_name = domain.replace('www.', '').split('.')[0].capitalize()
                        
                        task.update({
                            'status': 'completed',
                            'progress': 100,
                            'filename': file,
                            'filesize': filesize,
                            'filepath': filepath,
                            'filetype': filetype,
                            'mimetype': mimetype,
                            'title': f'{site_name} Download',
                            'site': site_name,
                            'completed': True,
                            'completed_at': datetime.now().isoformat(),
                        })
                        return True
        
        return False
        
    except Exception as e:
        error_msg = str(e)
        print(f"Download error: {error_msg}")
        task['error'] = error_msg[:200]
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
    """Main download processor"""
    try:
        task = download_tasks[task_id]
        
        if not url.startswith(('http://', 'https://')):
            task.update({
                'status': 'error',
                'message': 'Invalid URL',
                'completed': False
            })
            return
        
        task['status'] = 'starting'
        domain = urlparse(url).netloc.replace('www.', '')
        site_name = domain.split('.')[0].capitalize()
        task['site'] = site_name
        task['message'] = f'Downloading from {site_name}...'
        
        # Direct download
        if download_direct(url, download_type, task_id):
            return
        
        # If failed
        task.update({
            'status': 'error',
            'message': f'Download failed. Try a different URL or check if the site is supported.',
            'completed': False,
        })
        
    except Exception as e:
        print(f"Download processing error: {str(e)}")
        task.update({
            'status': 'error',
            'message': f'Error: {str(e)[:100]}',
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
        url = data.get('url', '').strip()
        download_type = data.get('type', 'video')
        
        if not url:
            return jsonify({'success': False, 'error': 'URL is required'}), 400
        
        if not url.startswith(('http://', 'https://')):
            return jsonify({
                'success': False,
                'error': 'Invalid URL'
            }), 400
        
        task_id = str(uuid.uuid4())[:12]
        
        try:
            domain = urlparse(url).netloc.replace('www.', '')
            site_name = domain.split('.')[0].capitalize()
        except:
            site_name = 'Unknown'
        
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
            'title': None,
            'site': site_name,
            'started_at': datetime.now().isoformat(),
            'completed': False,
            'speed': None,
            'eta': None
        }
        
        thread = threading.Thread(
            target=process_download,
            args=(task_id, url, download_type),
            daemon=True
        )
        thread.start()
        
        return jsonify({
            'success': True,
            'task_id': task_id,
            'message': f'Download started'
        })
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/status/<task_id>', methods=['GET'])
def get_status(task_id):
    """Get status"""
    if task_id not in download_tasks:
        return jsonify({'error': 'Task not found'}), 404
    
    task = download_tasks[task_id]
    
    # Clean old tasks
    if task.get('completed') or task.get('status') == 'error':
        started = datetime.fromisoformat(task['started_at'].replace('Z', '+00:00'))
        if (datetime.now() - started).total_seconds() > 300:
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
    })

@app.route('/api/download-file/<task_id>', methods=['GET'])
def download_file(task_id):
    """Serve file"""
    if task_id not in download_tasks:
        return jsonify({'error': 'Task not found'}), 404
    
    task = download_tasks[task_id]
    
    if not task.get('completed'):
        return jsonify({'error': 'File not ready'}), 404
    
    if task.get('filepath') and os.path.exists(task['filepath']):
        if task.get('title'):
            safe_title = re.sub(r'[<>:"/\\|?*]', '', task['title'])
            safe_title = safe_title.replace('\n', ' ').replace('\r', ' ')
            safe_title = safe_title[:80].strip()
            
            ext = task.get('filetype', '')
            if not ext or ext == 'None':
                ext = '.mp4' if task['type'] == 'video' else '.mp3'
            
            if not ext.startswith('.'):
                ext = '.' + ext
            
            filename = f"{safe_title}{ext}"
        else:
            filename = task.get('filename', 'download.mp4')
        
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
        
        return send_file(
            task['filepath'],
            as_attachment=True,
            download_name=filename,
            mimetype=mimetype
        )
    
    return jsonify({'error': 'File not found'}), 404

@app.route('/health', methods=['GET'])
def health():
    return jsonify({
        'status': 'ok',
        'time': datetime.now().isoformat(),
        'active_tasks': len(download_tasks)
    })

# ========== CLEANUP ==========
def cleanup():
    """Clean old files"""
    try:
        now = time.time()
        for file in os.listdir(DOWNLOAD_FOLDER):
            filepath = os.path.join(DOWNLOAD_FOLDER, file)
            if os.path.isfile(filepath):
                if now - os.path.getmtime(filepath) > 3600:
                    try:
                        os.remove(filepath)
                    except:
                        pass
    except:
        pass

@app.before_request
def before_request():
    cleanup()

# ========== MAIN ==========
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
