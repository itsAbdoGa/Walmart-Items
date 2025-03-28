from flask import Flask, request, render_template, Response, session , jsonify
import sqlite3
import csv
import io
import pgeocode
from geopy.distance import geodesic
app = Flask(__name__)
app.secret_key = "admin"  


import sqlite3
from geopy.distance import geodesic

def search_by_zip_upc(zipcode, radius, city="", state="", price=""):
    conn = sqlite3.connect("stores.db")
    cursor = conn.cursor()
    
    # Get ZIP code coordinates (cache to avoid repeated API calls)
    nomi = pgeocode.Nominatim("US")
    search_location = nomi.query_postal_code(zipcode)
    search_coords = (search_location.latitude, search_location.longitude)
    
    # Query stores and items directly, avoid fetching unnecessary data
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
    
    nearby_stores = []
    
    # Pre-fetch store coordinates and cache them to avoid multiple geocoding calls
    store_coords_cache = {}
    
    for row in cursor:
        store_zip = row[3]
        
        # Check if coordinates are already cached
        if store_zip not in store_coords_cache:
            store_location = nomi.query_postal_code(store_zip)
            store_coords_cache[store_zip] = (store_location.latitude, store_location.longitude)
        
        store_coords = store_coords_cache[store_zip]
        distance = geodesic(search_coords, store_coords).miles
        
        # If within the radius, add the store to the results
        if distance <= radius:
            nearby_stores.append(row)

    conn.close()
    return nearby_stores






@app.route("/", methods=["GET", "POST"])
def index():
    conn = sqlite3.connect("stores.db")
    cursor = conn.cursor()

    # Fetch distinct cities and states for dropdowns
    cursor.execute("SELECT DISTINCT city FROM stores ORDER BY city")
    cities = [row[0] for row in cursor.fetchall()]
    
    cursor.execute("SELECT DISTINCT state FROM stores ORDER BY state")
    states = [row[0] for row in cursor.fetchall()]

    # Initialize price variable to avoid UnboundLocalError
    price = ""

    results = None
    if request.method == "POST":
        zipcode = request.form["zipcode"]
        radius = int(request.form.get("radius", ""))
        city = request.form.get("city", "")
        state = request.form.get("state", "")
        price = request.form.get("price", "")  # Set price from the form data
        results = search_by_zip_upc(zipcode, radius, city, state, price)

    conn.close()
    
    return render_template("index.html", results=results, cities=cities, states=states, price=price)




@app.route('/export', methods=['GET'])
def export_data():
    results = session.get("search_results")

    if not results:
        return "No search results found. Please perform a search first.", 400

    # Create CSV in memory
    output = io.StringIO()
    writer = csv.writer(output)

    # Write header (matching table columns)
    writer.writerow(["Name", "Store Address", "Store Price", "Salesfloor", "Backroom"])

    # Write only the displayed data
    for row in results:
        writer.writerow([row[5], row[0], row[9], row[10], row[11]])

    output.seek(0)

    return Response(output, mimetype="text/csv", headers={"Content-Disposition": "attachment;filename=search_results.csv"})
@app.route("/get_cities")
def get_cities():
    state = request.args.get("state")

    if not state:
        return jsonify([]) 

    conn = sqlite3.connect("stores.db")
    cursor = conn.cursor()
    cursor.execute("SELECT DISTINCT city FROM stores WHERE state = ? ORDER BY city", (state,))
    cities = [row[0] for row in cursor.fetchall()]
    conn.close()

    return jsonify(cities)

