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

    return app


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="AgentOps — Multi-Agent Dashboard")
    parser.add_argument("--repo", type=str, default=os.getcwd(), help="Path to git repository")
    parser.add_argument("--port", type=int, default=5050, help="Port to run on")
    parser.add_argument("--api-key", type=str, default=None, help="Anthropic API key (or set ANTHROPIC_API_KEY)")
    parser.add_argument("--debug", action="store_true", help="Debug mode")
    args = parser.parse_args()

    app = create_app(repo_path=args.repo, api_key=args.api_key)

    # Track graceful shutdown
    atexit.register(telemetry.track_app_stopped)

    api_status = "✅ API key configured" if (args.api_key or os.environ.get("ANTHROPIC_API_KEY")) else "⚠️ No API key — set ANTHROPIC_API_KEY"
    telemetry_status = "✅ Telemetry enabled" if telemetry.is_enabled() else "⚪ Telemetry disabled (set AGENTOPS_TELEMETRY=true to enable)"
    print(f"\n🤖 AgentOps running at http://localhost:{args.port}")
    print(f"📂 Repository: {args.repo}")
    print(f"🔑 {api_status}")
    print(f"🔌 Mode: Anthropic API + Tool Use + Streaming")
    print(f"📊 {telemetry_status}\n")
    app.run(host="0.0.0.0", port=args.port, debug=args.debug)
