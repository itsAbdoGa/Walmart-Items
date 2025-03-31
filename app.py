import eventlet
eventlet.monkey_patch()
import gc
from flask import Flask, request, render_template, Response, session, jsonify
from flask_socketio import SocketIO
import sqlite3
import json
import requests
import csv
import os
import io
import time
import sys
from threading import Lock , Event
# Initialize Flask app and SocketIO
app = Flask(__name__)
app.secret_key = "admin"  
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")
MAX_LOGS = 10
csv_processing = False
processing_lock = Lock()
upload_cancel_event = Event()

# Constants
UPLOAD_FOLDER = "uploads"
if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)
UPCZIP_DB = "upczip.db"
DATABASE = "stores.db"
API_URL = "http://5.75.246.251:9099/stock/store"
MAX_RESULTS_IN_SESSION = 10

# Helper functions
def log_message(message):
    if 'logs' not in session:
        session['logs'] = []
    session['logs'] = [message] + session['logs'][:MAX_LOGS - 1]  # Keep recent
    session.modified = True  # Force save
    """Log messages to console and emit to socket"""
    print(f"Logging: {message}", flush=True)
    socketio.emit('log_update', message , namespace="/")
    socketio.sleep(0)

def init_databases():
    """Initialize all necessary database tables"""
    # UPC-ZIP database
    conn = sqlite3.connect(UPCZIP_DB)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS upczip (
            upc TEXT NOT NULL,
            zip TEXT NOT NULL,
            timestamp INTEGER NOT NULL,
            PRIMARY KEY (upc, zip)
        )
    """)
    conn.commit()
    conn.close()
    
    # Main database
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()
    cursor.executescript("""
        CREATE TABLE IF NOT EXISTS stores (
            id INTEGER PRIMARY KEY, 
            address TEXT, 
            city TEXT, 
            state TEXT, 
            zipcode TEXT, 
            motherZip TEXT, 
            store_url TEXT
        );

        CREATE TABLE IF NOT EXISTS items (
            id INTEGER PRIMARY KEY AUTOINCREMENT, 
            name TEXT NOT NULL, 
            upc TEXT UNIQUE NOT NULL, 
            msrp REAL, 
            image_url TEXT, 
            item_url TEXT
        );

        CREATE TABLE IF NOT EXISTS store_items (
            store_id INTEGER, 
            item_id INTEGER, 
            price REAL, 
            salesfloor INTEGER DEFAULT 0, 
            backroom INTEGER DEFAULT 0,
            PRIMARY KEY (store_id, item_id),
            FOREIGN KEY (store_id) REFERENCES stores(id),
            FOREIGN KEY (item_id) REFERENCES items(id)
        );
    """)
    conn.commit()
    conn.close()

def store_upc_zip(upc, zip_code):
    """Store the UPC-ZIP combination in the database"""
    conn = sqlite3.connect(UPCZIP_DB)
    cursor = conn.cursor()
    current_time = int(time.time())
    
    cursor.execute("""
        INSERT INTO upczip (upc, zip, timestamp)
        VALUES (?, ?, ?)
        ON CONFLICT(upc, zip) DO UPDATE 
        SET timestamp = excluded.timestamp
    """, (upc, zip_code, current_time))
    
    log_message(f"Stored or updated: UPC {upc}, ZIP {zip_code}")
    conn.commit()
    conn.close()

def process_entry(upc, zip_code):
    """Process a single UPC-ZIP entry by sending a request and storing data"""
    if not upc or not zip_code:
        log_message("Skipping entry with missing UPC or Zipcode")
        return False
        
    store_upc_zip(upc, zip_code)
    payload = json.dumps({"storeName": "walmart", "upc": upc, "zip": zip_code})
    headers = {'Content-Type': 'application/json'}

    try:
        response = requests.post(API_URL, headers=headers, data=payload)
        response_data = response.json()
    except (requests.RequestException, json.JSONDecodeError) as e:
        log_message(f"Error processing {upc}: {str(e)}")
        return False

    if "stores" not in response_data or "itemDetails" not in response_data:
        log_message(f"Skipping {upc} due to missing data")
        log_message(f"Response: {response_data}")
        return False

    # Database operations
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()

    # Insert item details
    item = response_data["itemDetails"]
    cursor.execute("""
        INSERT INTO items (name, upc, msrp, image_url, item_url)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(upc) DO UPDATE 
        SET name=excluded.name, msrp=excluded.msrp, image_url=excluded.image_url, item_url=excluded.item_url
    """, (item["name"], upc, item.get("msrp"), item.get("imageUrl"), item.get("url")))
    conn.commit()

    # Get item ID
    cursor.execute("SELECT id FROM items WHERE upc = ?", (upc,))
    item_id = cursor.fetchone()[0]

    # Insert store data
    for store in response_data["stores"]:
        store_id = int(store["id"])
        cursor.execute("""
            INSERT OR IGNORE INTO stores (id, address, city, state, zipcode, motherZip, store_url)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (store_id, store["address"], store["city"], store["state"], store["zip"], zip_code, store["storeUrl"]))

        cursor.execute("""
            INSERT INTO store_items (store_id, item_id, price, salesfloor, backroom)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(store_id, item_id) DO UPDATE
            SET price = excluded.price, salesfloor = excluded.salesfloor, backroom = excluded.backroom
        """, (store_id, item_id, store["price"], store.get("salesFloor", 0), store.get("backRoom", 0)))

    conn.commit()
    conn.close()
    log_message(f"Processed: UPC {upc}, ZIP {zip_code}")
    return True

