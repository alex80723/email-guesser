import os
import json
from flask import Flask, render_template, request, jsonify
from email_guesser import run as run_pipeline

app = Flask(__name__)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/run", methods=["POST"])
def run():
    data = request.get_json()
    name = (data.get("name") or "").strip()
    url  = (data.get("url")  or "").strip()

    if not name or not url:
        return jsonify({"error": "Name and URL are required"}), 400

    try:
        result = run_pipeline(name, url, verbose=False)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
