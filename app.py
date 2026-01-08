import os
import sys
import zipfile
from flask import Flask, render_template, request, redirect, url_for, send_file, send_from_directory
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func
from werkzeug.utils import secure_filename
from datetime import datetime
from io import BytesIO
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.graphics.barcode import code128
from reportlab.graphics import renderPDF
from PIL import Image

app = Flask(__name__)

# CONFIGURATION
# --- CONFIGURATION (UPDATED FOR EXE SUPPORT) ---
# --- CONFIGURATION (UPDATED FOR DATA DIRECTORY) ---
if getattr(sys, 'frozen', False):
    # If EXE: Base dir is where the EXE lives
    BASE_DIR = os.path.dirname(sys.executable)
else:
    # If Script: Base dir is where the script lives
    BASE_DIR = os.path.abspath(os.path.dirname(__file__))

# 1. Define the DATA directory
DATA_DIR = os.path.join(BASE_DIR, 'Data')
if not os.path.exists(DATA_DIR):
    os.makedirs(DATA_DIR)

# 2. Define IMAGES directory (Data/Images)
IMAGES_DIR = os.path.join(DATA_DIR, 'Images')
if not os.path.exists(IMAGES_DIR):
    os.makedirs(IMAGES_DIR)

# 3. Database Config
# Main DB (Film Log)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(DATA_DIR, 'filmlog.db')

# New DB (Gear Log) - Binds allows multiple DB files
app.config['SQLALCHEMY_BINDS'] = {
    'gear': 'sqlite:///' + os.path.join(DATA_DIR, 'gearlog.db')
}
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# 4. Upload Config (Lives in Data/Images)
app.config['UPLOAD_FOLDER'] = IMAGES_DIR
app.config['ALLOWED_EXTENSIONS'] = {'jpg', 'jpeg', 'png'}

# Ensure upload directory exists
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

db = SQLAlchemy(app)

# DATABASE MODELS
class Roll(db.Model):
    id = db.Column(db.Integer, primary_key=True) 
    film_type = db.Column(db.String(100), nullable=False)
    iso = db.Column(db.Integer)            
    camera = db.Column(db.String(100))
    lens = db.Column(db.String(100))
    date_started = db.Column(db.Date)
    date_finished = db.Column(db.Date)
    date_added = db.Column(db.DateTime, default=datetime.utcnow)
    contact_sheet = db.Column(db.String(200)) 
    notes = db.Column(db.Text)
    
    @property
    def formatted_id(self):
        """Returns ID as 0001 string"""
        return f"{self.id:04d}"
        
 # Settings for GearLog       
class Gear(db.Model):
    __bind_key__ = 'gear'  # Tells Flask to save this in gearlog.db
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False) # e.g. "Canon AE-1"
    hardware_type = db.Column(db.String(50), nullable=False) # Camera, Lens, etc
    serial_number = db.Column(db.String(100))
    date_added = db.Column(db.DateTime, default=datetime.utcnow)

class AppSetting(db.Model):
    # Stores global app preferences
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(50), unique=True, nullable=False)
    value = db.Column(db.String(50)) # "true" or "false"
# HELPER FUNCTIONS
def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in app.config['ALLOWED_EXTENSIONS']

# NEW: Image Optimization Function
def save_optimized_image(file_storage, filename):
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    
    # Open the image using Pillow
    img = Image.open(file_storage)
    
    # 1. Convert to RGB (Fixes issues with some PNGs/TIFFs when saving as JPEG)
    if img.mode in ("RGBA", "P"):
        img = img.convert("RGB")
        
    # 2. Resize to fit within 4K Box (3840x2160)
    # 'thumbnail' modifies the image in-place and PRESERVES ASPECT RATIO.
    # It will never stretch or crop; it just shrinks it until it fits.
    img.thumbnail((3840, 2160), Image.Resampling.LANCZOS)
    
    # 3. Save with optimization
    # quality=85 reduces file size by ~60-80% with no visible quality loss
    img.save(filepath, optimize=True, quality=85)

# ROUTES
# Makes 'gear_enabled' variable available to ALL templates automatically

@app.route('/shutdown', methods=['POST'])
def shutdown():
    # Force the application to exit
    os._exit(0)