def search_by_zip_upc(upc="", motherzipcode="", city="", state="", price=""):
    """Search the database based on filters"""
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()

    query = """
        SELECT s.address, s.city, s.state, s.zipcode, s.store_url, 
               i.name, i.upc, i.msrp, i.image_url, i.item_url, 
               si.price, si.salesfloor, si.backroom
        FROM store_items si
        JOIN stores s ON si.store_id = s.id 
        JOIN items i ON si.item_id = i.id
    """

    filters = []
    params = []
    
    if upc:
        filters.append("i.upc = ?")
        params.append(upc)

    if motherzipcode:
        filters.append("s.motherZip = ?")
        params.append(motherzipcode)

    if city:
        filters.append("s.city = ?")
        params.append(city)

    if state:
        filters.append("s.state = ?")
        params.append(state)

    if price:
        filters.append("si.price <= ?")
        params.append(price)

    if filters:
        query += " WHERE " + " AND ".join(filters)

    cursor.execute(query, params)
    results = cursor.fetchall()

    conn.close()
    return results

# Routes
@app.route('/clear_logs', methods=['POST'])
def clear_logs():
    if 'logs' in session:
        session.pop('logs')  # Remove only logs (keeps other session data)
        session.modified = True  # Force save
        return jsonify({"message": "Logs cleared successfully"})
    return jsonify({"message": "No logs to clear"}), 404


@app.route("/adminpanel")
def admin():
    """Admin panel route"""
    return render_template("admin.html")

