from flask import Blueprint, request, jsonify ,render_template,session
from core.processing import csv_queue, upload_cancel_event, processing_lock, csv_processing,process_entry
from utils import log_message
from config import Config
import os
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
    global csv_processing, upload_cancel_event
    
    with processing_lock:
        if csv_processing:
            return jsonify({"message": "ERROR: Another file is already being processed"}), 429
        csv_processing = True
        upload_cancel_event.clear()  # Reset cancellation flag

    try:
        if "file" not in request.files:
            return jsonify({"message": "No file provided"}), 400

        file = request.files["file"]
        if file.filename == "":
            return jsonify({"message": "No selected file"}), 400

        filepath = os.path.join(UPLOAD_FOLDER, file.filename)
        file.save(filepath)
        log_message(f"CSV uploaded. Processing... {filepath}")

        # Enqueue the CSV file for processing
        csv_queue.put(filepath)

        return jsonify({"message": f"CSV uploaded successfully. Will be processed shortly."}), 200

    except Exception as e:
        return jsonify({"message": f"Error uploading CSV: {e}"}), 500

    finally:
        with processing_lock:
            csv_processing = False
            upload_cancel_event.clear()

@bp.route('/cancel_upload', methods=['POST'])
def cancel_upload():
    """Endpoint to trigger cancellation"""
    global upload_cancel_event
    if csv_processing:
        upload_cancel_event.set()
        return jsonify({"message": "Cancellation signal sent"})
    return jsonify({"message": "No active upload to cancel"}), 404

@bp.route("/manual_input", methods=["POST"])
def manual_input():
    """Handle manual UPC & ZIP input"""
    data = request.json
    upc = data.get("upc")
    zip_code = data.get("zip")

    if not upc or not zip_code:
        return jsonify({"message": "Both UPC and ZIP are required"}), 400

    log_message(f"Processing manual entry: UPC {upc}, ZIP {zip_code}")
    success = process_entry(upc, zip_code)
    
    if success:
        return jsonify({"message": "Manual entry processed successfully"})
    else:
        return jsonify({"message": "Failed to process entry"}), 400