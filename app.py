from flask import Flask, render_template, request, jsonify, session, redirect, url_for, send_file
from flask_cors import CORS
import os
import uuid
import hashlib
import secrets
from datetime import datetime, timedelta
import mysql.connector
from mysql.connector import Error
import json
from PIL import Image, ImageEnhance, ImageFilter, ImageOps
import cv2
import numpy as np
import rembg
import io
import time
import logging
import requests
import base64
from functools import wraps
import warnings
warnings.filterwarnings('ignore')

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', secrets.token_hex(32))
CORS(app)

# Configuration
app.config['UPLOAD_FOLDER'] = 'static/uploads'
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max
app.config['ALLOWED_EXTENSIONS'] = {'png', 'jpg', 'jpeg', 'gif', 'bmp', 'webp'}

# Create upload folder if it doesn't exist
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs('static', exist_ok=True)

# ============================================================
# RAPIDAPI CONFIGURATION FOR CARTOON GENERATION
# ============================================================
# Get your FREE API key from RapidAPI:
# 1. Go to https://rapidapi.com/AI-Engine/api/phototoanime1
# 2. Click "Pricing" → Select "Basic" (Free)
# 3. Subscribe and copy your X-RapidAPI-Key
RAPIDAPI_KEY = os.environ.get("RAPIDAPI_KEY", "")
RAPIDAPI_HOST = "phototoanime1.p.rapidapi.com"

# Available cartoon styles
CARTOON_STYLES = {
    "anime": "🎀 Anime",
    "3d": "🎮 3D",
    "handdrawn": "✏️ Hand-drawn",
    "sketch": "📝 Sketch",
    "artstyle": "🎨 Art Style",
    "design": "💎 Design",
    "illustration": "🖼️ Illustration"
}

# Database configuration - UPDATE THESE VALUES
db_config = {
    'host': 'localhost',
    'user': 'root',
    'password': 'root',
    'database': 'image_editor_db',
}

# Global variables
current_image_path = None
current_original_path = None
current_image_id = None
current_user_id = None

def get_db_connection():
    """Create database connection"""
    try:
        connection = mysql.connector.connect(**db_config)
        return connection
    except Error as e:
        logger.error(f"Database connection error: {e}")
        return None

