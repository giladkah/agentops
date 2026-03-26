#!/usr/bin/env python3
"""Dump a Shortcut epic's stories to JSON + print summary."""
import json, os
from urllib.request import urlopen, Request
from collections import Counter

BASE = "https://api.app.shortcut.com/api/v3"
TOKEN = os.environ.get("SHORTCUT_API_TOKEN", "")

def get(path):
    req = Request(f"{BASE}{path}", headers={"Content-Type": "application/json", "Shortcut-Token": TOKEN})
    with urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode())

# Find epic
epics = get("/epics")
epic = next((e for e in epics if "bug pile" in e["name"].lower()), None)
if not epic:
    print("Epic not found")
    exit(1)

# Get stories + workflow states
stories = get(f"/epics/{epic['id']}/stories")
workflows = get("/workflows")
state_map = {}
for wf in workflows:
    for st in wf.get("states", []):
        state_map[st["id"]] = st["name"]

# Build clean output
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
        "description": (s.get("description") or "")[:500],
    })

with open("/tmp/bug_pile.json", "w") as f:
    json.dump(out, f, indent=2)

states = Counter(b["state"] for b in out)
types = Counter(b["type"] for b in out)

print(f"Epic: {epic['name']}")
print(f"Total: {len(out)} stories\n")

print("By type:")
for t, n in types.most_common():
    print(f"  {t}: {n}")

print("\nBy state:")
for s, n in states.most_common():
    print(f"  {s}: {n}")

print()
active = [b for b in out if b["state"] not in ("Backlog", "Done", "Completed")]
print(f"Active (not backlog/done): {len(active)}")
for b in active[:25]:
    tag = ", ".join(b["labels"])
    tag_str = f" [{tag}]" if tag else ""
    est = f" ({b['estimate']}pt)" if b["estimate"] else ""
    print(f"  [{b['state']}] {b['name']}{tag_str}{est}")

print(f"\nFull JSON saved to /tmp/bug_pile.json")
