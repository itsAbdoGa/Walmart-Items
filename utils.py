import sys
import json
import time
from flask_socketio import SocketIO

socketio = None

def init_utils(sio):
    global socketio
    socketio = sio

def log_message(message):
    """Log messages to console and emit to socket"""
    print(f"Logging: {message}", flush=True)
    socketio.emit('log_update', message, namespace="/")
    socketio.sleep(0)

def get_size_kb(data):
    return round(sys.getsizeof(json.dumps(data)) / 1024, 2)