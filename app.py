from gevent import monkey
monkey.patch_all()

from flask import Flask, request, render_template, Response, session, jsonify, g
from flask_socketio import SocketIO
import sqlite3
import json
import grequests
import csv
import os
import io
import time
import sys
from gevent.lock import Semaphore
from gevent.event import Event
from gevent.queue import Queue
import gevent
from datetime import datetime

# Application Constants
UPLOAD_FOLDER = "uploads"
UPCZIP_DB = "upczip.db"
DATABASE = "stores.db"
API_URL = "http://5.75.246.251:9099/stock/store"
MAX_LOGS = 10
MAX_RESULTS_IN_SESSION = 10

# Ensure upload directory exists
if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

# Initialize Flask app and SocketIO
app = Flask(__name__)
app.secret_key = "admin"  
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="gevent")

# Global state variables
csv_processing = False
processing_lock = Semaphore()
upload_cancel_event = Event()
csv_queue = Queue()
processing_semaphore = Semaphore()

# ===========================
# Database Helper Functions
# ===========================

def get_db_connection(db_path):
    """Create and return a database connection"""
    conn = sqlite3.connect(db_path)
    return conn

def init_databases():
    """Initialize all necessary database tables"""
    # UPC-ZIP database
    with get_db_connection(UPCZIP_DB) as conn:
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
    
    # Main database
    with get_db_connection(DATABASE) as conn:
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
                aisles TEXT DEFAULT NULL,
                PRIMARY KEY (store_id, item_id),
                FOREIGN KEY (store_id) REFERENCES stores(id),
                FOREIGN KEY (item_id) REFERENCES items(id)
            );
            
            CREATE TABLE IF NOT EXISTS upc_max_prices (
                upc TEXT PRIMARY KEY,
                max_price REAL NOT NULL,
                description TEXT
            );
        """)
        conn.commit()

# ===========================
# UPC and Store Data Processing 
# ===========================

def log_message(message):
    """Log messages to console and emit to socket"""
    print(f"Logging: {message}", flush=True)
    socketio.emit('log_update', message, namespace="/")
    socketio.sleep(0)

def store_upc_zip(upc, zip_code):
    """Store the UPC-ZIP combination in the database"""
    with get_db_connection(UPCZIP_DB) as conn:
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

def process_entry(upc, zip_code):
    """Process a single UPC-ZIP entry by sending a request and storing data"""
    if not upc or not zip_code:
        log_message("Skipping entry with missing UPC or Zipcode")
        return False
        
    store_upc_zip(upc, zip_code)
    payload = json.dumps({"storeName": "walmart", "upc": upc, "zip": zip_code})
    headers = {'Content-Type': 'application/json'}

    try:
        # Using grequests for async requests
        rs = (grequests.post(API_URL, headers=headers, data=payload))
        response = grequests.map([rs])[0]
        response_data = response.json()
    except (grequests.exceptions.RequestException, json.JSONDecodeError, AttributeError) as e:
        log_message(f"Error processing {upc}: {str(e)}")
        return False

    if "stores" not in response_data or "itemDetails" not in response_data:
        log_message(f"Skipping {upc} due to missing data")
        log_message(f"Response: {response_data}")
        return False

    # Database operations
    with get_db_connection(DATABASE) as conn:
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
                INSERT INTO store_items (store_id, item_id, price, salesfloor, backroom, aisles)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(store_id, item_id) DO UPDATE
                SET price = excluded.price, salesfloor = excluded.salesfloor, backroom = excluded.backroom, aisles = excluded.aisles
            """, (store_id, item_id, store["price"], store.get("salesFloor", 0), store.get("backRoom", 0), store.get("aisles", "None")))

        conn.commit()
    
    log_message(f"Processed: UPC {upc}, ZIP {zip_code}")
    return True

