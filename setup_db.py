import sqlite3
import json
import requests
import csv

def csv_to_json(csv_file):
    """Converts CSV file to JSON format while skipping rows with only 'zip' populated."""
    with open(csv_file, mode='r', encoding='utf-8') as file:
        reader = csv.DictReader(file)
        data = [
            row for row in reader 
            if any(value for key, value in row.items() if key.lower() != "zip")  # Skip if only 'zip' has a value
        ]
    return data

hits = csv_to_json("test.csv")
url = "http://5.75.246.251:9099/stock/store"

for combos in hits:
    upc = combos.get("UPC")
    zip_code = combos.get("Zip")

    # Skip if UPC is missing
    if not upc:
        print("Skipping entry with missing UPC")
        continue

    payload = json.dumps({
        "storeName": "walmart",
        "upc": upc,
        "zip": zip_code,
    })

    headers = {'Content-Type': 'application/json'}
    response = requests.request("POST", url, headers=headers, data=payload)

    print(f"Fetched request for: {upc} {zip_code}")

    try:
        data = response.json()
    except json.JSONDecodeError:
        print(f"Skipping {upc} due to invalid JSON response")
        continue

    # Check if expected keys exist
    if "stores" not in data or "itemDetails" not in data:
        print(f"Skipping UPC {upc} due to missing data")
        continue  

    conn = sqlite3.connect("stores.db")
    cursor = conn.cursor()

    # Create tables
    cursor.executescript("""
        CREATE TABLE IF NOT EXISTS stores (
            id INTEGER PRIMARY KEY,
            address TEXT,
            city TEXT,
            state TEXT,
            zipcode TEXT,
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

    # Load JSON
    data = response.json()

    # Insert item
    item_name = data["itemDetails"]["name"]
    item_msrp = data["itemDetails"]["msrp"]
    item_image = data["itemDetails"]["imageUrl"]
    item_url = data["itemDetails"]["url"]
    item_upc = combos.get("UPC")

    cursor.execute("""
    INSERT INTO items (name, upc, msrp, image_url, item_url) 
    VALUES (?, ?, ?, ?, ?) 
    ON CONFLICT(upc) DO UPDATE 
    SET name = excluded.name, 
        msrp = excluded.msrp, 
        image_url = excluded.image_url, 
        item_url = excluded.item_url
""", (item_name, item_upc, item_msrp, item_image, item_url))
    conn.commit()

    # Get item_id
    cursor.execute("SELECT id FROM items WHERE upc = ?", (item_upc,))
    item_id = cursor.fetchone()[0]

    # Insert stores and store_items
    for store in data["stores"]:
        store_id = int(store["id"])
        cursor.execute("INSERT OR IGNORE INTO stores (id, address, city, state, zipcode, store_url) VALUES (?, ?, ?, ?, ?, ?)", 
                      (store_id, store["address"], store["city"], store["state"], store["zip"], store["storeUrl"]))
        
        cursor.execute("""
    INSERT INTO store_items (store_id, item_id, price, salesfloor, backroom)
    VALUES (?, ?, ?, ?, ?)
    ON CONFLICT(store_id, item_id) DO UPDATE
    SET price = excluded.price,
        salesfloor = excluded.salesfloor,
        backroom = excluded.backroom
""", (store_id, item_id, store["price"], store.get("salesFloor", 0), store.get("backRoom", 0)))


    conn.commit()
    conn.close()
