"""
AgentOps — Multi-Agent Workflow Dashboard
Flask application entry point.
"""
import os
import signal
import atexit
from flask import Flask, render_template, send_from_directory
from models import db
from routes.api import api, init_services
import services.repos as repos_svc
from seed import seed_all
import services.telemetry as telemetry


def create_app(repo_path: str = None, api_key: str = None):
    app = Flask(__name__, static_folder="static", template_folder="templates")

    # Config
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///agentops.db"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "agentops-dev-key")
    app.config["REPO_PATH"] = repo_path or os.environ.get("AGENTOPS_REPO", os.getcwd())
    app.config["ANTHROPIC_API_KEY"] = api_key or os.environ.get("ANTHROPIC_API_KEY", "")

    # Init DB
    db.init_app(app)

    with app.app_context():
        db.create_all()
        # Migrate: add chat_messages column to signal_clusters if missing (SQLite)
        from sqlalchemy import text
        with db.engine.connect() as conn:
            try:
                conn.execute(text("ALTER TABLE signal_clusters ADD COLUMN chat_messages TEXT DEFAULT '[]'"))
                conn.commit()
            except Exception:
                pass  # Column already exists
        seed_all()

    # Init services
    with app.app_context():
        init_services(app.config["REPO_PATH"], app=app, api_key=app.config["ANTHROPIC_API_KEY"])

    # Init telemetry (after DB is ready so it can read the opt-out setting)
    with app.app_context():
        telemetry.init(app=app)

    # Register API routes
    app.register_blueprint(api)

    # Page routes
    @app.route("/")
    def dashboard():
        return render_template("dashboard.html")

    @app.route("/focus")
    def focus():
        return render_template("focus.html")

    return app


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="AgentOps — Multi-Agent Dashboard")
    parser.add_argument("--repo", action="append", dest="repos", metavar="PATH", help="Register a repo path (repeatable)")
    parser.add_argument("--port", type=int, default=5050, help="Port to run on")
    parser.add_argument("--api-key", type=str, default=None, help="Anthropic API key (or set ANTHROPIC_API_KEY)")
    parser.add_argument("--debug", action="store_true", help="Debug mode")
    args = parser.parse_args()

    app = create_app(repo_path=args.repos[0] if args.repos else None, api_key=args.api_key)

    # Track graceful shutdown
    atexit.register(telemetry.track_app_stopped)

    api_status = "✅ API key configured" if (args.api_key or os.environ.get("ANTHROPIC_API_KEY")) else "⚠️ No API key — set ANTHROPIC_API_KEY"
    telemetry_status = "✅ Telemetry enabled" if telemetry.is_enabled() else "⚪ Telemetry disabled (set AGENTOPS_TELEMETRY=true to enable)"
    print(f"\n🤖 AgentOps running at http://localhost:{args.port}")
    with app.app_context():
        _def_repo = repos_svc.get_default()
        if _def_repo:
            print(f"📂 Active repo: {_def_repo.name} ({_def_repo.path})")
    print(f"🔑 {api_status}")
    print(f"🔌 Mode: Anthropic API + Tool Use + Streaming")
    print(f"📊 {telemetry_status}\n")
    app.run(host="0.0.0.0", port=args.port, debug=args.debug)
