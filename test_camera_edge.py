"""
Edge Camera Client for Cloud Deployment
========================================
This script runs on your LOCAL laptop/PC with the webcam.
It detects accidents using the AI model and sends alerts to the CLOUD server.

USAGE:
  1. Deploy app_cloud.py to your cloud server (Render, GCP, etc.)
  2. Set CLOUD_SERVER_URL below to your cloud server's URL
  3. Run this script on your laptop: python test_camera_edge.py

This is a modified version of test_camera_integrated.py that:
  - Runs the camera and AI model LOCALLY (on your laptop)
  - Sends accident alerts to the CLOUD server (not localhost)
"""

import cv2
import numpy as np
from tensorflow.keras.models import load_model
from tensorflow.keras.applications.mobilenet_v2 import preprocess_input
import requests
import time
import json
import threading
from datetime import datetime
import os
import base64
import sys

# Fix Windows terminal Unicode (emoji) support
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

# ==================== CONFIGURATION ====================

# ⚠️ CHANGE THIS to your cloud server URL after deployment!
# Examples:
#   Render:  "https://your-app-name.onrender.com"
#   GCP:     "https://your-project-id.uc.r.appspot.com"
#   VPS:     "http://YOUR_SERVER_IP:5000"
CLOUD_SERVER_URL = "https://emergency-response-cloud.onrender.com"

# Camera location
CAMERA_CONFIG = {
    "camera_id": "CAM_EDGE_001",
    "camera_name": "Laptop Camera (Edge)",
    "latitude": None,
    "longitude": None,
    "use_fixed_location": False,
}

# Detection thresholds
ACCIDENT_THRESHOLD = 0.60
CONFIRMATION_THRESHOLD = 15
COOLDOWN_PERIOD = 300

AUTO_MODE_ENABLED = True

# ==================== HELPER FUNCTIONS ====================

def get_gps_location_from_server():
    """Fetch GPS location from the cloud server."""
    try:
        response = requests.get(f"{CLOUD_SERVER_URL}/get_device_location", timeout=10)
        if response.status_code == 200:
            data = response.json()
            if data.get("status") == "success" and data.get("location"):
                loc = data["location"]
                lat = loc.get("latitude")
                lon = loc.get("longitude")
                accuracy = loc.get("accuracy", "unknown")
                if lat is not None and lon is not None:
                    print(f"[INFO] ✅ Got GPS location from cloud: {lat}, {lon} (accuracy: {accuracy}m)")
                    return float(lat), float(lon)
    except requests.exceptions.ConnectionError:
        print(f"[WARNING] Cannot connect to cloud server: {CLOUD_SERVER_URL}")
    except Exception as e:
        print(f"[WARNING] Failed to get GPS location: {e}")
    return None, None


def open_location_setup_page():
    """Open the cloud server's location setup page."""
    import webbrowser
    location_url = f"{CLOUD_SERVER_URL}/location_setup"
    print(f"\n[INFO] Opening location setup page: {location_url}")
    print("[INFO] Allow location access in your browser to set GPS location.")
    try:
        webbrowser.open(location_url)
        return True
    except Exception as e:
        print(f"[ERROR] Failed to open browser: {e}")
        print(f"[INFO] Manually open: {location_url}")
        return False


def get_location_from_ip():
    """Fallback: Get location from IP address."""
    geolocation_services = [
        {"url": "http://ip-api.com/json/", "lat_key": "lat", "lon_key": "lon"},
        {"url": "https://ipwho.is/", "lat_key": "latitude", "lon_key": "longitude"},
        {"url": "https://ipapi.co/json/", "lat_key": "latitude", "lon_key": "longitude"}
    ]
    for service in geolocation_services:
        try:
            response = requests.get(service["url"], timeout=5)
            if response.status_code == 200:
                data = response.json()
                lat = data.get(service["lat_key"])
                lon = data.get(service["lon_key"])
                if lat is not None and lon is not None:
                    print(f"[INFO] IP-based location: {lat}, {lon}")
                    return float(lat), float(lon)
        except:
            continue
    return None, None


