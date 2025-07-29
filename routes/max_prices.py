from flask import Blueprint, request, jsonify,render_template
from core.database import get_db_connection
from config import Config
import csv
import os
from utils import log_message
import sqlite3
import time


bp = Blueprint('max_prices', __name__)

DATABASE = Config.DATABASE
UPLOAD_FOLDER = Config.UPLOAD_FOLDER
UPCZIP_DB = Config.UPCZIP_DB
@bp.route("/max_prices")
def max_prices():
    """Route for managing UPC max prices"""
    return render_template("max_prices.html")

@bp.route("/upload_max_prices", methods=["POST"])
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
                net_col = next((i for i, h in enumerate(headers) if h == "NET"), None)
                dept_col = next((i for i, h in enumerate(headers) if h == "DEPARTMENT"), None)
                product_col = next((i for i, h in enumerate(headers) if h == "PRODUCTID"), None)
                
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
                    
                    # Get optional fields if available
                    description = ""
                    if desc_col is not None and len(row) > desc_col:
                        description = row[desc_col].strip()
                    
                    net = None
                    if net_col is not None and len(row) > net_col and row[net_col].strip():
                        net_str = row[net_col].strip().replace('$', '').replace('£', '').replace('€', '').strip()
                        try:
                            net = float(net_str)
                        except ValueError:
                            pass  # Leave as None if can't convert
                    
                    department = ""
                    if dept_col is not None and len(row) > dept_col:
                        department = row[dept_col].strip()
                    
                    productid = ""
                    if product_col is not None and len(row) > product_col:
                        productid = row[product_col].strip()
                    
                    if not upc or not price_str:
                        error_count += 1
                        continue
                    
                    try:
                        price = float(price_str)
                        cursor.execute("""
                            INSERT INTO upc_max_prices (upc, max_price, description, net, department , productid)
                            VALUES (?, ?, ?, ?, ? , ?)
                            ON CONFLICT(upc) DO UPDATE 
                            SET max_price = excluded.max_price, 
                                description = excluded.description,
                                net = excluded.net,
                                department = excluded.department,
                                productid = excluded.productid
                        """, (upc, price, description, net, department, productid))
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

# Update the manage_max_price route to handle new fields
@bp.route("/manage_max_price", methods=["POST"])
def manage_max_price():
    """Add, update or delete a max price record"""
    try:
        data = request.json
        action = data.get("action")
        
        with get_db_connection(DATABASE) as conn:
            cursor = conn.cursor()
            
            if action == "add":
                upc = data.get("upc")
                price = data.get("price")
                description = data.get("description", "")
                net = data.get("net")
                department = data.get("department", "")
                productid = data.get("productid","")
                
                if not upc or not price:
                    return jsonify({"success": False, "message": "UPC and price are required"}), 400
                
                try:
                    price = float(price)
                    if net is not None and net != "":
                        net = float(net)
                    else:
                        net = None
                        
                    cursor.execute("""
                        INSERT INTO upc_max_prices (upc, max_price, description, net, department,productid)
                        VALUES (?, ?, ?, ?, ? , ?)
                        ON CONFLICT(upc) DO UPDATE 
                        SET max_price = excluded.max_price, 
                            description = excluded.description,
                            net = excluded.net,
                            department = excluded.department,
                            productid = excluded.productid
                            
                    """, (upc, price, description, net, department,productid))
                    conn.commit()
                    
                    return jsonify({
                        "success": True, 
                        "message": f"Max price for UPC {upc} added/updated successfully"
                    }), 200
                except ValueError:
                    return jsonify({
                        "success": False, 
                        "message": "Price and net must be valid numbers"
                    }), 400
                except sqlite3.Error as e:
                    return jsonify({
                        "success": False, 
                        "message": f"Database error: {str(e)}"
                    }), 500
            
            elif action == "delete":
                upc = data.get("upc")
                
                if not upc:
                    return jsonify({"success": False, "message": "UPC is required"}), 400
                
                try:
                    cursor.execute("DELETE FROM upc_max_prices WHERE upc = ?", (upc,))
                    conn.commit()
                    
                    return jsonify({
                        "success": True, 
                        "message": f"Max price for UPC {upc} deleted successfully"
                    }), 200
                except sqlite3.Error as e:
                    return jsonify({
                        "success": False, 
                        "message": f"Database error: {str(e)}"
                    }), 500
            
            else:
                return jsonify({
                    "success": False, 
                    "message": "Invalid action"
                }), 400
                
    except Exception as e:
        return jsonify({
            "success": False, 
            "message": f"Error managing max price: {str(e)}"
        }), 500

