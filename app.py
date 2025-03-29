from flask import Flask, request, render_template, Response, session, jsonify
from flask_socketio import SocketIO
import sqlite3
import json
import requests
import csv
import os
import io
import time

# Initialize Flask app and SocketIO
app = Flask(__name__)
app.secret_key = "admin"  
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")


# Constants
UPLOAD_FOLDER = "uploads"
if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)
UPCZIP_DB = "upczip.db"
DATABASE = "stores.db"
API_URL = "http://5.75.246.251:9099/stock/store"

# Helper functions
def log_message(message):
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
    if not upc:
        log_message("Skipping entry with missing UPC")
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
               i.name, i.msrp, i.image_url, i.item_url, 
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
@app.route("/adminpanel")
def admin():
    """Admin panel route"""
    return render_template("admin.html")

@app.route("/upload_csv", methods=["POST"])
def upload_csv():
    """Handle CSV file upload and process each entry"""
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
    
    with open(filepath, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            upc = row.get("UPC")
            zip_code = row.get("Zip")
            if process_entry(upc, zip_code):
                success_count += 1
            else:
                error_count += 1

    log_message(f"CSV processing complete. Successes: {success_count}, Errors: {error_count}")
    return jsonify({
        "message": "CSV processed successfully", 
        "processed": success_count,
        "errors": error_count
    })

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
        session["search_results"] = results  

    conn.close()
    
    return render_template("index.html", results=results, cities=cities, states=states, price=price)

@app.route('/export', methods=['GET'])
def export_data():
    """Export search results as CSV"""
    results = session.get("search_results")

    if not results:
        return "No search results found. Please perform a search first.", 400

    output = io.StringIO()
    writer = csv.writer(output)

    # Write header
    writer.writerow(["Name", "Store Address", "Store Price", "Salesfloor", "Backroom"])

    # Write data
    for row in results:
        writer.writerow([row[5], row[0], row[9], row[10], row[11]])

    output.seek(0)

    return Response(
        output, 
        mimetype="text/csv", 
        headers={"Content-Disposition": "attachment;filename=search_results.csv"}
    )

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