def get_camera_location():
    """Get camera location using best available method."""
    if CAMERA_CONFIG["use_fixed_location"] and CAMERA_CONFIG["latitude"] and CAMERA_CONFIG["longitude"]:
        return CAMERA_CONFIG["latitude"], CAMERA_CONFIG["longitude"]

    print("[INFO] 📡 Fetching GPS location from cloud server...")
    lat, lon = get_gps_location_from_server()
    if lat and lon:
        CAMERA_CONFIG["latitude"] = lat
        CAMERA_CONFIG["longitude"] = lon
        return lat, lon

    print("\n" + "=" * 60)
    print("⚠️  GPS LOCATION NOT CONFIGURED!")
    print("=" * 60)
    print("Opening location setup page on cloud server...")
    print("=" * 60 + "\n")

    open_location_setup_page()
    print("\n[WAITING] Set your location in the browser, then press Enter...")
    input()

    lat, lon = get_gps_location_from_server()
    if lat and lon:
        CAMERA_CONFIG["latitude"] = lat
        CAMERA_CONFIG["longitude"] = lon
        return lat, lon

    print("[WARNING] GPS not available, using IP-based location")
    lat, lon = get_location_from_ip()
    if lat and lon:
        CAMERA_CONFIG["latitude"] = lat
        CAMERA_CONFIG["longitude"] = lon
        return lat, lon

    print("[ERROR] Using default coordinates")
    return 18.5204, 73.8567


def save_accident_image(frame, camera_id):
    """Save the accident frame locally and return the filename."""
    try:
        captures_folder = "captures"
        if not os.path.exists(captures_folder):
            os.makedirs(captures_folder)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"accident_{camera_id}_{timestamp}.jpg"
        filepath = os.path.join(captures_folder, filename)
        cv2.imwrite(filepath, frame)
        print(f"[INFO] Accident image saved: {filepath}")
        return filename, filepath
    except Exception as e:
        print(f"[ERROR] Failed to save image: {e}")
        return None, None


def upload_accident_image(filepath):
    """Upload accident image to the CLOUD server."""
    try:
        with open(filepath, 'rb') as f:
            files = {'image': f}
            response = requests.post(
                f"{CLOUD_SERVER_URL}/upload_accident_image",
                files=files,
                timeout=30
            )
            if response.status_code == 200:
                result = response.json()
                print(f"[INFO] Image uploaded to cloud: {result.get('image_url')}")
                return result.get('image_url')
    except Exception as e:
        print(f"[ERROR] Failed to upload image to cloud: {e}")
    return None


def trigger_automatic_emergency_response(lat, lon, camera_id, frame=None):
    """Trigger emergency response on the CLOUD server."""
    try:
        print("\n" + "=" * 60)
        print("🚨 TRIGGERING EMERGENCY RESPONSE ON CLOUD SERVER")
        print(f"🌐 Server: {CLOUD_SERVER_URL}")
        print("=" * 60)

        # Re-fetch latest GPS location before sending alert
        fresh_lat, fresh_lon = get_gps_location_from_server()
        if fresh_lat and fresh_lon:
            lat, lon = fresh_lat, fresh_lon
            # Update global config too
            CAMERA_CONFIG["latitude"] = lat
            CAMERA_CONFIG["longitude"] = lon
            print(f"[INFO] 📍 Using fresh GPS: {lat}, {lon}")
        else:
            print(f"[INFO] 📍 Using cached GPS: {lat}, {lon}")

        image_url = None
        if frame is not None:
            filename, filepath = save_accident_image(frame, camera_id)
            if filepath:
                image_url = upload_accident_image(filepath)

        payload = {
            "latitude": lat,
            "longitude": lon,
            "camera_id": camera_id,
            "timestamp": datetime.now().isoformat(),
            "mode": "automatic",
            "image_url": image_url
        }

        print(f"[INFO] Sending to: {CLOUD_SERVER_URL}/trigger_auto_response")

        response = requests.post(
            f"{CLOUD_SERVER_URL}/trigger_auto_response",
            json=payload,
            timeout=30
        )

        if response.status_code == 200:
            result = response.json()
            print("\n✅ EMERGENCY RESPONSE TRIGGERED ON CLOUD!")
            print(f"   - Ambulance: {result.get('ambulance', 'N/A')}")
            print(f"   - Hospital: {result.get('hospital', 'N/A')}")
            print(f"   - SMS: {result.get('sms_status', 'N/A')}")
            print(f"   - Route: {result.get('route_link', 'N/A')}")
            return True
        else:
            print(f"\n⚠️ Cloud response status: {response.status_code}")
            print(f"   Response: {response.text}")
            return False

    except requests.exceptions.ConnectionError:
        print(f"\n❌ ERROR: Cannot connect to cloud server!")
        print(f"   Make sure {CLOUD_SERVER_URL} is running")
        return False
    except Exception as e:
        print(f"\n❌ ERROR: {e}")
        return False


