from __future__ import annotations

import json
import sys
from pathlib import Path

from scripts import score_jobs


def test_dealbreaker_caps_numeric_priority_score(tmp_path: Path, monkeypatch):
    project = tmp_path / "project"
    strategy = project / "strategy"
    strategy.mkdir(parents=True)
    config = {
        "criteria": [
            {"name": "role_fit", "weight": 100},
            {"name": "career_trajectory", "weight": 0},
        ],
        "dealbreaker_criteria": ["career_trajectory"],
        "priority_thresholds": {"high": 70, "medium": 50},
    }
    ratings = [{
        "job_id": "job-1",
        "ratings": {"role_fit": 5, "career_trajectory": 1},
    }]
    config_path = strategy / "scoring-config.json"
    ratings_path = strategy / "job-ratings.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    ratings_path.write_text(json.dumps(ratings), encoding="utf-8")
    monkeypatch.setattr(score_jobs.store_io, "project_dir", lambda: project)
    monkeypatch.setattr(
        sys,
        "argv",
        ["score_jobs.py", str(ratings_path), "--config", str(config_path)],
    )

    assert score_jobs.main() == 0
    result = json.loads((strategy / "job-scores.json").read_text(encoding="utf-8"))[0]
    assert result["priority_score"] == 49.0
    assert result["suggested_priority"] == "low"
    assert result["dealbreaker_hit"] == ["career_trajectory"]
