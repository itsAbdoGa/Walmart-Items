from flask import Blueprint, request, jsonify ,render_template,session
from core.processing import csv_processing,priority_queue,start_processing_worker,Priority,processing_worker
from utils import log_message
from config import Config
import os
from gevent import queue



UPLOAD_FOLDER = Config.UPLOAD_FOLDER
bp = Blueprint('admin', __name__)

@bp.route("/adminpanel")
def admin():
    """Admin panel route"""
    return render_template("admin.html")

@bp.route('/clear_logs', methods=['POST'])
def clear_logs():
    if 'logs' in session:
        session.pop('logs')  # Remove only logs (keeps other session data)
        session.modified = True  # Force save
        return jsonify({"message": "Logs cleared successfully"})
    return jsonify({"message": "No logs to clear"}), 404

@bp.route("/upload_csv", methods=["POST"])
def upload_csv():
    """Handle CSV file upload and process each entry"""
    try:
        if "file" not in request.files:
            return jsonify({"message": "No file provided"}), 400

        file = request.files["file"]
        if file.filename == "":
            return jsonify({"message": "No selected file"}), 400

        filepath = os.path.join(UPLOAD_FOLDER, file.filename)
        file.save(filepath)
        log_message(f"CSV uploaded. Adding to processing queue... {filepath}")

        # Add CSV file to priority queue with LOW priority
        priority_queue.put((Priority.LOW, "csv", filepath))
        
        # Start worker if not running
        start_processing_worker()

        return jsonify({
            "message": f"CSV uploaded successfully. Added to processing queue.",
            "queue_position": priority_queue.qsize()
        }), 200

    except Exception as e:
        log_message(f"Error uploading CSV: {e}")
        return jsonify({"message": f"Error uploading CSV: {e}"}), 500

@bp.route("/manual_input", methods=["POST"])
def manual_input():
    """Handle manual UPC & ZIP input with HIGH priority"""
    data = request.json
    upc = data.get("upc")
    zip_code = data.get("zip")

    if not upc or not zip_code:
        return jsonify({"message": "Both UPC and ZIP are required"}), 400

    log_message(f"Adding HIGH PRIORITY manual entry to queue: UPC {upc}, ZIP {zip_code}")
    
    # Add manual input to priority queue with HIGH priority
    priority_queue.put((Priority.HIGH, "manual", (str(upc), str(zip_code))))
    
    # Start worker if not running
    start_processing_worker()
    
    return jsonify({
        "message": "Manual entry added to priority queue and will be processed immediately",
        "priority": "HIGH"
    })

@bp.route("/queue_status", methods=["GET"])
def queue_status():
    """Get current queue status"""
    return jsonify({
        "queue_size": priority_queue.qsize(),
        "csv_processing": csv_processing,
        "worker_active": processing_worker is not None and not processing_worker.dead
    })

@bp.route("/clear_queue", methods=["POST"])
def clear_queue():
    """Clear all pending items from queue"""
    count = 0
    try:
        while True:
            priority_queue.get_nowait()
            count += 1
    except queue.Empty:
        pass
    
    log_message(f"Cleared {count} items from queue")
    return jsonify({"message": f"Cleared {count} items from queue"})
