from flask import Flask, request, jsonify, render_template, session
from twilio.rest import Client
import sqlite3
import requests
from math import radians, sin, cos, sqrt, atan2
import logging
import os
import json
import random
from datetime import datetime
import pytz

# Cloud deployment note:
# - cv2, numpy, subprocess removed (not needed on cloud server)
# - Camera runs locally on edge device, sends data to this cloud server
# - inference_sdk imported only when needed in /evaluate route

# Timezone setup
IST = pytz.timezone('Asia/Kolkata')


# ==================== CONFIGURATION ====================

logging.basicConfig(level=logging.DEBUG)
app = Flask(__name__)
app.secret_key = 'super_secret_key_123'
app.config['SESSION_PERMANENT'] = False

# Twilio credentials - MUST be set via environment variables on cloud platform
# Set these in Render.com dashboard under Environment Variables
TWILIO_ACCOUNT_SID = os.environ.get('TWILIO_ACCOUNT_SID', '')
TWILIO_AUTH_TOKEN = os.environ.get('TWILIO_AUTH_TOKEN', '')
TWILIO_PHONE_NUMBER = os.environ.get('TWILIO_PHONE_NUMBER', '')

# Upload folder for image evaluation
UPLOAD_FOLDER = "uploads"
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

# Accident captures folder for camera images
CAPTURES_FOLDER = "captures"
if not os.path.exists(CAPTURES_FOLDER):
    os.makedirs(CAPTURES_FOLDER)

# Database path — use absolute path to avoid cwd issues with gunicorn
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'emergency.db')

# ==================== HELPER FUNCTIONS ====================

def shorten_url(long_url):
    """Shorten URL using TinyURL API"""
    try:
        # Use params to handle URL encoding properly
        response = requests.get("https://tinyurl.com/api-create.php", params={"url": long_url})
        if response.status_code == 200:
            return response.text
        return long_url  # Return original if shortening fails
    except Exception as e:
        logging.error(f"Error shortening URL: {e}")
        return long_url  # Return original if error

def calculate_distance(lat1, lon1, lat2, lon2):
    """Haversine distance in kilometers"""
    R = 6371.0
    lat1_rad, lon1_rad = radians(lat1), radians(lon1)
    lat2_rad, lon2_rad = radians(lat2), radians(lon2)
    dlat = lat2_rad - lat1_rad
    dlon = lon2_rad - lon1_rad
    a = sin(dlat/2)**2 + cos(lat1_rad)*cos(lat2_rad)*sin(dlon/2)**2
    c = 2 * atan2(sqrt(a), sqrt(1 - a))
    return R * c

# ==================== DYNAMIC DATA FETCHING ====================

# ==================== STATIC BASELINE HOSPITALS (Pune region) ====================
# These are always available as fallback even if Overpass API fails on cloud
BASELINE_HOSPITALS = [
    ('Kamla Nehru Hospital', '+919356992477', 18.5204, 73.8567, 15, 3),
    ('Dr. Naidu Contagious Disease Hospital', '+919356992477', 18.5195, 73.8555, 12, 2),
    ('Pune District Hospital (Pune Civil Hospital)', '+919356992477', 18.5300, 73.8000, 20, 5),
    ('Sassoon General Hospital', '+919356992477', 18.5250, 73.8500, 25, 6),
    ('Poona Hospital', '+919356992477', 18.5280, 73.8450, 18, 4),
    ('Ruby Hall Clinic', '+919356992477', 18.5249, 73.8478, 30, 8),
    ('Deenanath Mangeshkar Hospital', '+919356992477', 18.5150, 73.8200, 22, 5),
    ('Bharati Hospital', '+919356992477', 18.4500, 73.8700, 15, 3),
    ('Jehangir Hospital', '+919356992477', 18.5267, 73.8489, 28, 7),
    ('Noble Hospital', '+919356992477', 18.5000, 73.9000, 14, 3),
    ('Yashwantrao Chavan Memorial Hospital', '+919356992477', 18.6000, 73.8000, 20, 4),
    ('Dr. Bansal Hospital', '+919356992477', 18.5500, 73.7500, 10, 2),
    ('Sai Snehdeep Hospital', '+919356992477', 18.6100, 73.7800, 8, 1),
    ('Aditya Birla Memorial Hospital', '+919356992477', 18.5600, 73.7900, 35, 10),
    ('Lokmanya Hospital', '+919356992477', 18.6200, 73.8100, 12, 2),
    ('Niramaya Hospital', '+919356992477', 18.6300, 73.8200, 10, 2),
    ('Om Hospital', '+919356992477', 18.6400, 73.8300, 8, 1),
    ('Sainath Hospital', '+919356992477', 18.6500, 73.8400, 10, 2),
    ('Astha Hospital', '+919356992477', 18.6600, 73.8500, 12, 3),
    ('Sushrut Hospital', '+919356992477', 18.6700, 73.8600, 15, 3),
]


def init_database():
    """
    Initialize the database: create all tables if they don't exist,
    and populate with baseline hospital + ambulance data if empty.
    This runs on every startup so the cloud system always has data,
    even on Render's ephemeral filesystem where the DB is recreated.
    This matches exactly what database_setup.py does for the local system.
    """
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    
    # ===== CREATE TABLES (IF NOT EXISTS) =====
    cur.execute('''
    CREATE TABLE IF NOT EXISTS accident_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
        camera_id TEXT,
        latitude REAL,
        longitude REAL,
        ambulance_id TEXT,
        driver_name TEXT,
        driver_phone TEXT,
        hospital_name TEXT,
        hospital_phone TEXT,
        response_time_seconds INTEGER,
        image_path TEXT,
        sms_status TEXT,
        route_link TEXT,
        status TEXT DEFAULT 'pending',
        completed_at DATETIME
    )
    ''')
    
    cur.execute('''
    CREATE TABLE IF NOT EXISTS accidents (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        latitude REAL,
        longitude REAL,
        reported_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    ''')
    
    cur.execute('''
    CREATE TABLE IF NOT EXISTS hospitals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        phone_no TEXT NOT NULL,
        latitude REAL NOT NULL,
        longitude REAL NOT NULL,
        available_beds INTEGER DEFAULT 10,
        icu_beds INTEGER DEFAULT 2
    )
    ''')
    
    cur.execute('''
    CREATE TABLE IF NOT EXISTS ambulances (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ambulance_no TEXT NOT NULL,
        driver_name TEXT NOT NULL,
        phone_no TEXT NOT NULL,
        latitude REAL NOT NULL,
        longitude REAL NOT NULL,
        hospital_id INTEGER,
        status TEXT DEFAULT 'available',
        current_latitude REAL,
        current_longitude REAL,
        FOREIGN KEY (hospital_id) REFERENCES hospitals(id)
    )
    ''')
    
    conn.commit()
    
    # ===== SEED BASELINE DATA IF EMPTY =====
    cur.execute("SELECT COUNT(*) FROM hospitals")
    count = cur.fetchone()[0]
    
    if count == 0:
        logging.info("📋 Database empty — loading baseline hospital data (same as local system)...")
        
        for h in BASELINE_HOSPITALS:
            cur.execute(
                'INSERT INTO hospitals (name, phone_no, latitude, longitude, available_beds, icu_beds) VALUES (?, ?, ?, ?, ?, ?)',
                h
            )
        
        # Generate ambulances for each hospital (two per hospital, same as database_setup.py)
        for i, hospital in enumerate(BASELINE_HOSPITALS, start=1):
            lat, lon = hospital[2], hospital[3]
            hospital_id = i
            
            cur.execute(
                'INSERT INTO ambulances (ambulance_no, driver_name, phone_no, latitude, longitude, hospital_id, status, current_latitude, current_longitude) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (f'AMB{i*2-1:03}', 'Driver A', '+919356992477', lat + 0.001, lon + 0.001, hospital_id, 'available', lat + 0.001, lon + 0.001)
            )
            cur.execute(
                'INSERT INTO ambulances (ambulance_no, driver_name, phone_no, latitude, longitude, hospital_id, status, current_latitude, current_longitude) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (f'AMB{i*2:03}', 'Driver B', '+919356992477', lat - 0.001, lon - 0.001, hospital_id, 'available', lat - 0.001, lon - 0.001)
            )
        
        conn.commit()
        logging.info(f"✅ Database seeded: {len(BASELINE_HOSPITALS)} hospitals + {len(BASELINE_HOSPITALS) * 2} ambulances")
    else:
        logging.info(f"✅ Database already has {count} hospitals — skipping seed.")
    
    conn.close()


