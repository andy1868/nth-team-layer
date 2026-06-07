"""Architect audit M-4 (2026-06-07): marketplace narrowed-except gains KeyError.

The recent narrowing of ``except Exception`` to
``except (OSError, json.JSONDecodeError, TypeError, ValueError)`` in
marketplace.py's listing helpers missed ``KeyError`` - which is the
exact shape thrown by ``Order(**partial_data)`` when a stored order
file is missing a required field (e.g., a v0.9.x order loaded by a
v0.10 reader after a schema rename).

Without KeyError in the list, a single malformed order file kills the
entire ``list_open_orders`` / ``list_orders`` / ``get_stats`` /
``expire_old_orders`` call instead of being skipped.

This test pins:
  * KeyError on individual order parsing is swallowed + logged
  * Other valid orders are still returned
  * Stats computation completes despite the bad file
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nth_dao.marketplace import TaskMarketplace


def _seed_orders(workspace: Path, orders: list[dict]) -> None:
    """Write order JSON files directly into the marketplace dir."""
    orders_dir = workspace / "team_marketplace"
    orders_dir.mkdir(parents=True, exist_ok=True)
    for i, payload in enumerate(orders):
        (orders_dir / f"order_{i:03d}.json").write_text(
            json.dumps(payload), encoding="utf-8",
        )


def test_M4_list_open_orders_skips_records_missing_required_field(tmp_path):
    """An order file missing 'order_id' (the field the canonical
    schema requires) used to crash list_open with KeyError.
    Now it's skipped, the legitimate order is returned."""
    _seed_orders(tmp_path, [
        # Malformed - missing many required fields
        {"context": "shared"},
        # Well-formed - should be returned
        {
            "order_id": "ord-good-001",
            "creator": "alice",
            "capability": "code-review",
            "description": "review a PR",
            "reward": 10.0,
            "status": "open",
            "created_at": "2026-06-07T00:00:00",
            "context": "shared",
        },
    ])
    mp = TaskMarketplace(agent_id="alice", workspace=tmp_path)
    open_orders = mp.list_open()
    # The well-formed order survives; the malformed one is silently skipped.
    assert any(o.order_id == "ord-good-001" for o in open_orders)


def test_M4_stats_computation_survives_malformed_order(tmp_path):
    """get_stats iterates all orders; one corrupt file used to abort
    the whole call. Now the bad file is skipped, stats reflect
    only valid orders."""
    _seed_orders(tmp_path, [
        {"this_is_not_a_valid_order_schema": True},
        {
            "order_id": "ord-002",
            "creator": "alice",
            "capability": "x",
            "description": "x",
            "reward": 5.0,
            "status": "completed",
            "created_at": "2026-06-07T00:00:00",
            "context": "shared",
        },
    ])
    mp = TaskMarketplace(agent_id="alice", workspace=tmp_path)
    stats = mp.stats()
    # We got a stats dict (didn't crash) with the valid order counted
    assert isinstance(stats, dict)
    assert stats.get("completed", 0) == 1
