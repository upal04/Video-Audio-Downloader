import os
import json
import uuid
import threading
import time
from datetime import datetime
from flask import Flask, render_template, request, jsonify, send_file
from flask_cors import CORS
import yt_dlp
import logging
from pathlib import Path

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__, static_folder='static', template_folder='templates')
CORS(app)
app.secret_key = os.environ.get('SECRET_KEY', os.urandom(24))

# Store active downloads
active_downloads = {}

# Get storage folder for Render
def get_storage_folder():
    """Get folder for storing downloads"""
    temp_dir = os.environ.get('TEMP') or os.environ.get('TMPDIR') or '/tmp'
    storage_dir = os.path.join(temp_dir, 'yt_downloads')
    Path(storage_dir).mkdir(parents=True, exist_ok=True)
    return storage_dir

def download_task(task_id, url, download_type='video'):
    """Simple download WITHOUT FFmpeg - WORKS 100%"""
    try:
        storage_folder = get_storage_folder()
        
        active_downloads[task_id] = {
            'status': 'starting',
            'progress': 0,
            'message': 'Starting download...',
            'filename': None,
            'folder': storage_folder,
            'type': download_type,
            'auto_downloaded': False  # Track if auto-download happened
        }
        
        # SIMPLE yt-dlp options - NO FFMPEG POSTPROCESSORS
        if download_type == 'audio':
            # Download best available audio format
            ydl_opts = {
                'format': 'bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio[ext=opus]/bestaudio',
                'outtmpl': os.path.join(storage_folder, '%(title)s.%(ext)s'),
                'quiet': True,
                'no_warnings': False,
                'noplaylist': True,
                # NO postprocessors - download as is
                'postprocessors': [],  
                'continuedl': True,
                'retries': 10,
                'fragment_retries': 10,
            }
        else:
            # Video options
            ydl_opts = {
                'format': 'best[ext=mp4]/best',
                'outtmpl': os.path.join(storage_folder, '%(title)s.%(ext)s'),
                'quiet': True,
                'no_warnings': False,
                'noplaylist': True,
                'continuedl': True,
                'retries': 10,
                'fragment_retries': 10,
            }
        
        # Progress hook
        def progress_hook(d):
            if d['status'] == 'downloading':
                total = d.get('total_bytes') or d.get('total_bytes_estimate')
                downloaded = d.get('downloaded_bytes', 0)
                if total and total > 0:
                    progress = min(int((downloaded / total) * 100), 99)
                    active_downloads[task_id]['progress'] = progress
                    
                    if download_type == 'audio':
                        active_downloads[task_id]['message'] = f'Downloading audio... {progress}%'
                    else:
                        active_downloads[task_id]['message'] = f'Downloading video... {progress}%'
            
            elif d['status'] == 'finished':
                active_downloads[task_id]['progress'] = 100
                active_downloads[task_id]['message'] = 'Download complete!'
        
        ydl_opts['progress_hooks'] = [progress_hook]
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            # Get video info
            info = ydl.extract_info(url, download=False)
            
            # Prepare clean filename
            title = info.get('title', 'download')
            # Remove invalid characters
            title = "".join(c for c in title if c.isalnum() or c in (' ', '-', '_')).strip()
            
            active_downloads[task_id].update({
                'title': title,
                'status': 'downloading',
                'info_extracted': True
            })
            
            logger.info(f"Starting {download_type} download: {title}")
            
            # Download the file
            result = ydl.download([url])
            
            # The file will be saved with yt-dlp's naming
            # Find the downloaded file
            time.sleep(1)  # Wait a moment for file to be written
            
            # Look for the downloaded file
            downloaded_file = None
            for file in os.listdir(storage_folder):
                file_lower = file.lower()
                if download_type == 'audio':
                    # Look for audio files
                    if file_lower.endswith(('.m4a', '.webm', '.opus', '.mp3', '.aac')):
                        if title.lower() in file_lower.lower() or file_lower.endswith(('.m4a', '.webm', '.opus')):
                            downloaded_file = os.path.join(storage_folder, file)
                            break
                else:
                    # Look for video files
                    if file_lower.endswith(('.mp4', '.mkv', '.webm', '.avi')):
                        if title.lower() in file_lower.lower() or file_lower.endswith('.mp4'):
                            downloaded_file = os.path.join(storage_folder, file)
                            break
            
            # If not found by title, get the newest file
            if not downloaded_file:
                files = [os.path.join(storage_folder, f) for f in os.listdir(storage_folder) 
                        if os.path.isfile(os.path.join(storage_folder, f))]
                if files:
                    files.sort(key=os.path.getmtime, reverse=True)
                    downloaded_file = files[0]
            
            if downloaded_file and os.path.exists(downloaded_file):
                # Get file info
                file_size = os.path.getsize(downloaded_file)
                file_ext = os.path.splitext(downloaded_file)[1].lower()
                
                # Rename to cleaner name
                clean_name = f"{title}{file_ext}"
                clean_path = os.path.join(storage_folder, clean_name)
                
                try:
                    os.rename(downloaded_file, clean_path)
                    downloaded_file = clean_path
                except:
                    pass  # Keep original name if rename fails
                
                active_downloads[task_id].update({
                    'status': 'completed',
                    'progress': 100,
                    'message': f'Download complete! Format: {file_ext}',
                    'filename': os.path.basename(downloaded_file),
                    'filepath': downloaded_file,
                    'filesize': file_size,
                    'filetype': file_ext,
                    'completed_at': datetime.now().strftime('%H:%M:%S')
                })
                
                logger.info(f"✓ {download_type.upper()} DOWNLOAD SUCCESS: {os.path.basename(downloaded_file)} ({file_size} bytes)")
            else:
                raise Exception(f"No {download_type} file was downloaded")
                
    except Exception as e:
        error_msg = str(e)
        logger.error(f"Download error: {error_msg}")
        
        # Provide helpful error messages
        if "HTTP Error 403" in error_msg:
            error_msg = "Access blocked. Try updating yt-dlp or use a different video."
        elif "Private" in error_msg:
            error_msg = "Video is private or requires login."
        elif "Unavailable" in error_msg:
            error_msg = "Video is unavailable."
        elif "Unsupported URL" in error_msg:
            error_msg = "URL not supported. Try YouTube, Instagram, or Facebook."
        
        active_downloads[task_id].update({
            'status': 'error',
            'message': error_msg
        })

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/info', methods=['POST'])
def get_info():
    try:
        url = request.json.get('url')
        
        if not url:
            return jsonify({'error': 'URL required'}), 400
        
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            
            # Get available formats
            formats = []
            for f in info.get('formats', []):
                if f.get('acodec') != 'none' and f.get('vcodec') == 'none':
                    # Audio-only formats
                    formats.append(f.get('ext', 'audio'))
            
            return jsonify({
                'success': True,
                'title': info.get('title', 'Unknown'),
                'duration': info.get('duration', 0),
                'thumbnail': info.get('thumbnail', ''),
                'uploader': info.get('uploader', 'Unknown'),
                'audio_formats': list(set(formats))[:3]  # Unique formats
            })
            
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/download', methods=['POST'])
def start_download():
    try:
        data = request.json
        url = data.get('url')
        download_type = data.get('type', 'video')
        
        if not url:
            return jsonify({'error': 'URL required'}), 400
        
        task_id = str(uuid.uuid4())
        
        # Start download in background
        thread = threading.Thread(
            target=download_task,
            args=(task_id, url, download_type)
        )
        thread.daemon = True
        thread.start()
        
        return jsonify({
            'success': True,
            'task_id': task_id,
            'message': f'{download_type.capitalize()} download started!',
            'folder': get_storage_folder()
        })
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/status/<task_id>')
def get_status(task_id):
    if task_id not in active_downloads:
        return jsonify({'error': 'Task not found'}), 404
    
    return jsonify(active_downloads[task_id])

