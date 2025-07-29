import sqlite3
from config import Config


DATABASE = Config.DATABASE
UPCZIP_DB = Config.UPCZIP_DB

def alter_max_prices():
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()

    # Get existing column names from the table
    cursor.execute("PRAGMA table_info(upc_max_prices)")
    existing_columns = [col[1] for col in cursor.fetchall()]

    # Define the columns you want to add (name and type)
    new_columns = {
        "net": "REAL",          # change to the correct type if needed
        "department": "TEXT",    # change to the correct type if needed
        "productid" : "TEXT"
    }

    # Loop through and add only the missing ones
    for column_name, column_type in new_columns.items():
        if column_name not in existing_columns:
            cursor.execute(f"ALTER TABLE upc_max_prices ADD COLUMN {column_name} {column_type}")
            print(f"Added column: {column_name}")
        

    conn.commit()
    conn.close()
    
    
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
        
        
def create_indexes():
    """Create indexes to optimize query performance"""
    with get_db_connection(DATABASE) as conn:
        cursor = conn.cursor()
        
        # Indexes for the main search query
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_items_upc ON items(upc)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_stores_motherzip ON stores(motherZip)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_stores_city ON stores(city)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_stores_state ON stores(state)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_store_items_price ON store_items(price)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_store_items_store_id ON store_items(store_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_store_items_item_id ON store_items(item_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_upc_max_prices_upc ON upc_max_prices(upc)")
        
        # Composite indexes for common query patterns
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_stores_city_state ON stores(city, state)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_store_items_price_inventory ON store_items(price, salesfloor, backroom)")
        
        
        conn.commit()
        print("Database indexes created successfully")