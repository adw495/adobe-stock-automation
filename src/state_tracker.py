import datetime
import json
import subprocess
from pathlib import Path

TRACKER_PATH = Path(__file__).parent.parent / "state" / "tracker.json"

DEFAULT_STATE = {
    "total_generated": 0,
    "total_uploaded": 0,
    "total_approved": 0,
    "daily": {"date": "1970-01-01", "pollinations": 0, "huggingface": 0, "leonardo": 0, "ideogram": 0},
    "uploaded_hashes": [],
    "used_prompt_ids": [],
    "last_prompt_index": 0
}

def load_state() -> dict:
    """Load tracker.json, resetting daily counters if date has changed."""
    try:
        state = json.loads(TRACKER_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return dict(DEFAULT_STATE)

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

    result = subprocess.run(
        ["git", "add", "state/tracker.json", "prompts/bank.json"],
        cwd=repo_root,
        check=True,
        capture_output=True
    )

    try:
        result = subprocess.run(
            ["git", "commit", "-m", "chore: update tracker and prompt bank [skip ci]"],
            cwd=repo_root,
            check=True,
            capture_output=True
        )
    except subprocess.CalledProcessError as e:
        if e.returncode == 1:
            # Nothing to commit — treat as no-op
            return
        if e.stderr:
            print(f"git commit stderr: {e.stderr.decode()}")
        raise

    result = subprocess.run(
        ["git", "push"],
        cwd=repo_root,
        check=True,
        capture_output=True
    )
    if result.returncode != 0 and result.stderr:
        print(f"git push stderr: {result.stderr.decode()}")