def csv_worker():
    """Worker that processes CSV files from the queue."""
    while True:
        filepath = csv_queue.get()  # Block until a CSV file is available
        try:
            with app.app_context():  # Ensure Flask app context is available
                log_message(f"Processing CSV file: {filepath}")
                g.success_count = 0
                g.error_count = 0

                # First pass: header validation
                with open(filepath, "r", encoding="utf-8") as f:
                    reader = csv.DictReader(f)
                    headers = reader.fieldnames or []
                
                if not {"UPC", "Zip"}.issubset(set(headers)):
                    os.remove(filepath)
                    log_message(f"Deleted {filepath} - Missing UPC/Zip headers")
                    continue  # Skip to the next file in the queue

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
                        break  # Stop processing this file

                    upc = row.get("UPC")
                    zip_code = row.get("Zip")
                    
                    # Example of using g for storing the count of successes and errors (optional)
                    g.success_count = g.success_count if hasattr(g, 'success_count') else 0
                    g.error_count = g.error_count if hasattr(g, 'error_count') else 0
                    
                    if process_entry(upc, zip_code):
                        g.success_count += 1
                    else:
                        g.error_count += 1

                log_message(f"CSV processing complete. Successes: {g.success_count}, Errors: {g.error_count}")

        except Exception as e:
            log_message(f"Error processing CSV: {e}")
        finally:
            if os.path.exists(filepath):
                os.remove(filepath)  # Ensure any file gets cleaned up

# ===========================
# Data Search Functions
# ===========================

def search_by_zip_upc(upc="", motherzipcode="", city="", state="", price="", deal_filter=False):
    """Search the database based on filters"""
    with get_db_connection(DATABASE) as conn:
        cursor = conn.cursor()
        
        base_query = """
        SELECT s.address, s.city, s.state, s.zipcode, s.store_url, 
               i.name, i.upc, i.msrp, i.image_url, i.item_url, 
               si.price, si.salesfloor, si.backroom, si.aisles
        FROM store_items si
        JOIN stores s ON si.store_id = s.id 
        JOIN items i ON si.item_id = i.id
        """

        deal_query = """
        SELECT s.address, s.city, s.state, s.zipcode, s.store_url, 
               i.name, i.upc, i.msrp, i.image_url, i.item_url, 
               si.price, si.salesfloor, si.backroom, si.aisles
        FROM store_items si
        JOIN stores s ON si.store_id = s.id 
        JOIN items i ON si.item_id = i.id
        JOIN upc_max_prices ump ON i.upc = ump.upc
        WHERE si.price <= ump.max_price
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
            
        # Use the appropriate base query
        query = deal_query if deal_filter else base_query
        
        # Add filters if we have any
        if filters and not deal_filter:
            query += " WHERE " + " AND ".join(filters)
        elif filters and deal_filter:
            query += " AND " + " AND ".join(filters)

        cursor.execute(query, params)
        results = cursor.fetchall()

    return results

def get_size_kb(data):
    """Return the size of an object in kilobytes."""
    return round(sys.getsizeof(json.dumps(data)) / 1024, 2)  # Convert bytes to KB

# ===========================
# Route Handlers
# ===========================

@app.route("/adminpanel")
def admin():
    """Admin panel route"""
    return render_template("admin.html")

@app.route('/clear_logs', methods=['POST'])
def clear_logs():
    if 'logs' in session:
        session.pop('logs')  # Remove only logs (keeps other session data)
        session.modified = True  # Force save
        return jsonify({"message": "Logs cleared successfully"})
    return jsonify({"message": "No logs to clear"}), 404

@app.route("/upload_csv", methods=["POST"])
def upload_csv():
    """Handle CSV file upload and process each entry"""
    global csv_processing, upload_cancel_event
    
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
        log_message(f"CSV uploaded. Processing... {filepath}")

        # Enqueue the CSV file for processing
        csv_queue.put(filepath)

        return jsonify({"message": f"CSV uploaded successfully. Will be processed shortly."}), 200

    except Exception as e:
        return jsonify({"message": f"Error uploading CSV: {e}"}), 500

    finally:
        with processing_lock:
            csv_processing = False
            upload_cancel_event.clear()

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

@app.route("/", methods=["GET", "POST"])
def index():
    """Main route for search interface"""
    with get_db_connection(DATABASE) as conn:
        cursor = conn.cursor()

        # Fetch distinct cities and states for dropdowns
        cursor.execute("SELECT DISTINCT city FROM stores ORDER BY city")
        cities = [row[0] for row in cursor.fetchall()]
        
        cursor.execute("SELECT DISTINCT state FROM stores ORDER BY state")
        states = [row[0] for row in cursor.fetchall()]

    results = None
    price = ""
    deal_filter = False

    if request.method == "POST":
        upc = request.form.get("upc")
        zipcode = request.form.get("zipcode")
        city = request.form.get("city", "")
        state = request.form.get("state", "")
        price = request.form.get("price", "")
        deal_filter = request.form.get("deal_filter") == "on"
        
        remove_zero_inventory = request.form.get("remove_zero_inventory") == "on"
        if remove_zero_inventory:
            with get_db_connection(DATABASE) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    DELETE FROM store_items 
                    WHERE salesfloor = 0 AND backroom = 0
                """)
                deleted_count = cursor.rowcount
                conn.commit()
                log_message(f"Deleted {deleted_count} items with zero inventory")

        results = search_by_zip_upc(upc, zipcode, city, state, price, deal_filter)
        results_size_kb = get_size_kb(results)
        print(f"Found : {results_size_kb} KB Worth of data")
        
        # Prepare CSV export
        output = io.StringIO()
        writer = csv.writer(output)

        # Write header
        writer.writerow(["UPC","Name", "Store Address", "Store Price", "Salesfloor", "Backroom", "City", "State", "Aisles"])

        # Write data
        for row in results:
            writer.writerow([row[6], row[5], row[0], row[10], row[11], row[12], row[1], row[2], row[13]])

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
        if deal_filter:
            filters.append("DealsOnly")

        if filters:
            filename += "_" + "_".join(filters)

        filename += ".csv"

        return Response(
            output, 
            mimetype="text/csv", 
            headers={"Content-Disposition": f"attachment;filename={filename}"}
        )
    
    return render_template("index.html", results=results, cities=cities, states=states, price=price)

