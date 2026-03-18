"""
services/repos.py — Multi-repo management service.

Handles adding, removing, detecting, and switching repositories.
All functions assume they're called inside a Flask app context.
"""
import os
import subprocess
from models import Repository, Setting, REPO_COLORS, db


# ── CRUD ─────────────────────────────────────────────────────────────────────

def add_repo(name: str, path: str, is_default: bool = False) -> Repository:
    """
    Register a repository. Auto-detects GitHub remote URL.
    If path already registered, returns the existing entry.
    """
    path = os.path.expanduser(os.path.abspath(path))

    existing = Repository.query.filter_by(path=path).first()
    if existing:
        return existing

    if not os.path.isdir(path):
        raise ValueError(f"Path does not exist: {path}")

    # Pick next unused color
    used = {r.color for r in Repository.query.all()}
    color = next((c for c in REPO_COLORS if c not in used), REPO_COLORS[0])

    github_remote = _detect_remote(path)

    is_first = Repository.query.count() == 0
    if is_first or is_default:
        # Unset any existing default
        Repository.query.filter_by(is_default=True).update({"is_default": False})
        is_default = True

    repo = Repository(
        name=name,
        path=path,
        color=color,
        github_remote=github_remote,
        is_default=is_default,
    )
    db.session.add(repo)
    db.session.commit()
    return repo


def update_repo(repo_id: str, name: str = None, path: str = None) -> Repository:
    repo = db.session.get(Repository, repo_id)
    if not repo:
        raise ValueError(f"Repository not found: {repo_id}")
    if name:
        repo.name = name
    if path:
        path = os.path.expanduser(os.path.abspath(path))
        repo.path = path
        repo.github_remote = _detect_remote(path)
    db.session.commit()
    return repo


def remove_repo(repo_id: str) -> bool:
    repo = db.session.get(Repository, repo_id)
    if not repo:
        return False
    was_default = repo.is_default
    db.session.delete(repo)
    db.session.commit()
    # If deleted repo was default, make the first remaining one default
    if was_default:
        first = Repository.query.first()
        if first:
            first.is_default = True
            db.session.commit()
    return True


def set_default(repo_id: str) -> Repository:
    Repository.query.filter_by(is_default=True).update({"is_default": False})
    repo = db.session.get(Repository, repo_id)
    if not repo:
        raise ValueError(f"Repository not found: {repo_id}")
    repo.is_default = True
    db.session.commit()
    return repo


def list_repos() -> list:
    return Repository.query.order_by(Repository.created_at).all()


def get_default() -> Repository | None:
    return Repository.get_default()


# ── Repo hint detection ───────────────────────────────────────────────────────

def detect_repo_hint(files_hint: list) -> str | None:
    """
    Given a list of relative file paths from a signal (e.g. ["src/auth.py"]),
    find which registered repo contains those files.
    Returns the repo.id of the best match, or None.
    """
    if not files_hint:
        return None
    repos = list_repos()
    if not repos:
        return None

    scores = {}
    for repo in repos:
        score = 0
        for rel_path in files_hint:
            full = os.path.join(repo.path, rel_path.lstrip("/"))
            if os.path.exists(full):
                score += 1
        scores[repo.id] = score

    best_id = max(scores, key=scores.get)
    return best_id if scores[best_id] > 0 else None


# ── Startup migration ─────────────────────────────────────────────────────────

def ensure_migrated(app, cli_repos: list = None):
    """
    Called at startup. Ensures:
    1. The repositories table exists (db.create_all handles new tables).
    2. Any repos passed via --repo CLI flag are registered.
    3. If no repos at all, migrates the legacy repo_path setting.

    cli_repos: list of (name, path) tuples from --repo flags, or just paths.
    """
    with app.app_context():
        db.create_all()

        # Register any repos passed from the command line
        if cli_repos:
            for repo_spec in cli_repos:
                if isinstance(repo_spec, tuple):
                    name, path = repo_spec
                else:
                    # Just a path — derive name from basename
                    path = repo_spec
                    name = os.path.basename(path.rstrip("/")) or "repo"
                try:
                    repo = add_repo(name=name, path=path)
                    print(f"📁 Repo registered: {repo.name} ({repo.path})")
                except Exception as e:
                    print(f"⚠️  Could not register repo {path}: {e}")

        # Migrate from legacy single-repo setting if no repos yet
        if Repository.query.count() == 0:
            legacy_path = Setting.get("repo_path", "")
            if not legacy_path:
                legacy_path = app.config.get("REPO_PATH", "")
            if legacy_path and os.path.isdir(legacy_path):
                name = os.path.basename(legacy_path.rstrip("/")) or "default"
                try:
                    repo = add_repo(name=name, path=legacy_path, is_default=True)
                    print(f"📁 Migrated legacy repo: {repo.name} ({repo.path})")
                except Exception as e:
                    print(f"⚠️  Could not migrate legacy repo: {e}")

        # Print all registered repos
        repos = list_repos()
        if repos:
            default = get_default()
            for r in repos:
                star = " ★" if r.id == (default.id if default else None) else ""
                print(f"   📂 {r.name}: {r.path}{star}")
        else:
            print("   ⚠️  No repositories configured. Use Settings → Repositories to add one.")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _detect_remote(path: str) -> str:
    """Auto-detect GitHub remote URL from git config."""
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=path, capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return ""


def repo_path_for_run(run) -> str:
    """
    Convenience: get the filesystem path for a Run or EnsembleRun.
    Handles missing repo_id gracefully (old runs before multi-repo).
    """
    if hasattr(run, "get_repo_path"):
        return run.get_repo_path()
    default = get_default()
    return default.path if default else Setting.get("repo_path", "")