@bp.route("/get_max_prices", methods=["GET"])
def get_max_prices():
    """Return all UPC max price entries"""
    with get_db_connection(DATABASE) as conn:
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT ump.upc, ump.max_price, ump.description, ump.net, ump.department ,ump.productid , i.name 
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
            "net": row[3],
            "department": row[4],
            "productid" : row[5],
            "name": row[6] if row[6] else "Unknown Item"
        })
    
    return jsonify(max_prices)
@bp.route('/clear_old_items', methods=['POST'])
def clear_old_items():
    """Delete items from database older than specified days"""
    try:
        data = request.json
        days = data.get('days', 30)  # Default to 30 days if not specified
        
        if not isinstance(days, int) or days < 1:
            return jsonify({"message": "Invalid days parameter. Must be a positive integer.", "success": False}), 400
            
        # Calculate the cutoff timestamp (current time - days)
        cutoff_timestamp = int(time.time()) - (days * 24 * 60 * 60)
        
        # Step 1: Get the list of UPCs to be deleted from upczip table
        with get_db_connection(UPCZIP_DB) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT DISTINCT upc FROM upczip WHERE timestamp < ?", (cutoff_timestamp,))
            old_upcs = [row[0] for row in cursor.fetchall()]
            
            # Now delete the old records from upczip
            cursor.execute("DELETE FROM upczip WHERE timestamp < ?", (cutoff_timestamp,))
            upczip_deleted = cursor.rowcount
            conn.commit()
        
        log_message(f"Found {len(old_upcs)} unique UPCs to remove")
        
        # Step 2: Delete from other tables in the main database
        items_deleted = 0
        store_items_deleted = 0
        price_history_deleted = 0
        
        if old_upcs:
            with get_db_connection(DATABASE) as conn:
                cursor = conn.cursor()
                
                # First, find all item IDs associated with these UPCs
                placeholders = ','.join(['?' for _ in old_upcs])
                cursor.execute(f"SELECT id FROM items WHERE upc IN ({placeholders})", old_upcs)
                item_ids = [row[0] for row in cursor.fetchall()]
                
                # Step 3: Delete from store_items using the item IDs
                if item_ids:
                    placeholders = ','.join(['?' for _ in item_ids])
                    cursor.execute(f"DELETE FROM store_items WHERE item_id IN ({placeholders})", item_ids)
                    store_items_deleted = cursor.rowcount
                    
                    # Step 4: Delete price history records using the item IDs
                    cursor.execute(f"DELETE FROM price_history WHERE item_id IN ({placeholders})", item_ids)
                    price_history_deleted = cursor.rowcount
                
                # Step 5: Finally delete the items from the items table
                placeholders = ','.join(['?' for _ in old_upcs])
                cursor.execute(f"DELETE FROM items WHERE upc IN ({placeholders})", old_upcs)
                items_deleted = cursor.rowcount
                
                conn.commit()
        
        log_message(f"Cleared items older than {days} days: {upczip_deleted} UPC-ZIP combinations, {items_deleted} items, {store_items_deleted} store items, {price_history_deleted} price history records")
        
        return jsonify({
            "message": f"Successfully removed {upczip_deleted} UPC-ZIP combinations, {items_deleted} items, {store_items_deleted} store items, and {price_history_deleted} price history records older than {days} days.",
            "success": True,
            "upczip_deleted": upczip_deleted,
            "items_deleted": items_deleted,
            "store_items_deleted": store_items_deleted,
            "price_history_deleted": price_history_deleted
        })
        
    except Exception as e:
        log_message(f"Error clearing old items: {str(e)}")
        return jsonify({"message": f"Error: {str(e)}", "success": False}), 500