@app.route("/upload_csv", methods=["POST"])
def upload_csv():
    """Handle CSV file upload and process each entry"""
    global csv_processing,upload_cancel_event
    
    with processing_lock:
        if csv_processing:
            return jsonify({"message": "ERROR: Another file is already being processed"}), 429
        csv_processing = True
        upload_cancel_event.clear()  # Reset cancellation flag

    try:
        if "file" not in request.files:
            return jsonify({"message": "No file provided"}), 400

        file = request.files["file"]
        if file.filename == "":
            return jsonify({"message": "No selected file"}), 400

        filepath = os.path.join(UPLOAD_FOLDER, file.filename)
        file.save(filepath)
        log_message("CSV uploaded. Processing...")

        success_count = 0
        error_count = 0
        
        # First pass: header validation
        with open(filepath, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            headers = next(reader, [])
            
        if not {"UPC", "Zip"}.issubset(set(headers)):
            os.remove(filepath)
            log_message(f"Deleted {filepath} - Missing UPC/Zip headers")
            log_message(f"DEBUG: Found headers: {headers}")
            return jsonify({"message": "Invalid CSV headers"}), 400

        # Second pass: data processing
        with open(filepath, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = list(reader)  # Convert iterator to list

        log_message(f"FOUND : {len(rows)} COMBOS")
        
        for counter, row in enumerate(rows):
            log_message(f"PROCESSING : {counter + 1} / {len(rows)} Combo")
            
            if upload_cancel_event.is_set():  # Kill switch check
                log_message("PROCESS CANCELLED BY USER")
                os.remove(filepath)
                return jsonify({
                    "message": "Upload cancelled",
                    "processed": success_count,
                    "errors": error_count
                }), 499

            upc = row.get("UPC")
            zip_code = row.get("Zip")
            
            if process_entry(upc, zip_code):
                success_count += 1
            else:
                error_count += 1
            if counter % 100 == 0:
                gc.collect()

    finally:
        with processing_lock:
            csv_processing = False
            upload_cancel_event.clear()

    log_message(f"CSV processing complete. Successes: {success_count}, Errors: {error_count}")
    return jsonify({
        "message": "CSV processed successfully", 
        "processed": success_count,
        "errors": error_count
    })
    
    
@app.route('/cancel_upload', methods=['POST'])
def cancel_upload():
    """Endpoint to trigger cancellation"""
    global upload_cancel_event
    if csv_processing:
        upload_cancel_event.set()
        return jsonify({"message": "Cancellation signal sent"})
    return jsonify({"message": "No active upload to cancel"}), 404
@app.route("/manual_input", methods=["POST"])
def manual_input():
    """Handle manual UPC & ZIP input"""
    data = request.json
    upc = data.get("upc")
    zip_code = data.get("zip")

    if not upc or not zip_code:
        return jsonify({"message": "Both UPC and ZIP are required"}), 400

    log_message(f"Processing manual entry: UPC {upc}, ZIP {zip_code}")
    success = process_entry(upc, zip_code)
    
    if success:
        return jsonify({"message": "Manual entry processed successfully"})
    else:
        return jsonify({"message": "Failed to process entry"}), 400
    

def get_size_kb(data):
    """Return the size of an object in kilobytes."""
    return round(sys.getsizeof(json.dumps(data)) / 1024, 2)  # Convert bytes to KB


@app.route("/", methods=["GET", "POST"])
def index():
    """Main route for search interface"""
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()

    # Fetch distinct cities and states for dropdowns
    cursor.execute("SELECT DISTINCT city FROM stores ORDER BY city")
    cities = [row[0] for row in cursor.fetchall()]
    
    cursor.execute("SELECT DISTINCT state FROM stores ORDER BY state")
    states = [row[0] for row in cursor.fetchall()]

    results = None
    price = ""

    if request.method == "POST":
        upc = request.form.get("upc")
        zipcode = request.form.get("zipcode")
        city = request.form.get("city", "")
        state = request.form.get("state", "")
        price = request.form.get("price", "")

        results = search_by_zip_upc(upc, zipcode, city, state, price)
        results_size_kb = get_size_kb(results)
        print(f"Found : {results_size_kb} KB Worth of data")
        output = io.StringIO()
        writer = csv.writer(output)

        # Write header
        writer.writerow(["UPC","Name", "Store Address", "Store Price", "Salesfloor", "Backroom" , "City" , "State"])


        # Write data
        for row in results:
            writer.writerow([row[6],row[5], row[0], row[9], row[10], row[11] , row[1] , row[2]])

        output.seek(0)

        filename = "search_results"

        filters = []
        if upc:
            filters.append(f"UPC_{upc}")
        if zipcode:
            filters.append(f"ZIP_{zipcode}")
        if city:
            filters.append(f"City_{city}")
        if state:
            filters.append(f"State_{state}")
        if price:
            filters.append(f"Price_{price}")

        if filters:
            filename += "_" + "_".join(filters)

        filename += ".csv"

        return Response(
            output, 
            mimetype="text/csv", 
            headers={"Content-Disposition": f"attachment;filename={filename}"}
        )

    conn.close()
    
    return render_template("index.html", results=results, cities=cities, states=states, price=price)


@app.route("/get_cities")
def get_cities():
    """API endpoint to get cities by state for dynamic form updates"""
    state = request.args.get("state")

    if not state:
        return jsonify([]) 

    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()
    cursor.execute("SELECT DISTINCT city FROM stores WHERE state = ? ORDER BY city", (state,))
    cities = [row[0] for row in cursor.fetchall()]
    conn.close()

    return jsonify(cities)

# Initialize databases on startup
init_databases()



