import json
import csv
import os
import time
import grequests
from gevent.lock import Semaphore
from gevent.event import Event
from gevent.queue import Queue
import gevent
from config import Config
from core.database import get_db_connection
from utils import log_message
import pandas as pd
from flask import Flask,g


# Global state
csv_processing = False
processing_lock = Semaphore()
upload_cancel_event = Event()
csv_queue = Queue()
app = Flask(__name__)

UPCZIP_DB = Config.UPCZIP_DB
API_URL = Config.API_URL
DATABASE = Config.DATABASE



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

            # Get current price before updating
            cursor.execute("SELECT price FROM store_items WHERE store_id = ? AND item_id = ?", (store_id, item_id))
            current_price_result = cursor.fetchone()
            
            # Store new price in history if there was a previous price and it's different
            if current_price_result and current_price_result[0] != store["price"]:
                cursor.execute("""
                    INSERT INTO price_history (store_id, item_id, price, timestamp)
                    VALUES (?, ?, ?, ?)
                """, (store_id, item_id, current_price_result[0], int(time.time())))

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
    """Worker that processes CSV or XLSX files from the queue."""
    while True:
        filepath = csv_queue.get()  # Block until a file is available
        try:
            with app.app_context():
                log_message(f"Processing file: {filepath}")
                g.success_count = 0
                g.error_count = 0

                # Read file based on extension
                ext = os.path.splitext(filepath)[1].lower()
                if ext == ".csv":
                    df = pd.read_csv(filepath, dtype=str, encoding="utf-8")
                elif ext == ".xlsx":
                    df = pd.read_excel(filepath, dtype=str)
                else:
                    log_message(f"Unsupported file format: {ext}")
                    os.remove(filepath)
                    continue

                # Normalize column headers
                original_headers = list(df.columns)
                normalized_headers = {col.strip().lower() for col in original_headers}
                required_headers = {"upc", "zip"}

                if not required_headers.issubset(normalized_headers):
                    os.remove(filepath)
                    log_message(f"Deleted {filepath} - Missing UPC/Zip headers, found instead {original_headers}")
                    continue

                # Normalize column names in the DataFrame
                df.columns = [col.strip().lower() for col in df.columns]

                rows = df.to_dict(orient="records")
                log_message(f"FOUND: {len(rows)} COMBOS")

                for counter, row in enumerate(rows):
                    log_message(f"PROCESSING: {counter + 1} / {len(rows)} Combo")

                    if upload_cancel_event.is_set():
                        log_message("PROCESS CANCELLED BY USER")
                        os.remove(filepath)
                        break

                    upc = row.get("upc")
                    zip_code = row.get("zip")

                    g.success_count = getattr(g, 'success_count', 0)
                    g.error_count = getattr(g, 'error_count', 0)

                    if process_entry(upc, zip_code):
                        g.success_count += 1
                    else:
                        g.error_count += 1

                log_message(f"Processing complete. Successes: {g.success_count}, Errors: {g.error_count}")

        except Exception as e:
            log_message(f"Error processing file: {e}")
        finally:
            if os.path.exists(filepath):
                os.remove(filepath)


def start_processing_worker():
    gevent.spawn(csv_worker)