@app.route('/save_advanced', methods=['POST'])
def save_advanced():
    # 1. Get Form Data
    port = request.form.get('server_port', '5000')
    host_type = request.form.get('server_host') # 'local' or 'network'
    
    # 2. Determine IP Address
    # '0.0.0.0' = Available to whole network
    # '127.0.0.1' = Localhost only (Private)
    new_host = '0.0.0.0' if host_type == 'network' else '127.0.0.1'
    
    # 3. Save Port
    p_setting = AppSetting.query.filter_by(key='server_port').first()
    if not p_setting:
        p_setting = AppSetting(key='server_port', value=port)
        db.session.add(p_setting)
    else:
        p_setting.value = port
        
    # 4. Save Host
    h_setting = AppSetting.query.filter_by(key='server_host').first()
    if not h_setting:
        h_setting = AppSetting(key='server_host', value=new_host)
        db.session.add(h_setting)
    else:
        h_setting.value = new_host
        
    db.session.commit()
    
    return redirect(url_for('preferences'))


# Makes variables available to ALL templates automatically
@app.context_processor
def inject_globals():
    # 1. Gear Setting
    gear_setting = AppSetting.query.filter_by(key='enable_gearlog').first()
    is_enabled = gear_setting.value == 'true' if gear_setting else False
    
    # 2. Network Settings (Get value from DB)
    port_setting = AppSetting.query.filter_by(key='server_port').first()
    db_port = port_setting.value if port_setting else '5000'  # <--- Defined as db_port
    
    host_setting = AppSetting.query.filter_by(key='server_host').first()
    db_host = host_setting.value if host_setting else '0.0.0.0' # <--- Defined as db_host
    
    # 3. CHECK FOR PENDING RESTART
    # Compare DB (Future) vs Running (Current)
    pending_changes = False
    
    # Use str() to ensure we don't crash comparing numbers to strings
    if str(db_port) != str(app.config.get('RUNNING_PORT', 5000)):
        pending_changes = True
    
    if str(db_host) != str(app.config.get('RUNNING_HOST', '0.0.0.0')):
        pending_changes = True
    
    # Return using the keys the template expects
    return dict(gear_enabled=is_enabled, 
                current_port=db_port, 
                current_host=db_host, 
                pending_changes=pending_changes)
    
    # --- GEAR ROUTES ---

@app.route('/gear')
def gear_index():
    # Security check: if disabled, kick them back home
    setting = AppSetting.query.filter_by(key='enable_gearlog').first()
    if not setting or setting.value != 'true':
        return redirect(url_for('index'))
        
    gear_list = Gear.query.order_by(Gear.hardware_type.asc(), Gear.name.asc()).all()
    return render_template('gear.html', gear_list=gear_list)

@app.route('/gear/add', methods=['GET', 'POST'])
def add_gear():
    if request.method == 'POST':
        name = request.form['name']
        hw_type = request.form['hardware_type']
        serial = request.form['serial_number']
        
        new_gear = Gear(name=name, hardware_type=hw_type, serial_number=serial)
        db.session.add(new_gear)
        db.session.commit()
        return redirect(url_for('gear_index'))
        
    return render_template('add_gear.html')

@app.route('/gear/delete/<int:id>', methods=['POST'])
def delete_gear(id):
    item = Gear.query.get_or_404(id)
    db.session.delete(item)
    db.session.commit()
    return redirect(url_for('gear_index'))

# --- SETTINGS ROUTE (For the Toggle) ---
@app.route('/toggle_feature/<feature_key>', methods=['POST'])
def toggle_feature(feature_key):
    setting = AppSetting.query.filter_by(key=feature_key).first()
    
    # If setting doesn't exist yet, create it
    if not setting:
        setting = AppSetting(key=feature_key, value='true')
        db.session.add(setting)
    else:
        # Toggle 'true' <-> 'false'
        if setting.value == 'true':
            setting.value = 'false'
        else:
            setting.value = 'true'
            
    db.session.commit()
    return redirect(url_for('preferences'))