def init_database():
    """Initialize database tables if they don't exist"""
    connection = get_db_connection()
    if connection:
        cursor = connection.cursor()
        
        cursor.execute("CREATE DATABASE IF NOT EXISTS image_editor_db")
        cursor.execute("USE image_editor_db")
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INT AUTO_INCREMENT PRIMARY KEY,
                username VARCHAR(100) UNIQUE NOT NULL,
                email VARCHAR(255) UNIQUE NOT NULL,
                password_hash VARCHAR(255) NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_login TIMESTAMP NULL,
                is_active BOOLEAN DEFAULT TRUE
            )
        """)
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS images (
                id INT AUTO_INCREMENT PRIMARY KEY,
                user_id INT,
                original_path VARCHAR(500) NOT NULL,
                edited_path VARCHAR(500),
                file_name VARCHAR(255),
                file_size INT,
                mime_type VARCHAR(100),
                uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL
            )
        """)
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS edit_history (
                id INT AUTO_INCREMENT PRIMARY KEY,
                image_id INT,
                user_id INT,
                operation_type VARCHAR(50) NOT NULL,
                operation_details TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (image_id) REFERENCES images(id) ON DELETE CASCADE,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL
            )
        """)
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS ai_effects_log (
                id INT AUTO_INCREMENT PRIMARY KEY,
                user_id INT,
                effect_type VARCHAR(50) NOT NULL,
                image_id INT,
                processing_time_ms INT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL,
                FOREIGN KEY (image_id) REFERENCES images(id) ON DELETE SET NULL
            )
        """)
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS cartoon_conversions (
                id VARCHAR(36) PRIMARY KEY,
                user_id INT,
                original_filename VARCHAR(255),
                original_image LONGBLOB,
                cartoon_image LONGBLOB,
                style_used VARCHAR(50),
                method_used VARCHAR(100),
                created_at DATETIME,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL
            )
        """)
        
        connection.commit()
        cursor.close()
        connection.close()
        logger.info("Database initialized successfully")
    else:
        logger.error("Failed to initialize database")

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return jsonify({'error': 'Authentication required', 'redirect': '/login'}), 401
        return f(*args, **kwargs)
    return decorated_function

def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()

def verify_password(password, hash_val):
    return hash_password(password) == hash_val

def save_edit_history(image_id, user_id, operation_type, details):
    if not image_id or not user_id:
        return
    connection = get_db_connection()
    if connection:
        cursor = connection.cursor()
        try:
            cursor.execute("""
                INSERT INTO edit_history (image_id, user_id, operation_type, operation_details)
                VALUES (%s, %s, %s, %s)
            """, (image_id, user_id, operation_type, json.dumps(details)))
            connection.commit()
        except Error as e:
            logger.error(f"Save history error: {e}")
        finally:
            cursor.close()
            connection.close()

def log_ai_effect(user_id, effect_type, image_id, processing_time):
    if not image_id or not user_id:
        return
    connection = get_db_connection()
    if connection:
        cursor = connection.cursor()
        try:
            cursor.execute("""
                INSERT INTO ai_effects_log (user_id, effect_type, image_id, processing_time_ms)
                VALUES (%s, %s, %s, %s)
            """, (user_id, effect_type, image_id, processing_time))
            connection.commit()
        except Error as e:
            logger.error(f"Log AI effect error: {e}")
        finally:
            cursor.close()
            connection.close()

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in app.config['ALLOWED_EXTENSIONS']

def get_unique_filename(original_filename):
    ext = original_filename.rsplit('.', 1)[1].lower() if '.' in original_filename else 'png'
    return f"{uuid.uuid4().hex}.{ext}"

def load_current_image():
    global current_image_path
    if current_image_path and os.path.exists(current_image_path):
        return Image.open(current_image_path).convert('RGB')
    return None

def save_image(image, suffix=""):
    global current_image_path
    if current_image_path:
        base_dir = os.path.dirname(current_image_path)
        base_name = os.path.splitext(os.path.basename(current_image_path))[0]
        ext = os.path.splitext(current_image_path)[1]
    else:
        base_dir = app.config['UPLOAD_FOLDER']
        base_name = "edited_image"
        ext = ".png"
    
    if suffix:
        filename = f"{base_name}_{suffix}{ext}"
    else:
        filename = f"{base_name}_edited{ext}"
    
    save_path = os.path.join(base_dir, filename)
    
    if image.mode == 'RGBA' and (ext.lower() in ['.jpg', '.jpeg'] or suffix == ''):
        rgb_image = Image.new('RGB', image.size, (255, 255, 255))
        rgb_image.paste(image, mask=image.split()[-1] if image.mode == 'RGBA' else None)
        rgb_image.save(save_path, quality=95)
    else:
        image.save(save_path, quality=95)
    
    current_image_path = save_path
    return save_path

# ==================== CARTOON GENERATION FUNCTIONS (from code2) ====================

def cartoon_rapidapi(image_bytes: bytes, style: str = "anime") -> bytes:
    """Convert image to cartoon using PhotoToAnime API (AI-Engine)"""
    logger.info(f"[API] Converting with style: {style}")
    
    url = "https://phototoanime1.p.rapidapi.com/cartoonize"
    
    headers = {
        "x-rapidapi-key": RAPIDAPI_KEY,
        "x-rapidapi-host": RAPIDAPI_HOST,
    }
    
    files = {
        "image": ("image.jpg", image_bytes, "image/jpeg")
    }
    
    data = {
        "style": style
    }
    
    try:
        response = requests.post(url, headers=headers, files=files, data=data, timeout=60)
        
        if response.status_code == 200:
            result = response.json()
            image_url = result.get("image_url") or result.get("output_url") or result.get("url")
            
            if image_url:
                img_response = requests.get(image_url, timeout=30)
                logger.info(f"[API] Success! Style: {style}")
                return img_response.content
            else:
                raise Exception(f"No image URL in response: {result}")
        else:
            raise Exception(f"API returned {response.status_code}: {response.text[:200]}")
            
    except requests.exceptions.Timeout:
        raise Exception("API request timeout - please try again")
    except Exception as e:
        logger.error(f"[API] Error: {e}")
        raise

def cartoon_opencv(image_bytes: bytes) -> bytes:
    """OpenCV cartoon effect (fallback when API fails)"""
    logger.info("[OpenCV] Generating cartoon using OpenCV...")
    
    nparr = np.frombuffer(image_bytes, np.uint8)
    img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    
    if img is None:
        raise ValueError("Could not decode image")
    
    h, w = img.shape[:2]
    if h > 800:
        scale = 800 / h
        new_w = int(w * scale)
        img = cv2.resize(img, (new_w, 800))
    
    for _ in range(5):
        img = cv2.bilateralFilter(img, 9, 75, 75)
    
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    gray = cv2.medianBlur(gray, 5)
    edges = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY, 9, 9)
    edges_rgb = cv2.cvtColor(edges, cv2.COLOR_GRAY2BGR)
    cartoon = cv2.bitwise_and(img, edges_rgb)
    
    hsv = cv2.cvtColor(cartoon, cv2.COLOR_BGR2HSV)
    hsv[:, :, 1] = np.clip(hsv[:, :, 1] * 1.5, 0, 255)
    cartoon = cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)
    
    _, buf = cv2.imencode('.png', cartoon)
    logger.info("[OpenCV] Cartoon generated successfully")
    return buf.tobytes()

def cartoonify(image_bytes: bytes, style: str = "anime"):
    """Main function: Try API first, fallback to OpenCV if needed"""
    if RAPIDAPI_KEY and RAPIDAPI_KEY != "":
        try:
            result = cartoon_rapidapi(image_bytes, style)
            return result, f"AI ({CARTOON_STYLES.get(style, style)})"
        except Exception as e:
            logger.error(f"[!] API failed: {e}")
            logger.info("[!] Falling back to OpenCV...")
            return cartoon_opencv(image_bytes), f"OpenCV (Fallback)"
    else:
        logger.info("[!] No API key found, using OpenCV")
        return cartoon_opencv(image_bytes), "OpenCV (Local)"

def save_cartoon_to_db(user_id, filename, orig_bytes, cartoon_bytes, style, method):
    """Save cartoon conversion to database"""
    record_id = str(uuid.uuid4())
    try:
        connection = get_db_connection()
        if connection:
            cursor = connection.cursor()
            cursor.execute("""
                INSERT INTO cartoon_conversions (id, user_id, original_filename, original_image, cartoon_image, style_used, method_used, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
            """, (record_id, user_id, filename, orig_bytes, cartoon_bytes, style, method))
            connection.commit()
            cursor.close()
            connection.close()
            logger.info(f"[DB] Saved cartoon conversion: {record_id}")
            return record_id
    except Exception as e:
        logger.error(f"[DB] Save error: {e}")
    return None

# ==================== ENHANCED AI EFFECTS (from code1) ====================

def remove_background_advanced(image):
    """Remove background using rembg AI"""
    img_bytes = io.BytesIO()
    image.save(img_bytes, format='PNG')
    img_bytes.seek(0)
    output_bytes = rembg.remove(img_bytes.read())
    result = Image.open(io.BytesIO(output_bytes)).convert('RGBA')
    return result

def enhance_image_advanced(image):
    """Advanced image enhancement"""
    img_array = np.array(image.convert('RGB'))
    img_float = img_array.astype(np.float32) / 255.0
    
    p2 = np.percentile(img_float, 2)
    p98 = np.percentile(img_float, 98)
    stretched = np.clip((img_float - p2) / (p98 - p2), 0, 1)
    
    blurred = cv2.GaussianBlur(stretched, (0, 0), 2.0)
    sharpened = stretched + (stretched - blurred) * 0.8
    sharpened = np.clip(sharpened, 0, 1)
    sharpened_uint8 = (sharpened * 255).astype(np.uint8)
    
    hsv = cv2.cvtColor(sharpened_uint8, cv2.COLOR_RGB2HSV).astype(np.float32)
    hsv[:, :, 1] = np.clip(hsv[:, :, 1] * 1.3, 0, 255)
    enhanced = cv2.cvtColor(hsv.astype(np.uint8), cv2.COLOR_HSV2RGB)
    denoised = cv2.fastNlMeansDenoisingColored(enhanced, None, 5, 5, 7, 21)
    
    return Image.fromarray(denoised)

def apply_filter_to_image(image, filter_type):
    """Apply various filters to image"""
    if filter_type == 'grayscale':
        return image.convert('L').convert('RGB')
    elif filter_type == 'sepia':
        img_array = np.array(image)
        sepia_filter = np.array([[0.393, 0.769, 0.189],
                                  [0.349, 0.686, 0.168],
                                  [0.272, 0.534, 0.131]])
        sepia_img = img_array @ sepia_filter.T
        sepia_img = np.clip(sepia_img, 0, 255).astype(np.uint8)
        return Image.fromarray(sepia_img)
    elif filter_type == 'blur':
        return image.filter(ImageFilter.GaussianBlur(radius=3))
    elif filter_type == 'sharpen':
        return image.filter(ImageFilter.SHARPEN)
    elif filter_type == 'edge_enhance':
        return image.filter(ImageFilter.EDGE_ENHANCE)
    elif filter_type == 'emboss':
        return image.filter(ImageFilter.EMBOSS)
    elif filter_type == 'vibrant':
        enhancer = ImageEnhance.Color(image)
        return enhancer.enhance(1.5)
    elif filter_type == 'invert':
        return ImageOps.invert(image.convert('RGB'))
    elif filter_type == 'vignette':
        img_array = np.array(image)
        rows, cols = img_array.shape[:2]
        kernel_x = cv2.getGaussianKernel(cols, cols/3)
        kernel_y = cv2.getGaussianKernel(rows, rows/3)
        kernel = kernel_y * kernel_x.T
        mask = kernel / kernel.max()
        for i in range(3):
            img_array[:,:,i] = img_array[:,:,i] * mask
        return Image.fromarray(img_array.astype(np.uint8))
    return image

def adjust_brightness_contrast(image, brightness=1.0, contrast=1.0):
    brightness_enhancer = ImageEnhance.Brightness(image)
    image = brightness_enhancer.enhance(brightness)
    contrast_enhancer = ImageEnhance.Contrast(image)
    image = contrast_enhancer.enhance(contrast)
    return image

def rotate_image(image, angle):
    return image.rotate(angle, expand=True, fillcolor=(255,255,255))

def flip_image(image, direction):
    if direction == 'horizontal':
        return ImageOps.mirror(image)
    elif direction == 'vertical':
        return ImageOps.flip(image)
    return image

def crop_image(image, x, y, width, height):
    return image.crop((x, y, x + width, y + height))

def resize_image(image, width, height, maintain_aspect=True):
    if maintain_aspect:
        image.thumbnail((width, height), Image.LANCZOS)
        return image
    else:
        return image.resize((width, height), Image.LANCZOS)

def detect_faces(image):
    img_array = np.array(image.convert('RGB'))
    gray = cv2.cvtColor(img_array, cv2.COLOR_RGB2GRAY)
    face_cascade = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_frontalface_default.xml')
    faces = face_cascade.detectMultiScale(gray, 1.1, 5)
    return faces.tolist() if len(faces) > 0 else []

# ==================== FLASK ROUTES ====================

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/check-session', methods=['GET'])
def check_session():
    if 'user_id' in session:
        return jsonify({'logged_in': True, 'username': session.get('username')})
    return jsonify({'logged_in': False})

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        data = request.get_json()
        username = data.get('username')
        password = data.get('password')
        
        connection = get_db_connection()
        if connection:
            cursor = connection.cursor(dictionary=True)
            cursor.execute("SELECT * FROM users WHERE username = %s", (username,))
            user = cursor.fetchone()
            cursor.close()
            connection.close()
            
            if user and verify_password(password, user['password_hash']):
                session['user_id'] = user['id']
                session['username'] = user['username']
                
                connection = get_db_connection()
                cursor = connection.cursor()
                cursor.execute("UPDATE users SET last_login = NOW() WHERE id = %s", (user['id'],))
                connection.commit()
                cursor.close()
                connection.close()
                
                return jsonify({'success': True, 'username': user['username']})
        
        return jsonify({'success': False, 'error': 'Invalid credentials'})
    
    return render_template('login.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        data = request.get_json()
        username = data.get('username')
        email = data.get('email')
        password = data.get('password')
        
        if not username or not email or not password:
            return jsonify({'success': False, 'error': 'All fields required'})
        
        connection = get_db_connection()
        if connection:
            cursor = connection.cursor()
            try:
                password_hash = hash_password(password)
                cursor.execute(
                    "INSERT INTO users (username, email, password_hash) VALUES (%s, %s, %s)",
                    (username, email, password_hash)
                )
                connection.commit()
                user_id = cursor.lastrowid
                cursor.close()
                connection.close()
                
                session['user_id'] = user_id
                session['username'] = username
                
                return jsonify({'success': True, 'username': username})
            except Error as e:
                return jsonify({'success': False, 'error': 'Username or email already exists'})
    
    return render_template('register.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))

@app.route('/api/upload', methods=['POST'])
def upload_image():
    global current_image_path, current_original_path, current_image_id, current_user_id
    
    if 'image' not in request.files:
        return jsonify({'success': False, 'error': 'No image file'})
    
    file = request.files['image']
    if file.filename == '' or not allowed_file(file.filename):
        return jsonify({'success': False, 'error': 'Invalid file type'})
    
    filename = get_unique_filename(file.filename)
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    file.save(filepath)
    
    user_id = session.get('user_id')
    image_id = None
    
    if user_id:
        connection = get_db_connection()
        if connection:
            cursor = connection.cursor()
            try:
                cursor.execute("""
                    INSERT INTO images (user_id, original_path, edited_path, file_name, file_size, mime_type)
                    VALUES (%s, %s, %s, %s, %s, %s)
                """, (user_id, filepath, filepath, filename, os.path.getsize(filepath), file.content_type))
                connection.commit()
                image_id = cursor.lastrowid
            except Error as e:
                logger.error(f"DB insert error: {e}")
            finally:
                cursor.close()
                connection.close()
    
    current_image_path = filepath
    current_original_path = filepath
    current_image_id = image_id
    current_user_id = user_id
    
    return jsonify({'success': True, 'image_path': f'/{filepath}', 'image_id': image_id})

@app.route('/api/filter', methods=['POST'])
@login_required
def apply_filter():
    global current_image_path, current_image_id, current_user_id
    image = load_current_image()
    if not image:
        return jsonify({'success': False, 'error': 'No image loaded'})
    data = request.get_json()
    filter_type = data.get('filter')
    start_time = int(time.time() * 1000)
    edited_image = apply_filter_to_image(image, filter_type)
    processing_time = int(time.time() * 1000) - start_time
    save_path = save_image(edited_image, filter_type)
    if current_image_id and current_user_id:
        save_edit_history(current_image_id, current_user_id, 'filter', {'type': filter_type})
        log_ai_effect(current_user_id, f'filter_{filter_type}', current_image_id, processing_time)
    return jsonify({'success': True, 'edited_image': f'/{save_path}'})

# ==================== CARTOON GENERATION ROUTES (from code2) ====================

@app.route('/api/cartoon-convert', methods=['POST'])
@login_required
def cartoon_convert():
    """Convert uploaded image to cartoon using AI API or OpenCV"""
    global current_image_path, current_image_id, current_user_id
    
    if not current_image_path or not os.path.exists(current_image_path):
        return jsonify({'success': False, 'error': 'No image loaded'})
    
    # Get style preference
    data = request.get_json() or {}
    style = data.get('style', 'anime')
    
    # Read image bytes
    with open(current_image_path, 'rb') as f:
        image_bytes = f.read()
    
    try:
        # Generate cartoon
        cartoon_bytes, method = cartoonify(image_bytes, style)
        
        # Save cartoon to file
        cartoon_filename = f"cartoon_{style}_{uuid.uuid4().hex[:8]}.png"
        cartoon_path = os.path.join(app.config['UPLOAD_FOLDER'], cartoon_filename)
        
        with open(cartoon_path, 'wb') as f:
            f.write(cartoon_bytes)
        
        # Save to database
        if current_user_id:
            save_cartoon_to_db(
                current_user_id, 
                os.path.basename(current_image_path),
                image_bytes, 
                cartoon_bytes, 
                style, 
                method
            )
            save_edit_history(current_image_id, current_user_id, 'cartoon_convert', {'style': style, 'method': method})
        
        # Update current image to the cartoon version
        current_image_path = cartoon_path
        
        # Convert to base64 for response
        cart_b64 = base64.b64encode(cartoon_bytes).decode()
        
        return jsonify({
            'success': True, 
            'edited_image': f'/{cartoon_path}',
            'cartoon_data': f'data:image/png;base64,{cart_b64}',
            'method': method,
            'style': style
        })
    except Exception as e:
        logger.error(f"Cartoon conversion failed: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/cartoon-styles', methods=['GET'])
def get_cartoon_styles():
    """Get available cartoon styles"""
    return jsonify({
        'success': True,
        'styles': CARTOON_STYLES,
        'api_configured': bool(RAPIDAPI_KEY)
    })

@app.route('/api/cartoon-history', methods=['GET'])
@login_required
def get_cartoon_history():
    """Get user's cartoon conversion history"""
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'success': False, 'error': 'Not logged in'})
    
    try:
        connection = get_db_connection()
        history = []
        if connection:
            cursor = connection.cursor(dictionary=True)
            cursor.execute("""
                SELECT id, original_filename, style_used, method_used, created_at
                FROM cartoon_conversions
                WHERE user_id = %s
                ORDER BY created_at DESC
                LIMIT 20
            """, (user_id,))
            history = cursor.fetchall()
            cursor.close()
            connection.close()
        
        return jsonify({'success': True, 'history': history})
    except Exception as e:
        logger.error(f"History error: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/background-remove', methods=['POST'])
@login_required
def background_remove():
    global current_image_id, current_user_id
    image = load_current_image()
    if not image:
        return jsonify({'success': False, 'error': 'No image loaded'})
    start_time = int(time.time() * 1000)
    try:
        result_image = remove_background_advanced(image)
        processing_time = int(time.time() * 1000) - start_time
        save_path = save_image(result_image, 'nobg')
        if current_image_id and current_user_id:
            save_edit_history(current_image_id, current_user_id, 'ai_effect', {'type': 'background_remove'})
            log_ai_effect(current_user_id, 'background_remove', current_image_id, processing_time)
        return jsonify({'success': True, 'edited_image': f'/{save_path}'})
    except Exception as e:
        logger.error(f"Background removal failed: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/enhance', methods=['POST'])
@login_required
def enhance():
    global current_image_id, current_user_id
    image = load_current_image()
    if not image:
        return jsonify({'success': False, 'error': 'No image loaded'})
    start_time = int(time.time() * 1000)
    enhanced_image = enhance_image_advanced(image)
    processing_time = int(time.time() * 1000) - start_time
    save_path = save_image(enhanced_image, 'enhanced')
    if current_image_id and current_user_id:
        save_edit_history(current_image_id, current_user_id, 'ai_effect', {'type': 'enhance'})
        log_ai_effect(current_user_id, 'enhance', current_image_id, processing_time)
    return jsonify({'success': True, 'edited_image': f'/{save_path}'})

@app.route('/api/rotate', methods=['POST'])
@login_required
def rotate():
    image = load_current_image()
    if not image:
        return jsonify({'success': False, 'error': 'No image loaded'})
    data = request.get_json()
    angle = data.get('angle', 90)
    rotated_image = rotate_image(image, angle)
    save_path = save_image(rotated_image, f'rotated_{angle}')
    return jsonify({'success': True, 'edited_image': f'/{save_path}'})

@app.route('/api/flip', methods=['POST'])
@login_required
def flip():
    image = load_current_image()
    if not image:
        return jsonify({'success': False, 'error': 'No image loaded'})
    data = request.get_json()
    direction = data.get('direction', 'horizontal')
    flipped_image = flip_image(image, direction)
    save_path = save_image(flipped_image, f'flipped_{direction}')
    return jsonify({'success': True, 'edited_image': f'/{save_path}'})

@app.route('/api/crop', methods=['POST'])
@login_required
def crop():
    image = load_current_image()
    if not image:
        return jsonify({'success': False, 'error': 'No image loaded'})
    data = request.get_json()
    x = int(data.get('x', 0))
    y = int(data.get('y', 0))
    width = int(data.get('width', image.width))
    height = int(data.get('height', image.height))
    cropped_image = crop_image(image, x, y, width, height)
    save_path = save_image(cropped_image, 'cropped')
    return jsonify({'success': True, 'edited_image': f'/{save_path}'})

@app.route('/api/resize', methods=['POST'])
@login_required
def resize():
    image = load_current_image()
    if not image:
        return jsonify({'success': False, 'error': 'No image loaded'})
    data = request.get_json()
    width = int(data.get('width', image.width))
    height = int(data.get('height', image.height))
    maintain_aspect = data.get('maintain_aspect', True)
    resized_image = resize_image(image, width, height, maintain_aspect)
    save_path = save_image(resized_image, f'resized_{width}x{height}')
    return jsonify({'success': True, 'edited_image': f'/{save_path}'})

@app.route('/api/adjust', methods=['POST'])
@login_required
def adjust():
    image = load_current_image()
    if not image:
        return jsonify({'success': False, 'error': 'No image loaded'})
    data = request.get_json()
    brightness = float(data.get('brightness', 1.0))
    contrast = float(data.get('contrast', 1.0))
    adjusted_image = adjust_brightness_contrast(image, brightness, contrast)
    save_path = save_image(adjusted_image, 'adjusted')
    return jsonify({'success': True, 'edited_image': f'/{save_path}'})

@app.route('/api/face-detect', methods=['POST'])
@login_required
def face_detect():
    image = load_current_image()
    if not image:
        return jsonify({'success': False, 'error': 'No image loaded'})
    faces = detect_faces(image)
    return jsonify({'success': True, 'face_count': len(faces), 'faces': faces})

@app.route('/api/reset', methods=['POST'])
@login_required
def reset():
    global current_image_path, current_original_path
    if current_original_path and os.path.exists(current_original_path):
        current_image_path = current_original_path
        return jsonify({'success': True, 'edited_image': f'/{current_original_path}'})
    return jsonify({'success': False, 'error': 'No original image to reset'})

@app.route('/api/download', methods=['POST'])
@login_required
def download():
    global current_image_path
    if current_image_path and os.path.exists(current_image_path):
        original_filename = os.path.basename(current_image_path)
        download_name = f"visioncraft_edited_{original_filename}"
        return send_file(current_image_path, as_attachment=True, download_name=download_name)
    return jsonify({'success': False, 'error': 'No image to download'}), 404

@app.route('/api/history', methods=['GET'])
@login_required
def get_history():
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'success': False, 'error': 'Not logged in'})
    connection = get_db_connection()
    history = []
    if connection:
        cursor = connection.cursor(dictionary=True)
        try:
            cursor.execute("""
                SELECT eh.operation_type, eh.operation_details, eh.created_at, i.file_name
                FROM edit_history eh
                JOIN images i ON eh.image_id = i.id
                WHERE eh.user_id = %s
                ORDER BY eh.created_at DESC
                LIMIT 50
            """, (user_id,))
            history = cursor.fetchall()
        except Error as e:
            logger.error(f"History query error: {e}")
        finally:
            cursor.close()
            connection.close()
    return jsonify({'success': True, 'history': history})

