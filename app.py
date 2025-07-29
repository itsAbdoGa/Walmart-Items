from gevent import monkey
monkey.patch_all()

from flask import Flask
from flask_socketio import SocketIO
from config import Config
from core.database import init_databases, alter_max_prices,create_indexes
from core.processing import start_processing_worker
from utils import init_utils
from routes import main, admin, max_prices

# Initialize app
Config.init_app()
app = Flask(__name__)
app.secret_key = Config.SECRET_KEY

# Initialize SocketIO
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="gevent")
init_utils(socketio)

# Register blueprints
app.register_blueprint(main.bp)
app.register_blueprint(admin.bp)
app.register_blueprint(max_prices.bp)

init_databases()
alter_max_prices()
create_indexes()


# Start processing worker
start_processing_worker()
