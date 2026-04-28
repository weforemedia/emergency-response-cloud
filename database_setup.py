import sqlite3
from datetime import datetime

# Connect to the SQLite database
conn = sqlite3.connect('emergency.db')
cursor = conn.cursor()

# Drop tables if they exist (to avoid schema conflicts)
cursor.execute('DROP TABLE IF EXISTS accident_history')
cursor.execute('DROP TABLE IF EXISTS accidents')
cursor.execute('DROP TABLE IF EXISTS hospitals')
cursor.execute('DROP TABLE IF EXISTS ambulances')

# ==================== ACCIDENT HISTORY TABLE ====================
# Logs all detected accidents for analytics and reporting
cursor.execute('''
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

# Accidents table (for manual reports)
cursor.execute('''
CREATE TABLE IF NOT EXISTS accidents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    latitude REAL,
    longitude REAL,
    reported_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
''')

# Hospitals table with bed availability
cursor.execute('''
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

# Ambulances table with status
cursor.execute('''
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

# Commit table creation
conn.commit()

# List of hospitals with their addresses and contact numbers
hospitals_data = [
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

# Insert hospitals into the database
cursor.executemany(
    'INSERT INTO hospitals (name, phone_no, latitude, longitude, available_beds, icu_beds) VALUES (?, ?, ?, ?, ?, ?)', 
    hospitals_data
)

# Generate ambulances for each hospital (two per hospital)
ambulances_data = []

# Loop through each hospital to create ambulances
for i, hospital in enumerate(hospitals_data, start=1):
    lat, lon = hospital[2], hospital[3]  # extract latitude & longitude of hospital
    hospital_id = i  # hospital ID (auto-increment assumed sequentially)

    # Two ambulances per hospital
    ambulances_data.append((
        f'AMB{i*2-1:03}', 'Driver A', '+919356992477', lat + 0.001, lon + 0.001, hospital_id, 'available', lat + 0.001, lon + 0.001
    ))
    ambulances_data.append((
        f'AMB{i*2:03}', 'Driver B', '+919356992477', lat - 0.001, lon - 0.001, hospital_id, 'available', lat - 0.001, lon - 0.001
    ))

# Insert ambulances into the database
cursor.executemany(
    'INSERT INTO ambulances (ambulance_no, driver_name, phone_no, latitude, longitude, hospital_id, status, current_latitude, current_longitude) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
    ambulances_data
)

# Commit changes and close the connection
conn.commit()
conn.close()

print("✅ Database populated successfully with new schema!")
print("   - accident_history table created")
print("   - Hospitals with bed availability")
print("   - Ambulances with status tracking")