def fetch_and_update_local_resources(lat, lon):
    """
    Fetch ALL hospitals, clinics, health facilities, and ambulance services
    from OpenStreetMap (Overpass API) near the given location and MERGE them
    into the local database. Never deletes existing data — only adds new
    facilities. If the API fails, baseline data is always preserved.
    """
    try:
        logging.info(f"🔄 Fetching ALL local resources for {lat}, {lon}...")
        
        # Ensure baseline data exists first
        init_database()
        
        SEARCH_RADIUS = 15000  # 15km radius for comprehensive coverage
        
        overpass_url = "https://overpass-api.de/api/interpreter"
        headers = {'User-Agent': 'EmergencyResponseApp/1.0'}
        
        # ====== QUERY 1: All medical facilities (hospitals, clinics, health centres) ======
        hospital_query = f"""
        [out:json][timeout:30];
        (
          node["amenity"="hospital"](around:{SEARCH_RADIUS}, {lat}, {lon});
          way["amenity"="hospital"](around:{SEARCH_RADIUS}, {lat}, {lon});
          relation["amenity"="hospital"](around:{SEARCH_RADIUS}, {lat}, {lon});
          node["amenity"="clinic"](around:{SEARCH_RADIUS}, {lat}, {lon});
          way["amenity"="clinic"](around:{SEARCH_RADIUS}, {lat}, {lon});
          node["healthcare"="hospital"](around:{SEARCH_RADIUS}, {lat}, {lon});
          way["healthcare"="hospital"](around:{SEARCH_RADIUS}, {lat}, {lon});
          node["healthcare"="clinic"](around:{SEARCH_RADIUS}, {lat}, {lon});
          way["healthcare"="clinic"](around:{SEARCH_RADIUS}, {lat}, {lon});
          node["amenity"="doctors"](around:{SEARCH_RADIUS}, {lat}, {lon});
          way["amenity"="doctors"](around:{SEARCH_RADIUS}, {lat}, {lon});
          node["healthcare"="centre"](around:{SEARCH_RADIUS}, {lat}, {lon});
          way["healthcare"="centre"](around:{SEARCH_RADIUS}, {lat}, {lon});
        );
        out center;
        """
        
        logging.info(f"📡 Querying Overpass API with {SEARCH_RADIUS}m radius...")
        
        hospital_elements = []
        try:
            response = requests.post(overpass_url, data=hospital_query, headers=headers, timeout=30)
            if response.status_code == 200:
                data = response.json()
                hospital_elements = data.get('elements', [])
                logging.info(f"📍 Found {len(hospital_elements)} medical facilities from API")
            else:
                logging.warning(f"Overpass API returned status {response.status_code}, keeping existing data")
        except Exception as api_err:
            logging.warning(f"Overpass API failed: {api_err}, keeping existing data")
        
        # ====== QUERY 2: Ambulance stations ======
        ambulance_stations = []
        try:
            ambulance_query = f"""
            [out:json][timeout:30];
            (
              node["emergency"="ambulance_station"](around:{SEARCH_RADIUS}, {lat}, {lon});
              way["emergency"="ambulance_station"](around:{SEARCH_RADIUS}, {lat}, {lon});
              node["amenity"="ambulance_station"](around:{SEARCH_RADIUS}, {lat}, {lon});
              way["amenity"="ambulance_station"](around:{SEARCH_RADIUS}, {lat}, {lon});
            );
            out center;
            """
            amb_response = requests.post(overpass_url, data=ambulance_query, headers=headers, timeout=30)
            if amb_response.status_code == 200:
                amb_data = amb_response.json()
                ambulance_stations = amb_data.get('elements', [])
                logging.info(f"🚑 Found {len(ambulance_stations)} ambulance stations from API")
        except Exception as amb_err:
            logging.warning(f"Ambulance station query failed: {amb_err}")
        
        if not hospital_elements and not ambulance_stations:
            logging.info("⚠️ No API results, but baseline data is preserved in database.")
            return True  # Return True because baseline data still exists
            
        # ====== MERGE INTO DATABASE (never delete existing!) ======
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        
        # Get existing hospital names to avoid duplicates
        cur.execute("SELECT LOWER(name) FROM hospitals")
        existing_names = set(row[0] for row in cur.fetchall())
        
        # Get next ambulance counter
        cur.execute("SELECT MAX(CAST(REPLACE(REPLACE(ambulance_no, 'AMB', ''), '-', '') AS INTEGER)) FROM ambulances")
        max_amb = cur.fetchone()[0] or 0
        amb_counter = max_amb + 1
        
        new_hospitals = 0
        
        for element in hospital_elements:
            tags = element.get('tags', {})
            
            # Try multiple name fields for better coverage
            name = (tags.get('name') or 
                    tags.get('name:en') or 
                    tags.get('operator') or 
                    tags.get('brand') or
                    tags.get('short_name') or '')
            
            if not name or name.strip() == '':
                continue
                
            name = name.strip()
            
            # Skip if already exists
            if name.lower() in existing_names:
                continue
            existing_names.add(name.lower())
            
            # Get coordinates
            if element['type'] == 'node':
                h_lat, h_lon = element.get('lat'), element.get('lon')
            else:
                center = element.get('center', {})
                h_lat, h_lon = center.get('lat'), center.get('lon')
                
            if h_lat is None or h_lon is None:
                continue
            
            # Get phone from OSM if available, otherwise use default
            phone_no = tags.get('phone') or tags.get('contact:phone') or "+919356992477"
            
            # Determine facility type for bed count estimation
            amenity = tags.get('amenity', '')
            healthcare = tags.get('healthcare', '')
            is_hospital = (amenity == 'hospital' or healthcare == 'hospital')
            
            avail_beds = random.randint(10, 50) if is_hospital else random.randint(3, 15)
            icu_beds = random.randint(2, 10) if is_hospital else random.randint(0, 3)
            
            # Add facility type suffix for clinics
            display_name = name
            if amenity == 'clinic' or healthcare == 'clinic':
                if 'clinic' not in name.lower() and 'hospital' not in name.lower():
                    display_name = f"{name} (Clinic)"
            
            cur.execute(
                'INSERT INTO hospitals (name, phone_no, latitude, longitude, available_beds, icu_beds) VALUES (?, ?, ?, ?, ?, ?)',
                (display_name, phone_no, h_lat, h_lon, avail_beds, icu_beds)
            )
            hospital_id = cur.lastrowid
            new_hospitals += 1
            
            # Generate 2 ambulances per hospital
            for i in range(2):
                amb_no = f"AMB{amb_counter:03d}"
                driver_name = f"Driver {random.choice(['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H'])}"
                
                a_lat = h_lat + random.uniform(-0.003, 0.003)
                a_lon = h_lon + random.uniform(-0.003, 0.003)
                
                cur.execute(
                    'INSERT INTO ambulances (ambulance_no, driver_name, phone_no, latitude, longitude, hospital_id, status, current_latitude, current_longitude) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                    (amb_no, driver_name, phone_no, a_lat, a_lon, hospital_id, 'available', a_lat, a_lon)
                )
                amb_counter += 1
        
        # Add ambulances from actual ambulance stations
        for station in ambulance_stations:
            tags = station.get('tags', {})
            station_name = tags.get('name') or tags.get('operator') or 'Ambulance Station'
            
            if station['type'] == 'node':
                s_lat, s_lon = station.get('lat'), station.get('lon')
            else:
                center = station.get('center', {})
                s_lat, s_lon = center.get('lat'), center.get('lon')
                
            if s_lat is None or s_lon is None:
                continue
            
            phone_no = tags.get('phone') or tags.get('contact:phone') or "+919356992477"
            
            # Generate 3 ambulances per ambulance station
            for i in range(3):
                amb_no = f"AMB{amb_counter:03d}"
                driver_name = f"Driver {random.choice(['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H'])}"
                
                a_lat = s_lat + random.uniform(-0.002, 0.002)
                a_lon = s_lon + random.uniform(-0.002, 0.002)
                
                cur.execute(
                    'INSERT INTO ambulances (ambulance_no, driver_name, phone_no, latitude, longitude, hospital_id, status, current_latitude, current_longitude) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                    (amb_no, driver_name, phone_no, a_lat, a_lon, None, 'available', a_lat, a_lon)
                )
                amb_counter += 1
        
        conn.commit()
        
        # Get total counts for logging
        cur2 = conn.cursor()
        cur2.execute("SELECT COUNT(*) FROM hospitals")
        total_hospitals = cur2.fetchone()[0]
        cur2.execute("SELECT COUNT(*) FROM ambulances")
        total_ambulances = cur2.fetchone()[0]
        conn.close()
        
        logging.info(f"✅ Database updated: {new_hospitals} new facilities added from API. Total: {total_hospitals} hospitals, {total_ambulances} ambulances.")
        return True
        
    except Exception as e:
        logging.error(f"❌ Error updating local resources: {e}")
        import traceback
        traceback.print_exc()
        return False


# ==================== GLOBAL STORAGE FOR AUTO-DETECTION ====================
# Using server-side storage instead of sessions so data is shared across all clients

auto_detection_data = {
    "accident_location": None,
    "detection_mode": "manual",
    "camera_id": None,
    "auto_selected_ambulance": None,
    "auto_selected_hospital": None,
    "accident_image_url": None,
    "ambulance_location": None,
    "hospital_location": None,
    "timestamp": None,
    "route_link": None,
    "current_history_id": None,
    # Cancel countdown tracking
    "countdown_active": False,
    "countdown_start_time": None,
    "alert_cancelled": False,
    # Ambulance tracking
    "ambulance_current_position": None,
    "ambulance_route": [],
    "tracking_active": False
}

# ==================== DEVICE LOCATION (GPS) STORAGE ====================
# This stores the accurate GPS location from browser geolocation
# The camera detection script fetches this instead of using IP geolocation
device_gps_location = {
    "latitude": None,
    "longitude": None,
    "accuracy": None,
    "timestamp": None,
    "source": None  # "browser_gps" or "ip_fallback"
}

first_request = False

@app.before_request
def clear_session_once():
    global first_request
    if not first_request:
        session.clear()
        logging.info("Session cleared at startup.")
        first_request = True

# ==================== ROUTES ====================

@app.route('/')
def index():
    """Website 1 - Accident Reporting"""
    return render_template('index.html')


# ==================== DEVICE LOCATION ROUTES ====================

@app.route('/location_setup')
def location_setup():
    """Page to set up device GPS location using browser geolocation"""
    return render_template('location_setup.html')