@app.route('/api/stats', methods=['GET'])
@login_required
def get_stats():
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'success': False, 'error': 'Not logged in'})
    connection = get_db_connection()
    stats = {
        'total_images': 0,
        'total_ai_effects': 0,
        'most_used_effect': 'None',
        'total_edits': 0
    }
    if connection:
        cursor = connection.cursor(dictionary=True)
        try:
            cursor.execute("SELECT COUNT(*) as total FROM images WHERE user_id = %s", (user_id,))
            result = cursor.fetchone()
            stats['total_images'] = result['total'] if result else 0
            cursor.execute("SELECT COUNT(*) as total FROM ai_effects_log WHERE user_id = %s", (user_id,))
            result = cursor.fetchone()
            stats['total_ai_effects'] = result['total'] if result else 0
            cursor.execute("""
                SELECT effect_type, COUNT(*) as count 
                FROM ai_effects_log 
                WHERE user_id = %s 
                GROUP BY effect_type 
                ORDER BY count DESC 
                LIMIT 1
            """, (user_id,))
            result = cursor.fetchone()
            stats['most_used_effect'] = result['effect_type'] if result else 'None'
            cursor.execute("SELECT COUNT(*) as total FROM edit_history WHERE user_id = %s", (user_id,))
            result = cursor.fetchone()
            stats['total_edits'] = result['total'] if result else 0
        except Error as e:
            logger.error(f"Stats query error: {e}")
        finally:
            cursor.close()
            connection.close()
    return jsonify({'success': True, 'stats': stats})

if __name__ == '__main__':
    init_database()
    app.run(debug=True, host='0.0.0.0', port=5000)