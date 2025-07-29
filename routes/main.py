from flask import Blueprint, render_template, request, Response, session, jsonify,flash
from core.search import search_by_zip_upc,bulk_search_by_zipcodes,bulk_search_by_zipcodes_chunked,process_zipcode_file
from core.database import get_db_connection
from utils import get_size_kb
from config import Config
import csv
import io
from utils import log_message
bp = Blueprint('main', __name__)


DATABASE = Config.DATABASE


@bp.route("/", methods=["GET", "POST"])
def index():
    """Debug version with detailed logging"""
    print("=== INDEX ROUTE CALLED ===")
    print(f"Request method: {request.method}")
    
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
        print("=== POST REQUEST DETECTED ===")
        
        # Debug: Print all form data
        print("Form data:")
        for key, value in request.form.items():
            print(f"  {key}: {value}")
        
        # Debug: Print all files
        print("Files:")
        for key, file in request.files.items():
            print(f"  {key}: {file.filename if file else 'No file'}")
        
        search_mode = request.form.get("search_mode", "single")
        print(f"Search mode: {search_mode}")
        
        price = request.form.get("price", "")
        deal_filter = request.form.get("deal_filter") == "on"
        remove_zero_inventory = request.form.get("remove_zero_inventory") == "on"
        
        print(f"Price: {price}")
        print(f"Deal filter: {deal_filter}")
        print(f"Remove zero inventory: {remove_zero_inventory}")
        
        # Handle zero inventory removal (common to both search modes)
        if remove_zero_inventory:
            print("Processing zero inventory removal...")
            with get_db_connection(DATABASE) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    DELETE FROM store_items 
                    WHERE salesfloor = 0 AND backroom = 0
                """)
                deleted_count = cursor.rowcount
                conn.commit()
                log_message(f"Deleted {deleted_count} items with zero inventory")
                print(f"Deleted {deleted_count} items with zero inventory")

        # Handle bulk search
        if search_mode == "bulk":
            print("=== PROCESSING BULK SEARCH ===")
            
            uploaded_file = request.files.get('csv_file')
            print(f"Uploaded file: {uploaded_file}")
            print(f"File name: {uploaded_file.filename if uploaded_file else 'None'}")
            
            if not uploaded_file or uploaded_file.filename == '':
                print("ERROR: No file uploaded")
                flash('Please upload a CSV or XLSX file for bulk search.', 'error')
                return render_template("index.html", results=None, cities=cities, states=states, price=price)
            
            print(f"Processing file: {uploaded_file.filename}")
            
            # Process file and extract zipcodes
            try:
                zipcodes, error = process_zipcode_file(uploaded_file)
                print(f"Zipcodes extracted: {len(zipcodes) if zipcodes else 0}")
                print(f"Error: {error}")
                
                if error:
                    print(f"ERROR in file processing: {error}")
                    flash(error, 'error')
                    return render_template("index.html", results=None, cities=cities, states=states, price=price)
                
                if not zipcodes:
                    print("ERROR: No zipcodes found")
                    flash('No valid zipcodes found in uploaded file.', 'warning')
                    return render_template("index.html", results=None, cities=cities, states=states, price=price)
                
                print(f"First 5 zipcodes: {zipcodes[:5]}")
                
                # Get bulk search parameters
                upc = request.form.get('bulk_upc', '').strip()
                print(f"Bulk UPC: '{upc}'")
                
                # Perform bulk search
                print("Starting bulk search...")
                
                if len(zipcodes) > 100:
                    print("Using chunked search for large zipcode list")
                    results = bulk_search_by_zipcodes_chunked(
                        zipcodes=zipcodes,
                        upc=upc,
                        price=price,
                        deal_filter=deal_filter,
                        remove_zero_inventory=False  # Already handled above
                    )
                else:
                    print("Using regular search for small zipcode list")
                    results = bulk_search_by_zipcodes(
                        zipcodes=zipcodes,
                        upc=upc,
                        price=price,
                        deal_filter=deal_filter,
                        remove_zero_inventory=False  # Already handled above
                    )
                
                print(f"Search completed. Results count: {len(results) if results else 0}")
                
                if results:
                    results_size_kb = get_size_kb(results)
                    print(f"Bulk search found: {results_size_kb} KB worth of data from {len(zipcodes)} zipcodes")
                    
                    # Prepare CSV export for bulk results
                    print("Preparing CSV export...")
                    output = io.StringIO()
                    writer = csv.writer(output)

                    # Write header
                    writer.writerow([
                        "UPC", "Name", "Store Address", "Store Price",  
                        "Salesfloor", "Backroom", "City", 
                        "State", "Aisles", "Zipcode", "Max Price Noted", "Description", "Net", "Department", "Product ID"
                    ])

                    for row in results:
                        upc_code = row[6]
                        max_price = row[14] or ""      # max_price from JOIN
                        description = row[15] or ""    # description from JOIN
                        net = row[16] or ""           # net from JOIN
                        department = row[17] or ""    # department from JOIN
                        productid = row[18] or ""     # productid from JOIN      
                        zipcode = row[3]              # zipcode from results
                        
                        writer.writerow([
                            upc_code, row[5], row[0], row[10],  
                            row[11], row[12], row[1], 
                            row[2], row[13], zipcode, max_price, description, net, department, productid
                        ])

                    output.seek(0)

                    # Generate filename for bulk export
                    filename = "bulk_search_results"
                    filters = []
                    
                    if upc:
                        filters.append(f"UPC_{upc}")
                    if price:
                        filters.append(f"Price_{price}")
                    if deal_filter:
                        filters.append("DealsOnly")
                    
                    filters.append(f"Zipcodes_{len(zipcodes)}")

                    if filters:
                        filename += "_" + "_".join(filters)

                    filename += ".csv"
                    
                    print(f"Returning CSV file: {filename}")

                    return Response(
                        output.getvalue(), 
                        mimetype="text/csv", 
                        headers={"Content-Disposition": f"attachment;filename={filename}"}
                    )
                else:
                    print("No results found, returning empty results")
                    flash(f'Bulk search completed for {len(zipcodes)} zipcodes. No results found.', 'info')
                    return render_template("index.html", results=[], cities=cities, states=states, price=price)
                
            except Exception as e:
                print(f"EXCEPTION in bulk search: {str(e)}")
                import traceback
                traceback.print_exc()
                flash(f'Error performing bulk search: {str(e)}', 'error')
                return render_template("index.html", results=None, cities=cities, states=states, price=price)

        # Handle single search (original logic)
        else:
            print("=== PROCESSING SINGLE SEARCH ===")
            upc = request.form.get("upc")
            zipcode = request.form.get("zipcode")
            city = request.form.get("city", "")
            state = request.form.get("state", "")

            print(f"Single search params - UPC: {upc}, Zipcode: {zipcode}, City: {city}, State: {state}")

            # Get results with all data in single query
            results = search_by_zip_upc(upc, zipcode, city, state, price, deal_filter)
            results_size_kb = get_size_kb(results)
            print(f"Single search found: {results_size_kb} KB worth of data")
            
            # Prepare CSV export for single search
            output = io.StringIO()
            writer = csv.writer(output)

            # Write header
            writer.writerow([
                "UPC", "Name", "Store Address", "Store Price",  
                "Salesfloor", "Backroom", "City", 
                "State", "Aisles", "Max Price Noted", "Description", "Net", "Department", "Product ID"
            ])

            for row in results:
                upc_code = row[6]
                max_price = row[14] or ""      # max_price from JOIN
                description = row[15] or ""    # description from JOIN
                net = row[16] or ""           # net from JOIN
                department = row[17] or ""    # department from JOIN
                productid = row[18] or ""     # productid from JOIN      
                writer.writerow([
                    upc_code, row[5], row[0], row[10],  
                    row[11], row[12], row[1], 
                    row[2], row[13], max_price, description, net, department, productid
                ])

            output.seek(0)

            # Generate filename for single search
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

            print(f"Returning single search CSV: {filename}")

            return Response(
                output.getvalue(), 
                mimetype="text/csv", 
                headers={"Content-Disposition": f"attachment;filename={filename}"}
            )
    
    print("=== RETURNING GET REQUEST ===")
    return render_template("index.html", results=results, cities=cities, states=states, price=price)

@bp.route("/get_cities")
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
