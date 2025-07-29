import json
import os
import time
import grequests
from gevent.lock import Semaphore
from gevent.queue import Queue
from gevent import queue
import gevent
from config import Config
from core.database import get_db_connection
from utils import log_message
from flask import Flask
from enum import IntEnum


app = Flask(__name__)

UPCZIP_DB = Config.UPCZIP_DB
API_URL = Config.API_URL
DATABASE = Config.DATABASE
UPLOAD_FOLDER = Config.UPLOAD_FOLDER



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


class Priority(IntEnum):
    HIGH = 1    # Manual input (higher priority)
    LOW = 2     # CSV batch processing (lower priority)

# Replace your existing csv_queue with a priority queue
priority_queue = queue.PriorityQueue()
processing_worker = None
csv_processing = False

def start_processing_worker():
    """Start the background worker greenlet if not already running"""
    global processing_worker
    if processing_worker is None or processing_worker.dead:
        processing_worker = gevent.spawn(queue_worker)

def queue_worker():
    """Background worker that processes items from the priority queue"""
    global csv_processing
    
    while True:
        try:
            # Get item from priority queue (blocks until available)
            priority, item_type, data = priority_queue.get()
            
            if item_type == "manual":
                upc, zip_code = data
                log_message(f"Processing HIGH PRIORITY manual entry: UPC {upc}, ZIP {zip_code}")
                success = process_entry(upc, zip_code)
                log_message(f"Manual entry processed: {'success' if success else 'failed'}")
                
            elif item_type == "csv":
                if isinstance(data, tuple):
                    # New format with row tracking
                    filepath, start_row, total_rows = data
                    csv_processing = True
                    log_message(f"Processing CSV file: {filepath}")
                    process_csv_file(filepath, start_row, total_rows)
                else:
                    # Original format (backward compatibility)
                    filepath = data
                    csv_processing = True
                    log_message(f"Processing CSV file: {filepath}")
                    process_csv_file(filepath)
                csv_processing = False
                
            # gevent.queue doesn't have task_done(), just continue
            
        except Exception as e:
            log_message(f"Error in queue worker: {e}")
            csv_processing = False

def process_csv_file(filepath, start_row=0, total_original_rows=None):
    """Process a CSV file row by row, allowing interruption for high priority items"""
    try:
        import pandas as pd
        
        # Read the CSV/Excel file
        if filepath.endswith('.xlsx') or filepath.endswith('.xls'):
            df = pd.read_excel(filepath, engine='openpyxl')
        else:
            df = pd.read_csv(filepath)
        
        # Calculate total rows for progress tracking
        if total_original_rows is None:
            total_original_rows = len(df)
            current_start = 1
        else:
            current_start = start_row + 1
            
        log_message(f"Starting CSV processing: {len(df)} rows (rows {current_start}-{start_row + len(df)} of {total_original_rows} total)")
        
        for index, row in df.iterrows():
            # Check if there are higher priority items waiting
            if not priority_queue.empty():
                # Peek at the next item to check priority
                try:
                    # Create a temporary list to check priorities without consuming items
                    temp_items = []
                    has_high_priority = False
                    
                    # Check up to 5 items in queue for high priority
                    for _ in range(min(5, priority_queue.qsize())):
                        try:
                            item = priority_queue.get_nowait()
                            temp_items.append(item)
                            if item[0] == Priority.HIGH:
                                has_high_priority = True
                        except queue.Empty:
                            break
                    
                    # Put items back in queue
                    for item in temp_items:
                        priority_queue.put(item)
                    
                    if has_high_priority:
                        log_message("Pausing CSV processing for high priority manual input")
                        # Re-queue the remaining CSV data
                        remaining_df = df.iloc[index:]
                        if len(remaining_df) > 0:
                            # Preserve original file format
                            base_name = os.path.splitext(os.path.basename(filepath))[0]
                            original_ext = os.path.splitext(filepath)[1]
                            temp_filepath = f"temp_{int(time.time())}_{base_name}{original_ext}"
                            temp_full_path = os.path.join(UPLOAD_FOLDER, temp_filepath)
                            
                            # Save in same format as original
                            if original_ext.lower() in ['.xlsx', '.xls']:
                                remaining_df.to_excel(temp_full_path, index=False, engine='openpyxl')
                            else:
                                remaining_df.to_csv(temp_full_path, index=False)
                            
                            # Calculate the new start row for the remaining data
                            new_start_row = start_row + index
                            priority_queue.put((Priority.LOW, "csv", (temp_full_path, new_start_row, total_original_rows)))
                            log_message(f"Remaining {len(remaining_df)} rows re-queued as {temp_filepath} (continuing from row {new_start_row + 1})")
                        return  # Exit current CSV processing
                        
                except (queue.Empty, IndexError):
                    pass  # No items or error checking queue
            
            # Process current row
            actual_row_number = start_row + index + 1
            upc = row.get('upc') or row.get('UPC') or row.get('Upc')
            zip_code = row.get('zip') or row.get('ZIP') or row.get('Zip')
            
            if upc and zip_code:
                log_message(f"Processing CSV row {actual_row_number}/{total_original_rows}: UPC {upc}, ZIP {zip_code}")
                success = process_entry(str(upc), str(zip_code))
                if not success:
                    log_message(f"Failed to process row {actual_row_number}")
            else:
                log_message(f"Skipping row {actual_row_number}/{total_original_rows}: missing UPC or ZIP")
            
            # Yield control to allow other greenlets to run
            gevent.sleep(0.01)
            
        log_message(f"Completed CSV processing: {filepath}")
            
    except Exception as e:
        log_message(f"Error processing CSV file {filepath}: {e}")
    finally:
        # Clean up temporary file if it exists
        if os.path.basename(filepath).startswith("temp_"):
            try:
                os.remove(filepath)
                log_message(f"Cleaned up temporary file: {filepath}")
            except Exception as e:
                log_message(f"Error cleaning up temp file: {e}")
