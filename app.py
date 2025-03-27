from flask import Flask, request, render_template, Response, session , jsonify
import sqlite3
import csv
import io
import pgeocode
from geopy.distance import geodesic
app = Flask(__name__)
app.secret_key = "admin"  


def search_by_zip_upc(zipcode, radius, city="", state="",price=""):
    conn = sqlite3.connect("stores.db")
    cursor = conn.cursor()
    
    # Get ZIP code coordinates
    nomi = pgeocode.Nominatim("US")
    search_location = nomi.query_postal_code(zipcode)
    search_coords = (search_location.latitude, search_location.longitude)
    
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
    results = cursor.fetchall()
    

    nearby_stores = []
    for row in results:
        store_location = nomi.query_postal_code(row[3])  # Zipcode
        store_coords = (store_location.latitude, store_location.longitude)
        
        distance = geodesic(search_coords, store_coords).miles
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
    
    results = None
    if request.method == "POST":
        zipcode = request.form["zipcode"]
        radius = int(request.form.get("radius", ""))
        city = request.form.get("city", "")
        state = request.form.get("state", "")
        price = request.form.get("price","")
        results = search_by_zip_upc(zipcode, radius, city, state, price)

    conn.close()
    
    return render_template("index.html", results=results, cities=cities, states=states,price=price)




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


