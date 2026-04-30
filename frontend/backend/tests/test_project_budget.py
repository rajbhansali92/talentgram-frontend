"""Regression tests for the project-level budget feature.

Validates:
- `_clean_budget_lines` drops empty rows and coerces shape
- `_public_project_for_talent` strips `client_budget` (talents never see it)
- `_public_project` retains it for admin internal use
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core import (  # noqa: E402
    _clean_budget_lines,
    _public_project,
    _public_project_for_talent,
)


def test_clean_budget_lines_drops_empty_rows():
    lines = [
        {"label": "Shoot fee", "value": "50000"},
        {"label": "", "value": ""},
        {"label": "Usage fee"},
        {"value": "10000"},
        "not a dict",
    ]
    out = _clean_budget_lines(lines)
    assert out == [
        {"label": "Shoot fee", "value": "50000"},
        {"label": "Usage fee", "value": ""},
        {"label": "", "value": "10000"},
    ]


def test_clean_budget_lines_handles_none_and_non_list():
    assert _clean_budget_lines(None) == []
    assert _clean_budget_lines("string") == []
    assert _clean_budget_lines({"label": "x"}) == []


def test_clean_budget_lines_coerces_strings_and_trims():
    lines = [{"label": "  Travel  ", "value": 2500}]
    out = _clean_budget_lines(lines)
    assert out == [{"label": "Travel", "value": "2500"}]


def test_public_project_for_talent_strips_client_budget():
    project = {
        "id": "p1",
        "brand_name": "Acme",
        "talent_budget": [{"label": "Shoot", "value": "20k"}],
        "client_budget": [{"label": "Total", "value": "200k"}],
        "created_by": "admin-1",
    }
    out = _public_project_for_talent(project)
    assert "client_budget" not in out
    assert "created_by" not in out
    assert out["talent_budget"] == [{"label": "Shoot", "value": "20k"}]


def test_public_project_retains_client_budget():
    project = {
        "id": "p1",
        "brand_name": "Acme",
        "client_budget": [{"label": "Total", "value": "200k"}],
        "created_by": "admin-1",
    }
    out = _public_project(project)
    # Admin-facing sanitizer keeps client_budget (strips only _id/created_by)
    assert out["client_budget"] == [{"label": "Total", "value": "200k"}]
    assert "created_by" not in out
