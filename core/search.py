from core.database import get_db_connection
from config import Config
import time
import sys
import json

DATABASE = Config.DATABASE

def search_by_zip_upc(upc="", motherzipcode="", city="", state="", price="", deal_filter=False):
    """Optimized search function with single query and proper indexing"""
    with get_db_connection(DATABASE) as conn:
        cursor = conn.cursor()
        
        # Single optimized query that includes all necessary data in one go
        if deal_filter:
            query = """
            SELECT s.address, s.city, s.state, s.zipcode, s.store_url, 
                   i.name, i.upc, i.msrp, i.image_url, i.item_url, 
                   si.price, si.salesfloor, si.backroom, si.aisles,
                   ump.max_price, ump.description, ump.net, ump.department, ump.productid,
                   s.id as store_id
            FROM store_items si
            JOIN stores s ON si.store_id = s.id 
            JOIN items i ON si.item_id = i.id
            JOIN upc_max_prices ump ON i.upc = ump.upc
            WHERE si.price <= ump.max_price
            """
        else:
            query = """
            SELECT s.address, s.city, s.state, s.zipcode, s.store_url, 
                   i.name, i.upc, i.msrp, i.image_url, i.item_url, 
                   si.price, si.salesfloor, si.backroom, si.aisles,
                   ump.max_price, ump.description, ump.net, ump.department, ump.productid,
                   s.id as store_id
            FROM store_items si
            JOIN stores s ON si.store_id = s.id 
            JOIN items i ON si.item_id = i.id
            LEFT JOIN upc_max_prices ump ON i.upc = ump.upc
            """

        filters = []
        params = []
        
        # Build filters dynamically
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
            
        # Add filters to query
        if filters:
            if deal_filter:
                query += " AND " + " AND ".join(filters)
            else:
                query += " WHERE " + " AND ".join(filters)

        cursor.execute(query, params)
        results = cursor.fetchall()

    return results

def get_size_kb(data):
    """Return the size of an object in kilobytes."""
    return round(sys.getsizeof(json.dumps(data)) / 1024, 2)  # Convert bytes to KB

def process_zipcode_file(uploaded_file):
    """
    Process uploaded CSV or XLSX file and extract zipcodes
    """
    try:
        import pandas as pd
        
        print(f"Processing file: {uploaded_file.filename}")
        
        # Get file extension
        filename = uploaded_file.filename.lower()
        print(f"File extension check: {filename}")
        
        # Read file based on extension
        if filename.endswith('.csv'):
            print("Reading as CSV...")
            df = pd.read_csv(uploaded_file)
        elif filename.endswith(('.xlsx', '.xls')):
            print("Reading as Excel...")
            # Read Excel file - use first sheet by default
            df = pd.read_excel(uploaded_file, engine='openpyxl' if filename.endswith('.xlsx') else 'xlrd')
        else:
            print(f"Unsupported file format: {filename}")
            return None, "Unsupported file format. Please upload a CSV or XLSX file."
        
        print(f"File read successfully. Shape: {df.shape}")
        print(f"Columns: {list(df.columns)}")
        
        # Look for zipcode column (flexible naming)
        zipcode_col = None
        possible_names = ['zipcode', 'zip', 'postal_code', 'postcode', 'zip_code', 'postal code']
        
        for col in df.columns:
            if col.lower().strip() in possible_names:
                zipcode_col = col
                print(f"Found zipcode column: {col}")
                break
        
        if not zipcode_col:
            print(f"No zipcode column found. Available columns: {list(df.columns)}")
            return None, f"No zipcode column found. Expected one of: {', '.join(possible_names)}"
        
        # Extract zipcodes and clean them properly
        raw_zipcodes = df[zipcode_col].dropna()
        print(f"Raw zipcodes before conversion: {raw_zipcodes.head().tolist()}")
        
        # Convert to string and handle float values properly
        zipcodes = []
        for zipcode in raw_zipcodes:
            # Convert to string
            zipcode_str = str(zipcode).strip()
            
            # Remove .0 from float values (e.g., "64801.0" -> "64801")
            if zipcode_str.endswith('.0'):
                zipcode_str = zipcode_str[:-2]
            
            # Only keep non-empty zipcodes
            if zipcode_str and zipcode_str != 'nan':
                zipcodes.append(zipcode_str)
        
        print(f"Zipcodes after conversion: {zipcodes[:5]}")
        
        # Remove duplicates while preserving order
        seen = set()
        clean_zipcodes = []
        for zipcode in zipcodes:
            if zipcode not in seen:
                seen.add(zipcode)
                clean_zipcodes.append(zipcode)
        
        print(f"Clean zipcodes: {len(clean_zipcodes)}")
        print(f"Final sample zipcodes: {clean_zipcodes[:5]}")
        
        return clean_zipcodes, None
        
    except Exception as e:
        print(f"Exception in process_zipcode_file: {str(e)}")
        import traceback
        traceback.print_exc()
        return None, f"Error processing file: {str(e)}"


