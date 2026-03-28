import json
import subprocess
from pathlib import Path

TRACKER_PATH = Path(__file__).parent.parent / "state" / "tracker.json"

def load_state() -> dict:
    """Load tracker.json, resetting daily counters if date has changed."""
    import datetime
    state = json.loads(TRACKER_PATH.read_text())
    today = datetime.date.today().isoformat()
    if state["daily"]["date"] != today:
        state["daily"] = {
            "date": today,
            "pollinations": 0,
            "huggingface": 0,
            "leonardo": 0,
            "ideogram": 0
        }
    return state

def save_state(state: dict) -> None:
    """Save tracker.json."""
    TRACKER_PATH.write_text(json.dumps(state, indent=2))

def commit_state() -> None:
    """Git add + commit + push tracker.json and bank.json back to repo."""
    repo_root = Path(__file__).parent.parent
    subprocess.run(["git", "add", "state/tracker.json", "prompts/bank.json"], cwd=repo_root, check=True)
    subprocess.run(
        ["git", "commit", "-m", "chore: update tracker and prompt bank [skip ci]"],
        cwd=repo_root,
        check=True
    )
    subprocess.run(["git", "push"], cwd=repo_root, check=True)
