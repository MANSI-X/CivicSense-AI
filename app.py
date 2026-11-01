"""Compatibility entry point. Run the project root app.py instead."""
from app import app

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=app.config["DEBUG"])