def check_cloud_connection():
    """Check if the cloud server is reachable."""
    try:
        response = requests.get(f"{CLOUD_SERVER_URL}/", timeout=10)
        return response.status_code == 200
    except:
        return False


def fetch_settings():
    """Fetch camera settings from the cloud server."""
    try:
        response = requests.get(f"{CLOUD_SERVER_URL}/api/settings", timeout=5)
        if response.status_code == 200:
            settings = response.json()
            cam_settings = settings.get("camera_settings", {})
            global ACCIDENT_THRESHOLD, CONFIRMATION_THRESHOLD, COOLDOWN_PERIOD
            new_thresh = cam_settings.get("accident_threshold", ACCIDENT_THRESHOLD)
            new_frames = cam_settings.get("confirmation_frames", CONFIRMATION_THRESHOLD)
            new_cooldown = cam_settings.get("cooldown_seconds", COOLDOWN_PERIOD)
            if new_thresh != ACCIDENT_THRESHOLD or new_frames != CONFIRMATION_THRESHOLD or new_cooldown != COOLDOWN_PERIOD:
                ACCIDENT_THRESHOLD = new_thresh
                CONFIRMATION_THRESHOLD = new_frames
                COOLDOWN_PERIOD = new_cooldown
                print(f"[INFO] ⚙️ Settings from cloud: Thresh={ACCIDENT_THRESHOLD}, Frames={CONFIRMATION_THRESHOLD}, Cooldown={COOLDOWN_PERIOD}s")
            return True
    except:
        pass
    return False


# ==================== MAIN DETECTION LOOP ====================