def bulk_search_by_zipcodes(zipcodes, upc="", price="", deal_filter=False, remove_zero_inventory=False):
    """
    Optimized bulk search function for multiple zipcodes
    """
    if not zipcodes:
        return []
    
    with get_db_connection(DATABASE) as conn:
        cursor = conn.cursor()
        
        # Build the base query
        if deal_filter:
            query = """
            SELECT s.address, s.city, s.state, s.zipcode, s.store_url, 
                   i.name, i.upc, i.msrp, i.image_url, i.item_url, 
                   si.price, si.salesfloor, si.backroom, si.aisles,
                   ump.max_price, ump.description, ump.net, ump.department, ump.productid,
                   s.id as store_id
            FROM store_items si
            JOIN stores s ON si.store_id = s.id 
            JOIN items i ON si.item_id = i.id
            JOIN upc_max_prices ump ON i.upc = ump.upc
            WHERE si.price <= ump.max_price
            """
        else:
            query = """
            SELECT s.address, s.city, s.state, s.zipcode, s.store_url, 
                   i.name, i.upc, i.msrp, i.image_url, i.item_url, 
                   si.price, si.salesfloor, si.backroom, si.aisles,
                   ump.max_price, ump.description, ump.net, ump.department, ump.productid,
                   s.id as store_id
            FROM store_items si
            JOIN stores s ON si.store_id = s.id 
            JOIN items i ON si.item_id = i.id
            LEFT JOIN upc_max_prices ump ON i.upc = ump.upc
            WHERE 1=1
            """

        filters = []
        params = []
        
        # Add zipcode filter using IN clause for efficiency
        zipcode_placeholders = ','.join(['?' for _ in zipcodes])
        filters.append(f"s.motherZip IN ({zipcode_placeholders})")
        params.extend(zipcodes)

        # Add optional filters
        if upc:
            filters.append("i.upc = ?")
            params.append(upc)

        if price:
            filters.append("si.price <= ?")
            params.append(price)
            
        if remove_zero_inventory:
            filters.append("(si.salesfloor > 0 OR si.backroom > 0)")
            
        # Add filters to query
        if filters:
            if deal_filter:
                query += " AND " + " AND ".join(filters)
            else:
                query += " AND " + " AND ".join(filters)

        cursor.execute(query, params)
        results = cursor.fetchall()

    return results


def bulk_search_by_zipcodes_chunked(zipcodes, upc="", price="", deal_filter=False, remove_zero_inventory=False, chunk_size=100):
    """
    Chunked version for handling large numbers of zipcodes
    """
    if not zipcodes:
        return []
    
    all_results = []
    
    # Process zipcodes in chunks to avoid SQL parameter limits
    for i in range(0, len(zipcodes), chunk_size):
        chunk = zipcodes[i:i + chunk_size]
        chunk_results = bulk_search_by_zipcodes(
            zipcodes=chunk,
            upc=upc,
            price=price,
            deal_filter=deal_filter,
            remove_zero_inventory=remove_zero_inventory
        )
        all_results.extend(chunk_results)
    
    return all_results