@app.route('/', methods=['GET', 'POST'])
def index():
    search_query = request.args.get('q', '')
    results = []
    
    if search_query:
        # Check if search is a Roll ID (e.g., 0004 or 4)
        if search_query.isdigit():
            results = Roll.query.filter_by(id=int(search_query)).all()
        else:
            # Search by Film Type or Camera (partial match)
            results = Roll.query.filter(
                (Roll.film_type.contains(search_query)) | 
                (Roll.camera.contains(search_query))
            ).all()
    else:
        # Show recent 5 rolls if no search
        results = Roll.query.order_by(Roll.id.desc()).limit(5).all()

    return render_template('index.html', results=results, search_query=search_query)

@app.route('/add', methods=['GET', 'POST'])
def add_roll():
    # --- GET REQUEST (Calculate Next ID and Load Data for Autocomplete) ---
    last_roll = db.session.query(func.max(Roll.id)).scalar() or 0
    next_id = last_roll + 1
    
    # Autocomplete Lists
    existing_films = [r.film_type for r in db.session.query(Roll.film_type).distinct()]
    existing_cameras = [r.camera for r in db.session.query(Roll.camera).distinct()]
    existing_lenses = [r.lens for r in db.session.query(Roll.lens).distinct() if r.lens]

    if request.method == 'POST':
        # 1. Get the Custom Roll ID
        try:
            custom_id = int(request.form.get('roll_id'))
        except (ValueError, TypeError):
            custom_id = next_id

        # 2. VALIDATION: Check if this ID is already taken
        if Roll.query.get(custom_id):
            error_msg = f"⚠️ Error: Roll #{custom_id:04d} already exists in the database."
            return render_template('add_roll.html', error=error_msg, next_id=next_id,
                                   films=existing_films, cameras=existing_cameras, lenses=existing_lenses)

        # 3. Get Other Data
        film_type = request.form['film_type']
        camera = request.form['camera']
        lens = request.form.get('lens')
        notes = request.form['notes']
        
        iso_input = request.form.get('iso')
        iso = int(iso_input) if iso_input else None
        
        # Date Logic
        def parse_date(date_str):
            if date_str:
                try:
                    return datetime.strptime(date_str, '%Y-%m-%d').date()
                except ValueError:
                    return None
            return None

        date_started = parse_date(request.form.get('date_started'))
        date_finished = parse_date(request.form.get('date_finished'))
        
        # Image Upload
        file = request.files['contact_sheet']
        filename = None
        if file and allowed_file(file.filename):
            filename = secure_filename(f"{datetime.now().timestamp()}_{file.filename}")
            save_optimized_image(file, filename)

        # 4. SAVE
        new_roll = Roll(
            id=custom_id, 
            film_type=film_type, 
            iso=iso,
            camera=camera, 
            lens=lens,
            date_started=date_started,
            date_finished=date_finished,
            notes=notes, 
            contact_sheet=filename
        )
        
        try:
            db.session.add(new_roll)
            db.session.commit()
            return redirect(url_for('index'))
        except Exception as e:
            db.session.rollback()
            return render_template('add_roll.html', error="Database Error: " + str(e), next_id=custom_id,
                                   films=existing_films, cameras=existing_cameras, lenses=existing_lenses)
        
    return render_template('add_roll.html', next_id=next_id, 
                           films=existing_films, 
                           cameras=existing_cameras, 
                           lenses=existing_lenses)

@app.route('/roll/<int:roll_id>')
def roll_detail(roll_id):
    roll = Roll.query.get_or_404(roll_id)
    return render_template('roll_detail.html', roll=roll)

@app.route('/edit/<int:roll_id>', methods=['GET', 'POST'])
def edit_roll(roll_id):
    roll = Roll.query.get_or_404(roll_id)
    
    if request.method == 'POST':
        roll.film_type = request.form['film_type']
        roll.camera = request.form['camera']
        roll.lens = request.form.get('lens') 
        roll.notes = request.form['notes']
        iso_input = request.form.get('iso')
        roll.iso = int(iso_input) if iso_input else None
        
        def parse_date(date_str):
            if date_str:
                return datetime.strptime(date_str, '%Y-%m-%d').date()
            return None

        roll.date_started = parse_date(request.form.get('date_started'))
        roll.date_finished = parse_date(request.form.get('date_finished'))
        
        # Check if a NEW file was uploaded to replace the old one
        file = request.files['contact_sheet']
        if file and allowed_file(file.filename):
            filename = secure_filename(f"{datetime.now().timestamp()}_{file.filename}")
            save_optimized_image(file, filename)
            
            # (Optional) Delete old image here if you wanted to keep it clean
            roll.contact_sheet = filename
            
        db.session.commit()
        return redirect(url_for('roll_detail', roll_id=roll.id))
    
    # Load Autocomplete for Edit Page too
    existing_films = [r.film_type for r in db.session.query(Roll.film_type).distinct()]
    existing_cameras = [r.camera for r in db.session.query(Roll.camera).distinct()]
    existing_lenses = [r.lens for r in db.session.query(Roll.lens).distinct() if r.lens]

    return render_template('edit_roll.html', roll=roll,
                           films=existing_films, 
                           cameras=existing_cameras, 
                           lenses=existing_lenses)