@app.route('/set_device_location', methods=['POST'])
def set_device_location():
    """
    Store the device's GPS location from browser geolocation.
    This is called by the location_setup page to store accurate GPS coordinates.
    The camera detection script will fetch this location instead of using IP geolocation.
    """
    global device_gps_location
    try:
        data = request.json
        lat = data.get('latitude')
        lon = data.get('longitude')
        accuracy = data.get('accuracy')
        
        if lat is None or lon is None:
            return jsonify({"error": "Missing latitude or longitude"}), 400
        
        device_gps_location['latitude'] = float(lat)
        device_gps_location['longitude'] = float(lon)
        device_gps_location['accuracy'] = accuracy
        device_gps_location['latitude'] = float(lat)
        device_gps_location['longitude'] = float(lon)
        device_gps_location['accuracy'] = accuracy
        device_gps_location['timestamp'] = datetime.now(IST).isoformat()
        device_gps_location['source'] = 'browser_gps'
        
        logging.info(f"📍 Device GPS location set: {lat}, {lon} (accuracy: {accuracy}m)")
        
        # Trigger dynamic resource update
        # Run in background or directly (direct for simplicity here)
        fetch_and_update_local_resources(float(lat), float(lon))
        
        return jsonify({
            "status": "success",
            "message": "Device location saved and local resources updated!",
            "location": device_gps_location
        })
        
    except Exception as e:
        logging.error(f"Error in /set_device_location: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/get_device_location')
def get_device_location():
    """
    Get the stored device GPS location.
    This is called by the camera detection script to get accurate GPS coordinates.
    Returns the browser GPS location if set, otherwise returns empty location.
    """
    global device_gps_location
    
    if device_gps_location['latitude'] is None:
        return jsonify({
            "status": "not_set",
            "message": "Device location not yet configured. Please open /location_setup in browser.",
            "location": None
        })
    
    return jsonify({
        "status": "success",
        "location": device_gps_location
    })


@app.route('/api/wifi_locate', methods=['POST'])
def wifi_locate():
    """
    WiFi-based geolocation for ESP32 when GPS has no fix.
    Receives BSSID scan data from ESP32 and returns coordinates.
    Tries: Google Geolocation API → Mozilla Location Service → IP-based fallback.
    """
    try:
        data = request.json
        wifi_aps = data.get('wifiAccessPoints', [])
        
        if not wifi_aps:
            logging.warning("[WIFI_LOCATE] No WiFi access points received")
            return jsonify({"error": "No WiFi data"}), 400
        
        logging.info(f"[WIFI_LOCATE] Received {len(wifi_aps)} WiFi access points")
        
        # ===== METHOD 1: Google Geolocation API (if key is configured) =====
        google_api_key = os.environ.get('GOOGLE_GEOLOCATION_API_KEY', '')
        if google_api_key:
            try:
                google_url = f"https://www.googleapis.com/geolocation/v1/geolocate?key={google_api_key}"
                google_payload = {"wifiAccessPoints": wifi_aps}
                resp = requests.post(google_url, json=google_payload, timeout=10)
                if resp.status_code == 200:
                    result = resp.json()
                    loc = result.get("location", {})
                    lat = loc.get("lat")
                    lng = loc.get("lng")
                    accuracy = result.get("accuracy", 0)
                    if lat and lng:
                        logging.info(f"[WIFI_LOCATE] Google API: {lat}, {lng} (accuracy: {accuracy}m)")
                        return jsonify({
                            "status": "success",
                            "latitude": lat,
                            "longitude": lng,
                            "accuracy": accuracy,
                            "source": "google_geolocation"
                        })
                else:
                    logging.warning(f"[WIFI_LOCATE] Google API failed: {resp.status_code}")
            except Exception as e:
                logging.warning(f"[WIFI_LOCATE] Google API error: {e}")
        
        # ===== METHOD 2: Mozilla Location Service (free, no key needed) =====
        try:
            mozilla_url = "https://location.services.mozilla.com/v1/geolocate?key=test"
            mozilla_payload = {
                "wifiAccessPoints": [
                    {
                        "macAddress": ap.get("macAddress", ""),
                        "signalStrength": ap.get("signalStrength", -70),
                        "channel": ap.get("channel", 0)
                    }
                    for ap in wifi_aps
                ]
            }
            resp = requests.post(mozilla_url, json=mozilla_payload, timeout=10)
            if resp.status_code == 200:
                result = resp.json()
                loc = result.get("location", {})
                lat = loc.get("lat")
                lng = loc.get("lng")
                accuracy = result.get("accuracy", 0)
                if lat and lng:
                    logging.info(f"[WIFI_LOCATE] Mozilla API: {lat}, {lng} (accuracy: {accuracy}m)")
                    return jsonify({
                        "status": "success",
                        "latitude": lat,
                        "longitude": lng,
                        "accuracy": accuracy,
                        "source": "mozilla_location"
                    })
            else:
                logging.warning(f"[WIFI_LOCATE] Mozilla API: HTTP {resp.status_code}")
        except Exception as e:
            logging.warning(f"[WIFI_LOCATE] Mozilla API error: {e}")
        
        # ===== METHOD 3: Use device location if already set via browser =====
        if device_gps_location.get('latitude') is not None:
            logging.info(f"[WIFI_LOCATE] Using stored browser GPS as fallback")
            return jsonify({
                "status": "success",
                "latitude": device_gps_location['latitude'],
                "longitude": device_gps_location['longitude'],
                "accuracy": device_gps_location.get('accuracy', 100),
                "source": "browser_gps_fallback"
            })
        
        # ===== METHOD 4: IP-based geolocation as last resort =====
        try:
            ip_resp = requests.get("http://ip-api.com/json/", timeout=5)
            if ip_resp.status_code == 200:
                ip_data = ip_resp.json()
                lat = ip_data.get("lat")
                lon = ip_data.get("lon")
                if lat and lon:
                    logging.info(f"[WIFI_LOCATE] IP fallback: {lat}, {lon}")
                    return jsonify({
                        "status": "success",
                        "latitude": lat,
                        "longitude": lon,
                        "accuracy": 5000,
                        "source": "ip_geolocation"
                    })
        except Exception as e:
            logging.warning(f"[WIFI_LOCATE] IP fallback error: {e}")
        
        logging.error("[WIFI_LOCATE] All geolocation methods failed")
        return jsonify({"error": "Could not determine location"}), 500
        
    except Exception as e:
        logging.error(f"[WIFI_LOCATE] Error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/report_accident', methods=['POST'])
def report_accident():
    """Receive accident coordinates"""
    try:
        data = request.json
        lat, lon = data.get('latitude'), data.get('longitude')
        if not lat or not lon:
            return jsonify({"error": "Invalid location"}), 400
        session['accident_location'] = (lat, lon)
        logging.info(f"Accident reported: {lat}, {lon}")
        
        # PERSIST TO DATABASE for Command Center list
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        current_time = datetime.now(IST).isoformat()
        cur.execute('''
            INSERT INTO accident_history 
            (camera_id, latitude, longitude, status, timestamp)
            VALUES (?, ?, ?, ?, ?)
        ''', ('Manual', lat, lon, 'pending', current_time))
        conn.commit()
        conn.close()
        
        return jsonify({"status": "success", "message": "Accident reported and saved"})
    except Exception as e:
        logging.error(f"/report_accident: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/command_center', methods=['GET', 'POST'])
def command_center():
    """Website 2 - Command Center"""
    if request.method == 'POST':
        data = request.json
        lat, lon = data.get('latitude'), data.get('longitude')
        session['accident_location'] = (lat, lon)
        return jsonify({"status": "success", "message": "Accident sent to Command Center"})
    accident = session.get('accident_location')
    logging.info(f"Command Center session: {accident}")
    return render_template('command_center.html', accident_location=accident)


@app.route('/send_to_control_room', methods=['GET', 'POST'])
def send_to_control_room():
    """Send selected accident, ambulance, hospital data"""
    try:
        data = request.json if request.is_json else {}
        keys = ['accident_lat', 'accident_lon', 'ambulance_lat', 'ambulance_lon', 'hospital_lat', 'hospital_lon']
        if not all(k in data for k in keys):
            return jsonify({"error": "Missing fields"}), 400

        session['accident_location'] = (data['accident_lat'], data['accident_lon'])
        session['ambulance_location'] = (data['ambulance_lat'], data['ambulance_lon'])
        session['hospital_location'] = (data['hospital_lat'], data['hospital_lon'])
        logging.info(f"Sent to control room: {data}")
        return jsonify({"status": "success", "message": "Data stored"})
    except Exception as e:
        logging.error(f"/send_to_control_room: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/refresh_nearby_resources', methods=['POST'])
def refresh_nearby_resources():
    """
    Refresh the database with hospitals and ambulances near a specific
    accident location. Called by the command center whenever a new accident
    location is selected so that results are always accurate and local.
    """
    try:
        data = request.json
        lat = data.get('latitude')
        lon = data.get('longitude')
        
        if lat is None or lon is None:
            return jsonify({"error": "Missing latitude or longitude"}), 400
        
        lat = float(lat)
        lon = float(lon)
        
        logging.info(f"🔄 Refreshing nearby resources for accident at {lat}, {lon}")
        
        success = fetch_and_update_local_resources(lat, lon)
        
        if success:
            # Count what we have now
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM hospitals")
            h_count = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM ambulances")
            a_count = cur.fetchone()[0]
            conn.close()
            
            return jsonify({
                "status": "success",
                "message": f"Found {h_count} hospitals and {a_count} ambulances near accident location",
                "hospitals_count": h_count,
                "ambulances_count": a_count
            })
        else:
            return jsonify({
                "status": "partial",
                "message": "Could not fetch fresh data from API. Using existing database."
            })
    except Exception as e:
        logging.error(f"Error in /refresh_nearby_resources: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/get_ambulances')
def get_ambulances():
    """Get nearby ambulances from DB"""
    try:
        lat = request.args.get('latitude')
        lon = request.args.get('longitude')
        
        if not lat or not lon:
            logging.error("Missing latitude or longitude")
            return jsonify([]), 200
        
        try:
            lat = float(lat)
            lon = float(lon)
        except ValueError:
            logging.error(f"Invalid coordinates: lat={lat}, lon={lon}")
            return jsonify([]), 200
        
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute('SELECT ambulance_no, driver_name, phone_no, latitude, longitude FROM ambulances')
        ambulances = cur.fetchall()
        conn.close()

        ambs = []
        for amb in ambulances:
            try:
                amb_no, driver, phone, a_lat, a_lon = amb
                if a_lat is None or a_lon is None:
                    continue
                dist = round(calculate_distance(lat, lon, a_lat, a_lon), 2)
                ambs.append({
                    "ambulance_no": amb_no, 
                    "driver_name": driver, 
                    "phone_no": phone,
                    "latitude": a_lat, 
                    "longitude": a_lon, 
                    "distance_km": dist
                })
            except Exception as e:
                logging.error(f"Error processing ambulance: {e}")
                continue
                
        ambs.sort(key=lambda x: x["distance_km"])
        return jsonify(ambs)
    except Exception as e:
        logging.error(f"/get_ambulances: {e}")
        return jsonify([]), 200


@app.route('/get_hospitals')
def get_hospitals():
    """Get hospitals from local DB"""
    try:
        # Get and validate parameters
        lat = request.args.get('latitude')
        lon = request.args.get('longitude')
        
        if not lat or not lon:
            logging.error("Missing latitude or longitude")
            return jsonify([]), 200
            
        try:
            lat = float(lat)
            lon = float(lon)
        except ValueError:
            logging.error(f"Invalid coordinates: lat={lat}, lon={lon}")
            return jsonify([]), 200
        
        logging.info(f"Fetching hospitals near: {lat}, {lon}")
        
        # Connect to database
        try:
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()
            
            # Fetch all hospitals
            cur.execute("SELECT name, phone_no, latitude, longitude FROM hospitals")
            rows = cur.fetchall()
            conn.close()
            
            logging.info(f"Found {len(rows)} hospitals in database")
            
        except sqlite3.Error as db_error:
            logging.error(f"Database error: {db_error}")
            return jsonify([]), 200
        
        # Build hospital list with distances
        hospitals = []
        for row in rows:
            try:
                name, phone, h_lat, h_lon = row
                
                # Skip if coordinates are None
                if h_lat is None or h_lon is None:
                    logging.warning(f"Skipping hospital {name} - missing coordinates")
                    continue
                
                # Calculate distance
                dist = round(calculate_distance(lat, lon, h_lat, h_lon), 2)
                
                hospitals.append({
                    "name": name,
                    "phone_no": phone,
                    "latitude": h_lat,
                    "longitude": h_lon,
                    "distance_km": dist
                })
            except Exception as row_error:
                logging.error(f"Error processing hospital row: {row_error}")
                continue
        
        # Sort by distance
        hospitals.sort(key=lambda x: x["distance_km"])
        
        logging.info(f"Returning {len(hospitals)} hospitals")
        return jsonify(hospitals)
        
    except Exception as e:
        logging.error(f"Unexpected error in /get_hospitals: {e}", exc_info=True)
        return jsonify([]), 200


@app.route('/control_room')
def control_room():
    """Website 3 - Control Room with Real-time Tracking"""
    global auto_detection_data
    
    # Try global storage first, then fall back to session
    a = auto_detection_data.get('accident_location') or session.get('accident_location')
    b = auto_detection_data.get('ambulance_location') or session.get('ambulance_location')
    h = auto_detection_data.get('hospital_location') or session.get('hospital_location')
    
    # If still no data, use defaults for demo
    if not a:
        a = (18.5204, 73.8567)
    if not b:
        b = (18.5214, 73.8577)
    if not h:
        h = (18.5250, 73.8500)
    
    return render_template('control_room.html', accident_location=a, ambulance_location=b, hospital_location=h)


@app.route('/calculate_route')
def calculate_route():
    """Get route between two points"""
    try:
        o_lat, o_lon = float(request.args.get('origin_lat')), float(request.args.get('origin_lon'))
        d_lat, d_lon = float(request.args.get('destination_lat')), float(request.args.get('destination_lon'))
        url = f"http://router.project-osrm.org/route/v1/driving/{o_lon},{o_lat};{d_lon},{d_lat}?overview=full&geometries=geojson"
        r = requests.get(url).json()
        if r.get("code") != "Ok":
            return jsonify({"error": "Route failed"}), 400
        route = r["routes"][0]
        return jsonify({
            "distance_km": round(route["distance"] / 1000, 2),
            "duration_minutes": round(route["duration"] / 60, 2),
            "route": route["geometry"]["coordinates"]
        })
    except Exception as e:
        logging.error(f"/calculate_route: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/generate_best_route_link', methods=['GET'])
def generate_best_route_link():
    try:
        ambulance_lat = request.args.get('ambulance_lat')
        ambulance_lon = request.args.get('ambulance_lon')
        accident_lat = request.args.get('accident_lat')
        accident_lon = request.args.get('accident_lon')
        hospital_lat = request.args.get('hospital_lat')
        hospital_lon = request.args.get('hospital_lon')

        if not all([ambulance_lat, ambulance_lon, accident_lat, accident_lon, hospital_lat, hospital_lon]):
            return jsonify({"error": "Missing required location data"}), 400

        google_maps_link = f"https://www.google.com/maps/dir/?api=1" \
                           f"&origin={ambulance_lat},{ambulance_lon}" \
                           f"&waypoints={accident_lat},{accident_lon}" \
                           f"&destination={hospital_lat},{hospital_lon}" \
                           f"&travelmode=driving"
        
        # Shorten URL for SMS
        short_link = shorten_url(google_maps_link)
        logging.info(f"Shortened URL: {short_link}")

        return jsonify({"google_maps_link": short_link})

    except Exception as e:
        logging.error(f"Error in /generate_best_route_link: {str(e)}")
        return jsonify({"error": "Failed to generate best route link"}), 500


@app.route('/get_best_route', methods=['GET'])
def get_best_route():
    try:
        ambulance_lat = request.args.get('ambulance_lat')
        ambulance_lon = request.args.get('ambulance_lon')
        accident_lat = request.args.get('accident_lat')
        accident_lon = request.args.get('accident_lon')
        hospital_lat = request.args.get('hospital_lat')
        hospital_lon = request.args.get('hospital_lon')

        if not all([ambulance_lat, ambulance_lon, accident_lat, accident_lon, hospital_lat, hospital_lon]):
            return jsonify({"error": "Missing required location data"}), 400

        osrm_url_1 = f"http://router.project-osrm.org/route/v1/driving/{ambulance_lon},{ambulance_lat};{accident_lon},{accident_lat}?overview=full&geometries=geojson"
        response_1 = requests.get(osrm_url_1).json()
        
        if response_1.get("code") != "Ok":
            return jsonify({"error": "Route calculation failed"}), 400

        route_1 = response_1["routes"][0]
        distance_1 = round(route_1["distance"] / 1000, 2)
        duration_1 = round(route_1["duration"] / 60, 2)
        route_geometry_1 = route_1["geometry"]["coordinates"]

        osrm_url_2 = f"http://router.project-osrm.org/route/v1/driving/{accident_lon},{accident_lat};{hospital_lon},{hospital_lat}?overview=full&geometries=geojson"
        response_2 = requests.get(osrm_url_2).json()
        
        if response_2.get("code") != "Ok":
            return jsonify({"error": "Route calculation failed"}), 400

        route_2 = response_2["routes"][0]
        distance_2 = round(route_2["distance"] / 1000, 2)
        duration_2 = round(route_2["duration"] / 60, 2)
        route_geometry_2 = route_2["geometry"]["coordinates"]

        google_maps_link = f"https://www.google.com/maps/dir/?api=1" \
                           f"&origin={ambulance_lat},{ambulance_lon}" \
                           f"&waypoints={accident_lat},{accident_lon}" \
                           f"&destination={hospital_lat},{hospital_lon}" \
                           f"&travelmode=driving"

        return jsonify({
            "ambulance_to_accident": {"distance_km": distance_1, "duration_minutes": duration_1, "route": route_geometry_1},
            "accident_to_hospital": {"distance_km": distance_2, "duration_minutes": duration_2, "route": route_geometry_2},
            "total_distance_km": distance_1 + distance_2,
            "total_duration_minutes": duration_1 + duration_2,
            "google_maps_link": google_maps_link
        })

    except Exception as e:
        logging.error(f"Error in /get_best_route: {str(e)}")
        return jsonify({"error": "Internal Server Error"}), 500


@app.route('/send_sms', methods=['POST'])
def send_sms():
    """Send an SMS via Twilio"""
    try:
        data = request.json
        phone, message = data.get('phone_no'), data.get('message')
        
        if not phone or not message:
            return jsonify({"error": "Missing phone number or message"}), 400
        
        logging.info(f"Attempting to send SMS to {phone} from {TWILIO_PHONE_NUMBER}")
        logging.info(f"Using SID: {TWILIO_ACCOUNT_SID[:10]}...")
        
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        msg = client.messages.create(body=message, from_=TWILIO_PHONE_NUMBER, to=phone)
        
        logging.info(f"✅ SMS sent successfully to {phone}! SID: {msg.sid}")
        return jsonify({"status": "sent", "sid": msg.sid})
    except Exception as e:
        error_msg = str(e)
        logging.error(f"❌ /send_sms FULL error: {error_msg}")
        logging.error(f"❌ Error type: {type(e).__name__}")
        # Check for common Twilio errors
        if "unverified" in error_msg.lower():
            logging.error("⚠️ HINT: The destination phone number is not verified. Add it in Twilio Console > Verified Caller IDs")
        elif "geo" in error_msg.lower() or "permission" in error_msg.lower():
            logging.error("⚠️ HINT: Geographic permission issue. Enable India in Twilio Console > Messaging > Geo Permissions")
        return jsonify({"error": error_msg}), 500


@app.route('/send_best_route_sms', methods=['POST'])
def send_best_route_sms():
    try:
        data = request.json
        driver_phone_no = data.get('driver_phone_no')
        officer_phone_nos = data.get('officer_phone_nos', [])
        best_route_link = data.get('best_route_link')

        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

        driver_message = f"🚑 Follow this best route: {best_route_link}"
        msg = client.messages.create(body=driver_message, from_=TWILIO_PHONE_NUMBER, to=driver_phone_no)
        logging.info(f"✅ SMS sent to driver {driver_phone_no}: {msg.sid}")

        for phone_no in officer_phone_nos:
            officer_message = f"🚦 Ambulance is following this route: {best_route_link}"
            msg = client.messages.create(body=officer_message, from_=TWILIO_PHONE_NUMBER, to=phone_no)
            logging.info(f"✅ SMS sent to officer {phone_no}: {msg.sid}")

        return jsonify({"message": "Best route SMS sent successfully!"})

    except Exception as e:
        logging.error(f"❌ Error in /send_best_route_sms: {str(e)}")
        return jsonify({"error": "Failed to send SMS"}), 500


@app.route('/control_traffic_lights', methods=['POST'])
def control_traffic_lights():
    try:
        data = request.json
        logging.info(f"Received /control_traffic_lights data: {data}")

        ambulance_location = data.get('ambulance_location')
        traffic_officers = data.get('traffic_officers')

        if not ambulance_location or not traffic_officers:
            logging.warning("Invalid input: Missing ambulance location or traffic officers.")
            return jsonify({"error": "Invalid input"}), 400

        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

        for officer in traffic_officers:
            phone_no = officer.get('phone_no')
            if phone_no:
                message = f"🚦 Please clear the path for ambulance at {ambulance_location}."
                msg = client.messages.create(body=message, from_=TWILIO_PHONE_NUMBER, to=phone_no)
                logging.info(f"✅ SMS sent to {phone_no}: {msg.sid}")

        return jsonify({"message": "Traffic lights controlled and SMS sent to officers!"})

    except Exception as e:
        logging.error(f"❌ Error in /control_traffic_lights: {str(e)}")
        return jsonify({"error": "Internal Server Error"}), 500


@app.route("/evaluate", methods=["GET", "POST"])
def evaluate():
    if request.method == "POST":
        if "file" not in request.files:
            return "No file uploaded", 400

        file = request.files["file"]
        if file.filename == "":
            return "No file selected", 400

        file_path = os.path.join(app.config['UPLOAD_FOLDER'], file.filename)
        file.save(file_path)

        from inference_sdk import InferenceHTTPClient
        CLIENT = InferenceHTTPClient(
            api_url="https://classify.roboflow.com",
            api_key="S4Fh4K2wwAxvBmWnnIW1"
        )

        result = CLIENT.infer(file_path, model_id="accident-classification-jbmo5/2")
        print("Inference Result:", result)

        is_accident = False
        if "predictions" in result:
            for prediction in result["predictions"]:
                if prediction["class"] == "accident" and prediction["confidence"] > 0.5:
                    is_accident = True
                    break

        return render_template("result.html", is_accident=is_accident)

    return render_template("upload.html")


@app.route("/send-location", methods=["POST"])
def send_location():
    try:
        data = request.json
        latitude = data.get("latitude")
        longitude = data.get("longitude")

        if not latitude or not longitude:
            return jsonify({"error": "Invalid location data"}), 400

        session['accident_location'] = (latitude, longitude)
        logging.info(f"Stored accident location in session: {session['accident_location']}")

        return jsonify({
            "status": "success",
            "message": "Accident location received!",
            "latitude": latitude,
            "longitude": longitude
        })

    except Exception as e:
        logging.error(f"Error in /send-location: {str(e)}")
        return jsonify({"error": "Internal Server Error"}), 500


def filter_traffic_lights(route_coordinates, traffic_lights, threshold_km=0.05):
    filtered_lights = []
    for light in traffic_lights:
        light_lat, light_lon = light["location"]
        min_distance = min(
            calculate_distance(light_lat, light_lon, coord[1], coord[0])
            for coord in route_coordinates
        )
        if min_distance <= threshold_km:
            filtered_lights.append(light)
    return filtered_lights


@app.route('/update_traffic_lights', methods=["GET", 'POST'])
def update_traffic_lights():
    try:
        data = request.json
        ambulance_location = data.get('ambulance_location')
        route_coordinates = data.get('route')

        if not ambulance_location or not route_coordinates:
            return jsonify({"error": "Invalid input"}), 400

        overpass_url = "https://overpass-api.de/api/interpreter"
        overpass_query = f"""
        [out:json];
        node["highway"="traffic_signals"](around:5000,{ambulance_location[0]},{ambulance_location[1]});
        out;
        """
        headers = {'User-Agent': 'EmergencyResponseApp/1.0'}
        response = requests.post(overpass_url, data=overpass_query, headers=headers, timeout=10)
        data = response.json()

        traffic_lights = [
            {"id": element["id"], "location": [element["lat"], element["lon"]], "status": "red"}
            for element in data.get("elements", [])
        ]

        filtered_lights = filter_traffic_lights(route_coordinates, traffic_lights)

        updated_lights = []
        for light in filtered_lights:
            light_lat, light_lon = light["location"]
            distance_km = calculate_distance(ambulance_location[0], ambulance_location[1], light_lat, light_lon)

            if distance_km < 1:
                light["status"] = "green"
            else:
                light["status"] = "red"

            updated_lights.append(light)

        logging.info(f"Updated traffic lights: {updated_lights}")
        return jsonify({"status": "success", "traffic_lights": updated_lights})

    except Exception as e:
        logging.error(f"Error in /update_traffic_lights: {str(e)}")
        return jsonify({"error": "Internal Server Error"}), 500


# ==================== AUTOMATIC TRIGGER FROM CAMERA ====================

@app.route('/trigger_auto_response', methods=['POST'])
def trigger_auto_response():
    """
    Automatic emergency response triggered by camera accident detection.
    This endpoint:
    1. Receives accident location from camera
    2. Auto-selects the nearest ambulance
    3. Auto-selects the nearest hospital
    4. Calculates the best route
    5. Sends SMS to the driver with route link
    6. Notifies officers if configured
    
    Can be overridden by manual selection in command center (manual mode fallback)
    """
    try:
        data = request.json
        logging.info(f"🚨 AUTO TRIGGER received: {data}")
        
        # Extract data
        lat = data.get('latitude')
        lon = data.get('longitude')
        camera_id = data.get('camera_id', 'Unknown')
        mode = data.get('mode', 'automatic')
        timestamp = data.get('timestamp', '')
        image_url = data.get('image_url')  # Captured accident image
        
        if not lat or not lon:
            return jsonify({"error": "Missing location data"}), 400
        
        # Cast to float for distance calculations
        lat = float(lat)
        lon = float(lon)
        
        # Store in GLOBAL storage (not session) so all clients can see it
        global auto_detection_data
        auto_detection_data['accident_location'] = (lat, lon)
        auto_detection_data['detection_mode'] = mode
        auto_detection_data['camera_id'] = camera_id
        auto_detection_data['accident_image_url'] = image_url
        auto_detection_data['timestamp'] = timestamp
        
        logging.info(f"✅ Accident location stored globally: ({lat}, {lon}) from camera {camera_id}")
        if image_url:
            logging.info(f"📸 Accident image: {image_url}")
        
        # ========== STEP 0: Refresh nearby resources for accident location ==========
        logging.info(f"🔄 Refreshing nearby resources for accident at {lat}, {lon}...")
        fetch_and_update_local_resources(lat, lon)
        
        # ========== STEP 1: Find nearest ambulance ==========
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        
        # Get all ambulances
        cur.execute('SELECT ambulance_no, driver_name, phone_no, latitude, longitude FROM ambulances')
        ambulances = cur.fetchall()
        
        if not ambulances:
            conn.close()
            return jsonify({"error": "No ambulances available in database"}), 500
        
        # Calculate distances and find nearest
        nearest_ambulance = None
        min_distance = float('inf')
        
        for amb in ambulances:
            amb_no, driver, phone, a_lat, a_lon = amb
            if a_lat is None or a_lon is None:
                continue
            dist = calculate_distance(lat, lon, a_lat, a_lon)
            if dist < min_distance:
                min_distance = dist
                nearest_ambulance = {
                    "ambulance_no": amb_no,
                    "driver_name": driver,
                    "phone_no": phone,
                    "latitude": a_lat,
                    "longitude": a_lon,
                    "distance_km": round(dist, 2)
                }
        
        if not nearest_ambulance:
            conn.close()
            return jsonify({"error": "No valid ambulance found"}), 500
        
        logging.info(f"✅ Nearest ambulance: {nearest_ambulance['ambulance_no']} at {nearest_ambulance['distance_km']}km")
        
        # ========== STEP 2: Find nearest hospital ==========
        cur.execute("SELECT name, phone_no, latitude, longitude FROM hospitals")
        hospitals = cur.fetchall()
        conn.close()
        
        if not hospitals:
            return jsonify({"error": "No hospitals available in database"}), 500
        
        nearest_hospital = None
        min_hospital_distance = float('inf')
        
        for hosp in hospitals:
            name, phone, h_lat, h_lon = hosp
            if h_lat is None or h_lon is None:
                continue
            dist = calculate_distance(lat, lon, h_lat, h_lon)
            if dist < min_hospital_distance:
                min_hospital_distance = dist
                nearest_hospital = {
                    "name": name,
                    "phone_no": phone,
                    "latitude": h_lat,
                    "longitude": h_lon,
                    "distance_km": round(dist, 2)
                }
        
        if not nearest_hospital:
            return jsonify({"error": "No valid hospital found"}), 500
        
        logging.info(f"✅ Nearest hospital: {nearest_hospital['name']} at {nearest_hospital['distance_km']}km")
        
        # Store in GLOBAL storage for control room and command center
        auto_detection_data['ambulance_location'] = (nearest_ambulance['latitude'], nearest_ambulance['longitude'])
        auto_detection_data['hospital_location'] = (nearest_hospital['latitude'], nearest_hospital['longitude'])
        auto_detection_data['auto_selected_ambulance'] = nearest_ambulance
        auto_detection_data['auto_selected_hospital'] = nearest_hospital
        
        # ========== STEP 3: Generate route link ==========
        google_maps_link = f"https://www.google.com/maps/dir/?api=1" \
                           f"&origin={nearest_ambulance['latitude']},{nearest_ambulance['longitude']}" \
                           f"&waypoints={lat},{lon}" \
                           f"&destination={nearest_hospital['latitude']},{nearest_hospital['longitude']}" \
                           f"&travelmode=driving"
        
        # Shorten URL
        short_link = shorten_url(google_maps_link)
        logging.info(f"✅ Route link generated: {short_link}")
        
        # ========== STEP 4: Send SMS to driver ==========
        sms_status = "not_sent"
        sms_sid = None
        
        try:
            driver_phone = nearest_ambulance['phone_no']
            logging.info(f"📨 Preparing to send SMS to driver: {driver_phone} using Sender: {TWILIO_PHONE_NUMBER}")
            
            if driver_phone:
                client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
                
                # Add timestamp to make the SMS unique and bypass telecom spam filters for duplicate messages
                current_time = datetime.now().strftime("%I:%M:%S %p")
                driver_message = f"🚑 EMERGENCY [{current_time}]! Follow route: {short_link}"
                
                logging.info(f"📨 Sending Message Content: {driver_message}")
                
                msg = client.messages.create(
                    body=driver_message,
                    from_=TWILIO_PHONE_NUMBER,
                    to=driver_phone
                )
                sms_status = "sent"
                sms_sid = msg.sid
                logging.info(f"✅ SMS sent successfully to driver {driver_phone}! SID: {msg.sid}")
            else:
                sms_status = "no_phone"
                logging.warning("⚠️ Driver has no phone number configured in database!")
        except Exception as sms_error:
            sms_status = f"failed: {str(sms_error)}"
            logging.error(f"❌ Failed to send SMS: {sms_error}", exc_info=True)
            # Print to console for immediate visibility
            print(f"CRITICAL TWILIO ERROR: {sms_error}")
        
        # ========== STEP 5: Log to Accident History ==========
        history_id = None
        try:
            history_conn = sqlite3.connect(DB_PATH)
            history_cur = history_conn.cursor()
            history_cur.execute('''
                INSERT INTO accident_history 
                (timestamp, camera_id, latitude, longitude, ambulance_id, driver_name, driver_phone, 
                 hospital_name, hospital_phone, image_path, sms_status, route_link, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S'),
                camera_id, lat, lon,
                nearest_ambulance['ambulance_no'],
                nearest_ambulance['driver_name'],
                nearest_ambulance['phone_no'],
                nearest_hospital['name'],
                nearest_hospital['phone_no'],
                image_url,
                sms_status,
                short_link,
                'dispatched'
            ))
            history_id = history_cur.lastrowid
            history_conn.commit()
            history_conn.close()
            logging.info(f"📊 Accident logged to history with ID: {history_id}")
            
            # Store history ID in global data
            auto_detection_data['current_history_id'] = history_id
            auto_detection_data['route_link'] = short_link
            
        except Exception as history_error:
            logging.error(f"❌ Error logging to history: {history_error}")
        
        # ========== STEP 6: Update ambulance status to busy ==========
        try:
            amb_conn = sqlite3.connect(DB_PATH)
            amb_cur = amb_conn.cursor()
            amb_cur.execute('''
                UPDATE ambulances SET status = 'busy' 
                WHERE ambulance_no = ?
            ''', (nearest_ambulance['ambulance_no'],))
            amb_conn.commit()
            amb_conn.close()
            logging.info(f"🚑 Ambulance {nearest_ambulance['ambulance_no']} status set to busy")
        except Exception as amb_error:
            logging.error(f"❌ Error updating ambulance status: {amb_error}")
        
        # ========== RETURN SUCCESS RESPONSE ==========
        response_data = {
            "status": "success",
            "mode": mode,
            "camera_id": camera_id,
            "timestamp": timestamp,
            "accident_location": {"latitude": lat, "longitude": lon},
            "ambulance": {
                "ambulance_no": nearest_ambulance['ambulance_no'],
                "driver_name": nearest_ambulance['driver_name'],
                "phone_no": nearest_ambulance['phone_no'],
                "distance_km": nearest_ambulance['distance_km']
            },
            "hospital": {
                "name": nearest_hospital['name'],
                "distance_km": nearest_hospital['distance_km']
            },
            "route_link": short_link,
            "sms_status": sms_status,
            "sms_sid": sms_sid,
            "history_id": history_id,
            "message": "Emergency response triggered automatically!"
        }
        
        logging.info(f"🚨 AUTO RESPONSE COMPLETE: Ambulance {nearest_ambulance['ambulance_no']} dispatched to accident, heading to {nearest_hospital['name']}")
        
        return jsonify(response_data)
        
    except Exception as e:
        logging.error(f"❌ Error in /trigger_auto_response: {str(e)}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route('/get_auto_detection_status')
def get_auto_detection_status():
    """Get the status of automatic detection for command center dashboard"""
    try:
        # Return GLOBAL auto_detection_data (shared across all clients)
        return jsonify(auto_detection_data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/esp_alerts')
def get_esp_alerts():
    """Get accident alerts from ESP8266/IoT hardware devices"""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute('''
            SELECT * FROM accident_history 
            WHERE camera_id LIKE 'ESP%' 
            ORDER BY timestamp DESC 
            LIMIT 50
        ''')
        rows = cur.fetchall()
        conn.close()
        
        alerts = [dict(row) for row in rows]
        return jsonify(alerts)
    except Exception as e:
        logging.error(f"Error fetching ESP alerts: {e}")
        return jsonify([])


@app.route('/api/esp_device_status')
def get_esp_device_status():
    """Get current ESP8266 device status and last detection data"""
    global auto_detection_data
    try:
        # Get counts from database
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        
        cur.execute("SELECT COUNT(*) FROM accident_history WHERE camera_id LIKE 'ESP%'")
        total_esp_alerts = cur.fetchone()[0]
        
        cur.execute("SELECT COUNT(*) FROM accident_history WHERE camera_id LIKE 'ESP%' AND status = 'dispatched'")
        active_esp_alerts = cur.fetchone()[0]
        
        cur.execute("SELECT COUNT(*) FROM accident_history WHERE camera_id LIKE 'ESP%' AND status = 'completed'")
        resolved_esp_alerts = cur.fetchone()[0]
        
        # Get latest ESP alert
        cur.execute('''
            SELECT * FROM accident_history 
            WHERE camera_id LIKE 'ESP%' 
            ORDER BY timestamp DESC LIMIT 1
        ''')
        row = cur.fetchone()
        conn.close()
        
        latest = None
        if row:
            cols = ['id', 'camera_id', 'latitude', 'longitude', 'ambulance_id', 'driver_name', 
                    'driver_phone', 'hospital_name', 'hospital_phone', 'image_path', 
                    'sms_status', 'route_link', 'status', 'timestamp', 'completed_at']
            latest = dict(zip(cols, row))
        
        # Check if current auto_detection is from ESP
        is_esp_active = auto_detection_data.get('camera_id', '').startswith('ESP') if auto_detection_data.get('camera_id') else False
        
        return jsonify({
            "total_alerts": total_esp_alerts,
            "active_alerts": active_esp_alerts,
            "resolved_alerts": resolved_esp_alerts,
            "latest_alert": latest,
            "is_esp_active": is_esp_active,
            "current_detection": auto_detection_data if is_esp_active else None
        })
    except Exception as e:
        logging.error(f"Error fetching ESP device status: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/upload_accident_image', methods=['POST'])
def upload_accident_image():
    """
    Receive and store accident image captured by camera system.
    Returns the URL to access the image.
    """
    try:
        if 'image' not in request.files:
            return jsonify({"error": "No image file provided"}), 400
        
        file = request.files['image']
        if file.filename == '':
            return jsonify({"error": "No file selected"}), 400
        
        # Save the image
        filename = file.filename
        filepath = os.path.join(CAPTURES_FOLDER, filename)
        file.save(filepath)
        
        # Generate URL to access the image
        image_url = f"/captures/{filename}"
        
        logging.info(f"📸 Accident image saved: {filepath}")
        
        return jsonify({
            "status": "success",
            "image_url": image_url,
            "filename": filename
        })
        
    except Exception as e:
        logging.error(f"❌ Error uploading image: {str(e)}")
        return jsonify({"error": str(e)}), 500


@app.route('/captures/<filename>')
def serve_capture(filename):
    """Serve captured accident images"""
    from flask import send_from_directory
    return send_from_directory(CAPTURES_FOLDER, filename)


@app.route('/manual_override', methods=['POST'])
def manual_override():
    """
    Allow manual override of auto-selected ambulance/hospital.
    This is the fallback mode when automatic selection needs to be changed.
    """
    try:
        data = request.json
        
        # Override with manual selection
        if 'ambulance' in data:
            session['ambulance_location'] = (data['ambulance']['latitude'], data['ambulance']['longitude'])
            session['auto_selected_ambulance'] = data['ambulance']
            logging.info(f"✅ Ambulance manually overridden: {data['ambulance']}")
        
        if 'hospital' in data:
            session['hospital_location'] = (data['hospital']['latitude'], data['hospital']['longitude'])
            session['auto_selected_hospital'] = data['hospital']
            logging.info(f"✅ Hospital manually overridden: {data['hospital']}")
        
        session['detection_mode'] = 'manual_override'
        
        return jsonify({"status": "success", "message": "Manual override applied"})
        
    except Exception as e:
        logging.error(f"❌ Error in /manual_override: {str(e)}")
        return jsonify({"error": str(e)}), 500


# ==================== ACCIDENT HISTORY ====================

@app.route('/accident_history')
def accident_history_page():
    """Accident History Dashboard"""
    return render_template('accident_history.html')


@app.route('/api/accident_history')
def get_accident_history():
    """Get all accident history records"""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute('''
            SELECT * FROM accident_history 
            ORDER BY timestamp DESC 
            LIMIT 100
        ''')
        rows = cur.fetchall()
        conn.close()
        
        history = [dict(row) for row in rows]
        return jsonify(history)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/update_accident_status', methods=['POST'])
def update_accident_status():
    """Update accident status (pending/dispatched/completed/cancelled)"""
    try:
        data = request.json
        history_id = data.get('id')
        new_status = data.get('status')
        
        logging.info(f"📝 Updating accident {history_id} to status: {new_status}")
        
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        
        if new_status == 'completed':
            cur.execute('''
                UPDATE accident_history 
                SET status = ?, completed_at = CURRENT_TIMESTAMP
                WHERE id = ?
            ''', (new_status, history_id))
            
            # Also reset ambulance status to available
            cur.execute('''
                UPDATE ambulances SET status = 'available'
                WHERE ambulance_no = (
                    SELECT ambulance_id FROM accident_history WHERE id = ?
                )
            ''', (history_id,))
            logging.info(f"✅ Accident {history_id} marked as completed, ambulance released")
            
        elif new_status == 'cancelled':
            cur.execute('''
                UPDATE accident_history SET status = ? WHERE id = ?
            ''', (new_status, history_id))
            
            # Also reset ambulance status to available when cancelled
            cur.execute('''
                UPDATE ambulances SET status = 'available'
                WHERE ambulance_no = (
                    SELECT ambulance_id FROM accident_history WHERE id = ?
                )
            ''', (history_id,))
            logging.info(f"🚫 Accident {history_id} cancelled, ambulance released")
        else:
            cur.execute('''
                UPDATE accident_history SET status = ? WHERE id = ?
            ''', (new_status, history_id))
        
        conn.commit()
        conn.close()
        
        return jsonify({"status": "success", "message": f"Status updated to {new_status}"})
    except Exception as e:
        logging.error(f"❌ Error updating status: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/api/delete_accident', methods=['POST'])
def delete_accident():
    """Delete an accident record from history"""
    try:
        data = request.json
        history_id = data.get('id')
        
        logging.info(f"🗑️ Deleting accident history ID: {history_id}")
        
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        
        # Check if accident exists
        cur.execute("SELECT * FROM accident_history WHERE id = ?", (history_id,))
        record = cur.fetchone()
        
        if not record:
            return jsonify({"error": "Record not found"}), 404
            
        # Delete from DB
        cur.execute("DELETE FROM accident_history WHERE id = ?", (history_id,))
        conn.commit()
        conn.close()
        
        logging.info(f"✅ Deleted accident history ID: {history_id}")
        return jsonify({"status": "success", "message": "Record deleted"})
        
    except Exception as e:
        logging.error(f"❌ Error deleting accident: {e}")
        return jsonify({"error": str(e)}), 500


# ==================== CANCEL ALERT ====================

@app.route('/api/cancel_alert', methods=['POST'])
def cancel_alert():
    """Cancel the current alert within countdown period"""
    global auto_detection_data
    try:
        # Mark as cancelled
        auto_detection_data['alert_cancelled'] = True
        auto_detection_data['countdown_active'] = False
        auto_detection_data['detection_mode'] = 'cancelled'
        
        # Update database status if history exists
        if auto_detection_data.get('current_history_id'):
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()
            cur.execute('''
                UPDATE accident_history SET status = 'cancelled' WHERE id = ?
            ''', (auto_detection_data['current_history_id'],))
            
            # Reset ambulance status
            if auto_detection_data.get('auto_selected_ambulance'):
                cur.execute('''
                    UPDATE ambulances SET status = 'available'
                    WHERE ambulance_no = ?
                ''', (auto_detection_data['auto_selected_ambulance']['ambulance_no'],))
            
            conn.commit()
            conn.close()
        
        logging.info("🚫 Alert cancelled by operator")
        return jsonify({"status": "success", "message": "Alert cancelled successfully"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/start_countdown', methods=['POST'])
def start_countdown():
    """Start 30-second countdown before dispatching"""
    global auto_detection_data
    from datetime import datetime
    
    auto_detection_data['countdown_active'] = True
    auto_detection_data['countdown_start_time'] = datetime.now().isoformat()
    auto_detection_data['alert_cancelled'] = False
    
    return jsonify({"status": "success", "countdown_started": True})


# ==================== AMBULANCE TRACKING ====================

@app.route('/api/start_tracking', methods=['POST'])
def start_tracking():
    """Start simulated ambulance tracking"""
    global auto_detection_data
    
    auto_detection_data['tracking_active'] = True
    
    # Get the route from OSRM if we have the locations
    if (auto_detection_data.get('ambulance_location') and 
        auto_detection_data.get('accident_location') and 
        auto_detection_data.get('hospital_location')):
        
        amb_loc = auto_detection_data['ambulance_location']
        acc_loc = auto_detection_data['accident_location']
        hosp_loc = auto_detection_data['hospital_location']
        
        try:
            # Get route from OSRM
            osrm_url = f"http://router.project-osrm.org/route/v1/driving/{amb_loc[1]},{amb_loc[0]};{acc_loc[1]},{acc_loc[0]};{hosp_loc[1]},{hosp_loc[0]}?overview=full&geometries=geojson"
            response = requests.get(osrm_url, timeout=10)
            
            if response.status_code == 200:
                route_data = response.json()
                if route_data.get('routes'):
                    coords = route_data['routes'][0]['geometry']['coordinates']
                    # Convert to [lat, lon] format
                    auto_detection_data['ambulance_route'] = [[c[1], c[0]] for c in coords]
                    auto_detection_data['ambulance_current_position'] = auto_detection_data['ambulance_route'][0]
                    logging.info(f"📍 Route loaded with {len(coords)} points")
        except Exception as e:
            logging.error(f"❌ Error loading route: {e}")
    
    return jsonify({"status": "success", "tracking_started": True})


@app.route('/api/get_ambulance_position')
def get_ambulance_position():
    """Get current ambulance position for tracking"""
    global auto_detection_data
    import random
    
    if not auto_detection_data.get('tracking_active'):
        return jsonify({"tracking_active": False})
    
    route = auto_detection_data.get('ambulance_route', [])
    
    if route:
        # Simulate movement by advancing position
        current_idx = getattr(get_ambulance_position, 'position_index', 0)
        if current_idx < len(route) - 1:
            get_ambulance_position.position_index = current_idx + max(1, len(route) // 50)  # Move faster for demo
        else:
            auto_detection_data['tracking_active'] = False
            return jsonify({"tracking_active": False, "arrived": True})
        
        position = route[min(current_idx, len(route) - 1)]
        auto_detection_data['ambulance_current_position'] = position
        
        # Calculate progress percentage
        progress = min(100, int((current_idx / len(route)) * 100))
        
        return jsonify({
            "tracking_active": True,
            "position": position,
            "progress": progress,
            "route": route[:current_idx + 1]  # Path traveled
        })
    
    return jsonify({"tracking_active": False})


@app.route('/api/reset_tracking', methods=['POST'])
def reset_tracking():
    """Reset ambulance tracking"""
    global auto_detection_data
    get_ambulance_position.position_index = 0
    auto_detection_data['tracking_active'] = False
    auto_detection_data['ambulance_route'] = []
    auto_detection_data['ambulance_current_position'] = None
    return jsonify({"status": "success"})


# ==================== TRAFFIC LIGHTS ====================

@app.route('/api/get_traffic_lights')
def get_traffic_lights():
    """Get traffic light status along the route"""
    global auto_detection_data
    
    route = auto_detection_data.get('ambulance_route', [])
    current_pos = auto_detection_data.get('ambulance_current_position')
    
    if not route or not current_pos:
        return jsonify({"lights": []})
    
    # Simulate traffic lights at intervals along route
    lights = []
    for i in range(0, len(route), max(1, len(route) // 5)):  # 5 traffic lights
        light_pos = route[i]
        # Check if ambulance has passed this light
        passed = False
        if current_pos:
            current_idx = 0
            for j, pos in enumerate(route):
                if pos == current_pos:
                    current_idx = j
                    break
            passed = i < current_idx
        
        lights.append({
            "position": light_pos,
            "index": i,
            "status": "green" if passed else "red",
            "passed": passed
        })
    
    return jsonify({"lights": lights})


# ==================== PDF REPORT ====================

@app.route('/api/generate_report')
def generate_report():
    """Generate PDF report of accidents"""
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import letter, A4
        from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
        from reportlab.lib.styles import getSampleStyleSheet
        from io import BytesIO
        from flask import send_file
        from datetime import datetime
        
        # Get accident history
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute('SELECT * FROM accident_history ORDER BY timestamp DESC LIMIT 50')
        rows = cur.fetchall()
        conn.close()
        
        # Create PDF
        buffer = BytesIO()
        doc = SimpleDocTemplate(buffer, pagesize=A4)
        elements = []
        styles = getSampleStyleSheet()
        
        # Title
        elements.append(Paragraph("🚨 Emergency Response System - Accident Report", styles['Title']))
        elements.append(Spacer(1, 20))
        elements.append(Paragraph(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", styles['Normal']))
        elements.append(Spacer(1, 20))
        
        # Summary stats
        total = len(rows)
        completed = sum(1 for r in rows if r['status'] == 'completed')
        cancelled = sum(1 for r in rows if r['status'] == 'cancelled')
        
        elements.append(Paragraph(f"Total Accidents: {total}", styles['Normal']))
        elements.append(Paragraph(f"Completed: {completed}", styles['Normal']))
        elements.append(Paragraph(f"Cancelled: {cancelled}", styles['Normal']))
        elements.append(Spacer(1, 30))
        
        # Table data
        data = [['ID', 'Date/Time', 'Camera', 'Ambulance', 'Hospital', 'Status']]
        for row in rows:
            data.append([
                str(row['id']),
                row['timestamp'][:19] if row['timestamp'] else 'N/A',
                row['camera_id'] or 'N/A',
                row['ambulance_id'] or 'N/A',
                (row['hospital_name'] or 'N/A')[:20],
                row['status'] or 'N/A'
            ])
        
        # Create table
        table = Table(data, colWidths=[30, 100, 60, 60, 120, 60])
        table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.darkblue),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, 0), 10),
            ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
            ('BACKGROUND', (0, 1), (-1, -1), colors.beige),
            ('GRID', (0, 0), (-1, -1), 1, colors.black),
            ('FONTSIZE', (0, 1), (-1, -1), 8),
        ]))
        elements.append(table)
        
        doc.build(elements)
        buffer.seek(0)
        
        return send_file(
            buffer,
            mimetype='application/pdf',
            as_attachment=True,
            download_name=f'accident_report_{datetime.now().strftime("%Y%m%d_%H%M%S")}.pdf'
        )
        
    except ImportError:
        return jsonify({"error": "ReportLab not installed. Run: pip install reportlab"}), 500
    except Exception as e:
        logging.error(f"❌ Error generating report: {e}")
        return jsonify({"error": str(e)}), 500


# ==================== ANALYTICS ====================

@app.route('/analytics')
def analytics_page():
    """Analytics Dashboard"""
    return render_template('analytics.html')


@app.route('/api/analytics_data')
def get_analytics_data():
    """Get analytics data for charts"""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        
        # Get accident counts by hour
        cur.execute('''
            SELECT strftime('%H', timestamp) as hour, COUNT(*) as count
            FROM accident_history
            GROUP BY hour
            ORDER BY hour
        ''')
        hourly = [{"hour": r['hour'], "count": r['count']} for r in cur.fetchall()]
        
        # Get accident counts by camera
        cur.execute('''
            SELECT camera_id, COUNT(*) as count
            FROM accident_history
            GROUP BY camera_id
            ORDER BY count DESC
        ''')
        by_camera = [{"camera": r['camera_id'], "count": r['count']} for r in cur.fetchall()]
        
        # Get status distribution
        cur.execute('''
            SELECT status, COUNT(*) as count
            FROM accident_history
            GROUP BY status
        ''')
        by_status = [{"status": r['status'], "count": r['count']} for r in cur.fetchall()]
        
        # Get average response time (mock for now)
        cur.execute('SELECT AVG(response_time_seconds) as avg_time FROM accident_history WHERE response_time_seconds IS NOT NULL')
        avg_response = cur.fetchone()
        
        # Get heatmap data (all accident locations)
        cur.execute('SELECT latitude, longitude FROM accident_history')
        heatmap = [[r['latitude'], r['longitude']] for r in cur.fetchall()]
        
        conn.close()
        
        return jsonify({
            "hourly": hourly,
            "by_camera": by_camera,
            "by_status": by_status,
            "avg_response_time": avg_response['avg_time'] if avg_response else None,
            "heatmap": heatmap,
            "total_accidents": sum(item['count'] for item in by_status) if by_status else 0
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500



# ==================== SETTINGS API ====================

@app.route('/api/settings', methods=['GET', 'POST'])
def manage_settings():
    """Read or update configuration settings"""
    config_path = 'config.json'
    
    if request.method == 'GET':
        try:
            with open(config_path, 'r') as f:
                config = json.load(f)
            return jsonify(config)
        except Exception as e:
            return jsonify({"error": str(e)}), 500
            
    elif request.method == 'POST':
        try:
            new_settings = request.json
            
            # Read existing to preserve structure
            with open(config_path, 'r') as f:
                current_config = json.load(f)
            
            # Update camera settings
            if 'camera_settings' in new_settings:
                if 'camera_settings' not in current_config:
                    current_config['camera_settings'] = {}
                current_config['camera_settings'].update(new_settings['camera_settings'])
            
            # Update ESP/gyro settings
            if 'esp_settings' in new_settings:
                if 'esp_settings' not in current_config:
                    current_config['esp_settings'] = {}
                current_config['esp_settings'].update(new_settings['esp_settings'])
            
            # Save back
            with open(config_path, 'w') as f:
                json.dump(current_config, f, indent=4)
            
            logging.info(f"⚙️ Settings updated: {new_settings}")
                
            return jsonify({"status": "success", "message": "Settings updated", "config": current_config})
            
        except Exception as e:
            return jsonify({"error": str(e)}), 500


# ==================== SYSTEM START/STOP (CLOUD VERSION) ====================
# Camera runs locally on edge devices, not on this cloud server.
# These routes are kept for API compatibility but return cloud-appropriate responses.

@app.route('/api/start_system', methods=['POST'])
def start_system():
    """Cloud note: Camera runs locally on your laptop, not on cloud server"""
    return jsonify({
        "status": "cloud_mode",
        "message": "Camera runs locally on your device. Start test_camera_edge.py on your laptop."
    })


@app.route('/api/stop_system', methods=['POST'])
def stop_system():
    """Cloud note: Camera runs locally on your laptop"""
    return jsonify({
        "status": "cloud_mode",
        "message": "Camera runs locally. Stop test_camera_edge.py on your laptop."
    })


@app.route('/api/system_status')
def system_status():
    """Get status of edge devices (camera and ESP)"""
    global auto_detection_data

    # ESP status — check latest ESP alert timestamp
    esp_online = False
    esp_last_seen = None
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("SELECT timestamp FROM accident_history WHERE camera_id LIKE 'ESP%' ORDER BY timestamp DESC LIMIT 1")
        row = cur.fetchone()
        conn.close()
        if row and row[0]:
            esp_last_seen = row[0]
            try:
                last_time = datetime.fromisoformat(str(row[0]))
                esp_online = (datetime.now() - last_time).total_seconds() < 600
            except:
                esp_online = True
    except:
        pass

    if auto_detection_data.get('camera_id', '').startswith('ESP'):
        esp_online = True

    # Camera status: check if we received a camera trigger recently
    cam_online = False
    if auto_detection_data.get('camera_id') and not auto_detection_data.get('camera_id', '').startswith('ESP'):
        cam_online = True

    return jsonify({
        "camera": {
            "running": cam_online,
            "pid": None,
            "mode": "edge_client"
        },
        "esp": {
            "online": esp_online,
            "last_seen": esp_last_seen
        },
        "system_active": cam_online or esp_online,
        "deployment": "cloud"
    })


# ==================== DATABASE AUTO-SEED ====================

def seed_database():
    """Create tables and populate with initial hospital/ambulance data"""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    
    # Create tables
    cur.execute('''CREATE TABLE IF NOT EXISTS accident_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
        camera_id TEXT, latitude REAL, longitude REAL, ambulance_id TEXT, driver_name TEXT,
        driver_phone TEXT, hospital_name TEXT, hospital_phone TEXT, response_time_seconds INTEGER,
        image_path TEXT, sms_status TEXT, route_link TEXT, status TEXT DEFAULT 'pending', completed_at DATETIME)''')
    cur.execute('''CREATE TABLE IF NOT EXISTS accidents (
        id INTEGER PRIMARY KEY AUTOINCREMENT, latitude REAL, longitude REAL, reported_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    cur.execute('''CREATE TABLE IF NOT EXISTS hospitals (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, phone_no TEXT NOT NULL,
        latitude REAL NOT NULL, longitude REAL NOT NULL, available_beds INTEGER DEFAULT 10, icu_beds INTEGER DEFAULT 2)''')
    cur.execute('''CREATE TABLE IF NOT EXISTS ambulances (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ambulance_no TEXT NOT NULL, driver_name TEXT NOT NULL,
        phone_no TEXT NOT NULL, latitude REAL NOT NULL, longitude REAL NOT NULL,
        hospital_id INTEGER, status TEXT DEFAULT 'available', current_latitude REAL, current_longitude REAL,
        FOREIGN KEY (hospital_id) REFERENCES hospitals(id))''')
    conn.commit()
    
    # Check if data already exists
    cur.execute("SELECT COUNT(*) FROM hospitals")
    count = cur.fetchone()[0]
    if count > 0:
        conn.close()
        return False  # Already seeded
    
    # Seed hospitals (full dataset — matches local system)
    hospitals = [
        ('Kamla Nehru Hospital', '+919356992477', 18.5204, 73.8567, 15, 3),
        ('Dr. Naidu Contagious Disease Hospital', '+919356992477', 18.5195, 73.8555, 12, 2),
        ('Pune District Hospital (Pune Civil Hospital)', '+919356992477', 18.5300, 73.8000, 20, 5),
        ('Sassoon General Hospital', '+919356992477', 18.5250, 73.8500, 25, 6),
        ('Poona Hospital', '+919356992477', 18.5280, 73.8450, 18, 4),
        ('Ruby Hall Clinic', '+919356992477', 18.5249, 73.8478, 30, 8),
        ('Deenanath Mangeshkar Hospital', '+919356992477', 18.5150, 73.8200, 22, 5),
        ('Bharati Hospital', '+919356992477', 18.4500, 73.8700, 15, 3),
        ('Jehangir Hospital', '+919356992477', 18.5267, 73.8489, 28, 7),
        ('Noble Hospital', '+919356992477', 18.5000, 73.9000, 14, 3),
        ('Yashwantrao Chavan Memorial Hospital', '+919356992477', 18.6000, 73.8000, 20, 4),
        ('Dr. Bansal Hospital', '+919356992477', 18.5500, 73.7500, 10, 2),
        ('Sai Snehdeep Hospital', '+919356992477', 18.6100, 73.7800, 8, 1),
        ('Aditya Birla Memorial Hospital', '+919356992477', 18.5600, 73.7900, 35, 10),
        ('Lokmanya Hospital', '+919356992477', 18.6200, 73.8100, 12, 2),
        ('Niramaya Hospital', '+919356992477', 18.6300, 73.8200, 10, 2),
        ('Om Hospital', '+919356992477', 18.6400, 73.8300, 8, 1),
        ('Sainath Hospital', '+919356992477', 18.6500, 73.8400, 10, 2),
        ('Astha Hospital', '+919356992477', 18.6600, 73.8500, 12, 3),
        ('Sushrut Hospital', '+919356992477', 18.6700, 73.8600, 15, 3),
    ]
    cur.executemany('INSERT INTO hospitals (name, phone_no, latitude, longitude, available_beds, icu_beds) VALUES (?, ?, ?, ?, ?, ?)', hospitals)
    
    # Seed ambulances (2 per hospital)
    for i, h in enumerate(hospitals, 1):
        lat, lon = h[2], h[3]
        cur.execute('INSERT INTO ambulances (ambulance_no, driver_name, phone_no, latitude, longitude, hospital_id, status, current_latitude, current_longitude) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (f'AMB{i*2-1:03}', 'Driver A', '+919356992477', lat+0.001, lon+0.001, i, 'available', lat+0.001, lon+0.001))
        cur.execute('INSERT INTO ambulances (ambulance_no, driver_name, phone_no, latitude, longitude, hospital_id, status, current_latitude, current_longitude) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (f'AMB{i*2:03}', 'Driver B', '+919356992477', lat-0.001, lon-0.001, i, 'available', lat-0.001, lon-0.001))
    
    conn.commit()
    conn.close()
    logging.info("Database seeded with hospitals and ambulances!")
    return True


@app.route('/api/seed_database', methods=['GET', 'POST'])
def api_seed_database():
    """Manually trigger database seeding"""
    try:
        force = request.args.get('force', 'false').lower() == 'true'
        if force:
            # Force re-seed: delete existing data first
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()
            cur.execute("DELETE FROM hospitals")
            cur.execute("DELETE FROM ambulances")
            conn.commit()
            conn.close()
            logging.info("Force cleared hospitals and ambulances tables")
        
        was_seeded = seed_database()
        if was_seeded:
            return jsonify({"status": "success", "message": "Database seeded with 10 hospitals and 20 ambulances!"})
        else:
            return jsonify({"status": "already_seeded", "message": "Database already has data."})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/wifi_locate', methods=['POST'])
def wifi_locate():
    """
    Receive WiFi BSSID scan data from ESP32 and return GPS coordinates.
    Tries Google Geolocation API first, falls back to free alternatives.
    """
    try:
        data = request.json
        wifi_aps = data.get('wifiAccessPoints', [])
        
        if not wifi_aps:
            return jsonify({"error": "No WiFi access points provided"}), 400
        
        logging.info(f"📡 WiFi locate request with {len(wifi_aps)} access points")
        
        # Format access points
        formatted_aps = [
            {
                "macAddress": ap.get("macAddress", ""),
                "signalStrength": ap.get("signalStrength", -70),
                "channel": ap.get("channel", 0)
            }
            for ap in wifi_aps
        ]
        
        lat, lng, accuracy, source = None, None, None, None
        
        # ===== TRY 1: Google Geolocation API =====
        google_api_key = os.environ.get('GOOGLE_GEOLOCATION_KEY', '')
        if google_api_key:
            try:
                google_url = f"https://www.googleapis.com/geolocation/v1/geolocate?key={google_api_key}"
                resp = requests.post(google_url, json={"wifiAccessPoints": formatted_aps}, timeout=10)
                
                if resp.status_code == 200:
                    result = resp.json()
                    loc = result.get('location', {})
                    lat, lng = loc.get('lat'), loc.get('lng')
                    accuracy = result.get('accuracy', 0)
                    source = "google_wifi"
                    logging.info(f"📍 Google Geolocation: {lat}, {lng} (accuracy: {accuracy}m)")
                else:
                    logging.warning(f"Google API failed: {resp.status_code} — {resp.text[:200]}")
            except Exception as ge:
                logging.warning(f"Google Geolocation error: {ge}")
        
        # ===== TRY 2: Unwired Labs (free tier — 100 req/day) =====
        if lat is None:
            unwired_key = os.environ.get('UNWIRED_API_KEY', '')
            if unwired_key:
                try:
                    unwired_url = "https://us1.unwiredlabs.com/v2/process.php"
                    unwired_payload = {
                        "token": unwired_key,
                        "wifi": [{"bssid": ap["macAddress"], "signal": ap["signalStrength"]} for ap in formatted_aps]
                    }
                    resp = requests.post(unwired_url, json=unwired_payload, timeout=10)
                    if resp.status_code == 200:
                        result = resp.json()
                        if result.get('status') == 'ok':
                            lat = result.get('lat')
                            lng = result.get('lon')
                            accuracy = result.get('accuracy', 50)
                            source = "unwired_labs"
                            logging.info(f"📍 Unwired Labs: {lat}, {lng}")
                except Exception as ue:
                    logging.warning(f"Unwired Labs error: {ue}")
        
        # ===== TRY 3: Use stored device GPS location =====
        if lat is None and device_gps_location.get('latitude'):
            lat = device_gps_location['latitude']
            lng = device_gps_location['longitude']
            accuracy = device_gps_location.get('accuracy', 100)
            source = "device_gps_fallback"
            logging.info(f"📍 Fallback to stored device location: {lat}, {lng}")
        
        # Return result
        if lat is not None and lng is not None:
            # Update device location for other routes
            device_gps_location['latitude'] = lat
            device_gps_location['longitude'] = lng
            device_gps_location['accuracy'] = accuracy
            device_gps_location['timestamp'] = datetime.now(IST).isoformat()
            device_gps_location['source'] = source
            
            return jsonify({
                "latitude": lat,
                "longitude": lng,
                "accuracy": accuracy,
                "source": source
            })
        else:
            return jsonify({"error": "Could not determine location from any source"}), 500
            
    except Exception as e:
        logging.error(f"Error in /api/wifi_locate: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route('/api/db_check')
def db_check():
    """Debug: Check what's in the database"""
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        
        # Get table list
        cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [r[0] for r in cur.fetchall()]
        
        # Count rows in each table
        counts = {}
        for table in tables:
            cur.execute(f"SELECT COUNT(*) FROM {table}")
            counts[table] = cur.fetchone()[0]
        
        # Sample hospital
        sample_hospital = None
        try:
            cur.execute("SELECT * FROM hospitals LIMIT 1")
            row = cur.fetchone()
            if row:
                cols = [d[0] for d in cur.description]
                sample_hospital = dict(zip(cols, row))
        except:
            pass
        
        # Sample ambulance
        sample_ambulance = None
        try:
            cur.execute("SELECT * FROM ambulances LIMIT 1")
            row = cur.fetchone()
            if row:
                cols = [d[0] for d in cur.description]
                sample_ambulance = dict(zip(cols, row))
        except:
            pass
        
        conn.close()
        
        return jsonify({
            "tables": tables,
            "row_counts": counts,
            "sample_hospital": sample_hospital,
            "sample_ambulance": sample_ambulance
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# Initialize database on startup (create tables + seed baseline data)
init_database()


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(debug=False, host='0.0.0.0', port=port)