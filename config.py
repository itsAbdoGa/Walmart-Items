import os
from collections import deque
import threading
from gevent.queue import Queue

class Config:
    SECRET_KEY = "admin"
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    UPLOAD_FOLDER = os.path.join(BASE_DIR, "uploads")
    UPCZIP_DB = "/database/upczip.db"
    DATABASE = "/database/stores.db"
    API_URL = "http://5.75.246.251:9099/stock/store"
    MAX_LOGS = 10
    MAX_RESULTS_IN_SESSION = 10
    


class qmanager:
    
    manual_queue = Queue()
    csv_queue = Queue()