@app.route('/delete/<int:roll_id>', methods=['POST'])
def delete_roll(roll_id):
    roll = Roll.query.get_or_404(roll_id)
    
    # Try to delete the image file from the folder to save space
    if roll.contact_sheet:
        try:
            os.remove(os.path.join(app.config['UPLOAD_FOLDER'], roll.contact_sheet))
        except:
            pass 
            
    db.session.delete(roll)
    db.session.commit()
    return redirect(url_for('index'))

@app.route('/stats')
def stats():
    total_rolls = Roll.query.count()
    
    # Get top 5 Cameras
    cameras = db.session.query(Roll.camera, func.count(Roll.camera)).group_by(Roll.camera).order_by(func.count(Roll.camera).desc()).limit(5).all()
    
    # Get top 5 Films
    films = db.session.query(Roll.film_type, func.count(Roll.film_type)).group_by(Roll.film_type).order_by(func.count(Roll.film_type).desc()).limit(5).all()
    
    return render_template('stats.html', total_rolls=total_rolls, cameras=cameras, films=films)

@app.route('/preferences')
def preferences():
    return render_template('preferences.html')

@app.route('/backup')
def backup():
    include_images = request.args.get('images') == 'true'
    
    # Create a memory buffer for the zip file
    buffer = BytesIO()
    
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
        # 1. Add Film Database
        db_path = os.path.join(DATA_DIR, 'filmlog.db')
        if os.path.exists(db_path):
            zip_file.write(db_path, arcname='filmlog.db')
            
        # 2. Add Gear Database (if it exists)
        gear_path = os.path.join(DATA_DIR, 'gearlog.db')
        if os.path.exists(gear_path):
            zip_file.write(gear_path, arcname='gearlog.db')
            
        # 3. Add Images (Optional)
        if include_images and os.path.exists(IMAGES_DIR):
            # Walk through the images directory and add every file
            for root, dirs, files in os.walk(IMAGES_DIR):
                for file in files:
                    file_path = os.path.join(root, file)
                    # Add to zip, preserving the "Images/" folder structure
                    zip_file.write(file_path, arcname=os.path.join('Images', file))

    buffer.seek(0)
    
    # Name the file appropriately
    date_str = datetime.now().strftime('%Y-%m-%d')
    filename = f"FilmLog_Full_Backup_{date_str}.zip" if include_images else f"FilmLog_Data_Backup_{date_str}.zip"
    
    return send_file(buffer, as_attachment=True, download_name=filename, mimetype='application/zip')
# NEW ROUTE: Serve images from the custom Data/Images folder

@app.route('/import_backup', methods=['POST'])
def import_backup():
    file = request.files['backup_file']
    if not file or not file.filename.endswith('.zip'):
        return redirect(url_for('preferences'))

    try:
        # 1. DISCONNECT FROM DATABASE (To release Windows file locks)
        db.session.remove()
        db.engine.dispose()
        
        # 2. Open the uploaded Zip
        with zipfile.ZipFile(file) as z:
            # 3. Extract logic
            for member in z.namelist():
                # Block malicious paths (basic security)
                if '..' in member or member.startswith('/') or member.startswith('\\'):
                    continue
                
                target_path = None
                
                # CASE A: Database Files (Extract to DATA_DIR)
                if member in ['filmlog.db', 'gearlog.db']:
                    target_path = os.path.join(DATA_DIR, member)
                
                # CASE B: Images (Extract to IMAGES_DIR)
                # The zip stores them as "Images/filename.jpg"
                elif member.startswith('Images/'):
                    # Strip "Images/" from the path to get just the filename
                    filename = os.path.basename(member)
                    if filename: # Ensure it's not just the folder itself
                        target_path = os.path.join(IMAGES_DIR, filename)
                
                # Perform the extraction if we found a valid target
                if target_path:
                    # Create directory if missing
                    os.makedirs(os.path.dirname(target_path), exist_ok=True)
                    # Write the file
                    with open(target_path, "wb") as f:
                        f.write(z.read(member))
                        
        # 4. Trigger Restart Warning
        # We redirect with 'restart=True' so the user knows to relaunch the app
        # to re-establish the database connection cleanly.
        return redirect(url_for('preferences', restart=True))

    except Exception as e:
        return f"Error importing backup: {str(e)}", 500