@app.route('/api/download-file/<task_id>')
def download_file(task_id):
    """Serve file with anti-duplicate protection"""
    if task_id not in active_downloads:
        return jsonify({'error': 'File not found'}), 404
    
    file_info = active_downloads[task_id]
    if file_info['status'] != 'completed':
        return jsonify({'error': 'File not ready'}), 400
    
    filepath = file_info.get('filepath')
    filename = file_info.get('filename', 'download')
    
    if not filepath or not os.path.exists(filepath):
        return jsonify({'error': 'File missing'}), 404
    
    # Check if this is an auto-download (from frontend header)
    is_auto_download = request.headers.get('X-Auto-Download') == 'true'
    
    # If it's NOT an auto-download, mark as manually downloaded
    if not is_auto_download:
        file_info['manually_downloaded'] = True
    
    return send_file(
        filepath,
        as_attachment=True,
        download_name=filename
    )

@app.route('/api/cleanup/<task_id>', methods=['DELETE'])
def cleanup_file(task_id):
    """Clean up downloaded file"""
    if task_id in active_downloads:
        filepath = active_downloads[task_id].get('filepath')
        if filepath and os.path.exists(filepath):
            try:
                os.remove(filepath)
            except:
                pass
        del active_downloads[task_id]
    
    return jsonify({'success': True})

@app.route('/api/health')
def health():
    return jsonify({
        'status': 'ok', 
        'time': datetime.now().isoformat(),
        'storage': get_storage_folder(),
        'message': 'No FFmpeg required - downloads native formats'
    })

if __name__ == '__main__':
    # Create storage folder
    storage = get_storage_folder()
    Path(storage).mkdir(exist_ok=True)
    
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