@app.route("/get_cities")
def get_cities():
    """API endpoint to get cities by state for dynamic form updates"""
    state = request.args.get("state")

    if not state:
        return jsonify([]) 

    with get_db_connection(DATABASE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT DISTINCT city FROM stores WHERE state = ? ORDER BY city", (state,))
        cities = [row[0] for row in cursor.fetchall()]

    return jsonify(cities)

# ===========================
# Max Price Management Routes
# ===========================

@app.route("/max_prices")
def max_prices():
    """Route for managing UPC max prices"""
    return render_template("max_prices.html")

@app.route("/upload_max_prices", methods=["POST"])
def upload_max_prices():
    """Handle CSV file upload for UPC max prices"""
    try:
        if "file" not in request.files:
            return jsonify({"message": "No file provided"}), 400

        file = request.files["file"]
        if file.filename == "":
            return jsonify({"message": "No selected file"}), 400

        # Save and process the file
        filepath = os.path.join(UPLOAD_FOLDER, "max_prices_" + file.filename)
        file.save(filepath)
        log_message(f"Max prices CSV uploaded. Processing... {filepath}")

        # Process CSV file
        success_count = 0
        error_count = 0

        with get_db_connection(DATABASE) as conn:
            cursor = conn.cursor()
            
            with open(filepath, "r", encoding="utf-8") as f:
                reader = csv.reader(f)
                headers = [h.upper() for h in next(reader)]  # Convert headers to uppercase
                
                # Find column indices (case-insensitive matching)
                upc_col = next((i for i, h in enumerate(headers) if h == "UPC"), None)
                price_col = next((i for i, h in enumerate(headers) if h == "PRICE"), None)
                desc_col = next((i for i, h in enumerate(headers) if h == "DESCRIPTION"), None)
                
                if upc_col is None or price_col is None:
                    os.remove(filepath)
                    return jsonify({"message": "CSV must contain UPC and PRICE columns"}), 400
                
                for row in reader:
                    if len(row) <= max(upc_col, price_col):
                        error_count += 1
                        continue
                        
                    upc = row[upc_col].strip()
                    price_str = row[price_col].strip()
                    
                    # Remove currency symbols and clean price string
                    price_str = price_str.replace('$', '').replace('£', '').replace('€', '').strip()
                    
                    # Get description if available
                    description = ""
                    if desc_col is not None and len(row) > desc_col:
                        description = row[desc_col].strip()
                    
                    if not upc or not price_str:
                        error_count += 1
                        continue
                    
                    try:
                        price = float(price_str)
                        cursor.execute("""
                            INSERT INTO upc_max_prices (upc, max_price, description)
                            VALUES (?, ?, ?)
                            ON CONFLICT(upc) DO UPDATE 
                            SET max_price = excluded.max_price, 
                                description = excluded.description
                        """, (upc, price, description))
                        success_count += 1
                    except (ValueError, sqlite3.Error) as e:
                        log_message(f"Error processing {upc}: {str(e)}")
                        error_count += 1
            
            conn.commit()
        
        os.remove(filepath)
        
        return jsonify({
            "message": f"Max prices uploaded successfully. Processed: {success_count}, Errors: {error_count}"
        }), 200
        
    except Exception as e:
        return jsonify({"message": f"Error uploading max prices: {str(e)}"}), 500

@app.route("/manage_max_price", methods=["POST"])
def manage_max_price():
    """Add or update a single UPC max price entry"""
    data = request.json
    upc = data.get("upc")
    price = data.get("price")
    description = data.get("description", "")
    action = data.get("action", "add")  # 'add', 'update', or 'delete'
    
    if not upc:
        return jsonify({"message": "UPC is required"}), 400
        
    if action in ["add", "update"] and not price:
        return jsonify({"message": "Price is required for add/update"}), 400
    
    try:
        with get_db_connection(DATABASE) as conn:
            cursor = conn.cursor()
            
            if action == "delete":
                cursor.execute("DELETE FROM upc_max_prices WHERE upc = ?", (upc,))
                message = f"UPC {upc} removed from max price list"
            else:
                # Clean price string if it's a string (could be a number already)
                if isinstance(price, str):
                    price = price.replace('$', '').replace('£', '').replace('€', '').strip()
                
                cursor.execute("""
                    INSERT INTO upc_max_prices (upc, max_price, description)
                    VALUES (?, ?, ?)
                    ON CONFLICT(upc) DO UPDATE 
                    SET max_price = excluded.max_price,
                        description = excluded.description
                """, (upc, float(price), description))
                message = f"UPC {upc} max price set to ${float(price)}"
            
            conn.commit()
        
        return jsonify({"message": message, "success": True})
    
    except Exception as e:
        return jsonify({"message": f"Error: {str(e)}", "success": False}), 500

@app.route("/get_max_prices", methods=["GET"])
def get_max_prices():
    """Return all UPC max price entries"""
    with get_db_connection(DATABASE) as conn:
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT ump.upc, ump.max_price, ump.description, i.name 
            FROM upc_max_prices ump
            LEFT JOIN items i ON ump.upc = i.upc
            ORDER BY ump.upc
        """)
        
        results = cursor.fetchall()
    
    max_prices = []
    for row in results:
        max_prices.append({
            "upc": row[0],
            "max_price": row[1],
            "description": row[2],
            "name": row[3] if row[3] else "Unknown Item"
        })
    
    return jsonify(max_prices)

# ===========================
# Application Initialization
# ===========================

# Initialize databases on startup
init_databases()

# Start CSV worker thread
gevent.spawn(csv_worker)

# Run the app
if __name__ == "__main__":
    socketio.run(app=app, host="0.0.0.0", debug=True)