@app.route('/images/<filename>')
def serve_image(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)
    
@app.route('/generate_labels', methods=['POST'])
def generate_labels():
    try:
        start_num = int(request.form.get('start_num', 1))
        count = int(request.form.get('label_count', 65)) # Default to 1 sheet
    except ValueError:
        start_num = 1
        count = 65

    # Setup PDF Buffer (In Memory)
    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4
    
    # --- YOUR LAYOUT CONFIGURATION (Avery L7651) ---
    COLUMNS = 5
    ROWS = 13
    LABEL_WIDTH = 38 * mm
    LABEL_HEIGHT = 21 * mm
    MARGIN_X = 10 * mm
    MARGIN_Y = 12 * mm
    GAP_X = 2 * mm
    GAP_Y = 0 * mm
    # -----------------------------------------------

    current_num = start_num
    
    for i in range(count):
        # Calculate Grid Position
        col = i % COLUMNS
        row = (i // COLUMNS) % ROWS
        
        # New page if we fill the sheet
        if i > 0 and i % (COLUMNS * ROWS) == 0:
            c.showPage()

        # Calculate Coordinates
        x = MARGIN_X + (col * (LABEL_WIDTH + GAP_X))
        # ReportLab draws from bottom-up, so we invert the row logic
        y = height - MARGIN_Y - ((row + 1) * (LABEL_HEIGHT + GAP_Y))

        # 1. Draw Text (Roll ID) - Centered
        roll_id_str = f"{current_num:04d}"
        c.setFont("Courier-Bold", 8) # Size 8 to fit small label
        
        # Center text logic
        text_width = c.stringWidth(f"ROLL #{roll_id_str}", "Courier-Bold", 8)
        text_x = x + (LABEL_WIDTH - text_width) / 2
        c.drawString(text_x, y + 14*mm, f"ROLL #{roll_id_str}")

        # 2. Draw Barcode (Code128)
        # Scaled to fit inside the 38mm width
        barcode = code128.Code128(roll_id_str, barHeight=8*mm, barWidth=0.18*mm)
        
        # Center barcode logic
        barcode_width = barcode.width
        barcode_x = x + (LABEL_WIDTH - barcode_width) / 2
        
        # FIX: Draw directly on canvas instead of using renderPDF
        barcode.drawOn(c, barcode_x, y + 4*mm)
        
        current_num += 1

    c.save()
    buffer.seek(0)
    
    return send_file(buffer, as_attachment=True, download_name=f'labels_{start_num}_to_{current_num-1}.pdf', mimetype='application/pdf')

# INIT DB & START SERVER
if __name__ == '__main__':
    with app.app_context():
        db.create_all()
        
        # Load Network Settings from DB (or use defaults)
        # We must query inside the app_context
        try:
            p_setting = AppSetting.query.filter_by(key='server_port').first()
            port = int(p_setting.value) if p_setting else 5000
            
            h_setting = AppSetting.query.filter_by(key='server_host').first()
            host = h_setting.value if h_setting else '0.0.0.0'
        except:
            # Fallback if DB isn't ready or query fails
            port = 5000
            host = '0.0.0.0'

        print(f" * Starting FilmLog on {host}:{port}")
        
        # SAVE RUNNING CONFIG TO APP MEMORY (For comparison)
        app.config['RUNNING_PORT'] = port
        app.config['RUNNING_HOST'] = host
        
    # Start the app with the loaded settings
    app.run(debug=True, host=host, port=port)