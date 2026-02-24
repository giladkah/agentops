"""
AgentOps — Multi-Agent Workflow Dashboard
Flask application entry point.
"""
import os
from flask import Flask, render_template, send_from_directory
from models import db
from routes.api import api, init_services
from seed import seed_all


def create_app(repo_path: str = None):
    app = Flask(__name__, static_folder="static", template_folder="templates")

    # Config
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///agentops.db"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "agentops-dev-key")
    app.config["REPO_PATH"] = repo_path or os.environ.get("AGENTOPS_REPO", os.getcwd())

    # Init DB
    db.init_app(app)

    with app.app_context():
        db.create_all()
        seed_all()

    # Init services
    with app.app_context():
        init_services(app.config["REPO_PATH"], app=app)

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
    parser.add_argument("--debug", action="store_true", help="Debug mode")
    args = parser.parse_args()

    app = create_app(repo_path=args.repo)
    print(f"\n🤖 AgentOps running at http://localhost:{args.port}")
    print(f"📂 Repository: {args.repo}\n")
    app.run(host="0.0.0.0", port=args.port, debug=args.debug)
