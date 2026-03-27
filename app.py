"""
AgentOps — Multi-Agent Workflow Dashboard
Flask application entry point.
"""
import os
import atexit
import requests as _requests
from flask import Flask, render_template, redirect, request, session, url_for
from models import db, User
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

    # GitHub OAuth config
    app.config["GITHUB_CLIENT_ID"] = os.environ.get("GITHUB_CLIENT_ID", "")
    app.config["GITHUB_CLIENT_SECRET"] = os.environ.get("GITHUB_CLIENT_SECRET", "")

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

    # ── Helper: ensure local-dev user exists when OAuth is not configured ──

    def _get_or_create_local_user():
        """Auto-create/use a 'local-dev' user when GITHUB_CLIENT_ID is not set."""
        user = User.query.filter_by(github_id=0).first()
        if not user:
            user = User(
                github_id=0,
                github_login="local-dev",
                github_avatar="",
                anthropic_api_key=app.config.get("ANTHROPIC_API_KEY", ""),
            )
            db.session.add(user)
            db.session.commit()
        return user

    # ── Page routes ──

    @app.route("/")
    def dashboard():
        if app.config["GITHUB_CLIENT_ID"]:
            # OAuth mode: require login
            uid = session.get("user_id")
            if not uid:
                return render_template("login.html")
            user = db.session.get(User, uid)
            if not user:
                session.clear()
                return render_template("login.html")
            return render_template("dashboard.html", user=user)
        else:
            # Local dev mode: auto-login
            user = _get_or_create_local_user()
            session["user_id"] = user.id
            return render_template("dashboard.html", user=user)

    @app.route("/focus")
    def focus():
        return render_template("focus.html")

    # ── GitHub OAuth routes ──

    @app.route("/login")
    def login():
        client_id = app.config["GITHUB_CLIENT_ID"]
        if not client_id:
            return redirect("/")
        callback_url = url_for("auth_callback", _external=True)
        return redirect(
            f"https://github.com/login/oauth/authorize"
            f"?client_id={client_id}"
            f"&scope=repo,read:user"
            f"&redirect_uri={callback_url}"
        )

    @app.route("/auth/callback")
    def auth_callback():
        code = request.args.get("code")
        if not code:
            return redirect("/")

        # Exchange code for access token
        resp = _requests.post(
            "https://github.com/login/oauth/access_token",
            json={
                "client_id": app.config["GITHUB_CLIENT_ID"],
                "client_secret": app.config["GITHUB_CLIENT_SECRET"],
                "code": code,
            },
            headers={"Accept": "application/json"},
            timeout=10,
        )
        token_data = resp.json()
        access_token = token_data.get("access_token")
        if not access_token:
            return redirect("/")

        # Get user profile from GitHub
        gh_user = _requests.get(
            "https://api.github.com/user",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        ).json()

        github_id = gh_user.get("id")
        if not github_id:
            return redirect("/")

        # Upsert user record
        user = User.query.filter_by(github_id=github_id).first()
        is_new = user is None
        if is_new:
            user = User(
                github_id=github_id,
                github_login=gh_user.get("login", ""),
                github_avatar=gh_user.get("avatar_url", ""),
                github_token=access_token,
            )
            db.session.add(user)
        else:
            user.github_login = gh_user.get("login", user.github_login)
            user.github_avatar = gh_user.get("avatar_url", user.github_avatar)
            user.github_token = access_token
        db.session.commit()

        session["user_id"] = user.id

        if is_new:
            return redirect("/onboarding")
        return redirect("/")

    @app.route("/onboarding")
    def onboarding():
        uid = session.get("user_id")
        if not uid:
            return redirect("/")
        user = db.session.get(User, uid)
        if not user:
            return redirect("/")
        return render_template("onboarding.html", user=user)

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect("/")

    return app


# Module-level app instance for gunicorn (e.g. gunicorn "app:create_app()")
# Also used when running directly via `python app.py` below.

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
