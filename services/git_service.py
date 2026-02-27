"""
Git Service — Worktree management, branching, merging, diffing.
Wraps git CLI commands via subprocess.
"""
import os
import subprocess
import shutil
from typing import Optional


class GitService:
    def __init__(self, repo_path: str):
        self.repo_path = os.path.expanduser(repo_path)
        self.worktree_base = os.path.join(self.repo_path, ".wt")

    def _run(self, args: list[str], cwd: Optional[str] = None) -> tuple[int, str, str]:
        """Run a git command, return (returncode, stdout, stderr)."""
        result = subprocess.run(
            ["git"] + args,
            cwd=cwd or self.repo_path,
            capture_output=True,
            text=True,
            timeout=60,
        )
        return result.returncode, result.stdout.strip(), result.stderr.strip()

    def ensure_worktree_base(self):
        """Create .wt/ directory if it doesn't exist."""
        os.makedirs(self.worktree_base, exist_ok=True)
        gitignore = os.path.join(self.repo_path, ".gitignore")
        if os.path.exists(gitignore):
            with open(gitignore, "r") as f:
                content = f.read()
            if ".wt/" not in content:
                with open(gitignore, "a") as f:
                    f.write("\n.wt/\n")

    def create_worktree(self, name: str, base_branch: str = "main") -> tuple[bool, str]:
        """Create a git worktree. Returns (success, path_or_error)."""
        self.ensure_worktree_base()
        wt_path = os.path.join(self.worktree_base, name)

        if os.path.exists(wt_path):
            return True, wt_path  # Already exists

        code, out, err = self._run(["worktree", "add", wt_path, "-b", f"agentops/{name}", base_branch])
        if code != 0:
            # Branch might already exist, try without -b
            code, out, err = self._run(["worktree", "add", wt_path, f"agentops/{name}"])
            if code != 0:
                return False, err

        return True, wt_path

    def remove_worktree(self, name: str) -> tuple[bool, str]:
        """Remove a git worktree."""
        wt_path = os.path.join(self.worktree_base, name)
        if not os.path.exists(wt_path):
            return True, "Already removed"

        code, out, err = self._run(["worktree", "remove", wt_path, "--force"])
        if code != 0:
            # Force cleanup
            shutil.rmtree(wt_path, ignore_errors=True)
            self._run(["worktree", "prune"])

        return True, "Removed"

    def get_diff(self, branch_name: str, base: str = "main") -> str:
        """Get diff between a branch and base."""
        code, out, err = self._run(["diff", f"{base}...{branch_name}", "--stat"])
        if code != 0:
            return f"Error getting diff: {err}"
        return out

    def get_diff_full(self, branch_name: str, base: str = "main") -> str:
        """Get full diff between a branch and base."""
        code, out, err = self._run(["diff", f"{base}...{branch_name}"])
        if code != 0:
            return f"Error: {err}"
        return out

    def merge_branch(self, branch_name: str, target: str = "main") -> tuple[bool, str]:
        """Merge a branch into target."""
        code, out, err = self._run(["checkout", target])
        if code != 0:
            return False, f"Failed to checkout {target}: {err}"

        code, out, err = self._run(["merge", branch_name, "--no-edit"])
        if code != 0:
            # Abort the failed merge
            self._run(["merge", "--abort"])
            return False, f"Merge conflict: {err}"

        return True, "Merged successfully"

    def commit_worktree(self, name: str, message: str) -> tuple[bool, str]:
        """Stage and commit all changes in a worktree."""
        wt_path = os.path.join(self.worktree_base, name)
        code, out, err = self._run(["add", "-A"], cwd=wt_path)
        if code != 0:
            return False, err

        code, out, err = self._run(["commit", "-m", message, "--allow-empty"], cwd=wt_path)
        if code != 0:
            return False, err

        return True, out

    def list_worktrees(self) -> list[dict]:
        """List all worktrees."""
        code, out, err = self._run(["worktree", "list", "--porcelain"])
        if code != 0:
            return []

        worktrees = []
        current = {}
        for line in out.split("\n"):
            if line.startswith("worktree "):
                if current:
                    worktrees.append(current)
                current = {"path": line.split(" ", 1)[1]}
            elif line.startswith("branch "):
                current["branch"] = line.split(" ", 1)[1].replace("refs/heads/", "")
        if current:
            worktrees.append(current)

        return worktrees

    def cleanup_all_worktrees(self) -> int:
        """Remove all agentops worktrees. Returns count removed."""
        count = 0
        if os.path.exists(self.worktree_base):
            for name in os.listdir(self.worktree_base):
                self.remove_worktree(name)
                count += 1
        self._run(["worktree", "prune"])
        return count

    def get_current_branch(self) -> str:
        code, out, err = self._run(["branch", "--show-current"])
        return out if code == 0 else "unknown"

    def push_branch(self, branch_name: str, remote: str = "origin") -> tuple[bool, str]:
        """Push a branch to remote."""
        code, out, err = self._run(["push", remote, branch_name, "--set-upstream"])
        if code != 0:
            return False, err
        return True, out

    def create_pr(self, branch: str, title: str, body: str, base: str = "main") -> tuple[bool, str]:
        """Create a pull request using GitHub CLI (gh)."""
        import subprocess as sp
        try:
            result = sp.run(
                ["gh", "pr", "create",
                 "--title", title,
                 "--body", body,
                 "--base", base,
                 "--head", branch],
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode != 0:
                return False, result.stderr.strip()
            # gh pr create outputs the PR URL
            pr_url = result.stdout.strip()
            return True, pr_url
        except FileNotFoundError:
            return False, "GitHub CLI (gh) not installed. Install with: brew install gh"
        except Exception as e:
            return False, str(e)

    def merge_into_worktree(self, target_wt_name: str, source_branch: str, auto_resolve: bool = False) -> tuple[bool, str]:
        """Merge a source branch into a target worktree. Returns (success, message).
        If auto_resolve=True, uses -X theirs to auto-resolve conflicts (favors incoming changes).
        """
        wt_path = os.path.join(self.worktree_base, target_wt_name)
        if not os.path.exists(wt_path):
            return False, f"Target worktree {target_wt_name} not found"

        merge_args = ["merge", source_branch, "--no-edit"]
        if auto_resolve:
            merge_args = ["merge", source_branch, "--no-edit", "-X", "theirs"]

        code, out, err = self._run(merge_args, cwd=wt_path)
        if code != 0:
            if not auto_resolve:
                # Try again with auto-resolve as fallback
                self._run(["merge", "--abort"], cwd=wt_path)
                code2, out2, err2 = self._run(
                    ["merge", source_branch, "--no-edit", "-X", "theirs"], cwd=wt_path
                )
                if code2 == 0:
                    return True, "Merged (auto-resolved conflicts)"
                self._run(["merge", "--abort"], cwd=wt_path)
            else:
                self._run(["merge", "--abort"], cwd=wt_path)
            return False, f"Conflict merging {source_branch}: {err}"
        return True, "Merged"

    def merge_branches_into_worktree(self, target_wt_name: str, source_branches: list[str], auto_resolve: bool = False) -> tuple[bool, list[str], list[str]]:
        """
        Merge multiple branches into a target worktree sequentially.
        Returns (all_success, merged_branches, failed_branches).
        """
        merged = []
        failed = []
        for branch in source_branches:
            ok, msg = self.merge_into_worktree(target_wt_name, branch, auto_resolve=auto_resolve)
            if ok:
                merged.append(branch)
            else:
                failed.append(f"{branch}: {msg}")
        return len(failed) == 0, merged, failed

    def create_synthesis_worktree(self, run_id_short: str, base_branch: str) -> tuple[bool, str]:
        """Create a synthesis worktree for merging parallel reviewer branches."""
        name = f"run-{run_id_short}-synthesis"
        return self.create_worktree(name, base_branch)

    def run_tests(self, worktree_name: str) -> tuple[bool, str]:
        """Run pytest in a worktree."""
        wt_path = os.path.join(self.worktree_base, worktree_name)
        result = subprocess.run(
            ["python", "-m", "pytest", "--tb=short", "-q"],
            cwd=wt_path,
            capture_output=True,
            text=True,
            timeout=300,
        )
        passed = result.returncode == 0
        output = result.stdout + result.stderr
        return passed, output