def run_accident_detection():
    """Main accident detection loop - runs locally, sends alerts to cloud."""
    print("\n" + "=" * 60)
    print("EDGE CAMERA CLIENT - CLOUD DEPLOYMENT MODE")
    print(f"Cloud Server: {CLOUD_SERVER_URL}")
    print("=" * 60)

    if AUTO_MODE_ENABLED:
        if check_cloud_connection():
            print("✅ Connected to Cloud Server")
            fetch_settings()
        else:
            print("⚠️ WARNING: Cloud server not reachable!")
            print(f"   Check if {CLOUD_SERVER_URL} is running.")
            print("   Starting in detection-only mode...")

    print(f"\n[CONFIG] Camera ID: {CAMERA_CONFIG['camera_id']}")
    print(f"[CONFIG] Auto Mode: {'ENABLED' if AUTO_MODE_ENABLED else 'DISABLED'}")
    print(f"[CONFIG] Accident Threshold: {ACCIDENT_THRESHOLD}")
    print(f"[CONFIG] Confirmation Frames: {CONFIRMATION_THRESHOLD}")
    print(f"[CONFIG] Cooldown Period: {COOLDOWN_PERIOD}s")

    lat, lon = get_camera_location()
    print(f"[CONFIG] Camera Location: {lat}, {lon}")
    print("=" * 60)
    print("\nPress ESC to exit\n")

    print("[INFO] Loading accident detection model...")
    model = load_model("accident_model.h5")
    print("[INFO] Model loaded successfully!")

    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        print("❌ ERROR: Cannot open camera!")
        return

    accident_count = 0
    accident_confirmed = False
    last_accident_time = 0
    emergency_triggered = False
    last_settings_fetch = 0
    SETTINGS_FETCH_INTERVAL = 10  # Check cloud settings every 10 seconds

    while True:
        ret, frame = cap.read()
        if not ret:
            print("[WARNING] Failed to grab frame")
            break

        current_time = time.time()
        if current_time - last_settings_fetch > SETTINGS_FETCH_INTERVAL:
            fetch_settings()
            last_settings_fetch = current_time

        img = cv2.resize(frame, (224, 224))
        img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        img = np.array(img, dtype=np.float32)
        img = preprocess_input(img)
        img = np.reshape(img, (1, 224, 224, 3))

        raw_pred = model.predict(img, verbose=0)[0][0]
        prediction = 1.0 - raw_pred

        current_time = time.time()
        in_cooldown = (current_time - last_accident_time) < COOLDOWN_PERIOD

        if in_cooldown:
            remaining = int(COOLDOWN_PERIOD - (current_time - last_accident_time))
            status_text = f"COOLDOWN: {remaining}s remaining"
            cv2.putText(frame, status_text, (20, 80),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 165, 0), 2)
        else:
            if emergency_triggered:
                emergency_triggered = False
                accident_confirmed = False
                accident_count = 0

        if int(time.time() * 10) % 30 == 0:
            print(f"[DEBUG] Prediction: {prediction:.6f}")

        if not in_cooldown:
            if prediction > ACCIDENT_THRESHOLD:
                accident_count += 1
            else:
                accident_count = 0
                accident_confirmed = False

            if accident_count >= CONFIRMATION_THRESHOLD and not accident_confirmed:
                accident_confirmed = True
                print(f"\n🚨 ACCIDENT CONFIRMED! (Prob: {prediction:.4f})")

                if AUTO_MODE_ENABLED and not emergency_triggered:
                    emergency_triggered = True
                    last_accident_time = current_time
                    accident_frame = frame.copy()
                    # Use latest stored location (trigger function will re-fetch fresh GPS)
                    current_lat = CAMERA_CONFIG.get("latitude", lat)
                    current_lon = CAMERA_CONFIG.get("longitude", lon)
                    threading.Thread(
                        target=trigger_automatic_emergency_response,
                        args=(current_lat, current_lon, CAMERA_CONFIG["camera_id"], accident_frame),
                        daemon=True
                    ).start()

        if accident_confirmed:
            label = "ACCIDENT CONFIRMED"
            color = (0, 0, 255)
            if emergency_triggered:
                label += " - SENT TO CLOUD"
        else:
            label = "NO ACCIDENT"
            color = (0, 255, 0)

        cv2.putText(frame, label, (20, 40),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, color, 2)

        info = f"Prob: {prediction:.3f} | Count: {accident_count}/{CONFIRMATION_THRESHOLD}"
        cv2.putText(frame, info, (20, frame.shape[0] - 40),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)

        mode_text = f"EDGE MODE -> {CLOUD_SERVER_URL}"
        cv2.putText(frame, mode_text, (20, frame.shape[0] - 15),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, (200, 200, 200), 1)

        controls_text = "ESC=Exit | R=Restart | C=Skip Cooldown"
        cv2.putText(frame, controls_text, (frame.shape[1] - 350, frame.shape[0] - 15),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, (150, 150, 150), 1)

        cv2.imshow("Edge Camera - Cloud Mode", frame)

        key = cv2.waitKey(1) & 0xFF
        if key == 27:
            break
        elif key == ord('r') or key == ord('R'):
            accident_count = 0
            accident_confirmed = False
            emergency_triggered = False
            last_accident_time = 0
            print("\n[INFO] 🔄 Detection RESTARTED!")
        elif key == ord('c') or key == ord('C'):
            if in_cooldown:
                last_accident_time = 0
                emergency_triggered = False
                accident_confirmed = False
                accident_count = 0
                print("\n[INFO] ⏭️ Cooldown SKIPPED!")

    cap.release()
    cv2.destroyAllWindows()
    print("\n[INFO] Edge camera stopped.")


if __name__ == "__main__":
    run_accident_detection()
