"""
Inception-of-Wisdom (IoW) - Demo Target Service
This is the containerized target application that the IoW agent observes,
diagnoses, and automatically repairs when failures occur.
"""

import os
import sys
import logging
from flask import Flask, jsonify

# Configure standard logging to stderr/stdout for the Observer to stream
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] in %(module)s: %(message)s",
    stream=sys.stdout
)
logger = logging.getLogger("demo_app")

app = Flask(__name__)

SERVICE_NAME = "demo-target"
SERVICE_VERSION = "1.0.0"


def calculate_summary(data: list) -> dict:
    """Helper function performing basic business logic.
    Provides a clear AST function chunk for the Analyst to index.
    """
    if not data:
        return {"count": 0, "total": 0, "average": 0}
    total = sum(data)
    count = len(data)
    average = total / count
    return {"count": count, "total": total, "average": average}


@app.route("/", methods=["GET"])
def index():
    """Target public root route for HTTP probes."""
    logger.info("Handling request to root '/'")
    return jsonify({
        "service": SERVICE_NAME,
        "version": SERVICE_VERSION,
        "status": "operational",
        "message": "Demo target service is running smoothly."
    }), 200


@app.route("/healthz", methods=["GET"])
def healthcheck():
    """Healthcheck endpoint used by the Observer HTTP probe."""
    logger.info("Healthcheck probe passed.")
    return jsonify({
        "status": "healthy",
        "service": SERVICE_NAME
    }), 200


@app.route("/api/data", methods=["GET"])
def get_data():
    """Sample data endpoint demonstrating service functionality."""
    sample_values = [10, 20, 30, 40, 50]
    metrics = calculate_summary(sample_values)
    return jsonify({
        "data": sample_values,
        "metrics": metrics
    }), 200


@app.route("/api/crash", methods=["POST"])
def trigger_crash():
    """Manual crash trigger for testing and evaluation defense.
    Immediately raises a runtime exception that logs a full traceback.
    """
    logger.error("CRITICAL: Intentional crash triggered via /api/crash!")
    raise RuntimeError("Intentional target service crash for IoW verification")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    logger.info(f"Starting {SERVICE_NAME} v{SERVICE_VERSION} on port {port}")
    app.run(host="0.0.0.0", port=port)
