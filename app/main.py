"""Flask API + static UI.   Run:  python -m app.main"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

from .errors import AssistantError
from .service import EXAMPLES, Assistant

ROOT = Path(__file__).resolve().parent.parent
log = logging.getLogger("bi_assistant")


def create_app(csv_path=None, db_path=None, assistant: Assistant | None = None) -> Flask:
    app = Flask(__name__, static_folder=str(Path(__file__).parent / "static"), static_url_path="/static")
    assistant = assistant or Assistant(csv_path or os.environ.get("BI_CSV", ROOT / "data" / "sales.csv"),
                                       db_path or os.environ.get("BI_DB", ROOT / "data" / "sales.db"))
    app.config["assistant"] = assistant

    def error_response(exc: AssistantError):
        body = {"ok": False, "error": {"code": exc.code, "message": exc.message, "hints": exc.hints}}
        return jsonify(body), exc.status

    @app.get("/")
    def index():
        return send_from_directory(app.static_folder, "index.html")

    @app.get("/api/health")
    def health():
        return jsonify({"ok": True, "load_report": assistant.load_report,
                        "parser_mode": assistant.parser_mode,
                        "llm_configured": bool(os.environ.get("ANTHROPIC_API_KEY"))})

    @app.get("/api/examples")
    def examples():
        return jsonify({"examples": EXAMPLES})

    @app.get("/api/data-quality")
    def data_quality():
        return jsonify(assistant.data_quality())

    @app.post("/api/ask")
    def ask():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or "question" not in payload:
            return error_response(AssistantError(
                "invalid_request", 'Send JSON like {"question": "What is the total revenue?"}', 400))
        try:
            return jsonify(assistant.ask(payload["question"]))
        except AssistantError as exc:
            return error_response(exc)

    @app.errorhandler(404)
    def not_found(_):
        return jsonify({"ok": False, "error": {"code": "not_found", "message": "No such endpoint."}}), 404

    @app.errorhandler(405)
    def bad_method(_):
        return jsonify({"ok": False, "error": {"code": "method_not_allowed", "message": "Wrong HTTP method."}}), 405

    @app.errorhandler(Exception)
    def unexpected(exc):
        log.exception("Unhandled error")         # details stay in the log, not in the response
        return jsonify({"ok": False, "error": {"code": "internal_error",
                                               "message": "Something went wrong on our side."}}), 500

    return app


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    create_app().run(host="127.0.0.1", port=int(os.environ.get("PORT", 5000)))
