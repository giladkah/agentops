#!/usr/bin/env python3
"""List all stories in a Shortcut epic by name. Useful for triage sessions."""
import json
import os
import sys
from urllib.request import urlopen, Request
from urllib.error import HTTPError

BASE_URL = "https://api.app.shortcut.com/api/v3"
TOKEN = os.environ.get("SHORTCUT_API_TOKEN", "")


def sc_get(path):
    req = Request(f"{BASE_URL}{path}", headers={
        "Content-Type": "application/json",
        "Shortcut-Token": TOKEN,
    })
    with urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def sc_post(path, body):
    req = Request(f"{BASE_URL}{path}", headers={
        "Content-Type": "application/json",
        "Shortcut-Token": TOKEN,
    }, data=json.dumps(body).encode(), method="POST")
    with urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def find_epic(name_query):
    """Find an epic by partial name match."""
    epics = sc_get("/epics")
    matches = [e for e in epics if name_query.lower() in e["name"].lower()]
    return matches


def list_stories_in_epic(epic_id):
    """List all stories in an epic via search."""
    stories = sc_get(f"/epics/{epic_id}/stories")
    return stories


def main():
    if not TOKEN:
        print("ERROR: Set SHORTCUT_API_TOKEN environment variable")
        sys.exit(1)

    epic_name = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "bug pile"
    print(f"Searching for epic: \"{epic_name}\"...\n")

    epics = find_epic(epic_name)
    if not epics:
        print(f"No epic found matching \"{epic_name}\"")
        print("\nAll epics:")
        for e in sc_get("/epics"):
            print(f"  - {e['name']} (id: {e['id']}, stories: {e.get('stats', {}).get('num_stories', '?')})")
        sys.exit(1)

    epic = epics[0]
    if len(epics) > 1:
        print(f"Multiple matches, using first: \"{epic['name']}\"")

    print(f"Epic: {epic['name']}")
    print(f"URL:  {epic.get('app_url', 'N/A')}")
    print(f"State: {epic.get('state', 'N/A')}")
    print()

    stories = list_stories_in_epic(epic["id"])
    if not stories:
        print("No stories in this epic.")
        return

    # Group by story type
    bugs = [s for s in stories if s.get("story_type") == "bug"]
    features = [s for s in stories if s.get("story_type") == "feature"]
    chores = [s for s in stories if s.get("story_type") == "chore"]

    # Fetch workflow states for human-readable names
    workflows = sc_get("/workflows")
    state_map = {}
    for wf in workflows:
        for st in wf.get("states", []):
            state_map[st["id"]] = st["name"]

    def print_stories(label, items):
        if not items:
            return
        print(f"── {label} ({len(items)}) ──")
        for s in sorted(items, key=lambda x: x.get("position", 0)):
            state = state_map.get(s.get("workflow_state_id"), "?")
            labels = ", ".join(l["name"] for l in s.get("labels", []))
            estimate = s.get("estimate")
            est_str = f" [{estimate}pt]" if estimate else ""
            owners = len(s.get("owner_ids", []))
            owner_str = f" ({owners} owner{'s' if owners != 1 else ''})" if owners else " (unassigned)"
            print(f"  [{state:.<20s}] {s['name']}")
            if labels:
                print(f"  {'':.<22s} labels: {labels}")
            print(f"  {'':.<22s} id: {s['id']}{est_str}{owner_str}")
            print(f"  {'':.<22s} {s.get('app_url', '')}")
            print()

    print(f"Total: {len(stories)} stories\n")
    print_stories("Bugs", bugs)
    print_stories("Features", features)
    print_stories("Chores", chores)

    # Also output as JSON for piping to other tools
    if "--json" in sys.argv:
        print("\n── JSON ──")
        out = []
        for s in stories:
            out.append({
                "id": s["id"],
                "name": s["name"],
                "type": s.get("story_type"),
                "state": state_map.get(s.get("workflow_state_id"), "?"),
                "labels": [l["name"] for l in s.get("labels", [])],
                "estimate": s.get("estimate"),
                "url": s.get("app_url", ""),
                "description": (s.get("description") or "")[:300],
            })
        print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
