from flask import Flask, request, jsonify
from robot_common import run_shell

app = Flask(__name__)


@app.route("/run", methods=["POST"])
def handle_run():
    try:
        data = request.get_json(silent=True)
        if not data or "cmd" not in data:
            return jsonify({"output": "missing required field: cmd", "success": False}), 400

        cmd = data["cmd"]
        output = run_shell(cmd)
        return jsonify({"output": output, "success": True})
    except Exception as e:
        return jsonify({"output": str(e), "success": False}), 500


def main():
    app.run(host="0.0.0.0", port=8080)